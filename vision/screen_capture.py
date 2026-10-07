# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/screen_capture.py — Phidipus v1.0
Screen capture using PIL.ImageGrab exclusively — no subprocess, no pyautogui.

Architecture contract (R-01 / R-02 / H-8):
  "Remove scrot/ImageMagick subprocess fallback (H-8).
   R-01: orchestrator MUST NOT import pyautogui.
   PIL.ImageGrab is the sole capture backend — read-only, no automation."

ScreenCapture provides a single method to capture the current screen or
a region of it.  The v9.10 implementation had a subprocess fallback that
called scrot or ImageMagick when pyautogui was unavailable.  This fallback
is removed in v9.11 (H-8) because:
  1. subprocess calls are prohibited in the orchestrator (R-02).
  2. scrot/ImageMagick are external processes that could be replaced or
     manipulated by an attacker.

pyautogui is also NOT used, even for screen capture only, because R-01
prohibits any pyautogui import in the orchestrator process.
PIL.ImageGrab.grab() is a passive read-only API — it performs no OS
automation and is safe to use in L1.

Note: ScreenCapture is in the orchestrator process (L1) because screenshots
are used as input to the vision pipeline, not as OS automation output.  The
actual mouse/keyboard OS actions remain in the automation daemon (L2).

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui import of any kind — PIL.ImageGrab is the only
        capture backend (read-only screen reader, no automation).
  R-02  No subprocess.run(), os.system(), scrot, or ImageMagick calls.
  H-8   scrot/ImageMagick subprocess fallback removed — PIL.ImageGrab only.

Used by:
  vision/perception_pipeline.py — capture() for VLM input
  core/agent_loop.py            — capture for screen-state observations

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# Region dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureRegion:
    """
    Screen region for partial screenshot.

    Attributes:
        x:      Left edge in pixels.
        y:      Top edge in pixels.
        width:  Width in pixels.
        height: Height in pixels.
    """

    x:      int
    y:      int
    width:  int
    height: int

    def to_box(self) -> tuple[int, int, int, int]:
        """Return (left, top, right, bottom) box for PIL."""
        return (self.x, self.y, self.x + self.width, self.y + self.height)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CaptureError(RuntimeError):
    """
    Raised when screen capture fails.

    Attributes:
        reason: Short machine-readable reason code.
    """

    def __init__(self, message: str, *, reason: str = "CAPTURE_ERROR") -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"[{self.reason}] {super().__str__()}"


# ---------------------------------------------------------------------------
# ScreenCapture
# ---------------------------------------------------------------------------

class ScreenCapture:
    """
    Screen capture using PIL.ImageGrab exclusively.

    No subprocess, no scrot, no ImageMagick, no pyautogui (R-01, R-02, H-8).

    Usage::

        sc = ScreenCapture()

        # Full screen as PIL Image:
        img = sc.capture()

        # Partial region:
        img = sc.capture(region=CaptureRegion(x=0, y=0, width=800, height=600))

        # As PNG bytes:
        png_bytes = sc.capture_bytes()
    """

    def __init__(self) -> None:
        # Verify capture backend at construction time so failures are early.
        self._backend = self._detect_backend()
        _log.info(
            "ScreenCapture initialised",
            extra={"backend": self._backend},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture(self, region: CaptureRegion | None = None) -> Any:
        """
        Capture the screen and return a PIL Image object.

        Args:
            region: Optional region to capture.  Full screen if None.

        Returns:
            PIL.Image.Image object.

        Raises:
            CaptureError: if capture fails.
        """
        try:
            if self._backend == "imagegrab":
                return self._capture_imagegrab(region)
            else:
                raise CaptureError(
                    "No screen capture backend available.  "
                    "Install Pillow (pip install Pillow).",
                    reason="NO_BACKEND",
                )
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(
                f"Screen capture failed ({self._backend}): {exc}",
                reason="CAPTURE_FAILED",
            ) from exc

    def capture_bytes(
        self,
        region: CaptureRegion | None = None,
        fmt:    str = "JPEG",
        quality: int = 80,
    ) -> bytes:
        """
        Capture the screen and return raw image bytes.

        Phase 1 upgrade: default changed from PNG (~3MB) to JPEG q=80
        (~200KB) for ~15× smaller payload to VLM, reducing transfer
        and processing time by ~30%.

        Args:
            region:  Optional region to capture.
            fmt:     PIL image format (default "JPEG").
            quality: JPEG quality 1–95 (default 80, ignored for PNG).

        Returns:
            Image bytes in the specified format.

        Raises:
            CaptureError: if capture fails.
        """
        img = self.capture(region)
        # JPEG does not support RGBA — convert to RGB if needed.
        if fmt.upper() == "JPEG" and img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        save_kwargs: dict[str, Any] = {"format": fmt}
        if fmt.upper() == "JPEG":
            save_kwargs["quality"] = quality
            save_kwargs["optimize"] = True
        img.save(buf, **save_kwargs)
        return buf.getvalue()

    def get_screen_size(self) -> tuple[int, int]:
        """
        Return (width, height) of the primary screen in pixels.

        Uses PIL.ImageGrab exclusively (R-01: no pyautogui).

        Raises:
            CaptureError: if screen size cannot be determined.
        """
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            return img.size
        except Exception as exc:
            raise CaptureError(
                f"Cannot determine screen size: {exc}",
                reason="SIZE_FAILED",
            ) from exc

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_backend() -> str:
        """
        Return "imagegrab" if PIL.ImageGrab is available, else "none".

        R-01: pyautogui is NOT used — it is an OS automation library
        forbidden in the orchestrator process.  PIL.ImageGrab is a
        read-only capture library with no automation capability.
        """
        try:
            from PIL import ImageGrab  # noqa: F401
            return "imagegrab"
        except ImportError:
            pass
        _log.warning(
            "ScreenCapture: PIL.ImageGrab not available — install Pillow"
        )
        return "none"

    @staticmethod
    def _capture_imagegrab(region: CaptureRegion | None) -> Any:
        """
        Capture via PIL.ImageGrab.grab() exclusively (R-01, R-02, H-8).

        PIL.ImageGrab is a passive read-only screen reader — it performs
        no OS automation.  pyautogui is intentionally NOT used here even
        though it provides a screenshot() function, because R-01 forbids
        any pyautogui import in the orchestrator process.
        """
        from PIL import ImageGrab
        if region is None:
            return ImageGrab.grab()
        return ImageGrab.grab(bbox=region.to_box())
