# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/platform_adapter.py — Phidipus v4.3
═══════════════════════════════════════════

Small cross-platform helpers used by workflow nodes.

FIX v4.3: core/workflow_executor.py imported ``take_screenshot`` and
``clipboard_paste`` from this module, but the file did not exist — every
``screenshot`` node and every ``ai_process`` node with
``input_from="clipboard"`` failed with ImportError.

Functions
---------
  take_screenshot(path="")  → str   async, returns saved PNG path
  clipboard_paste()         → str   current clipboard text
  clipboard_copy(text)      → bool  put text on the clipboard

Screenshots are written to ~/Phidipus/screenshots/ by default (a user-visible
folder inside the allowed safe roots) unless an explicit path is given.
"""
from __future__ import annotations

import asyncio
import platform
import subprocess
import time
from pathlib import Path

_SCREENSHOT_DIR = Path.home() / "Phidipus" / "screenshots"


def _default_path() -> Path:
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return _SCREENSHOT_DIR / f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"


def _capture_sync(dest: Path) -> str:
    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 1) Project ScreenCapture (PIL.ImageGrab) — no subprocess
    try:
        from vision.screen_capture import ScreenCapture
        data = ScreenCapture().capture_bytes(fmt="PNG")
        if data:
            dest.write_bytes(data)
            return str(dest)
    except Exception:
        pass

    # 2) macOS screencapture CLI (-x = no shutter sound)
    if platform.system() == "Darwin":
        proc = subprocess.run(["screencapture", "-x", str(dest)],
                              capture_output=True, timeout=15)
        if proc.returncode == 0 and dest.exists():
            return str(dest)
        raise RuntimeError(
            "screencapture thất bại — cấp quyền Screen Recording cho Terminal "
            "(System Settings → Privacy & Security → Screen Recording)."
        )

    # 3) Generic PIL fallback
    from PIL import ImageGrab  # type: ignore
    ImageGrab.grab().save(str(dest))
    return str(dest)


async def take_screenshot(path: str = "") -> str:
    """Capture the full screen to *path* (or a timestamped default) and return the path."""
    dest = Path(path).expanduser() if path else _default_path()
    if dest.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        dest = dest / _default_path().name if (dest.exists() and dest.is_dir()) else dest.with_suffix(".png")
    return await asyncio.to_thread(_capture_sync, dest)


def clipboard_paste() -> str:
    """Return the current clipboard text ('' when empty / unavailable)."""
    if platform.system() == "Darwin":
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString  # type: ignore
            text = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
            return str(text or "")
        except Exception:
            pass
        try:
            out = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
            return out.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        import pyperclip  # type: ignore
        return pyperclip.paste() or ""
    except Exception:
        return ""


def clipboard_copy(text: str) -> bool:
    """Put *text* on the clipboard. Returns True on success."""
    if platform.system() == "Darwin":
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString  # type: ignore
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            return bool(pb.setString_forType_(text, NSPasteboardTypeString))
        except Exception:
            pass
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5, check=True)
            return True
        except Exception:
            pass
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return True
    except Exception:
        return False
