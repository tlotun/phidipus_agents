# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/scheduler.py — Phidipus Dependency Scheduler v9.20
═══════════════════════════════════════════════════════

Parse TaskGraph dependencies → chạy song song khi có thể.

VẤN ĐỀ với sequential execution:
    T1 → T2 → T3 → T4   (tổng ~40s nếu mỗi task ~10s)

GIẢI PHÁP với Dependency Scheduler:
    TaskGraph:
        T1: find_file    (no deps)    ─┐ chạy SONG SONG
        T2: open_chrome  (no deps)    ─┘ → tổng ~10s
        T3: read_excel   [deps: T1]   ─┐ chờ T1
        T4: navigate_url [deps: T2]   ─┘ chờ T2

    Kết quả: 20s thay vì 40s — tiết kiệm 50% thời gian.

Thuật toán: Topological Sort + Wave-based parallel execution.
    - Wave 0: các task không có dependency
    - Wave 1: các task mà tất cả deps đã xong ở Wave 0
    - Wave 2: ...
    Mỗi wave: asyncio.gather() với max_concurrency=3

Max concurrency = 3:
    - Mac Mini 64GB RAM đủ chạy 3 tasks đồng thời
    - Tránh quá tải Ollama (chỉ có 1 model loaded tại một thời điểm)
    - Gemini API rate limit: 60 req/min → 3 concurrent là an toàn

Usage:
    scheduler = TaskScheduler(agent_loop, max_concurrency=3)
    result = await scheduler.execute_plan(task_id, goal, plan)

Tích hợp với agent_loop._execute_plan() thông qua dependency detection.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


# ── Vietnamese log helper ──────────────────────────────────────────
def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class ScheduledTask:
    """A task unit ready for scheduled execution."""
    id: str
    description: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"     # pending | running | done | failed | skipped
    result: Any = None
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_ms(self) -> int:
        if self.finished_at and self.started_at:
            return int((self.finished_at - self.started_at) * 1000)
        return 0


@dataclass
class ScheduleResult:
    """Full result of a scheduled plan execution."""
    task_id: str
    goal: str
    success: bool
    total_tasks: int
    done_count: int
    failed_count: int
    skipped_count: int
    total_duration_ms: int
    waves_executed: int
    parallel_savings_ms: int    # Estimated time saved vs sequential
    step_outputs: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    providers_used: list[str] = field(default_factory=list)  # Providers used across tasks

    @property
    def completion_rate(self) -> float:
        if self.total_tasks == 0:
            return 0.0
        return self.done_count / self.total_tasks


# ══════════════════════════════════════════════════════════════════
# Wave builder — topological sort
# ══════════════════════════════════════════════════════════════════

def build_execution_waves(tasks: list[ScheduledTask]) -> list[list[ScheduledTask]]:
    """
    Topo-sort tasks vào các waves để chạy song song.

    Wave 0 = tasks không có dependency.
    Wave N = tasks mà tất cả deps đều đã ở wave < N.

    Returns: list of waves, each wave is a list of tasks to run in parallel.

    Ví dụ:
        T1 (no deps), T2 (no deps) → Wave 0: [T1, T2]
        T3 (deps: T1), T4 (deps: T2) → Wave 1: [T3, T4]
        T5 (deps: T3, T4) → Wave 2: [T5]

    Raises:
        ValueError: nếu có circular dependency.
    """
    # Build id → task map
    task_map = {t.id: t for t in tasks}
    wave_of: dict[str, int] = {}
    ordered_waves: list[list[ScheduledTask]] = []

    def _wave(task_id: str, visiting: set[str]) -> int:
        """Recursively compute wave number for a task."""
        if task_id in wave_of:
            return wave_of[task_id]
        if task_id in visiting:
            raise ValueError(f"Circular dependency detected at task '{task_id}'")
        if task_id not in task_map:
            return 0  # Unknown dep → treat as already done in wave 0

        visiting = visiting | {task_id}
        task = task_map[task_id]

        if not task.depends_on:
            wave_of[task_id] = 0
            return 0

        max_dep_wave = max(
            _wave(dep, visiting)
            for dep in task.depends_on
            if dep in task_map
        )
        w = max_dep_wave + 1
        wave_of[task_id] = w
        return w

    # Compute wave for each task
    for task in tasks:
        _wave(task.id, set())

    # Group by wave
    max_wave = max(wave_of.values()) if wave_of else 0
    for w in range(max_wave + 1):
        wave_tasks = [t for t in tasks if wave_of.get(t.id, 0) == w]
        if wave_tasks:
            ordered_waves.append(wave_tasks)

    return ordered_waves


# ══════════════════════════════════════════════════════════════════
# TaskScheduler
# ══════════════════════════════════════════════════════════════════

class TaskScheduler:
    """
    Dependency-aware parallel task scheduler cho Phidipus v1.20.

    Nhận TaskPlan từ TaskDecomposer → parse dependencies → chạy
    song song các tasks độc lập → sequential cho tasks có dependency.

    Max concurrency = 3 để tránh quá tải Ollama + Gemini API.

    Usage::

        scheduler = TaskScheduler(
            executor_fn=my_executor,
            max_concurrency=3,
        )
        result = await scheduler.run(task_id, goal, scheduled_tasks)
    """

    def __init__(
        self,
        executor_fn: Callable[[ScheduledTask, dict], Coroutine[Any, Any, tuple[bool, Any, str]]],
        max_concurrency: int = 3,
        event_bus: Any | None = None,
    ) -> None:
        """
        Args:
            executor_fn: Async fn(task, context) → (success, result, error).
                         Called for each task in the plan.
            max_concurrency: Max tasks running simultaneously. Default 3.
            event_bus: Optional EventBus for publishing step events.
        """
        self._executor = executor_fn
        self._sem = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._bus = event_bus

    async def run(
        self,
        task_id: str,
        goal: str,
        tasks: list[ScheduledTask],
        context: dict[str, Any] | None = None,
    ) -> ScheduleResult:
        """
        Execute tasks respecting dependency order, parallelising where possible.

        Args:
            task_id:  Parent task UUID.
            goal:     Original user goal.
            tasks:    List of ScheduledTask objects (from TaskDecomposer).
            context:  Shared execution context (passed to each task).

        Returns:
            ScheduleResult with full execution summary.
        """
        if not tasks:
            return ScheduleResult(
                task_id=task_id, goal=goal, success=True,
                total_tasks=0, done_count=0, failed_count=0, skipped_count=0,
                total_duration_ms=0, waves_executed=0, parallel_savings_ms=0,
            )

        ctx = dict(context or {})
        ctx["task_id"] = task_id
        ctx["goal"] = goal
        step_outputs: dict[str, Any] = {}

        # Build execution waves
        try:
            waves = build_execution_waves(tasks)
        except ValueError as exc:
            _vlog("❌", f"Scheduler: dependency error — {exc}")
            return ScheduleResult(
                task_id=task_id, goal=goal, success=False,
                total_tasks=len(tasks), done_count=0, failed_count=len(tasks),
                skipped_count=0, total_duration_ms=0, waves_executed=0,
                parallel_savings_ms=0, error=str(exc),
            )

        start_time = time.monotonic()
        done_count = failed_count = skipped_count = 0
        stop_on_failure = False
        sequential_time_estimate = 0
        actual_time = 0

        _vlog("📐", f"Scheduler: {len(tasks)} tasks → {len(waves)} waves "
              f"(max {self._max_concurrency} concurrent)")

        for wave_idx, wave in enumerate(waves):
            # Check if we should skip remaining waves due to earlier failure
            if stop_on_failure:
                skipped_count += len(wave)
                for t in wave:
                    t.status = "skipped"
                continue

            # Filter: skip tasks whose deps failed
            runnable = []
            for task in wave:
                dep_ok = all(
                    any(t.id == dep and t.status == "done" for t in tasks)
                    for dep in task.depends_on
                )
                if dep_ok or not task.depends_on:
                    runnable.append(task)
                else:
                    task.status = "skipped"
                    skipped_count += 1
                    _vlog("⏭️", f"  Skip {task.id} (deps failed)")

            if not runnable:
                continue

            parallel_label = (
                f"song song {len(runnable)} tasks"
                if len(runnable) > 1
                else f"1 task"
            )
            _vlog("🌊", f"Wave {wave_idx + 1}/{len(waves)}: {parallel_label}")

            # Estimate sequential time for savings calculation
            sequential_time_estimate += len(runnable) * 10_000  # 10s avg per task

            wave_start = time.monotonic()

            # Run wave tasks in parallel (up to max_concurrency)
            wave_coros = [
                self._run_single(task, ctx, step_outputs, task_id)
                for task in runnable
            ]
            wave_results = await asyncio.gather(*wave_coros, return_exceptions=True)

            wave_duration_ms = int((time.monotonic() - wave_start) * 1000)
            actual_time += wave_duration_ms

            # Process results
            wave_failed = False
            for task, res in zip(runnable, wave_results):
                if isinstance(res, BaseException):
                    task.status = "failed"
                    task.error = str(res)[:200]
                    failed_count += 1
                    wave_failed = True
                    _vlog("❌", f"  {task.id}: exception — {str(res)[:60]}")
                elif res[0]:  # success
                    done_count += 1
                    step_outputs[task.id] = res[1]
                    ctx[f"result_{task.id}"] = res[1]
                    ctx["previous_result"] = res[1]  # Last result for chaining
                    _vlog("✅", f"  {task.id}: xong ({task.duration_ms}ms)")
                else:
                    failed_count += 1
                    wave_failed = True
                    _vlog("❌", f"  {task.id}: {task.error[:60]}")

            # Publish wave completion event
            if self._bus:
                await self._bus.publish("WAVE_COMPLETED", {
                    "task_id": task_id,
                    "wave": wave_idx,
                    "done": sum(1 for t in runnable if t.status == "done"),
                    "failed": sum(1 for t in runnable if t.status == "failed"),
                    "duration_ms": wave_duration_ms,
                }, source="scheduler")

            if wave_failed:
                _vlog("⚠️", f"Wave {wave_idx + 1} có task thất bại — dừng scheduler")
                stop_on_failure = True

        total_ms = int((time.monotonic() - start_time) * 1000)
        # Savings = estimated sequential time - actual time
        savings_ms = max(0, sequential_time_estimate - actual_time)

        success = (failed_count == 0 and skipped_count == 0)
        if success:
            _vlog("✅", f"Scheduler hoàn thành: {done_count}/{len(tasks)} tasks "
                  f"trong {total_ms}ms (~{savings_ms}ms tiết kiệm vs sequential)")
        else:
            _vlog("⚠️", f"Scheduler: {done_count} done, {failed_count} failed, "
                  f"{skipped_count} skipped — {total_ms}ms")

        return ScheduleResult(
            task_id=task_id,
            goal=goal,
            success=success,
            total_tasks=len(tasks),
            done_count=done_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            total_duration_ms=total_ms,
            waves_executed=len(waves),
            parallel_savings_ms=savings_ms,
            step_outputs=step_outputs,
        )

    async def _run_single(
        self,
        task: ScheduledTask,
        ctx: dict[str, Any],
        step_outputs: dict[str, Any],
        task_id: str,
    ) -> tuple[bool, Any, str]:
        """
        Run a single task with semaphore (concurrency limit).

        Returns: (success, result, error_msg)
        """
        async with self._sem:
            task.status = "running"
            task.started_at = time.monotonic()

            # Publish step start
            if self._bus:
                await self._bus.publish("TASK_STEP_START", {
                    "task_id": task_id,
                    "step_id": task.id,
                    "action": task.action,
                    "description": task.description,
                }, source="scheduler")

            try:
                success, result, error = await self._executor(task, ctx)
                task.result = result
                task.error = error[:200] if error else ""
                task.status = "done" if success else "failed"
                task.finished_at = time.monotonic()

                # Publish step done
                if self._bus:
                    await self._bus.publish("TASK_STEP_DONE", {
                        "task_id": task_id,
                        "step_id": task.id,
                        "success": success,
                        "duration_ms": task.duration_ms,
                    }, source="scheduler")

                return success, result, error

            except Exception as exc:
                task.status = "failed"
                task.error = str(exc)[:200]
                task.finished_at = time.monotonic()

                if self._bus:
                    await self._bus.publish("TASK_STEP_DONE", {
                        "task_id": task_id,
                        "step_id": task.id,
                        "success": False,
                        "error": str(exc)[:100],
                    }, source="scheduler")

                return False, None, str(exc)


# ══════════════════════════════════════════════════════════════════
# Helpers to convert TaskDecomposer output → ScheduledTask list
# ══════════════════════════════════════════════════════════════════

def plan_to_scheduled_tasks(plan: Any) -> list[ScheduledTask]:
    """
    Convert TaskPlan (from TaskDecomposer) → list of ScheduledTask.

    Works with both TaskPlan objects and raw dicts.
    """
    if plan is None:
        return []

    # Handle TaskPlan object
    if hasattr(plan, "tasks"):
        raw_tasks = plan.tasks
    elif isinstance(plan, dict):
        raw_tasks = plan.get("tasks", [])
    else:
        return []

    scheduled = []
    for t in raw_tasks:
        if hasattr(t, "id"):
            # SubTask object from task_decomposer
            scheduled.append(ScheduledTask(
                id=t.id,
                description=t.description,
                action=getattr(t, "action", "execute"),
                params=getattr(t, "params", {}) or {},
                depends_on=list(getattr(t, "depends_on", []) or []),
            ))
        elif isinstance(t, dict):
            scheduled.append(ScheduledTask(
                id=t.get("id", f"T{len(scheduled)+1}"),
                description=t.get("description", ""),
                action=t.get("action", "execute"),
                params=t.get("params", {}),
                depends_on=list(t.get("depends_on", [])),
            ))

    return scheduled


def analyze_parallelism(tasks: list[ScheduledTask]) -> dict:
    """
    Phân tích potential parallelism trong task list.

    Returns:
        {
            "waves": int,
            "max_parallel": int,   # max tasks in any single wave
            "parallel_ratio": float,  # 0.0 = fully sequential, 1.0 = all parallel
        }
    """
    if not tasks:
        return {"waves": 0, "max_parallel": 0, "parallel_ratio": 0.0}

    try:
        waves = build_execution_waves(tasks)
        max_parallel = max(len(w) for w in waves) if waves else 0
        # Ratio: if all parallel → 1 wave → ratio 1.0; all sequential → ratio 0.0
        ratio = 1.0 - (len(waves) - 1) / max(len(tasks) - 1, 1)
        return {
            "waves": len(waves),
            "max_parallel": max_parallel,
            "parallel_ratio": round(max(0.0, min(1.0, ratio)), 2),
        }
    except Exception:
        return {"waves": len(tasks), "max_parallel": 1, "parallel_ratio": 0.0}
