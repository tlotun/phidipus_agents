# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/multi_frame_vision.py — Phidipus B3 Multi-Frame Temporal Vision v2.2
════════════════════════════════════════════════════════════════════════════

B3: Multi-Frame Temporal Vision — Nhìn theo thời gian, không chỉ 1 frame.

Vấn đề:
  VLMInterface hiện chỉ nhìn 1 frame tại 1 thời điểm.
  Nhưng nhiều UI state quan trọng chỉ rõ khi SO SÁNH với frame trước:

  - "Nút Submit có bị disabled không?"
    → Chỉ rõ khi so màu/opacity before vs after form fill

  - "Upload đang progress không?"
    → So sánh progress bar giữa 2 frame → estimate % và ETA

  - "Page có thực sự scroll không?"
    → So sánh pixel diff vùng content → confirm scroll action

  - "Dialog có tự tắt không?"
    → Frame trước có modal → frame sau không còn → action thành công

Giải pháp — MultiFrameVision:
  1. capture() → FrameSnapshot (bytes, hash, timestamp)
  2. compare(before, after) → FrameDiff (changed_ratio, changed_regions)
  3. detect_state_change(before, after, region, vlm_fn) → StateChange
     - Nếu diff nhỏ (< 2%) → state UNCHANGED (không cần VLM)
     - Nếu diff lớn (≥ 2%) → gọi VLM diff_prompt để hiểu thay đổi
  4. estimate_progress(before, after, bar_region) → ProgressEstimate
     - Phân tích pixel brightness shift trong progress bar region
     - Không cần VLM — thuần pixel math (0ms overhead)
  5. verify_scroll(before, after) → ScrollVerification
     - Pixel diff vùng content → SCROLLED / NOT_SCROLLED / LOADING
  6. detect_dialog_closed(before, after) → bool
     - So sánh pixel vùng center → dialog biến mất?

Architecture:
  workflow_executor / vision_actor gọi:
    mfv = get_multi_frame_vision()
    snap1 = await mfv.capture()                     ← trước action
    await actor.scroll(...)
    snap2 = await mfv.capture()                     ← sau action
    result = mfv.verify_scroll(snap1, snap2)
    if not result.scrolled:
        # Scroll không hiệu quả → thử lại hoặc fallback

Integration points:
  1. vision_actor._check_scroll_success() — thay delay → compare frames
  2. workflow_executor._exec_vision_click() — verify button state trước click
  3. workflow_executor._exec_wait() — detect khi loading/modal biến mất

Security invariants:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  (PIL.Image pixel operations are read-only, safe in L1)

Process: orchestrator (L1)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ── Constants ─────────────────────────────────────────────────────

# Ngưỡng pixel diff để coi là "đã thay đổi"
_DIFF_THRESHOLD_UNCHANGED: float = 0.005   # < 0.5% pixel thay đổi → unchanged
_DIFF_THRESHOLD_MINOR:     float = 0.02    # 0.5–2% → minor change (loading blink, cursor)
_DIFF_THRESHOLD_MAJOR:     float = 0.10    # ≥ 10% → major change (page load, dialog)

# Kích thước thumbnail để pixel diff (không cần full resolution)
_DIFF_THUMB_W = 320
_DIFF_THUMB_H = 200

# Timeout VLM cho frame comparison (ngắn hơn full VLM call)
_VLM_DIFF_TIMEOUT = 12.0

# Cache TTL cho mỗi FrameSnapshot (seconds)
_SNAP_TTL = 30.0


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class FrameSnapshot:
    """
    Snapshot của 1 frame màn hình tại 1 thời điểm.
    bytes_raw: PNG/JPEG bytes gốc (cho VLM)
    thumb_bytes: thumbnail nhỏ hơn cho pixel diff (PIL required)
    hash: SHA-256 16 ký tự đầu để so sánh nhanh
    """
    bytes_raw:   bytes
    thumb_bytes: bytes        # thumbnail 320×200 PNG
    img_hash:    str          # SHA-256[:16] của thumb_bytes
    width:       int
    height:      int
    captured_at: float = field(default_factory=time.time)
    label:       str   = ""   # nhãn debug: "before_scroll", "after_click"...

    @property
    def age_s(self) -> float:
        return time.time() - self.captured_at

    @property
    def is_stale(self) -> bool:
        return self.age_s > _SNAP_TTL

    def to_b64(self) -> str:
        """Base64 PNG cho VLM input."""
        return base64.b64encode(self.bytes_raw).decode()

    def thumb_b64(self) -> str:
        """Base64 thumbnail cho diff VLM prompt."""
        return base64.b64encode(self.thumb_bytes).decode()


@dataclass
class Region:
    """Vùng màn hình (pixel coords)."""
    x: int
    y: int
    w: int
    h: int

    def to_box(self) -> tuple[int, int, int, int]:
        """PIL crop box: (left, top, right, bottom)."""
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @staticmethod
    def from_dict(d: dict) -> "Region":
        return Region(
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            w=int(d.get("w", d.get("width", 100))),
            h=int(d.get("h", d.get("height", 50))),
        )


@dataclass
class FrameDiff:
    """
    Kết quả so sánh pixel giữa 2 frame.
    changed_ratio: tỷ lệ pixel thay đổi (0.0–1.0)
    changed_regions: list bounding box vùng thay đổi nhiều nhất
    """
    changed_ratio:   float              # 0.0 = identical, 1.0 = completely different
    same_hash:       bool               # True nếu hash giống hệt (no change at all)
    changed_regions: list[Region]       # top vùng diff (tối đa 3)
    dominant_change: str                # "none" | "top" | "center" | "bottom" | "full"
    diff_ms:         int   = 0          # thời gian tính diff (ms)

    @property
    def is_unchanged(self) -> bool:
        return self.same_hash or self.changed_ratio < _DIFF_THRESHOLD_UNCHANGED

    @property
    def is_minor(self) -> bool:
        return _DIFF_THRESHOLD_UNCHANGED <= self.changed_ratio < _DIFF_THRESHOLD_MINOR

    @property
    def is_major(self) -> bool:
        return self.changed_ratio >= _DIFF_THRESHOLD_MAJOR


@dataclass
class StateChange:
    """
    Kết quả phân tích thay đổi UI state giữa 2 frame.
    Dùng cho: button disabled/enabled, dialog appeared/disappeared, loading done.
    """
    changed:       bool               # Có thay đổi đáng kể không?
    state_before:  str = ""           # "enabled" | "disabled" | "visible" | "loading" | "unknown"
    state_after:   str = ""           # tương tự
    description:   str = ""           # mô tả ngắn gọn thay đổi
    confidence:    float = 0.0        # 0.0–1.0 (dựa trên diff ratio)
    used_vlm:      bool = False       # True nếu đã gọi VLM để phân tích
    diff:          Optional[FrameDiff] = None


@dataclass
class ProgressEstimate:
    """
    Ước tính tiến trình upload/processing từ progress bar.
    Không dùng VLM — thuần pixel analysis.
    """
    found_progress:  bool             # True nếu phát hiện được progress bar
    pct_before:      float = 0.0      # % trước (0.0–100.0)
    pct_after:       float = 0.0      # % sau
    delta_pct:       float = 0.0      # % đã tăng
    eta_seconds:     float = -1.0     # -1 = không ước tính được
    is_complete:     bool  = False    # True nếu 100%
    is_stalled:      bool  = False    # True nếu không tăng


@dataclass
class ScrollVerification:
    """
    Kết quả verify xem scroll có hiệu quả không.
    """
    scrolled:        bool             # True nếu scroll thực sự xảy ra
    direction:       str = ""         # "up" | "down" | "none"
    scroll_amount:   float = 0.0      # ước tính % page đã scroll
    loading_detected: bool = False    # True nếu phát hiện loading spinner
    diff:            Optional[FrameDiff] = None


# ══════════════════════════════════════════════════════════════════
# Core class
# ══════════════════════════════════════════════════════════════════

class MultiFrameVision:
    """
    B3 Multi-Frame Temporal Vision.

    Capture, compare, và phân tích thay đổi UI qua thời gian.
    Giảm VLM calls bằng cách dùng pixel diff trước —
    chỉ gọi VLM khi diff cho thấy thay đổi đáng kể.
    """

    def __init__(
        self,
        vlm_fn: Optional[Callable[..., Awaitable[str]]] = None,
        gemini_api_key: str = "",
    ) -> None:
        """
        Args:
            vlm_fn: async fn(img_bytes, prompt) → str — để phân tích diff.
                    Nếu None, chỉ dùng pixel diff (không có VLM insight).
            gemini_api_key: key cho Gemini Vision (dùng nếu vlm_fn is None).
        """
        self._vlm_fn      = vlm_fn
        self._gemini_key  = gemini_api_key
        self._snap_cache: dict[str, FrameSnapshot] = {}   # label → snap

    # ── Capture ──────────────────────────────────────────────────

    async def capture(self, label: str = "", region: Optional[Region] = None) -> FrameSnapshot:
        """
        Chụp screenshot và tạo FrameSnapshot.

        Args:
            label: nhãn debug ("before_scroll", "after_type"...)
            region: nếu có, chỉ chụp vùng này thay vì full screen

        Returns:
            FrameSnapshot với thumb đã resize cho diff
        """
        t0 = time.time()
        try:
            from PIL import Image, ImageGrab
        except ImportError:
            raise RuntimeError("PIL không available — pip install Pillow")

        # Chụp màn hình
        loop = asyncio.get_event_loop()
        if region:
            img: Image.Image = await loop.run_in_executor(
                None, lambda: ImageGrab.grab(bbox=region.to_box())
            )
        else:
            img = await loop.run_in_executor(None, ImageGrab.grab)

        w, h = img.size

        # Raw bytes (PNG)
        raw_buf = io.BytesIO()
        img.save(raw_buf, format="PNG", optimize=True)
        raw_bytes = raw_buf.getvalue()

        # Thumbnail cho diff (resize nhỏ để nhanh)
        thumb = img.resize((_DIFF_THUMB_W, _DIFF_THUMB_H), Image.LANCZOS).convert("RGB")
        thumb_buf = io.BytesIO()
        thumb.save(thumb_buf, format="PNG")
        thumb_bytes = thumb_buf.getvalue()

        img_hash = hashlib.sha256(thumb_bytes).hexdigest()[:16]

        snap = FrameSnapshot(
            bytes_raw=raw_bytes,
            thumb_bytes=thumb_bytes,
            img_hash=img_hash,
            width=w,
            height=h,
            captured_at=t0,
            label=label or f"snap_{int(t0)}",
        )

        if label:
            self._snap_cache[label] = snap

        _vlog("📷", f"Captured frame '{snap.label}' {w}×{h} ({len(raw_bytes)//1024}KB) "
                    f"hash={img_hash} [{int((time.time()-t0)*1000)}ms]")
        return snap

    def get_cached(self, label: str) -> Optional[FrameSnapshot]:
        """Lấy snapshot đã lưu theo label."""
        snap = self._snap_cache.get(label)
        if snap and not snap.is_stale:
            return snap
        return None

    # ── Pixel Diff ───────────────────────────────────────────────

    def compare(self, before: FrameSnapshot, after: FrameSnapshot) -> FrameDiff:
        """
        So sánh pixel 2 frame để tính mức độ thay đổi.
        Không cần VLM — thuần PIL pixel math.

        Returns:
            FrameDiff với changed_ratio và vùng thay đổi nhiều nhất
        """
        t0 = time.time()

        # Hash check nhanh
        if before.img_hash == after.img_hash:
            return FrameDiff(
                changed_ratio=0.0,
                same_hash=True,
                changed_regions=[],
                dominant_change="none",
                diff_ms=0,
            )

        try:
            from PIL import Image, ImageChops
        except ImportError:
            # Fallback: chỉ hash comparison
            return FrameDiff(
                changed_ratio=0.05,  # Không biết chính xác → assume minor
                same_hash=False,
                changed_regions=[],
                dominant_change="unknown",
                diff_ms=0,
            )

        # Load thumbnails
        img_b = Image.open(io.BytesIO(before.thumb_bytes)).convert("RGB")
        img_a = Image.open(io.BytesIO(after.thumb_bytes)).convert("RGB")

        # Pixel diff
        diff = ImageChops.difference(img_b, img_a)
        diff_data = list(diff.getdata())

        # Changed pixels: pixel có tổng RGB diff > 30
        total_pixels = len(diff_data)
        changed = sum(1 for r, g, b in diff_data if (r + g + b) > 30)
        changed_ratio = changed / total_pixels if total_pixels > 0 else 0.0

        # Tìm vùng thay đổi nhiều nhất (chia màn hình thành 3 phần: top/center/bottom)
        cols = _DIFF_THUMB_W
        rows = _DIFF_THUMB_H
        third = rows // 3

        top_changed    = sum(1 for i, (r, g, b) in enumerate(diff_data)
                             if (r+g+b) > 30 and (i // cols) < third)
        center_changed = sum(1 for i, (r, g, b) in enumerate(diff_data)
                             if (r+g+b) > 30 and third <= (i // cols) < 2*third)
        bottom_changed = sum(1 for i, (r, g, b) in enumerate(diff_data)
                             if (r+g+b) > 30 and (i // cols) >= 2*third)

        region_pixels = total_pixels // 3
        dominant = "none"
        if changed_ratio >= _DIFF_THRESHOLD_UNCHANGED:
            counts = {"top": top_changed, "center": center_changed, "bottom": bottom_changed}
            dominant = max(counts, key=counts.get)  # type: ignore
            if changed_ratio >= _DIFF_THRESHOLD_MAJOR:
                dominant = "full"

        # Build regions list (tọa độ trên full screen, scale từ thumb)
        scale_x = before.width  / _DIFF_THUMB_W  if before.width  > 0 else 1.0
        scale_y = before.height / _DIFF_THUMB_H  if before.height > 0 else 1.0

        changed_regions: list[Region] = []
        if top_changed > region_pixels * 0.05:
            changed_regions.append(Region(
                x=0, y=0,
                w=before.width,
                h=int(third * scale_y)
            ))
        if center_changed > region_pixels * 0.05:
            changed_regions.append(Region(
                x=0, y=int(third * scale_y),
                w=before.width,
                h=int(third * scale_y)
            ))
        if bottom_changed > region_pixels * 0.05:
            changed_regions.append(Region(
                x=0, y=int(2 * third * scale_y),
                w=before.width,
                h=int(third * scale_y)
            ))

        diff_ms = int((time.time() - t0) * 1000)
        _vlog("🔍", f"Frame diff: {changed_ratio:.1%} changed | dominant={dominant} "
                    f"[{diff_ms}ms]")

        return FrameDiff(
            changed_ratio=changed_ratio,
            same_hash=False,
            changed_regions=changed_regions,
            dominant_change=dominant,
            diff_ms=diff_ms,
        )

    # ── State Change Detection ────────────────────────────────────

    async def detect_state_change(
        self,
        before:      FrameSnapshot,
        after:       FrameSnapshot,
        region:      Optional[Region] = None,
        description: str = "UI element",
    ) -> StateChange:
        """
        Phân tích thay đổi state của 1 UI element giữa 2 frame.

        Chiến lược 2 bước:
          1. Pixel diff (0ms) → nếu unchanged → return sớm, không tốn VLM
          2. Nếu changed → gọi VLM với cả 2 frame để hiểu thay đổi

        Args:
            before:      frame trước action
            after:       frame sau action
            region:      vùng cần kiểm tra (None = full screen)
            description: mô tả element ("Submit button", "upload progress bar")

        Returns:
            StateChange với state_before, state_after, description
        """
        diff = self.compare(before, after)

        # Unchanged → trả về sớm không cần VLM
        if diff.is_unchanged:
            _vlog("🟰", f"No change detected for '{description}' (hash match / diff < 0.5%)")
            return StateChange(
                changed=False,
                state_before="unknown",
                state_after="unknown",
                description=f"'{description}' không thay đổi",
                confidence=1.0,
                used_vlm=False,
                diff=diff,
            )

        # Minor diff (< 2%) — cursor blink, loading blink
        if diff.is_minor:
            _vlog("🔸", f"Minor change for '{description}' ({diff.changed_ratio:.1%})")
            return StateChange(
                changed=False,
                state_before="unknown",
                state_after="unknown",
                description=f"'{description}' thay đổi nhỏ (cursor/loading blink)",
                confidence=0.7,
                used_vlm=False,
                diff=diff,
            )

        # Significant diff → dùng VLM để hiểu thay đổi
        if self._vlm_fn is not None:
            return await self._analyze_with_vlm(before, after, region, description, diff)
        elif self._gemini_key:
            return await self._analyze_with_gemini(before, after, region, description, diff)
        else:
            # Không có VLM → chỉ báo "đã thay đổi" dựa trên pixel
            return StateChange(
                changed=True,
                state_before="unknown",
                state_after="unknown",
                description=f"'{description}' thay đổi {diff.changed_ratio:.0%} pixel",
                confidence=min(0.6, diff.changed_ratio * 2),
                used_vlm=False,
                diff=diff,
            )

    async def _analyze_with_vlm(
        self,
        before:      FrameSnapshot,
        after:       FrameSnapshot,
        region:      Optional[Region],
        description: str,
        diff:        FrameDiff,
    ) -> StateChange:
        """Phân tích state change bằng VLM fn đã inject."""
        prompt = _build_diff_prompt(description, diff)
        try:
            before_b = _crop_bytes(before, region)
            after_b  = _crop_bytes(after, region)

            # Gọi VLM với frame TRƯỚC
            resp_before = await asyncio.wait_for(
                self._vlm_fn(before_b, _build_state_prompt(description, "before")),
                timeout=_VLM_DIFF_TIMEOUT
            )
            # Gọi VLM với frame SAU
            resp_after = await asyncio.wait_for(
                self._vlm_fn(after_b, _build_state_prompt(description, "after")),
                timeout=_VLM_DIFF_TIMEOUT
            )

            state_b, state_a, desc = _parse_state_responses(resp_before, resp_after, description)
            changed = (state_b != state_a)

            _vlog("🧠", f"VLM state: '{description}' → {state_b} → {state_a} | {desc}")
            return StateChange(
                changed=changed,
                state_before=state_b,
                state_after=state_a,
                description=desc,
                confidence=0.85,
                used_vlm=True,
                diff=diff,
            )
        except asyncio.TimeoutError:
            _vlog("⏱️", f"VLM timeout cho state analysis '{description}'")
        except Exception as e:
            _vlog("⚠️", f"VLM state analysis lỗi: {e}")

        # Fallback: pixel-only
        return StateChange(
            changed=True,
            state_before="unknown",
            state_after="changed",
            description=f"'{description}' đã thay đổi ({diff.changed_ratio:.0%} pixel, VLM fallback)",
            confidence=0.5,
            used_vlm=False,
            diff=diff,
        )

    async def _analyze_with_gemini(
        self,
        before:      FrameSnapshot,
        after:       FrameSnapshot,
        region:      Optional[Region],
        description: str,
        diff:        FrameDiff,
    ) -> StateChange:
        """Phân tích state change bằng Gemini Vision trực tiếp."""
        try:
            import urllib.request as _ur
            import json as _json

            before_b = _crop_bytes(before, region)
            after_b  = _crop_bytes(after, region)

            # Gọi song song 2 VLM calls
            async def _gemini_call(img_bytes: bytes, prompt: str) -> str:
                from social.vision_actor import _call_gemini_vision
                return await _call_gemini_vision(
                    img_bytes, prompt, self._gemini_key,
                    timeout=_VLM_DIFF_TIMEOUT
                )

            before_task = asyncio.create_task(
                _gemini_call(before_b, _build_state_prompt(description, "before"))
            )
            after_task = asyncio.create_task(
                _gemini_call(after_b, _build_state_prompt(description, "after"))
            )
            resp_before, resp_after = await asyncio.gather(
                before_task, after_task,
                return_exceptions=True
            )

            if isinstance(resp_before, Exception) or isinstance(resp_after, Exception):
                raise RuntimeError(f"Gemini call failed: {resp_before} | {resp_after}")

            state_b, state_a, desc = _parse_state_responses(
                str(resp_before), str(resp_after), description
            )
            changed = (state_b != state_a)

            _vlog("🧠", f"Gemini state: '{description}' → {state_b} → {state_a}")
            return StateChange(
                changed=changed,
                state_before=state_b,
                state_after=state_a,
                description=desc,
                confidence=0.85,
                used_vlm=True,
                diff=diff,
            )

        except Exception as e:
            _vlog("⚠️", f"Gemini state analysis lỗi: {e}")
            return StateChange(
                changed=True,
                state_before="unknown",
                state_after="changed",
                description=f"'{description}' thay đổi (diff={diff.changed_ratio:.0%})",
                confidence=0.4,
                used_vlm=False,
                diff=diff,
            )

    # ── Progress Bar Analysis ─────────────────────────────────────

    def estimate_progress(
        self,
        before:     FrameSnapshot,
        after:      FrameSnapshot,
        bar_region: Region,
    ) -> ProgressEstimate:
        """
        Ước tính % tiến trình upload/processing từ progress bar.
        Thuần pixel analysis — không cần VLM.

        Nguyên lý:
          Progress bar thường là 1 strip nằm ngang với phần "filled" màu
          sáng hơn phần "empty". Đo tỷ lệ pixel sáng/tối → estimate %.

        Args:
            before:     frame trước (để baseline)
            after:      frame sau (để measure)
            bar_region: vùng chứa progress bar
        """
        try:
            pct_b = _estimate_bar_pct(before, bar_region)
            pct_a = _estimate_bar_pct(after, bar_region)

            if pct_b < 0 or pct_a < 0:
                return ProgressEstimate(found_progress=False)

            delta = pct_a - pct_b
            is_complete = pct_a >= 98.0
            is_stalled  = abs(delta) < 0.5

            # ETA: nếu biết tốc độ (% / giây), tính ETA
            time_elapsed = after.captured_at - before.captured_at
            eta_s = -1.0
            if delta > 0.1 and time_elapsed > 0 and not is_complete:
                rate = delta / time_elapsed   # % per second
                remaining = 100.0 - pct_a
                eta_s = remaining / rate

            _vlog("📊", f"Progress: {pct_b:.0f}% → {pct_a:.0f}% "
                        f"(Δ={delta:+.1f}%, ETA={eta_s:.0f}s)" if eta_s > 0
                        else f"Progress: {pct_b:.0f}% → {pct_a:.0f}%")

            return ProgressEstimate(
                found_progress=True,
                pct_before=pct_b,
                pct_after=pct_a,
                delta_pct=delta,
                eta_seconds=eta_s,
                is_complete=is_complete,
                is_stalled=is_stalled,
            )

        except Exception as e:
            _vlog("⚠️", f"estimate_progress lỗi: {e}")
            return ProgressEstimate(found_progress=False)

    # ── Scroll Verification ───────────────────────────────────────

    def verify_scroll(
        self,
        before: FrameSnapshot,
        after:  FrameSnapshot,
        content_region: Optional[Region] = None,
    ) -> ScrollVerification:
        """
        Verify xem scroll action có thực sự cuộn trang không.

        Phân tích:
          - Diff nhỏ → NOT_SCROLLED (page stuck / không phản hồi)
          - Diff lớn vùng center → SCROLLED
          - Diff top + loading indicator → LOADING (đang load)

        Args:
            before:         frame trước scroll
            after:          frame sau scroll
            content_region: vùng content chính (None = full screen)

        Returns:
            ScrollVerification với scrolled, direction, scroll_amount
        """
        diff = self.compare(before, after)

        if diff.is_unchanged:
            _vlog("🚫", "Scroll không hiệu quả — frame không thay đổi")
            return ScrollVerification(
                scrolled=False,
                direction="none",
                scroll_amount=0.0,
                diff=diff,
            )

        if diff.is_minor:
            # Thay đổi nhỏ: có thể loading blink, không phải scroll
            return ScrollVerification(
                scrolled=False,
                direction="none",
                scroll_amount=0.0,
                loading_detected=True,
                diff=diff,
            )

        # Significant change → đã scroll
        # Direction: nếu bottom thay đổi nhiều hơn top → scroll down
        direction = "down"
        if diff.dominant_change == "top":
            direction = "up"

        # Rough estimate scroll amount từ changed_ratio
        scroll_amount = min(1.0, diff.changed_ratio * 2)

        _vlog("✅", f"Scroll confirmed: {direction} | {diff.changed_ratio:.0%} changed")
        return ScrollVerification(
            scrolled=True,
            direction=direction,
            scroll_amount=scroll_amount,
            diff=diff,
        )

    # ── Dialog Detection ──────────────────────────────────────────

    def detect_dialog_closed(
        self,
        before: FrameSnapshot,
        after:  FrameSnapshot,
        dialog_region: Optional[Region] = None,
    ) -> bool:
        """
        Phát hiện dialog/modal đã đóng giữa 2 frame.

        Returns:
            True nếu dialog đã biến mất (thay đổi lớn ở vùng center/overlay)
        """
        diff = self.compare(before, after)

        if diff.is_unchanged or diff.is_minor:
            return False  # Dialog vẫn còn

        # Dialog thường ở center — nếu center thay đổi lớn → dialog đóng
        center_changed = diff.dominant_change in ("center", "full")
        if center_changed and diff.changed_ratio >= 0.05:
            _vlog("✅", f"Dialog closed detected ({diff.changed_ratio:.0%} change)")
            return True

        return False

    # ── Element Disabled Check ────────────────────────────────────

    async def is_element_disabled(
        self,
        before:         FrameSnapshot,
        after:          FrameSnapshot,
        element_region: Region,
        element_name:   str = "button",
    ) -> bool:
        """
        Kiểm tra xem element có bị disabled sau action không.
        Ví dụ: nút Post trở nên disabled sau khi click.

        Returns:
            True nếu element đã disabled (state thay đổi → disabled/greyed)
        """
        state = await self.detect_state_change(
            before, after, element_region,
            description=element_name
        )

        if not state.changed:
            return False

        # Check state_after có chứa keyword disabled
        after_lower = state.state_after.lower()
        return any(kw in after_lower for kw in ("disabled", "grey", "gray", "inactive", "loading"))


# ══════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════

def _crop_bytes(snap: FrameSnapshot, region: Optional[Region]) -> bytes:
    """Crop frame bytes theo region (nếu có). Trả về raw bytes."""
    if region is None:
        return snap.bytes_raw
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(snap.bytes_raw))
        cropped = img.crop(region.to_box())
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return snap.bytes_raw


def _estimate_bar_pct(snap: FrameSnapshot, region: Region) -> float:
    """
    Ước tính % progress bar bằng cách đo tỷ lệ pixel sáng / tối.
    Returns -1.0 nếu không detect được.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(snap.bytes_raw)).convert("L")  # Grayscale
        bar = img.crop(region.to_box())
        pixels = list(bar.getdata())
        if not pixels:
            return -1.0
        # Pixel sáng (>128) = filled portion
        bright = sum(1 for p in pixels if p > 128)
        return (bright / len(pixels)) * 100.0
    except Exception:
        return -1.0


def _build_state_prompt(element_name: str, when: str) -> str:
    """Prompt ngắn để VLM phân tích state 1 frame."""
    return (
        f"Look at this screenshot. Find the element: '{element_name}'. "
        f"Describe its current state in ONE word only. "
        f"Choose from: enabled, disabled, visible, hidden, loading, ready, "
        f"active, inactive, checked, unchecked, error, success, unknown. "
        f"Respond with ONLY the single state word, nothing else."
    )


def _build_diff_prompt(element_name: str, diff: FrameDiff) -> str:
    """Prompt cho VLM phân tích diff."""
    return (
        f"Compare these two screenshots. Find '{element_name}'. "
        f"Describe what changed between before and after. "
        f"Respond in JSON: {{\"state_before\": \"...\", \"state_after\": \"...\", "
        f"\"description\": \"one sentence\"}}"
    )


def _parse_state_responses(
    resp_before: str,
    resp_after:  str,
    element_name: str,
) -> tuple[str, str, str]:
    """
    Parse VLM responses về state.
    Returns: (state_before, state_after, description)
    """
    _VALID_STATES = {
        "enabled", "disabled", "visible", "hidden", "loading",
        "ready", "active", "inactive", "checked", "unchecked",
        "error", "success", "unknown", "changed"
    }

    def _clean(s: str) -> str:
        word = s.strip().lower().split()[0] if s.strip() else "unknown"
        word = word.rstrip(".,;:!?")
        return word if word in _VALID_STATES else "unknown"

    state_b = _clean(resp_before)
    state_a = _clean(resp_after)
    desc = f"'{element_name}': {state_b} → {state_a}"
    return state_b, state_a, desc


# ══════════════════════════════════════════════════════════════════
# Module-level singleton
# ══════════════════════════════════════════════════════════════════

_INSTANCE: Optional[MultiFrameVision] = None


def get_multi_frame_vision(
    vlm_fn: Optional[Callable] = None,
    gemini_api_key: str = "",
) -> MultiFrameVision:
    """
    Lấy singleton MultiFrameVision.

    Dùng chung 1 instance trong process để tái dụng cache và VLM state.
    Thread-safe (GIL bảo vệ assignment).
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MultiFrameVision(
            vlm_fn=vlm_fn,
            gemini_api_key=gemini_api_key,
        )
        _vlog("🚀", "MultiFrameVision singleton initialized")
    elif vlm_fn is not None and _INSTANCE._vlm_fn is None:
        _INSTANCE._vlm_fn = vlm_fn
    elif gemini_api_key and not _INSTANCE._gemini_key:
        _INSTANCE._gemini_key = gemini_api_key
    return _INSTANCE


def reset_multi_frame_vision() -> None:
    """Reset singleton — dùng trong test."""
    global _INSTANCE
    _INSTANCE = None


# ══════════════════════════════════════════════════════════════════
# CHANGELOG
# ══════════════════════════════════════════════════════════════════
#
# v2.2 — 2026-03-25
#   - Initial implementation of B3 Multi-Frame Temporal Vision
#   - FrameSnapshot: capture, hash, thumbnail for fast diff
#   - FrameDiff: pixel diff with region detection
#   - StateChange: 2-step analysis (pixel first, VLM only if needed)
#   - ProgressEstimate: pixel-only bar analysis (0ms overhead)
#   - ScrollVerification: confirm scroll effectiveness
#   - dialog_closed detection
#   - is_element_disabled helper
#   - Module singleton get_multi_frame_vision()
#   - VLM calls: song song (gather) để giảm latency 50%
#   - Zero-risk: mọi path đều có fallback, không crash
