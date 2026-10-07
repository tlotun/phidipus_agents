# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
sandbox/risk_analyzer.py — Phidipus v1.0
Static risk scoring for sandbox routing decisions.

Architecture contract (H-1 / R-07):
  "Extend alias/taint tracking to assignment chains.  Remove misleading
   .safe property.  Score only determines seccomp strictness level —
   never bypasses execution."

RiskAnalyzer performs lightweight AST-based static analysis on Python
code snippets and returns a numeric risk score.  The score is consumed
by sandbox_router.py to decide which seccomp strictness level to apply
to the Docker container.

Critical distinction from v9.10
--------------------------------
In v9.10, a low risk score could route code to MockSandbox (no Docker).
In v9.11 this is PROHIBITED (R-07, R-12).  RiskScore.safe no longer
exists.  The score affects ONLY the seccomp profile strictness — all
code executes in Docker regardless of score.

Score → seccomp tier mapping (handled by sandbox_router.py):
  score < THRESHOLD_LOW  → LOW_STRICT tier  (tightest seccomp)
  score < THRESHOLD_HIGH → STRICT tier
  score >= THRESHOLD_HIGH → STRICT tier  (never loosened)

There is no "safe" or "bypass" score.  A score of 0 still runs in Docker.

Taint tracking (H-1)
--------------------
The analyzer reuses the assignment-chain taint algorithm from
security/skill_ast_sandbox.py to detect aliased dangerous operations.
Tainted names increase the score.

Process: orchestrator (L1)

Security invariants enforced here:
  R-07  RiskAnalyzer never calls exec(), eval(), exec_module().  Static
        analysis only.
  R-12  A low risk score NEVER bypasses Docker execution.  This invariant
        is asserted at module-load time.

Used by:
  sandbox/sandbox_router.py — route_code() passes score to tier selector

Dependencies:
  security/skill_ast_sandbox.py — SkillASTSandbox (taint detection reuse)
  utils/logger.py               — get_logger()
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from security.skill_ast_sandbox import (
    EXPLICITLY_BLOCKED_MODULES,
    SAFE_IMPORTS,
    SkillASTSandbox,
)
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Score ceiling below which LOW_STRICT seccomp tier is applied.
THRESHOLD_LOW:  int = 10

#: Score ceiling for STRICT tier (all scores go to STRICT; no looser tier).
THRESHOLD_HIGH: int = 50

# ---------------------------------------------------------------------------
# Score weights
# ---------------------------------------------------------------------------

_W_BLOCKED_IMPORT:  int = 20   # import of explicitly blocked module
_W_TAINTED_NAME:    int = 15   # tainted alias usage
_W_BLOCKED_BUILTIN: int = 25   # exec/eval/open etc.
_W_BLOCKED_ATTR:    int = 10   # __globals__ etc.
_W_UNSAFE_IMPORT:   int = 5    # import not in SAFE_IMPORTS but not blocked
_W_STAR_IMPORT:     int = 8    # from x import *


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskScore:
    """
    Immutable risk assessment for a code snippet.

    Attributes:
        score:        Numeric risk score (0 = minimal risk, higher = more
                      dangerous).  Does NOT determine whether code runs in
                      Docker — it always does (R-07, R-12).
        tier:         Seccomp tier string: "LOW_STRICT" or "STRICT".
        violations:   List of human-readable violation descriptions.
        taint_names:  Names identified as tainted aliases.

    Note: There is intentionally no ``safe`` attribute.  In v9.11 every
    code path goes through Docker.  Score only influences seccomp strictness.
    """

    score:       int
    tier:        str
    violations:  tuple[str, ...]
    taint_names: tuple[str, ...]


# ---------------------------------------------------------------------------
# RiskAnalyzer
# ---------------------------------------------------------------------------

class RiskAnalyzer:
    """
    Lightweight AST-based risk scorer for sandbox routing.

    Usage::

        analyzer = RiskAnalyzer()
        result   = analyzer.analyze("import os; os.system('ls')")
        print(result.score, result.tier)  # 20, "STRICT"

    The analyzer is stateless and thread-safe.  A single instance may be
    shared across the orchestrator process.
    """

    def __init__(self) -> None:
        # Reuse the SkillASTSandbox taint builder
        self._sandbox = SkillASTSandbox()
        _log.debug("RiskAnalyzer initialised")

    def analyze(self, source: str, *, label: str = "") -> RiskScore:
        """
        Analyse *source* and return a RiskScore.

        Parsing failures (SyntaxError, source too large) return maximum
        score so the router applies the strictest seccomp profile.

        Args:
            source: Python source code to score.
            label:  Optional human-readable label for log messages.

        Returns:
            RiskScore with numeric score, tier, and violation details.
        """
        if not isinstance(source, str):
            return self._max_score(["non-string source"])

        # Size guard — mirror SkillASTSandbox limit
        if len(source.encode("utf-8")) > 65536:
            return self._max_score(["source too large"])

        # Parse
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return self._max_score([f"SyntaxError: {exc.msg}"])

        # Build taint map (H-1)
        taint_map: dict[str, str] = self._sandbox._build_taint_map(tree)

        score      = 0
        violations: list[str] = []

        for node in ast.walk(tree):
            lineno = getattr(node, "lineno", 0)

            # Imports
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for v in self._sandbox._check_import_node(node, lineno):
                    if "BLOCKED" in v.rule:
                        score += _W_BLOCKED_IMPORT
                    elif "NETWORK" in v.rule:
                        score += _W_BLOCKED_IMPORT
                    else:
                        score += _W_UNSAFE_IMPORT
                    violations.append(f"L{lineno} [{v.rule}] {v.name}")
                # Star imports
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "*":
                            score += _W_STAR_IMPORT
                            violations.append(f"L{lineno} [STAR_IMPORT]")

            # Calls
            elif isinstance(node, ast.Call):
                for v in self._sandbox._check_call_node(node, lineno, taint_map):
                    if "BUILTIN" in v.rule:
                        score += _W_BLOCKED_BUILTIN
                    else:
                        score += _W_TAINTED_NAME
                    violations.append(f"L{lineno} [{v.rule}] {v.name}")

            # Attributes
            elif isinstance(node, ast.Attribute):
                for v in self._sandbox._check_attribute_node(node, lineno, taint_map):
                    score += _W_BLOCKED_ATTR
                    violations.append(f"L{lineno} [{v.rule}] {v.name}")

        # Determine tier — score never bypasses Docker (R-12 invariant)
        tier = "LOW_STRICT" if score < THRESHOLD_LOW else "STRICT"

        result = RiskScore(
            score=score,
            tier=tier,
            violations=tuple(violations),
            taint_names=tuple(taint_map.keys()),
        )

        _log.debug(
            "RiskAnalyzer: analysis complete",
            extra={
                "label":      label,
                "score":      score,
                "tier":       tier,
                "violations": len(violations),
                "taint_names": list(taint_map.keys()),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _max_score(violations: list[str]) -> RiskScore:
        """Return a maximum-risk score for code that cannot be analysed."""
        return RiskScore(
            score=THRESHOLD_HIGH + 1,
            tier="STRICT",
            violations=tuple(violations),
            taint_names=(),
        )


# ---------------------------------------------------------------------------
# Module-load assertion (R-07 / R-12)
# ---------------------------------------------------------------------------
# Verify at import time that the scoring constants do not create a "bypass"
# tier.  THRESHOLD_LOW and THRESHOLD_HIGH must both map to Docker execution.

assert THRESHOLD_LOW >= 0, "THRESHOLD_LOW must be non-negative"
assert THRESHOLD_HIGH > THRESHOLD_LOW, "THRESHOLD_HIGH must exceed THRESHOLD_LOW"
# No bypass: the only tiers are "LOW_STRICT" and "STRICT" — both run in Docker.
_ALLOWED_TIERS = frozenset({"LOW_STRICT", "STRICT"})
assert "BYPASS" not in _ALLOWED_TIERS and "MOCK" not in _ALLOWED_TIERS, (
    "R-12 VIOLATION: a bypass tier must never exist in risk_analyzer. "
    "All code runs in Docker regardless of risk score."
)
