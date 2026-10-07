# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/hash_utils.py — Phidipus v1.0.2
Cryptographic helpers: SHA-256 digests, HMAC-SHA256 signing/verification,
ed25519 key generation and sign/verify, constant-time comparison, and
installation key generation.

Used by:
  memory_integrity/memory_guard.py  — episode HMAC signing (R-16)
  memory_integrity/memory_guard.py  — startup manifest check (R-20)
  ipc/action_log.py                 — audit-log HMAC (R-05 support)
  patcher/runtime_patcher.py        — patch-ledger per-entry hash (R-18)
  skill_validator/skill_signer.py   — ed25519 signing (R-09, R-17)
  installer                         — generate_installation_keys() (FIX-6)

Security guarantees:
  - Constant-time comparison via hmac.compare_digest() for all MAC/signature
    verification to prevent timing-oracle attacks.
  - HMAC keys are passed by the caller; this module never stores key material.
  - ed25519 uses stdlib `cryptography` when available; raises ImportError with
    a clear message if the package is absent.  Callers that require signing must
    ensure `cryptography` is installed (pip install cryptography).
  - No external dependencies for SHA-256 / HMAC paths — stdlib only.

Patch v9.11.1:
  FIX-6  Added generate_installation_keys() — a one-call helper for the
         installer that generates and writes all key files required by a
         Phidipus v1.0 installation with correct file permissions.
         This function is intentionally NOT called by any library code;
         it is an installer-only entry point.

Patch v9.11.2:
  TASK-4  Added PHIDIPUS_INSTALL_MODE environment guard at the top of
          generate_installation_keys().  The function now raises
          RuntimeError immediately if the guard variable is absent or not
          set to "1", preventing accidental runtime key regeneration.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Union

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

BytesLike = Union[bytes, bytearray, memoryview]


# ---------------------------------------------------------------------------
# SHA-256 helpers
# ---------------------------------------------------------------------------

def sha256_bytes(data: BytesLike) -> bytes:
    """Return the raw 32-byte SHA-256 digest of *data*."""
    return hashlib.sha256(data).digest()


def sha256_hex(data: BytesLike) -> str:
    """Return the lowercase hex-encoded SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Union[str, Path], *, chunk_size: int = 1 << 20) -> str:
    """
    Return the lowercase hex SHA-256 digest of the file at *path*.

    Reads in *chunk_size*-byte chunks so arbitrarily large files are handled
    without loading the entire content into memory.

    Raises:
        FileNotFoundError: if *path* does not exist.
        OSError:           on read failure.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_verify_file(path: Union[str, Path], expected_hex: str) -> bool:
    """
    Return True iff the SHA-256 of the file at *path* matches *expected_hex*.
    Uses constant-time comparison to prevent timing attacks.

    Args:
        path:         File to hash.
        expected_hex: Lowercase hex digest to compare against.

    Returns:
        True on match, False on mismatch or read error.
    """
    try:
        actual = sha256_file(path)
    except OSError:
        return False
    return hmac.compare_digest(
        actual.encode("ascii"),
        expected_hex.lower().encode("ascii"),
    )


# ---------------------------------------------------------------------------
# HMAC-SHA256 helpers
# ---------------------------------------------------------------------------

def hmac_sign(key: bytes, data: BytesLike) -> bytes:
    """
    Return a 32-byte HMAC-SHA256 MAC over *data* using *key*.

    Args:
        key:  Secret key bytes (caller is responsible for secure generation).
        data: Payload to authenticate.

    Returns:
        Raw 32-byte MAC.
    """
    return hmac.new(key, data, hashlib.sha256).digest()


def hmac_sign_hex(key: bytes, data: BytesLike) -> str:
    """Return a lowercase hex-encoded HMAC-SHA256 MAC."""
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def hmac_verify(key: bytes, data: BytesLike, mac: bytes) -> bool:
    """
    Verify *mac* is the correct HMAC-SHA256 of *data* under *key*.
    Uses constant-time comparison.

    Args:
        key:  The same secret key used to produce *mac*.
        data: The payload that was authenticated.
        mac:  The 32-byte MAC to verify.

    Returns:
        True if valid, False if tampered or wrong key.
    """
    expected = hmac.new(key, data, hashlib.sha256).digest()
    return hmac.compare_digest(expected, bytes(mac))


def hmac_verify_hex(key: bytes, data: BytesLike, mac_hex: str) -> bool:
    """
    Verify a hex-encoded HMAC-SHA256 MAC.

    Returns:
        True if valid, False otherwise.
    """
    expected_hex = hmac.new(key, data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(
        expected_hex.encode("ascii"),
        mac_hex.lower().encode("ascii"),
    )


# ---------------------------------------------------------------------------
# Constant-time comparison
# ---------------------------------------------------------------------------

def constant_time_eq(a: Union[bytes, str], b: Union[bytes, str]) -> bool:
    """
    Compare two byte strings or str objects in constant time.

    Both arguments must be the same type.  Mixing bytes and str raises
    TypeError (mirrors hmac.compare_digest semantics).

    Returns:
        True iff a == b.
    """
    if type(a) is not type(b):
        raise TypeError(
            f"constant_time_eq requires both arguments to be the same type, "
            f"got {type(a).__name__} and {type(b).__name__}"
        )
    return hmac.compare_digest(a, b)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Secure random key generation
# ---------------------------------------------------------------------------

def generate_key(n_bytes: int = 32) -> bytes:
    """
    Return *n_bytes* cryptographically random bytes suitable for use as an
    HMAC key.  Uses os.urandom() which reads from the OS CSPRNG.

    Args:
        n_bytes: Number of bytes.  Must be >= 16.

    Raises:
        ValueError: if n_bytes < 16.
    """
    if n_bytes < 16:
        raise ValueError(f"Key length must be >= 16 bytes, got {n_bytes}")
    return os.urandom(n_bytes)


# ---------------------------------------------------------------------------
# ed25519 signing / verification
# ---------------------------------------------------------------------------
# These functions require the `cryptography` package (not stdlib).
# They are placed here rather than in a separate module so that all
# cryptographic primitives live in one auditable location.

def _get_ed25519() -> tuple:
    """
    Lazily import ed25519 primitives from the `cryptography` package.

    Returns:
        (Ed25519PrivateKey, Ed25519PublicKey) classes.

    Raises:
        ImportError: if `cryptography` is not installed.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        return Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as exc:
        raise ImportError(
            "ed25519 signing requires the 'cryptography' package: "
            "pip install cryptography"
        ) from exc


def ed25519_generate_keypair() -> tuple[bytes, bytes]:
    """
    Generate a new ed25519 keypair.

    Returns:
        (private_key_bytes, public_key_bytes) where both are raw 32-byte
        representations in the RFC 8032 format.

    Raises:
        ImportError: if `cryptography` is not installed.
    """
    Ed25519PrivateKey, _ = _get_ed25519()
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption,
    )
    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes, pub_bytes


def ed25519_sign(private_key_bytes: bytes, data: BytesLike) -> bytes:
    """
    Sign *data* with a 32-byte raw ed25519 private key.

    Args:
        private_key_bytes: 32-byte raw private key (from ed25519_generate_keypair).
        data:              Payload to sign.

    Returns:
        64-byte ed25519 signature.

    Raises:
        ImportError: if `cryptography` is not installed.
        ValueError:  if private_key_bytes is not 32 bytes.
    """
    if len(private_key_bytes) != 32:
        raise ValueError(
            f"ed25519 private key must be 32 bytes, got {len(private_key_bytes)}"
        )
    Ed25519PrivateKey, _ = _get_ed25519()
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    priv = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return priv.sign(bytes(data))


def ed25519_verify(public_key_bytes: bytes, data: BytesLike, signature: bytes) -> bool:
    """
    Verify an ed25519 *signature* over *data* using a 32-byte raw public key.

    Args:
        public_key_bytes: 32-byte raw public key (from ed25519_generate_keypair).
        data:             Payload that was signed.
        signature:        64-byte signature to verify.

    Returns:
        True if the signature is valid, False otherwise.
        Never raises on signature mismatch — returns False instead.

    Raises:
        ImportError: if `cryptography` is not installed.
        ValueError:  if public_key_bytes is not 32 bytes or signature is not 64 bytes.
    """
    if len(public_key_bytes) != 32:
        raise ValueError(
            f"ed25519 public key must be 32 bytes, got {len(public_key_bytes)}"
        )
    if len(signature) != 64:
        raise ValueError(
            f"ed25519 signature must be 64 bytes, got {len(signature)}"
        )
    _, Ed25519PublicKey = _get_ed25519()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from cryptography.exceptions import InvalidSignature
    pub = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        pub.verify(signature, bytes(data))
        return True
    except InvalidSignature:
        return False


def ed25519_load_private_key(path: Union[str, Path]) -> bytes:
    """
    Load a raw 32-byte ed25519 private key from a binary file.

    The file must contain exactly 32 bytes.

    Raises:
        ValueError: if the file is not exactly 32 bytes.
        OSError:    on read failure.
    """
    data = Path(path).read_bytes()
    if len(data) != 32:
        raise ValueError(
            f"Expected 32-byte ed25519 private key in {path}, got {len(data)} bytes"
        )
    return data


def ed25519_load_public_key(path: Union[str, Path]) -> bytes:
    """
    Load a raw 32-byte ed25519 public key from a binary file.

    The file must contain exactly 32 bytes.

    Raises:
        ValueError: if the file is not exactly 32 bytes.
        OSError:    on read failure.
    """
    data = Path(path).read_bytes()
    if len(data) != 32:
        raise ValueError(
            f"Expected 32-byte ed25519 public key in {path}, got {len(data)} bytes"
        )
    return data


# ---------------------------------------------------------------------------
# Installer key generation helper (FIX-6)
# ---------------------------------------------------------------------------

def generate_installation_keys(
    keys_dir: Union[str, Path],
    *,
    overwrite: bool = False,
) -> dict[str, Path]:
    """
    Generate all cryptographic key files required for a Phidipus v1.0
    installation and write them to *keys_dir* with secure permissions.

    This function is an **installer-only** entry point.  It must NOT be
    called by any library or agent code at runtime — key material is
    generated once during installation and loaded at startup from the files
    created here.

    Environment guard (v9.11.2 TASK-4):
        The environment variable ``PHIDIPUS_INSTALL_MODE`` must be set to
        ``"1"`` before calling this function.  If it is absent or has any
        other value, :exc:`RuntimeError` is raised immediately and no key
        material is generated.  This prevents accidental runtime invocation
        from agent code, evolved skills, or mutation prompts.

    Files created
    -------------
    ``skill_signing.priv``
        32-byte raw ed25519 private key used by
        ``skill_validator/skill_signer.py`` to sign validated skills (R-09,
        R-17).  Written with mode **0600** (owner read/write only).

    ``skill_signing.pub``
        32-byte raw ed25519 public key used by ``skills/skill_loader.py`` to
        verify skill signatures before loading (R-09).  Written with mode
        **0644** (world-readable; this is intentional — it is the *public*
        key).

    ``memory_hmac.key``
        32-byte random HMAC key used by
        ``memory_integrity/memory_guard.py`` to sign and verify every
        episodic memory entry (R-16).  Written with mode **0600**.

    Args:
        keys_dir:  Directory in which to write the key files.  Created with
                   mode 0700 if it does not already exist.
        overwrite: If False (default), raises :exc:`FileExistsError` if any
                   of the three key files already exist.  Pass
                   ``overwrite=True`` only when intentionally regenerating
                   keys (e.g. during a re-installation).  Regenerating keys
                   invalidates all previously signed skills and memory
                   episodes.

    Returns:
        A ``dict`` mapping each key filename to its resolved :class:`Path`::

            {
                "skill_signing.priv": Path("/path/to/keys/skill_signing.priv"),
                "skill_signing.pub":  Path("/path/to/keys/skill_signing.pub"),
                "memory_hmac.key":    Path("/path/to/keys/memory_hmac.key"),
            }

    Raises:
        RuntimeError:    if PHIDIPUS_INSTALL_MODE != "1" (v9.11.2 guard).
        FileExistsError: if any key file already exists and *overwrite* is
                         False.
        ImportError:     if the ``cryptography`` package is not installed.
        OSError:         on any filesystem I/O failure.
    """
    # ── Installer-mode guard (v9.11.2 TASK-4) ────────────────────────────────
    # Key generation must only run during installation.  Any call from runtime
    # code (agent loop, skill evolution, mutation prompts, etc.) is a security
    # violation and must be blocked here before any key material is touched.
    if os.environ.get("PHIDIPUS_INSTALL_MODE") != "1":
        raise RuntimeError(
            "Installation key generation is only allowed in installer mode. "
            "Set the environment variable PHIDIPUS_INSTALL_MODE=1 before "
            "calling generate_installation_keys(). "
            "This function must never be called from runtime agent code."
        )

    keys_dir = Path(keys_dir).resolve()
    # Create the keys directory with restrictive permissions if absent.
    keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    priv_path = keys_dir / "skill_signing.priv"
    pub_path  = keys_dir / "skill_signing.pub"
    hmac_path = keys_dir / "memory_hmac.key"

    # Guard: refuse to overwrite existing keys unless explicitly permitted.
    if not overwrite:
        existing = [p for p in (priv_path, pub_path, hmac_path) if p.exists()]
        if existing:
            names = ", ".join(str(p.name) for p in existing)
            raise FileExistsError(
                f"Key file(s) already exist in {keys_dir}: {names}. "
                "Pass overwrite=True to regenerate (this invalidates all "
                "previously signed skills and memory episodes)."
            )

    # ── ed25519 keypair for skill signing (R-09, R-17) ────────────────────
    priv_bytes, pub_bytes = ed25519_generate_keypair()

    # ── HMAC key for episodic memory integrity (R-16) ─────────────────────
    hmac_key_bytes = generate_key(32)

    # ── Write files with secure permissions ───────────────────────────────
    # Private key — mode 0600: only the owning user may read or write.
    priv_path.write_bytes(priv_bytes)
    os.chmod(priv_path, 0o600)

    # Public key — mode 0644: world-readable (it is the public component).
    pub_path.write_bytes(pub_bytes)
    os.chmod(pub_path, 0o644)

    # HMAC key — mode 0600: only the owning user may read or write.
    hmac_path.write_bytes(hmac_key_bytes)
    os.chmod(hmac_path, 0o600)

    return {
        "skill_signing.priv": priv_path,
        "skill_signing.pub":  pub_path,
        "memory_hmac.key":    hmac_path,
    }
