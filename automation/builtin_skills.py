# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
automation/builtin_skills.py — Phidipus v1.0
Built-in skill handlers for the automation_daemon process (L2).

Architecture contract (R-01 / R-06):
  "Move to automation_daemon.  Builtin skills execute only via IPC
   message dispatch.  Remove pyautogui from orchestrator."

  "R-06: skills/builtin_skills.py MUST NOT exist in the orchestrator
   process.  It is automation_daemon-only."

BuiltinSkills handles app_launch, browser_navigate, and user_confirm
actions.  These actions involve spawning applications or prompting the
user — operations that must run in the daemon process (L2), not the
orchestrator (L1).

user_confirm (R-22)
-------------------
When an action arrives with require_confirm=True, OSController calls
user_confirm() to surface a confirmation prompt before execution.  The
daemon does NOT auto-execute such actions — it blocks until the operator
responds (or the request times out).

Process: automation_daemon (L2)

Security invariants enforced here:
  R-03  No LLMClient import; no LLM API calls.
  R-04  No network access from this module.
  R-06  This module exists ONLY in automation_daemon — never in
        orchestrator.

Used by:
  automation/os_controller.py — app_launch, browser_navigate, user_confirm

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

import asyncio
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="daemon")

# ---------------------------------------------------------------------------
# Allowlist for app_launch (R-05: IPC layer already schema-validated name)
# ---------------------------------------------------------------------------

_ALLOWED_APPS: frozenset[str] = frozenset({
    # ── Browsers ─────────────────────────────────────────────────────────
    "firefox",
    "google-chrome",
    "google chrome",
    "chrome",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "safari",
    "msedge",
    "arc",
    # ── Editors ──────────────────────────────────────────────────────────
    "code",               # VS Code
    "gedit",
    "notepad",
    "notepad++",
    "textedit",
    # ── File managers ────────────────────────────────────────────────────
    "finder",
    "explorer",
    "thunar",
    "nautilus",
    "dolphin",
    # ── macOS apps ───────────────────────────────────────────────────────
    "terminal",
    "iterm",
    "iterm2",
    "preview",
    "calculator",
    "notes",
    "reminders",
    "calendar",
    "messages",
    "mail",
    "music",
    "photos",
    "maps",
    "system preferences",
    "system settings",
})

# Map common names to macOS app names (for `open -a`)
_MACOS_APP_MAP: dict[str, str] = {
    "chrome":          "Google Chrome",
    "google-chrome":   "Google Chrome",
    "google chrome":   "Google Chrome",
    "firefox":         "Firefox",
    "safari":          "Safari",
    "arc":             "Arc",
    "brave-browser":   "Brave Browser",
    "code":            "Visual Studio Code",
    "textedit":        "TextEdit",
    "finder":          "Finder",
    "terminal":        "Terminal",
    "iterm":           "iTerm",
    "iterm2":          "iTerm",
    "preview":         "Preview",
    "calculator":      "Calculator",
    "notes":           "Notes",
    "reminders":       "Reminders",
    "calendar":        "Calendar",
    "messages":        "Messages",
    "mail":            "Mail",
    "music":           "Music",
    "photos":          "Photos",
    "maps":            "Maps",
    "system preferences": "System Preferences",
    "system settings":    "System Settings",
}

#: Applications that MUST NOT receive arguments.
_NO_ARGS_APPS: frozenset[str] = frozenset({
    "finder",
    "explorer",
    "thunar",
    "nautilus",
    "dolphin",
})


# ---------------------------------------------------------------------------
# BuiltinSkills
# ---------------------------------------------------------------------------

class BuiltinSkills:
    """
    Handles built-in IPC actions that require daemon-side execution.

    Uses AppScanner for instant app lookup — no VLM needed to find apps.
    """

    def __init__(self, app_scanner: Any = None) -> None:
        self._scanner = app_scanner
        _log.info("BuiltinSkills initialised",
                   extra={"has_scanner": app_scanner is not None})

    # ------------------------------------------------------------------
    # app_launch — now uses AppScanner for instant lookup
    # ------------------------------------------------------------------

    async def app_launch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Launch an application — uses AppScanner for instant fuzzy lookup.

        Args:
            payload: {
                "app_name": str,
                "args": list[str] (optional),
                "chrome_profile": str (optional — opens Chrome with specific profile)
            }
        """
        app_name = str(payload.get("app_name", "")).strip().lower()
        chrome_profile = str(payload.get("chrome_profile", "")).strip()
        args: list[str] = [str(a) for a in payload.get("args", [])]
        url = str(payload.get("url", "")).strip()

        # FIX v4.3: content_pipeline / social_poster send {"app_name", "url"}.
        # Launch the app, then open the URL in it (browsers only).
        if url:
            launched = await self.app_launch({k: v for k, v in payload.items() if k != "url"})
            if not launched.get("success"):
                return launched
            await asyncio.sleep(1.5)
            nav = await self.browser_navigate({"url": url, "_app": app_name})
            return {
                "success": bool(nav.get("success")),
                "result": f"{launched.get('result')} → {nav.get('result')}",
            }

        # ── Strategy 1: AppScanner lookup (instant, no VLM) ──────────
        if self._scanner and self._scanner.scanned:
            found = self._scanner.find_app(app_name)
            if found:
                _log.info(
                    "BuiltinSkills: app found via scanner",
                    extra={"query": app_name, "found": found.name, "path": found.path},
                )
                try:
                    result = await asyncio.to_thread(
                        self._launch_via_scanner, found.name, args, chrome_profile
                    )
                    return result
                except Exception as exc:
                    return {"success": False, "result": str(exc)}

        # ── Strategy 2: Allowlist fallback ────────────────────────────
        if app_name not in _ALLOWED_APPS:
            _log.warning(
                "BuiltinSkills: app_launch rejected — not found in scanner or allowlist",
                extra={"app_name": app_name},
            )
            return {
                "success": False,
                "result": f"app {app_name!r} not found. Available apps can be checked via /system.",
            }

        if args and app_name in _NO_ARGS_APPS:
            return {"success": False, "result": f"app {app_name!r} does not accept arguments."}

        try:
            result = await asyncio.to_thread(self._launch_app, [app_name] + args)
            return result
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    def _launch_via_scanner(self, app_name: str, args: list[str],
                            chrome_profile: str = "") -> dict[str, Any]:
        """Launch app using macOS `open -a` with scanner-verified name."""
        import subprocess as _sp
        import time

        # Special: Chrome with specific profile
        if chrome_profile and "chrome" in app_name.lower() and self._scanner:
            profile = self._scanner.find_chrome_profile(chrome_profile)

            if profile:
                # CRITICAL: --profile-directory only works when Chrome is NOT running.
                # Must quit Chrome first, then relaunch with the flag.
                _log.info("BuiltinSkills: quitting Chrome before profile launch",
                          extra={"profile": profile.name, "dir": profile.directory})
                try:
                    _sp.run(["osascript", "-e", 'tell application "Google Chrome" to quit'],
                            timeout=5, capture_output=True)
                    time.sleep(2)  # Wait for Chrome to fully quit
                except Exception:
                    # Chrome might not be running — that's fine
                    pass

                # FIX v4.3: keep Chrome DevTools Protocol enabled — the launcher
                # starts Chrome with --remote-debugging-port=9222 and many skills
                # rely on it; relaunching without the flag silently disabled CDP.
                cmd = ["open", "-a", "Google Chrome", "--args",
                       f"--profile-directory={profile.directory}",
                       "--remote-debugging-port=9222"]
                _sp.Popen(cmd, start_new_session=True)
                return {
                    "success": True,
                    "result": f"Chrome mở thẳng vào profile '{profile.name}' (dir={profile.directory})",
                }
            else:
                _log.warning("BuiltinSkills: Chrome profile not found in scanner",
                             extra={"query": chrome_profile,
                                    "available": len(self._scanner.list_chrome_profiles())})
                # Fallback: open Chrome normally
                _sp.Popen(["open", "-a", "Google Chrome"], start_new_session=True)
                return {
                    "success": True,
                    "result": f"Chrome đã mở nhưng không tìm thấy profile '{chrome_profile}'",
                }

        # Standard macOS launch
        cmd = ["open", "-a", app_name]
        if args:
            cmd.append("--args")
            cmd.extend(args)

        _sp.Popen(cmd, start_new_session=True)
        return {"success": True, "result": f"launched {app_name!r} via open -a"}

    def _launch_app(self, cmd: list[str]) -> dict[str, Any]:
        """
        Synchronous app launch — platform-aware.

        macOS:   `open -a "Google Chrome"` (resolves app bundles correctly)
        Linux:   Direct Popen (e.g. `google-chrome`)
        Windows: `start "" "app"` (handled by Popen shell)
        """
        import platform
        import subprocess as _sp

        app_name = cmd[0]
        args = cmd[1:] if len(cmd) > 1 else []

        try:
            system = platform.system()

            if system == "Darwin":
                # macOS: use `open -a` with mapped app name
                macos_name = _MACOS_APP_MAP.get(app_name, app_name)
                open_cmd = ["open", "-a", macos_name]
                if args:
                    open_cmd.append("--args")
                    open_cmd.extend(args)
                _sp.Popen(open_cmd, start_new_session=True)
                return {"success": True, "result": f"launched {macos_name!r} via open -a"}

            elif system == "Windows":
                # Windows: use start command
                _sp.Popen(["cmd", "/c", "start", "", app_name] + args,
                          start_new_session=True)
                return {"success": True, "result": f"launched {app_name!r}"}

            else:
                # Linux: direct Popen
                _sp.Popen(cmd, start_new_session=True)
                return {"success": True, "result": f"launched {app_name!r}"}

        except FileNotFoundError:
            return {
                "success": False,
                "result":  f"application {app_name!r} not found on this system.",
            }
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    # ------------------------------------------------------------------
    # browser_navigate
    # ------------------------------------------------------------------

    async def browser_navigate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Open a URL in the system default browser.

        The URL has already been validated by IPC schema (must be http/https).

        Args:
            payload: {"url": str}
        """
        url = str(payload.get("url", ""))

        # Double-check URL scheme (IPC schema already validated, belt+suspenders)
        if not url.startswith(("http://", "https://")):
            return {
                "success": False,
                "result":  f"URL {url!r} is not http/https.",
            }

        try:
            result = await asyncio.to_thread(self._open_url, url, str(payload.get("_app", "")))
            return result
        except Exception as exc:
            return {"success": False, "result": str(exc)}

    def _open_url(self, url: str, app_hint: str = "") -> dict[str, Any]:
        """
        Open *url* in Google Chrome when installed (Phidipus skills, CDP and
        Chrome profiles all assume Chrome), otherwise in the default browser.
        FIX v4.3: the old code always used the system default browser, so on
        Macs where Safari is default the agent lost track of the page.
        """
        import platform
        import subprocess as _sp
        from pathlib import Path as _P
        if platform.system() == "Darwin":
            browser = "Google Chrome"
            hint = (app_hint or "").lower()
            if hint in ("safari", "firefox", "arc", "brave-browser"):
                browser = _MACOS_APP_MAP.get(hint, browser)
            if browser != "Google Chrome" or _P("/Applications/Google Chrome.app").exists() \
                    or (_P.home() / "Applications/Google Chrome.app").exists():
                r = _sp.run(["open", "-a", browser, url], capture_output=True, timeout=10)
                if r.returncode == 0:
                    return {"success": True, "result": f"navigated to {url} ({browser})"}
        import webbrowser
        ok = webbrowser.open(url)
        return {"success": bool(ok), "result": f"navigated to {url}" if ok else "no browser available"}

    # ------------------------------------------------------------------
    # user_confirm (R-22)
    # ------------------------------------------------------------------

    async def user_confirm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Surface a user confirmation prompt (R-22).

        In a full deployment this would show a desktop notification or
        dialog box.  In this implementation it logs the request and
        returns a pending confirmation dict for the IPC layer to relay
        back to the orchestrator.

        Args:
            payload: {
                "message": str,
                "action_description": str,
                "pending_action": str (optional),
                "pending_cid": str (optional),
            }

        Returns:
            {"success": True, "result": {"status": "pending_confirmation",
             "message": str}}
        """
        message     = str(payload.get("message", ""))
        description = str(payload.get("action_description", ""))

        _log.info(
            "R-22: BuiltinSkills: user confirmation requested",
            extra={
                "message":     message[:200],
                "description": description[:200],
            },
        )

        import platform
        if platform.system() == "Darwin":
            # FIX v4.3: on macOS this used to do nothing and return
            # "pending_confirmation" that nobody handled.  Show a real dialog.
            status = await asyncio.to_thread(self._confirm_dialog_macos, message, description)
            return {
                "success": status == "confirmed",
                "result": {"status": status, "confirmed": status == "confirmed",
                           "message": message},
            }

        # Other platforms: best-effort notification, explicit "pending" status
        await asyncio.to_thread(self._show_notification, message, description)

        return {
            "success": False,
            "result": {
                "status":  "pending_confirmation",
                "confirmed": False,
                "message": message,
            },
        }

    @staticmethod
    def _confirm_dialog_macos(message: str, description: str, timeout_s: int = 60) -> str:
        """Blocking native dialog → 'confirmed' | 'denied' | 'timeout'."""
        import subprocess as _sp

        def q(t: str) -> str:
            t = t.replace("\\", "\\\\").replace('"', '\\"')
            return t.replace("\r", " ").replace("\n", " ")[:400]

        script = (
            f'display dialog "{q(description)}" & return & return & "{q(message)}" '
            f'with title "Phidipus Agents — cần xác nhận" '
            f'buttons {{"Từ chối", "Đồng ý"}} default button "Từ chối" '
            f'with icon caution giving up after {int(timeout_s)}'
        )
        try:
            r = _sp.run(["osascript", "-e", script], capture_output=True,
                        text=True, timeout=timeout_s + 5)
        except Exception:
            return "timeout"
        out = (r.stdout or "") + (r.stderr or "")
        if "gave up:true" in out:
            return "timeout"
        if r.returncode == 0 and "Đồng ý" in out:
            return "confirmed"
        return "denied"

    def _show_notification(self, message: str, description: str) -> None:
        """Attempt to show a desktop notification (best-effort)."""
        try:
            import subprocess as _sp  # noqa: daemon process — notification only
            import platform
            if platform.system() == "Linux":
                _sp.run(
                    ["notify-send", "Phidipus Agents — Confirmation Required",
                     f"{description}\n{message}"],
                    check=False,
                    timeout=3,
                )
        except Exception:
            pass  # Best-effort — log already covers the event
