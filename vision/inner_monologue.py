# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/inner_monologue.py — Phidipus v1.33
═══════════════════════════════════════════════════════════════════════

Lớp Tư Duy Nội Tại (Inner Monologue & Self-Reflect)

2 thành phần:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A. MonologueStore — Kho lưu trữ tư duy của agent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Nhận monologue từ react_reasoner → lưu per-task → export cho:
    - Trajectory Memory (học pattern reasoning)
    - Admin Panel (hiển thị "tiếng nói nội tâm")
    - Debug log

  Mỗi MonologueEntry ghi lại:
    step_n, action, monologue text, confidence, is_critical, timestamp

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
B. @critical_guard decorator — "Điểm chốt chặn an toàn"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Áp dụng lên các method KHÔNG THỂ HOÀN TÁC:
    - click_post_button() → đăng bài lên Facebook/IG/X
    - click_send_email() → gửi email
    - confirm_delete() → xóa file

  Trước khi thực hiện:
    1. Chụp screenshot
    2. Gọi VLM kiểm tra "Màn hình có sẵn sàng chưa?"
    3. Nếu SAFE → proceed
    4. Nếu UNSAFE:
       - on_unsafe="wait"     → chờ N giây, retry 1 lần
       - on_unsafe="escalate" → gửi Telegram + return failed dict
       - on_unsafe="skip"     → proceed anyway (non-critical)

  Design decisions:
    - Timeout 20s cho VLM call (không block vô hạn)
    - Nếu VLM timeout/error → proceed optimistically (không block workflow)
    - Max 1 retry sau wait (không vòng lặp vô tận)
    - Graceful fallback: không raise Exception, trả về dict

  Tích hợp với ReflectionSupervisor:
    critical_guard chạy TRƯỚC action (pre-check)
    ReflectionSupervisor chạy SAU action (post-check nếu verify fail)
    → Hai lớp bảo vệ độc lập

Dùng:
    from vision.inner_monologue import critical_guard, MonologueStore

    # Decorator:
    @critical_guard("Click nút Đăng Facebook", on_unsafe="escalate")
    async def click_post_button(self):
        return await self.find_and_click_v2(...)

    # Store:
    store = MonologueStore()
    store.record(step_n=3, action="mouse_click", monologue="...", confidence=0.92)
    store.export_for_trajectory()  # → dict để TrajectoryDB lưu
"""
from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


def _vlog_mono(step_n: int, text: str) -> None:
    """Log monologue với màu dim — tiếng nói nội tâm của agent."""
    # \033[2;37m = dim + light gray
    print(f"\033[2;37m🧠 [{step_n}] {text[:300]}\033[0m")


# ══════════════════════════════════════════════════════════════════
# MonologueEntry & MonologueStore
# ══════════════════════════════════════════════════════════════════

@dataclass
class MonologueEntry:
    """Một tư duy nội tâm tại 1 bước."""
    step_n:      int
    action:      str
    monologue:   str
    confidence:  float = 1.0
    is_critical: bool  = False
    ts:          str   = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "step_n":      self.step_n,
            "action":      self.action,
            "monologue":   self.monologue[:300],
            "confidence":  round(self.confidence, 2),
            "is_critical": self.is_critical,
            "ts":          self.ts,
        }


class MonologueStore:
    """
    Lưu trữ tư duy nội tâm per-task.

    Nhận entries từ react_reasoner sau mỗi step.
    Export cho Trajectory Memory và Admin Panel.

    Usage trong agent_loop.py:
        self._monologue_store = MonologueStore(task_id=task_id)
        # Sau mỗi react step:
        self._monologue_store.record(
            step_n=result.step_n,
            action=result.action_name,
            monologue=result.monologue,
            confidence=result.confidence,
            is_critical=result.is_critical,
        )
        # Khi task kết thúc:
        trajectory_data = self._monologue_store.export_for_trajectory()
    """

    def __init__(self, task_id: str = "", max_entries: int = 50) -> None:
        self._task_id   = task_id
        self._max       = max_entries
        self._entries:  list[MonologueEntry] = []
        self._task_ts   = datetime.now(timezone.utc).isoformat()

    def record(
        self,
        step_n: int,
        action: str,
        monologue: str,
        confidence: float = 1.0,
        is_critical: bool = False,
    ) -> None:
        """Ghi nhận 1 monologue entry."""
        if not monologue:
            return

        entry = MonologueEntry(
            step_n=step_n,
            action=action,
            monologue=monologue,
            confidence=confidence,
            is_critical=is_critical,
        )
        self._entries.append(entry)

        # Evict nếu quá max
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]

        # Log màu dim
        _vlog_mono(step_n, monologue)

        # Extra warning nếu critical step thiếu monologue
        if is_critical and len(monologue) < 20:
            _vlog("⚠️ ", f"Critical step {action} có monologue ngắn — "
                  f"LLM chưa reasoning đủ sâu ({len(monologue)} chars)")

    def get_critical_entries(self) -> list[MonologueEntry]:
        """Lấy tất cả entries là critical action."""
        return [e for e in self._entries if e.is_critical]

    def export_for_trajectory(self) -> dict:
        """
        Export dưới dạng dict để TrajectoryDB.record() lưu vào trajectory.
        Thêm field 'reasoning_chain' vào trajectory step data.
        """
        return {
            "task_id":        self._task_id,
            "task_ts":        self._task_ts,
            "total_steps":    len(self._entries),
            "critical_steps": len(self.get_critical_entries()),
            "avg_confidence": (
                sum(e.confidence for e in self._entries) / len(self._entries)
                if self._entries else 0.0
            ),
            "reasoning_chain": [e.to_dict() for e in self._entries],
        }

    def clear(self) -> None:
        """Reset sau khi task kết thúc."""
        self._entries.clear()

    def summary(self) -> str:
        """Tóm tắt ngắn gọn để log."""
        if not self._entries:
            return "no monologue"
        critical = self.get_critical_entries()
        return (
            f"{len(self._entries)} thoughts, "
            f"{len(critical)} critical, "
            f"avg_conf={sum(e.confidence for e in self._entries)/len(self._entries):.0%}"
        )


# ══════════════════════════════════════════════════════════════════
# VLM Guard prompt builder
# ══════════════════════════════════════════════════════════════════

def _build_guard_prompt(context_msg: str) -> str:
    """
    Build prompt để VLM kiểm tra "Màn hình có sẵn sàng thực hiện action không?"

    Hỏi 5 điều cụ thể thay vì câu hỏi mơ hồ.
    VLM phải trả JSON — không prose.
    """
    return f"""\
You are a safety checker before an irreversible UI action.

About to perform: {context_msg}

Inspect the screenshot carefully and answer:
1. Is any popup, modal, or overlay BLOCKING the target element?
2. Is the UI still loading (spinner, skeleton, progress bar visible)?
3. Is there any ERROR message on screen?
4. Does the screen state MATCH what's expected before this action?
5. Is the target button/element ENABLED and VISIBLE?

Respond ONLY with JSON (no markdown):
{{
  "safe": <true if ALL checks pass — safe to proceed>,
  "blocking_popup": <true if popup detected>,
  "still_loading":  <true if loading indicator visible>,
  "error_visible":  <true if error message on screen>,
  "target_visible": <true if target element is visible and enabled>,
  "reason":         "<1 sentence: why safe or why not safe>",
  "confidence":     <0.0-1.0 how certain you are>
}}"""


def _parse_guard_result(raw: str) -> dict:
    """Parse VLM JSON response, robust với markdown noise."""
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
    # Fallback: heuristic từ text
    raw_lower = raw.lower()
    safe = (
        "safe" in raw_lower
        and "not safe" not in raw_lower
        and "unsafe" not in raw_lower
        and "false" not in raw_lower
    )
    return {
        "safe": safe,
        "reason": raw[:100],
        "confidence": 0.5,
        "blocking_popup": False,
        "still_loading": False,
        "error_visible": False,
        "target_visible": True,
    }


# ══════════════════════════════════════════════════════════════════
# @critical_guard decorator
# ══════════════════════════════════════════════════════════════════

def critical_guard(
    context_msg: str,
    *,
    on_unsafe: str   = "wait",     # "wait" | "escalate" | "skip"
    wait_s: float    = 3.0,        # giây chờ nếu on_unsafe="wait"
    timeout_s: float = 20.0,       # timeout VLM call
    min_confidence: float = 0.60,  # ignore guard nếu VLM conf < 0.60
    log_always: bool = True,       # luôn log dù safe hay không
) -> Callable:
    """
    Decorator: kiểm tra màn hình an toàn trước khi thực hiện action không thể undo.

    Args:
        context_msg:    Mô tả action sắp làm (cho VLM hiểu context)
        on_unsafe:      Hành động nếu VLM nói "not safe":
                          "wait"     → chờ wait_s giây, retry 1 lần, rồi proceed
                          "escalate" → trả về failed dict, KHÔNG proceed
                          "skip"     → bỏ qua guard, proceed luôn
        wait_s:         Giây chờ nếu on_unsafe="wait" (default 3s)
        timeout_s:      Timeout cho VLM call (default 20s)
        min_confidence: Nếu VLM confidence < min_confidence → ignore guard
        log_always:     Luôn log kết quả dù safe=True

    Yêu cầu: self phải có:
        - self._vlm (VLMRouter)
        - self.screenshot() async method
        - self._tg_send (optional, cho escalate notification)

    Ví dụ:
        @critical_guard("Click nút Đăng Facebook", on_unsafe="escalate")
        async def click_post_button(self):
            ...

        @critical_guard("Submit form", on_unsafe="wait", wait_s=5.0)
        async def submit_form(self):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs) -> Any:
            t0 = time.time()

            # ── Bước 1: Chụp screenshot ────────────────────────────
            screenshot = None
            try:
                screenshot = await asyncio.wait_for(
                    self.screenshot(), timeout=5.0
                )
            except Exception as exc:
                _vlog("⚪", f"critical_guard: screenshot failed ({exc}) → proceed")
                return await func(self, *args, **kwargs)

            if not screenshot:
                _vlog("⚪", f"critical_guard: no screenshot → proceed")
                return await func(self, *args, **kwargs)

            # ── Bước 2: Gọi VLM kiểm tra ──────────────────────────
            guard_result: dict = {}
            try:
                prompt = _build_guard_prompt(context_msg)
                raw, tier = await asyncio.wait_for(
                    self._vlm.call(screenshot, prompt),
                    timeout=timeout_s,
                )
                guard_result = _parse_guard_result(raw)
                elapsed = int((time.time() - t0) * 1000)

                vlm_conf = float(guard_result.get("confidence", 0.5))
                is_safe  = bool(guard_result.get("safe", True))
                reason   = str(guard_result.get("reason", ""))[:100]

                if log_always or not is_safe:
                    icon = "✅" if is_safe else "⚠️ "
                    _vlog(icon, f"critical_guard [{tier}]: "
                          f"safe={is_safe} conf={vlm_conf:.0%} "
                          f"reason='{reason}' ({elapsed}ms)")

                    # Log chi tiết các flags nếu unsafe
                    if not is_safe:
                        flags = {
                            "popup":   guard_result.get("blocking_popup", False),
                            "loading": guard_result.get("still_loading", False),
                            "error":   guard_result.get("error_visible", False),
                            "target":  guard_result.get("target_visible", True),
                        }
                        active_flags = [k for k, v in flags.items() if v]
                        if active_flags:
                            _vlog("🔍", f"  Flags: {active_flags}")

                # Ignore guard nếu VLM không tự tin
                if vlm_conf < min_confidence:
                    _vlog("⚪", f"critical_guard: VLM conf={vlm_conf:.0%} < "
                          f"{min_confidence:.0%} threshold → proceed anyway")
                    return await func(self, *args, **kwargs)

                # ── Bước 3: Xử lý kết quả ─────────────────────────
                if is_safe:
                    # Safe → proceed bình thường
                    return await func(self, *args, **kwargs)

                # NOT SAFE — xử lý theo on_unsafe mode
                if on_unsafe == "skip":
                    _vlog("⚪", f"critical_guard on_unsafe=skip → proceed anyway")
                    return await func(self, *args, **kwargs)

                elif on_unsafe == "wait":
                    # Chờ rồi retry 1 lần
                    _vlog("⏳", f"critical_guard: WAIT {wait_s:.0f}s — '{reason}'")
                    await asyncio.sleep(wait_s)

                    # Retry screenshot + VLM
                    try:
                        screenshot2 = await asyncio.wait_for(
                            self.screenshot(), timeout=5.0
                        )
                        raw2, _ = await asyncio.wait_for(
                            self._vlm.call(screenshot2, prompt),
                            timeout=timeout_s,
                        )
                        guard2 = _parse_guard_result(raw2)
                        if guard2.get("safe", True):
                            _vlog("✅", f"critical_guard: retry OK after {wait_s:.0f}s wait")
                            return await func(self, *args, **kwargs)
                        else:
                            _vlog("⚠️ ", f"critical_guard: still unsafe after wait — proceeding anyway")
                            # Proceed anyway sau wait — không block vô tận
                            return await func(self, *args, **kwargs)
                    except Exception:
                        # Retry failed → proceed anyway
                        return await func(self, *args, **kwargs)

                elif on_unsafe == "escalate":
                    # KHÔNG thực hiện action — báo lỗi
                    _vlog("🚨", f"critical_guard ESCALATE: '{context_msg}' — {reason}")

                    # Gửi Telegram nếu có
                    tg_send = getattr(self, "_tg_send", None)
                    if tg_send:
                        try:
                            await tg_send(
                                f"⚠️ *Phidipus tự dừng lại*\n\n"
                                f"Hành động: {context_msg}\n"
                                f"Lý do: {reason}\n\n"
                                f"Các vấn đề phát hiện:\n"
                                + (f"• Popup đang che: {'✅' if guard_result.get('blocking_popup') else '❌'}\n" )
                                + (f"• Đang loading: {'✅' if guard_result.get('still_loading') else '❌'}\n")
                                + (f"• Có lỗi hiển thị: {'✅' if guard_result.get('error_visible') else '❌'}\n")
                                + f"\nDùng /retry hoặc /skip để tiếp tục."
                            )
                        except Exception:
                            pass

                    # Trả về failed dict (không raise Exception)
                    return {
                        "success":        False,
                        "reason":         f"critical_guard blocked: {reason}",
                        "escalated":      True,
                        "guard_result":   guard_result,
                        "context":        context_msg,
                    }

            except asyncio.TimeoutError:
                elapsed = int((time.time() - t0) * 1000)
                _vlog("⏱️", f"critical_guard VLM timeout ({elapsed}ms) → proceed optimistically")
                return await func(self, *args, **kwargs)

            except Exception as exc:
                _vlog("⚪", f"critical_guard error ({exc}) → proceed")
                return await func(self, *args, **kwargs)

        # Đánh dấu function là critical (để agent_loop biết)
        wrapper._is_critical_guarded = True
        wrapper._critical_context = context_msg
        wrapper._on_unsafe = on_unsafe
        return wrapper

    return decorator


# ══════════════════════════════════════════════════════════════════
# Pre-built guard configs cho các site phổ biến
# ══════════════════════════════════════════════════════════════════

class GuardPresets:
    """
    Pre-built critical_guard configs cho các action phổ biến.
    Dùng thay vì viết tay để đảm bảo consistency.
    """

    # Facebook đăng bài — escalate vì không thể undo
    FB_POST = dict(
        context_msg="Click nút Đăng (Post) để đăng bài lên Facebook",
        on_unsafe="escalate",
        wait_s=3.0,
        timeout_s=20.0,
        min_confidence=0.60,
    )

    # Instagram share — escalate
    IG_SHARE = dict(
        context_msg="Click nút Share để đăng bài lên Instagram",
        on_unsafe="escalate",
        wait_s=3.0,
        timeout_s=20.0,
        min_confidence=0.60,
    )

    # X (Twitter) post — escalate
    X_POST = dict(
        context_msg="Click nút Post để đăng tweet lên X/Twitter",
        on_unsafe="escalate",
        wait_s=3.0,
        timeout_s=20.0,
        min_confidence=0.60,
    )

    # ChatGPT send prompt — wait (có thể dừng generate)
    CHATGPT_SEND = dict(
        context_msg="Click Send để gửi prompt vào ChatGPT",
        on_unsafe="wait",
        wait_s=2.0,
        timeout_s=15.0,
        min_confidence=0.55,
    )

    # ChatGPT download image — wait
    CHATGPT_DOWNLOAD = dict(
        context_msg="Click Download/Save để lưu ảnh AI từ ChatGPT",
        on_unsafe="wait",
        wait_s=2.0,
        timeout_s=15.0,
        min_confidence=0.50,
    )
