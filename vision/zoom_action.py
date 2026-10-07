# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/zoom_action.py — Phidipus v1.30
═══════════════════════════════════════════════════════════════════════

Local Zoom Action Engine — Không cần API trả phí.

Vấn đề cần giải quyết:
  qwen3-vl nhìn ảnh 1920×1080 → nút "Đăng" 60×30px chỉ chiếm 0.09% diện tích
  → VLM đoán mò, confidence thấp, tọa độ lệch 20-50px.

Giải pháp — 3-pass adaptive zoom (mimic Claude Computer Use zoom action):

  Pass 1 — COARSE (toàn màn hình, full res thu nhỏ):
    Gửi VLM ảnh 960×540 → tìm vùng chứa target (rx, ry, rw, rh)
    → nhanh, tìm đúng "khu vực" (± 200px)

  Pass 2 — FINE (crop + upscale 3×):
    Crop 400×400px quanh tọa độ Pass 1 → upscale lên 1200×1200 bằng LANCZOS
    → VLM nhìn element to gấp 9 lần → confidence tăng, coord chính xác ±10px

  Pass 3 — ULTRA (crop + upscale 6×, chỉ khi confidence < 0.85):
    Crop 200×200px → upscale 1200×1200 → element chiếm 30% ảnh
    → click chính xác pixel-perfect

Lợi ích so với CropLocator cũ:
  ┌─────────────────────────────────────────────────────────────┐
  │ CropLocator (cũ):  crop vùng → gửi VLM → parse coord      │
  │ ZoomAction (mới):  full → coarse → upscale → fine → map   │
  │                    + confidence gate tự động quyết pass    │
  │                    + annotation overlay (debug view)        │
  │                    + multi-element disambiguation          │
  └─────────────────────────────────────────────────────────────┘

Tích hợp:
  # Trong find_and_click_v2 — thêm Strategy 0 (trước MultiModal):
  zoom_result = await zoom.locate(query, screenshot)
  if zoom_result.confidence > 0.9:
      x, y = zoom_result.x, zoom_result.y  # click ngay

  # Standalone:
  engine = ZoomActionEngine(vlm_fn=actor._vlm.call, screenshot_fn=actor.screenshot)
  result = await engine.locate("Post button", confidence_target=0.92)

Usage:
  engine = ZoomActionEngine(vlm_fn=vlm.call, screenshot_fn=capture_fn)
  result = await engine.locate(
      query="Đăng/Post blue submit button",
      confidence_target=0.90,
      # screen_w/screen_h: omit → auto-detect via _detect_screen_size()
      # hoặc truyền Chrome window size: screen_w=bounds["w"], screen_h=bounds["h"]
  )
  if result.ok:
      await click(result.x, result.y)
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Screen size detection — cached, HiDPI/Retina aware
# ══════════════════════════════════════════════════════════════════

_SCREEN_SIZE_CACHE: tuple[int, int] | None = None  # (w, h) — detected once, reused


def _detect_screen_size() -> tuple[int, int]:
    """
    Detect primary screen size using PIL.ImageGrab (R-01 safe, no pyautogui).

    Result is cached module-level after the first successful call — screen
    resolution doesn't change during a workflow run.

    Falls back to 1440×900 (common MacBook 13") instead of the old 1920×1080
    default, which was wrong for every MacBook ever shipped.

    Why not trust a hardcoded default?
      - MacBook 13" Retina  : 2560×1600 logical or 1440×900 (scaled)
      - MacBook 14"/16" M-series: 3024×1964 / 3456×2234 physical
      - External 2K monitor : 2560×1440
      - External 4K monitor : 3840×2160
      Any hardcoded value will be wrong on at least 3 of these 5 scenarios.

    Returns:
        (width, height) in logical pixels of the primary screen.
    """
    global _SCREEN_SIZE_CACHE
    if _SCREEN_SIZE_CACHE is not None:
        return _SCREEN_SIZE_CACHE

    # Method 1: PIL.ImageGrab — grabs a 1-pixel image to read dimensions
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        _SCREEN_SIZE_CACHE = img.size  # (w, h)
        _vlog("🖥️", f"Screen size detected: {_SCREEN_SIZE_CACHE[0]}×{_SCREEN_SIZE_CACHE[1]}")
        return _SCREEN_SIZE_CACHE
    except Exception:
        pass

    # Method 2: AppKit (macOS only) — logical point resolution
    try:
        import subprocess
        out = subprocess.check_output(
            ["python3", "-c",
             "import AppKit; s=AppKit.NSScreen.mainScreen().frame().size; "
             "print(int(s.width), int(s.height))"],
            timeout=3, text=True,
        ).strip().split()
        if len(out) == 2:
            _SCREEN_SIZE_CACHE = (int(out[0]), int(out[1]))
            _vlog("🖥️", f"Screen size (AppKit): {_SCREEN_SIZE_CACHE[0]}×{_SCREEN_SIZE_CACHE[1]}")
            return _SCREEN_SIZE_CACHE
    except Exception:
        pass

    # Method 3: Xrandr (Linux)
    try:
        import subprocess, re as _re
        out = subprocess.check_output(["xrandr", "--current"], timeout=3, text=True)
        m = _re.search(r"current (\d+) x (\d+)", out)
        if m:
            _SCREEN_SIZE_CACHE = (int(m.group(1)), int(m.group(2)))
            _vlog("🖥️", f"Screen size (xrandr): {_SCREEN_SIZE_CACHE[0]}×{_SCREEN_SIZE_CACHE[1]}")
            return _SCREEN_SIZE_CACHE
    except Exception:
        pass

    # Safe fallback — 1440×900 is more common on MacBooks than 1920×1080
    _vlog("⚠️", "Screen size detection failed — using 1440×900 fallback")
    _SCREEN_SIZE_CACHE = (1440, 900)
    return _SCREEN_SIZE_CACHE


def _invalidate_screen_size_cache() -> None:
    """Call after display configuration changes (e.g. external monitor plugged in)."""
    global _SCREEN_SIZE_CACHE
    _SCREEN_SIZE_CACHE = None


# ══════════════════════════════════════════════════════════════════
# Enums & Data classes
# ══════════════════════════════════════════════════════════════════

class ZoomPass(Enum):
    COARSE = auto()   # Full screen → 960×540, tìm vùng chứa target
    FINE   = auto()   # Crop 400×400 → upscale 1200×1200, tìm chính xác
    ULTRA  = auto()   # Crop 200×200 → upscale 1200×1200, pixel-perfect


@dataclass
class ZoomBBox:
    """Bounding box trong tọa độ màn hình thực (absolute pixels)."""
    x: int       # top-left x
    y: int       # top-left y
    w: int       # width
    h: int       # height

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def expand(self, margin: int, screen_w: int, screen_h: int) -> "ZoomBBox":
        """Mở rộng bbox ra ngoài margin px, clamp trong screen."""
        nx = max(0, self.x - margin)
        ny = max(0, self.y - margin)
        nx2 = min(screen_w, self.x + self.w + margin)
        ny2 = min(screen_h, self.y + self.h + margin)
        return ZoomBBox(nx, ny, nx2 - nx, ny2 - ny)


@dataclass
class ZoomPassResult:
    """Kết quả 1 pass zoom."""
    pass_type:   ZoomPass
    found:       bool
    cx:          int   = 0       # absolute screen coords
    cy:          int   = 0
    bbox:        ZoomBBox | None = None   # element bounding box (nếu có)
    confidence:  float = 0.0
    latency_ms:  int   = 0
    detail:      str   = ""
    zoom_factor: float = 1.0     # bao nhiêu lần đã upscale
    crop_box:    tuple = ()      # (x, y, w, h) vùng đã crop trên màn hình


@dataclass
class ZoomResult:
    """Kết quả cuối cùng từ ZoomActionEngine."""
    ok:           bool
    x:            int   = 0
    y:            int   = 0
    confidence:   float = 0.0
    passes_used:  int   = 0      # số lần zoom đã chạy
    final_pass:   ZoomPass = ZoomPass.COARSE
    latency_ms:   int   = 0
    debug_crops:  list  = field(default_factory=list)  # bytes của từng crop (debug)

    def log(self) -> None:
        status = "✅" if self.ok else "❌"
        _vlog("🔍", f"{status} ZoomAction → ({self.x},{self.y}) "
              f"conf={self.confidence:.0%} passes={self.passes_used} "
              f"final={self.final_pass.name} {self.latency_ms}ms")


# ══════════════════════════════════════════════════════════════════
# Image Processor (local, Pillow only — zero API cost)
# ══════════════════════════════════════════════════════════════════

class ImageProcessor:
    """
    Xử lý ảnh local bằng Pillow.
    - Resize ảnh về kích thước phù hợp trước khi gửi VLM (tiết kiệm token)
    - Upscale vùng crop để VLM nhìn rõ hơn (LANCZOS quality)
    - Vẽ debug annotation (highlight bounding box)
    """

    # Kích thước tối đa gửi VLM — qwen3-vl:8b tốt nhất với ảnh 1024px
    VLM_MAX_DIM = 1024

    @staticmethod
    def resize_for_vlm(img_bytes: bytes, max_dim: int = VLM_MAX_DIM) -> bytes:
        """
        Thu nhỏ ảnh về max_dim (giữ aspect ratio) trước khi gửi VLM.
        1920×1080 → 1024×576 → token giảm 64%, tốc độ tăng 2×.
        """
        try:
            from PIL import Image
            with Image.open(io.BytesIO(img_bytes)) as img:
                w, h = img.size
                if max(w, h) <= max_dim:
                    return img_bytes  # đã đủ nhỏ
                scale = max_dim / max(w, h)
                new_w = int(w * scale)
                new_h = int(h * scale)
                resized = img.resize((new_w, new_h), Image.LANCZOS)
                out = io.BytesIO()
                resized.save(out, format="JPEG", quality=85, optimize=True)
                return out.getvalue()
        except Exception as exc:
            _log.debug("resize_for_vlm failed: %s", exc)
            return img_bytes

    @staticmethod
    def crop_and_upscale(
        img_bytes: bytes,
        crop_x: int, crop_y: int, crop_w: int, crop_h: int,
        target_size: int = 1200,
        screen_w: int = 0, screen_h: int = 0,
    ) -> tuple[bytes, float]:
        """
        Crop vùng (crop_x, crop_y, crop_w, crop_h) rồi upscale lên target_size.

        Đây là core của ZoomAction — upscale LANCZOS làm pixels sắc nét hơn,
        VLM nhìn element to gấp N lần so với full-screen.

        screen_w=0 / screen_h=0 → auto-detect via _detect_screen_size().
        Không hardcode 1920×1080 — sai trên MacBook Retina và màn 2K/4K.

        Returns:
            (zoomed_bytes, zoom_factor) — zoom_factor = target_size / max(crop_w, crop_h)
        """
        try:
            from PIL import Image
            with Image.open(io.BytesIO(img_bytes)) as img:
                img_w, img_h = img.size

                # Resolve screen size — use actual detection if not explicitly provided
                _sw = screen_w if screen_w > 0 else _detect_screen_size()[0]
                _sh = screen_h if screen_h > 0 else _detect_screen_size()[1]

                # Scale compensation cho Retina/HiDPI:
                # img_w/img_h là physical pixels (có thể 2×), _sw/_sh là logical points
                sx = img_w / _sw
                sy = img_h / _sh

                # Tính box trong pixel ảnh thực tế
                box = (
                    max(0,     int(crop_x * sx)),
                    max(0,     int(crop_y * sy)),
                    min(img_w, int((crop_x + crop_w) * sx)),
                    min(img_h, int((crop_y + crop_h) * sy)),
                )

                if box[2] <= box[0] or box[3] <= box[1]:
                    return img_bytes, 1.0

                cropped = img.crop(box)
                cw, ch = cropped.size

                # Upscale về target_size (giữ aspect ratio)
                zoom_factor = target_size / max(cw, ch)
                if zoom_factor > 1.0:
                    new_w = int(cw * zoom_factor)
                    new_h = int(ch * zoom_factor)
                    zoomed = cropped.resize((new_w, new_h), Image.LANCZOS)
                else:
                    zoomed = cropped  # đã đủ lớn
                    zoom_factor = 1.0

                out = io.BytesIO()
                zoomed.save(out, format="JPEG", quality=92, optimize=True)
                return out.getvalue(), zoom_factor

        except Exception as exc:
            _log.debug("crop_and_upscale failed: %s", exc)
            return img_bytes, 1.0

    @staticmethod
    def draw_debug_overlay(
        img_bytes: bytes,
        cx: int, cy: int,
        radius: int = 15,
        color: tuple = (255, 50, 50),
    ) -> bytes:
        """
        Vẽ crosshair đỏ tại (cx, cy) để debug — không ảnh hưởng gì đến logic.
        """
        try:
            from PIL import Image, ImageDraw
            with Image.open(io.BytesIO(img_bytes)) as img:
                draw = ImageDraw.Draw(img)
                # Crosshair
                draw.line([(cx - radius, cy), (cx + radius, cy)], fill=color, width=3)
                draw.line([(cx, cy - radius), (cx, cy + radius)], fill=color, width=3)
                # Circle
                draw.ellipse(
                    [(cx - radius, cy - radius), (cx + radius, cy + radius)],
                    outline=color, width=2,
                )
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=85)
                return out.getvalue()
        except Exception:
            return img_bytes


# ══════════════════════════════════════════════════════════════════
# VLM Prompt Templates
# ══════════════════════════════════════════════════════════════════

class ZoomPrompts:
    """
    Prompt tối ưu cho từng pass.
    Key insight: prompt khác nhau cho từng zoom level → accuracy cao hơn.
    """

    @staticmethod
    def coarse(query: str, screen_w: int, screen_h: int) -> str:
        """
        Pass 1: Tìm VÙNG chứa element (không cần chính xác).
        Trả về relative coords + bbox ước lượng.
        """
        return (
            f"Find: {query}\n"
            f"Screen size: {screen_w}×{screen_h}px\n"
            "Task: Locate the UI element. Give approximate position.\n"
            "Respond ONLY with JSON (no markdown, no explanation):\n"
            '  Found: {"found":true,"rx":<0-1>,"ry":<0-1>,'
            '"rw":<width ratio 0-1>,"rh":<height ratio 0-1>,'
            '"confidence":<0-1>,"note":"<brief description>"}\n'
            '  Not found: {"found":false}'
        )

    @staticmethod
    def fine(query: str, crop_w: int, crop_h: int, zoom_factor: float) -> str:
        """
        Pass 2: Tìm CHÍNH XÁC trong ảnh đã zoom.
        Ảnh này là crop phóng to zoom_factor lần — element trông to hơn nhiều.
        """
        return (
            f"Find: {query}\n"
            f"This image is a ZOOMED crop ({crop_w}×{crop_h}px, "
            f"zoomed {zoom_factor:.1f}× from original screen).\n"
            "Task: Find the EXACT center of the target element.\n"
            "Respond ONLY with JSON:\n"
            '  Found: {"found":true,"cx":<pixel x in THIS image>,'
            '"cy":<pixel y in THIS image>,'
            '"confidence":<0-1>,"element_size":"<tiny|small|medium|large>"}\n'
            '  Not found: {"found":false}'
        )

    @staticmethod
    def ultra(query: str, crop_w: int, crop_h: int) -> str:
        """
        Pass 3: Ultra precision — element nên chiếm >20% ảnh này.
        Hỏi cả sub-pixel offset để tối ưu.
        """
        return (
            f"Find: {query}\n"
            f"ULTRA ZOOM image ({crop_w}×{crop_h}px). "
            "The target element should be very prominent in this image.\n"
            "Task: Click-point with maximum precision.\n"
            "Respond ONLY with JSON:\n"
            '{"found":true,"cx":<x>,"cy":<y>,'
            '"confidence":<0-1>,'
            '"click_hint":"<center|top-half|bottom-half|left|right>"}\n'
            '  or {"found":false,"reason":"<why>"}'
        )

    @staticmethod
    def disambiguate(query: str, count: int) -> str:
        """Khi có nhiều element trùng tên — chọn đúng cái."""
        return (
            f"Find: {query}\n"
            f"There appear to be {count} similar elements. "
            "Pick the MOST RELEVANT one (primary action button, not disabled).\n"
            "Respond ONLY with JSON:\n"
            '{"found":true,"cx":<x>,"cy":<y>,"confidence":<0-1>,'
            '"chosen_reason":"<why this one>"}'
        )


# ══════════════════════════════════════════════════════════════════
# ZoomActionEngine — Main Class
# ══════════════════════════════════════════════════════════════════

class ZoomActionEngine:
    """
    3-pass adaptive zoom locator. Zero API cost — dùng VLM local.

    Thuật toán:

      [Pass 1 — COARSE]
        FullScreen → resize 960×540 → VLM → (rx,ry,rw,rh)
        Nếu confidence >= coarse_threshold → tính bbox
        → sang Pass 2

      [Pass 2 — FINE]
        Crop bbox + margin (400×400) → upscale 3× → 1200×1200
        → VLM với pixel coords → map về screen coords
        Nếu confidence >= fine_threshold → DONE
        Nếu không → sang Pass 3

      [Pass 3 — ULTRA]
        Crop tight 200×200 quanh Pass 2 result → upscale 6×
        → VLM → final coords
        Luôn trả về kết quả tốt nhất sau Pass 3

    Args:
        vlm_fn:          async (img_bytes, prompt) → (raw_str, tier_str)
        screenshot_fn:   async () → bytes  (full screenshot)
        coarse_threshold: confidence tối thiểu để trust Pass 1 (default 0.5)
        fine_threshold:   confidence để dừng sớm ở Pass 2 (default 0.88)
        enable_ultra:     có chạy Pass 3 không (default True)
        save_debug_crops: lưu crops để debug (default False)
    """

    # PERF-01 FIX: Reduced per-pass timeout from 25s → 12s for COARSE/FINE/ULTRA.
    # Worst-case with 3 passes + S4 fallback was 25×3 + 25 = 100s per click.
    # New worst-case: 12×3 + 25 (S4 final) = 61s — still high but ~40% faster.
    # S4 final fallback retains 25s as it is the last resort with no further retry.
    VLM_TIMEOUT_PASS: float = 12.0    # COARSE / FINE / ULTRA individual passes
    VLM_TIMEOUT_FINAL: float = 25.0   # S4 final VLM fallback (last resort)

    def __init__(
        self,
        vlm_fn: Optional[Callable[..., Awaitable]] = None,
        screenshot_fn: Optional[Callable[..., Awaitable]] = None,
        coarse_threshold: float = 0.50,
        fine_threshold:   float = 0.88,
        enable_ultra:     bool  = True,
        save_debug_crops: bool  = False,
        screen_w: int = 0,
        screen_h: int = 0,
    ) -> None:
        self._vlm              = vlm_fn
        self._screen           = screenshot_fn
        self._coarse_thresh    = coarse_threshold
        self._fine_thresh      = fine_threshold
        self._ultra            = enable_ultra
        self._debug            = save_debug_crops
        self._proc             = ImageProcessor()

        # FIX: auto-detect real screen resolution instead of trusting caller's
        # hardcoded 1920×1080 default. screen_w=0 / screen_h=0 means "detect".
        # On MacBook Retina / 2K / 4K the old default was wrong in every scenario.
        if screen_w > 0 and screen_h > 0:
            self._screen_w = screen_w
            self._screen_h = screen_h
        else:
            self._screen_w, self._screen_h = _detect_screen_size()
        _vlog("🖥️", f"ZoomActionEngine init: screen={self._screen_w}×{self._screen_h}")

    # ── Public API ────────────────────────────────────────────────

    async def locate(
        self,
        query: str,
        screenshot: bytes | None = None,
        screen_w: int | None = None,
        screen_h: int | None = None,
        confidence_target: float | None = None,
    ) -> ZoomResult:
        """
        Tìm element với adaptive zoom.

        Args:
            query:             Mô tả element
            screenshot:        Full screenshot bytes (nếu None → tự chụp)
            screen_w/h:        Override kích thước màn hình
            confidence_target: Override fine_threshold cho lần này

        Returns:
            ZoomResult với tọa độ absolute screen coords
        """
        if not self._vlm:
            return ZoomResult(ok=False, confidence=0.0)

        sw = screen_w or self._screen_w
        sh = screen_h or self._screen_h
        fine_thresh = confidence_target or self._fine_thresh
        t0 = time.time()
        debug_crops = []

        # PERF-01 FIX: shared budget — tổng thời gian tối đa cho toàn bộ locate()
        # Mỗi pass lấy budget proportionally, không để mỗi pass timeout 12s độc lập.
        TOTAL_BUDGET_MS: float = 24_000.0   # 24s cho cả 3 passes (8s mỗi pass)

        def _remaining_ms() -> float:
            return max(0.0, TOTAL_BUDGET_MS - (time.time() - t0) * 1000)

        def _pass_timeout(fraction: float = 0.4) -> float:
            """Trả về timeout (giây) theo phần budget còn lại."""
            return max(2.0, min(self.VLM_TIMEOUT_PASS, _remaining_ms() * fraction / 1000))

        # Chụp screenshot nếu chưa có
        if screenshot is None:
            if self._screen:
                try:
                    screenshot = await self._screen()
                except Exception as exc:
                    _log.debug("screenshot failed: %s", exc)
                    return ZoomResult(ok=False)
            else:
                return ZoomResult(ok=False)

        # ── Pass 1: COARSE ────────────────────────────────────────
        _vlog("🔭", f"ZoomPass 1/COARSE: '{query[:40]}'")
        # PERF-01 FIX: sử dụng budget-aware timeout thay vì VLM_TIMEOUT_PASS cứng
        coarse = await self._pass_coarse(query, screenshot, sw, sh,
                                         timeout=_pass_timeout(fraction=0.4))

        if not coarse.found:
            _vlog("❌", f"COARSE not found: '{query[:40]}'")
            return ZoomResult(
                ok=False, passes_used=1, latency_ms=int((time.time()-t0)*1000)
            )

        if self._debug:
            debug_crops.append(("coarse", screenshot))

        _vlog("🔭", f"COARSE → ({coarse.cx},{coarse.cy}) conf={coarse.confidence:.0%} "
              f"zoom={coarse.zoom_factor:.1f}× [{coarse.latency_ms}ms]")

        # ── PERF-01 FIX: COARSE high-confidence early exit ────────
        # Nếu element lớn/rõ (button, dialog, input) → COARSE đã đủ chính xác.
        # Skip FINE+ULTRA → tiết kiệm 2–6s mỗi click.
        COARSE_SUFFICIENT_CONF = 0.95  # element rõ, to, không cần refine
        if coarse.found and coarse.confidence >= COARSE_SUFFICIENT_CONF:
            total_ms = int((time.time() - t0) * 1000)
            _vlog("⚡", f"COARSE sufficient ({coarse.confidence:.0%} ≥ {COARSE_SUFFICIENT_CONF:.0%}) "
                  f"→ skip FINE+ULTRA [{total_ms}ms]")
            result = ZoomResult(
                ok=True, x=coarse.cx, y=coarse.cy,
                confidence=coarse.confidence,
                passes_used=1, final_pass=ZoomPass.COARSE,
                latency_ms=total_ms, debug_crops=debug_crops,
            )
            result.log()
            return result

        # ── Pass 2: FINE ──────────────────────────────────────────
        # Crop 400×400 quanh tọa độ coarse
        crop_radius = 200   # → 400×400 crop
        fine_crop_bytes, fine_zoom = self._proc.crop_and_upscale(
            screenshot,
            coarse.cx - crop_radius, coarse.cy - crop_radius,
            crop_radius * 2, crop_radius * 2,
            target_size=1200,
            screen_w=sw, screen_h=sh,
        )
        fine_crop_box = (
            max(0, coarse.cx - crop_radius),
            max(0, coarse.cy - crop_radius),
            crop_radius * 2, crop_radius * 2,
        )

        _vlog("🔬", f"ZoomPass 2/FINE: upscale {fine_zoom:.1f}× "
              f"crop=({fine_crop_box[0]},{fine_crop_box[1]},"
              f"{fine_crop_box[2]}×{fine_crop_box[3]})")

        fine = await self._pass_fine(
            query, fine_crop_bytes, fine_crop_box, fine_zoom, sw, sh,
            timeout=_pass_timeout(fraction=0.5),   # PERF-01 FIX: budget-aware
        )

        if self._debug and fine_crop_bytes:
            debug_crops.append(("fine", fine_crop_bytes))

        if fine.found:
            _vlog("🔬", f"FINE → ({fine.cx},{fine.cy}) conf={fine.confidence:.0%} "
                  f"[{fine.latency_ms}ms]")

            # Đủ confidence → dừng sớm
            if fine.confidence >= fine_thresh:
                total_ms = int((time.time()-t0)*1000)
                result = ZoomResult(
                    ok=True, x=fine.cx, y=fine.cy,
                    confidence=fine.confidence,
                    passes_used=2, final_pass=ZoomPass.FINE,
                    latency_ms=total_ms, debug_crops=debug_crops,
                )
                result.log()
                return result

        # ── Pass 3: ULTRA (chỉ khi enable + Pass 2 không đủ tự tin) ──
        if self._ultra:
            # Dùng tọa độ tốt nhất hiện có để crop ultra-tight
            best_cx = fine.cx if fine.found else coarse.cx
            best_cy = fine.cy if fine.found else coarse.cy

            ultra_radius = 100   # → 200×200 crop
            ultra_crop_bytes, ultra_zoom = self._proc.crop_and_upscale(
                screenshot,
                best_cx - ultra_radius, best_cy - ultra_radius,
                ultra_radius * 2, ultra_radius * 2,
                target_size=1200,
                screen_w=sw, screen_h=sh,
            )
            ultra_crop_box = (
                max(0, best_cx - ultra_radius),
                max(0, best_cy - ultra_radius),
                ultra_radius * 2, ultra_radius * 2,
            )

            _vlog("⚡", f"ZoomPass 3/ULTRA: upscale {ultra_zoom:.1f}× "
                  f"crop=(200×200 around ({best_cx},{best_cy}))")

            ultra = await self._pass_ultra(
                query, ultra_crop_bytes, ultra_crop_box, ultra_zoom, sw, sh,
                timeout=_pass_timeout(fraction=1.0),   # PERF-01 FIX: budget remainder
            )

            if self._debug:
                debug_crops.append(("ultra", ultra_crop_bytes))

            if ultra.found:
                _vlog("⚡", f"ULTRA → ({ultra.cx},{ultra.cy}) "
                      f"conf={ultra.confidence:.0%} [{ultra.latency_ms}ms]")
                total_ms = int((time.time()-t0)*1000)
                result = ZoomResult(
                    ok=True, x=ultra.cx, y=ultra.cy,
                    confidence=ultra.confidence,
                    passes_used=3, final_pass=ZoomPass.ULTRA,
                    latency_ms=total_ms, debug_crops=debug_crops,
                )
                result.log()
                return result

        # Fallback: dùng kết quả tốt nhất có được
        best = fine if fine.found else coarse
        total_ms = int((time.time()-t0)*1000)
        result = ZoomResult(
            ok=best.found,
            x=best.cx, y=best.cy,
            confidence=best.confidence * 0.9,  # penalty vì không reach target conf
            passes_used=3 if self._ultra else 2,
            final_pass=ZoomPass.FINE if fine.found else ZoomPass.COARSE,
            latency_ms=total_ms, debug_crops=debug_crops,
        )
        result.log()
        return result

    # ── Pass implementations ──────────────────────────────────────

    async def _pass_coarse(
        self, query: str, img_bytes: bytes, sw: int, sh: int,
        timeout: float | None = None,
    ) -> ZoomPassResult:
        """
        Pass 1: Gửi full screenshot đã resize → tìm approximate location.
        Resize xuống 960×540 trước → tiết kiệm 75% token, nhanh 2×.
        """
        t0 = time.time()
        _timeout = timeout if timeout is not None else self.VLM_TIMEOUT_PASS
        try:
            # Resize cho VLM (không upscale — đây là full overview)
            resized = self._proc.resize_for_vlm(img_bytes, max_dim=960)
            prompt = ZoomPrompts.coarse(query, sw, sh)

            raw, tier = await asyncio.wait_for(
                self._vlm(resized, prompt), timeout=_timeout
            )
            ms = int((time.time()-t0)*1000)

            d = self._parse_json(raw)
            if not d.get("found", False):
                return ZoomPassResult(
                    pass_type=ZoomPass.COARSE, found=False,
                    latency_ms=ms, detail="not_found",
                )

            rx = float(d.get("rx", 0.5))
            ry = float(d.get("ry", 0.5))
            rw = float(d.get("rw", 0.05))
            rh = float(d.get("rh", 0.03))
            conf = float(d.get("confidence", 0.5))

            abs_cx = int(rx * sw)
            abs_cy = int(ry * sh)
            bbox = ZoomBBox(
                x=int((rx - rw/2) * sw),
                y=int((ry - rh/2) * sh),
                w=int(rw * sw),
                h=int(rh * sh),
            )

            _vlog("🟡", f"[{tier}] COARSE found '{query[:30]}' "
                  f"→ ({abs_cx},{abs_cy}) bbox={bbox.w}×{bbox.h} "
                  f"conf={conf:.0%} {ms}ms")

            return ZoomPassResult(
                pass_type=ZoomPass.COARSE, found=True,
                cx=abs_cx, cy=abs_cy, bbox=bbox,
                confidence=conf, latency_ms=ms,
                detail=d.get("note", ""),
                zoom_factor=1.0,
            )

        except asyncio.TimeoutError:
            ms = int((time.time()-t0)*1000)
            return ZoomPassResult(pass_type=ZoomPass.COARSE, found=False,
                                  latency_ms=ms, detail="timeout")
        except Exception as exc:
            ms = int((time.time()-t0)*1000)
            _log.debug("COARSE pass error: %s", exc)
            return ZoomPassResult(pass_type=ZoomPass.COARSE, found=False,
                                  latency_ms=ms, detail=str(exc)[:60])

    async def _pass_fine(
        self,
        query: str,
        zoomed_bytes: bytes,
        crop_box: tuple,       # (cx_orig, cy_orig, w, h) trong screen coords
        zoom_factor: float,
        sw: int, sh: int,
        timeout: float | None = None,   # PERF-01 FIX: budget-aware timeout
    ) -> ZoomPassResult:
        """
        Pass 2: VLM nhìn ảnh zoomed → trả pixel coords trong ảnh đó
        → map về screen coords.
        """
        t0 = time.time()
        _timeout = timeout if timeout is not None else self.VLM_TIMEOUT_PASS
        try:
            # Lấy kích thước ảnh đã zoom
            crop_img_w, crop_img_h = self._get_image_size(zoomed_bytes)

            prompt = ZoomPrompts.fine(query, crop_img_w, crop_img_h, zoom_factor)

            raw, tier = await asyncio.wait_for(
                self._vlm(zoomed_bytes, prompt), timeout=_timeout
            )
            ms = int((time.time()-t0)*1000)

            d = self._parse_json(raw)
            if not d.get("found", False):
                return ZoomPassResult(
                    pass_type=ZoomPass.FINE, found=False,
                    latency_ms=ms, detail="not_found",
                )

            # Pixel coords trong ảnh zoom
            cx_zoomed = float(d.get("cx", crop_img_w / 2))
            cy_zoomed = float(d.get("cy", crop_img_h / 2))
            conf      = float(d.get("confidence", 0.7))

            # Map từ zoomed pixel → original crop pixel
            cx_crop = cx_zoomed / zoom_factor
            cy_crop = cy_zoomed / zoom_factor

            # Map từ crop pixel → absolute screen coords
            crop_x, crop_y = crop_box[0], crop_box[1]
            abs_cx = int(crop_x + cx_crop)
            abs_cy = int(crop_y + cy_crop)

            # Clamp trong screen
            abs_cx = max(0, min(sw, abs_cx))
            abs_cy = max(0, min(sh, abs_cy))

            _vlog("🟢", f"[{tier}] FINE found '{query[:30]}' "
                  f"→ ({abs_cx},{abs_cy}) zoom={zoom_factor:.1f}× "
                  f"conf={conf:.0%} {ms}ms")

            return ZoomPassResult(
                pass_type=ZoomPass.FINE, found=True,
                cx=abs_cx, cy=abs_cy,
                confidence=conf, latency_ms=ms,
                zoom_factor=zoom_factor,
                crop_box=crop_box,
                detail=d.get("element_size", ""),
            )

        except asyncio.TimeoutError:
            ms = int((time.time()-t0)*1000)
            return ZoomPassResult(pass_type=ZoomPass.FINE, found=False,
                                  latency_ms=ms, detail="timeout")
        except Exception as exc:
            ms = int((time.time()-t0)*1000)
            _log.debug("FINE pass error: %s", exc)
            return ZoomPassResult(pass_type=ZoomPass.FINE, found=False,
                                  latency_ms=ms, detail=str(exc)[:60])

    async def _pass_ultra(
        self,
        query: str,
        zoomed_bytes: bytes,
        crop_box: tuple,
        zoom_factor: float,
        sw: int, sh: int,
        timeout: float | None = None,   # PERF-01 FIX: budget-aware timeout
    ) -> ZoomPassResult:
        """
        Pass 3: Ultra precision. Element nên rất lớn trong ảnh này.
        """
        t0 = time.time()
        _timeout = timeout if timeout is not None else self.VLM_TIMEOUT_PASS
        try:
            crop_img_w, crop_img_h = self._get_image_size(zoomed_bytes)
            prompt = ZoomPrompts.ultra(query, crop_img_w, crop_img_h)

            raw, tier = await asyncio.wait_for(
                self._vlm(zoomed_bytes, prompt), timeout=_timeout
            )
            ms = int((time.time()-t0)*1000)

            d = self._parse_json(raw)
            if not d.get("found", False):
                return ZoomPassResult(
                    pass_type=ZoomPass.ULTRA, found=False,
                    latency_ms=ms, detail=d.get("reason", "not_found"),
                )

            cx_zoomed = float(d.get("cx", crop_img_w / 2))
            cy_zoomed = float(d.get("cy", crop_img_h / 2))
            conf      = float(d.get("confidence", 0.9))

            # Apply click_hint offset (tinh chỉnh sub-element accuracy)
            hint = d.get("click_hint", "center")
            offset_x, offset_y = self._hint_offset(hint, crop_img_w, crop_img_h, zoom_factor)

            cx_crop = (cx_zoomed + offset_x) / zoom_factor
            cy_crop = (cy_zoomed + offset_y) / zoom_factor

            crop_x, crop_y = crop_box[0], crop_box[1]
            abs_cx = max(0, min(sw, int(crop_x + cx_crop)))
            abs_cy = max(0, min(sh, int(crop_y + cy_crop)))

            _vlog("⚡", f"[{tier}] ULTRA found '{query[:30]}' "
                  f"→ ({abs_cx},{abs_cy}) hint={hint} "
                  f"zoom={zoom_factor:.1f}× conf={conf:.0%} {ms}ms")

            return ZoomPassResult(
                pass_type=ZoomPass.ULTRA, found=True,
                cx=abs_cx, cy=abs_cy,
                confidence=conf, latency_ms=ms,
                zoom_factor=zoom_factor,
                crop_box=crop_box,
            )

        except asyncio.TimeoutError:
            ms = int((time.time()-t0)*1000)
            return ZoomPassResult(pass_type=ZoomPass.ULTRA, found=False,
                                  latency_ms=ms, detail="timeout")
        except Exception as exc:
            ms = int((time.time()-t0)*1000)
            _log.debug("ULTRA pass error: %s", exc)
            return ZoomPassResult(pass_type=ZoomPass.ULTRA, found=False,
                                  latency_ms=ms, detail=str(exc)[:60])

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Parse JSON từ VLM response, robust với markdown và noise."""
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            m = re.search(r"\{[^{}]+\}", cleaned, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        return {"found": False}

    @staticmethod
    def _get_image_size(img_bytes: bytes) -> tuple[int, int]:
        """Lấy kích thước ảnh mà không load toàn bộ."""
        try:
            from PIL import Image
            with Image.open(io.BytesIO(img_bytes)) as img:
                return img.size  # (w, h)
        except Exception:
            return (1200, 1200)  # safe default

    @staticmethod
    def _hint_offset(hint: str, img_w: int, img_h: int, zoom: float) -> tuple[float, float]:
        """
        Tính pixel offset từ click_hint.
        VLM đôi khi trả cx/cy ở center nhưng cần click top-half (ví dụ: tab).
        """
        unit = 15.0 / zoom  # offset nhỏ tỉ lệ với zoom
        mapping = {
            "center":      (0.0, 0.0),
            "top-half":    (0.0, -unit),
            "bottom-half": (0.0, +unit),
            "left":        (-unit, 0.0),
            "right":       (+unit, 0.0),
        }
        return mapping.get(hint, (0.0, 0.0))


# ══════════════════════════════════════════════════════════════════
# Integration helper — dùng trong find_and_click_v2
# ══════════════════════════════════════════════════════════════════

class ZoomActionMixin:
    """
    Mixin để thêm zoom_locate() vào VisionActor.

    Thêm vào VisionActor.__init__():
        self._zoom = ZoomActionEngine(
            vlm_fn=self._vlm.call,
            screenshot_fn=self.screenshot,
            # screen_w=0, screen_h=0 → auto-detect (PIL → AppKit → fallback 1440×900)
            # KHÔNG truyền 1920×1080 hardcoded — sai trên MacBook Retina và 2K/4K
        )

    Dùng trong find_and_click_v2() trước Strategy 2 (MultiModal):
        # Strategy 1.5: ZoomAction (high precision, local)
        if (x == 0 and y == 0) and self._zoom:
            zoom_r = await self._zoom.locate(
                query, screenshot=img,
                # Truyền Chrome window size (không phải full screen):
                screen_w=bounds.get('w') or None,
                screen_h=bounds.get('h') or None,
            )
            if zoom_r.ok and zoom_r.confidence >= 0.85:
                x, y, conf, method = zoom_r.x, zoom_r.y, zoom_r.confidence, \\
                    f"zoom_{zoom_r.final_pass.name.lower()}"
    """
    pass


# ══════════════════════════════════════════════════════════════════
# Predefined configs cho từng use case
# ══════════════════════════════════════════════════════════════════

class ZoomPresets:
    """
    Preset cấu hình tối ưu cho từng loại element.

    screen_w / screen_h default = 0 → ZoomActionEngine tự detect qua
    _detect_screen_size() (PIL.ImageGrab → AppKit → xrandr → 1440×900).
    Không bao giờ truyền 1920×1080 hardcoded — sai trên mọi MacBook.
    """

    @staticmethod
    def small_button(vlm_fn, screenshot_fn, screen_w=0, screen_h=0):
        """
        Nút nhỏ (<60px): Facebook Đăng, Instagram Next, X Post.
        Ultra pass bật — cần accuracy cao nhất.
        screen_w=0 / screen_h=0 → auto-detect actual screen resolution.
        """
        return ZoomActionEngine(
            vlm_fn=vlm_fn, screenshot_fn=screenshot_fn,
            coarse_threshold=0.40,
            fine_threshold=0.88,
            enable_ultra=True,
            screen_w=screen_w, screen_h=screen_h,
        )

    @staticmethod
    def large_element(vlm_fn, screenshot_fn, screen_w=0, screen_h=0):
        """
        Element lớn (>200px): text area, dialog box, image.
        Dừng sớm ở Pass 2 — không cần ultra.
        screen_w=0 / screen_h=0 → auto-detect actual screen resolution.
        """
        return ZoomActionEngine(
            vlm_fn=vlm_fn, screenshot_fn=screenshot_fn,
            coarse_threshold=0.60,
            fine_threshold=0.75,
            enable_ultra=False,
            screen_w=screen_w, screen_h=screen_h,
        )

    @staticmethod
    def icon_button(vlm_fn, screenshot_fn, screen_w=0, screen_h=0):
        """
        Icon không có text (<40px): ChatGPT download arrow, IG like button.
        Ultra bật, crop radius nhỏ hơn (icon nằm gần anchor).
        screen_w=0 / screen_h=0 → auto-detect actual screen resolution.
        """
        engine = ZoomActionEngine(
            vlm_fn=vlm_fn, screenshot_fn=screenshot_fn,
            coarse_threshold=0.35,
            fine_threshold=0.90,
            enable_ultra=True,
            screen_w=screen_w, screen_h=screen_h,
        )
        return engine
