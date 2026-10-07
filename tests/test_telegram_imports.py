# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tests/test_telegram_imports.py — the project's telegram/ package vs python-telegram-bot

telegram_bot.py points sys.modules["telegram"] at the python-telegram-bot library;
the project's own telegram.telegram_config must stay importable whatever the order.

Run:  ./venv/bin/python -m unittest tests.test_telegram_imports -v
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TelegramImportOrderTests(unittest.TestCase):
    def _run(self, code: str) -> subprocess.CompletedProcess:
        # fresh interpreter: the swap changes sys.modules for the whole process
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, text=True, timeout=180)

    def test_config_importable_after_bot(self):
        r = self._run("import telegram.telegram_bot\n"
                      "from telegram.telegram_config import TelegramConfig\n"
                      "print('ok', TelegramConfig.__module__)")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("ok telegram.telegram_config", r.stdout)

    def test_config_then_bot(self):
        r = self._run("from telegram.telegram_config import TelegramConfig\n"
                      "from telegram.telegram_bot import PhidipusBot\n"
                      "print('ok')")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
