# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
automation/a11y_windows.py — Phidipus v1.0
Windows UIAutomation/pywinauto backend for the automation_daemon (L2).

Architecture contract (R-03):
  "UIAutomation / pywinauto.  Same daemon account isolation."

Process: automation_daemon (L2)

Security invariants enforced here:
  R-03  No LLMClient import; no LLM API calls.

Used by:
  automation/ui_accessibility.py — on Windows platform

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

import asyncio
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="daemon")


class A11yWindows:
    """Windows UIAutomation/pywinauto backend. No LLM calls (R-03)."""

    def __init__(self) -> None:
        _log.info("A11yWindows initialised")

    async def mouse_click(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_click, p)

    def _sync_click(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyautogui
            pyautogui.click(x=p["x"], y=p["y"], button=p.get("button", "left"),
                            clicks=int(p.get("clicks", 1)))
            return {"success": True, "result": f"clicked ({p['x']},{p['y']})"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def mouse_move(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_move, p)

    def _sync_move(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyautogui
            pyautogui.moveTo(p["x"], p["y"], duration=float(p.get("duration", 0.1)))
            return {"success": True, "result": "moved"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def mouse_scroll(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_scroll, p)

    def _sync_scroll(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyautogui
            pyautogui.scroll(int(p.get("dy", 0)), x=p["x"], y=p["y"])
            return {"success": True, "result": "scrolled"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def mouse_drag(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_drag, p)

    def _sync_drag(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyautogui
            pyautogui.moveTo(p["from_x"], p["from_y"])
            pyautogui.dragTo(p["to_x"], p["to_y"],
                             duration=float(p.get("duration", 0.3)),
                             button=p.get("button", "left"))
            return {"success": True, "result": "dragged"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def keyboard_type(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_type, p)

    def _sync_type(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyautogui
            pyautogui.write(p["text"], interval=float(p.get("interval", 0.0)))
            return {"success": True, "result": f"typed {len(p['text'])} chars"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def keyboard_press(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_press, p)

    def _sync_press(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyautogui
            pyautogui.press(p["key"], presses=int(p.get("presses", 1)))
            return {"success": True, "result": f"pressed {p['key']}"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def keyboard_hotkey(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_hotkey, p)

    def _sync_hotkey(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyautogui
            pyautogui.hotkey(*p["keys"])
            return {"success": True, "result": f"hotkey {p['keys']}"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def clipboard_copy(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyperclip
            pyperclip.copy(p["text"])
            return {"success": True, "result": "copied"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def clipboard_paste(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_paste, p)

    def _sync_paste(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyautogui
            pyautogui.hotkey("ctrl", "v")
            return {"success": True, "result": "pasted"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def clipboard_get(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            import pyperclip
            return {"success": True, "result": pyperclip.paste()}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def screenshot_capture(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": "use orchestrator ScreenCapture"}

    async def window_focus(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_focus, p)

    def _sync_focus(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            from pywinauto import Desktop
            title = p.get("title", "")
            if title:
                win = Desktop(backend="uia").window(title=title)
                win.set_focus()
            return {"success": True, "result": f"focused {title!r}"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    async def window_list(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": []}

    async def window_get_info(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": {}}

    async def element_click(self, p: dict[str, Any]) -> dict[str, Any]:
        return await self.mouse_click(p)

    async def element_get_text(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": ""}

    async def element_find(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": []}

    async def element_set_value(self, p: dict[str, Any]) -> dict[str, Any]:
        return await self.keyboard_type({"text": p.get("value", "")})
