# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tests/test_keyboard_first.py — keyboard-first layer (catalog matching + runner)

Run:  ./venv/bin/python -m unittest tests.test_keyboard_first -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.keyboard_first import KeyboardFirst, ShortcutCatalog, UIContext, chord_action  # noqa: E402

CHROME = UIContext(ok=True, app="Google Chrome", bundle_id="com.google.Chrome", window_title="Inbox", window_count=1)
FINDER = UIContext(ok=True, app="Finder", bundle_id="com.apple.finder", window_title="Desktop", window_count=1)
SAFARI = UIContext(ok=True, app="Safari", bundle_id="com.apple.Safari")
UNKNOWN = UIContext()


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = ShortcutCatalog()

    def find(self, cmd, ui):
        m = self.cat.find(cmd, ui)
        return (m.shortcut.qualified, m.params, m.app_key) if m else None

    def test_app_aware_resolution(self):
        self.assertEqual(self.find("mở tab mới", CHROME)[0], "chromium.new_tab")
        self.assertEqual(self.find("mở tab mới", FINDER)[0], "finder.new_tab")
        self.assertEqual(self.find("tab sau", SAFARI)[0], "safari.next_tab")
        self.assertEqual(self.find("tab trước", CHROME)[0], "chromium.prev_tab")
        self.assertIsNone(self.find("mở tab mới", UNKNOWN))   # legacy table handles it

    def test_full_match_only(self):
        self.assertEqual(self.find("lưu", UNKNOWN)[0], "standard.save")
        self.assertEqual(self.find("Nhấn lưu đi!", UNKNOWN)[0], "standard.save")
        self.assertIsNone(self.find("lưu file báo cáo vào desktop", UNKNOWN))
        self.assertEqual(self.find("in", UNKNOWN)[0], "standard.print")
        self.assertIsNone(self.find("log in to facebook", UNKNOWN))

    def test_app_mention_switches_target(self):
        q, _params, app = self.find("mở tab mới trong chrome", FINDER)
        self.assertEqual((q, app), ("chromium.new_tab", "chrome"))

    def test_macro_params_keep_case(self):
        q, params, app = self.find("đi tới thư mục ~/Downloads/Báo Cáo", FINDER)
        self.assertEqual(q, "macro.finder_go_to")
        self.assertEqual(params["path"], "~/Downloads/Báo Cáo")
        q, params, _ = self.find("chuyển sang tab Gmail", CHROME)
        self.assertEqual((q, params["title"]), ("macro.switch_tab", "Gmail"))
        self.assertIsNone(self.find("đi tới thư mục báo cáo", FINDER))   # path rule: must start with ~ or /

    def test_context_conditions(self):
        gmail = UIContext(ok=True, bundle_id="com.google.Chrome", url="https://mail.google.com/mail/u/0/#inbox")
        self.assertEqual(self.find("soạn email", gmail)[0], "gmail.compose")
        typing = UIContext(ok=True, bundle_id="com.google.Chrome", url="https://mail.google.com/x", focused_is_text=True)
        self.assertIsNone(self.find("soạn email", typing))           # single-key shortcut would type "c"
        self.assertEqual(self.find("gửi email", typing)[0], "gmail_send.send")
        text_ui = UIContext(ok=True, bundle_id="com.apple.TextEdit", focused_is_text=True)
        self.assertEqual(self.find("về cuối dòng", text_ui)[0], "text.line_end")
        self.assertIsNone(self.find("về cuối dòng", UNKNOWN))

    def test_other_macros(self):
        self.assertEqual(self.find("xuất pdf", UNKNOWN)[0], "macro.export_pdf")
        self.assertEqual(self.find("copy toàn bộ", UNKNOWN)[0], "macro.copy_all")
        self.assertEqual(self.find("tìm trong trang giá vàng", CHROME)[1]["text"], "giá vàng")
        self.assertEqual(self.find("tìm trong trang", CHROME)[0], "chromium.find")

    def test_tools_and_chords(self):
        names = [s.qualified for s in self.cat.tools_for(CHROME)]
        self.assertIn("chromium.new_tab", names)
        self.assertNotIn("finder.empty_trash", names)
        self.assertEqual(names[0].split(".")[0], "chromium")        # app-specific first
        sc = self.cat.by_name("chromium.close_tab", CHROME)
        self.assertEqual(chord_action(sc), ("keyboard_hotkey", {"keys": ["cmd", "w"]}))
        self.assertEqual(chord_action(self.cat.by_name("finder.quick_look", FINDER)),
                         ("keyboard_press", {"key": "space"}))

    def test_could_match_is_cheap_filter(self):
        self.assertTrue(self.cat.could_match("đóng tab"))
        self.assertTrue(self.cat.could_match("chọn menu File > Export as PDF"))
        self.assertFalse(self.cat.could_match("viết bài linkedin về AI"))


class FakeIPC:
    def __init__(self, snapshots, confirm=True, fail=()):
        self.snapshots = list(snapshots)
        self.calls = []
        self.confirm = confirm
        self.fail = set(fail)

    async def send_action(self, action, payload):
        self.calls.append((action, payload))
        if action == "ui_snapshot":
            snap = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            return SimpleNamespace(success=True, result=snap)
        if action == "user_confirm":
            return SimpleNamespace(success=self.confirm, result={"confirmed": self.confirm}, error="denied")
        if action == "clipboard_get":
            return SimpleNamespace(success=True, result="nội dung đã copy")
        if action in self.fail:
            return SimpleNamespace(success=False, result="", error="failed")
        return SimpleNamespace(success=True, result="ok")


def snap(title, count=1, bundle="com.google.Chrome"):
    return {"app": "Google Chrome", "bundle_id": bundle, "window_title": title, "window_count": count}


class RunnerTests(unittest.TestCase):
    def test_shortcut_verified_by_snapshot(self):
        ipc = FakeIPC([snap("Inbox"), snap("New Tab")])
        res = asyncio.run(KeyboardFirst(ipc).try_command("mở tab mới"))
        self.assertTrue(res.success)
        self.assertTrue(res.verified)
        self.assertIn(("keyboard_hotkey", {"keys": ["cmd", "t"]}), ipc.calls)

    def test_ordinary_goal_costs_no_ipc(self):
        ipc = FakeIPC([snap("Inbox")])
        self.assertIsNone(asyncio.run(KeyboardFirst(ipc).try_command("tổng hợp tin tức về AI")))
        self.assertEqual(ipc.calls, [])

    def test_high_risk_needs_confirmation(self):
        finder = {"app": "Finder", "bundle_id": "com.apple.finder", "window_title": "Desktop"}
        ipc = FakeIPC([finder], confirm=False)
        res = asyncio.run(KeyboardFirst(ipc).try_command("chuyển vào thùng rác"))
        self.assertFalse(res.success)
        self.assertNotIn("keyboard_hotkey", [c[0] for c in ipc.calls])
        ipc = FakeIPC([finder], confirm=True)
        res = asyncio.run(KeyboardFirst(ipc).try_command("chuyển vào thùng rác"))
        self.assertTrue(res.success)
        self.assertIn(("keyboard_hotkey", {"keys": ["cmd", "delete"]}), ipc.calls)

    def test_critical_blocked_by_default_cap(self):
        finder = {"app": "Finder", "bundle_id": "com.apple.finder"}
        ipc = FakeIPC([finder])
        res = asyncio.run(KeyboardFirst(ipc).try_command("dọn sạch thùng rác"))
        self.assertFalse(res.success)
        self.assertEqual([c[0] for c in ipc.calls], ["ui_snapshot"])

    def test_macro_and_clipboard(self):
        ipc = FakeIPC([snap("Doc")])
        res = asyncio.run(KeyboardFirst(ipc).try_command("copy toàn bộ"))
        self.assertTrue(res.success)
        self.assertEqual(res.output, "nội dung đã copy")
        self.assertEqual([c[0] for c in ipc.calls if c[0] != "ui_snapshot"],
                         ["keyboard_hotkey", "keyboard_hotkey", "clipboard_get"])

    def test_menu_fallback_when_shortcut_has_no_effect(self):
        ipc = FakeIPC([snap("A"), snap("A")])
        res = asyncio.run(KeyboardFirst(ipc).try_command("xếp cửa sổ sang trái"))
        self.assertTrue(res.success)
        self.assertEqual(res.method, "menu")
        self.assertEqual(ipc.calls[-1], ("menu_select", {"path": ["Window", "Move & Resize", "Left"]}))

    def test_explicit_menu_path(self):
        ipc = FakeIPC([snap("A")])
        res = asyncio.run(KeyboardFirst(ipc).try_command("chọn menu File > Export as PDF…"))
        self.assertTrue(res.success)
        self.assertEqual(ipc.calls[-1], ("menu_select", {"path": ["File", "Export as PDF…"]}))

    def test_app_switch_before_shortcut(self):
        finder = {"app": "Finder", "bundle_id": "com.apple.finder", "window_title": "Desktop"}
        ipc = FakeIPC([finder, snap("Inbox"), snap("New Tab")])
        res = asyncio.run(KeyboardFirst(ipc).try_command("mở tab mới trong chrome"))
        self.assertTrue(res.success)
        actions = [c[0] for c in ipc.calls]
        self.assertLess(actions.index("app_launch"), actions.index("keyboard_hotkey"))


if __name__ == "__main__":
    unittest.main()
