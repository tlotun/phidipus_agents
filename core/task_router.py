# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/task_router.py — Phidipus v2.4 Priority 3 / C1
═══════════════════════════════════════════════════════════════════

Extracted task routing logic from agent_loop.py (1697 lines → modular).

This module handles the decision: "given a user goal, which execution
path should handle it?"

Routing priority:
  P0: Workflow match (taught workflows from Workflow Teacher)
  P1: SmartAction instant patterns (open app, navigate, screenshot)
  P2: SemanticRouter classification (intent + lane detection)
  P3: SkillForge code generation (Gemini)
  P4: TaskDecomposer multi-step planning
  P5: LLM + VLM loop (last resort)

This file is ADDITIVE — agent_loop.py continues to work as before.
Over time, agent_loop.py can delegate to TaskRouter for cleaner code.

Usage:
    router = TaskRouter(config)
    route = await router.route("báo cáo doanh số hôm nay")
    # route.lane = "workflow" | "smart_action" | "skill_forge" | "decomposer" | "explorer"
    # route.workflow = matched workflow dict (if lane == "workflow")
    # route.variables = extracted variables
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

_log = logging.getLogger("phidipus.task_router")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


@dataclass
class RouteResult:
    """Result of task routing decision."""
    lane: str                           # "workflow" | "smart_action" | "skill_forge" | "decomposer" | "explorer"
    goal: str                           # Original or modified goal
    confidence: float = 0.0             # Routing confidence
    workflow: Optional[dict] = None     # Matched workflow (if lane == "workflow")
    variables: dict = field(default_factory=dict)
    intent: Optional[Any] = None        # Parsed intent (from SemanticRouter)
    elapsed_ms: float = 0.0


class TaskRouter:
    """
    Decides which execution path handles a given goal.

    Checks in order:
      1. Workflow match (exact trigger phrase or fuzzy match)
      2. SmartAction patterns (regex-based instant actions)
      3. SemanticRouter (LLM-based intent classification)
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._smart_engine = None
        self._semantic_router = None

    def inject(self, smart_engine=None, semantic_router=None) -> None:
        if smart_engine:
            self._smart_engine = smart_engine
        if semantic_router:
            self._semantic_router = semantic_router

    async def route(self, goal: str) -> RouteResult:
        """
        Route a goal to the best execution lane.

        Returns RouteResult with lane + metadata.
        """
        t0 = time.perf_counter()
        goal = goal.strip()

        if not goal:
            return RouteResult(lane="error", goal=goal, confidence=0)

        # ── P0: Workflow match ────────────────────────────────
        try:
            # v4.3: real match score (the old code read a "_score" key that the
            # matcher never set, so every match reported 0.00 / confidence 0.8)
            from core.workflow_executor import find_matching_workflow_scored
            wf, wf_vars, wf_score = find_matching_workflow_scored(goal)
            if wf:
                elapsed = (time.perf_counter() - t0) * 1000
                _vlog("📋", f"Route → workflow: '{wf.get('name', '?')}' (score={wf_score:.2f})")
                return RouteResult(
                    lane="workflow",
                    goal=goal,
                    confidence=wf_score,
                    workflow=wf,
                    variables=wf_vars,
                    elapsed_ms=elapsed,
                )
        except ImportError:
            pass

        # ── P1: SmartAction pattern match ─────────────────────
        if self._smart_engine:
            try:
                matched = self._smart_engine.match(goal)
                if matched:
                    elapsed = (time.perf_counter() - t0) * 1000
                    _vlog("⚡", f"Route → smart_action: '{matched.get('action', '?')}'")
                    return RouteResult(
                        lane="smart_action",
                        goal=goal,
                        confidence=0.9,
                        elapsed_ms=elapsed,
                    )
            except Exception:
                pass

        # ── P2: SemanticRouter classification ─────────────────
        if self._semantic_router:
            try:
                sr_result = await self._semantic_router.route(goal)
                if sr_result and sr_result.lane_used:
                    elapsed = (time.perf_counter() - t0) * 1000
                    _vlog("🔍", f"Route → {sr_result.lane_used}: "
                                 f"intent={getattr(sr_result, 'intent', '?')}")
                    return RouteResult(
                        lane=sr_result.lane_used,
                        goal=goal,
                        confidence=getattr(sr_result, 'confidence', 0.7),
                        intent=getattr(sr_result, 'intent', None),
                        elapsed_ms=elapsed,
                    )
            except Exception:
                pass

        # ── Default: explorer (LLM-based execution) ──────────
        elapsed = (time.perf_counter() - t0) * 1000
        _vlog("🧭", f"Route → explorer (default): '{goal[:40]}'")
        return RouteResult(
            lane="explorer",
            goal=goal,
            confidence=0.5,
            elapsed_ms=elapsed,
        )

    def is_simple_command(self, goal: str) -> bool:
        """Check if goal matches simple instant-action patterns."""
        simple_patterns = [
            r"^(mở|open|launch)\s+",
            r"^(chụp|screenshot|capture)\b",
            r"^(gõ|type|nhập)\s+",
            r"^(click|bấm|nhấn)\s+",
        ]
        for pat in simple_patterns:
            if re.match(pat, goal, re.I):
                return True
        return False
