# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
automation/a11y_macos.py — Phidipus v4.3
macOS automation backend for the automation daemon (L2).

v4.3 rewrite — native Quartz events instead of best-effort shell tools.

Why
---
The previous backend (cliclick → osascript) had several silent failures:
  * osascript return codes were ignored → actions reported success even when
    macOS refused them (no Accessibility permission).
  * cliclick has no scroll command and is ASCII-only → scrolling and
    Vietnamese typing were unreliable (AppleScript ``keystroke`` mangles
    diacritics).
  * window_list / window_get_info / element_* / screenshot_capture were stubs
    returning fake "success" with empty data.

Strategy per action
-------------------
  mouse_*        Quartz CGEvent (precise, Retina-correct points)
  keyboard_type  ASCII short text → Quartz unicode events;
                 non-ASCII / long text → clipboard paste (IME-proof, restores
                 the previous clipboard text)
  keyboard_*     Quartz key events with modifier flags
  clipboard_*    AppKit NSPasteboard
  screenshot     /usr/sbin/screencapture → returns a file path
  window_*       Quartz CGWindowList + NSWorkspace
  element_*      Accessibility API through atomacos (frontmost app)
  browser JS     Chrome DevTools (CDP, port 9222) when available, otherwise
                 Chrome AppleScript ("Allow JavaScript from Apple Events")
  user_confirm   native dialog (osascript display dialog) with timeout

Every action returns ``{"success": bool, "result": ...}`` and never raises
for expected OS failures.  When macOS privacy permissions are missing the
error message says exactly which permission to grant.

Security:
  R-03  No LLMClient import; no LLM API calls.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="daemon")

# ── Optional native frameworks (pyobjc) ──────────────────────────────────
try:
    import Quartz  # type: ignore
    _HAS_QUARTZ = True
except Exception:  # pragma: no cover - non-mac / missing pyobjc
    Quartz = None  # type: ignore
    _HAS_QUARTZ = False

try:
    from ApplicationServices import AXIsProcessTrusted  # type: ignore
except Exception:  # pragma: no cover
    AXIsProcessTrusted = None  # type: ignore

try:
    from AppKit import NSPasteboard, NSPasteboardTypeString, NSWorkspace  # type: ignore
    _HAS_APPKIT = True
except Exception:  # pragma: no cover
    _HAS_APPKIT = False

_CLICLICK_BIN: str | None | bool = False  # False = unchecked

_PERM_ACCESSIBILITY = (
    "macOS chưa cấp quyền Accessibility cho tiến trình đang chạy Phidipus Agents "
    "(thường là Terminal). Mở System Settings → Privacy & Security → "
    "Accessibility và bật cho Terminal, rồi chạy lại."
)
_PERM_SCREEN = (
    "macOS chưa cấp quyền Screen Recording. Mở System Settings → Privacy & "
    "Security → Screen Recording và bật cho Terminal, rồi chạy lại."
)

# ── Key codes (ANSI US physical layout) ──────────────────────────────────
_KEY_CODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53,
    "forwarddelete": 117, "del": 117,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "arrowleft": 123, "arrowright": 124, "arrowdown": 125, "arrowup": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "minus": 27, "equal": 24, "plus": 24, "comma": 43, "period": 47,
    "slash": 44, "semicolon": 41, "quote": 39, "backslash": 42,
    "leftbracket": 33, "rightbracket": 30, "grave": 50,
}

_MODIFIER_ALIASES: dict[str, str] = {
    "command": "cmd", "cmd": "cmd", "meta": "cmd", "super": "cmd", "⌘": "cmd",
    "option": "alt", "alt": "alt", "opt": "alt", "⌥": "alt",
    "shift": "shift", "⇧": "shift",
    "control": "ctrl", "ctrl": "ctrl", "⌃": "ctrl",
    "fn": "fn",
}


def _modifier_flag(mod: str) -> int:
    if not _HAS_QUARTZ:
        return 0
    return {
        "cmd":   Quartz.kCGEventFlagMaskCommand,
        "alt":   Quartz.kCGEventFlagMaskAlternate,
        "shift": Quartz.kCGEventFlagMaskShift,
        "ctrl":  Quartz.kCGEventFlagMaskControl,
        "fn":    Quartz.kCGEventFlagMaskSecondaryFn,
    }.get(mod, 0)


def _split_keys(keys: list[str]) -> list[str]:
    """Accept both ["command","v"] and legacy ["cmd+v"] / ["command+shift+t"]."""
    out: list[str] = []
    for k in keys:
        k = str(k).strip()
        if "+" in k and len(k) > 1:
            out.extend(p.strip() for p in k.split("+") if p.strip())
        else:
            out.append(k)
    return out


def _cliclick_path() -> str | None:
    global _CLICLICK_BIN
    if _CLICLICK_BIN is False:
        _CLICLICK_BIN = shutil.which("cliclick")
    return _CLICLICK_BIN  # type: ignore[return-value]


def _osa(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Run AppleScript. Returns (ok, stdout-or-stderr). Never raises."""
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=timeout)
        if proc.returncode == 0:
            return True, proc.stdout.strip()
        return False, (proc.stderr or proc.stdout).strip()
    except subprocess.TimeoutExpired:
        return False, f"osascript timeout ({timeout}s)"
    except Exception as exc:  # pragma: no cover
        return False, str(exc)


def _as_quote(text: str) -> str:
    """Escape text for an AppleScript double-quoted string literal."""
    return (text.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\r", "\\r").replace("\n", "\\n"))


def _accessibility_ok() -> bool:
    if AXIsProcessTrusted is None:
        return True  # cannot check → assume granted
    try:
        return bool(AXIsProcessTrusted())
    except Exception:
        return True


def _screen_recording_ok() -> bool:
    if not _HAS_QUARTZ or not hasattr(Quartz, "CGPreflightScreenCaptureAccess"):
        return True
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def _post(event: Any) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _need_accessibility() -> dict[str, Any] | None:
    if not _accessibility_ok():
        return {"success": False, "result": _PERM_ACCESSIBILITY,
                "error_code": "PERMISSION_ACCESSIBILITY"}
    return None


class A11yMacOS:
    """macOS automation — Quartz primary, AppleScript fallbacks."""

    def __init__(self) -> None:
        self._last_clipboard_restore: float = 0.0
        _log.info("A11yMacOS v4.3 ready", extra={
            "quartz": _HAS_QUARTZ, "appkit": _HAS_APPKIT,
            "accessibility_granted": _accessibility_ok(),
        })

    # ══════════════════════════════════════════════════════════════════
    # Mouse
    # ══════════════════════════════════════════════════════════════════

    async def mouse_click(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_mouse_click, p)

    def _sync_mouse_click(self, p: dict[str, Any]) -> dict[str, Any]:
        perm = _need_accessibility()
        if perm:
            return perm
        x, y = int(p["x"]), int(p["y"])
        button = p.get("button", "left")
        clicks = max(1, min(3, int(p.get("clicks", 1))))
        interval = float(p.get("interval", 0.0) or 0.0)

        if _HAS_QUARTZ:
            try:
                if button == "right":
                    down, up, btn = (Quartz.kCGEventRightMouseDown,
                                     Quartz.kCGEventRightMouseUp,
                                     Quartz.kCGMouseButtonRight)
                elif button == "middle":
                    down, up, btn = (Quartz.kCGEventOtherMouseDown,
                                     Quartz.kCGEventOtherMouseUp,
                                     Quartz.kCGMouseButtonCenter)
                else:
                    down, up, btn = (Quartz.kCGEventLeftMouseDown,
                                     Quartz.kCGEventLeftMouseUp,
                                     Quartz.kCGMouseButtonLeft)
                pos = (float(x), float(y))
                _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pos, btn))
                time.sleep(0.03)
                for n in range(1, clicks + 1):
                    ev_down = Quartz.CGEventCreateMouseEvent(None, down, pos, btn)
                    ev_up = Quartz.CGEventCreateMouseEvent(None, up, pos, btn)
                    Quartz.CGEventSetIntegerValueField(ev_down, Quartz.kCGMouseEventClickState, n)
                    Quartz.CGEventSetIntegerValueField(ev_up, Quartz.kCGMouseEventClickState, n)
                    _post(ev_down)
                    time.sleep(0.02)
                    _post(ev_up)
                    if n < clicks:
                        time.sleep(interval or 0.06)
                return {"success": True, "result": f"click ({x},{y}) ×{clicks} [{button}]"}
            except Exception as exc:
                _log.debug("Quartz click failed: %s", exc)

        cc = _cliclick_path()
        if cc:
            cmd = "rc" if button == "right" else ("dc" if clicks == 2 else "c")
            r = subprocess.run([cc, f"m:{x},{y}", f"{cmd}:{x},{y}"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return {"success": True, "result": f"cliclick ({x},{y})"}
        ok, out = _osa(f'tell application "System Events" to click at {{{x}, {y}}}')
        return {"success": ok, "result": f"osascript click ({x},{y})" if ok else out}

    async def mouse_move(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_mouse_move, p)

    def _sync_mouse_move(self, p: dict[str, Any]) -> dict[str, Any]:
        perm = _need_accessibility()
        if perm:
            return perm
        x, y = int(p["x"]), int(p["y"])
        if _HAS_QUARTZ:
            try:
                _post(Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved, (float(x), float(y)), Quartz.kCGMouseButtonLeft))
                return {"success": True, "result": f"moved ({x},{y})"}
            except Exception as exc:
                _log.debug("Quartz move failed: %s", exc)
        cc = _cliclick_path()
        if cc and subprocess.run([cc, f"m:{x},{y}"], capture_output=True, timeout=5).returncode == 0:
            return {"success": True, "result": "cliclick moved"}
        return {"success": False, "result": "mouse_move unavailable (Quartz/cliclick missing)"}

    async def mouse_scroll(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_mouse_scroll, p)

    def _sync_mouse_scroll(self, p: dict[str, Any]) -> dict[str, Any]:
        """
        Two payload forms (see ipc/action_schema.py):
          {"x","y","dy"[,"dx"]}            dy/dx in lines, dy > 0 = DOWN
          {"direction","amount"[,"x","y"]} amount in pixels
        """
        perm = _need_accessibility()
        if perm:
            return perm
        direction = p.get("direction")
        if direction:
            amount = int(p.get("amount", 300))
            px_y = amount if direction == "down" else (-amount if direction == "up" else 0)
            px_x = amount if direction == "right" else (-amount if direction == "left" else 0)
            unit = "pixel"
        else:
            px_y = int(p.get("dy", 0))
            px_x = int(p.get("dx", 0))
            unit = "line"
        if px_x == 0 and px_y == 0:
            return {"success": False, "result": "scroll amount is zero"}

        if _HAS_QUARTZ:
            try:
                if "x" in p and "y" in p:
                    _post(Quartz.CGEventCreateMouseEvent(
                        None, Quartz.kCGEventMouseMoved,
                        (float(p["x"]), float(p["y"])), Quartz.kCGMouseButtonLeft))
                    time.sleep(0.02)
                units = (Quartz.kCGScrollEventUnitPixel if unit == "pixel"
                         else Quartz.kCGScrollEventUnitLine)
                # Quartz: positive wheel value scrolls content UP → invert sign.
                remaining_y, remaining_x = -px_y, -px_x
                step = 120 if unit == "pixel" else 3
                while remaining_y or remaining_x:
                    sy = max(-step, min(step, remaining_y))
                    sx = max(-step, min(step, remaining_x))
                    ev = Quartz.CGEventCreateScrollWheelEvent(None, units, 2, int(sy), int(sx))
                    _post(ev)
                    remaining_y -= sy
                    remaining_x -= sx
                    time.sleep(0.012)
                return {"success": True, "result": f"scrolled dy={px_y} dx={px_x} ({unit})"}
            except Exception as exc:
                _log.debug("Quartz scroll failed: %s", exc)

        # Fallback: arrow keys / page keys
        key_code = 125 if px_y > 0 else 126
        repeats = max(1, min(30, abs(px_y) // (40 if unit == "pixel" else 1)))
        ok, out = _osa("tell application \"System Events\"\n" +
                       "\n".join(f"key code {key_code}" for _ in range(repeats)) +
                       "\nend tell")
        return {"success": ok, "result": f"key-scroll ×{repeats}" if ok else out}

    async def mouse_drag(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_drag, p)

    def _sync_drag(self, p: dict[str, Any]) -> dict[str, Any]:
        perm = _need_accessibility()
        if perm:
            return perm
        fx, fy = float(p["from_x"]), float(p["from_y"])
        tx, ty = float(p["to_x"]), float(p["to_y"])
        duration = max(0.1, min(10.0, float(p.get("duration", 0.6) or 0.6)))
        if _HAS_QUARTZ:
            try:
                btn = Quartz.kCGMouseButtonLeft
                _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (fx, fy), btn))
                time.sleep(0.05)
                _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (fx, fy), btn))
                time.sleep(0.15)
                steps = max(8, int(duration * 60))
                for i in range(1, steps + 1):
                    x = fx + (tx - fx) * i / steps
                    y = fy + (ty - fy) * i / steps
                    _post(Quartz.CGEventCreateMouseEvent(
                        None, Quartz.kCGEventLeftMouseDragged, (x, y), btn))
                    time.sleep(duration / steps)
                time.sleep(0.1)
                _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (tx, ty), btn))
                return {"success": True, "result": f"drag ({fx:.0f},{fy:.0f})→({tx:.0f},{ty:.0f})"}
            except Exception as exc:
                _log.debug("Quartz drag failed: %s", exc)
        cc = _cliclick_path()
        if cc:
            r = subprocess.run([cc, f"m:{fx:.0f},{fy:.0f}", f"dd:{fx:.0f},{fy:.0f}",
                                f"dm:{tx:.0f},{ty:.0f}", f"du:{tx:.0f},{ty:.0f}"],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return {"success": True, "result": "cliclick drag"}
        return {"success": False, "result": "drag unavailable (Quartz/cliclick missing)"}

    # ══════════════════════════════════════════════════════════════════
    # Keyboard
    # ══════════════════════════════════════════════════════════════════

    async def keyboard_type(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_type, p)

    def _sync_type(self, p: dict[str, Any]) -> dict[str, Any]:
        perm = _need_accessibility()
        if perm:
            return perm
        text = unicodedata.normalize("NFC", str(p["text"]))
        interval = float(p.get("interval", 0.0) or 0.0)
        is_ascii = all(ord(c) < 128 for c in text)

        # Non-ASCII (Vietnamese) or long text → clipboard paste: immune to
        # input methods (Telex/VNI) and much faster than per-char events.
        if not is_ascii or len(text) > 40:
            res = self._paste_text(text)
            if res["success"]:
                return res

        if _HAS_QUARTZ:
            try:
                self._quartz_type_unicode(text, interval)
                return {"success": True, "result": f"typed {len(text)} chars"}
            except Exception as exc:
                _log.debug("Quartz unicode typing failed: %s", exc)

        res = self._paste_text(text)
        if res["success"]:
            return res
        ok, out = _osa(f'tell application "System Events" to keystroke "{_as_quote(text)}"')
        return {"success": ok, "result": f"osascript typed {len(text)}" if ok else out}

    def _quartz_type_unicode(self, text: str, interval: float = 0.0) -> None:
        # CGEventKeyboardSetUnicodeString accepts at most 20 UTF-16 units/event
        chunk: list[str] = []
        units = 0

        def flush() -> None:
            nonlocal chunk, units
            if not chunk:
                return
            s = "".join(chunk)
            n = len(s.encode("utf-16-le")) // 2
            down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(down, n, s)
            up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            Quartz.CGEventKeyboardSetUnicodeString(up, n, s)
            _post(down)
            _post(up)
            time.sleep(interval or 0.008)
            chunk, units = [], 0

        for ch in text:
            if ch == "\n":
                flush()
                self._press_code(_KEY_CODES["return"], 0)
                continue
            if ch == "\t":
                flush()
                self._press_code(_KEY_CODES["tab"], 0)
                continue
            u = len(ch.encode("utf-16-le")) // 2
            if units + u > 20 or interval:
                flush()
            chunk.append(ch)
            units += u
        flush()

    def _paste_text(self, text: str) -> dict[str, Any]:
        """Clipboard paste with restoration of the previous clipboard text."""
        previous = self._clip_get()
        if not self._clip_set(text):
            return {"success": False, "result": "clipboard unavailable"}
        time.sleep(0.05)
        res = self._sync_hotkey({"keys": ["command", "v"]})
        time.sleep(0.25)
        if previous is not None:
            self._clip_set(previous)
        if res.get("success"):
            return {"success": True, "result": f"pasted {len(text)} chars"}
        return res

    def _press_code(self, code: int, flags: int) -> None:
        down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
        up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
        if flags:
            Quartz.CGEventSetFlags(down, flags)
            Quartz.CGEventSetFlags(up, flags)
        _post(down)
        time.sleep(0.01)
        _post(up)

    async def keyboard_press(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_press, p)

    def _sync_press(self, p: dict[str, Any]) -> dict[str, Any]:
        perm = _need_accessibility()
        if perm:
            return perm
        key = str(p["key"]).strip()
        presses = max(1, min(10, int(p.get("presses", 1))))
        code = _KEY_CODES.get(key.lower())
        if _HAS_QUARTZ and code is not None:
            try:
                for _ in range(presses):
                    self._press_code(code, 0)
                    time.sleep(0.03)
                return {"success": True, "result": f"pressed {key} ×{presses}"}
            except Exception as exc:
                _log.debug("Quartz press failed: %s", exc)
        if code is None and len(key) == 1:
            return self._sync_type({"text": key * presses})
        if code is not None:
            ok, out = _osa("\n".join(
                f'tell application "System Events" to key code {code}' for _ in range(presses)))
            return {"success": ok, "result": f"osascript pressed {key}" if ok else out}
        return {"success": False, "result": f"unknown key {key!r}"}

    async def keyboard_hotkey(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_hotkey, p)

    def _sync_hotkey(self, p: dict[str, Any]) -> dict[str, Any]:
        perm = _need_accessibility()
        if perm:
            return perm
        keys = _split_keys(list(p["keys"]))
        mods = [_MODIFIER_ALIASES[k.lower()] for k in keys if k.lower() in _MODIFIER_ALIASES]
        main = [k for k in keys if k.lower() not in _MODIFIER_ALIASES]
        if not main:
            return {"success": False, "result": "hotkey has no main key"}
        main_key = main[0].lower()
        code = _KEY_CODES.get(main_key)
        if code is None:
            return {"success": False, "result": f"unknown key {main_key!r}"}
        if _HAS_QUARTZ:
            try:
                flags = 0
                for m in mods:
                    flags |= _modifier_flag(m)
                self._press_code(code, flags)
                return {"success": True, "result": f"hotkey {'+'.join(mods + [main_key])}"}
            except Exception as exc:
                _log.debug("Quartz hotkey failed: %s", exc)
        osa_mods = {"cmd": "command down", "alt": "option down",
                    "shift": "shift down", "ctrl": "control down"}
        using = ", ".join(osa_mods[m] for m in mods if m in osa_mods)
        script = f'tell application "System Events" to key code {code}'
        if using:
            script += f" using {{{using}}}"
        ok, out = _osa(script)
        return {"success": ok, "result": f"osascript hotkey {keys}" if ok else out}

    # ══════════════════════════════════════════════════════════════════
    # Clipboard
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _clip_get() -> str | None:
        if _HAS_APPKIT:
            try:
                val = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
                return None if val is None else str(val)
            except Exception:
                pass
        try:
            r = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
            return r.stdout.decode("utf-8", errors="replace")
        except Exception:
            return None

    @staticmethod
    def _clip_set(text: str) -> bool:
        if _HAS_APPKIT:
            try:
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                if pb.setString_forType_(text, NSPasteboardTypeString):
                    return True
            except Exception:
                pass
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5, check=True)
            return True
        except Exception:
            return False

    async def clipboard_copy(self, p: dict[str, Any]) -> dict[str, Any]:
        ok = await asyncio.to_thread(self._clip_set, str(p.get("text", "")))
        return {"success": ok, "result": "copied" if ok else "clipboard unavailable"}

    async def clipboard_paste(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_hotkey, {"keys": ["command", "v"]})

    async def clipboard_get(self, p: dict[str, Any]) -> dict[str, Any]:
        val = await asyncio.to_thread(self._clip_get)
        if val is None:
            return {"success": False, "result": "clipboard unavailable"}
        return {"success": True, "result": val[:8000]}

    # ══════════════════════════════════════════════════════════════════
    # Screen / windows
    # ══════════════════════════════════════════════════════════════════

    async def screenshot_capture(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_screenshot, p)

    def _sync_screenshot(self, p: dict[str, Any]) -> dict[str, Any]:
        if not _screen_recording_ok():
            return {"success": False, "result": _PERM_SCREEN,
                    "error_code": "PERMISSION_SCREEN_RECORDING"}
        save_as = str(p.get("save_as", "")).strip()
        if save_as:
            dest = Path(save_as).expanduser()
            safe_roots = [Path.home() / d for d in ("Desktop", "Downloads", "Documents", "Phidipus")]
            safe_roots.append(Path("/tmp"))
            try:
                resolved = dest.resolve()
                if not any(str(resolved).startswith(str(r.resolve())) for r in safe_roots):
                    return {"success": False, "result": f"save_as outside allowed folders: {dest}"}
            except Exception:
                return {"success": False, "result": f"invalid save_as: {save_as}"}
        else:
            out_dir = Path(tempfile.gettempdir()) / "phidipus_screens"
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"screen_{int(time.time() * 1000)}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["/usr/sbin/screencapture", "-x"]
        region = p.get("region")
        if isinstance(region, dict):
            cmd += ["-R", f"{region['x']},{region['y']},{region['width']},{region['height']}"]
        cmd.append(str(dest))
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
        except Exception as exc:
            return {"success": False, "result": str(exc)}
        if r.returncode != 0 or not dest.exists():
            return {"success": False, "result": (r.stderr or b"screencapture failed").decode(errors="replace")}
        return {"success": True, "result": {"path": str(dest), "bytes": dest.stat().st_size}}

    @staticmethod
    def _windows() -> list[dict[str, Any]]:
        if not _HAS_QUARTZ:
            return []
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        info = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
        out = []
        for w in info:
            if int(w.get("kCGWindowLayer", 0)) != 0:
                continue
            b = w.get("kCGWindowBounds", {}) or {}
            out.append({
                "app": str(w.get("kCGWindowOwnerName", "")),
                "title": str(w.get("kCGWindowName", "") or ""),
                "pid": int(w.get("kCGWindowOwnerPID", 0)),
                "window_id": int(w.get("kCGWindowNumber", 0)),
                "x": int(b.get("X", 0)), "y": int(b.get("Y", 0)),
                "width": int(b.get("Width", 0)), "height": int(b.get("Height", 0)),
            })
        return out

    async def window_list(self, p: dict[str, Any]) -> dict[str, Any]:
        wins = await asyncio.to_thread(self._windows)
        if not wins and not _HAS_QUARTZ:
            return {"success": False, "result": "Quartz unavailable"}
        return {"success": True, "result": wins[:40]}

    async def window_get_info(self, p: dict[str, Any]) -> dict[str, Any]:
        def _info() -> dict[str, Any]:
            front: dict[str, Any] = {}
            if _HAS_APPKIT:
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                if app is not None:
                    front = {"app": str(app.localizedName()), "pid": int(app.processIdentifier()),
                             "bundle_id": str(app.bundleIdentifier() or "")}
            wins = [w for w in self._windows() if front and w["pid"] == front.get("pid")]
            front["windows"] = wins[:5]
            return front
        info = await asyncio.to_thread(_info)
        return {"success": bool(info), "result": info}

    async def window_focus(self, p: dict[str, Any]) -> dict[str, Any]:
        title = str(p.get("title", "")).strip()
        pid = p.get("pid")

        def _focus() -> dict[str, Any]:
            if _HAS_APPKIT:
                try:
                    from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps  # type: ignore
                    target = None
                    if pid:
                        target = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
                    if target is None and title:
                        for app in NSWorkspace.sharedWorkspace().runningApplications():
                            name = str(app.localizedName() or "")
                            if name.lower() == title.lower():
                                target = app
                                break
                        if target is None:
                            for w in self._windows():
                                if title.lower() in (w["title"] + " " + w["app"]).lower():
                                    target = NSRunningApplication.runningApplicationWithProcessIdentifier_(w["pid"])
                                    break
                    if target is not None:
                        target.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                        return {"success": True, "result": f"focused {target.localizedName()}"}
                except Exception as exc:
                    _log.debug("AppKit focus failed: %s", exc)
            if title:
                ok, out = _osa(f'tell application "{_as_quote(title)}" to activate')
                return {"success": ok, "result": f"activated {title}" if ok else out}
            return {"success": False, "result": "window_focus needs title or pid"}

        return await asyncio.to_thread(_focus)

    # ══════════════════════════════════════════════════════════════════
    # Accessibility elements (frontmost app)
    # element_id format returned by element_find:
    #   "ax:<x>,<y>,<w>,<h>|<role>|<title>"
    # Any other string is treated as a title/description to search for.
    # ══════════════════════════════════════════════════════════════════

    _BY_ATTR = {
        "name": "AXTitle", "label": "AXDescription", "description": "AXDescription",
        "role": "AXRole", "value": "AXValue",
    }

    @staticmethod
    def _ax_frontmost():
        import atomacos  # type: ignore
        return atomacos.getFrontmostApp()

    @staticmethod
    def _ax_describe(el: Any) -> dict[str, Any] | None:
        try:
            pos = el.AXPosition
            size = el.AXSize
            x, y, w, h = int(pos.x), int(pos.y), int(size.width), int(size.height)
        except Exception:
            return None
        def attr(name: str) -> str:
            try:
                v = getattr(el, name)
                return "" if v is None else str(v)
            except Exception:
                return ""
        role, title = attr("AXRole"), attr("AXTitle") or attr("AXDescription")
        return {
            "element_id": f"ax:{x},{y},{w},{h}|{role}|{title[:80]}",
            "role": role, "title": title[:200], "value": attr("AXValue")[:200],
            "x": x, "y": y, "width": w, "height": h,
            "center_x": x + w // 2, "center_y": y + h // 2,
        }

    def _ax_search(self, by: str, query: str, limit: int) -> list[dict[str, Any]]:
        attr = self._BY_ATTR.get(by, "AXTitle")
        app = self._ax_frontmost()
        found = []
        for pattern in (query, f"*{query}*"):
            try:
                found = app.findAllR(**{attr: pattern})
            except Exception:
                found = []
            if found:
                break
        out = []
        for el in found:
            d = self._ax_describe(el)
            if d:
                out.append(d)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _parse_element_id(element_id: str) -> tuple[int, int] | None:
        if element_id.startswith("ax:"):
            try:
                geo = element_id[3:].split("|", 1)[0]
                x, y, w, h = (int(v) for v in geo.split(","))
                return x + w // 2, y + h // 2
            except Exception:
                return None
        return None

    async def element_find(self, p: dict[str, Any]) -> dict[str, Any]:
        by, query = str(p.get("by", "name")), str(p.get("query", ""))
        limit = max(1, min(100, int(p.get("limit", 10))))
        try:
            res = await asyncio.wait_for(asyncio.to_thread(self._ax_search, by, query, limit), timeout=8)
        except asyncio.TimeoutError:
            return {"success": False, "result": "element_find timeout (AX tree too large)"}
        except ImportError:
            return {"success": False, "result": "atomacos not installed"}
        except Exception as exc:
            return {"success": False, "result": f"element_find error: {exc}"}
        if not res and not _accessibility_ok():
            return {"success": False, "result": _PERM_ACCESSIBILITY}
        return {"success": True, "result": res}

    async def _resolve_element_center(self, element_id: str) -> tuple[int, int] | None:
        center = self._parse_element_id(element_id)
        if center:
            return center
        found = await self.element_find({"by": "name", "query": element_id, "limit": 1})
        if not found.get("success") or not found.get("result"):
            found = await self.element_find({"by": "description", "query": element_id, "limit": 1})
        if found.get("success") and found.get("result"):
            first = found["result"][0]
            return first["center_x"], first["center_y"]
        return None

    async def element_click(self, p: dict[str, Any]) -> dict[str, Any]:
        center = await self._resolve_element_center(str(p["element_id"]))
        if not center:
            return {"success": False, "result": f"element not found: {p['element_id']!r}"}
        return await self.mouse_click({"x": center[0], "y": center[1]})

    async def element_get_text(self, p: dict[str, Any]) -> dict[str, Any]:
        element_id = str(p["element_id"])
        title = element_id.split("|")[-1] if element_id.startswith("ax:") else element_id
        found = await self.element_find({"by": "name", "query": title, "limit": 1})
        if found.get("success") and found.get("result"):
            first = found["result"][0]
            return {"success": True, "result": first.get("value") or first.get("title", "")}
        return {"success": False, "result": f"element not found: {element_id!r}"}

    async def element_set_value(self, p: dict[str, Any]) -> dict[str, Any]:
        click = await self.element_click({"element_id": p["element_id"]})
        if not click.get("success"):
            return click
        await asyncio.sleep(0.15)
        await self.keyboard_hotkey({"keys": ["command", "a"]})
        value = str(p.get("value", ""))
        if not value:
            return await self.keyboard_press({"key": "delete"})
        return await self.keyboard_type({"text": value})

    # ══════════════════════════════════════════════════════════════════
    # v4.3 Keyboard-first layer — UI state + menus, no screenshots
    # ══════════════════════════════════════════════════════════════════
    # Reading the focused window / element through Accessibility costs a few
    # milliseconds, while a screenshot + VLM call costs 1.5-25 s.  The agent
    # uses ui_snapshot to verify keyboard actions and menu_list / menu_select
    # to run app commands exactly (no coordinates, any UI language).

    _BROWSER_URL_SCRIPTS = {
        "com.google.Chrome": 'tell application "Google Chrome" to return URL of active tab of front window',
        "com.brave.Browser": 'tell application "Brave Browser" to return URL of active tab of front window',
        "com.microsoft.edgemac": 'tell application "Microsoft Edge" to return URL of active tab of front window',
        "company.thebrowser.Browser": 'tell application "Arc" to return URL of active tab of front window',
        "com.apple.Safari": 'tell application "Safari" to return URL of front document',
    }
    _TEXT_ROLES = ("AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXSecureTextField")
    _SKIP_MENUS = ("services", "dich vu", "open recent", "mo gan day", "recent items")
    # menu glyph codes (Carbon kMenu*Glyph) → key names
    _GLYPH_KEYS = {23: "delete", 10: "forwarddelete", 27: "escape", 11: "return", 4: "enter",
                   2: "tab", 9: "space", 100: "left", 101: "right", 104: "up", 106: "down",
                   98: "pageup", 107: "pagedown", 115: "home", 119: "end"}

    @staticmethod
    def _fold(text: str) -> str:
        t = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "D")
        t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
        return " ".join(t.replace("…", " ").replace("...", " ").split())

    @staticmethod
    def _attr(el: Any, name: str, default: Any = None) -> Any:
        try:
            v = getattr(el, name)
            return default if v is None else v
        except Exception:
            return default

    def _ax_app(self, app: str = "") -> Any:
        import atomacos  # type: ignore
        if app:
            for getter in (atomacos.getAppRefByLocalizedName, atomacos.getAppRefByBundleId):
                try:
                    return getter(app)
                except Exception:
                    continue
            raise ValueError(f"ứng dụng chưa chạy: {app}")
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        return atomacos.getAppRefByPid(int(front.processIdentifier()))

    async def ui_snapshot(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_ui_snapshot, p)

    def _sync_ui_snapshot(self, p: dict[str, Any]) -> dict[str, Any]:
        if not _HAS_APPKIT:
            return {"success": False, "result": "AppKit unavailable"}
        max_chars = int(p.get("max_chars", 2000))
        include_value = bool(p.get("include_value", True))
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        snap: dict[str, Any] = {
            "app": str(front.localizedName() or ""),
            "bundle_id": str(front.bundleIdentifier() or ""),
            "pid": int(front.processIdentifier()),
        }
        if _accessibility_ok():
            try:
                import atomacos  # type: ignore
                ax = atomacos.getAppRefByPid(snap["pid"])
                win = self._attr(ax, "AXFocusedWindow")
                if win is not None:
                    snap["window_title"] = str(self._attr(win, "AXTitle", ""))[:300]
                    pos, size = self._attr(win, "AXPosition"), self._attr(win, "AXSize")
                    if pos is not None and size is not None:
                        snap["window_frame"] = [int(pos.x), int(pos.y), int(size.width), int(size.height)]
                    doc = self._attr(win, "AXDocument", "")
                    if doc:
                        snap["document"] = str(doc)[:500]
                    kids = self._attr(win, "AXChildren", []) or []
                    if any(str(self._attr(k, "AXRole", "")) == "AXSheet" for k in kids):
                        snap["sheet_open"] = True
                snap["window_count"] = len(self._attr(ax, "AXWindows", []) or [])
                foc = self._attr(ax, "AXFocusedUIElement")
                if foc is not None:
                    role = str(self._attr(foc, "AXRole", ""))
                    snap["focused_role"] = role
                    label = (self._attr(foc, "AXTitle", "") or self._attr(foc, "AXDescription", "")
                             or self._attr(foc, "AXPlaceholderValue", ""))
                    if label:
                        snap["focused_label"] = str(label)[:200]
                    is_text = role in self._TEXT_ROLES
                    snap["focused_is_text"] = is_text
                    if include_value and is_text and role != "AXSecureTextField":
                        val = self._attr(foc, "AXValue", "")
                        if isinstance(val, str):
                            snap["focused_value"] = val[:max_chars]
                    sel = self._attr(foc, "AXSelectedText", "")
                    if isinstance(sel, str) and sel:
                        snap["selected_text"] = sel[:max_chars]
            except Exception as exc:
                snap["ax_error"] = str(exc)[:160]
        else:
            snap["ax_error"] = "missing Accessibility permission"
        script = self._BROWSER_URL_SCRIPTS.get(snap["bundle_id"])
        if script:
            ok, out = _osa(script, timeout=3)
            if ok and out:
                snap["url"] = out[:500]
        return {"success": True, "result": snap}

    def _shortcut_text(self, item: Any) -> str:
        char = str(self._attr(item, "AXMenuItemCmdChar", "") or "").strip()
        vk = self._attr(item, "AXMenuItemCmdVirtualKey", None)
        glyph = self._attr(item, "AXMenuItemCmdGlyph", None)
        if not char and vk is None and not glyph:
            return ""
        mods = int(self._attr(item, "AXMenuItemCmdModifiers", 0) or 0)
        parts = []
        if mods & 4:
            parts.append("ctrl")
        if mods & 2:
            parts.append("alt")
        if mods & 1:
            parts.append("shift")
        if not mods & 8:
            parts.append("cmd")
        key = ""
        if char:
            key = {" ": "space"}.get(char, char.lower())
        elif vk is not None:
            key = next((k for k, c in _KEY_CODES.items() if c == int(vk) and len(k) > 1), "")
        if not key and glyph is not None:
            key = self._GLYPH_KEYS.get(int(glyph), "")
        return "+".join(parts + [key]) if key else ""

    async def menu_list(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._sync_menu_list, p), timeout=12)
        except asyncio.TimeoutError:
            return {"success": False, "result": "menu_list timeout (menu quá lớn)"}

    def _sync_menu_list(self, p: dict[str, Any]) -> dict[str, Any]:
        perm = _need_accessibility()
        if perm:
            return perm
        try:
            app = self._ax_app(str(p.get("app", "") or ""))
        except ImportError:
            return {"success": False, "result": "atomacos not installed"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}
        bar = self._attr(app, "AXMenuBar")
        if bar is None:
            return {"success": False, "result": "ứng dụng không có thanh menu"}
        flt = self._fold(p.get("filter", ""))
        max_items = int(p.get("max_items", 400))
        items: list[dict[str, Any]] = []

        def submenu(el: Any) -> Any:
            for c in self._attr(el, "AXChildren", []) or []:
                if str(self._attr(c, "AXRole", "")) == "AXMenu":
                    return c
            return None

        def walk(menu: Any, path: list[str], depth: int) -> None:
            for child in self._attr(menu, "AXChildren", []) or []:
                if len(items) >= max_items:
                    return
                title = str(self._attr(child, "AXTitle", "") or "").strip()
                if not title:
                    continue  # separator
                sub = submenu(child)
                new_path = path + [title]
                if sub is not None:
                    if depth < 3 and self._fold(title) not in self._SKIP_MENUS:
                        walk(sub, new_path, depth + 1)
                    continue
                if flt and flt not in self._fold(" > ".join(new_path)):
                    continue
                items.append({"path": new_path, "enabled": bool(self._attr(child, "AXEnabled", True)),
                              "shortcut": self._shortcut_text(child)})

        for idx, top in enumerate(self._attr(bar, "AXChildren", []) or []):
            title = str(self._attr(top, "AXTitle", "") or "").strip()
            if idx == 0 or not title or self._fold(title) == "apple":
                continue  # Apple menu (restart / shut down / log out) is never exposed
            sub = submenu(top)
            if sub is not None:
                walk(sub, [title], 1)
            if len(items) >= max_items:
                break
        app_name = str(self._attr(app, "AXTitle", "") or "")
        return {"success": True, "result": {"app": app_name, "items": items}}

    _DESTRUCTIVE_MENU = (
        "delete", "xoa", "remove", "go bo", "empty trash", "don sach", "thung rac", "erase",
        "move to trash", "discard", "revert", "uninstall", "go cai dat", "clear history",
        "xoa lich su", "reset", "dat lai", "force quit", "buoc thoat", "log out", "dang xuat",
    )

    def _match_child(self, parent: Any, title: str, roles: tuple[str, ...]) -> tuple[Any, list[str]]:
        want = self._fold(title)
        cands = []
        for c in self._attr(parent, "AXChildren", []) or []:
            if roles and str(self._attr(c, "AXRole", "")) not in roles:
                continue
            t = str(self._attr(c, "AXTitle", "") or "").strip()
            if t:
                cands.append((self._fold(t), t, c))
        for f, _t, c in cands:
            if f == want:
                return c, []
        starts = [c for f, _t, c in cands if f.startswith(want)]
        if len(starts) == 1:
            return starts[0], []
        contains = [c for f, _t, c in cands if want and want in f]
        if len(contains) == 1:
            return contains[0], []
        return None, [t for _f, t, _c in cands][:40]

    async def menu_select(self, p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync_menu_select, p)

    def _sync_menu_select(self, p: dict[str, Any]) -> dict[str, Any]:
        perm = _need_accessibility()
        if perm:
            return perm
        path = [str(x).strip() for x in p["path"]]
        if self._fold(path[0]) == "apple":
            return {"success": False, "result": "menu Apple (khởi động lại / tắt máy / đăng xuất) không được phép"}
        try:
            app = self._ax_app(str(p.get("app", "") or ""))
        except ImportError:
            return {"success": False, "result": "atomacos not installed"}
        except Exception as exc:
            return {"success": False, "result": str(exc)}
        bar = self._attr(app, "AXMenuBar")
        if bar is None:
            return {"success": False, "result": "ứng dụng không có thanh menu"}
        top, avail = self._match_child(bar, path[0], ("AXMenuBarItem",))
        if top is None:
            return {"success": False, "result": f"không thấy menu {path[0]!r}; có: {avail}"}
        node = top
        for title in path[1:]:
            menu = next((c for c in self._attr(node, "AXChildren", []) or []
                         if str(self._attr(c, "AXRole", "")) == "AXMenu"), None)
            if menu is None:
                return {"success": False, "result": f"{title!r}: mục cha không có menu con"}
            node, avail = self._match_child(menu, title, ("AXMenuItem",))
            if node is None:
                return {"success": False, "result": f"không thấy mục {title!r}; có: {avail}"}
        if not self._attr(node, "AXEnabled", True):
            return {"success": False, "result": f"mục menu đang bị vô hiệu: {' > '.join(path)}"}
        full = " > ".join(path)
        folded = self._fold(full)
        if p.get("require_confirm") or any(w in folded for w in self._DESTRUCTIVE_MENU):
            from automation.builtin_skills import BuiltinSkills
            answer = BuiltinSkills._confirm_dialog_macos(
                f"Phidipus Agents muốn chọn menu: {full}",
                "Thao tác này có thể xoá hoặc thay đổi dữ liệu.", 60)
            if answer != "confirmed":
                return {"success": False, "result": f"người dùng {answer}: {full}", "error_code": "USER_DENIED"}
        errors = []
        for attempt in ("direct", "open_then_press"):
            try:
                if attempt == "open_then_press":
                    top.Press()
                    time.sleep(0.2)
                node.Press()
                return {"success": True, "result": f"menu: {full}"}
            except Exception as exc:
                errors.append(str(exc)[:80])
        # AppleScript fallback (System Events UI scripting)
        proc = str(self._attr(app, "AXTitle", "") or "")
        ref = f'menu bar item "{_as_quote(path[0])}" of menu bar 1'
        ref = f'menu "{_as_quote(path[0])}" of {ref}'
        for t in path[1:-1]:
            ref = f'menu "{_as_quote(t)}" of menu item "{_as_quote(t)}" of {ref}'
        script = (f'tell application "System Events" to tell process "{_as_quote(proc)}" '
                  f'to click menu item "{_as_quote(path[-1])}" of {ref}')
        ok, out = _osa(script, timeout=8)
        if ok:
            return {"success": True, "result": f"menu (osascript): {full}"}
        return {"success": False, "result": f"không bấm được menu {full}: {errors} / {out[:120]}"}

    # ══════════════════════════════════════════════════════════════════
    # Browser JS execution
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _cdp_eval(js_code: str, timeout_s: float) -> dict[str, Any] | None:
        """Evaluate JS in the active Chrome tab via CDP. None → CDP unavailable."""
        try:
            with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=1.5) as r:
                tabs = json.loads(r.read().decode("utf-8"))
        except Exception:
            return None
        pages = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                 and not str(t.get("url", "")).startswith(("chrome://", "devtools://", "chrome-extension://"))]
        if not pages:
            return None
        ws_url = pages[0]["webSocketDebuggerUrl"]
        try:
            from websockets.sync.client import connect  # type: ignore
        except Exception:
            return None
        try:
            with connect(ws_url, open_timeout=3, close_timeout=1, max_size=8 * 1024 * 1024) as ws:
                ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": js_code, "returnByValue": True, "awaitPromise": True,
                    "timeout": int(timeout_s * 1000)}}))
                deadline = time.time() + timeout_s + 2
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=max(0.5, deadline - time.time())))
                    if msg.get("id") != 1:
                        continue
                    res = msg.get("result", {})
                    if "exceptionDetails" in res:
                        detail = res["exceptionDetails"].get("text", "JS exception")
                        return {"success": False, "result": f"js_error:{detail}"}
                    val = res.get("result", {}).get("value")
                    if val is None:
                        val = ""
                    if not isinstance(val, str):
                        val = json.dumps(val, ensure_ascii=False)
                    return {"success": True, "result": val[:12000], "via": "cdp"}
        except Exception as exc:
            _log.debug("CDP eval failed: %s", exc)
            return None
        return None

    async def browser_execute_js(self, p: dict[str, Any]) -> dict[str, Any]:
        js_code = str(p.get("js_code", ""))
        timeout_s = float(p.get("timeout_s", 8.0))
        if not js_code:
            return {"success": False, "result": "empty js_code"}

        cdp = await asyncio.to_thread(self._cdp_eval, js_code, timeout_s)
        if cdp is not None:
            return cdp

        script = (
            'tell application "Google Chrome"\n'
            '    if (count of windows) = 0 then return "js_error:no Chrome window"\n'
            '    try\n'
            f'        set r to execute active tab of front window javascript "{_as_quote(js_code)}"\n'
            '        if r is missing value then return ""\n'
            '        return r as string\n'
            '    on error errMsg\n'
            '        return "js_error:" & errMsg\n'
            '    end try\n'
            'end tell'
        )
        ok, out = await asyncio.to_thread(_osa, script, min(timeout_s + 2.0, 32.0))
        if not ok:
            return {"success": False, "result": out}
        if out.startswith("js_error:"):
            hint = ""
            if "JavaScript" in out and ("Apple Events" in out or "turned off" in out or "tắt" in out):
                hint = (" — bật Chrome: View → Developer → Allow JavaScript from Apple Events, "
                        "hoặc mở Chrome bằng Phidipus_Agent.command (CDP)")
            return {"success": False, "result": out + hint}
        return {"success": True, "result": out[:12000], "via": "applescript"}
