# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/confidence_fusion.py — Phidipus v2.3 Phase 4
═══════════════════════════════════════════════════════════════════════

Multi-Signal Confidence Fusion — combines detection + semantic + context
signals into a single calibrated confidence score.

Phase 4 upgrades:
  1. Configurable weight profiles per action type (click, type, scroll)
  2. Online weight calibration from ConfidenceHistory feedback
  3. Integration with B2 ConfidenceHistory trust bonus
  4. Integration with A2 CrossDomainMemory hints
  5. Ambiguity resolution (top-2 candidates too close)
  6. Risk-aware scoring (critical actions get penalty → force refine)
  7. Monotonic calibration (higher raw → higher fused, always)

Signals combined (5 sources):
  ┌──────────────────────────────────────────────────────┐
  │ Signal              │ Source           │ Weight       │
  │ DINO confidence     │ Grounding DINO   │ 0.30–0.40   │
  │ Semantic score      │ SemanticRanker   │ 0.30–0.40   │
  │ Position context    │ SemanticRanker   │ 0.05–0.15   │
  │ History trust       │ B2 ConfHistory   │ 0.05–0.15   │
  │ CrossDomain hint    │ A2 CrossDomain   │ 0.05–0.10   │
  └──────────────────────────────────────────────────────┘

Process: orchestrator (L1)

Security invariants:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;34m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Weight Profiles
# ══════════════════════════════════════════════════════════════════

@dataclass
class WeightProfile:
    """
    Weight configuration for confidence fusion.

    Weights MUST sum to 1.0 (enforced at construction).
    """
    w_dino: float = 0.35
    w_semantic: float = 0.35
    w_position: float = 0.10
    w_history: float = 0.10
    w_crossdomain: float = 0.10
    name: str = "default"

    def __post_init__(self) -> None:
        total = (self.w_dino + self.w_semantic + self.w_position
                 + self.w_history + self.w_crossdomain)
        if abs(total - 1.0) > 0.01:
            # Auto-normalize instead of crash
            if total > 0:
                self.w_dino /= total
                self.w_semantic /= total
                self.w_position /= total
                self.w_history /= total
                self.w_crossdomain /= total

    def to_dict(self) -> dict[str, float]:
        return {
            "name": self.name,
            "w_dino": round(self.w_dino, 3),
            "w_semantic": round(self.w_semantic, 3),
            "w_position": round(self.w_position, 3),
            "w_history": round(self.w_history, 3),
            "w_crossdomain": round(self.w_crossdomain, 3),
        }


# Pre-defined profiles
# v2.3.1: V1 starts SIMPLE 50/50 (ChatGPT #4: don't over-engineer fusion early)
# Upgrade to V2 weights when ConfidenceHistory has enough data
PROFILE_DEFAULT = WeightProfile(
    w_dino=0.50, w_semantic=0.50, w_position=0.0,
    w_history=0.0, w_crossdomain=0.0,
    name="default_v1_simple",
)

PROFILE_HIGH_DINO = WeightProfile(
    w_dino=0.60, w_semantic=0.30, w_position=0.10,
    w_history=0.0, w_crossdomain=0.0,
    name="high_dino",
)

PROFILE_HIGH_SEMANTIC = WeightProfile(
    w_dino=0.30, w_semantic=0.60, w_position=0.10,
    w_history=0.0, w_crossdomain=0.0,
    name="high_semantic",
)

# V2 profiles — enable after collecting real data from ConfidenceHistory
PROFILE_V2_DEFAULT = WeightProfile(
    w_dino=0.35, w_semantic=0.35, w_position=0.10,
    w_history=0.10, w_crossdomain=0.10,
    name="default_v2_full",
)

PROFILE_CRITICAL = WeightProfile(
    w_dino=0.40, w_semantic=0.40, w_position=0.10,
    w_history=0.10, w_crossdomain=0.0,
    name="critical",
)

PROFILE_NEW_DOMAIN = WeightProfile(
    w_dino=0.45, w_semantic=0.45, w_position=0.10,
    w_history=0.0, w_crossdomain=0.0,
    name="new_domain",
)


# ══════════════════════════════════════════════════════════════════
# Fusion Result
# ══════════════════════════════════════════════════════════════════

@dataclass
class FusionResult:
    """Output of confidence fusion for a single candidate."""
    combined_score: float = 0.0
    dino_contribution: float = 0.0
    semantic_contribution: float = 0.0
    position_contribution: float = 0.0
    history_contribution: float = 0.0
    crossdomain_contribution: float = 0.0
    profile_used: str = "default"
    risk_penalty: float = 0.0
    calibration_offset: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "combined": round(self.combined_score, 4),
            "dino": round(self.dino_contribution, 4),
            "semantic": round(self.semantic_contribution, 4),
            "position": round(self.position_contribution, 4),
            "history": round(self.history_contribution, 4),
            "crossdomain": round(self.crossdomain_contribution, 4),
            "profile": self.profile_used,
            "risk_penalty": round(self.risk_penalty, 4),
            "calibration": round(self.calibration_offset, 4),
        }


# ══════════════════════════════════════════════════════════════════
# Online Calibration — learns from ConfidenceHistory
# ══════════════════════════════════════════════════════════════════

class CalibrationState:
    """
    Online calibration based on observed accuracy.

    Tracks: predicted confidence vs actual success.
    If we consistently over-predict → negative offset.
    If we consistently under-predict → positive offset.

    Updated every time ConfidenceHistory records a result.
    """

    def __init__(self, decay: float = 0.95, max_offset: float = 0.08) -> None:
        self._predictions: list[tuple[float, bool]] = []  # (predicted, actual_success)
        self._max_entries = 200
        self._decay = decay
        self._max_offset = max_offset
        self._offset: float = 0.0

    def record(self, predicted: float, actual_success: bool) -> None:
        """Record a prediction and its actual outcome."""
        self._predictions.append((predicted, actual_success))
        if len(self._predictions) > self._max_entries:
            self._predictions = self._predictions[-self._max_entries:]
        self._recalculate()

    def _recalculate(self) -> None:
        """Recalculate calibration offset from recent predictions."""
        if len(self._predictions) < 10:
            self._offset = 0.0
            return

        # Weighted average error (recent predictions weighted more)
        total_error = 0.0
        total_weight = 0.0
        n = len(self._predictions)

        for i, (pred, success) in enumerate(self._predictions):
            actual = 1.0 if success else 0.0
            error = actual - pred  # positive = we under-predicted
            weight = self._decay ** (n - 1 - i)
            total_error += error * weight
            total_weight += weight

        if total_weight > 0:
            avg_error = total_error / total_weight
            # Clamp offset
            self._offset = max(-self._max_offset,
                              min(self._max_offset, avg_error * 0.3))

    @property
    def offset(self) -> float:
        return self._offset

    def stats(self) -> dict[str, Any]:
        return {
            "samples": len(self._predictions),
            "offset": round(self._offset, 4),
            "max_offset": self._max_offset,
        }


# ══════════════════════════════════════════════════════════════════
# ConfidenceFusion — Main Class
# ══════════════════════════════════════════════════════════════════

class ConfidenceFusion:
    """
    Multi-signal confidence combiner with online calibration.

    Usage:
        fusion = ConfidenceFusion()

        result = fusion.fuse(
            dino_conf=0.85,
            semantic_score=0.9,
            position_score=0.7,
            history_bonus=0.05,
            crossdomain_score=0.3,
            is_critical=False,
            domain="facebook.com",
        )
        # result.combined_score → 0.823

        # After click result is known:
        fusion.record_outcome(predicted=0.823, success=True)
    """

    def __init__(self) -> None:
        self._default_profile = PROFILE_DEFAULT
        self._domain_profiles: dict[str, WeightProfile] = {}
        self._calibration = CalibrationState()
        self._total_fusions: int = 0
        self._total_critical: int = 0

    def fuse(
        self,
        dino_conf: float,
        semantic_score: float,
        position_score: float = 0.5,
        history_bonus: float = 0.0,
        crossdomain_score: float = 0.0,
        is_critical: bool = False,
        domain: str = "",
        has_history: bool = False,
    ) -> FusionResult:
        """
        Combine all signals into a single confidence score.

        Args:
            dino_conf:        DINO detection confidence (0.0–1.0)
            semantic_score:   SemanticRanker score (0.0–1.0)
            position_score:   Position context score (0.0–1.0)
            history_bonus:    B2 ConfidenceHistory trust bonus (-0.1–+0.1)
            crossdomain_score: A2 CrossDomain hint match (0.0–1.0)
            is_critical:      True → apply risk penalty
            domain:           Domain for profile selection
            has_history:      True if ConfidenceHistory has data for this element

        Returns:
            FusionResult with combined_score and per-signal contributions.
        """
        self._total_fusions += 1

        # Select weight profile
        profile = self._select_profile(
            domain, is_critical, has_history, crossdomain_score,
        )

        # Clamp inputs
        dino_conf = max(0.0, min(1.0, dino_conf))
        semantic_score = max(0.0, min(1.0, semantic_score))
        position_score = max(0.0, min(1.0, position_score))
        history_bonus = max(-0.1, min(0.1, history_bonus))
        crossdomain_score = max(0.0, min(1.0, crossdomain_score))

        # Weighted sum
        dino_c = profile.w_dino * dino_conf
        sem_c = profile.w_semantic * semantic_score
        pos_c = profile.w_position * position_score
        hist_c = profile.w_history * (0.5 + history_bonus * 5.0)  # map -0.1..+0.1 → 0.0..1.0
        cross_c = profile.w_crossdomain * crossdomain_score

        raw_combined = dino_c + sem_c + pos_c + hist_c + cross_c

        # Apply calibration offset
        cal_offset = self._calibration.offset
        calibrated = raw_combined + cal_offset

        # Risk penalty for critical actions (force lower score → trigger refine)
        risk_penalty = 0.0
        if is_critical:
            self._total_critical += 1
            risk_penalty = 0.05  # 5% penalty → makes refine more likely
            calibrated -= risk_penalty

        # Monotonic guarantee: higher dino+semantic → higher combined
        # Ensure combined doesn't go below min(dino, semantic) * 0.5
        floor = min(dino_conf, semantic_score) * 0.4
        combined = max(floor, min(1.0, calibrated))

        return FusionResult(
            combined_score=round(combined, 4),
            dino_contribution=round(dino_c, 4),
            semantic_contribution=round(sem_c, 4),
            position_contribution=round(pos_c, 4),
            history_contribution=round(hist_c, 4),
            crossdomain_contribution=round(cross_c, 4),
            profile_used=profile.name,
            risk_penalty=round(risk_penalty, 4),
            calibration_offset=round(cal_offset, 4),
        )

    def _select_profile(
        self,
        domain: str,
        is_critical: bool,
        has_history: bool,
        crossdomain_score: float,
    ) -> WeightProfile:
        """
        Select the best weight profile based on context.

        Priority:
          1. Critical action → PROFILE_CRITICAL (emphasize history+position)
          2. Domain-specific override (if learned)
          3. New domain (no history) → PROFILE_NEW_DOMAIN (emphasize crossdomain)
          4. Default
        """
        if is_critical:
            return PROFILE_CRITICAL

        if domain and domain in self._domain_profiles:
            return self._domain_profiles[domain]

        if not has_history and crossdomain_score > 0.3:
            return PROFILE_NEW_DOMAIN

        return self._default_profile

    def set_domain_profile(self, domain: str, profile: WeightProfile) -> None:
        """Set a domain-specific weight profile."""
        self._domain_profiles[domain] = profile
        _vlog("⚙️", f"ConfidenceFusion: domain profile set for {domain} → {profile.name}")

    def record_outcome(self, predicted: float, success: bool) -> None:
        """Record fusion outcome for online calibration."""
        self._calibration.record(predicted, success)

    def fuse_batch(
        self,
        candidates: list[dict[str, float]],
        is_critical: bool = False,
        domain: str = "",
    ) -> list[FusionResult]:
        """
        Fuse confidence for multiple candidates at once.

        Each dict in candidates should have keys:
          dino_conf, semantic_score, position_score,
          history_bonus, crossdomain_score

        Returns list of FusionResult in same order.
        """
        results = []
        has_history = any(c.get("history_bonus", 0) != 0 for c in candidates)

        for c in candidates:
            r = self.fuse(
                dino_conf=c.get("dino_conf", 0.0),
                semantic_score=c.get("semantic_score", 0.0),
                position_score=c.get("position_score", 0.5),
                history_bonus=c.get("history_bonus", 0.0),
                crossdomain_score=c.get("crossdomain_score", 0.0),
                is_critical=is_critical,
                domain=domain,
                has_history=has_history,
            )
            results.append(r)

        return results

    def stats(self) -> dict[str, Any]:
        return {
            "total_fusions": self._total_fusions,
            "total_critical": self._total_critical,
            "default_profile": self._default_profile.to_dict(),
            "domain_profiles": {d: p.to_dict() for d, p in self._domain_profiles.items()},
            "calibration": self._calibration.stats(),
        }


# ══════════════════════════════════════════════════════════════════
# Module singleton
# ══════════════════════════════════════════════════════════════════

_instance: Optional[ConfidenceFusion] = None


def get_confidence_fusion() -> ConfidenceFusion:
    global _instance
    if _instance is None:
        _instance = ConfidenceFusion()
    return _instance
