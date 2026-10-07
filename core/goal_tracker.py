# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/goal_tracker.py — Phidipus Goal Integrity System v9.20
════════════════════════════════════════════════════════════

Prevents "Goal Drift" — agent losing focus after many steps.

Tracks:
  - Goal progress (which sub-tasks done vs remaining)
  - Alignment check per step (is this action relevant?)
  - Drift detection (agent doing unrelated things)
  - Auto-replan when drifted

Usage:
    tracker = GoalTracker()
    tracker.begin("tổng hợp giá bóng đèn từ file tồn kho")
    
    # Before each step
    aligned = tracker.check_alignment("search random files on desktop")
    # → {"aligned": False, "reason": "Không liên quan đến mục tiêu", "score": 0.2}
    
    # After each step
    tracker.record_step("T1", "find_file", success=True)
    progress = tracker.progress()
    # → {"completed": 1, "total": 5, "percent": 20.0, "remaining": ["T2","T3","T4","T5"]}
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


@dataclass
class StepRecord:
    """Record of one executed step."""
    step_id: str
    action: str
    description: str = ""
    success: bool = False
    timestamp: float = field(default_factory=time.time)
    alignment_score: float = 1.0


@dataclass
class GoalState:
    """Complete goal tracking state."""
    goal: str
    goal_keywords: set[str] = field(default_factory=set)
    sub_tasks: list[dict] = field(default_factory=list)  # From TaskPlan
    steps_done: list[StepRecord] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    drift_count: int = 0  # How many times agent drifted
    max_drift: int = 3    # Trigger replan after this many drifts


class GoalTracker:
    """
    Goal Integrity System — keeps agent focused.

    Features:
      - Extract goal keywords for alignment checking
      - Score each step's relevance to the goal
      - Track progress through sub-tasks
      - Detect drift and suggest replan
    """

    # Minimum alignment score to consider a step "on track"
    ALIGNMENT_THRESHOLD = 0.25

    def __init__(self) -> None:
        self._state: GoalState | None = None

    # ══════════════════════════════════════════════════════════
    # Lifecycle
    # ══════════════════════════════════════════════════════════

    def begin(self, goal: str, sub_tasks: list[dict] | None = None) -> None:
        """Start tracking a new goal."""
        keywords = self._extract_keywords(goal)
        self._state = GoalState(
            goal=goal,
            goal_keywords=keywords,
            sub_tasks=sub_tasks or [],
        )
        _vlog("🎯", f"Goal tracking: {len(keywords)} keywords, {len(sub_tasks or [])} sub-tasks")

    def end(self) -> dict[str, Any]:
        """End tracking and return summary."""
        if not self._state:
            return {}
        summary = {
            "goal": self._state.goal,
            "steps_done": len(self._state.steps_done),
            "drift_count": self._state.drift_count,
            "success_rate": self._step_success_rate(),
            "duration_s": round(time.time() - self._state.started_at, 1),
        }
        self._state = None
        return summary

    @property
    def active(self) -> bool:
        return self._state is not None

    # ══════════════════════════════════════════════════════════
    # Alignment Check (BEFORE executing a step)
    # ══════════════════════════════════════════════════════════

    def check_alignment(self, action_description: str) -> dict[str, Any]:
        """
        Check if a proposed action aligns with the current goal.

        Args:
            action_description: What the agent wants to do next

        Returns:
            {
                "aligned": bool,
                "score": float (0-1),
                "reason": str,
                "drift_count": int,
            }
        """
        if not self._state:
            return {"aligned": True, "score": 1.0, "reason": "No goal tracking", "drift_count": 0}

        score = self._alignment_score(action_description)
        aligned = score >= self.ALIGNMENT_THRESHOLD

        if not aligned:
            self._state.drift_count += 1
            reason = f"Hành động '{action_description[:40]}' không liên quan đến mục tiêu '{self._state.goal[:40]}'"
            _vlog("⚠️", f"Goal drift #{self._state.drift_count}: {reason[:60]}")
        else:
            reason = "Phù hợp với mục tiêu"

        return {
            "aligned": aligned,
            "score": round(score, 2),
            "reason": reason,
            "drift_count": self._state.drift_count,
            "should_replan": self._state.drift_count >= self._state.max_drift,
        }

    def _alignment_score(self, action_desc: str) -> float:
        """Score how well an action aligns with the goal (0-1)."""
        if not self._state:
            return 1.0

        action_words = set(re.findall(r'\w+', action_desc.lower()))
        goal_words = self._state.goal_keywords

        if not action_words or not goal_words:
            return 0.5  # Can't determine — assume OK

        # Word overlap
        overlap = action_words & goal_words
        score = len(overlap) / max(len(goal_words), 1)

        # Boost for action-type words that are always relevant
        always_relevant = {"tìm", "search", "find", "read", "đọc", "open", "mở",
                          "create", "tạo", "send", "gửi", "filter", "lọc",
                          "export", "save", "lưu", "write", "viết"}
        if action_words & always_relevant:
            score = min(1.0, score + 0.15)

        # Penalty for completely unrelated actions
        if not overlap and not (action_words & always_relevant):
            score = max(0.0, score - 0.2)

        return min(1.0, max(0.0, score))

    # ══════════════════════════════════════════════════════════
    # Progress Tracking (AFTER executing a step)
    # ══════════════════════════════════════════════════════════

    def record_step(self, step_id: str, action: str, success: bool,
                    description: str = "", alignment_score: float = 1.0) -> None:
        """Record a completed step."""
        if not self._state:
            return

        self._state.steps_done.append(StepRecord(
            step_id=step_id,
            action=action,
            description=description,
            success=success,
            alignment_score=alignment_score,
        ))

        # Mark sub-task as done if matching
        for st in self._state.sub_tasks:
            if st.get("id") == step_id:
                st["status"] = "done" if success else "failed"
                break

    def progress(self) -> dict[str, Any]:
        """Get current progress toward goal completion."""
        if not self._state:
            return {"completed": 0, "total": 0, "percent": 0.0}

        total = len(self._state.sub_tasks)
        if total == 0:
            # No sub-tasks — estimate from steps done
            done = len([s for s in self._state.steps_done if s.success])
            return {
                "completed": done,
                "total": "unknown",
                "percent": 0.0,
                "steps_done": done,
            }

        completed = sum(1 for t in self._state.sub_tasks if t.get("status") == "done")
        failed = sum(1 for t in self._state.sub_tasks if t.get("status") == "failed")
        remaining = [t.get("id", "?") for t in self._state.sub_tasks
                     if t.get("status") not in ("done", "failed")]

        return {
            "completed": completed,
            "failed": failed,
            "total": total,
            "percent": round(completed / total * 100, 1) if total > 0 else 0.0,
            "remaining": remaining,
            "drift_count": self._state.drift_count,
        }

    def progress_bar(self) -> str:
        """Visual progress bar for terminal."""
        p = self.progress()
        total = p.get("total", 0)
        if total == 0 or total == "unknown":
            return ""
        done = p["completed"]
        bar_len = 20
        filled = int(bar_len * done / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        return f"[{bar}] {done}/{total} ({p['percent']}%)"

    # ══════════════════════════════════════════════════════════
    # Keyword extraction
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _extract_keywords(goal: str) -> set[str]:
        """Extract meaningful keywords from goal (Vietnamese + English)."""
        # Remove common stop words
        stop_words = {
            "tôi", "cho", "của", "và", "rồi", "sau", "đó", "này", "đến",
            "từ", "trong", "với", "một", "các", "những", "được", "là", "có",
            "the", "a", "an", "is", "are", "to", "from", "in", "on", "for",
            "and", "or", "then", "it", "my", "me", "i", "please",
        }
        words = set(re.findall(r'\w+', goal.lower()))
        return words - stop_words

    def _step_success_rate(self) -> float:
        """Calculate step success rate."""
        if not self._state or not self._state.steps_done:
            return 0.0
        successes = sum(1 for s in self._state.steps_done if s.success)
        return round(successes / len(self._state.steps_done), 2)

    # ══════════════════════════════════════════════════════════
    # Stats
    # ══════════════════════════════════════════════════════════

    def stats(self) -> dict:
        if not self._state:
            return {"active": False}
        return {
            "active": True,
            "goal": self._state.goal[:60],
            "progress": self.progress(),
            "drift_count": self._state.drift_count,
            "steps_done": len(self._state.steps_done),
        }
