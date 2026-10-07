# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/task_executor.py — Phidipus v2.4 Roadmap C1
═══════════════════════════════════════════════════════════════════

Extracted task execution logic from agent_loop.py (1744 lines).

This module handles HOW a task is executed, after TaskRouter decides
WHERE to route it.

Execution Paths:
  P1-3: SmartActionEngine — instant regex-based actions (open app, navigate)
  P3.5: SkillIntelligence — registry lookup (cached skills, 0 API calls)
  P4:   SkillForge — Gemini code generation (10-30s)
  P4b:  TaskDecomposer — multi-step planning (5-60s)
  P5:   LLM+VLM loop — last resort (30-90s)

Architecture:
  - TaskExecutor owns SmartActionEngine, SkillForge, TaskDecomposer
  - agent_loop.py creates TaskExecutor and delegates execute() calls
  - WorkflowExecutor handles workflow-type tasks separately

Usage:
    executor = TaskExecutor(config)
    executor.inject(ipc=ipc, llm=llm, forge=forge, smart=smart, ...)
    result = await executor.execute(route_result)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable

_log = logging.getLogger("phidipus.task_executor")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;32m[{icon}]\033[0m  {msg}")


@dataclass
class ExecutionResult:
    """Result from task execution."""
    success: bool = False
    method: str = ""          # smart_action | registry_skill | forge | decomposer | llm_vlm
    output: str = ""
    error: str = ""
    steps_taken: int = 0
    duration_ms: float = 0.0
    skill_name: str = ""


class TaskExecutor:
    """
    Executes tasks using the appropriate engine based on routing decision.

    Owns references to execution engines but does NOT own their lifecycle.
    AgentLoop creates the engines; TaskExecutor uses them.
    """

    def __init__(self) -> None:
        self._smart: Any = None          # SmartActionEngine
        self._forge: Any = None          # SkillForge
        self._decomposer: Any = None     # TaskDecomposer
        self._intelligence: Any = None   # SkillIntelligence
        self._llm: Any = None            # LLMClient
        self._ipc: Any = None
        self._notify_fn: Optional[Callable] = None
        self._bus: Any = None            # EventBus
        self._monitor: Any = None        # RuntimeMonitor

    def inject(self, **components: Any) -> None:
        """Inject execution engine references from AgentLoop."""
        for key, val in components.items():
            attr = f"_{key}"
            if hasattr(self, attr):
                setattr(self, attr, val)

    # ── Main execution entry point ────────────────────────────

    async def execute(self, goal: str, task_id: str = "",
                      lane: str = "auto") -> ExecutionResult:
        """
        Execute a task goal using the best available engine.

        Args:
            goal: The task description
            task_id: Unique task identifier
            lane: Routing hint ("smart_action", "forge", "decomposer", "auto")

        Returns:
            ExecutionResult with success, method, output, etc.
        """
        t0 = time.perf_counter()

        # ── Lane: Smart Action (P1-3) ─────────────────────────
        if lane in ("smart_action", "auto") and self._smart:
            result = await self._try_smart_action(goal)
            if result:
                result.duration_ms = (time.perf_counter() - t0) * 1000
                return result

        # ── Lane: Registry Skill (P3.5) ───────────────────────
        if lane in ("registry_skill", "auto") and self._intelligence:
            result = await self._try_registry_skill(goal, task_id)
            if result:
                result.duration_ms = (time.perf_counter() - t0) * 1000
                return result

        # ── Lane: SkillForge (P4) ─────────────────────────────
        if lane in ("forge", "skill_forge", "auto") and self._forge:
            result = await self._try_skill_forge(goal)
            if result:
                result.duration_ms = (time.perf_counter() - t0) * 1000
                return result

        # ── Lane: TaskDecomposer (P4b) ────────────────────────
        if lane in ("decomposer", "auto") and self._decomposer:
            result = await self._try_decomposer(goal, task_id)
            if result:
                result.duration_ms = (time.perf_counter() - t0) * 1000
                return result

        # ── Fallback: no engine handled it ────────────────────
        elapsed = (time.perf_counter() - t0) * 1000
        _vlog("❌", f"No execution engine handled: '{goal[:50]}' (lane={lane})")
        return ExecutionResult(
            success=False,
            method="none",
            error=f"No execution engine could handle this task",
            duration_ms=elapsed,
        )

    # ── P1-3: SmartAction ─────────────────────────────────────

    async def _try_smart_action(self, goal: str) -> Optional[ExecutionResult]:
        """Try SmartActionEngine for instant pattern-based actions."""
        try:
            result = await self._smart.execute(goal)
            if result is not None:
                _vlog("⚡", f"SmartAction P{result.priority}: {result.method} "
                             f"({result.duration_ms:.0f}ms)")
                return ExecutionResult(
                    success=result.success,
                    method=f"smart_action_{result.method}",
                    output=getattr(result, "output", ""),
                    error=result.error or "",
                    steps_taken=1,
                )
        except Exception as exc:
            _vlog("⚠️", f"SmartAction error: {exc}")
        return None

    # ── P3.5: Registry Skill ──────────────────────────────────

    async def _try_registry_skill(self, goal: str, task_id: str) -> Optional[ExecutionResult]:
        """Try cached skill from SkillIntelligence registry."""
        try:
            if not self._intelligence.enabled:
                return None

            decision = self._intelligence.decide(goal)
            if decision.action != "execute_registry" or not decision.candidate:
                return None

            candidate = decision.candidate
            _vlog("📚", f"Registry: '{candidate.name}' "
                         f"(rate={candidate.success_rate:.0%}, used={candidate.usage_count}x)")

            if not self._forge:
                return None

            forge_result = await self._forge.execute(
                goal, telegram_send_fn=self._notify_fn,
            )
            if forge_result.success:
                validation = self._intelligence.validate_output(
                    forge_result.message, goal,
                )
                if validation.valid:
                    self._intelligence.record_outcome(
                        goal=goal, skill_name=candidate.skill_id,
                        success=True, runtime_ms=forge_result.duration_ms,
                        source="registry",
                    )
                    return ExecutionResult(
                        success=True,
                        method="registry_skill",
                        output=forge_result.message,
                        skill_name=candidate.name,
                        steps_taken=1,
                    )
        except Exception as exc:
            _vlog("⚠️", f"Registry skill error: {exc}")
        return None

    # ── P4: SkillForge ────────────────────────────────────────

    async def _try_skill_forge(self, goal: str) -> Optional[ExecutionResult]:
        """Try SkillForge (Gemini code generation)."""
        try:
            if not self._forge or not self._forge.enabled:
                return None

            _vlog("🔨", f"SkillForge: '{goal[:50]}'...")
            forge_result = await self._forge.execute(
                goal, telegram_send_fn=self._notify_fn,
            )
            if forge_result.success:
                _vlog("✅", f"SkillForge OK ({forge_result.duration_ms:.0f}ms)")
                return ExecutionResult(
                    success=True,
                    method="skill_forge",
                    output=forge_result.message,
                    steps_taken=1,
                )
            else:
                _vlog("⚠️", f"SkillForge failed: {forge_result.error[:80]}")
        except Exception as exc:
            _vlog("⚠️", f"SkillForge error: {exc}")
        return None

    # ── P4b: TaskDecomposer ───────────────────────────────────

    async def _try_decomposer(self, goal: str, task_id: str) -> Optional[ExecutionResult]:
        """Try TaskDecomposer for multi-step planning."""
        try:
            if not self._decomposer:
                return None

            plan = await self._decomposer.decompose(goal)
            if not plan or not plan.steps:
                return None

            _vlog("📋", f"Decomposed into {len(plan.steps)} steps")

            # Execute each step
            completed = 0
            last_output = ""
            for step in plan.steps:
                step_result = await self._execute_step(step, last_output)
                if step_result and step_result.success:
                    completed += 1
                    last_output = step_result.output
                else:
                    break

            success = completed == len(plan.steps)
            return ExecutionResult(
                success=success,
                method="decomposer",
                output=last_output,
                steps_taken=completed,
                error="" if success else f"Failed at step {completed + 1}/{len(plan.steps)}",
            )
        except Exception as exc:
            _vlog("⚠️", f"Decomposer error: {exc}")
        return None

    async def _execute_step(self, step: Any, prev_output: str) -> Optional[ExecutionResult]:
        """Execute a single decomposed step via SmartAction or SkillForge."""
        goal = getattr(step, "goal", str(step))

        # Try SmartAction first (instant)
        result = await self._try_smart_action(goal)
        if result and result.success:
            return result

        # Try SkillForge (slower but more capable)
        result = await self._try_skill_forge(goal)
        if result and result.success:
            return result

        return ExecutionResult(success=False, error=f"Step failed: {goal[:50]}")

    # ── Stats ─────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "smart_action": self._smart is not None,
            "skill_forge": self._forge is not None and self._forge.enabled,
            "decomposer": self._decomposer is not None,
            "intelligence": self._intelligence is not None and self._intelligence.enabled,
        }


# ── Singleton ─────────────────────────────────────────────────
_instance: Optional[TaskExecutor] = None

def get_task_executor() -> TaskExecutor:
    global _instance
    if _instance is None:
        _instance = TaskExecutor()
    return _instance
