# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/license_manager.py — Phidipus licensing (v4.3)
═══════════════════════════════════════════════════════

Phidipus is source-available under the PolyForm Noncommercial License 1.0.0
(see LICENSE / COMMERCIAL.md):

  * personal, research, education, non-profit and other noncommercial use:
    free — no key, no sign-up;
  * commercial use: needs a commercial license from the copyright holder.

A commercial license is a small JSON document signed OFFLINE by the copyright
holder with an Ed25519 key (tools/issue_license.py).  This module only
verifies it locally:

  * no network calls, no telemetry, no device fingerprinting;
  * no feature is locked — licensing is a legal agreement, not DRM.

The previous version contacted a non-existent server (api.phidipus.com),
sent the hostname + a hardware fingerprint and printed a fake "7-day trial".

License file: config.yaml → license.file, else data/license.json.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Optional

_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_FILE = _BASE_DIR / "data" / "license.json"
LICENSE_SPDX = "PolyForm-Noncommercial-1.0.0"

#: Hex-encoded Ed25519 public keys of the copyright holder.  Generate with
#: ``./venv/bin/python tools/issue_license.py keygen`` and paste the printed key
#: here (the private key never goes into the repository).
PUBLIC_KEYS: tuple[str, ...] = (
    "e15f3cc6081609bd9ee46010ab00f1f0e8b12b9498eacbdc019f39871b2d53c1",   # Phidipus Agents — commercial license key (2026)
)


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


def canonical_payload(doc: dict[str, Any]) -> bytes:
    """Bytes that are signed: the document without its signature, canonical JSON."""
    body = {k: v for k, v in doc.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_document(doc: dict[str, Any], public_keys: tuple[str, ...] | list[str] | None = None) -> tuple[bool, str]:
    """(ok, error) — signature, type and expiry checks."""
    keys = tuple(public_keys if public_keys is not None else PUBLIC_KEYS)
    if not keys:
        return False, "chưa cấu hình khoá công khai (PUBLIC_KEYS)"
    if doc.get("type") != "commercial":
        return False, "không phải giấy phép thương mại"
    sig = doc.get("signature", "")
    try:
        raw_sig = base64.b64decode(sig, validate=True)
    except (ValueError, TypeError):
        return False, "chữ ký không hợp lệ"
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False, "thiếu gói cryptography"
    payload = canonical_payload(doc)
    for hex_key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key)).verify(raw_sig, payload)
            break
        except (InvalidSignature, ValueError):
            continue
    else:
        return False, "chữ ký không khớp khoá của Phidipus Agents"
    expires = float(doc.get("expires_at", 0) or 0)
    if expires and expires < time.time():
        return False, "giấy phép đã hết hạn"
    return True, ""


class LicenseInfo:
    """Current licensing state (attribute names kept for older callers)."""

    def __init__(self) -> None:
        self.valid = False                  # a commercial license is present and valid
        self.tier = "noncommercial"         # "noncommercial" | "commercial"
        self.licensee = ""
        self.email = ""
        self.seats = 0
        self.license_id = ""
        self.issued_at = 0.0
        self.expires_at = 0.0
        self.error = ""
        self.features: dict[str, Any] = {"name": "Noncommercial (PolyForm NC 1.0.0)"}

    def is_expired(self) -> bool:
        return bool(self.expires_at) and self.expires_at < time.time()

    def days_left(self) -> int:
        if not self.expires_at:
            return -1                       # perpetual
        return max(0, int((self.expires_at - time.time()) / 86400))

    def can_use(self, feature: str) -> bool:
        return True                         # nothing is feature-locked

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid, "tier": self.tier, "license": LICENSE_SPDX,
            "licensee": self.licensee, "seats": self.seats, "license_id": self.license_id,
            "expires_at": self.expires_at, "days_left": self.days_left(), "error": self.error,
            "commercial_use": "allowed" if self.valid else "requires a commercial license (COMMERCIAL.md)",
        }


class LicenseManager:
    def __init__(self) -> None:
        self._license = LicenseInfo()

    @property
    def license(self) -> LicenseInfo:
        return self._license

    @property
    def is_active(self) -> bool:
        return True

    @property
    def tier(self) -> str:
        return self._license.tier

    def _license_path(self) -> Path:
        try:
            import yaml
            cfg = yaml.safe_load((_BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
            p = ((cfg.get("license") or {}).get("file") or "").strip()
            if p:
                path = Path(p).expanduser()
                return path if path.is_absolute() else _BASE_DIR / path
        except Exception:
            pass
        return _DEFAULT_FILE

    def _apply(self, doc: dict[str, Any]) -> tuple[bool, str]:
        ok, err = verify_document(doc)
        info = LicenseInfo()
        if ok:
            info.valid = True
            info.tier = "commercial"
            info.licensee = str(doc.get("licensee", ""))
            info.email = str(doc.get("email", ""))
            info.seats = int(doc.get("seats", 0) or 0)
            info.license_id = str(doc.get("license_id", ""))
            info.issued_at = float(doc.get("issued_at", 0) or 0)
            info.expires_at = float(doc.get("expires_at", 0) or 0)
            info.features = {"name": f"Commercial — {info.licensee}"}
        else:
            info.error = err
        self._license = info
        return ok, err

    async def initialize(self) -> LicenseInfo:
        path = self._license_path()
        if path.exists():
            try:
                self._apply(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                self._license.error = f"không đọc được {path.name}: {exc}"
        return self._license

    async def activate_key(self, key: str, email: str = "") -> dict[str, Any]:
        """Install a license: *key* is the license JSON text (or base64 of it)."""
        text = key.strip()
        if not text.startswith("{"):
            try:
                text = base64.b64decode(text, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return {"ok": False, "error": "dữ liệu giấy phép không hợp lệ"}
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": "dữ liệu giấy phép không phải JSON"}
        ok, err = self._apply(doc)
        if not ok:
            return {"ok": False, "error": err}
        path = self._license_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)
        return {"ok": True, **self._license.to_dict()}

    async def verify_key(self, key: str) -> bool:
        return (await self.activate_key(key)).get("ok", False)

    def check_feature(self, feature: str) -> dict[str, Any]:
        return {"allowed": True, "tier": self._license.tier, "feature": feature, "upgrade_url": ""}

    def stats(self) -> dict[str, Any]:
        return self._license.to_dict()


_manager: Optional[LicenseManager] = None


async def get_license_manager() -> LicenseManager:
    global _manager
    if _manager is None:
        _manager = LicenseManager()
        await _manager.initialize()
    return _manager


def get_license_sync() -> LicenseManager | None:
    return _manager
