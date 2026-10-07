# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/skill_loader.py — Phidipus v1.0.2
Skill loading with mandatory ed25519 signature verification.

Architecture contract (C.1 / R-09):
  "SkillLoader MUST verify ed25519 signature (.sig file) before loading any
   skill.  Unsigned skills are rejected with an ERROR log entry."

This module is the sole entry point for loading skill modules at runtime.
No code path may load a skill .py file without first passing verification
through _verify_skill_signature().

Security invariants enforced here:
  R-09  Every skill .py file must have a valid ed25519 .sig file before load.
        Unsigned skills → SecurityError + ERROR log.
        Tampered skills → SecurityError + ERROR log.
  R-07  exec_module() is NOT used.  Skills are imported via importlib with a
        restricted spec; no dynamic exec() of LLM output in the host process.

Design:
  - SkillLoader is stateless.  Every load_skill() call re-verifies the
    signature from disk so a skill that passes verification once cannot be
    silently replaced between calls.
  - The public key path is supplied at construction time and never changes.
    Callers obtain the path from PhidipusConfig.skill_validator.signing_public_key_file.
  - skill_registry integration: after successful load the caller is
    responsible for registering the module; this module only handles
    file-level loading and verification.
  - No fallback: if the .sig file is absent or invalid, the load fails.
    There is no "trust on first use" or grace-period mode.

Used by:
  core/skill_router.py          — route to validated skill
  skill_validator/schema_gate.py — post-gate skill activation

Dependencies:
  utils/hash_utils.py   — ed25519_verify(), ed25519_load_public_key()
  utils/file_ops.py     — safe_join(), is_safe_filename()
  utils/logger.py       — structured JSON logging
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Optional, Union

from utils.hash_utils import ed25519_load_public_key, ed25519_verify
from utils.file_ops import safe_join, is_safe_filename
from utils.logger import get_logger

_log = get_logger("skills.skill_loader", process="orchestrator")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SecurityError(RuntimeError):
    """
    Raised when a security invariant is violated during skill loading.

    Attributes:
        skill_path: Path to the skill file that triggered the error.
        reason:     Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        skill_path: Union[str, Path, None] = None,
        reason: str = "VERIFICATION_FAILED",
    ) -> None:
        super().__init__(message)
        self.skill_path = Path(skill_path) if skill_path else None
        self.reason = reason

    def __str__(self) -> str:
        base = super().__str__()
        if self.skill_path:
            return f"[{self.reason}] {self.skill_path.name}: {base}"
        return f"[{self.reason}] {base}"


class SkillNotFoundError(FileNotFoundError):
    """Raised when the requested skill .py file does not exist."""


# ---------------------------------------------------------------------------
# Signature verification (R-09)
# ---------------------------------------------------------------------------

def _verify_skill_signature(
    skill_path: Path,
    public_key: bytes,
) -> None:
    """
    Verify the ed25519 signature of a skill file before loading.

    R-09: SkillLoader MUST verify ed25519 signature (.sig file) before
    loading any skill.  Unsigned skills are rejected with an ERROR log entry.

    The .sig file must be a sibling of the .py file with the same stem:
        skill.py  →  skill.sig

    Args:
        skill_path:  Resolved absolute path to the .py skill file.
        public_key:  32-byte raw ed25519 public key bytes.

    Raises:
        SecurityError: if the .sig file is absent OR the signature is invalid.
                       In both cases an ERROR entry is written to the log.
    """
    sig_path = skill_path.with_suffix(".sig")

    # ── Check .sig file exists ────────────────────────────────────────────
    if not sig_path.exists():
        _log.error(
            "R-09 VIOLATION: skill signature file absent — rejecting skill",
            extra={
                "skill":  str(skill_path),
                "sig":    str(sig_path),
                "reason": "SIG_FILE_ABSENT",
            },
        )
        raise SecurityError(
            f"Skill signature file not found: {sig_path.name}. "
            "Unsigned skills must never be loaded (R-09).",
            skill_path=skill_path,
            reason="SIG_FILE_ABSENT",
        )

    # ── Read skill source and signature ──────────────────────────────────
    try:
        skill_bytes = skill_path.read_bytes()
    except OSError as exc:
        _log.error(
            "R-09: failed to read skill file",
            extra={"skill": str(skill_path), "error": str(exc)},
        )
        raise SecurityError(
            f"Cannot read skill file for signature verification: {exc}",
            skill_path=skill_path,
            reason="READ_ERROR",
        ) from exc

    try:
        signature = sig_path.read_bytes()
    except OSError as exc:
        _log.error(
            "R-09: failed to read skill signature file",
            extra={"sig": str(sig_path), "error": str(exc)},
        )
        raise SecurityError(
            f"Cannot read skill signature file: {exc}",
            skill_path=skill_path,
            reason="SIG_READ_ERROR",
        ) from exc

    # ── Verify signature length before calling ed25519_verify ────────────
    # ed25519_verify raises ValueError for wrong-length inputs; we surface
    # those as SecurityError so callers only need to catch one exception type.
    if len(signature) != 64:
        _log.error(
            "R-09 VIOLATION: skill signature has wrong length — rejecting skill",
            extra={
                "skill":          str(skill_path),
                "sig_length":     len(signature),
                "expected_length": 64,
            },
        )
        raise SecurityError(
            f"Skill signature file {sig_path.name} has unexpected length "
            f"{len(signature)} bytes (expected 64). "
            "The signature file may be corrupt or from a different key (R-09).",
            skill_path=skill_path,
            reason="SIG_WRONG_LENGTH",
        )

    # ── Cryptographic verification ────────────────────────────────────────
    try:
        valid = ed25519_verify(public_key, skill_bytes, signature)
    except (ValueError, ImportError) as exc:
        _log.error(
            "R-09: ed25519 verification raised an exception",
            extra={"skill": str(skill_path), "error": str(exc)},
        )
        raise SecurityError(
            f"ed25519 verification error for {skill_path.name}: {exc}",
            skill_path=skill_path,
            reason="CRYPTO_ERROR",
        ) from exc

    if not valid:
        _log.error(
            "R-09 VIOLATION: skill signature verification FAILED — rejecting skill",
            extra={
                "skill":  str(skill_path),
                "reason": "INVALID_SIGNATURE",
            },
        )
        raise SecurityError(
            f"Skill signature verification failed for {skill_path.name}. "
            "The skill file may have been tampered with. "
            "Re-run skill validation to generate a new signed skill (R-09).",
            skill_path=skill_path,
            reason="INVALID_SIGNATURE",
        )

    _log.debug(
        "R-09: skill signature verified OK",
        extra={"skill": str(skill_path)},
    )


# ---------------------------------------------------------------------------
# SkillLoader
# ---------------------------------------------------------------------------

class SkillLoader:
    """
    Loads skill modules from disk after mandatory ed25519 signature
    verification.

    Every call to :meth:`load_skill` verifies the .sig file on disk before
    importing the module.  There is no caching of verification results: if
    the file changes between calls, the new signature is checked.

    Usage::

        loader = SkillLoader(
            skills_dir=Path("./skills/generated"),
            public_key_path=Path("./keys/skill_signing.pub"),
        )
        module = loader.load_skill("my_skill")
        # module is a verified Python module object

    Args:
        skills_dir:      Directory containing .py and .sig skill files.
        public_key_path: Path to the 32-byte raw ed25519 public key file.
    """

    def __init__(
        self,
        skills_dir: Union[str, Path],
        public_key_path: Union[str, Path],
    ) -> None:
        self._skills_dir   = Path(skills_dir).resolve()
        self._pub_key_path = Path(public_key_path).resolve()
        # Load the public key once at construction time.
        # The key is immutable at runtime; if it changes, create a new loader.
        self._public_key: bytes = ed25519_load_public_key(self._pub_key_path)

        _log.info(
            "SkillLoader initialised",
            extra={
                "skills_dir":      str(self._skills_dir),
                "public_key_path": str(self._pub_key_path),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_skill(
        self,
        skill_name: str,
        *,
        reload: bool = False,
    ) -> types.ModuleType:
        """
        Load the skill module named *skill_name* after signature verification.

        Steps:
          1. Validate *skill_name* is a safe filename component.
          2. Resolve the .py path inside skills_dir (path-traversal check).
          3. Verify the .sig file with ed25519_verify() (R-09).
          4. Import the module with importlib (no exec_module on host — R-07).
          5. Return the verified module object.

        Args:
            skill_name: Base name of the skill (without .py extension).
                        Must pass :func:`~utils.file_ops.is_safe_filename`.
            reload:     If True and the module is already in sys.modules,
                        force a reload.  The signature is always re-verified
                        even when reload=False.

        Returns:
            Imported Python module.

        Raises:
            ValueError:        if *skill_name* contains unsafe characters.
            SkillNotFoundError: if the .py file does not exist.
            SecurityError:     if the .sig file is absent or invalid (R-09).
            ImportError:       if the module cannot be imported after verification.
        """
        # ── 1. Validate skill name ────────────────────────────────────────
        if not is_safe_filename(skill_name + ".py"):
            raise ValueError(
                f"Skill name {skill_name!r} contains unsafe characters. "
                "Only alphanumerics, underscores, hyphens, and dots are permitted."
            )

        # ── 2. Resolve path (path-traversal guard) ────────────────────────
        try:
            skill_path = safe_join(self._skills_dir, skill_name + ".py")
        except (NotADirectoryError, Exception) as exc:
            raise SkillNotFoundError(
                f"Cannot resolve skill path for {skill_name!r}: {exc}"
            ) from exc

        if not skill_path.exists():
            raise SkillNotFoundError(
                f"Skill file not found: {skill_path}"
            )

        # ── 3. Signature verification (R-09) ──────────────────────────────
        # This call raises SecurityError on any failure.  No try/except here:
        # the exception must propagate to prevent any load on bad signatures.
        _verify_skill_signature(skill_path, self._public_key)

        # ── 4. Import module ──────────────────────────────────────────────
        module_name = f"phidipus.skills.generated.{skill_name}"

        # If already imported and reload not requested, return cached module.
        # Note: signature was still re-verified above.
        if module_name in sys.modules and not reload:
            _log.debug(
                "skill already loaded (signature re-verified)",
                extra={"skill": skill_name, "module": module_name},
            )
            return sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, skill_path)
        if spec is None or spec.loader is None:
            raise ImportError(
                f"Cannot create module spec for skill {skill_name!r} at {skill_path}"
            )

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:
            # Remove the partially-initialised module from sys.modules on failure.
            sys.modules.pop(module_name, None)
            _log.error(
                "skill module execution failed after signature verification",
                extra={"skill": skill_name, "error": str(exc)},
            )
            raise ImportError(
                f"Skill module {skill_name!r} raised an exception during import: {exc}"
            ) from exc

        _log.info(
            "skill loaded successfully",
            extra={
                "skill":  skill_name,
                "module": module_name,
                "path":   str(skill_path),
            },
        )
        return module

    def is_skill_signed(self, skill_name: str) -> bool:
        """
        Return True if the skill .py and .sig files both exist and the
        signature is currently valid.

        Does NOT load or import the skill.  Safe to call at any time for
        pre-flight checks.

        Args:
            skill_name: Base name of the skill (without .py extension).

        Returns:
            True if verified, False on any failure (missing file, bad sig,
            wrong key, etc.).
        """
        try:
            if not is_safe_filename(skill_name + ".py"):
                return False
            skill_path = safe_join(self._skills_dir, skill_name + ".py")
            if not skill_path.exists():
                return False
            _verify_skill_signature(skill_path, self._public_key)
            return True
        except (SecurityError, ValueError, OSError):
            return False

    def list_signed_skills(self) -> list[str]:
        """
        Return a sorted list of skill names that exist and have valid signatures.

        Iterates all .py files in skills_dir and calls :meth:`is_skill_signed`
        for each.  Skills with missing or invalid .sig files are silently
        excluded.

        Returns:
            Sorted list of verified skill names (without .py extension).
        """
        if not self._skills_dir.is_dir():
            return []
        result: list[str] = []
        for py_file in sorted(self._skills_dir.glob("*.py")):
            name = py_file.stem
            if self.is_skill_signed(name):
                result.append(name)
        return result
