# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
automation/ui_accessibility.py — Phidipus v1.0
Cross-platform UI automation dispatcher for the automation_daemon (L2).

Architecture contract (R-03):
  "Runs only within daemon process.  No LLM calls.  No changes to logic."

UIAccessibility dispatches OS automation actions to the appropriate
platform-specific backend:
  - Linux:   a11y_linux.py   (AT-SPI)
  - macOS:   a11y_macos.py   (atomacos/pyobjc)
  - Windows: a11y_windows.py (UIAutomation/pywinauto)

The backend is detected once at construction time.  All methods receive
pre-validated payload dicts from OSController (IPC layer already
validated the schemas).

Process: automation_daemon (L2)

Security invariants enforced here:
  R-03  No LLMClient import; no LLM API calls.
  R-04  No network access.

Used by:
  automation/os_controller.py — all mouse/keyboard/window/element actions

Dependencies:
  automation/a11y_linux.py   — Linux AT-SPI backend
  automation/a11y_macos.py   — macOS atomacos backend
  automation/a11y_windows.py — Windows UIAutomation backend
  utils/logger.py            — get_logger()
"""

from __future__ import annotations

import platform
import sys
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="daemon")


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def _detect_platform() -> str:
    system = platform.system().lower()
    if system == "linux":
        return "linux"
    elif system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    return "unknown"


# ---------------------------------------------------------------------------
# UIAccessibility
# ---------------------------------------------------------------------------

class UIAccessibility:
    """
    Cross-platform UI automation dispatcher.

    Detects the OS at construction time and delegates all actions to
    the appropriate platform backend.

    Usage::

        a11y = UIAccessibility()
        await a11y.mouse_click({"x": 100, "y": 200, "button": "left"})
        await a11y.keyboard_type({"text": "hello"})
    """

    def __init__(self) -> None:
        self._platform = _detect_platform()
        self._backend  = self._load_backend()

        _log.info(
            "UIAccessibility initialised",
            extra={"platform": self._platform},
        )

    def _load_backend(self) -> Any:
        """Load the platform-specific backend."""
        if self._platform == "linux":
            from automation.a11y_linux import A11yLinux
            return A11yLinux()
        elif self._platform == "macos":
            from automation.a11y_macos import A11yMacOS
            return A11yMacOS()
        elif self._platform == "windows":
            from automation.a11y_windows import A11yWindows
            return A11yWindows()
        else:
            _log.warning(
                "UIAccessibility: unknown platform — using stub backend",
                extra={"platform": self._platform},
            )
            return _StubBackend()

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    async def mouse_click(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.mouse_click(payload)

    async def mouse_move(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.mouse_move(payload)

    async def mouse_scroll(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.mouse_scroll(payload)

    async def mouse_drag(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.mouse_drag(payload)

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    async def keyboard_type(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.keyboard_type(payload)

    async def keyboard_press(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.keyboard_press(payload)

    async def keyboard_hotkey(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.keyboard_hotkey(payload)

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    async def clipboard_copy(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.clipboard_copy(payload)

    async def clipboard_paste(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.clipboard_paste(payload)

    async def clipboard_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.clipboard_get(payload)

    # ------------------------------------------------------------------
    # Screen / window
    # ------------------------------------------------------------------

    async def screenshot_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.screenshot_capture(payload)

    async def window_focus(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.window_focus(payload)

    async def window_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.window_list(payload)

    async def window_get_info(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.window_get_info(payload)

    # ------------------------------------------------------------------
    # Elements
    # ------------------------------------------------------------------

    async def element_click(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.element_click(payload)

    async def element_get_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.element_get_text(payload)

    async def element_find(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.element_find(payload)

    async def element_set_value(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._backend.element_set_value(payload)

    # ------------------------------------------------------------------
    # v4.3 Keyboard-first layer
    # ------------------------------------------------------------------

    async def ui_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call_optional("ui_snapshot", payload)

    async def menu_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call_optional("menu_list", payload)

    async def menu_select(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call_optional("menu_select", payload)

    async def _call_optional(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self._backend, name, None)
        if fn is None:
            return {"success": False, "result": f"{name} is not supported on {self._platform}"}
        return await fn(payload)

    # ------------------------------------------------------------------
    # Browser JS execution (v4.2 fix — CDP fallback)
    # ------------------------------------------------------------------

    async def browser_execute_js(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute JavaScript in browser via CDP or fallback.

        [FIX v4.2] Method was missing → OSController.dispatch() raised
        AttributeError on every browser_execute_js call, spamming logs
        ~50 times per workflow step (500ms retry loop with no limit).

        This stub returns a graceful error when CDP is not available,
        allowing the workflow to continue with degraded functionality
        instead of flooding the error log.
        """
        if hasattr(self._backend, 'browser_execute_js'):
            return await self._backend.browser_execute_js(payload)
        # CDP not available — return graceful error
        return {
            "success": False,
            "error": "browser_execute_js: CDP not available. "
                     "Start Chrome with --remote-debugging-port=9222",
            "result": None,
        }


# ---------------------------------------------------------------------------
# Stub backend (unknown platform)
# ---------------------------------------------------------------------------

class _StubBackend:
    """Stub backend for unknown platforms — returns not-implemented result."""

    async def _stub(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"success": False, "result": "Platform not supported"}

    async def mouse_click(self, p: dict) -> dict:      return await self._stub(p)
    async def mouse_move(self, p: dict) -> dict:       return await self._stub(p)
    async def mouse_scroll(self, p: dict) -> dict:     return await self._stub(p)
    async def mouse_drag(self, p: dict) -> dict:       return await self._stub(p)
    async def keyboard_type(self, p: dict) -> dict:    return await self._stub(p)
    async def keyboard_press(self, p: dict) -> dict:   return await self._stub(p)
    async def keyboard_hotkey(self, p: dict) -> dict:  return await self._stub(p)
    async def clipboard_copy(self, p: dict) -> dict:   return await self._stub(p)
    async def clipboard_paste(self, p: dict) -> dict:  return await self._stub(p)
    async def clipboard_get(self, p: dict) -> dict:    return await self._stub(p)
    async def screenshot_capture(self, p: dict) -> dict: return await self._stub(p)
    async def window_focus(self, p: dict) -> dict:     return await self._stub(p)
    async def window_list(self, p: dict) -> dict:      return await self._stub(p)
    async def window_get_info(self, p: dict) -> dict:  return await self._stub(p)
    async def element_click(self, p: dict) -> dict:    return await self._stub(p)
    async def element_get_text(self, p: dict) -> dict: return await self._stub(p)
    async def element_find(self, p: dict) -> dict:     return await self._stub(p)
    async def element_set_value(self, p: dict) -> dict: return await self._stub(p)
    async def browser_execute_js(self, p: dict) -> dict: return await self._stub(p)
