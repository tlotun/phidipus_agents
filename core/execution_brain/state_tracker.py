# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/execution_brain/state_tracker.py — Phidipus v1.37
═══════════════════════════════════════════════════════════════════════

P0.2 — StateTracker: Task-Level State Machine

Theo dõi trạng thái toàn bộ task — không phải từng click hay từng step,
mà là big picture: task đang tiến triển đúng hướng không?

States:
  INIT        → task mới bắt đầu
  RUNNING     → đang chạy bình thường, có tiến triển
  STUCK       → N steps liên tiếp fail, không advance
  REGRESSION  → quay về state cũ (loop lớn)
  RECOVERING  → đang xử lý stuck/regression
  DONE        → thành công
  FAILED      → không thể tiếp tục

Detect mechanisms:
  1. STUCK: N (default 3) steps liên tiếp không success
  2. REGRESSION: state_hash của bước hiện tại trùng với bước xa trước đó
  3. PROGRESS STALL: không advance step_index sau X giây

StateTracker không can thiệp — chỉ báo cáo.
RetryEngine nhận signal và quyết định làm gì.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Task state enum
# ══════════════════════════════════════════════════════════════════

class TaskState(Enum):
    INIT        = auto()
    RUNNING     = auto()
    STUCK       = auto()
    REGRESSION  = auto()
    RECOVERING  = auto()
    DONE        = auto()
    FAILED      = auto()


@dataclass
class StepRecord:
    """Record cho 1 bước đã thực hiện."""
    step_name:    str
    success:      bool
    state_hash:   str   = ""   # hash màn hình / state sau step
    duration_ms:  int   = 0
    error:        str   = ""
    timestamp:    float = field(default_factory=time.time)
    validation:   Any   = None  # ValidationResult nếu có


# ══════════════════════════════════════════════════════════════════
# StateTracker
# ══════════════════════════════════════════════════════════════════

class StateTracker:
    """
    Task-level state machine.

    Gọi record_step() sau mỗi bước.
    Gọi get_state() để biết task đang ở trạng thái nào.
    Gọi get_progress_report() để log tóm tắt.
    """

    # Config thresholds
    STUCK_THRESHOLD      = 3    # N consecutive fail steps → STUCK
    REGRESSION_WINDOW    = 8    # Kiểm tra N steps gần nhất để detect regression
    STALL_TIMEOUT_S      = 120  # Không advance sau X giây → warn

    def __init__(self,
                 stuck_threshold: int = 3,
                 regression_window: int = 8) -> None:
        self._history:          list[StepRecord] = []
        self._state:            TaskState        = TaskState.INIT
        self._task_start:       float            = time.time()
        self._last_success_ts:  float            = time.time()
        self._last_success_step:str              = ""
        self._last_advance_step:str              = ""
        self._state_change_log: list[tuple]      = []  # [(ts, old, new)]

        self.stuck_threshold    = stuck_threshold
        self.regression_window  = regression_window

        # Stats
        self._total_steps:    int = 0
        self._success_steps:  int = 0
        self._fail_steps:     int = 0

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def history(self) -> list[StepRecord]:
        return self._history.copy()

    @property
    def elapsed_s(self) -> float:
        return time.time() - self._task_start

    def record_step(
        self,
        step_name: str,
        success: bool,
        state_hash: str = "",
        duration_ms: int = 0,
        error: str = "",
        validation: Any = None,
    ) -> TaskState:
        """
        Ghi nhận 1 step và trả về TaskState mới.

        Args:
            step_name:  Tên step (vd 'B6_fb_composer_open')
            success:    Step có thành công không
            state_hash: Hash của screenshot/state sau step (để detect regression)
            duration_ms: Thời gian thực hiện step
            error:      Error message nếu fail
            validation: ValidationResult từ StepValidator nếu có

        Returns:
            TaskState hiện tại sau khi update
        """
        record = StepRecord(
            step_name=step_name,
            success=success,
            state_hash=state_hash or "",
            duration_ms=duration_ms,
            error=error,
            validation=validation,
        )
        self._history.append(record)
        self._total_steps += 1

        if success:
            self._success_steps += 1
            self._last_success_ts   = time.time()
            self._last_success_step = step_name
            self._last_advance_step = step_name
        else:
            self._fail_steps += 1

        # ── Detect STUCK ──────────────────────────────────────────
        recent = self._history[-self.stuck_threshold:]
        if (len(recent) >= self.stuck_threshold
                and all(not r.success for r in recent)):
            self._transition(TaskState.STUCK)
            _vlog("🔴", f"StateTracker STUCK: {self.stuck_threshold} steps fail liên tiếp "
                  f"(last: {step_name})")
            return self._state

        # ── Detect REGRESSION ─────────────────────────────────────
        if state_hash and len(self._history) > self.regression_window:
            # Kiểm tra xem state_hash đã xuất hiện trong các steps xa trước
            old_hashes = [r.state_hash for r in
                          self._history[-(self.regression_window + 1):-3]
                          if r.state_hash]
            if state_hash in old_hashes:
                self._transition(TaskState.REGRESSION)
                _vlog("🔄", f"StateTracker REGRESSION: state_hash lặp lại "
                      f"(step: {step_name})")
                return self._state

        # ── Normal running ────────────────────────────────────────
        if success or self._state == TaskState.RECOVERING:
            self._transition(TaskState.RUNNING)

        return self._state

    def mark_recovering(self) -> None:
        """Gọi khi RetryEngine đang xử lý stuck/regression."""
        self._transition(TaskState.RECOVERING)

    def mark_done(self) -> None:
        """Gọi khi task hoàn thành thành công."""
        self._transition(TaskState.DONE)

    def mark_failed(self) -> None:
        """Gọi khi task không thể tiếp tục."""
        self._transition(TaskState.FAILED)

    def get_progress_report(self) -> str:
        """Tóm tắt ngắn gọn để log."""
        elapsed = int(self.elapsed_s)
        rate = f"{self._success_steps}/{self._total_steps}"
        stall = int(time.time() - self._last_success_ts)
        return (
            f"State={self._state.name} | "
            f"Steps={rate} | "
            f"Elapsed={elapsed}s | "
            f"SinceLastOK={stall}s | "
            f"LastOK={self._last_success_step or 'none'}"
        )

    def is_stalling(self) -> bool:
        """True nếu không có success step trong quá lâu."""
        return (self._total_steps > 0
                and time.time() - self._last_success_ts > self.STALL_TIMEOUT_S)

    def consecutive_failures(self) -> int:
        """Số lần fail liên tiếp gần nhất."""
        count = 0
        for r in reversed(self._history):
            if not r.success:
                count += 1
            else:
                break
        return count

    def success_rate(self) -> float:
        """Tỷ lệ thành công của task hiện tại."""
        if self._total_steps == 0:
            return 0.0
        return self._success_steps / self._total_steps

    def to_dict(self) -> dict:
        """Export cho logging/debugging."""
        return {
            "state":              self._state.name,
            "total_steps":        self._total_steps,
            "success_steps":      self._success_steps,
            "fail_steps":         self._fail_steps,
            "success_rate":       round(self.success_rate(), 2),
            "elapsed_s":          round(self.elapsed_s, 1),
            "last_success_step":  self._last_success_step,
            "consecutive_fails":  self.consecutive_failures(),
            "stalling":           self.is_stalling(),
        }

    # ── Internal ──────────────────────────────────────────────────

    def _transition(self, new_state: TaskState) -> None:
        if new_state != self._state:
            self._state_change_log.append((time.time(), self._state, new_state))
            _log.debug("StateTracker: %s → %s", self._state.name, new_state.name)
            self._state = new_state


# ══════════════════════════════════════════════════════════════════
# Hash helper for state comparison
# ══════════════════════════════════════════════════════════════════

def compute_state_hash(screenshot_bytes: bytes | None,
                       step_name: str = "") -> str:
    """
    Tính hash ngắn để detect regression.

    Dùng MD5 của screenshot (hoặc step_name nếu không có screenshot).
    16 chars hex — đủ để phát hiện duplicate states.
    """
    if screenshot_bytes and len(screenshot_bytes) > 100:
        # Downsample: chỉ hash 4KB đầu để nhanh
        return hashlib.md5(screenshot_bytes[:4096]).hexdigest()[:16]
    if step_name:
        return hashlib.md5(step_name.encode()).hexdigest()[:16]
    return ""
