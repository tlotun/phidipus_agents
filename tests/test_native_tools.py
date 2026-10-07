# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tests/test_native_tools.py — ReAct native function calling (no Ollama needed)

Run:  ./venv/bin/python -m unittest tests.test_native_tools -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import model_registry  # noqa: E402
from core.llm_client import LLMResponse  # noqa: E402
from planner.react_reasoner import ReactReasoner, _coerce  # noqa: E402

CHROME_UI = {"app": "Google Chrome", "bundle_id": "com.google.Chrome", "window_title": "Inbox", "window_count": 1}
FINDER_UI = {"app": "Finder", "bundle_id": "com.apple.finder", "window_title": "Desktop", "window_count": 1}


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def model_for(self, role="reasoning"):
        return "qwen3.5:9b"

    def sanitize_traceback(self, text):
        return str(text)

    async def chat_tools(self, goal, *, tools, system_prompt="", messages=None, model=None,
                         temperature=None, max_tokens=None, think=None):
        self.calls.append({"tools": [t["function"]["name"] for t in tools], "messages": list(messages or [])})
        content, calls = self.replies.pop(0)
        return LLMResponse(content=content, model=model or "", tool_calls=tuple(calls))


def call(tool, **args):
    return {"name": tool, "arguments": args}


class NativeToolTests(unittest.TestCase):
    def setUp(self):
        reg = model_registry.get_registry()
        self._orig = (reg.supports, reg.native_tools_enabled)
        reg.supports = lambda model, cap: True
        reg.native_tools_enabled = lambda: True

    def tearDown(self):
        reg = model_registry.get_registry()
        reg.supports, reg.native_tools_enabled = self._orig

    def step(self, llm, ui=None, context=None):
        r = ReactReasoner(None, llm)
        return asyncio.run(r.step_native("mở tab mới", context=context, step_n=1, ui_state=ui))

    def test_shortcut_tool_uses_frontmost_app(self):
        llm = FakeLLM([("", [call("shortcut", name="chromium.new_tab")])])
        res = self.step(llm, CHROME_UI)
        self.assertEqual((res.action_name, res.action_payload), ("keyboard_hotkey", {"keys": ["cmd", "t"]}))
        self.assertEqual(llm.calls[0]["tools"][0], "shortcut")          # keyboard-first tool listed first
        self.assertIn("menu_select", llm.calls[0]["tools"])
        self.assertIn("task_complete", llm.calls[0]["tools"])
        self.assertTrue(any("UI state" in m["content"] for m in llm.calls[0]["messages"]))

    def test_high_risk_shortcut_asks_first(self):
        llm = FakeLLM([("", [call("shortcut", name="finder.move_to_trash")])])
        res = self.step(llm, FINDER_UI)
        self.assertEqual(res.action_name, "user_confirm")
        confirmed = {"steps": [{"action": "user_confirm", "observation": "Action 'user_confirm' succeeded."}]}
        llm = FakeLLM([("", [call("shortcut", name="finder.move_to_trash")])])
        res = self.step(llm, FINDER_UI, confirmed)
        self.assertEqual((res.action_name, res.action_payload), ("keyboard_hotkey", {"keys": ["cmd", "delete"]}))

    def test_invalid_arguments_get_one_repair_round(self):
        llm = FakeLLM([("", [call("mouse_click", x="abc")]),
                       ("", [call("mouse_click", x="540", y=380.4)])])
        res = self.step(llm, CHROME_UI)
        self.assertEqual((res.action_name, res.action_payload["x"], res.action_payload["y"]), ("mouse_click", 540, 380))
        self.assertEqual(llm.calls[1]["messages"][-1]["role"], "tool")

    def test_task_complete(self):
        res = self.step(FakeLLM([("", [call("task_complete", summary="Đã mở tab")])]), CHROME_UI)
        self.assertEqual(res.action_name, "ping")
        self.assertTrue(res.thought.startswith("Task complete:"))
        res = self.step(FakeLLM([("", [call("task_complete", summary="Không có mạng", success=False)])]), CHROME_UI)
        self.assertTrue(res.thought.startswith("Task failed:"))

    def test_text_answer_without_tool(self):
        res = self.step(FakeLLM([("Task complete — tab is open.", [])]), CHROME_UI)
        self.assertEqual(res.action_name, "ping")
        res = self.step(FakeLLM([("hmm", []), ("still thinking", [])]), CHROME_UI)
        self.assertIsNone(res)                                          # caller falls back to text protocol

    def test_no_tools_capability_falls_back(self):
        model_registry.get_registry().supports = lambda model, cap: False
        llm = FakeLLM([])
        self.assertIsNone(self.step(llm, CHROME_UI))
        self.assertEqual(llm.calls, [])

    def test_js_tool_is_never_exposed_or_accepted(self):
        llm = FakeLLM([("", [call("browser_execute_js", js_code="alert(1)")]), ("", [call("ui_snapshot")])])
        res = self.step(llm, CHROME_UI)
        self.assertNotIn("browser_execute_js", llm.calls[0]["tools"])
        self.assertEqual(res.action_name, "ui_snapshot")

    def test_coerce(self):
        self.assertEqual(_coerce("keyboard_hotkey", {"keys": "cmd+shift+t"}), {"keys": ["cmd", "shift", "t"]})
        self.assertEqual(_coerce("mouse_scroll", {"dy": "3", "dx": None}), {"dy": 3})


if __name__ == "__main__":
    unittest.main()
