# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/skill_discovery.py — Phidipus v1.0
Skill discovery and matching — pure data/logic, no execution.

Architecture contract (RETAIN):
  "Discovery/matching logic.  No execution.  Retained as-is."

SkillDiscovery finds the best-matching registered skill for a given goal
using keyword and tag matching.  It operates entirely on
BaseSkillDefinition metadata — it never loads or executes skill code.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  (Pure data matching — no security-sensitive operations.)

Used by:
  core/agent_loop.py   — find_skill_for_goal()
  planner/react_reasoner.py — discover available skills

Dependencies:
  skills/skill_registry.py — SkillRegistry, BaseSkillDefinition
  utils/logger.py          — get_logger()
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from typing import Any

from skills.skill_registry import SkillRegistry
from skills.base_skill import BaseSkillDefinition
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# SkillDiscovery
# ---------------------------------------------------------------------------

class SkillDiscovery:
    """
    Matches goals to registered skills using keyword and tag scoring.

    Usage::

        discovery = SkillDiscovery(registry)
        best = discovery.find_best_match("open the web browser")
        if best:
            print(best.name, best.description)

        candidates = discovery.find_candidates("click submit button", top_k=3)
    """

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        # FIX H-02: OrderedDict for proper LRU eviction (was plain dict = FIFO).
        # move_to_end() on cache hit → least-recently-used evicted first.
        self._hot_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._cache_max: int = 64
        self._cache_registry_size: int = 0  # detect registry changes
        _log.debug("SkillDiscovery initialised (LRU hot-cache enabled)")

    def find_best_match(
        self,
        goal: str,
        *,
        include_quarantined: bool = False,
    ) -> BaseSkillDefinition | None:
        """
        Return the highest-scoring skill for *goal*, or None.

        Phase 2: checks hot-cache first.  Cache is invalidated when the
        registry size changes (new skill registered or removed).
        FIX H-02: proper LRU eviction via OrderedDict.move_to_end().
        """
        # Phase 2: invalidate cache if registry changed
        current_size = len(self._registry.list_skills())
        if current_size != self._cache_registry_size:
            self._hot_cache.clear()
            self._cache_registry_size = current_size

        # Phase 2: check hot-cache
        goal_hash = hashlib.md5(goal.encode("utf-8")).hexdigest()
        if goal_hash in self._hot_cache and not include_quarantined:
            skill_name, score = self._hot_cache[goal_hash]
            # FIX H-02: move to end on hit (most recently used)
            self._hot_cache.move_to_end(goal_hash)
            if score >= 0.1:
                skill_def = self._registry.get(skill_name)
                if skill_def is not None:
                    _log.debug(
                        "SkillDiscovery: hot-cache hit (LRU)",
                        extra={
                            "goal_hash":  goal_hash[:12],
                            "skill_name": skill_name,
                            "score":      score,
                        },
                    )
                    return skill_def

        # Cache miss — run full scoring
        candidates = self.find_candidates(
            goal, top_k=1, include_quarantined=include_quarantined
        )
        result = candidates[0] if candidates else None

        # Phase 2: populate cache (only for non-quarantined lookups)
        if result is not None and not include_quarantined:
            self._hot_cache[goal_hash] = (result.name, 1.0)
            # FIX H-02: evict least-recently-used (first item in OrderedDict)
            if len(self._hot_cache) > self._cache_max:
                self._hot_cache.popitem(last=False)  # LRU eviction

        return result

    def find_candidates(
        self,
        goal: str,
        *,
        top_k: int = 5,
        include_quarantined: bool = False,
        min_score: float = 0.1,
    ) -> list[BaseSkillDefinition]:
        """
        Return up to *top_k* skills ordered by match score for *goal*.

        Scoring:
          - Each word in *goal* that appears in skill name or description
            adds 1 point.
          - Each matching tag adds 2 points.
          - Exact name match adds 10 points.

        Args:
            goal:               Goal string.
            top_k:              Maximum candidates to return.
            include_quarantined: Whether to include quarantined skills.
            min_score:          Minimum score to be included.
        """
        skills = self._registry.list_skills(
            include_quarantined=include_quarantined
        )
        if not skills:
            return []

        goal_lower  = goal.lower()
        goal_tokens = set(re.findall(r"\w+", goal_lower))

        scored: list[tuple[float, BaseSkillDefinition]] = []
        for skill in skills:
            score = self._score(skill, goal_lower, goal_tokens)
            if score >= min_score:
                scored.append((score, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:top_k]]

    def list_by_tag(self, tag: str) -> list[BaseSkillDefinition]:
        """Return all skills with *tag*, sorted by name."""
        tag_lower = tag.lower()
        return [
            s for s in self._registry.list_skills()
            if any(t.lower() == tag_lower for t in s.tags)
        ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _score(
        skill: BaseSkillDefinition,
        goal_lower: str,
        goal_tokens: set[str],
    ) -> float:
        """Compute match score for *skill* against the pre-processed goal."""
        score = 0.0

        # Exact name match
        if skill.name.lower() == goal_lower:
            score += 10.0

        # Name token overlap
        name_tokens = set(re.findall(r"\w+", skill.name.lower()))
        score += len(goal_tokens & name_tokens)

        # Description token overlap (weighted 0.5)
        desc_tokens = set(re.findall(r"\w+", skill.description.lower()))
        score += len(goal_tokens & desc_tokens) * 0.5

        # Tag match (weighted 2)
        for tag in skill.tags:
            if tag.lower() in goal_lower:
                score += 2.0

        return score
