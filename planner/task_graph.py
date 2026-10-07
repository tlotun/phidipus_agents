# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
planner/task_graph.py — Phidipus v1.0
Goal decomposition and task graph management — pure data, no execution.

Architecture contract (RETAIN):
  "Pure data structure.  Goal decomposition and task graph management.
   No execution.  No changes required."

TaskGraph decomposes a high-level goal into an ordered directed acyclic
graph (DAG) of subtasks.  It is a pure data structure — no LLM calls,
no IPC dispatch, no OS access.  The agent loop and planner read from the
graph; only the planner writes to it.

Each node in the graph represents a subtask with:
  - A unique ID
  - A description (sanitized goal fragment)
  - Dependencies (IDs of subtasks that must complete first)
  - Status: pending / running / done / failed

The graph supports simple topological ordering so the agent loop can
always find the next executable subtask (one whose dependencies are all
done).

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  (Pure data — no security-sensitive operations.)

Used by:
  planner/react_reasoner.py — add_subtask(), get_next_task()
  core/agent_loop.py        — get_next_task(), mark_done(), mark_failed()

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_PENDING: str = "pending"
STATUS_RUNNING: str = "running"
STATUS_DONE:    str = "done"
STATUS_FAILED:  str = "failed"

_VALID_STATUSES = frozenset({STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TaskGraphError(ValueError):
    """
    Raised on invalid TaskGraph operations (cycles, unknown IDs, etc.).

    Attributes:
        task_id: The task ID involved (may be empty).
        reason:  Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str = "",
        reason:  str = "GRAPH_ERROR",
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.reason  = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.task_id:
            parts.append(f"task_id={self.task_id!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# TaskNode
# ---------------------------------------------------------------------------

@dataclass
class TaskNode:
    """
    A single node in the task DAG.

    Attributes:
        task_id:      Unique identifier (e.g. "t-001").
        description:  Human-readable subtask description.
        status:       One of pending / running / done / failed.
        deps:         Set of task_ids that must be STATUS_DONE before
                      this task can be started.
        result:       Optional result string from execution.
        error:        Error description if status == failed.
    """

    task_id:     str
    description: str
    status:      str       = STATUS_PENDING
    deps:        set[str]  = field(default_factory=set)
    result:      str       = ""
    error:       str       = ""


# ---------------------------------------------------------------------------
# TaskGraph
# ---------------------------------------------------------------------------

class TaskGraph:
    """
    Directed acyclic graph of subtasks derived from a goal.

    Supports topological ordering to determine which subtasks are
    ready to execute (all dependencies satisfied).

    Usage::

        graph = TaskGraph(goal="Open browser and search for Python")
        graph.add_task("t-1", "Open browser application")
        graph.add_task("t-2", "Navigate to search engine", deps={"t-1"})
        graph.add_task("t-3", "Type 'Python' in search box", deps={"t-2"})

        next_task = graph.get_next_ready()   # → TaskNode(task_id="t-1", ...)
        graph.mark_running("t-1")
        graph.mark_done("t-1", result="Browser opened")
        next_task = graph.get_next_ready()   # → TaskNode(task_id="t-2", ...)

    Args:
        goal: The top-level goal this graph was created to accomplish.
    """

    def __init__(self, goal: str = "") -> None:
        self._goal:  str                       = goal
        self._nodes: dict[str, TaskNode]       = {}
        self._order: list[str]                 = []   # insertion order

        _log.debug(
            "TaskGraph created",
            extra={"goal_len": len(goal)},
        )

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def add_task(
        self,
        task_id:     str,
        description: str,
        *,
        deps: set[str] | None = None,
    ) -> TaskNode:
        """
        Add a new subtask node to the graph.

        Args:
            task_id:     Unique identifier for this subtask.
            description: Human-readable description.
            deps:        Set of task_ids that must complete before this one.

        Returns:
            The newly created TaskNode.

        Raises:
            TaskGraphError: if task_id already exists, or a dep is unknown.
        """
        if task_id in self._nodes:
            raise TaskGraphError(
                f"Task {task_id!r} already exists in the graph.",
                task_id=task_id,
                reason="DUPLICATE_ID",
            )
        dep_set = set(deps) if deps else set()
        for dep in dep_set:
            if dep not in self._nodes:
                raise TaskGraphError(
                    f"Dependency {dep!r} of task {task_id!r} is not in the graph. "
                    "Add dependency tasks before their dependants.",
                    task_id=task_id,
                    reason="UNKNOWN_DEP",
                )
        node = TaskNode(task_id=task_id, description=description, deps=dep_set)
        self._nodes[task_id] = node
        self._order.append(task_id)
        _log.debug(
            "TaskGraph: task added",
            extra={"task_id": task_id, "deps": list(dep_set)},
        )
        return node

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def mark_running(self, task_id: str) -> None:
        """Transition *task_id* from pending → running."""
        node = self._get(task_id)
        if node.status != STATUS_PENDING:
            raise TaskGraphError(
                f"Cannot start task {task_id!r}: status is {node.status!r}.",
                task_id=task_id,
                reason="INVALID_TRANSITION",
            )
        node.status = STATUS_RUNNING

    def mark_done(self, task_id: str, *, result: str = "") -> None:
        """Transition *task_id* from running → done."""
        node = self._get(task_id)
        if node.status != STATUS_RUNNING:
            raise TaskGraphError(
                f"Cannot complete task {task_id!r}: status is {node.status!r}.",
                task_id=task_id,
                reason="INVALID_TRANSITION",
            )
        node.status = STATUS_DONE
        node.result = result
        _log.debug("TaskGraph: task done", extra={"task_id": task_id})

    def mark_failed(self, task_id: str, *, error: str = "") -> None:
        """Transition *task_id* to failed (from any non-done state)."""
        node = self._get(task_id)
        if node.status == STATUS_DONE:
            raise TaskGraphError(
                f"Cannot fail task {task_id!r}: already done.",
                task_id=task_id,
                reason="INVALID_TRANSITION",
            )
        node.status = STATUS_FAILED
        node.error  = error
        _log.warning("TaskGraph: task failed", extra={"task_id": task_id, "error": error[:128]})

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_next_ready(self) -> TaskNode | None:
        """
        Return the first pending task whose dependencies are all done.

        Returns None if no such task exists (graph complete, blocked, or empty).
        """
        for task_id in self._order:
            node = self._nodes[task_id]
            if node.status != STATUS_PENDING:
                continue
            if all(self._nodes[dep].status == STATUS_DONE for dep in node.deps):
                return node
        return None

    def is_complete(self) -> bool:
        """Return True if all tasks are STATUS_DONE."""
        return all(n.status == STATUS_DONE for n in self._nodes.values())

    def has_failure(self) -> bool:
        """Return True if any task is STATUS_FAILED."""
        return any(n.status == STATUS_FAILED for n in self._nodes.values())

    def get_task(self, task_id: str) -> TaskNode | None:
        """Return the TaskNode for *task_id*, or None if not found.

        FIX B-05: return None instead of raising TaskGraphError for missing
        task_id — matches the expected ``TaskNode | None`` return type.
        Callers can safely use ``if task is None:`` pattern.
        """
        return self._nodes.get(task_id)  # FIX B-05: dict.get returns None

    def all_tasks(self) -> Iterator[TaskNode]:
        """Yield all TaskNodes in insertion order."""
        for tid in self._order:
            yield self._nodes[tid]

    @property
    def goal(self) -> str:
        return self._goal

    @property
    def task_count(self) -> int:
        return len(self._nodes)

    def summary(self) -> dict[str, int]:
        """Return counts of tasks in each status."""
        counts: dict[str, int] = {s: 0 for s in _VALID_STATUSES}
        for node in self._nodes.values():
            counts[node.status] += 1
        return counts

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, task_id: str) -> TaskNode:
        if task_id not in self._nodes:
            raise TaskGraphError(
                f"Unknown task_id {task_id!r}.",
                task_id=task_id,
                reason="UNKNOWN_TASK",
            )
        return self._nodes[task_id]
