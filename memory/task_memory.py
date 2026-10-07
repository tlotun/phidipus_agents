# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/task_memory.py — Phidipus v1.0
Session-scoped in-memory task context — never persisted to disk.

Architecture contract (M-7):
  "Session-scoped only.  Never persisted to disk.  Cleared on task
   completion.  Not propagated across sessions."

TaskMemory holds transient context for the currently executing task:
intermediate reasoning steps, observations, partial results, and tool
call history.  All data is stored in process memory only and is
explicitly cleared when a task completes (or fails).

No disk I/O occurs in this module — not even via MemoryGuard.  This is
intentional: task-level context is ephemeral by design and must not
contaminate episodic memory or be readable across session boundaries.

When a task completes successfully, the agent loop is responsible for
distilling the relevant task context into an episode and storing it via
EpisodicMemory.  TaskMemory itself holds the raw intermediate state.

Process: orchestrator (L1)

Security invariants enforced here:
  M-7   No writes to disk ever occur.  The clear() method wipes all
        in-memory state.  Data never crosses session boundaries.
  R-02  No subprocess or OS calls.

Used by:
  core/agent_loop.py         — add_step(), add_observation(), clear()
  planner/react_reasoner.py  — add_thought(), add_action(), add_observation()

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ipc.action_schema import _utc_now   # single source of truth for UTC timestamps
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum number of ReAct steps retained in memory (Bug #10 fix).
#: Prevents unbounded RAM growth if a task runs thousands of steps
#: before the stuck-loop detector in RuntimeMonitor fires.
#: When exceeded, the oldest steps are dropped — only the most recent
#: _MAX_INTERNAL_STEPS are kept.
_MAX_INTERNAL_STEPS: int = 500


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------



@dataclass
class ReActStep:
    """
    A single Thought → Action → Observation cycle in the ReAct loop.

    Attributes:
        step_n:       1-based step number within the current task.
        thought:      LLM reasoning text (may be empty until set).
        action:       IPC action name dispatched (may be empty).
        action_payload: Action payload dict (may be empty).
        observation:  Result/observation after action execution.
        ts:           UTC timestamp of step creation.
    """

    step_n:         int
    thought:        str             = ""
    action:         str             = ""
    action_payload: dict[str, Any]  = field(default_factory=dict)
    observation:    str             = ""
    ts:             str             = field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# TaskMemory
# ---------------------------------------------------------------------------

class TaskMemory:
    """
    Session-scoped in-memory task context.

    Stores ReAct steps, tool observations, and task metadata for the
    currently executing task.  All state is cleared between tasks.

    This class is NOT thread-safe.  It is designed for use within a
    single asyncio event loop with one task executing at a time.

    Usage::

        memory = TaskMemory()
        memory.begin_task(task_id="task-001", goal="Open browser")

        memory.add_thought("I should click the browser icon")
        memory.add_action("mouse_click", {"x": 100, "y": 200})
        memory.add_observation("Browser opened successfully")

        context = memory.get_context()   # for LLM prompt assembly
        memory.clear()                   # after task completes

    """

    def __init__(self) -> None:
        self._task_id:   str              = ""
        self._goal:      str              = ""
        self._steps:     list[ReActStep]  = []
        self._metadata:  dict[str, Any]   = {}
        self._started_at: str             = ""
        self._total_steps: int            = 0   # FIX B-03: true step counter

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def begin_task(self, task_id: str, goal: str) -> None:
        """
        Initialise memory for a new task.  Clears any previous state.

        Args:
            task_id: Unique identifier for this task (e.g. UUID4 hex).
            goal:    Sanitized goal string (must already be R-24 sanitized
                     by llm_client.py before reaching here).
        """
        self.clear()
        self._task_id    = task_id
        self._goal       = goal
        self._started_at = _utc_now()
        _log.info(
            "TaskMemory: task started",
            extra={"task_id": task_id, "goal_len": len(goal)},
        )

    def clear(self) -> None:
        """
        Wipe all in-memory task state (M-7).

        Must be called after every task regardless of outcome.
        """
        self._task_id    = ""
        self._goal       = ""
        self._steps      = []
        self._metadata   = {}
        self._started_at = ""
        self._total_steps = 0   # FIX B-03: reset true counter
        _log.debug("TaskMemory: cleared (M-7)")

    # ------------------------------------------------------------------
    # ReAct step building
    # ------------------------------------------------------------------

    def add_thought(self, thought: str) -> ReActStep:
        """
        Append a new step and record the LLM's reasoning thought.

        Returns the newly created step (so the caller can add action/obs).
        """
        self._total_steps += 1  # FIX B-03: increment true counter BEFORE truncation
        step = ReActStep(step_n=self._total_steps, thought=thought)
        self._steps.append(step)
        # Bug #10 fix: cap internal storage to prevent unbounded RAM growth.
        if len(self._steps) > _MAX_INTERNAL_STEPS:
            self._steps = self._steps[-_MAX_INTERNAL_STEPS:]
        return step

    def add_action(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        """
        Record the IPC action dispatched in the current (last) step.

        Args:
            action:  IPC action name.
            payload: Action payload dict (should be post-confidence-gate).
        """
        if not self._steps:
            # No thought yet — create an implicit step
            self._steps.append(ReActStep(step_n=1))

        last = self._steps[-1]
        # dataclass field is mutable — assign directly
        self._steps[-1] = ReActStep(
            step_n=last.step_n,
            thought=last.thought,
            action=action,
            action_payload=dict(payload),
            observation=last.observation,
            ts=last.ts,
        )

    def add_observation(self, observation: str) -> None:
        """
        Record the observation/result for the current (last) step.

        Args:
            observation: Text description of what happened after the action.
        """
        if not self._steps:
            self._steps.append(ReActStep(step_n=1))

        last = self._steps[-1]
        self._steps[-1] = ReActStep(
            step_n=last.step_n,
            thought=last.thought,
            action=last.action,
            action_payload=last.action_payload,
            observation=observation,
            ts=last.ts,
        )

    # ------------------------------------------------------------------
    # Context retrieval
    # ------------------------------------------------------------------

    def get_context(self, max_steps: int = 20) -> dict[str, Any]:
        """
        Return a structured context dict suitable for LLM prompt assembly.

        Only the most recent *max_steps* are included to keep the context
        window manageable.

        Returns:
            Dict with keys: task_id, goal, started_at, step_count, steps.
        """
        recent = self._steps[-max_steps:] if len(self._steps) > max_steps else self._steps
        return {
            "task_id":     self._task_id,
            "goal":        self._goal,
            "started_at":  self._started_at,
            "step_count":  self._total_steps,  # FIX B-03: true total
            "steps": [
                {
                    "step_n":      s.step_n,
                    "thought":     s.thought,
                    "action":      s.action,
                    "observation": s.observation,
                    "ts":          s.ts,
                }
                for s in recent
            ],
        }

    def get_steps(self) -> list[ReActStep]:
        """Return a copy of all recorded ReAct steps (oldest first)."""
        return list(self._steps)

    def last_observation(self) -> str:
        """Return the observation from the most recent step, or empty string."""
        if self._steps:
            return self._steps[-1].observation
        return ""

    def step_count(self) -> int:
        """Return the total number of steps recorded (including evicted)."""
        return self._total_steps  # FIX B-03: return true total, not buffer size

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def goal(self) -> str:
        return self._goal

    def set_metadata(self, key: str, value: Any) -> None:
        """Store arbitrary task metadata (e.g. skill name, start coordinates)."""
        self._metadata[key] = value

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Retrieve task metadata by key."""
        return self._metadata.get(key, default)
