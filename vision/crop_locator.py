# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/crop_locator.py — Phidipus v1.28
═══════════════════════════════════════════════════════════════════════

#4 Relative Coord via Crop (thay vì relative text)
  Thay vì nói với VLM "click bên phải text X 30px" (unreliable),
  ta crop ảnh quanh anchor element rồi hỏi "click đâu trong ảnh này?"
  → chính xác hơn vì VLM làm việc trên ảnh nhỏ, ít noise hơn.

  Pipeline:
    1. Find anchor element (bằng VLM hoặc JS — ví dụ: "Post text area")
    2. Crop screenshot quanh anchor (± crop_radius pixels)
    3. Ask VLM về ảnh cropped: "Find [target] in this image"
    4. Map tọa độ cropped → absolute screen coordinates

  Ưu điểm so với full-screen VLM:
    - Ảnh nhỏ hơn → nhanh hơn 3×, ít token hơn 5×
    - VLM chỉ nhìn vào vùng liên quan → ít hallucination
    - Robust với scroll (anchor + target giữ nguyên relative position)
    - Giỏi tìm small buttons gần large elements (ví dụ: nút Save cạnh ảnh)

  Ví dụ thực tế:
    # Tìm nút Save bên cạnh ảnh vừa generate trên ChatGPT
    result = await locator.locate_near(
        anchor_query="Generated AI image",
        target_query="Save or Download button near the image",
        screenshot=img_bytes,
        anchor_coords=(x, y),   # đã biết từ lần trước
    )
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class CropResult:
    """Kết quả từ CropLocator."""
    found:        bool
    x:            int   = 0        # absolute screen coordinates
    y:            int   = 0
    confidence:   float = 0.0
    crop_box:     tuple = ()       # (crop_x, crop_y, crop_w, crop_h) — debug
    anchor_box:   tuple = ()       # anchor coords used
    method:       str   = ""
    latency_ms:   int   = 0

    def log(self) -> None:
        status = "✅" if self.found else "❌"
        _vlog("✂️", f"{status} CropLocate → ({self.x},{self.y}) "
              f"conf={self.confidence:.0%} [{self.method}] {self.latency_ms}ms")


# ══════════════════════════════════════════════════════════════════
# CropLocator
# ══════════════════════════════════════════════════════════════════

class CropLocator:
    """
    Crop-based element locator (#4).

    Args:
        vlm_fn:         async callable(image_bytes, prompt) → (raw_str, tier_str)
        screenshot_fn:  async callable() → bytes (full screenshot)
        crop_radius:    pixels to expand around anchor (default 200)
        min_crop_size:  minimum crop dimension (default 300)
        max_crop_size:  maximum crop dimension (default 800)
    """

    def __init__(
        self,
        vlm_fn: Optional[Callable[..., Awaitable]] = None,
        screenshot_fn: Optional[Callable[..., Awaitable]] = None,
        crop_radius: int = 200,
        min_crop_size: int = 300,
        max_crop_size: int = 800,
    ) -> None:
        self._vlm        = vlm_fn
        self._screen     = screenshot_fn
        self._radius     = crop_radius
        self._min_size   = min_crop_size
        self._max_size   = max_crop_size

    # ── Public API ────────────────────────────────────────────────

    async def locate_near(
        self,
        target_query: str,
        anchor_x: int,
        anchor_y: int,
        screenshot: bytes | None = None,
        anchor_label: str = "",
        screen_w: int | None = None,
        screen_h: int | None = None,
    ) -> CropResult:
        """
        Tìm target_query trong vùng xung quanh anchor coords.

        Args:
            target_query:  Mô tả element cần tìm (ví dụ: "Save button")
            anchor_x/y:    Tọa độ absolute của anchor element
            screenshot:    Bytes của full screenshot (nếu None sẽ chụp mới)
            anchor_label:  Label của anchor để log
            screen_w/h:    Kích thước màn hình để clamp crop.
                           None → auto-detect via _detect_screen_size().
        """
        t0 = time.time()

        # Resolve screen size if not provided
        if screen_w is None or screen_h is None:
            try:
                from vision.zoom_action import _detect_screen_size
                _dw, _dh = _detect_screen_size()
                if screen_w is None:
                    screen_w = _dw
                if screen_h is None:
                    screen_h = _dh
            except Exception:
                screen_w = screen_w or 1440
                screen_h = screen_h or 900

        if not self._vlm:
            return CropResult(found=False, method="no_vlm_fn")

        # Chụp screenshot nếu chưa có
        if screenshot is None and self._screen:
            screenshot = await self._screen()
        if not screenshot:
            return CropResult(found=False, method="no_screenshot")

        # Tính crop box xung quanh anchor
        crop_x = max(0, anchor_x - self._radius)
        crop_y = max(0, anchor_y - self._radius)
        crop_x2 = min(screen_w, anchor_x + self._radius)
        crop_y2 = min(screen_h, anchor_y + self._radius)
        crop_w = max(self._min_size, crop_x2 - crop_x)
        crop_h = max(self._min_size, crop_y2 - crop_y)
        # Clamp max size
        if crop_w > self._max_size:
            crop_w = self._max_size
            crop_x = max(0, anchor_x - crop_w // 2)
        if crop_h > self._max_size:
            crop_h = self._max_size
            crop_y = max(0, anchor_y - crop_h // 2)

        # Crop ảnh
        try:
            cropped = self._crop_image(screenshot, crop_x, crop_y, crop_w, crop_h)
        except Exception as exc:
            _log.debug("CropLocator: crop failed: %s", exc)
            return CropResult(found=False, method="crop_failed")

        if not cropped:
            return CropResult(found=False, method="empty_crop")

        # Ask VLM về ảnh đã crop
        anchor_str = f" near {anchor_label}" if anchor_label else ""
        prompt = (
            f"Find: {target_query}{anchor_str}\n"
            f"This is a CROPPED image ({crop_w}×{crop_h}px).\n"
            "Respond ONLY with JSON (no markdown):\n"
            "  If found: {\"found\": true, \"cx\": <pixel x in this crop>, "
            "\"cy\": <pixel y in this crop>, \"confidence\": <0.0-1.0>}\n"
            "  If not found: {\"found\": false}"
        )

        try:
            raw, tier = await asyncio.wait_for(
                self._vlm(cropped, prompt),
                timeout=20.0
            )
        except asyncio.TimeoutError:
            ms = int((time.time() - t0) * 1000)
            return CropResult(found=False, method="vlm_timeout", latency_ms=ms)
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            return CropResult(found=False, method=f"vlm_error", latency_ms=ms)

        # Parse VLM response
        result = self._parse_vlm_response(raw)
        ms = int((time.time() - t0) * 1000)

        if not result.get("found"):
            r = CropResult(
                found=False, method="vlm_not_found",
                crop_box=(crop_x, crop_y, crop_w, crop_h),
                latency_ms=ms,
            )
            r.log()
            return r

        # Map crop-relative → absolute screen coords
        cx_crop = int(result.get("cx", crop_w // 2))
        cy_crop = int(result.get("cy", crop_h // 2))
        abs_x   = crop_x + cx_crop
        abs_y   = crop_y + cy_crop
        conf    = float(result.get("confidence", 0.7))

        r = CropResult(
            found=True,
            x=abs_x, y=abs_y,
            confidence=conf,
            crop_box=(crop_x, crop_y, crop_w, crop_h),
            anchor_box=(anchor_x, anchor_y),
            method=f"crop_{tier}",
            latency_ms=ms,
        )
        _vlog("✂️", f"Crop [{tier}]: '{target_query[:40]}' found at ({abs_x},{abs_y}) "
              f"[crop_local=({cx_crop},{cy_crop})] conf={conf:.0%} {ms}ms")
        return r

    async def locate_near_text(
        self,
        target_query: str,
        anchor_text_js: str,
        js_exec_fn: Callable,
        window_bounds: dict,
        screenshot: bytes | None = None,
    ) -> CropResult:
        """
        Convenience: find anchor by JS text → crop → find target.

        Args:
            anchor_text_js: JS để lấy tọa độ anchor
                           ví dụ: "document.querySelector('h1').getBoundingClientRect()"
        """
        win_x = window_bounds.get("x", 0)
        win_y = window_bounds.get("y", 0)
        win_w = window_bounds.get("w", 1440)
        win_h = window_bounds.get("h", 900)

        try:
            js = (
                f"(function(){{"
                f"  var r = ({anchor_text_js});"
                f"  if (!r) return null;"
                f"  return JSON.stringify({{"
                f"    cx: Math.round(r.left + r.width/2),"
                f"    cy: Math.round(r.top + r.height/2)"
                f"  }});"
                f"}})()"
            )
            coro = js_exec_fn(js)
            raw = await coro if asyncio.iscoroutine(coro) else coro

            if raw and raw != "null":
                import json as _json
                d = _json.loads(raw.strip())
                anchor_x = d.get("cx", 0) + win_x
                anchor_y = d.get("cy", 0) + win_y
            else:
                return CropResult(found=False, method="anchor_js_not_found")
        except Exception as exc:
            return CropResult(found=False, method=f"anchor_js_error: {exc!s:.40}")

        return await self.locate_near(
            target_query=target_query,
            anchor_x=anchor_x, anchor_y=anchor_y,
            screenshot=screenshot,
            screen_w=win_x + win_w,
            screen_h=win_y + win_h,
        )

    # ── Internal ──────────────────────────────────────────────────

    def _crop_image(
        self,
        img_bytes: bytes,
        x: int, y: int,
        w: int, h: int,
    ) -> bytes:
        """
        Crop image bytes.
        Dùng Pillow nếu có, fallback về raw byte parsing.
        """
        try:
            from PIL import Image
            with Image.open(io.BytesIO(img_bytes)) as img:
                # Handle Retina (2× scale) bằng cách check actual dimensions
                img_w, img_h = img.size
                scale_x = img_w / max(img_w, 1440)
                scale_y = img_h / max(img_h, 900)
                # Crop với scale compensation
                box = (
                    int(x * scale_x),
                    int(y * scale_y),
                    int((x + w) * scale_x),
                    int((y + h) * scale_y),
                )
                box = (
                    max(0, box[0]),
                    max(0, box[1]),
                    min(img_w, box[2]),
                    min(img_h, box[3]),
                )
                cropped = img.crop(box)
                out = io.BytesIO()
                cropped.save(out, format="PNG", optimize=True)
                return out.getvalue()
        except ImportError:
            # Pillow không có → trả về full ảnh (fallback graceful)
            _log.debug("CropLocator: Pillow not available, returning full image")
            return img_bytes
        except Exception as exc:
            _log.debug("CropLocator: crop error: %s", exc)
            return img_bytes

    def _parse_vlm_response(self, raw: str) -> dict:
        """Parse JSON response từ VLM."""
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            m = re.search(r'\{[^}]+\}', cleaned)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        return {"found": False}


# ══════════════════════════════════════════════════════════════════
# Predefined crop strategies cho các site phổ biến
# ══════════════════════════════════════════════════════════════════

class CropStrategies:
    """
    Chiến lược crop tối ưu cho từng site.
    Anchor = element dễ tìm và ổn định.
    Target = element nhỏ/khó tìm ở gần anchor.
    """

    FACEBOOK_POST = {
        "anchor_js": "document.querySelector('[role=\"dialog\"]') && "
                     "document.querySelector('[role=\"dialog\"]').getBoundingClientRect()",
        "target": "Post/Submit button in the bottom-right of the dialog",
        "crop_radius": 250,
    }

    FACEBOOK_PHOTO_UPLOAD = {
        "anchor_js": "document.querySelector('[data-testid=\"photo-selector\"]') && "
                     "document.querySelector('[data-testid=\"photo-selector\"]').getBoundingClientRect()",
        "target": "Photo/Video upload button or Add Photos icon",
        "crop_radius": 200,
    }

    CHATGPT_SAVE_IMAGE = {
        "anchor_js": "document.querySelector('[data-message-author-role=\"assistant\"] img') && "
                     "document.querySelector('[data-message-author-role=\"assistant\"] img').getBoundingClientRect()",
        "target": "Download or Save image button",
        "crop_radius": 150,
    }

    INSTAGRAM_NEXT = {
        "anchor_js": "document.querySelector('[role=\"dialog\"]') && "
                     "document.querySelector('[role=\"dialog\"]').getBoundingClientRect()",
        "target": "Next button in top-right of dialog",
        "crop_radius": 300,
    }
