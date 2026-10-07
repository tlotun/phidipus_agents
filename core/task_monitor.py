# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/task_monitor.py — Phidipus v2.4 Priority 3 / C1
═══════════════════════════════════════════════════════════════════

Extracted progress tracking & notification logic from agent_loop.py.

Tracks workflow/task execution progress and sends updates to:
  - Telegram bot (via notify_fn)
  - WeChat bot (via wechat_fn)
  - Admin Panel WebSocket (via ws_broadcast_fn)
  - Internal EventBus

This file is ADDITIVE — works alongside agent_loop.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

_log = logging.getLogger("phidipus.task_monitor")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


@dataclass
class StepProgress:
    """Progress of a single workflow step."""
    node_id: str
    node_type: str
    step_num: int
    total_steps: int
    status: str = "pending"      # pending | running | done | failed
    started_at: float = 0.0
    finished_at: float = 0.0
    elapsed_s: float = 0.0
    success: bool = False
    method: str = ""
    error: str = ""


@dataclass
class TaskProgress:
    """Progress of an entire task/workflow."""
    task_id: str
    goal: str
    workflow_name: str = ""
    total_steps: int = 0
    steps_done: int = 0
    current_step: Optional[StepProgress] = None
    steps: list[StepProgress] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    status: str = "pending"      # pending | running | done | failed
    success: bool = False
    error: str = ""


class TaskMonitor:
    """
    Tracks task/workflow execution progress and sends updates.

    Usage:
        monitor = TaskMonitor()
        monitor.inject(notify_fn=tg_send, ws_fn=ws_broadcast)

        monitor.start_task("task_1", "báo cáo doanh số", total_steps=6)
        monitor.start_step("task_1", "n_2", "chrome", step_num=1)
        monitor.finish_step("task_1", "n_2", success=True, elapsed=1.2)
        monitor.finish_task("task_1", success=True)
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskProgress] = {}
        self._notify_fn: Optional[Callable] = None
        self._wechat_fn: Optional[Callable] = None
        self._ws_fn: Optional[Callable] = None
        self._event_bus: Any = None

    def inject(self, notify_fn=None, wechat_fn=None, ws_fn=None, event_bus=None) -> None:
        if notify_fn:
            self._notify_fn = notify_fn
        if wechat_fn:
            self._wechat_fn = wechat_fn
        if ws_fn:
            self._ws_fn = ws_fn
        if event_bus:
            self._event_bus = event_bus

    # ── Task lifecycle ────────────────────────────────────────

    def start_task(self, task_id: str, goal: str, workflow_name: str = "",
                   total_steps: int = 0) -> TaskProgress:
        progress = TaskProgress(
            task_id=task_id,
            goal=goal,
            workflow_name=workflow_name,
            total_steps=total_steps,
            started_at=time.time(),
            status="running",
        )
        self._tasks[task_id] = progress
        _vlog("🚀", f"Task started: {goal[:50]} ({total_steps} steps)")
        self._broadcast("task_start", {
            "task_id": task_id, "goal": goal,
            "workflow_name": workflow_name, "total_steps": total_steps,
        })
        return progress

    def finish_task(self, task_id: str, success: bool = True, error: str = "") -> None:
        progress = self._tasks.get(task_id)
        if not progress:
            return
        progress.finished_at = time.time()
        progress.status = "done" if success else "failed"
        progress.success = success
        progress.error = error
        elapsed = progress.finished_at - progress.started_at

        icon = "✅" if success else "❌"
        _vlog(icon, f"Task finished: {progress.goal[:40]} "
                     f"({progress.steps_done}/{progress.total_steps} steps, {elapsed:.1f}s)")

        self._broadcast("task_done", {
            "task_id": task_id,
            "success": success,
            "steps_done": progress.steps_done,
            "total_steps": progress.total_steps,
            "elapsed_s": round(elapsed, 1),
            "error": error,
        })

        # Send summary notification
        if self._notify_fn:
            msg = (f"{icon} {'Workflow xong' if success else 'Workflow lỗi'}!\n"
                   f"📋 {progress.workflow_name or progress.goal[:40]}\n"
                   f"📦 {progress.steps_done}/{progress.total_steps} steps | ⏱️ {elapsed:.1f}s")
            if error:
                msg += f"\n❌ {error[:200]}"
            asyncio.ensure_future(self._safe_notify(msg))

    # ── Step lifecycle ────────────────────────────────────────

    def start_step(self, task_id: str, node_id: str, node_type: str,
                   step_num: int) -> Optional[StepProgress]:
        progress = self._tasks.get(task_id)
        if not progress:
            return None
        step = StepProgress(
            node_id=node_id,
            node_type=node_type,
            step_num=step_num,
            total_steps=progress.total_steps,
            status="running",
            started_at=time.time(),
        )
        progress.current_step = step
        progress.steps.append(step)

        self._broadcast("workflow_node_start", {
            "task_id": task_id, "node_id": node_id,
            "node_type": node_type,
            "step": step_num, "total": progress.total_steps,
            "workflow_name": progress.workflow_name,
        })
        return step

    def finish_step(self, task_id: str, node_id: str, success: bool = True,
                    elapsed: float = 0, method: str = "", error: str = "") -> None:
        progress = self._tasks.get(task_id)
        if not progress:
            return

        # Find the step
        step = next((s for s in progress.steps if s.node_id == node_id and s.status == "running"), None)
        if step:
            step.finished_at = time.time()
            step.elapsed_s = elapsed or (step.finished_at - step.started_at)
            step.status = "done" if success else "failed"
            step.success = success
            step.method = method
            step.error = error

        if success:
            progress.steps_done += 1

        progress.current_step = None

        self._broadcast("workflow_node_done", {
            "task_id": task_id, "node_id": node_id,
            "node_type": step.node_type if step else "",
            "step": progress.steps_done, "total": progress.total_steps,
            "success": success,
            "elapsed_s": round(step.elapsed_s if step else elapsed, 2),
            "method": method,
        })

    # ── Query ─────────────────────────────────────────────────

    def get_progress(self, task_id: str) -> Optional[TaskProgress]:
        return self._tasks.get(task_id)

    def get_active_tasks(self) -> list[TaskProgress]:
        return [t for t in self._tasks.values() if t.status == "running"]

    def stats(self) -> dict:
        return {
            "total_tracked": len(self._tasks),
            "active": len(self.get_active_tasks()),
            "completed": len([t for t in self._tasks.values() if t.status == "done"]),
            "failed": len([t for t in self._tasks.values() if t.status == "failed"]),
        }

    # ── Internal ──────────────────────────────────────────────

    def _broadcast(self, event: str, data: dict) -> None:
        """Send to WebSocket + EventBus.

        [FIX v4.1] EventBus.publish() là coroutine (async def) — phải wrap bằng
        asyncio.ensure_future() khi gọi từ context synchronous. Trước đây gọi trực
        tiếp self._event_bus.publish(event, data) khiến coroutine không bao giờ
        được await → RuntimeWarning và subscribers không nhận được event.
        """
        if self._ws_fn:
            try:
                asyncio.ensure_future(self._ws_fn(event, data))
            except Exception:
                pass
        if self._event_bus:
            try:
                # [FIX] publish() là async → dùng ensure_future để schedule trên event loop
                asyncio.ensure_future(self._event_bus.publish(event, data))
            except RuntimeError:
                # Không có running event loop (unit test / sync context) → bỏ qua
                pass
            except Exception:
                pass

    async def _safe_notify(self, msg: str) -> None:
        try:
            if asyncio.iscoroutinefunction(self._notify_fn):
                await self._notify_fn(msg)
            else:
                self._notify_fn(msg)
        except Exception:
            pass


# ── Singleton ─────────────────────────────────────────────────
_instance: Optional[TaskMonitor] = None

def get_task_monitor() -> TaskMonitor:
    global _instance
    if _instance is None:
        _instance = TaskMonitor()
    return _instance
