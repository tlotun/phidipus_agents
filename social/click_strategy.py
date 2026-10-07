# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
social/click_strategy.py — Phidipus v2.4 Priority 3 / C2
═══════════════════════════════════════════════════════════════════

Extracted click strategy selection logic from vision_actor.py (1953 lines).

Manages the strategy cascade: which detection method to try,
in what order, with what budget constraints.

Strategy Stack:
  S0a: ClickMemory cache (0ms)
  S0b: ScreenInventory cache (0ms)
  S0c: DINO+Florence pipeline (150-650ms)
  S1:  DOM/JS selector (10ms)
  S1.5: ZoomAction 3-pass (3-8s)
  S2:  MultiModal Consensus (3-10s)
  S3:  CropLocator (2-4s)
  S4:  VLM full-screen fallback (15-25s)

This file is ADDITIVE — vision_actor.py keeps working as before.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

_log = logging.getLogger("phidipus.click_strategy")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════
# Strategy definitions
# ══════════════════════════════════════════════════════════════

@dataclass
class Strategy:
    """Definition of a click detection strategy."""
    name: str
    tier: str           # S0a, S0b, S0c, S1, S1.5, S2, S3, S4
    min_budget_ms: int  # Minimum budget required to attempt
    avg_latency_ms: int # Average latency
    max_latency_ms: int # Worst-case latency
    requires: list[str] = field(default_factory=list)  # Required components
    description: str = ""


# Strategy definitions with budget thresholds (B1 integration)
STRATEGIES = [
    Strategy("click_memory",   "S0a", 0,    0,     5,    ["click_mem"],     "ClickMemory spatial cache"),
    Strategy("screen_inventory","S0b", 0,    0,     5,    ["screen_inv"],    "ScreenInventory element cache"),
    Strategy("dino_florence",  "S0c", 200,  350,   650,  ["dino"],          "DINO+Florence detection pipeline"),
    Strategy("dom_js",         "S1",  10,   10,    50,   ["ipc", "chrome"], "DOM/JS CSS selector"),
    Strategy("zoom_action",    "S1.5",3000, 5000,  8000, ["zoom"],          "ZoomAction 3-pass adaptive"),
    Strategy("consensus",      "S2",  2000, 5000,  10000,["consensus"],     "MultiModal 3-way consensus"),
    Strategy("crop_locator",   "S3",  1500, 3000,  4000, ["crop_loc"],      "Crop-based locator near anchor"),
    Strategy("vlm_fullscreen", "S4",  1000, 15000, 25000,["vlm"],           "VLM full-screen fallback"),
]

STRATEGY_MAP = {s.name: s for s in STRATEGIES}


@dataclass
class StrategyResult:
    """Result from a strategy attempt."""
    strategy: str       # Strategy name
    found: bool = False
    x: int = 0
    y: int = 0
    confidence: float = 0.0
    method: str = ""
    latency_ms: float = 0.0


@dataclass
class BudgetTracker:
    """Tracks remaining budget for strategy cascade."""
    total_ms: float = 5000.0
    _start: float = 0.0

    def __post_init__(self):
        self._start = time.perf_counter()

    @property
    def remaining_ms(self) -> float:
        elapsed = (time.perf_counter() - self._start) * 1000
        return max(0, self.total_ms - elapsed)

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000

    def can_attempt(self, strategy: Strategy) -> bool:
        """Check if enough budget to attempt this strategy."""
        return self.remaining_ms >= strategy.min_budget_ms

    @property
    def exhausted(self) -> bool:
        return self.remaining_ms <= 0


class ClickStrategySelector:
    """
    Selects and orders strategies for finding an element.

    Considers:
      - Available components (DINO loaded? ZoomEngine available?)
      - Budget constraints (B1)
      - Strategy hints from WorkflowMemory
      - Previous attempt history
    """

    def __init__(self) -> None:
        self._available_components: set[str] = set()
        self._strategy_stats: dict[str, dict] = {}  # name → {attempts, successes, avg_ms}

    def set_available(self, components: list[str]) -> None:
        """Set which components are available (click_mem, dino, zoom, etc)."""
        self._available_components = set(components)

    def select_strategies(
        self,
        budget_ms: float = 5000,
        strategy_hint: str = "",
        has_anchor: bool = False,
        has_js_selectors: bool = False,
    ) -> list[Strategy]:
        """
        Select ordered list of strategies to try.

        Args:
            budget_ms: Total budget for all strategies
            strategy_hint: "zoom_first" or "" (from WorkflowMemory)
            has_anchor: Whether anchor coords available (for S3)
            has_js_selectors: Whether JS selectors provided (for S1)

        Returns:
            Ordered list of Strategy objects to attempt
        """
        selected = []

        for s in STRATEGIES:
            # Skip if component not available
            if s.requires and not all(c in self._available_components for c in s.requires):
                continue
            # Skip S3 if no anchor
            if s.name == "crop_locator" and not has_anchor:
                continue
            # Skip S1 if no JS selectors
            if s.name == "dom_js" and not has_js_selectors:
                continue
            selected.append(s)

        # Apply strategy hint
        if strategy_hint == "zoom_first":
            # Move zoom_action before consensus
            selected = [s for s in selected if s.name != "consensus"] + \
                       [s for s in selected if s.name == "consensus"]

        return selected

    def record_attempt(self, strategy_name: str, success: bool, latency_ms: float) -> None:
        """Record strategy attempt for future optimization."""
        stats = self._strategy_stats.setdefault(strategy_name, {
            "attempts": 0, "successes": 0, "total_ms": 0
        })
        stats["attempts"] += 1
        if success:
            stats["successes"] += 1
        stats["total_ms"] += latency_ms

    def get_stats(self) -> dict:
        """Get strategy performance stats."""
        result = {}
        for name, stats in self._strategy_stats.items():
            attempts = stats["attempts"]
            result[name] = {
                "attempts": attempts,
                "success_rate": stats["successes"] / attempts if attempts > 0 else 0,
                "avg_ms": stats["total_ms"] / attempts if attempts > 0 else 0,
            }
        return result

    def suggest_budget(self, domain: str = "", action_key: str = "") -> int:
        """Suggest optimal budget based on historical performance."""
        # If we have stats, use P95 latency of successful strategies
        if self._strategy_stats:
            best_avg = min(
                (s["total_ms"] / s["attempts"]
                 for s in self._strategy_stats.values()
                 if s["successes"] > 0 and s["attempts"] > 0),
                default=5000,
            )
            # Give 2x the average as budget (with min 2000, max 10000)
            return max(2000, min(10000, int(best_avg * 2)))
        return 5000  # default


# ── Singleton ─────────────────────────────────────────────────
_instance: Optional[ClickStrategySelector] = None

def get_strategy_selector() -> ClickStrategySelector:
    global _instance
    if _instance is None:
        _instance = ClickStrategySelector()
    return _instance
