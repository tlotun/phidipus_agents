# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
evolution/mutation_scorer.py — Phidipus v1.0
Scores mutation candidates based on execution results, not docstrings.

Architecture contract (M-2):
  "Replace BoW cosine scoring with execution-result verification (M-2).
   Score based on actual task output, not docstring similarity."

MutationScorer assigns a quality score (0.0–1.0) to a validated mutation
candidate.  The score is based on:
  1. Gate 2 execution outcome (did the skill run without error?)
  2. Output schema compliance (does the output match the expected schema?)
  3. Code quality heuristics (length, complexity, import cleanliness)

Deliberately NOT used for scoring:
  - Bag-of-Words cosine similarity against the original skill docstring
    (this was the v9.10 approach, superseded by M-2)
  - AST similarity metrics (gaming-prone)

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.

Used by:
  evolution/evolution_engine.py — score() to rank candidates

Dependencies:
  skill_validator/schema_gate.py — ValidationResult
  utils/logger.py                — get_logger()
  config/config_loader.py        — PhidipusConfig

FIX (BUG-6 / MINOR): `import json as _json` was inside a static method.
  Moved to module level per project coding style.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.config_loader import PhidipusConfig
from skill_validator.schema_gate import ValidationResult
from utils.json_utils import loads as _json_loads  # Bug #15 fix: use json_utils
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Score weights
# ---------------------------------------------------------------------------

_W_GATE2_PASS:    float = 0.40   # Gate 2 passed (execution OK)
_W_SCHEMA_MATCH:  float = 0.30   # Output has success + result fields
_W_CODE_QUALITY:  float = 0.20   # Clean imports, reasonable length
_W_NO_VIOLATIONS: float = 0.10   # Gate 1 passed with zero violations


# ---------------------------------------------------------------------------
# MutationScorer
# ---------------------------------------------------------------------------

class MutationScorer:
    """
    Scores mutation candidates based on actual execution results (M-2).

    Usage::

        scorer = MutationScorer(cfg)
        score  = scorer.score(
            validation=validation_result,
            original_source=original_source,
            goal="open browser",
        )
        # score in [0.0, 1.0]

    Args:
        cfg: Validated PhidipusConfig (for min_acceptance_score).
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._min_score = cfg.evolution.min_acceptance_score
        _log.debug("MutationScorer initialised", extra={"min_score": self._min_score})

    def score(
        self,
        validation:      ValidationResult,
        original_source: str,
        goal:            str = "",
    ) -> float:
        """
        Compute a quality score for *validation* (M-2: execution-based).

        Args:
            validation:      ValidationResult from all 3 gates.
            original_source: Original skill source (for length comparison).
            goal:            Task goal (unused currently, for future use).

        Returns:
            Float score in [0.0, 1.0].
        """
        score = 0.0

        # 1. Gate 2 pass = execution succeeded in Docker
        score += _W_GATE2_PASS * self._gate2_score(validation)

        # 2. Output schema match
        score += _W_SCHEMA_MATCH * self._schema_score(validation)

        # 3. Code quality
        score += _W_CODE_QUALITY * self._quality_score(validation, original_source)

        # 4. Gate 1 violations (0 violations = full weight)
        score += _W_NO_VIOLATIONS * self._violation_score(validation)

        score = max(0.0, min(1.0, score))

        _log.debug(
            "MutationScorer: score computed",
            extra={
                "skill_name": validation.skill_name,
                "score":      round(score, 3),
            },
        )
        return score

    # ------------------------------------------------------------------
    # Sub-scorers
    # ------------------------------------------------------------------

    @staticmethod
    def _gate2_score(validation: ValidationResult) -> float:
        """1.0 if Gate 2 exit_code == 0, 0.0 otherwise."""
        return 1.0 if validation.gate2.exit_code == 0 else 0.0

    @staticmethod
    def _schema_score(validation: ValidationResult) -> float:
        """
        1.0 if Gate 2 stdout contains {"success": ..., "result": ...}.
        0.5 if only one required field is present.
        0.0 if stdout cannot be parsed as JSON.

        FIX: _json is now imported at module level — not inside this method.
        """
        try:
            stdout = validation.gate2.stdout_raw.strip()
            obj    = _json_loads(stdout)
            if isinstance(obj, dict):
                has_success = "success" in obj
                has_result  = "result" in obj
                if has_success and has_result:
                    return 1.0
                if has_success or has_result:
                    return 0.5
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _quality_score(
        validation:      ValidationResult,
        original_source: str,
    ) -> float:
        """
        Heuristic code quality score based on length ratio vs original.

        Mutations of a similar length to the original (0.5×–2×) receive
        full quality credit.  Unusually short or extremely long mutations
        receive reduced credit.
        """
        try:
            new_len  = len(validation.gate1.source_bytes)
            orig_len = len(original_source.encode("utf-8"))
            if orig_len == 0:
                return 0.5
            ratio = new_len / max(orig_len, 1)
            if 0.5 <= ratio <= 2.0:
                return 1.0
            elif 0.3 <= ratio <= 3.0:
                return 0.6
            else:
                return 0.2
        except Exception:
            return 0.5

    @staticmethod
    def _violation_score(validation: ValidationResult) -> float:
        """1.0 if gate1 passed cleanly (taint_names empty), 0.5 otherwise."""
        if not validation.gate1.taint_names:
            return 1.0
        return 0.5
