# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
automation/os_controller.py — Phidipus v1.0
OS automation dispatcher for the automation_daemon process (L2).

Architecture contract (R-03 / R-04 / R-05):
  "Receives only typed IPC messages.  ALLOWED_ACTIONS enforced at IPC
   layer.  Remove ALLOWED_COMMANDS whitelist (replaced by IPC schema)."

OSController is the sole OS automation executor in the daemon process.
It receives pre-validated action dicts from IPCServer (which has already
applied validate_request() and checked against ALLOWED_ACTIONS) and
dispatches them to the appropriate automation backend.

No raw LLM params, no raw strings — every action arrives as a typed,
schema-validated dict.  The ALLOWED_COMMANDS whitelist from v9.10 is
removed; enforcement is at the IPC schema layer (action_schema.py).

Process: automation_daemon (L2)

Security invariants enforced here:
  R-03  No LLMClient import; no LLM API calls of any kind.
  R-04  No network access — daemon has no network interface.
  R-05  dispatch() is only called by IPCServer after validate_request().
        OSController does NOT re-validate; it trusts the IPC layer.

Used by:
  ipc/ipc_server.py  — os_controller.dispatch(action, payload)

Dependencies:
  automation/ui_accessibility.py — UIAccessibility dispatcher
  automation/builtin_skills.py   — BuiltinSkills handler
  utils/logger.py                — get_logger()
"""

from __future__ import annotations

from typing import Any

from automation.ui_accessibility import UIAccessibility
from automation.builtin_skills import BuiltinSkills
from utils.logger import get_logger

_log = get_logger(__name__, process="daemon")


# ---------------------------------------------------------------------------
# OSController
# ---------------------------------------------------------------------------

class OSController:
    """
    Dispatches validated IPC actions to the appropriate OS backend.

    All methods in this class run inside the automation_daemon process
    (L2).  No LLM calls, no network access, no subprocess.

    Usage::

        controller = OSController()
        result = await controller.dispatch("mouse_click", {"x": 100, "y": 200})

    """

    def __init__(self, app_scanner: Any = None) -> None:
        self._a11y    = UIAccessibility()
        self._builtin = BuiltinSkills(app_scanner=app_scanner)

        _log.info("OSController initialised")

    # ------------------------------------------------------------------
    # Dispatch (called by IPCServer after validate_request())
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        action:  str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Dispatch a validated IPC action to the appropriate handler.

        Args:
            action:  IPC action name (already validated by IPCServer).
            payload: Action payload dict (already schema-validated).

        Returns:
            Result dict (JSON-serialisable).

        Raises:
            NotImplementedError: for actions not yet implemented.
            Exception:           for OS-level failures (propagated).
        """
        _log.debug(
            "OSController: dispatching action",
            extra={"action": action},
        )

        # ── Mouse actions ──────────────────────────────────────────────────
        if action == "mouse_click":
            return await self._a11y.mouse_click(payload)
        elif action == "mouse_move":
            return await self._a11y.mouse_move(payload)
        elif action == "mouse_scroll":
            return await self._a11y.mouse_scroll(payload)
        elif action == "mouse_drag":
            return await self._a11y.mouse_drag(payload)

        # ── Keyboard actions ───────────────────────────────────────────────
        elif action == "keyboard_type":
            return await self._a11y.keyboard_type(payload)
        elif action == "keyboard_press":
            return await self._a11y.keyboard_press(payload)
        elif action == "keyboard_hotkey":
            return await self._a11y.keyboard_hotkey(payload)

        # ── Clipboard actions ──────────────────────────────────────────────
        elif action == "clipboard_copy":
            return await self._a11y.clipboard_copy(payload)
        elif action == "clipboard_paste":
            return await self._a11y.clipboard_paste(payload)
        elif action == "clipboard_get":
            return await self._a11y.clipboard_get(payload)

        # ── Screen capture ─────────────────────────────────────────────────
        elif action == "screenshot_capture":
            return await self._a11y.screenshot_capture(payload)

        # ── Window management ──────────────────────────────────────────────
        elif action == "window_focus":
            return await self._a11y.window_focus(payload)
        elif action == "window_list":
            return await self._a11y.window_list(payload)
        elif action == "window_get_info":
            return await self._a11y.window_get_info(payload)

        # ── Accessibility / element actions ────────────────────────────────
        elif action == "element_click":
            return await self._a11y.element_click(payload)
        elif action == "element_get_text":
            return await self._a11y.element_get_text(payload)
        elif action == "element_find":
            return await self._a11y.element_find(payload)
        elif action == "element_set_value":
            return await self._a11y.element_set_value(payload)

        # ── v4.3 Keyboard-first layer (menus + cheap UI state) ─────────────
        elif action == "ui_snapshot":
            return await self._a11y.ui_snapshot(payload)
        elif action == "menu_list":
            return await self._a11y.menu_list(payload)
        elif action == "menu_select":
            return await self._a11y.menu_select(payload)

        # ── Application launch ─────────────────────────────────────────────
        elif action == "app_launch":
            return await self._builtin.app_launch(payload)

        # ── Browser navigation ─────────────────────────────────────────────
        elif action == "browser_navigate":
            return await self._builtin.browser_navigate(payload)

        # ── Browser JS execution (v9.34 A1) ───────────────────────────────
        elif action == "browser_execute_js":
            # [FIX v4.2] Rate limit + cache error to prevent 50x log spam
            # FIX v4.3: only cache *availability* failures (no Chrome window,
            # Apple-Events JS disabled, timeouts).  A normal JS exception on a
            # page must not disable every JS call for the next 30 seconds.
            _now = __import__('time').time()
            if not hasattr(self, '_js_fail_until'):
                self._js_fail_until = 0.0
            if _now < self._js_fail_until:
                return {"success": False, "result": "browser JS unavailable (cached 10s)"}
            try:
                result = await self._a11y.browser_execute_js(payload)
                if not result.get("success"):
                    msg = str(result.get("result", "")).lower()
                    if any(s in msg for s in ("apple events", "no chrome window",
                                              "timeout", "not running", "-1743", "-600")):
                        self._js_fail_until = _now + 10.0
                return result
            except Exception as exc:
                self._js_fail_until = _now + 10.0
                _log.warning("browser_execute_js unavailable — cached for 10s",
                             extra={"error": str(exc)})
                return {"success": False, "result": str(exc)}

        # ── System / control ───────────────────────────────────────────────
        elif action == "ping":
            return {"success": True, "result": "pong"}

        elif action == "user_confirm":
            # R-22: daemon surfaces confirmation request; result comes back
            # via a separate IPC response cycle handled by IPCServer.
            return await self._builtin.user_confirm(payload)

        else:
            _log.error(
                "OSController: unknown action (should be blocked by IPC schema)",
                extra={"action": action},
            )
            raise NotImplementedError(
                f"OSController: action {action!r} has no handler. "
                "This should have been rejected by the IPC schema layer."
            )
