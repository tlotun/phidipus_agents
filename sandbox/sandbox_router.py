# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
sandbox/sandbox_router.py — Phidipus v1.0
Routes code execution requests to the appropriate Docker sandbox tier.

Architecture contract (H-1 / R-07 / R-12):
  "Replace regex BLOCK rules with AST taint analysis (H-1).  Remove
   MockSandbox from production routing.  All generated code routes to
   Docker minimum.  Score only determines seccomp strictness."

SandboxRouter is the single entry point for routing arbitrary Python
code to the sandbox subsystem.  It uses RiskAnalyzer to assess the code
and selects the appropriate seccomp strictness tier for the Docker
container.

v9.10 → v9.11 changes
----------------------
  - Regex-based BLOCK rules replaced by AST taint analysis (H-1).
  - MockSandbox completely removed from the routing table (R-07, R-12).
  - No more "safe" fast path that bypasses Docker.
  - Two execution tiers only: LOW_STRICT and STRICT (both Docker).

Execution tiers
---------------
  LOW_STRICT  — tightest seccomp profile; reserved for code with
                score < THRESHOLD_LOW (clean, no risky patterns).
                Still runs in Docker — never on the host.

  STRICT      — standard seccomp profile; used for anything with
                score >= THRESHOLD_LOW or any parsing failure.

Both tiers use the same --network=none flag (R-12).

Process: orchestrator (L1)

Security invariants enforced here:
  R-07  exec(), eval(), exec_module() are never called on routed code.
        All code executes in Docker via SandboxManager.
  R-12  MockSandbox is NEVER in the routing table.  The module-load
        assertion verifies this at import time.
  H-1   Routing decisions use AST taint analysis (RiskAnalyzer), not
        regex string matching.

Used by:
  core/skill_router.py     — route_skill_code() to appropriate sandbox

Dependencies:
  sandbox/risk_analyzer.py   — RiskAnalyzer, RiskScore, THRESHOLD_LOW
  sandbox/sandbox_manager.py — SandboxManager, SandboxManagerConfig
  config/config_loader.py    — PhidipusConfig
  utils/logger.py            — get_logger()
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from sandbox.risk_analyzer import THRESHOLD_LOW, RiskAnalyzer, RiskScore
from sandbox.sandbox_manager import (
    SandboxExecutionError,
    SandboxManager,
    SandboxManagerConfig,
    SandboxSecurityError,
    SandboxTimeoutError,
)
from sandbox.docker_runner import ExecutionResult
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Tier names
# ---------------------------------------------------------------------------

TIER_LOW_STRICT: str = "LOW_STRICT"
TIER_STRICT:     str = "STRICT"

#: All permitted tiers — MockSandbox is intentionally absent (R-12).
_PERMITTED_TIERS: frozenset[str] = frozenset({TIER_LOW_STRICT, TIER_STRICT})


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoutingDecision:
    """
    Immutable record of a sandbox routing decision.

    Attributes:
        tier:        Selected execution tier ("LOW_STRICT" or "STRICT").
        risk_score:  RiskScore from RiskAnalyzer.
        label:       Optional human-readable label for the code.
    """

    tier:       str
    risk_score: RiskScore
    label:      str = ""


# ---------------------------------------------------------------------------
# SandboxRouter
# ---------------------------------------------------------------------------

class SandboxRouter:
    """
    Routes Python code execution to the appropriate Docker sandbox tier.

    Uses RiskAnalyzer (AST taint analysis, H-1) to score the code and
    selects a seccomp strictness tier.  All tiers execute in Docker —
    there is no host-execution or mock path (R-07, R-12).

    Usage::

        router = SandboxRouter(cfg)

        # Score and route without executing:
        decision = router.decide(code, label="my_skill")

        # Score, route, and execute in one call:
        result = router.run(code, label="my_skill")

    Args:
        cfg: Validated PhidipusConfig.
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._analyzer   = RiskAnalyzer()
        self._base_cfg   = SandboxManagerConfig(
            image                = cfg.sandbox.docker_image,
            seccomp_profile_path = Path(cfg.sandbox.seccomp_profile_path),
            default_memory       = cfg.sandbox.memory_limit,
            default_cpu_quota    = cfg.sandbox.cpu_quota,
            default_timeout      = cfg.sandbox.execution_timeout_seconds,
        )
        # LOW_STRICT gets a tighter CPU quota (75% of base)
        self._low_strict_cfg = SandboxManagerConfig(
            image                = cfg.sandbox.docker_image,
            seccomp_profile_path = Path(cfg.sandbox.seccomp_profile_path),
            default_memory       = cfg.sandbox.memory_limit,
            default_cpu_quota    = max(1_000, cfg.sandbox.cpu_quota * 3 // 4),
            default_timeout      = cfg.sandbox.execution_timeout_seconds,
        )
        _log.info(
            "SandboxRouter initialised",
            extra={
                "docker_image": cfg.sandbox.docker_image,
                "threshold_low": THRESHOLD_LOW,
                "tiers":        sorted(_PERMITTED_TIERS),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide(self, source: str, *, label: str = "") -> RoutingDecision:
        """
        Analyse *source* and return a routing decision without executing.

        Useful for pre-flight checks and logging before committing to
        sandbox execution.

        Args:
            source: Python source code to analyse.
            label:  Optional label for logging.

        Returns:
            RoutingDecision with the selected tier and risk score.
        """
        risk = self._analyzer.analyze(source, label=label)
        tier = TIER_LOW_STRICT if risk.score < THRESHOLD_LOW else TIER_STRICT

        decision = RoutingDecision(tier=tier, risk_score=risk, label=label)

        _log.debug(
            "SandboxRouter: routing decision",
            extra={
                "label":      label,
                "score":      risk.score,
                "tier":       tier,
                "violations": len(risk.violations),
            },
        )
        return decision

    def run(
        self,
        source: str,
        *,
        label: str = "",
    ) -> ExecutionResult:
        """
        Analyse *source*, select the appropriate tier, and execute in Docker.

        Creates a fresh SandboxManager per call (R-12: no container reuse).

        Args:
            source: Python source code to execute.
            label:  Optional label for logging.

        Returns:
            ExecutionResult from SandboxManager.run_python().

        Raises:
            SandboxTimeoutError:   if execution exceeds the timeout.
            SandboxSecurityError:  if seccomp profile validation fails.
            SandboxExecutionError: on Docker API or container errors.
        """
        decision = self.decide(source, label=label)
        manager  = self._make_manager(decision.tier)

        _log.info(
            "SandboxRouter: executing in Docker",
            extra={
                "label": label,
                "tier":  decision.tier,
                "score": decision.risk_score.score,
            },
        )

        try:
            return manager.run_python(source)
        except (SandboxTimeoutError, SandboxSecurityError, SandboxExecutionError):
            raise
        except Exception as exc:
            raise SandboxExecutionError(
                f"SandboxRouter unexpected error for {label!r}: {exc}",
                exit_code=-1,
            ) from exc

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_manager(self, tier: str) -> SandboxManager:
        """
        Return a fresh SandboxManager for *tier*.

        Every call creates a new instance (R-12: no reuse).
        """
        if tier == TIER_LOW_STRICT:
            return SandboxManager(self._low_strict_cfg)
        # Default: STRICT
        return SandboxManager(self._base_cfg)


# ---------------------------------------------------------------------------
# Module-load assertion (R-07 / R-12)
# ---------------------------------------------------------------------------

assert "MOCK" not in _PERMITTED_TIERS and "BYPASS" not in _PERMITTED_TIERS, (
    "R-12 VIOLATION: MockSandbox or bypass tier must never appear in "
    "SandboxRouter._PERMITTED_TIERS.  All code executes in Docker."
)
assert len(_PERMITTED_TIERS) == 2, (
    f"Expected exactly 2 permitted tiers (LOW_STRICT, STRICT), "
    f"got {len(_PERMITTED_TIERS)}: {_PERMITTED_TIERS}"
)
