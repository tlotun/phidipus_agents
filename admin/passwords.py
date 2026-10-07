# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
admin/passwords.py — salted password hashing for the SpiderHub admin panel
═════════════════════════════════════════════════════════════════════════════

v4.3 replaces the old scheme (unsalted SHA-256 + a built-in default
``admin / phidipus`` account) with salted scrypt hashes.

Stored format (config.yaml → admin_auth.password_hash)::

    scrypt$<n>$<r>$<p>$<salt-hex>$<hash-hex>

Legacy 64-char SHA-256 hex hashes are still *verified* so existing installs
keep working, but the admin server warns until they are replaced.

Create a hash::

    ./venv/bin/python -m admin.passwords
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32
_LEGACY_SHA256 = re.compile(r"[0-9a-f]{64}")


def hash_password(password: str) -> str:
    """Return a salted scrypt hash string for *password*."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def is_legacy_hash(stored: str) -> bool:
    return bool(_LEGACY_SHA256.fullmatch(stored or ""))


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of *password* against a stored hash."""
    if not password or not stored:
        return False
    if stored.startswith("scrypt$"):
        try:
            _, n, r, p, salt_hex, dk_hex = stored.split("$")
            dk = hashlib.scrypt(
                password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                n=int(n), r=int(r), p=int(p), dklen=len(dk_hex) // 2,
            )
            return hmac.compare_digest(dk.hex(), dk_hex)
        except (ValueError, TypeError):
            return False
    if is_legacy_hash(stored):
        legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy, stored)
    return False


def _main() -> None:
    import getpass
    pw = getpass.getpass("Mật khẩu admin mới: ")
    if pw != getpass.getpass("Nhập lại: "):
        raise SystemExit("Hai lần nhập không khớp.")
    if len(pw) < 10:
        raise SystemExit("Mật khẩu cần ít nhất 10 ký tự.")
    print("\nThêm vào config.yaml:\n")
    print("admin_auth:")
    print("  username: admin")
    print(f'  password_hash: "{hash_password(pw)}"')


if __name__ == "__main__":
    _main()
