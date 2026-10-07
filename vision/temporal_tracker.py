# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/temporal_tracker.py — Phidipus v2.3 Phase 6A
═══════════════════════════════════════════════════════════════════════

Temporal Continuity — Object Tracking + Coordinate Lock.

Vấn đề:
  Giữa detect (DINO 100ms) và click (IPC 10ms), user có thể di chuột
  hoặc cửa sổ dịch chuyển → tọa độ bị lệch 10–50px.

Giải pháp — 2 lớp bảo vệ:

  Layer 1: Template Matching (~5ms)
    Lưu crop template của target khi detect.
    Trước khi click → chụp screenshot mới → match template.
    Nếu match confidence > 0.85 và offset < 30px → dùng tọa độ mới.
    Nếu offset > 50px → UI thay đổi nhiều → re-detect.

  Layer 2: Coordinate Lock (instant)
    Nếu template match fail → kiểm tra xem window bounds có đổi không.
    Nếu window dịch chuyển (wx', wy' ≠ wx, wy) → offset correction.
    Dùng relative coords (rx, ry) × new window bounds.

Integration:
  perception_pipeline.py → perceive_dino() trả kết quả
  → temporal_tracker.lock(target_crop, coords, window_bounds)
  → trước click → temporal_tracker.verify_and_correct(new_screenshot)
  → corrected_coords hoặc re-detect signal

Process: orchestrator (L1)

Security invariants:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
"""
from __future__ import annotations

import hashlib
import io
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from vision.detection_types import BBox


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Template Matching — Pure PIL (no OpenCV dependency)
# ══════════════════════════════════════════════════════════════════

def _crop_template(image_bytes: bytes, bbox: BBox, padding: int = 5) -> Optional[bytes]:
    """Crop a template region from image."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        iw, ih = img.size
        x1 = max(0, bbox.x1 - padding)
        y1 = max(0, bbox.y1 - padding)
        x2 = min(iw, bbox.x2 + padding)
        y2 = min(ih, bbox.y2 + padding)
        if x2 - x1 < 5 or y2 - y1 < 5:
            return None
        crop = img.crop((x1, y1, x2, y2))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _template_match(
    screenshot_bytes: bytes,
    template_bytes: bytes,
    search_region: Optional[BBox] = None,
    step: int = 2,
) -> Optional[tuple[int, int, float]]:
    """
    Simple template matching using normalized cross-correlation.

    Pure PIL implementation — no OpenCV needed.
    Uses grayscale thumbnails for speed (~5ms on M4).

    Args:
        screenshot_bytes: Full screenshot PNG.
        template_bytes:   Template crop PNG.
        search_region:    Optional region to search within (reduces computation).
        step:             Pixel step for sliding window (2 = check every 2px).

    Returns:
        (best_x, best_y, confidence) or None if match too weak.
        best_x, best_y = top-left corner of best match in screenshot coords.
    """
    try:
        from PIL import Image
        import array
    except ImportError:
        return None

    try:
        screenshot = Image.open(io.BytesIO(screenshot_bytes)).convert("L")  # grayscale
        template = Image.open(io.BytesIO(template_bytes)).convert("L")

        # Resize for speed (max 400px wide)
        sw, sh = screenshot.size
        tw, th = template.size
        scale = 1.0
        if sw > 400:
            scale = sw / 400
            screenshot = screenshot.resize((400, int(sh / scale)), Image.BILINEAR)
            template = template.resize((int(tw / scale), int(th / scale)), Image.BILINEAR)
            sw, sh = screenshot.size
            tw, th = template.size

        if tw < 3 or th < 3 or tw > sw or th > sh:
            return None

        # Convert to flat arrays
        s_data = list(screenshot.getdata())
        t_data = list(template.getdata())

        # Template mean and std
        t_mean = sum(t_data) / len(t_data)
        t_std = math.sqrt(sum((v - t_mean) ** 2 for v in t_data) / len(t_data))
        if t_std < 1.0:
            return None  # flat template = useless

        # Search bounds
        x_start, y_start = 0, 0
        x_end, y_end = sw - tw, sh - th
        if search_region:
            x_start = max(0, int(search_region.x1 / scale) - tw)
            y_start = max(0, int(search_region.y1 / scale) - th)
            x_end = min(x_end, int(search_region.x2 / scale))
            y_end = min(y_end, int(search_region.y2 / scale))

        best_score = -1.0
        best_x, best_y = 0, 0

        for y in range(y_start, y_end + 1, step):
            for x in range(x_start, x_end + 1, step):
                # Extract window
                ncc = 0.0
                w_sum = 0.0
                w_sq_sum = 0.0
                cross = 0.0
                n = 0

                for ty in range(th):
                    row_offset = (y + ty) * sw + x
                    t_row_offset = ty * tw
                    for tx in range(tw):
                        sv = s_data[row_offset + tx]
                        tv = t_data[t_row_offset + tx]
                        w_sum += sv
                        w_sq_sum += sv * sv
                        cross += sv * (tv - t_mean)
                        n += 1

                if n == 0:
                    continue

                w_mean = w_sum / n
                w_std = math.sqrt(max(0, w_sq_sum / n - w_mean * w_mean))

                if w_std < 1.0:
                    continue

                ncc = cross / (n * w_std * t_std)

                if ncc > best_score:
                    best_score = ncc
                    best_x = x
                    best_y = y

        if best_score < 0.5:
            return None

        # Scale back to original coordinates
        real_x = int(best_x * scale)
        real_y = int(best_y * scale)

        return (real_x, real_y, best_score)

    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# Data Types
# ══════════════════════════════════════════════════════════════════

@dataclass
class TrackedTarget:
    """A target element being tracked across frames."""
    bbox: BBox                          # original bounding box
    template_bytes: Optional[bytes]     # cropped template image
    window_bounds: tuple[int, int, int, int]  # (wx, wy, ww, wh) at detection time
    relative_x: float                   # rx = (cx - wx) / ww
    relative_y: float                   # ry = (cy - wy) / wh
    created_at: float = field(default_factory=time.time)
    last_verified: float = 0.0

    @property
    def age_ms(self) -> float:
        return (time.time() - self.created_at) * 1000

    @property
    def stale(self) -> bool:
        """Target older than 2 seconds is stale — should re-detect."""
        return self.age_ms > 2000


@dataclass
class CorrectionResult:
    """Result of verify_and_correct."""
    corrected_x: int
    corrected_y: int
    offset_x: int = 0                  # delta from original
    offset_y: int = 0
    confidence: float = 0.0
    method: str = "none"               # "template" | "window_offset" | "unchanged" | "stale"
    needs_redetect: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.corrected_x,
            "y": self.corrected_y,
            "offset": (self.offset_x, self.offset_y),
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "needs_redetect": self.needs_redetect,
        }


# ══════════════════════════════════════════════════════════════════
# TemporalTracker
# ══════════════════════════════════════════════════════════════════

class TemporalTracker:
    """
    Object tracking between detection and click.

    Usage:
        tracker = TemporalTracker()

        # After DINO detection, lock the target
        tracker.lock(
            image_bytes=screenshot,
            bbox=best_candidate.bbox,
            window_bounds=(0, 80, 1440, 900),
        )

        # Before IPC click, verify position
        result = tracker.verify_and_correct(
            new_screenshot=new_screenshot_bytes,
            new_window_bounds=(10, 80, 1440, 900),  # window moved 10px right
        )
        if result.needs_redetect:
            # UI changed too much — re-run DINO
        else:
            click_at(result.corrected_x, result.corrected_y)
    """

    TEMPLATE_MATCH_THRESHOLD = 0.75
    SMALL_OFFSET_MAX = 30        # px — template match correction
    LARGE_OFFSET_MAX = 50        # px — beyond this → re-detect
    WINDOW_MOVE_TOLERANCE = 5    # px — ignore jitter < 5px

    def __init__(self) -> None:
        self._target: Optional[TrackedTarget] = None
        self._total_locks: int = 0
        self._total_corrections: int = 0
        self._total_redetects: int = 0
        self._method_counts: dict[str, int] = {
            "template": 0, "window_offset": 0, "unchanged": 0, "stale": 0,
        }

    def lock(
        self,
        image_bytes: bytes,
        bbox: BBox,
        window_bounds: tuple[int, int, int, int],
    ) -> None:
        """
        Lock a target for tracking.

        Called after DINO detection, before IPC click.
        Stores template crop + relative coords for later verification.
        """
        self._total_locks += 1

        template = _crop_template(image_bytes, bbox, padding=8)
        wx, wy, ww, wh = window_bounds
        cx, cy = bbox.center

        rx = (cx - wx) / ww if ww > 0 else 0.5
        ry = (cy - wy) / wh if wh > 0 else 0.5

        self._target = TrackedTarget(
            bbox=bbox,
            template_bytes=template,
            window_bounds=window_bounds,
            relative_x=max(0.0, min(1.0, rx)),
            relative_y=max(0.0, min(1.0, ry)),
        )

    def verify_and_correct(
        self,
        new_screenshot: bytes,
        new_window_bounds: Optional[tuple[int, int, int, int]] = None,
    ) -> CorrectionResult:
        """
        Verify target position and correct if needed.

        Called just before IPC click dispatch.

        Returns:
            CorrectionResult with corrected coordinates.
        """
        if self._target is None:
            return CorrectionResult(
                corrected_x=0, corrected_y=0,
                method="no_target", needs_redetect=True,
            )

        target = self._target
        orig_cx, orig_cy = target.bbox.center
        self._total_corrections += 1

        # Check staleness
        if target.stale:
            self._method_counts["stale"] += 1
            return CorrectionResult(
                corrected_x=orig_cx, corrected_y=orig_cy,
                method="stale", needs_redetect=True,
            )

        # ── Layer 1: Template Matching ────────────────────────
        if target.template_bytes and new_screenshot:
            # Search in region around original position (±100px)
            search = BBox(
                x1=max(0, target.bbox.x1 - 100),
                y1=max(0, target.bbox.y1 - 100),
                x2=target.bbox.x2 + 100,
                y2=target.bbox.y2 + 100,
            )
            match = _template_match(
                new_screenshot, target.template_bytes,
                search_region=search, step=2,
            )

            if match is not None:
                mx, my, conf = match
                # Calculate center of matched template
                tw = target.bbox.width
                th = target.bbox.height
                new_cx = mx + tw // 2
                new_cy = my + th // 2
                offset_x = new_cx - orig_cx
                offset_y = new_cy - orig_cy
                offset_dist = math.sqrt(offset_x ** 2 + offset_y ** 2)

                if conf >= self.TEMPLATE_MATCH_THRESHOLD:
                    if offset_dist <= self.SMALL_OFFSET_MAX:
                        # Good match, small offset → use corrected coords
                        self._method_counts["template"] += 1
                        _vlog("🎯", f"TemporalTracker template match: "
                                     f"offset=({offset_x},{offset_y}) conf={conf:.2f}")
                        target.last_verified = time.time()
                        return CorrectionResult(
                            corrected_x=new_cx, corrected_y=new_cy,
                            offset_x=offset_x, offset_y=offset_y,
                            confidence=conf, method="template",
                        )
                    elif offset_dist <= self.LARGE_OFFSET_MAX:
                        # Match found but large offset — use but flag uncertainty
                        self._method_counts["template"] += 1
                        _vlog("⚠️", f"TemporalTracker large offset: "
                                     f"({offset_x},{offset_y}) dist={offset_dist:.0f}px")
                        return CorrectionResult(
                            corrected_x=new_cx, corrected_y=new_cy,
                            offset_x=offset_x, offset_y=offset_y,
                            confidence=conf * 0.8, method="template",
                        )
                    else:
                        # Offset too large → UI changed significantly
                        self._total_redetects += 1
                        self._method_counts["stale"] += 1
                        return CorrectionResult(
                            corrected_x=orig_cx, corrected_y=orig_cy,
                            method="stale", needs_redetect=True,
                        )

        # ── Layer 2: Window Offset Correction ─────────────────
        if new_window_bounds:
            owx, owy, oww, owh = target.window_bounds
            nwx, nwy, nww, nwh = new_window_bounds

            dx = nwx - owx
            dy = nwy - owy

            if abs(dx) > self.WINDOW_MOVE_TOLERANCE or abs(dy) > self.WINDOW_MOVE_TOLERANCE:
                # Window moved — apply relative coords to new bounds
                new_cx = int(nwx + target.relative_x * nww)
                new_cy = int(nwy + target.relative_y * nwh)
                self._method_counts["window_offset"] += 1
                _vlog("📐", f"TemporalTracker window offset: "
                             f"dx={dx}, dy={dy} → ({new_cx},{new_cy})")
                return CorrectionResult(
                    corrected_x=new_cx, corrected_y=new_cy,
                    offset_x=new_cx - orig_cx, offset_y=new_cy - orig_cy,
                    confidence=0.8, method="window_offset",
                )

        # ── No change detected ────────────────────────────────
        self._method_counts["unchanged"] += 1
        return CorrectionResult(
            corrected_x=orig_cx, corrected_y=orig_cy,
            confidence=0.9, method="unchanged",
        )

    def clear(self) -> None:
        """Clear tracked target."""
        self._target = None

    def stats(self) -> dict[str, Any]:
        return {
            "total_locks": self._total_locks,
            "total_corrections": self._total_corrections,
            "total_redetects": self._total_redetects,
            "methods": dict(self._method_counts),
            "has_target": self._target is not None,
            "target_age_ms": round(self._target.age_ms, 0) if self._target else 0,
        }


# Module singleton
_instance: Optional[TemporalTracker] = None

def get_temporal_tracker() -> TemporalTracker:
    global _instance
    if _instance is None:
        _instance = TemporalTracker()
    return _instance
