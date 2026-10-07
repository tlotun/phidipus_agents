# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/diff_capture.py — Phidipus v2.3 Phase 6B
═══════════════════════════════════════════════════════════════════════

Differential Screen Capture — chỉ capture và xử lý vùng thay đổi.

3 tối ưu chính:

  1. Focused Window Capture
     Thay vì full screen 3440×1440 → chỉ capture Chrome window 1440×900.
     Giảm 60% pixels → DINO nhanh hơn ~2×.

  2. Frame Skipping (pHash)
     So sánh pHash frame hiện tại vs frame trước.
     Hamming distance < threshold → skip (màn hình không đổi).
     Tăng throughput 20–30% khi UI tĩnh.

  3. ROI Focus
     Nếu Planner chỉ định vùng quan tâm ("top toolbar") →
     chỉ capture và detect vùng đó.
     Giảm thêm 50–80% pixels cho DINO.

Integration:
  perception_pipeline.perceive_dino() dùng DiffCapture thay ScreenCapture
  khi vision_backend = "dino_primary".

Process: orchestrator (L1)

Security invariants:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
         PIL.ImageGrab is the sole capture backend (read-only).
"""
from __future__ import annotations

import hashlib
import io
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from vision.screen_capture import CaptureRegion, ScreenCapture


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Perceptual Hash (pHash) — Pure PIL, no external deps
# ══════════════════════════════════════════════════════════════════

def _compute_phash(image_bytes: bytes, hash_size: int = 8) -> Optional[int]:
    """
    Compute perceptual hash of an image.

    Resize to (hash_size×hash_size) grayscale → DCT-like comparison.
    Returns integer hash (64 bits for hash_size=8).
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.resize((hash_size, hash_size), Image.LANCZOS)
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for i, px in enumerate(pixels):
            if px > avg:
                bits |= (1 << i)
        return bits
    except Exception:
        return None


def _hamming_distance(a: int, b: int) -> int:
    """Count differing bits between two hashes."""
    return bin(a ^ b).count('1')


# ══════════════════════════════════════════════════════════════════
# Frame Diff Analysis
# ══════════════════════════════════════════════════════════════════

@dataclass
class FrameInfo:
    """Metadata about a captured frame."""
    image_bytes: bytes              # PNG or JPEG bytes
    phash: Optional[int] = None     # perceptual hash
    capture_region: Optional[CaptureRegion] = None
    timestamp: float = field(default_factory=time.time)
    width: int = 0
    height: int = 0
    byte_hash: str = ""             # SHA-256[:16] for exact comparison

    @property
    def pixel_count(self) -> int:
        return self.width * self.height


@dataclass
class DiffResult:
    """Result of comparing two frames."""
    changed: bool = True            # True if meaningful change detected
    hamming: int = 64               # pHash hamming distance (0–64)
    same_hash: bool = False         # exact byte match
    change_ratio: float = 1.0       # estimated % of pixels changed (0–1)
    skip_detection: bool = False    # True → safe to skip DINO this frame

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "hamming": self.hamming,
            "same_hash": self.same_hash,
            "change_ratio": round(self.change_ratio, 4),
            "skip": self.skip_detection,
        }


# ══════════════════════════════════════════════════════════════════
# ROI (Region of Interest) Presets
# ══════════════════════════════════════════════════════════════════

def roi_from_hint(
    hint: str,
    window_bounds: tuple[int, int, int, int],
) -> Optional[CaptureRegion]:
    """
    Convert a text hint to a capture region.

    Hints:
      "top toolbar"   → top 15% of window
      "bottom bar"    → bottom 15% of window
      "center"        → center 60% of window
      "left sidebar"  → left 25% of window
      "right panel"   → right 30% of window
      "full"          → entire window (no optimization)

    Args:
        hint:           Text hint from Planner/Workflow.
        window_bounds:  (wx, wy, ww, wh) of focused window.

    Returns:
        CaptureRegion or None if hint is "full" or unrecognized.
    """
    wx, wy, ww, wh = window_bounds
    if ww <= 0 or wh <= 0:
        return None

    hint_lower = hint.lower().strip()

    if "top" in hint_lower or "toolbar" in hint_lower or "header" in hint_lower:
        return CaptureRegion(x=wx, y=wy, width=ww, height=int(wh * 0.15))

    elif "bottom" in hint_lower or "footer" in hint_lower or "bar" in hint_lower:
        h = int(wh * 0.15)
        return CaptureRegion(x=wx, y=wy + wh - h, width=ww, height=h)

    elif "center" in hint_lower or "middle" in hint_lower:
        margin_x = int(ww * 0.2)
        margin_y = int(wh * 0.2)
        return CaptureRegion(
            x=wx + margin_x, y=wy + margin_y,
            width=ww - 2 * margin_x, height=wh - 2 * margin_y,
        )

    elif "left" in hint_lower or "sidebar" in hint_lower:
        return CaptureRegion(x=wx, y=wy, width=int(ww * 0.25), height=wh)

    elif "right" in hint_lower or "panel" in hint_lower:
        rw = int(ww * 0.30)
        return CaptureRegion(x=wx + ww - rw, y=wy, width=rw, height=wh)

    return None  # "full" or unrecognized → capture entire window


# ══════════════════════════════════════════════════════════════════
# DiffCapture
# ══════════════════════════════════════════════════════════════════

class DiffCapture:
    """
    Differential Screen Capture with frame skip and ROI focus.

    Usage:
        dc = DiffCapture()

        # Capture with diff analysis
        frame, diff = dc.capture(
            window_bounds=(0, 80, 1440, 900),
            roi_hint="top toolbar",
        )

        if diff.skip_detection:
            # Screen unchanged → reuse previous detection result
            pass
        else:
            # Screen changed → run DINO on frame.image_bytes
            result = await dino.detect(frame.image_bytes, queries)
    """

    # pHash hamming distance threshold for "unchanged"
    SKIP_THRESHOLD = 3        # hamming ≤ 3 → unchanged (0-64 scale)
    MINOR_THRESHOLD = 8       # hamming ≤ 8 → minor change (cursor blink etc.)
    MAX_HISTORY = 5           # keep last N frames for comparison

    def __init__(self) -> None:
        self._screen = ScreenCapture()
        self._history: list[FrameInfo] = []
        self._total_captures: int = 0
        self._total_skips: int = 0
        self._total_focused: int = 0

    def capture(
        self,
        window_bounds: Optional[tuple[int, int, int, int]] = None,
        roi_hint: Optional[str] = None,
        force: bool = False,
    ) -> tuple[FrameInfo, DiffResult]:
        """
        Capture screen with differential analysis.

        Args:
            window_bounds: (wx, wy, ww, wh) — capture only this window.
            roi_hint:      Text hint for focused region ("top toolbar", etc.).
            force:         Skip frame diff, always capture fresh.

        Returns:
            (FrameInfo, DiffResult) — frame data + diff vs previous frame.
        """
        self._total_captures += 1

        # Determine capture region
        region = None
        if roi_hint and window_bounds:
            region = roi_from_hint(roi_hint, window_bounds)
        if region is None and window_bounds:
            wx, wy, ww, wh = window_bounds
            if ww > 0 and wh > 0:
                region = CaptureRegion(x=wx, y=wy, width=ww, height=wh)
                self._total_focused += 1

        # Capture
        try:
            if region:
                image_bytes = self._screen.capture_bytes(region, fmt="PNG")
                img_obj = self._screen.capture(region)
            else:
                image_bytes = self._screen.capture_bytes(fmt="PNG")
                img_obj = self._screen.capture()

            w, h = img_obj.size
        except Exception:
            # Fallback: full screen
            image_bytes = self._screen.capture_bytes(fmt="PNG")
            img_obj = self._screen.capture()
            w, h = img_obj.size
            region = None

        # Compute hashes
        byte_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
        phash = _compute_phash(image_bytes)

        frame = FrameInfo(
            image_bytes=image_bytes,
            phash=phash,
            capture_region=region,
            width=w,
            height=h,
            byte_hash=byte_hash,
        )

        # Compare with previous frame
        diff = self._compare(frame, force)

        # Update history
        self._history.append(frame)
        if len(self._history) > self.MAX_HISTORY:
            self._history = self._history[-self.MAX_HISTORY:]

        if diff.skip_detection:
            self._total_skips += 1

        return frame, diff

    def _compare(self, current: FrameInfo, force: bool) -> DiffResult:
        """Compare current frame with previous."""
        if force or not self._history:
            return DiffResult(changed=True, skip_detection=False)

        prev = self._history[-1]

        # Exact byte match → definitely unchanged
        if current.byte_hash == prev.byte_hash:
            return DiffResult(
                changed=False, hamming=0, same_hash=True,
                change_ratio=0.0, skip_detection=True,
            )

        # pHash comparison
        if current.phash is not None and prev.phash is not None:
            hamming = _hamming_distance(current.phash, prev.phash)
            change_ratio = hamming / 64.0

            if hamming <= self.SKIP_THRESHOLD:
                return DiffResult(
                    changed=False, hamming=hamming,
                    change_ratio=change_ratio, skip_detection=True,
                )
            elif hamming <= self.MINOR_THRESHOLD:
                # Minor change (cursor blink, animation) — still skip
                return DiffResult(
                    changed=True, hamming=hamming,
                    change_ratio=change_ratio, skip_detection=True,
                )
            else:
                return DiffResult(
                    changed=True, hamming=hamming,
                    change_ratio=change_ratio, skip_detection=False,
                )

        # No hash available → assume changed
        return DiffResult(changed=True, skip_detection=False)

    def get_last_frame(self) -> Optional[FrameInfo]:
        """Get the most recent captured frame."""
        return self._history[-1] if self._history else None

    def clear_history(self) -> None:
        """Clear frame history (force fresh capture next time)."""
        self._history.clear()

    def stats(self) -> dict[str, Any]:
        skip_rate = self._total_skips / self._total_captures if self._total_captures > 0 else 0
        focused_rate = self._total_focused / self._total_captures if self._total_captures > 0 else 0
        return {
            "total_captures": self._total_captures,
            "total_skips": self._total_skips,
            "skip_rate": round(skip_rate, 3),
            "total_focused": self._total_focused,
            "focused_rate": round(focused_rate, 3),
            "history_size": len(self._history),
            "fps_boost_estimate": f"{skip_rate * 100:.0f}% frames skipped",
        }


# Module singleton
_instance: Optional[DiffCapture] = None

def get_diff_capture() -> DiffCapture:
    global _instance
    if _instance is None:
        _instance = DiffCapture()
    return _instance
