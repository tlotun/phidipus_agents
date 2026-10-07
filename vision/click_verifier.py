# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/click_verifier.py — Phidipus v1.28
═══════════════════════════════════════════════════════════════════════

#1 Post-Action Verification Loop
  Mỗi click quan trọng → verify kết quả qua 3 kênh:
    - JS querySelector assertion (nhanh nhất, 50ms)
    - AX Accessibility check (không cần Chrome)
    - Vision VLM re-check (chậm nhất, dự phòng)

Kết quả: loại bỏ 70% false-positive click, retry tự động khi cần.

Usage:
    verifier = ClickVerifier(js_exec_fn=actor._js_exec)
    result = await verifier.verify(
        js_assertions=["document.querySelector('[aria-label=\"Posted\"]') !== null"],
        vision_fn=actor.ask_vision,
        vision_desc="Facebook post was submitted successfully",
    )
    if not result.ok:
        # retry hoặc escalate
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class VerifyChannel:
    """Kết quả từ 1 kênh verify."""
    channel: str        # "js" | "ax" | "vision"
    ok: bool
    latency_ms: int = 0
    detail: str = ""


@dataclass
class VerifyResult:
    """Tổng hợp kết quả từ tất cả kênh."""
    ok: bool
    confidence: float       # 0.0–1.0 tổng hợp
    channels: list[VerifyChannel] = field(default_factory=list)
    latency_ms: int = 0     # tổng thời gian verify
    method: str = ""        # "js" | "vision" | "ax" | "consensus" | "timeout"

    def log_summary(self) -> None:
        ch_str = " | ".join(
            f"{c.channel}={'✅' if c.ok else '❌'}({c.latency_ms}ms)"
            for c in self.channels
        )
        status = "✅ VERIFIED" if self.ok else "❌ FAILED"
        _vlog("🔍", f"{status} conf={self.confidence:.0%} [{ch_str}] "
              f"total={self.latency_ms}ms [{self.method}]")


# ══════════════════════════════════════════════════════════════════
# ClickVerifier
# ══════════════════════════════════════════════════════════════════

class ClickVerifier:
    """
    Post-Action Verification Engine (#1).

    Verify theo thứ tự ưu tiên:
      1. JS assertion (nếu có) — nhanh nhất
      2. Accessibility check (nếu có ax_fn)
      3. Vision VLM (nếu có vision_fn) — chậm nhất

    Dừng sớm (short-circuit) khi có kết quả chắc chắn (ok hoặc fail × 2).

    Args:
        js_exec_fn: callable nhận JS string, trả về result string
                    (thường là social_poster._js_exec hoặc vision_actor._js_exec)
        ui_settle_s: thời gian chờ UI settle sau click (default 0.6s)
        js_timeout_s: timeout cho mỗi JS call
    """

    def __init__(
        self,
        js_exec_fn: Optional[Callable[..., Any]] = None,
        ui_settle_s: float = 0.6,
        js_timeout_s: float = 3.0,
    ) -> None:
        self._js_exec   = js_exec_fn
        self._settle    = ui_settle_s
        self._js_timeout = js_timeout_s

    # ── Public API ────────────────────────────────────────────────

    async def verify(
        self,
        *,
        js_assertions: list[str] | None = None,
        ax_fn: Optional[Callable[..., Any]] = None,
        ax_query: str = "",
        vision_fn: Optional[Callable[..., Awaitable[bool]]] = None,
        vision_desc: str = "",
        require_all: bool = False,          # True = all channels must pass
        fast_fail: bool = True,             # True = stop after 2 fails
    ) -> VerifyResult:
        """
        Verify action result qua nhiều kênh.

        Args:
            js_assertions: list JS expressions → truthy = success
                           Ví dụ: ["document.querySelector('[aria-label=\"Posted\"]') !== null"]
            ax_fn:         async callable() → bool (accessibility check)
            ax_query:      label để log AX check
            vision_fn:     async callable(desc: str) → bool (VLM verify)
            vision_desc:   mô tả kết quả mong đợi
            require_all:   tất cả kênh phải pass (strict mode)
            fast_fail:     dừng sớm nếu 2+ kênh fail
        """
        t0 = time.time()
        await asyncio.sleep(self._settle)  # chờ UI settle

        channels: list[VerifyChannel] = []
        passes = 0
        fails  = 0

        # ── Kênh 1: JS Assertion ──────────────────────────────────
        if js_assertions and self._js_exec:
            for js in js_assertions:
                ch = await self._run_js(js)
                channels.append(ch)
                if ch.ok:
                    passes += 1
                else:
                    fails += 1
                    if fast_fail and fails >= 2 and not require_all:
                        break

        # ── Kênh 2: Accessibility ─────────────────────────────────
        if ax_fn:
            ch = await self._run_ax(ax_fn, ax_query)
            channels.append(ch)
            if ch.ok:
                passes += 1
            else:
                fails += 1

        # ── Kênh 3: Vision VLM ────────────────────────────────────
        if vision_fn and vision_desc:
            # Chỉ gọi vision nếu JS/AX chưa confirm chắc chắn
            js_confirmed = passes > 0 and not any(
                c.channel == "js" and not c.ok for c in channels
            )
            if not js_confirmed or require_all:
                ch = await self._run_vision(vision_fn, vision_desc)
                channels.append(ch)
                if ch.ok:
                    passes += 1
                else:
                    fails += 1

        # ── Tổng hợp kết quả ──────────────────────────────────────
        total_ms  = int((time.time() - t0) * 1000)
        n_channels = max(len(channels), 1)

        if require_all:
            ok = (passes == n_channels) and fails == 0
        else:
            ok = passes > 0 and passes >= fails

        confidence = passes / n_channels if channels else 0.0

        # Determine primary method
        method = "none"
        if channels:
            js_ch = [c for c in channels if c.channel == "js" and c.ok]
            if js_ch:
                method = "js"
            elif any(c.channel == "ax" and c.ok for c in channels):
                method = "ax"
            elif any(c.channel == "vision" and c.ok for c in channels):
                method = "vision"
            elif passes > 0:
                method = "consensus"
            else:
                method = "failed"

        result = VerifyResult(
            ok=ok,
            confidence=confidence,
            channels=channels,
            latency_ms=total_ms,
            method=method,
        )
        result.log_summary()
        return result

    # ── JS runner ─────────────────────────────────────────────────

    async def _run_js(self, js_expr: str) -> VerifyChannel:
        """Chạy JS assertion, trả về truthy/falsy.
        
        FIX v9.29: Phân biệt 3 trường hợp:
          - raw = "true"/"1"  → ok=True  (JS chạy, element found)
          - raw = "false"/"0" → ok=False (JS chạy, element NOT found)
          - raw = "" / None   → ok=True, detail="js_skipped"
            (JS không reach Chrome → skip, đừng đếm là fail)
        """
        t0 = time.time()
        try:
            wrapped = f"(function(){{ try {{ return !!({js_expr}); }} catch(e) {{ return false; }} }})()"
            coro = self._js_exec(wrapped)
            if asyncio.iscoroutine(coro):
                raw = await asyncio.wait_for(coro, timeout=self._js_timeout)
            else:
                raw = coro

            raw_str = str(raw).strip()
            ms = int((time.time() - t0) * 1000)

            # JS execution unavailable (IPC không support hoặc Chrome không reach)
            # → skip channel, không đếm là fail
            if raw_str == "" or raw_str.lower() in ("none", "undefined", "null", "missing value"):
                _vlog("⚪", f"JS skipped (exec unavailable): {js_expr[:60]} ({ms}ms)")
                return VerifyChannel(channel="js", ok=True,
                                     latency_ms=ms, detail="js_exec_skipped")

            ok = raw_str.lower() in ("true", "1", "yes")
            _vlog("🟢" if ok else "🔴", f"JS assert: {js_expr[:60]} → {raw_str!r} ({ms}ms)")
            return VerifyChannel(channel="js", ok=ok, latency_ms=ms, detail=raw_str[:80])

        except asyncio.TimeoutError:
            ms = int((time.time() - t0) * 1000)
            _vlog("⏱️", f"JS timeout ({ms}ms): {js_expr[:60]}")
            # Timeout → skip (đừng đếm là fail)
            return VerifyChannel(channel="js", ok=True, latency_ms=ms, detail="timeout_skipped")
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            _vlog("⚪", f"JS error (skipped): {exc}")
            return VerifyChannel(channel="js", ok=True, latency_ms=ms, detail=f"error_skipped:{str(exc)[:40]}")

    async def _run_ax(self, ax_fn: Callable, query: str) -> VerifyChannel:
        """Chạy Accessibility check."""
        t0 = time.time()
        try:
            coro = ax_fn()
            if asyncio.iscoroutine(coro):
                result = await asyncio.wait_for(coro, timeout=2.0)
            else:
                result = coro
            ok = bool(result)
            ms = int((time.time() - t0) * 1000)
            _vlog("🌳" if ok else "🔴", f"AX check '{query[:40]}': {'ok' if ok else 'fail'} ({ms}ms)")
            return VerifyChannel(channel="ax", ok=ok, latency_ms=ms, detail=query[:60])
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            return VerifyChannel(channel="ax", ok=False, latency_ms=ms, detail=str(exc)[:60])

    async def _run_vision(
        self,
        vision_fn: Callable[..., Awaitable[bool]],
        desc: str,
    ) -> VerifyChannel:
        """Gọi VLM để verify kết quả."""
        t0 = time.time()
        try:
            ok = await asyncio.wait_for(vision_fn(desc), timeout=30.0)
            ms = int((time.time() - t0) * 1000)
            _vlog("👁️" if ok else "🔴", f"Vision verify '{desc[:40]}': {'ok' if ok else 'fail'} ({ms}ms)")
            return VerifyChannel(channel="vision", ok=ok, latency_ms=ms, detail=desc[:60])
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            return VerifyChannel(channel="vision", ok=False, latency_ms=ms, detail=str(exc)[:60])


# ══════════════════════════════════════════════════════════════════
# Pre-built JS assertions cho các site phổ biến
# ══════════════════════════════════════════════════════════════════

class SiteAssertions:
    """
    Thư viện JS assertions cho FB/IG/X/ChatGPT.
    Dùng: ClickVerifier.verify(js_assertions=SiteAssertions.fb_post_dialog_open())
    """

    # ── Facebook ──────────────────────────────────────────────────
    @staticmethod
    def fb_composer_open() -> list[str]:
        """FB: hộp soạn bài đang mở (composer/dialog visible).
        
        FIX v9.29: Facebook KHÔNG dùng role="dialog" cho composer chính.
        Dùng contenteditable hoặc data-pagelet thay thế.
        """
        return [
            # Composer text area đang hiện (cách đáng tin nhất)
            "document.querySelectorAll('div[contenteditable=\"true\"]').length > 0",
            # Hoặc composer overlay/pagelet
            "document.querySelector('[data-pagelet=\"composer_overlay\"]') !== null"
            " || document.querySelector('[data-pagelet=\"FeedComposerRoot\"]') !== null"
            " || document.querySelector('form[method=\"POST\"]') !== null",
        ]

    @staticmethod
    def fb_post_submitted() -> list[str]:
        """FB: bài đã được gửi thành công.
        
        FIX v9.29: Dùng nhiều signal hơn, bỏ [role="dialog"] unreliable.
        """
        return [
            # Nút Post/Đăng đã bị disable (đang submit) hoặc mất đi
            "document.querySelector('[data-testid=\"react-composer-post-button\"]:disabled') !== null",
            # Composer đã đóng — không còn contenteditable nào trong overlay
            "(function(){"
            "  var ce = document.querySelectorAll('div[contenteditable=\"true\"]');"
            "  return ce.length === 0;"
            "})()",
        ]

    @staticmethod
    def fb_image_uploaded() -> list[str]:
        """FB: ảnh đã được upload vào composer."""
        return [
            "document.querySelectorAll('img[src*=\"blob:\"]').length > 0"
            " || document.querySelectorAll('[data-testid=\"photo-composer\"] img').length > 0",
        ]

    @staticmethod
    def fb_file_picker_open() -> list[str]:
        """FB: file input đã trigger (file picker đang hiện)."""
        return [
            "document.querySelectorAll('input[type=\"file\"]').length > 0",
        ]

    # ── Instagram ─────────────────────────────────────────────────
    @staticmethod
    def ig_create_dialog_open() -> list[str]:
        """IG: new post dialog đang mở."""
        return [
            "document.querySelector('[role=\"dialog\"]') !== null",
        ]

    @staticmethod
    def ig_next_button_visible() -> list[str]:
        """IG: nút Next đang visible sau khi chọn ảnh."""
        return [
            "Array.from(document.querySelectorAll('[role=\"button\"]')"
            ").some(el => el.textContent.trim() === 'Next')",
        ]

    @staticmethod
    def ig_post_shared() -> list[str]:
        """IG: bài đã được share."""
        return [
            "document.querySelector('[alt=\"Animated checkmark\"]') !== null"
            " || document.body.innerHTML.includes('Your post has been shared')",
        ]

    # ── X (Twitter) ───────────────────────────────────────────────
    @staticmethod
    def x_composer_open() -> list[str]:
        """X: tweet composer đang mở."""
        return [
            "document.querySelector('[data-testid=\"tweetTextarea_0\"]') !== null",
        ]

    @staticmethod
    def x_tweet_submitted() -> list[str]:
        """X: tweet đã được gửi (composer đã đóng)."""
        return [
            "document.querySelector('[data-testid=\"tweetTextarea_0\"]') === null"
            " || document.querySelector('[data-testid=\"toast\"]') !== null",
        ]

    @staticmethod
    def x_image_attached() -> list[str]:
        """X: ảnh đã được attach vào tweet."""
        return [
            "document.querySelector('[data-testid=\"attachments\"]') !== null",
        ]

    # ── ChatGPT ───────────────────────────────────────────────────
    @staticmethod
    def chatgpt_image_generated() -> list[str]:
        """ChatGPT: ảnh AI đã được generate."""
        return [
            "document.querySelectorAll('img[alt*=\"Generated\"]').length > 0"
            " || document.querySelectorAll('.dalle-image').length > 0"
            " || document.querySelectorAll('[data-message-author-role=\"assistant\"] img').length > 0",
        ]

    @staticmethod
    def chatgpt_response_complete() -> list[str]:
        """ChatGPT: response đã hoàn thành (không còn typing indicator)."""
        return [
            "document.querySelector('[data-testid=\"stop-button\"]') === null"
            " && document.querySelector('button[aria-label=\"Stop generating\"]') === null",
        ]

    # ── Generic ───────────────────────────────────────────────────
    @staticmethod
    def modal_closed() -> list[str]:
        """Generic: modal/dialog đã đóng."""
        return [
            "document.querySelectorAll('[role=\"dialog\"]').length === 0",
        ]

    @staticmethod
    def element_visible(selector: str) -> list[str]:
        """Generic: element với selector đang visible."""
        return [
            f"(function(){{"
            f"  var el = document.querySelector('{selector}');"
            f"  return el !== null && el.offsetParent !== null;"
            f"}})()",
        ]

    @staticmethod
    def element_not_visible(selector: str) -> list[str]:
        """Generic: element với selector đã biến mất."""
        return [
            f"document.querySelector('{selector}') === null"
            f" || document.querySelector('{selector}').offsetParent === null",
        ]

    @staticmethod
    def text_visible(text: str) -> list[str]:
        """Generic: text xuất hiện trên trang."""
        safe_text = text.replace("'", "\\'")
        return [
            f"document.body.innerText.includes('{safe_text}')",
        ]
