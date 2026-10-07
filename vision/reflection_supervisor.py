# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/reflection_supervisor.py — Phidipus v1.31
═══════════════════════════════════════════════════════════════════════

Reflection Supervisor — Agent tự hỏi "Mình vừa làm gì sai?"

Vấn đề từ log thực tế (2026-03-21):
  Bot click B6 "What's on your mind" 4 lần liên tiếp cùng tọa độ.
  Mỗi lần: VLM nói YES (composer mở), JS nói NO (dialog không thấy).
  Bot không hiểu tại sao → retry mù quáng → mất 62 giây, không đăng được.

  Root cause thật: Composer FB đã mở NHƯNG JS không reach Chrome
  → verify luôn fail → không phải click sai, mà verify sai.

Reflection Supervisor giải quyết:
  Sau mỗi verify failed → chụp screenshot → hỏi VLM 1 prompt:
  "Nhìn vào màn hình: action {action} tại ({x},{y}) có thực sự thành công không?
   Nếu không: lý do? Bước tiếp theo?"

  VLM trả JSON với NextAction → bot hành động thông minh thay vì retry mù.

NextAction types:
  RETRY_SAME       → click lại cùng tọa độ (element bị miss lần trước)
  RETRY_SCROLL     → scroll rồi mới click (element bị che / ngoài viewport)
  RETRY_WAIT       → chờ N giây rồi click (trang chưa load xong)
  RETRY_DIFFERENT  → dùng strategy khác (ZoomAction/VLM thay cache)
  ALREADY_DONE     → action đã thành công, verify là false positive → tiếp tục
  ESCALATE         → không tự fix được → báo lỗi lên, dừng retry

Tích hợp:
  Trong find_and_click_v2(), thay đoạn:
    if not verified and attempt < retries:
        continue   ← mù quáng

  Thành:
    if not verified and attempt < retries:
        reflection = await supervisor.reflect(...)
        if reflection.next == NextAction.ALREADY_DONE:
            verified = True; break          # không retry
        elif reflection.next == NextAction.RETRY_SCROLL:
            await self._scroll_down()
        elif reflection.next == NextAction.RETRY_WAIT:
            await asyncio.sleep(reflection.wait_s)
        elif reflection.next == NextAction.ESCALATE:
            break                           # dừng sớm, không mất thêm thời gian
        # RETRY_SAME / RETRY_DIFFERENT → continue bình thường

Usage:
    supervisor = ReflectionSupervisor(vlm_fn=actor._vlm.call)
    reflection = await supervisor.reflect(
        action="click",
        x=787, y=300,
        query="Facebook Create post box",
        verify_result=v_result,
        screenshot_fn=actor.screenshot,
    )
    print(reflection.next, reflection.reason, reflection.wait_s)
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Optional

from utils.logger import get_logger


def _resolve_model(name: str, role: str | None = None) -> str:
    """v4.3: map a legacy hard-coded model name to an installed one (model registry)."""
    try:
        from core.model_registry import resolve_model
        return resolve_model(name, role)
    except Exception:
        return name

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# NextAction enum
# ══════════════════════════════════════════════════════════════════

class NextAction(Enum):
    RETRY_SAME      = "retry_same"       # click lại đúng tọa độ cũ
    RETRY_SCROLL    = "retry_scroll"     # scroll rồi retry
    RETRY_WAIT      = "retry_wait"       # chờ N giây rồi retry
    RETRY_DIFFERENT = "retry_different"  # đổi strategy (ZoomAction/VLM khác)
    ALREADY_DONE    = "already_done"     # action đã thành công thật sự
    ESCALATE        = "escalate"         # không tự fix, dừng retry

    @staticmethod
    def from_str(s: str) -> "NextAction":
        mapping = {
            "retry_same":      NextAction.RETRY_SAME,
            "retry_scroll":    NextAction.RETRY_SCROLL,
            "retry_wait":      NextAction.RETRY_WAIT,
            "retry_different": NextAction.RETRY_DIFFERENT,
            "already_done":    NextAction.ALREADY_DONE,
            "escalate":        NextAction.ESCALATE,
        }
        return mapping.get(s.lower().strip(), NextAction.RETRY_SAME)


# ══════════════════════════════════════════════════════════════════
# ReflectionResult
# ══════════════════════════════════════════════════════════════════

@dataclass
class ReflectionResult:
    """Kết quả reflection sau 1 verify-failed."""
    success:      bool           # VLM nghĩ action có thực sự thành công không
    next:         NextAction     # bước tiếp theo nên làm gì
    reason:       str   = ""     # lý do fail (ngắn gọn)
    detail:       str   = ""     # giải thích thêm
    wait_s:       float = 0.0    # giây cần chờ (cho RETRY_WAIT)
    scroll_dir:   str   = "down" # hướng scroll (cho RETRY_SCROLL)
    scroll_px:    int   = 300    # pixels scroll
    confidence:   float = 0.0    # VLM tự tin bao nhiêu về diagnosis này
    latency_ms:   int   = 0
    tier:         str   = ""     # "vlm" | "rule" | "fallback"

    def log(self) -> None:
        icon = "✅" if self.success else "🔍"
        _vlog(icon, f"Reflection → next={self.next.value} "
              f"reason='{self.reason[:60]}' "
              f"wait={self.wait_s:.0f}s conf={self.confidence:.0%} "
              f"[{self.tier}] {self.latency_ms}ms")


# ══════════════════════════════════════════════════════════════════
# Rule-based fast diagnosis (Tier 1, <5ms, no VLM)
# ══════════════════════════════════════════════════════════════════

@dataclass
class _RuleMatch:
    pattern: str        # regex match trên reason/detail string
    next: NextAction
    wait_s: float = 0.0
    reason: str = ""


_FAST_RULES: list[_RuleMatch] = [
    # JS exec unavailable → verify luôn false → thật ra đã done
    _RuleMatch(
        pattern=r"js_exec_skipped|js.*unavailable|missing value|ipc.*not.*support",
        next=NextAction.ALREADY_DONE,
        reason="JS verify không reach Chrome — action có thể đã thành công",
    ),
    # Trang chưa load
    _RuleMatch(
        pattern=r"timeout|loading|spinner|not.*loaded|slow",
        next=NextAction.RETRY_WAIT,
        wait_s=3.0,
        reason="Trang đang load — chờ 3s rồi retry",
    ),
    # Element ngoài viewport
    _RuleMatch(
        pattern=r"scroll|below.*fold|not.*visible|out.*view|viewport",
        next=NextAction.RETRY_SCROLL,
        reason="Element có thể bị che hoặc ngoài viewport",
    ),
    # Overlay/popup chặn
    _RuleMatch(
        pattern=r"overlay|modal|popup|blocked|covered|z-index",
        next=NextAction.RETRY_WAIT,
        wait_s=1.5,
        reason="Có overlay đang chặn — chờ dismiss rồi retry",
    ),
    # Tọa độ lệch rõ ràng (confidence thấp từ trước)
    _RuleMatch(
        pattern=r"wrong.*coord|wrong.*position|missed|offset|lệch",
        next=NextAction.RETRY_DIFFERENT,
        reason="Tọa độ click có thể sai — thử strategy khác",
    ),
]


def _fast_rule_check(
    verify_channels: list,   # list[VerifyChannel] từ ClickVerifier
    query: str,
) -> ReflectionResult | None:
    """
    Tier 1: kiểm tra nhanh bằng rules trước khi gọi VLM.
    Trả về None nếu không match rule nào.
    """
    # Gom tất cả detail strings từ verify channels
    all_details = " ".join(
        getattr(ch, "detail", "") for ch in verify_channels
    ).lower()

    # Rule đặc biệt: tất cả JS channels đều "skipped" nhưng Vision pass
    js_channels = [ch for ch in verify_channels if getattr(ch, "channel", "") == "js"]
    vision_channels = [ch for ch in verify_channels if getattr(ch, "channel", "") == "vision"]

    if js_channels and vision_channels:
        js_all_skipped = all(
            "skipped" in getattr(ch, "detail", "") or not ch.ok
            for ch in js_channels
        )
        vision_passed = any(ch.ok for ch in vision_channels)
        if js_all_skipped and vision_passed:
            return ReflectionResult(
                success=True,
                next=NextAction.ALREADY_DONE,
                reason="Vision xác nhận thành công, JS verify không reach Chrome",
                detail="JS exec unavailable là bình thường khi IPC không support browser_execute_js",
                confidence=0.85,
                tier="rule",
            )

    # Check các rule khác
    for rule in _FAST_RULES:
        if re.search(rule.pattern, all_details, re.IGNORECASE):
            return ReflectionResult(
                success=False,
                next=rule.next,
                reason=rule.reason,
                wait_s=rule.wait_s,
                confidence=0.80,
                tier="rule",
            )

    return None  # không match → gọi VLM


# ══════════════════════════════════════════════════════════════════
# VLM Prompt
# ══════════════════════════════════════════════════════════════════

_REFLECTION_PROMPT = """\
You are analyzing a UI automation action result.

Action performed: {action} at screen coordinates ({x}, {y})
Goal: {query}
Verify channels: {verify_summary}

Look at the screenshot carefully. Answer these questions:
1. Did the action ACTUALLY succeed? (Look at actual UI state, not just the verify result)
2. If failed: WHY? Choose the most likely reason:
   - wrong_coords: clicked wrong position
   - not_loaded: page/element still loading
   - covered: element blocked by overlay/popup
   - need_scroll: element outside visible area
   - already_done: action succeeded but verify gave false negative
   - unknown: cannot determine
3. What should the agent do next?

Respond ONLY with JSON (no markdown):
{{
  "success": <true if action actually worked>,
  "reason": "<one of: wrong_coords|not_loaded|covered|need_scroll|already_done|unknown>",
  "next": "<one of: retry_same|retry_scroll|retry_wait|retry_different|already_done|escalate>",
  "wait_s": <seconds to wait before retry, 0 if no wait needed>,
  "scroll_dir": "<up|down, only if next=retry_scroll>",
  "detail": "<brief explanation in 1 sentence>",
  "confidence": <0.0-1.0 how confident you are>
}}
"""

_REFLECTION_PROMPT_NOIMAGE = """\
You are diagnosing a failed UI automation action.

Action: {action} at ({x}, {y})
Goal: {query}
Verify result summary: {verify_summary}

Based on the verify details, diagnose the issue and recommend next step.

Common patterns:
- If JS verify returned empty/skipped but Vision said YES → likely already_done
- If all channels failed → likely wrong_coords or need_scroll
- If timed out → not_loaded, wait 3s

Respond ONLY with JSON:
{{
  "success": false,
  "reason": "<wrong_coords|not_loaded|covered|need_scroll|already_done|unknown>",
  "next": "<retry_same|retry_scroll|retry_wait|retry_different|already_done|escalate>",
  "wait_s": <0-10>,
  "scroll_dir": "down",
  "detail": "<1 sentence>",
  "confidence": <0.0-1.0>
}}
"""


# ══════════════════════════════════════════════════════════════════
# ReflectionSupervisor
# ══════════════════════════════════════════════════════════════════

class ReflectionSupervisor:
    """
    Supervisor tự phân tích lý do fail và đề xuất bước tiếp theo.

    Flow:
      1. Tier 1 (Rule-based, <5ms): check các pattern rõ ràng
         → nếu match → trả kết quả ngay, không tốn VLM call
      2. Tier 2 (VLM + screenshot, ~15s): chụp ảnh → gửi VLM
         → phân tích visual + verify details
      3. Tier 3 (fallback, 0ms): nếu VLM timeout/fail
         → trả RETRY_SAME với default wait 2s

    Args:
        vlm_fn:        async (img_bytes, prompt) → (raw_str, tier_str)
        screenshot_fn: async () → bytes
        use_vision:    có chụp ảnh gửi VLM không (default True)
                       False → chỉ dùng Rule + text-based VLM
        max_wait_s:    giới hạn wait_s trả về (default 10s)
    """

    def __init__(
        self,
        vlm_fn: Optional[Callable[..., Awaitable]] = None,
        screenshot_fn: Optional[Callable[..., Awaitable]] = None,
        use_vision: bool = True,
        max_wait_s: float = 10.0,
    ) -> None:
        self._vlm          = vlm_fn
        self._screen       = screenshot_fn
        self._use_vision   = use_vision
        self._max_wait     = max_wait_s

    # ── Public API ────────────────────────────────────────────────

    async def reflect(
        self,
        *,
        action: str,
        x: int,
        y: int,
        query: str,
        verify_result: Any,          # VerifyResult từ ClickVerifier
        screenshot: bytes | None = None,
    ) -> ReflectionResult:
        """
        Phân tích lý do verify failed và đề xuất next action.

        Args:
            action:        Tên action đã thực hiện (vd: "click")
            x, y:          Tọa độ đã click
            query:         Mô tả element (vd: "Facebook post button")
            verify_result: VerifyResult object từ ClickVerifier
            screenshot:    Screenshot sau click (None → tự chụp)

        Returns:
            ReflectionResult với next action và lý do
        """
        t0 = time.time()

        # Extract verify channels
        channels = getattr(verify_result, "channels", [])
        verify_summary = self._summarize_verify(verify_result, channels)

        _vlog("🪞", f"Reflecting on failed '{query[:40]}' at ({x},{y})")

        # ── Tier 1: Fast rule check ───────────────────────────────
        rule_result = _fast_rule_check(channels, query)
        if rule_result is not None:
            rule_result.latency_ms = int((time.time()-t0)*1000)
            rule_result.log()
            return rule_result

        # ── Tier 2: VLM diagnosis ─────────────────────────────────
        if self._vlm:
            result = await self._vlm_reflect(
                action=action, x=x, y=y, query=query,
                verify_summary=verify_summary,
                screenshot=screenshot,
                t0=t0,
            )
            result.log()
            return result

        # ── Tier 3: Fallback ──────────────────────────────────────
        _vlog("⚪", "Reflection fallback (no VLM) → RETRY_WAIT 2s")
        return ReflectionResult(
            success=False,
            next=NextAction.RETRY_WAIT,
            reason="Không xác định được lỗi — chờ 2s rồi retry",
            wait_s=2.0,
            confidence=0.3,
            latency_ms=int((time.time()-t0)*1000),
            tier="fallback",
        )

    # ── VLM diagnosis ─────────────────────────────────────────────

    async def _vlm_reflect(
        self,
        action: str, x: int, y: int, query: str,
        verify_summary: str,
        screenshot: bytes | None,
        t0: float,
    ) -> ReflectionResult:
        """Gọi VLM với hoặc không có screenshot."""

        # Chụp screenshot nếu cần và chưa có
        img = screenshot
        if self._use_vision and img is None and self._screen:
            try:
                img = await asyncio.wait_for(self._screen(), timeout=5.0)
            except Exception:
                img = None

        # Build prompt
        prompt_vars = dict(
            action=action, x=x, y=y, query=query,
            verify_summary=verify_summary,
        )

        if img and self._use_vision:
            prompt = _REFLECTION_PROMPT.format(**prompt_vars)
        else:
            prompt = _REFLECTION_PROMPT_NOIMAGE.format(**prompt_vars)
            img = None   # không gửi ảnh

        try:
            if img:
                raw, tier = await asyncio.wait_for(
                    self._vlm(img, prompt), timeout=20.0
                )
            else:
                # Text-only: gọi Ollama qua endpoint chat thường
                raw, tier = await asyncio.wait_for(
                    self._vlm_text(prompt), timeout=15.0
                )

            ms = int((time.time()-t0)*1000)
            result = self._parse_vlm_response(raw, ms, tier)
            _vlog("🪞", f"[{tier}] Reflection: next={result.next.value} "
                  f"reason='{result.reason[:50]}' {ms}ms")
            return result

        except asyncio.TimeoutError:
            ms = int((time.time()-t0)*1000)
            _vlog("⏱️", f"Reflection VLM timeout ({ms}ms) → RETRY_WAIT 3s")
            return ReflectionResult(
                success=False,
                next=NextAction.RETRY_WAIT,
                reason="VLM timeout khi phân tích — chờ 3s",
                wait_s=3.0,
                confidence=0.4,
                latency_ms=ms,
                tier="timeout_fallback",
            )
        except Exception as exc:
            ms = int((time.time()-t0)*1000)
            _log.debug("Reflection VLM error: %s", exc)
            return ReflectionResult(
                success=False,
                next=NextAction.RETRY_SAME,
                reason=f"VLM error: {str(exc)[:60]}",
                wait_s=1.0,
                confidence=0.3,
                latency_ms=ms,
                tier="error_fallback",
            )

    async def _vlm_text(self, prompt: str) -> tuple[str, str]:
        """
        Gọi Ollama text-only (không có ảnh) cho reflection không cần vision.
        Dùng qwen3:8b thay vì qwen3-vl — nhanh hơn vì không process ảnh.
        """
        import urllib.request as _ur
        payload = json.dumps({
            "model": _resolve_model("qwen3:8b"),
            "think": False,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.1, "num_predict": 200},
        }).encode()
        req = _ur.Request(
            "http://127.0.0.1:11434/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        def _call():
            with _ur.urlopen(req, timeout=12) as r:
                return json.loads(r.read().decode())
        result = await asyncio.wait_for(asyncio.to_thread(_call), timeout=14.0)
        text = result.get("message", {}).get("content", "").strip()
        return text, "ollama_text"

    # ── Parsing ───────────────────────────────────────────────────

    def _parse_vlm_response(
        self, raw: str, latency_ms: int, tier: str
    ) -> ReflectionResult:
        """Parse JSON response từ VLM reflection."""
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()

        d: dict = {}
        try:
            d = json.loads(cleaned)
        except Exception:
            m = re.search(r"\{[^{}]+\}", cleaned, re.DOTALL)
            if m:
                try:
                    d = json.loads(m.group())
                except Exception:
                    pass

        if not d:
            return ReflectionResult(
                success=False,
                next=NextAction.RETRY_SAME,
                reason="VLM không trả JSON hợp lệ",
                wait_s=1.5,
                confidence=0.3,
                latency_ms=latency_ms,
                tier=tier,
            )

        next_action = NextAction.from_str(d.get("next", "retry_same"))
        wait_s = min(float(d.get("wait_s", 0.0)), self._max_wait)
        confidence = float(d.get("confidence", 0.5))

        # Safety: nếu confidence < 0.5 → downgrade to RETRY_SAME
        if confidence < 0.5 and next_action == NextAction.ESCALATE:
            next_action = NextAction.RETRY_DIFFERENT
            d["detail"] = "(confidence thấp → không escalate ngay) " + d.get("detail", "")

        return ReflectionResult(
            success=bool(d.get("success", False)),
            next=next_action,
            reason=str(d.get("reason", ""))[:120],
            detail=str(d.get("detail", ""))[:200],
            wait_s=wait_s,
            scroll_dir=str(d.get("scroll_dir", "down")),
            scroll_px=300,
            confidence=confidence,
            latency_ms=latency_ms,
            tier=tier,
        )

    @staticmethod
    def _summarize_verify(verify_result: Any, channels: list) -> str:
        """
        Tóm tắt VerifyResult thành string ngắn gọn cho VLM prompt.
        Không cần import VerifyResult — dùng getattr safe.
        """
        ok      = getattr(verify_result, "ok", False)
        conf    = getattr(verify_result, "confidence", 0.0)
        method  = getattr(verify_result, "method", "unknown")
        total_ms= getattr(verify_result, "latency_ms", 0)

        ch_parts = []
        for ch in channels:
            ch_ok   = "✅" if getattr(ch, "ok", False) else "❌"
            ch_name = getattr(ch, "channel", "?")
            ch_det  = getattr(ch, "detail", "")[:40]
            ch_ms   = getattr(ch, "latency_ms", 0)
            ch_parts.append(f"{ch_name}={ch_ok}({ch_det},{ch_ms}ms)")

        channels_str = " | ".join(ch_parts) if ch_parts else "no channels"

        return (
            f"overall={'PASS' if ok else 'FAIL'} conf={conf:.0%} "
            f"method={method} [{channels_str}] total={total_ms}ms"
        )
