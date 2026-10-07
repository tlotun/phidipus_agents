# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tests/test_try_command.py — dry-run tool used by contributors (tools/try_command.py)

Run:  ./venv/bin/python -m unittest tests.test_try_command -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.keyboard_first import ShortcutCatalog  # noqa: E402
from tools.try_command import explain  # noqa: E402


class TryCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = ShortcutCatalog()

    def test_shortcut_with_app_in_command(self):
        out = explain(self.catalog, "mở tab mới trong chrome")
        self.assertIn("chromium.new_tab", out)
        self.assertIn("⌘T", out)

    def test_macro_parameters_are_filled(self):
        out = explain(self.catalog, "đi tới thư mục ~/Downloads", "finder")
        self.assertIn('path="~/Downloads"', out)
        self.assertIn("type           ~/Downloads", out)

    def test_risky_and_unknown_commands(self):
        self.assertIn("risk=high", explain(self.catalog, "chuyển vào thùng rác", "finder"))
        self.assertIn("không có trong danh mục", explain(self.catalog, "đặt vé máy bay đi Đà Nẵng"))


if __name__ == "__main__":
    unittest.main()
