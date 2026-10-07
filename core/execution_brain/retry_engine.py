# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/execution_brain/retry_engine.py — Phidipus v1.37
═══════════════════════════════════════════════════════════════════════

P0.4 — RetryEngine: Không bao giờ lặp lại cách fail cũ

"Insanity: doing the same thing over and over and expecting different results."

Vấn đề hiện tại: khi step fail, bot retry y chang (same click, same selector).
Sau N lần vẫn fail, timeout, không có kết quả.

Giải pháp: RetryEngine chọn strategy dựa trên:
  1. fail_count: đã thử bao nhiêu lần
  2. fail_reasons: lý do fail cụ thể (từ ValidationResult)
  3. step_context: bước này thường fail vì gì (từ lịch sử)
  4. task_state: TaskState hiện tại

5 strategies:
  WAIT:        chờ N giây để page load / animation xong
  SCROLL:      scroll màn hình → element có thể bị che
  ALTERNATIVE: dùng VLM tìm element khác / selector khác
  ZOOM:        force ZoomActionEngine (3-pass precision)
  ESCALATE:    dừng → báo Telegram → chờ user

RetryEngine chỉ quyết định strategy — KHÔNG thực hiện.
ExecutionBrain nhận strategy và execute.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;34m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# RetryStrategy enum
# ══════════════════════════════════════════════════════════════════

class RetryStrategy(Enum):
    SAME        = auto()  # retry y chang (hiếm khi dùng)
    WAIT        = auto()  # chờ N giây rồi retry
    SCROLL      = auto()  # scroll màn hình để unhide element
    ALTERNATIVE = auto()  # VLM tìm element/selector khác
    ZOOM        = auto()  # force ZoomActionEngine 3-pass
    ESCALATE    = auto()  # dừng, báo Telegram


@dataclass
class RetryDecision:
    """Quyết định retry từ RetryEngine."""
    strategy:     RetryStrategy
    reason:       str         = ""
    wait_s:       float       = 0.0   # Cho WAIT strategy
    scroll_px:    int         = 300   # Cho SCROLL strategy
    scroll_dir:   str         = "down"
    escalate_msg: str         = ""    # Cho ESCALATE
    reset_coords: bool        = True  # Reset x,y=0 sau retry (tìm lại)

    @property
    def icon(self) -> str:
        icons = {
            RetryStrategy.SAME:        "🔄",
            RetryStrategy.WAIT:        "⏳",
            RetryStrategy.SCROLL:      "📜",
            RetryStrategy.ALTERNATIVE: "🔍",
            RetryStrategy.ZOOM:        "🔬",
            RetryStrategy.ESCALATE:    "🚨",
        }
        return icons.get(self.strategy, "🔄")


# ══════════════════════════════════════════════════════════════════
# RetryEngine
# ══════════════════════════════════════════════════════════════════

class RetryEngine:
    """
    Chọn retry strategy thông minh — không bao giờ lặp lại cách fail cũ.

    Decision logic (rule-based, < 5ms, không cần LLM):
      fail_count=1:
        - "loading" / "spinner" in reasons → WAIT 3s
        - "not_visible" / "hidden" → SCROLL
        - default → WAIT 2s
      fail_count=2:
        - "timeout" → ZOOM
        - "wrong_element" / "coord" → ALTERNATIVE
        - default → SCROLL + WAIT
      fail_count=3:
        - → ZOOM (last resort before escalate)
      fail_count>=4:
        - → ESCALATE
    """

    MAX_RETRIES_BEFORE_ESCALATE = 4

    def choose(
        self,
        step_name: str,
        fail_count: int,
        fail_reasons: list[str],
        task_state_name: str = "RUNNING",
        validation_result: Any = None,
    ) -> RetryDecision:
        """
        Chọn retry strategy dựa trên context.

        Args:
            step_name:         Tên step đang fail
            fail_count:        Số lần đã fail liên tiếp
            fail_reasons:      List các criterion đã fail
            task_state_name:   TaskState.name hiện tại
            validation_result: ValidationResult từ StepValidator

        Returns:
            RetryDecision với strategy cụ thể
        """
        reasons_str = " ".join(fail_reasons).lower()

        # ── Immediate ESCALATE ────────────────────────────────
        if fail_count >= self.MAX_RETRIES_BEFORE_ESCALATE:
            return RetryDecision(
                strategy=RetryStrategy.ESCALATE,
                reason=f"Đã thử {fail_count} lần, vẫn fail",
                escalate_msg=(
                    f"⚠️ *Phidipus cần hỗ trợ*\n\n"
                    f"Bước: `{step_name}`\n"
                    f"Số lần thử: {fail_count}\n"
                    f"Lý do fail: {', '.join(fail_reasons[:3])}\n\n"
                    f"Dùng /skip để bỏ qua bước này hoặc /abort để dừng."
                ),
            )

        # ── STUCK state → aggressive recovery ────────────────
        if task_state_name == "STUCK":
            return RetryDecision(
                strategy=RetryStrategy.ALTERNATIVE,
                reason="Task STUCK — thử element/selector hoàn toàn khác",
                reset_coords=True,
            )

        # ── REGRESSION state → scroll reset ──────────────────
        if task_state_name == "REGRESSION":
            return RetryDecision(
                strategy=RetryStrategy.SCROLL,
                reason="Task REGRESSION — scroll lên đầu trang",
                scroll_dir="up",
                scroll_px=1000,
                reset_coords=True,
            )

        # ── fail_count = 1: nhẹ nhàng ─────────────────────────
        if fail_count == 1:
            if any(kw in reasons_str for kw in ("loading", "spinner", "not_ready",
                                                  "still_loading", "wait")):
                return RetryDecision(
                    strategy=RetryStrategy.WAIT,
                    reason="Page có vẻ đang loading, chờ thêm",
                    wait_s=3.0,
                    reset_coords=False,
                )
            if any(kw in reasons_str for kw in ("not_visible", "hidden",
                                                  "below_fold", "scroll")):
                return RetryDecision(
                    strategy=RetryStrategy.SCROLL,
                    reason="Element có thể bị che, scroll xuống",
                    scroll_px=300,
                    reset_coords=False,
                )
            # Default lần 1: chờ ngắn
            return RetryDecision(
                strategy=RetryStrategy.WAIT,
                reason="Lần đầu fail, chờ page settle",
                wait_s=2.0,
                reset_coords=False,
            )

        # ── fail_count = 2: tăng cường ─────────────────────────
        if fail_count == 2:
            if any(kw in reasons_str for kw in ("timeout", "slow", "latency")):
                return RetryDecision(
                    strategy=RetryStrategy.ZOOM,
                    reason="Timeout → dùng ZoomAction 3-pass để tìm chính xác hơn",
                    reset_coords=True,
                )
            if any(kw in reasons_str for kw in ("wrong", "coord", "position",
                                                  "lệch", "alternative")):
                return RetryDecision(
                    strategy=RetryStrategy.ALTERNATIVE,
                    reason="Tọa độ/element sai → tìm element khác",
                    reset_coords=True,
                )
            # Default lần 2: scroll + wait
            return RetryDecision(
                strategy=RetryStrategy.SCROLL,
                reason="Lần 2 fail, scroll + reset",
                scroll_px=200,
                reset_coords=True,
            )

        # ── fail_count = 3: last resort trước escalate ─────────
        if fail_count == 3:
            return RetryDecision(
                strategy=RetryStrategy.ZOOM,
                reason="Lần 3 fail — ZoomAction precision mode",
                reset_coords=True,
            )

        # Không bao giờ reach đây (đã catch ở fail_count >= MAX trên)
        return RetryDecision(
            strategy=RetryStrategy.ESCALATE,
            reason="Unexpected retry count",
            escalate_msg=f"Unexpected fail_count={fail_count} cho step {step_name}",
        )

    async def execute(
        self,
        decision: RetryDecision,
        ipc_client: Any = None,
        notify_fn: Any = None,
    ) -> bool:
        """
        Thực hiện retry action dựa trên decision.

        Returns:
            True nếu action được thực hiện,
            False nếu escalate (cần human intervention)
        """
        _vlog(decision.icon, f"RetryEngine [{decision.strategy.name}]: {decision.reason}")

        if decision.strategy == RetryStrategy.WAIT:
            await asyncio.sleep(decision.wait_s)
            return True

        if decision.strategy == RetryStrategy.SCROLL:
            if ipc_client:
                try:
                    await ipc_client.send_action("mouse_scroll", {
                        "direction": decision.scroll_dir,
                        "amount": decision.scroll_px,
                    })
                    await asyncio.sleep(0.5)  # wait for scroll animation
                except Exception as exc:
                    _log.debug("RetryEngine scroll error: %s", exc)
            return True

        if decision.strategy in (RetryStrategy.ALTERNATIVE, RetryStrategy.ZOOM):
            # ExecutionBrain sẽ handle bằng cách reset coords và gọi lại find_and_click_v2
            # với mode khác. Không cần làm gì ở đây ngoài return True.
            await asyncio.sleep(0.5)
            return True

        if decision.strategy == RetryStrategy.ESCALATE:
            if notify_fn and decision.escalate_msg:
                try:
                    await notify_fn(decision.escalate_msg)
                except Exception:
                    pass
            return False  # Signal: cần human

        # SAME: không làm gì
        return True
