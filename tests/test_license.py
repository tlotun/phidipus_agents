# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""tests/test_license.py — offline Ed25519 commercial licenses."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from core import license_manager as lm  # noqa: E402
from tools.issue_license import _public_hex, make_license  # noqa: E402


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.priv = Ed25519PrivateKey.generate()
        self.pub = _public_hex(self.priv)

    def test_valid_and_tampered(self):
        doc = make_license(self.priv, "Công ty ABC", "it@example.com", 5, 30)
        self.assertEqual(lm.verify_document(doc, [self.pub]), (True, ""))
        bad = dict(doc, seats=500)
        self.assertFalse(lm.verify_document(bad, [self.pub])[0])
        other = _public_hex(Ed25519PrivateKey.generate())
        self.assertFalse(lm.verify_document(doc, [other])[0])
        self.assertFalse(lm.verify_document(doc, [])[0])            # no key configured

    def test_expired(self):
        doc = make_license(self.priv, "X", "", 1, 1)
        doc["expires_at"] = int(time.time()) - 10
        from core.license_manager import canonical_payload
        import base64
        doc["signature"] = base64.b64encode(self.priv.sign(canonical_payload(doc))).decode()
        self.assertEqual(lm.verify_document(doc, [self.pub]), (False, "giấy phép đã hết hạn"))

    def test_manager_activation_writes_file(self):
        doc = make_license(self.priv, "ABC", "", 2, 0)
        with tempfile.TemporaryDirectory() as tmp:
            mgr = lm.LicenseManager()
            mgr._license_path = lambda: Path(tmp) / "license.json"
            orig = lm.PUBLIC_KEYS
            lm.PUBLIC_KEYS = (self.pub,)
            try:
                res = asyncio.run(mgr.activate_key(json.dumps(doc)))
                self.assertTrue(res["ok"])
                self.assertEqual(res["tier"], "commercial")
                self.assertEqual(res["days_left"], -1)                # perpetual
                self.assertEqual((Path(tmp) / "license.json").stat().st_mode & 0o777, 0o600)
                self.assertFalse(asyncio.run(mgr.activate_key("{not json"))["ok"])
            finally:
                lm.PUBLIC_KEYS = orig

    def test_default_is_noncommercial_without_network(self):
        mgr = lm.LicenseManager()
        mgr._license_path = lambda: Path("/nonexistent/license.json")
        info = asyncio.run(mgr.initialize())
        self.assertEqual(info.tier, "noncommercial")
        self.assertTrue(info.can_use("anything"))


if __name__ == "__main__":
    unittest.main()
