# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/runtime_monitor.py — Phidipus v1.0
Health monitoring for the orchestrator runtime — read-only observation only.

Architecture contract (RETAIN):
  "Health monitoring.  Alerts on stuck loops, error rate spikes.
   Read-only observation.  No remediation with OS access."

RuntimeMonitor collects and exposes runtime health metrics for the
agent loop.  It is intentionally limited to observation — it never
takes remedial action, never calls pyautogui, and never issues IPC
commands.  Alerts are raised exclusively via structured log entries
so external monitoring infrastructure can act on them.

Metrics tracked
---------------
  - Task count (started, succeeded, failed)
  - Error rate over a rolling window
  - Loop iteration count and stuck-loop detection
  - LLM call latency (min/max/mean)
  - Last-activity timestamp for dead-man's-switch detection

Stuck-loop detection
--------------------
A loop is considered "stuck" if the number of iterations exceeds
cfg.agent.max_steps without a task completing.  The monitor emits
an ERROR log entry; the agent loop decides whether to abort.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  (Monitor is read-only — no writes to disk, no IPC dispatch.)

Used by:
  core/agent_loop.py  — record_task_start(), record_task_end(),
                        record_error(), record_llm_latency(),
                        check_health()

Dependencies:
  utils/logger.py         — get_logger()
  config/config_loader.py — PhidipusConfig (cfg.agent.max_steps,
                             cfg.agent.monitor_interval_seconds)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from config.config_loader import PhidipusConfig
from ipc.action_schema import _utc_now   # Bug #13 fix: single source of truth
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Rolling window size for error-rate calculation.
_ERROR_WINDOW: int = 20

#: Error rate threshold above which an ALERT is emitted.
_ERROR_RATE_ALERT_THRESHOLD: float = 0.5   # 50 % of recent tasks failed

#: Maximum LLM latency (seconds) before a slow-response warning is emitted.
_LLM_SLOW_THRESHOLD: float = 30.0


# ---------------------------------------------------------------------------
# Health snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HealthSnapshot:
    """
    Immutable point-in-time health snapshot.

    Attributes:
        tasks_started:   Total tasks started since monitor creation.
        tasks_succeeded: Total tasks completed successfully.
        tasks_failed:    Total tasks that ended with an error.
        loop_iterations: Total ReAct loop iterations across all tasks.
        error_rate:      Error rate over the last _ERROR_WINDOW tasks.
        stuck:           True if the current task has exceeded max_steps.
        llm_calls:       Total LLM API calls made.
        llm_mean_ms:     Mean LLM latency in milliseconds.
        last_activity:   ISO-8601 timestamp of the last recorded event.
        alerts:          List of active alert strings.
    """

    tasks_started:   int
    tasks_succeeded: int
    tasks_failed:    int
    loop_iterations: int
    error_rate:      float
    stuck:           bool
    llm_calls:       int
    llm_mean_ms:     float
    last_activity:   str
    alerts:          tuple[str, ...]


# ---------------------------------------------------------------------------
# RuntimeMonitor
# ---------------------------------------------------------------------------

class RuntimeMonitor:
    """
    Read-only health monitor for the orchestrator agent loop.

    Collects metrics via record_*() methods and exposes a health snapshot
    via check_health().  No OS actions, no IPC dispatch — observation only.

    Usage::

        monitor = RuntimeMonitor(cfg)

        # In agent_loop:
        monitor.record_task_start(task_id="abc", goal="open browser")
        monitor.record_loop_iteration()
        monitor.record_llm_latency(latency_ms=450.0)
        monitor.record_task_end(success=True)

        health = monitor.check_health()
        if health.stuck:
            # Agent loop decides to abort current task
            ...

    Args:
        cfg: Validated PhidipusConfig.
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._max_steps        = cfg.agent.max_steps
        self._tasks_started    = 0
        self._tasks_succeeded  = 0
        self._tasks_failed     = 0
        self._loop_iterations  = 0
        self._current_task_iterations = 0
        self._current_task_id  = ""
        self._llm_calls        = 0
        self._llm_latencies:   deque[float] = deque(maxlen=100)
        self._recent_outcomes: deque[bool]  = deque(maxlen=_ERROR_WINDOW)
        self._last_activity    = _utc_now()
        self._alerts:          list[str]    = []

        _log.info(
            "RuntimeMonitor initialised",
            extra={"max_steps": self._max_steps},
        )

    # ------------------------------------------------------------------
    # Record methods (called by agent_loop)
    # ------------------------------------------------------------------

    def record_task_start(self, task_id: str, goal: str = "") -> None:
        """Record the beginning of a new task."""
        self._current_task_id         = task_id
        self._current_task_iterations = 0
        self._tasks_started          += 1
        self._last_activity           = _utc_now()
        _log.debug(
            "RuntimeMonitor: task started",
            extra={"task_id": task_id, "goal_len": len(goal)},
        )

    def record_task_end(self, *, success: bool, task_id: str = "") -> None:
        """Record the completion of the current task."""
        if success:
            self._tasks_succeeded += 1
        else:
            self._tasks_failed += 1
        self._recent_outcomes.append(success)
        self._current_task_id         = ""
        self._current_task_iterations = 0
        self._last_activity           = _utc_now()
        _log.debug(
            "RuntimeMonitor: task ended",
            extra={"success": success, "tasks_total": self._tasks_started},
        )

    def record_loop_iteration(self) -> None:
        """Record one ReAct loop iteration for the current task."""
        self._loop_iterations         += 1
        self._current_task_iterations += 1
        self._last_activity           = _utc_now()

        # Stuck-loop detection
        # FIX S1: use > (not >=) so step N=max_steps still executes before stuck fires
        if self._current_task_iterations > self._max_steps:
            alert = (
                f"STUCK_LOOP: task {self._current_task_id!r} has reached "
                f"{self._current_task_iterations} iterations "
                f"(max_steps={self._max_steps})"
            )
            _log.error(
                "RuntimeMonitor: STUCK LOOP detected",
                extra={
                    "task_id":    self._current_task_id,
                    "iterations": self._current_task_iterations,
                    "max_steps":  self._max_steps,
                },
            )
            if alert not in self._alerts:
                self._alerts.append(alert)

    def record_error(self, error: str = "", task_id: str = "") -> None:
        """Record an error event (does not count as task_end)."""
        self._last_activity = _utc_now()
        alert = f"ERROR: {error[:128]}" if error else "ERROR: unspecified"
        _log.error(
            "RuntimeMonitor: error recorded",
            extra={"task_id": task_id or self._current_task_id, "error": error[:256]},
        )
        # Spike detection: if error_rate exceeds threshold
        if len(self._recent_outcomes) >= 5:
            rate = self._compute_error_rate()
            if rate >= _ERROR_RATE_ALERT_THRESHOLD:
                spike_alert = f"ERROR_RATE_SPIKE: {rate:.0%} in last {len(self._recent_outcomes)} tasks"
                _log.error(
                    "RuntimeMonitor: ERROR RATE SPIKE",
                    extra={"rate": rate, "window": len(self._recent_outcomes)},
                )
                if spike_alert not in self._alerts:
                    self._alerts.append(spike_alert)

    def record_llm_latency(self, latency_ms: float) -> None:
        """Record the latency of an LLM API call in milliseconds."""
        self._llm_calls     += 1
        self._llm_latencies.append(latency_ms)
        self._last_activity  = _utc_now()
        if latency_ms > _LLM_SLOW_THRESHOLD * 1000:
            _log.warning(
                "RuntimeMonitor: slow LLM response",
                extra={"latency_ms": latency_ms, "threshold_ms": _LLM_SLOW_THRESHOLD * 1000},
            )

    def clear_alerts(self) -> None:
        """Clear all active alerts (e.g. after the agent loop handles them)."""
        self._alerts.clear()

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def check_health(self) -> HealthSnapshot:
        """
        Return an immutable snapshot of current health metrics.

        This method is read-only — it never modifies state or triggers
        any action.
        """
        error_rate  = self._compute_error_rate()
        stuck       = self._current_task_iterations > self._max_steps
        llm_mean_ms = (
            sum(self._llm_latencies) / len(self._llm_latencies)
            if self._llm_latencies else 0.0
        )

        return HealthSnapshot(
            tasks_started   = self._tasks_started,
            tasks_succeeded = self._tasks_succeeded,
            tasks_failed    = self._tasks_failed,
            loop_iterations = self._loop_iterations,
            error_rate      = error_rate,
            stuck           = stuck,
            llm_calls       = self._llm_calls,
            llm_mean_ms     = llm_mean_ms,
            last_activity   = self._last_activity,
            alerts          = tuple(self._alerts),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compute_error_rate(self) -> float:
        """Return the fraction of failed tasks in the recent outcomes window."""
        if not self._recent_outcomes:
            return 0.0
        failures = sum(1 for ok in self._recent_outcomes if not ok)
        return failures / len(self._recent_outcomes)
