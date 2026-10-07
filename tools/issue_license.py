#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tools/issue_license.py — issue Phidipus COMMERCIAL licenses (copyright holder only)
═══════════════════════════════════════════════════════════════════════════════════

    # 1. once: create your signing key (keep it secret, back it up, never commit it)
    ./venv/bin/python tools/issue_license.py keygen
    #    → paste the printed public key into core/license_manager.py PUBLIC_KEYS

    # 2. per customer
    ./venv/bin/python tools/issue_license.py issue --licensee "Công ty ABC" \\
        --email it@example.com --seats 5 --days 365 --out license-abc.json

    # 3. check a file
    ./venv/bin/python tools/issue_license.py verify license-abc.json

The customer copies the file to data/license.json (or pastes it in the admin
panel).  Verification is offline: no server is needed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DEFAULT_KEY = Path.home() / ".phidipus-license" / "signing.key"


def _load_private(path: Path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def _public_hex(priv) -> str:
    from cryptography.hazmat.primitives import serialization
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def cmd_keygen(a: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = Path(a.key).expanduser()
    if path.exists() and not a.force:
        print(f"Đã có khoá {path} (dùng --force để tạo khoá mới — giấy phép cũ sẽ không còn hợp lệ).")
        print("Public key:", _public_hex(_load_private(path)))
        return 1
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    path.write_bytes(priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption()))
    os.chmod(path, 0o600)
    print(f"Khoá ký riêng: {path}  (BÍ MẬT — sao lưu cẩn thận, không đưa lên GitHub)")
    print("Thêm public key này vào core/license_manager.py → PUBLIC_KEYS:")
    print(f'    PUBLIC_KEYS: tuple[str, ...] = ("{_public_hex(priv)}",)')
    return 0


def make_license(priv, licensee: str, email: str, seats: int, days: int, notes: str = "") -> dict:
    from core.license_manager import canonical_payload
    now = time.time()
    doc = {
        "type": "commercial",
        "product": "Phidipus Agents",
        "license_id": uuid.uuid4().hex[:16],
        "licensee": licensee,
        "email": email,
        "seats": seats,
        "issued_at": int(now),
        "expires_at": int(now + days * 86400) if days > 0 else 0,
        "notes": notes,
    }
    doc["signature"] = base64.b64encode(priv.sign(canonical_payload(doc))).decode()
    return doc


def cmd_issue(a: argparse.Namespace) -> int:
    priv = _load_private(Path(a.key).expanduser())
    doc = make_license(priv, a.licensee, a.email, a.seats, a.days, a.notes)
    out = Path(a.out)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Đã tạo {out} cho {a.licensee} ({a.seats} chỗ, "
          f"{'vĩnh viễn' if not a.days else str(a.days) + ' ngày'}) — id {doc['license_id']}")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    from core.license_manager import PUBLIC_KEYS, verify_document
    doc = json.loads(Path(a.file).read_text(encoding="utf-8"))
    keys = PUBLIC_KEYS
    if a.key:
        keys = (_public_hex(_load_private(Path(a.key).expanduser())),)
    ok, err = verify_document(doc, keys)
    print(("HỢP LỆ — " + doc.get("licensee", "")) if ok else ("KHÔNG HỢP LỆ — " + err))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phidipus Agents commercial license tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--key", default=str(DEFAULT_KEY))
    k.add_argument("--force", action="store_true")
    i = sub.add_parser("issue")
    i.add_argument("--key", default=str(DEFAULT_KEY))
    i.add_argument("--licensee", required=True)
    i.add_argument("--email", default="")
    i.add_argument("--seats", type=int, default=1)
    i.add_argument("--days", type=int, default=365, help="0 = perpetual")
    i.add_argument("--notes", default="")
    i.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("file")
    v.add_argument("--key", default="", help="verify against this private key's public half")
    a = ap.parse_args(argv)
    return {"keygen": cmd_keygen, "issue": cmd_issue, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
