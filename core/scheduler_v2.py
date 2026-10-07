# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/scheduler.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

Agent Work Scheduler — Tự động chạy task theo lịch hàng ngày.

Hỗ trợ:
  - Lịch hàng ngày, thứ 2-6, hàng tuần, tùy chỉnh
  - 3 nguồn task: Workflow Teacher, Skill, hoặc lệnh Telegram
  - Timezone configurable (15 múi giờ phổ biến)
  - Priority: low/normal/high
  - Telegram notification khi chạy xong
  - Execution history log
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


_SCHEDULER_DIR = Path(__file__).parent.parent / "data" / "schedules"
_SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)

_SCHEDULES_FILE = _SCHEDULER_DIR / "agent_schedule.json"
_HISTORY_FILE = _SCHEDULER_DIR / "schedule_history.json"
_CONFIG_FILE = _SCHEDULER_DIR / "scheduler_config.json"


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def _save_json(path: Path, data):
    # v4.3: atomic write — the admin panel, Brain scheduler nodes and this loop
    # all write the same files; a crash mid-write used to truncate them.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


_ROOT = Path(__file__).parent.parent
_WORKFLOWS_DIR = _ROOT / "data" / "workflows"


def _load_task_workflow(task: dict) -> dict | None:
    """v4.3: resolve a scheduled workflow from workflow_file → workflow_id → name.

    ``workflow_file`` (written by Brain/WorkflowExecutor scheduler nodes) must
    stay inside data/workflows.  Legacy entries only had ``workflow_id``.
    """
    rel = str(task.get("workflow_file") or "").strip()
    if rel:
        try:
            path = (_ROOT / rel).resolve()
            path.relative_to(_WORKFLOWS_DIR.resolve())
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    wf_id = str(task.get("workflow_id") or "").strip()
    if wf_id and "/" not in wf_id and ".." not in wf_id:
        f = _WORKFLOWS_DIR / f"{wf_id}.json"
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
    try:
        from core.workflow_nodes_ext import resolve_workflow
        return resolve_workflow(wf_id) or resolve_workflow(task.get("name", ""))
    except Exception:
        return None


async def _hub_notify(text: str) -> None:
    try:
        from core import notify_hub
        await notify_hub.send_text(text)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# Schedule CRUD
# ══════════════════════════════════════════════════════════════

def load_schedules() -> list[dict]:
    data = _load_json(_SCHEDULES_FILE, [])
    return data if isinstance(data, list) else []


def save_schedule(task: dict):
    schedules = load_schedules()
    # Update or insert
    idx = next((i for i, s in enumerate(schedules) if s.get("id") == task.get("id")), -1)
    if idx >= 0:
        schedules[idx] = task
    else:
        schedules.append(task)
    _save_json(_SCHEDULES_FILE, schedules)


def delete_schedule(task_id: str):
    schedules = [s for s in load_schedules() if s.get("id") != task_id]
    _save_json(_SCHEDULES_FILE, schedules)


def get_timezone() -> str:
    config = _load_json(_CONFIG_FILE, {})
    return config.get("timezone", "Asia/Ho_Chi_Minh")


def set_timezone(tz: str):
    config = _load_json(_CONFIG_FILE, {})
    config["timezone"] = tz
    _save_json(_CONFIG_FILE, config)


# ══════════════════════════════════════════════════════════════
# History log
# ══════════════════════════════════════════════════════════════

def add_history(task_name: str, success: bool, duration_s: float, error: str = ""):
    history = _load_json(_HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    tz = get_timezone()
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now()

    entry = {
        "ts": time.time(),
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "name": task_name,
        "success": success,
        "duration_s": round(duration_s, 1),
        "error": error[:200] if error else "",
    }
    history.insert(0, entry)
    history = history[:200]  # keep last 200
    _save_json(_HISTORY_FILE, history)


def load_history(limit: int = 30) -> list[dict]:
    history = _load_json(_HISTORY_FILE, [])
    if not isinstance(history, list):
        return []
    return history[:limit]


# ══════════════════════════════════════════════════════════════
# Scheduler Loop — runs as background asyncio task
# ══════════════════════════════════════════════════════════════

class AgentScheduler:
    """Background scheduler — checks every 30s, runs matching tasks."""

    def __init__(self, agent_loop=None, notify_fn=None):
        self._agent = agent_loop
        # v4.3: default to the notify hub so the target is resolved at send
        # time (Telegram registers its sender after the scheduler starts).
        self._notify = notify_fn or _hub_notify
        self._running = False
        self._task = None
        self._last_run = {}  # task_id → last run timestamp (prevent double-run)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        _vlog("📅", "Agent Scheduler started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _vlog("📅", "Agent Scheduler stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._check_and_run()
            except Exception as exc:
                _vlog("⚠️", f"Scheduler error: {str(exc)[:60]}")
            await asyncio.sleep(30)  # Check every 30 seconds

    async def _check_and_run(self):
        tz_name = get_timezone()
        try:
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now = datetime.now()

        current_time = now.strftime("%H:%M")
        current_day = now.weekday()  # 0=Mon
        # Convert to our format: 0=Sun, 1=Mon...6=Sat
        day_idx = (current_day + 1) % 7 if current_day < 6 else 0

        schedules = load_schedules()
        for task in schedules:
            if not task.get("enabled", True):
                continue

            # v4.3: interval tasks ({"interval_minutes": N}) written by Brain /
            # workflow scheduler nodes were silently ignored before.
            interval = task.get("interval_minutes")
            if interval:
                try:
                    interval_s = max(5, int(interval)) * 60
                except (TypeError, ValueError):
                    continue
                last = float(task.get("last_run_at") or task.get("created_at") or 0)
                if time.time() - last < interval_s:
                    continue
                task["last_run_at"] = time.time()
                save_schedule(task)
                _vlog("📅", f"Scheduler: running '{task.get('name','')}' (every {interval} min)")
                asyncio.create_task(self._execute_task(task))
                continue

            task_time = task.get("time", "")
            task_days = task.get("days") or list(range(7))  # v4.3: no days = every day

            # Match time (exact minute) and day
            if task_time != current_time:
                continue
            if day_idx not in task_days:
                continue

            # Prevent double-run within same minute
            run_key = f"{task['id']}_{current_time}_{now.strftime('%Y%m%d')}"
            if run_key in self._last_run:
                continue
            self._last_run[run_key] = time.time()

            # Clean old run keys (keep last hour)
            cutoff = time.time() - 3600
            self._last_run = {k: v for k, v in self._last_run.items() if v > cutoff}

            # Execute task
            _vlog("📅", f"Scheduler: running '{task.get('name','')}' at {current_time}")
            asyncio.create_task(self._execute_task(task))

    async def _execute_task(self, task: dict):
        t0 = time.time()
        name = task.get("name", "Unnamed")
        source = task.get("source", "command")
        success = False
        error = ""

        try:
            if source == "workflow":
                # Run taught / Brain-generated workflow
                wf = _load_task_workflow(task)
                if wf:
                    from core.workflow_executor import WorkflowExecutor
                    executor = WorkflowExecutor(
                        ipc_client=getattr(self._agent, '_ipc', None) if self._agent else None,
                        notify_fn=self._notify,
                    )
                    variables = dict(task.get("variables") or {})
                    variables["_scheduled_run"] = "1"   # scheduler nodes must not re-register
                    result = await asyncio.wait_for(
                        executor.run(wf, goal=task.get("goal", ""), variables=variables),
                        timeout=float(task.get("timeout_s", 900)),
                    )
                    success = result.get("success", False)
                    error = result.get("error", "")
                else:
                    error = f"Workflow {task.get('workflow_file') or task.get('workflow_id', '')} not found"

            elif source == "command":
                # Run as Telegram-style command
                command = task.get("command", "")
                if command and self._agent:
                    result = await asyncio.wait_for(
                        self._agent.run_task(command), timeout=300
                    )
                    success = getattr(result, "success", False)
                    error = getattr(result, "error", "")
                else:
                    error = "No command or agent not ready"

            elif source == "skill":
                # Run skill directly
                skill_name = task.get("skill_name", "")
                if skill_name and self._agent:
                    result = await asyncio.wait_for(
                        self._agent.run_task(skill_name), timeout=300
                    )
                    success = getattr(result, "success", False)
                    error = getattr(result, "error", "")
                else:
                    error = "No skill name or agent not ready"

        except asyncio.TimeoutError:
            error = "Timeout"
        except Exception as exc:
            error = str(exc)[:200]

        elapsed = round(time.time() - t0, 1)
        add_history(name, success, elapsed, error)

        # Notify
        if task.get("notify", True) and self._notify:
            icon = "✅" if success else "❌"
            try:
                await self._notify(
                    f"📅 *Lịch biểu:* {icon} {name}\n"
                    f"⏱ {elapsed}s" + (f"\n❌ {error}" if error else "")
                )
            except Exception:
                pass

        _vlog("📅" if success else "❌", f"Scheduled '{name}': {'✅' if success else '❌'} ({elapsed}s)")

    async def run_task_now(self, task_id: str):
        """Manually trigger a scheduled task."""
        schedules = load_schedules()
        task = next((s for s in schedules if s.get("id") == task_id), None)
        if task:
            await self._execute_task(task)


# Singleton
_scheduler: Optional[AgentScheduler] = None

def get_scheduler(agent_loop=None, notify_fn=None) -> AgentScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AgentScheduler(agent_loop=agent_loop, notify_fn=notify_fn)
    elif agent_loop is not None and _scheduler._agent is None:
        # v4.3: the admin panel may create the singleton before main.py wires
        # the agent — late-bind instead of keeping an agent-less scheduler.
        _scheduler._agent = agent_loop
        if notify_fn:
            _scheduler._notify = notify_fn
    return _scheduler
