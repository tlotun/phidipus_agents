# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/execution_brain/execution_brain.py — Phidipus v1.37
═══════════════════════════════════════════════════════════════════════

P0 — ExecutionBrain: Orchestrator của 4 components

Kiến trúc:
  User Goal
      ↓
  Semantic Router (P1)
      ↓
  Planner / Workflow
      ↓
  ⚠️ ExecutionBrain (P0) ← ĐÂY
      ↓
  Actions (IPC / VLM / SOM)

ExecutionBrain wrap quanh từng step của workflow:
  1. Trước step: chuẩn bị StepExpectation
  2. Chạy step (business logic không thay đổi)
  3. Sau step: StepValidator.validate() → ValidationResult
  4. Nếu fail: StateTracker update → RetryEngine.choose() → execute retry
  5. Cuối task: SuccessJudge.judge() → JudgeVerdict
  6. Ghi kết quả vào TrajectoryDB (với quality_score từ judge)

Usage trong workflow_spider_social_post.py:

    brain = ExecutionBrain.from_config(ipc_client, vlm, telegram_fn)
    await brain.begin_task(goal="tạo ảnh con ong đăng facebook")

    # Wrap từng bước:
    async with brain.step("B2_chatgpt_image",
                          expectation=expect_b2_image_saved(lambda: result.image_path)):
        image_path = await helper.create_and_save_image(subject)
        result.image_path = image_path

    # Cuối task:
    verdict = await brain.end_task(task_result=result, screenshot=final_screenshot)
    # verdict.achieved = True/False
    # verdict.evidence = "Post composer closed, success indicator visible"
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.execution_brain.step_validator import (
    StepExpectation, StepValidator, ValidationResult,
)
from core.execution_brain.state_tracker import (
    StateTracker, TaskState, compute_state_hash,
)
from core.execution_brain.retry_engine import RetryEngine, RetryStrategy
from core.execution_brain.success_judge import SuccessJudge, JudgeVerdict
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[1;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# StepContext — helper cho context manager
# ══════════════════════════════════════════════════════════════════

@dataclass
class StepContext:
    """Context trả về từ brain.step() context manager."""
    step_name:   str
    expectation: Optional[StepExpectation]
    brain:       Any  # ExecutionBrain

    # Set bởi business logic bên trong 'async with':
    success:     bool = True
    error:       str  = ""

    # Set bởi ExecutionBrain sau khi bước kết thúc:
    validation:  Optional[ValidationResult] = None
    task_state:  Optional[TaskState]        = None


# ══════════════════════════════════════════════════════════════════
# ExecutionBrain
# ══════════════════════════════════════════════════════════════════

class ExecutionBrain:
    """
    Orchestrator của Execution Reliability Layer.

    Tích hợp 4 components:
      P0.1 StepValidator  — verify outcome sau mỗi step
      P0.2 StateTracker   — theo dõi task-level state
      P0.3 SuccessJudge   — end-to-end task verification
      P0.4 RetryEngine    — intelligent retry strategy

    Thiết kế non-invasive: business logic KHÔNG thay đổi.
    ExecutionBrain chỉ wrap quanh các bước hiện có.
    """

    def __init__(
        self,
        ipc_client:      Any = None,
        vlm:             Any = None,
        notify_fn:       Any = None,     # async callable(msg) Telegram
        trajectory_db:   Any = None,     # TrajectoryDB để record quality
        enable_judge:    bool = True,    # có dùng SuccessJudge không
        enable_validator:bool = True,    # có dùng StepValidator không
    ) -> None:
        self._ipc      = ipc_client
        self._vlm      = vlm
        self._notify   = notify_fn
        self._traj_db  = trajectory_db

        self.enable_judge     = enable_judge
        self.enable_validator = enable_validator

        # 4 components
        self._validator  = StepValidator(ipc_client=ipc_client, vlm=vlm)
        self._tracker    = StateTracker()
        self._judge      = SuccessJudge(vlm=vlm, ipc_client=ipc_client)
        self._retry      = RetryEngine()

        # Task metadata
        self._goal:       str   = ""
        self._intent_type:str   = "default"
        self._task_start: float = 0.0

        # Step history cho reporting
        self._step_results: list[dict] = []

    @classmethod
    def from_config(
        cls,
        ipc_client: Any = None,
        vlm:        Any = None,
        notify_fn:  Any = None,
        trajectory_db: Any = None,
    ) -> "ExecutionBrain":
        """Factory method — tạo từ components đã có."""
        return cls(
            ipc_client=ipc_client,
            vlm=vlm,
            notify_fn=notify_fn,
            trajectory_db=trajectory_db,
        )

    # ── Task lifecycle ────────────────────────────────────────────

    def begin_task(self, goal: str, intent_type: str = "default") -> None:
        """Gọi đầu task để initialize state."""
        self._goal       = goal
        self._intent_type = intent_type
        self._task_start  = time.time()
        self._tracker     = StateTracker()  # fresh tracker
        self._step_results.clear()
        _vlog("🧠", f"ExecutionBrain: begin_task intent={intent_type}")

    async def end_task(
        self,
        task_result: Any,
        screenshot:  Optional[bytes] = None,
    ) -> JudgeVerdict:
        """
        Gọi cuối task — verify end-to-end và record quality.

        Returns:
            JudgeVerdict với achieved, confidence, evidence
        """
        # ── SuccessJudge ──────────────────────────────────────
        if self.enable_judge:
            verdict = await self._judge.judge(
                goal=self._goal,
                task_result=task_result,
                intent_type=self._intent_type,
                screenshot=screenshot,
            )
        else:
            # Không có judge → dựa vào task_result.success
            success = getattr(task_result, "success", True)
            verdict = JudgeVerdict(
                achieved=success,
                confidence=0.60,
                evidence="judge disabled — using task_result.success",
            )

        # Update state tracker
        if verdict.achieved:
            self._tracker.mark_done()
        else:
            self._tracker.mark_failed()

        # ── Ghi vào TrajectoryDB với quality_score ────────────
        if self._traj_db and verdict.achieved:
            try:
                quality_score = verdict.confidence
                # Không lưu task vòng vo (quá nhiều steps)
                task_dict = self._to_dict(task_result)
                steps_done = task_dict.get("steps_done", 0)
                if steps_done <= 20 and quality_score >= 0.70:
                    await asyncio.to_thread(
                        self._traj_db.record,
                        self._goal,
                        task_dict,
                        quality_score=quality_score,
                        judge_evidence=verdict.evidence,
                    )
            except Exception as exc:
                _log.debug("ExecutionBrain: TrajectoryDB record error: %s", exc)

        elapsed = int(time.time() - self._task_start)
        state = self._tracker.get_progress_report()
        _vlog("📊", f"ExecutionBrain end_task: {verdict.summary} | {state} | {elapsed}s")

        return verdict

    # ── Step context manager ──────────────────────────────────────

    @contextlib.asynccontextmanager
    async def step(
        self,
        step_name: str,
        expectation: Optional[StepExpectation] = None,
        skip_validate: bool = False,
    ):
        """
        Async context manager cho từng workflow step.

        Usage:
            async with brain.step("B6_fb_composer",
                                  expectation=expect_b6_fb_composer_open()) as ctx:
                # business logic ở đây
                await helper.click_composer()
                # nếu fail → set ctx.success = False, ctx.error = "..."

        ExecutionBrain tự động:
          - validate sau khi bước xong (nếu có expectation)
          - update StateTracker
          - quyết định retry nếu cần
        """
        t0 = time.time()
        ctx = StepContext(step_name=step_name, expectation=expectation, brain=self)

        try:
            yield ctx  # Business logic chạy ở đây
        except Exception as exc:
            ctx.success = False
            ctx.error   = str(exc)[:200]
            _log.debug("ExecutionBrain step '%s' exception: %s", step_name, exc)

        # ── Post-step validation ──────────────────────────────
        if not skip_validate and expectation and self.enable_validator and ctx.success:
            validation = await self._validator.validate(expectation)
            ctx.validation = validation

            if not validation.success:
                # Validator nói fail → update StateTracker
                state = self._tracker.record_step(
                    step_name=step_name,
                    success=False,
                    error=f"Validation failed: {validation.failed_criteria}",
                    validation=validation,
                )
                ctx.success   = False
                ctx.error     = f"Step outcome not verified: {validation.failed_criteria}"
                ctx.task_state = state

                # RetryEngine
                decision = self._retry.choose(
                    step_name=step_name,
                    fail_count=self._tracker.consecutive_failures(),
                    fail_reasons=validation.failed_criteria,
                    task_state_name=state.name,
                    validation_result=validation,
                )
                _vlog(decision.icon,
                      f"RetryEngine: {decision.strategy.name} — {decision.reason}")

                if decision.strategy != RetryStrategy.ESCALATE:
                    await self._retry.execute(decision, self._ipc, self._notify)
                else:
                    await self._retry.execute(decision, self._ipc, self._notify)
                    # Escalate: không tiếp tục

            else:
                # Validation pass
                screenshot_hash = compute_state_hash(None, step_name)
                state = self._tracker.record_step(
                    step_name=step_name,
                    success=True,
                    state_hash=screenshot_hash,
                    duration_ms=int((time.time()-t0)*1000),
                    validation=validation,
                )
                ctx.task_state = state

        elif ctx.success:
            # Không có expectation → record success theo business logic
            state = self._tracker.record_step(
                step_name=step_name,
                success=True,
                duration_ms=int((time.time()-t0)*1000),
            )
            ctx.task_state = state
        else:
            # Business logic báo fail
            state = self._tracker.record_step(
                step_name=step_name,
                success=False,
                error=ctx.error,
                duration_ms=int((time.time()-t0)*1000),
            )
            ctx.task_state = state

        # Record step summary
        self._step_results.append({
            "step":     step_name,
            "success":  ctx.success,
            "ms":       int((time.time()-t0)*1000),
            "error":    ctx.error,
            "state":    ctx.task_state.name if ctx.task_state else "?",
        })

    # ── Helpers ──────────────────────────────────────────────────

    @property
    def state(self) -> TaskState:
        return self._tracker.state

    @property
    def is_stuck(self) -> bool:
        return self._tracker.state == TaskState.STUCK

    def get_report(self) -> dict:
        """Tóm tắt toàn bộ execution để log."""
        return {
            "goal":          self._goal[:80],
            "elapsed_s":     round(time.time() - self._task_start, 1),
            "tracker":       self._tracker.to_dict(),
            "steps":         self._step_results,
        }

    def _to_dict(self, task_result: Any) -> dict:
        if isinstance(task_result, dict):
            return task_result
        if hasattr(task_result, "__dict__"):
            return task_result.__dict__
        return {}
