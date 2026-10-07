# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skill_validator/ast_gate.py — Phidipus v1.0
Gate 1 of 3: AST-based static analysis wrapper for the skill validation pipeline.

Architecture contract (R-08 / R-11):
  "All generated skill code MUST pass skill_validator Gates 1, 2, and 3
   before registration.  No exceptions."

  "R-11: ast_gate must have allow_network=False, pathlib EXCLUDED from
   SAFE_IMPORTS, and asyncio EXCLUDED from SAFE_IMPORTS.  This configuration
   is immutable."

This module is the entry point for Gate 1 in the three-gate skill validation
pipeline:

    Gate 1 — ASTGate (this module)           ← static AST analysis
    Gate 2 — DockerGate (docker_gate.py)     ← ephemeral execution
    Gate 3 — SchemaGate (schema_gate.py)     ← output schema validation

ASTGate wraps security/skill_ast_sandbox.SkillASTSandbox.  A skill source
string is passed to ASTGate.run(); on success a Gate1Result is returned and
the caller may proceed to Gate 2.  On any violation, Gate1RejectedError is
raised immediately — the skill never reaches Gate 2 (R-08).

Design
------
ASTGate is stateless per-call.  A single instance may be shared across
multiple validation requests in the orchestrator process.  The underlying
SkillASTSandbox instance is created once at construction time and reused —
it holds no mutable state between calls (it only stores configuration
integers and reads the module-level frozensets).

The gate deliberately avoids caching results: every call to run() performs
a full fresh analysis so that a skill that was modified between Gate 1 and
Gate 2 cannot slip through on a cached pass.

Process: orchestrator (L1)

Security invariants enforced here:
  R-07  ASTGate never calls exec(), eval(), exec_module(), or any importlib
        execution primitive.  It passes source bytes to SkillASTSandbox for
        pure static analysis only.
  R-08  If SkillASTSandbox raises ASTSandboxViolation, ASTGate immediately
        raises Gate1RejectedError.  The skill source is logged (truncated)
        and no further processing occurs.  Only a Gate1Result return value
        (no exception) allows the caller to advance to Gate 2.
  R-11  SkillASTSandbox enforces the immutable SAFE_IMPORTS frozenset,
        allow_network=False, and the pathlib/asyncio exclusions.  ASTGate
        makes no attempt to modify these — it only calls sandbox.check().

Used by:
  (skill validation pipeline entrypoint, e.g. core/skill_router.py or a
   future SkillValidatorService that chains Gates 1→2→3)

Dependencies:
  security/skill_ast_sandbox.py  — SkillASTSandbox, ASTSandboxViolation,
                                    ViolationDetail, MAX_SOURCE_BYTES,
                                    MAX_AST_NODES
  config/config_loader.py        — PhidipusConfig (cfg.skill_validator.
                                    max_skill_bytes)
  utils/logger.py                — get_logger()
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from config.config_loader import PhidipusConfig
from security.skill_ast_sandbox import (
    MAX_AST_NODES,
    MAX_SOURCE_BYTES,
    SAFE_IMPORTS,
    ASTSandboxViolation,
    SkillASTSandbox,
    ViolationDetail,
)
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of violation detail lines included in Gate1RejectedError.message
#: and log output.  Capped to keep log entries readable.
_MAX_VIOLATIONS_IN_LOG: int = 10

#: Maximum characters of skill source included in DEBUG log on rejection.
#: Enough to identify the offending code region; not the full source.
_MAX_SOURCE_IN_LOG: int = 512


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate1Result:
    """
    Immutable record of a successful Gate 1 analysis.

    Returned by ASTGate.run() when the skill source passes all AST checks.
    The caller may use this result to log metrics or pass metadata to Gate 2.

    Attributes:
        skill_name:   Identifier of the skill that was analysed
                      (may be empty if not provided by the caller).
        source_bytes: Number of UTF-8 bytes in the skill source.
        ast_nodes:    Total number of AST nodes in the parsed tree.
        taint_names:  List of local variable names that were identified as
                      aliases of dangerous modules or builtins (H-1 taint
                      tracking).  Empty list means no tainted names were
                      found.
        safe_imports: Snapshot of the SAFE_IMPORTS frozenset size at check
                      time (informational; always == len(SAFE_IMPORTS)).
    """

    skill_name:   str
    source_bytes: int
    ast_nodes:    int
    taint_names:  list[str]       = field(default_factory=list)
    safe_imports: int             = 0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class Gate1RejectedError(RuntimeError):
    """
    Raised by ASTGate.run() when the skill source fails Gate 1 AST analysis.

    A caller that receives this exception MUST NOT advance the skill to
    Gate 2.  The skill source must be discarded and the rejection logged.

    Attributes:
        skill_name:       Identifier of the rejected skill.
        violation_count:  Total number of violations found.
        violations:       List of ViolationDetail records from
                          SkillASTSandbox (up to _MAX_VIOLATIONS_IN_LOG
                          entries; the full list is in .full_violations).
        full_violations:  Complete list of all ViolationDetail records.
        first_rule:       Rule code of the first violation
                          (e.g. "BLOCKED_IMPORT", "TAINTED_NAME_CALL").
        first_line:       Source line number of the first violation (1-based).
        reason:           Short machine-readable summary reason code.
                          Always "GATE1_REJECTED".
    """

    def __init__(
        self,
        message: str,
        *,
        skill_name:      str                  = "",
        violations:      list[ViolationDetail] = (),
        reason:          str                  = "GATE1_REJECTED",
    ) -> None:
        super().__init__(message)
        self.skill_name       = skill_name
        self.full_violations  = list(violations)
        self.violation_count  = len(self.full_violations)
        self.violations       = self.full_violations[:_MAX_VIOLATIONS_IN_LOG]
        self.first_rule       = violations[0].rule  if violations else ""
        self.first_line       = violations[0].lineno if violations else 0
        self.reason           = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.skill_name:
            parts.append(f"skill={self.skill_name!r}")
        parts.append(f"violations={self.violation_count}")
        if self.first_rule:
            parts.append(f"first={self.first_rule}@L{self.first_line}")
        return " ".join(parts) + f" {base}"

    def format_violations(self, *, limit: int = _MAX_VIOLATIONS_IN_LOG) -> str:
        """
        Return a multi-line string of violation details for logging or
        display, capped at *limit* entries.
        """
        lines: list[str] = []
        for v in self.full_violations[:limit]:
            lines.append(
                f"  L{v.lineno:4d} [{v.rule:<28s}] {v.node_type}: {v.message}"
            )
        remaining = self.violation_count - limit
        if remaining > 0:
            lines.append(f"  ... +{remaining} more violation(s)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# ASTGate
# ---------------------------------------------------------------------------

class ASTGate:
    """
    Gate 1 of the skill validation pipeline: AST-based static analysis.

    Wraps SkillASTSandbox.check() and translates its result into the
    Gate 1 protocol:
      - Pass  → returns Gate1Result (caller may advance to Gate 2)
      - Fail  → raises Gate1RejectedError (caller must discard the skill)

    A single ASTGate instance is safe to reuse across multiple validation
    requests within the orchestrator process.

    Usage::

        gate = ASTGate(cfg)

        try:
            result = gate.run(skill_source, skill_name="open_browser")
        except Gate1RejectedError as exc:
            logger.error("Gate 1 rejected skill", extra={"error": str(exc)})
            # Do NOT advance to Gate 2.
            raise

        # Gate 1 passed — proceed to Gate 2.
        docker_result = docker_gate.run(skill_source, skill_name="open_browser")

    Args:
        cfg: Validated PhidipusConfig.  Gate 1 reads
             cfg.skill_validator.max_skill_bytes as the source size ceiling.
             The AST node limit is fixed at MAX_AST_NODES (10 000) and is
             not configurable to prevent operators from inadvertently raising
             it above a safe level.
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        # cfg.skill_validator.max_skill_bytes is the config-layer ceiling.
        # It must not exceed MAX_SOURCE_BYTES (the hard limit in SkillASTSandbox).
        max_skill_bytes: int = min(
            cfg.skill_validator.max_skill_bytes,
            MAX_SOURCE_BYTES,
        )

        # MAX_AST_NODES is fixed — not exposed as a config option.
        self._sandbox = SkillASTSandbox(
            max_source_bytes=max_skill_bytes,
            max_ast_nodes=MAX_AST_NODES,
        )

        _log.info(
            "ASTGate initialised (Gate 1 of 3)",
            extra={
                "max_skill_bytes": max_skill_bytes,
                "max_ast_nodes":   MAX_AST_NODES,
                "allow_network":   self._sandbox.allow_network,  # always False
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        source: str,
        *,
        skill_name: str = "",
    ) -> Gate1Result:
        """
        Run Gate 1 static analysis on *source*.

        The full SkillASTSandbox check suite is executed:
          1. Source size limit (cfg.skill_validator.max_skill_bytes).
          2. AST parse (SyntaxError → rejection).
          3. AST node count limit (MAX_AST_NODES).
          4. Import allowlist — SAFE_IMPORTS only (R-11).
          5. Blocked builtins — exec, eval, open, __import__, etc.
          6. Blocked attribute access — __globals__, __builtins__, etc.
          7. Assignment-chain taint tracking (H-1).
          8. Network API calls (always blocked — allow_network is always
             False per R-11 / C-5).

        All violations found in a single call are collected and reported
        together in Gate1RejectedError so the caller receives a complete
        picture of what is wrong with the skill.

        Args:
            source:     Skill Python source code as a string.  Must not be
                        empty.  Must be UTF-8 encodable.
            skill_name: Optional human-readable identifier used in log
                        messages and exception attributes.  Never used in
                        analysis logic.

        Returns:
            Gate1Result on success.  The caller MUST receive a Gate1Result
            (no exception) before advancing the skill to Gate 2.

        Raises:
            Gate1RejectedError: if the source fails any AST check (R-08).
                                The skill must not be advanced to Gate 2.
            TypeError:          if source is not a str.
        """
        if not isinstance(source, str):
            raise TypeError(
                f"ASTGate.run() requires a str source, "
                f"got {type(source).__name__!r}."
            )

        _log.debug(
            "Gate 1: starting AST analysis",
            extra={
                "skill_name":   skill_name,
                "source_chars": len(source),
            },
        )

        try:
            self._sandbox.check(source, skill_name=skill_name)

        except ASTSandboxViolation as exc:
            # ── Gate 1 FAIL ───────────────────────────────────────────────
            _log.error(
                "R-08: Gate 1 REJECTED — skill will not advance to Gate 2",
                extra={
                    "skill_name":      skill_name,
                    "violation_count": len(exc.violations),
                    "first_rule":      exc.violations[0].rule  if exc.violations else "",
                    "first_line":      exc.violations[0].lineno if exc.violations else 0,
                    "first_name":      exc.violations[0].name  if exc.violations else "",
                    # Truncated source snippet for context — never log full source
                    "source_snippet":  source[:_MAX_SOURCE_IN_LOG].replace("\n", "↵"),
                },
            )

            # Emit one structured log line per violation for operator review
            for idx, v in enumerate(exc.violations[:_MAX_VIOLATIONS_IN_LOG]):
                _log.error(
                    "Gate 1 violation detail",
                    extra={
                        "skill_name":   skill_name,
                        "violation_n":  idx + 1,
                        "rule":         v.rule,
                        "lineno":       v.lineno,
                        "node_type":    v.node_type,
                        "name":         v.name,
                        "message":      v.message,
                    },
                )

            if len(exc.violations) > _MAX_VIOLATIONS_IN_LOG:
                _log.error(
                    "Gate 1: additional violations truncated in log",
                    extra={
                        "skill_name":      skill_name,
                        "total_violations": len(exc.violations),
                        "logged":          _MAX_VIOLATIONS_IN_LOG,
                    },
                )

            raise Gate1RejectedError(
                f"Skill {skill_name!r} failed Gate 1 AST analysis with "
                f"{len(exc.violations)} violation(s). "
                f"First: L{exc.violations[0].lineno if exc.violations else 0} "
                f"[{exc.violations[0].rule if exc.violations else 'UNKNOWN'}] "
                f"{exc.violations[0].message if exc.violations else ''}",
                skill_name=skill_name,
                violations=exc.violations,
                reason="GATE1_REJECTED",
            ) from exc

        # ── Gate 1 PASS ───────────────────────────────────────────────────
        # Collect metadata from the sandbox for the result.
        # The sandbox.check() call above already verified node count and
        # source size, so we re-derive the values here for Gate1Result.
        source_bytes = len(source.encode("utf-8"))

        # Re-parse to get node count for the result record.
        # This is a second parse but it is cheap (source already passed
        # Gate 1) and avoids coupling to sandbox internals.
        try:
            tree      = ast.parse(source)
            ast_nodes = sum(1 for _ in ast.walk(tree))
        except SyntaxError:
            # Should be unreachable — sandbox.check() would have caught this.
            ast_nodes = 0

        result = Gate1Result(
            skill_name=skill_name,
            source_bytes=source_bytes,
            ast_nodes=ast_nodes,
            taint_names=[],   # sandbox does not expose taint_map externally
            safe_imports=len(SAFE_IMPORTS),
        )

        _log.info(
            "R-08: Gate 1 PASSED — skill may advance to Gate 2",
            extra={
                "skill_name":   result.skill_name,
                "source_bytes": result.source_bytes,
                "ast_nodes":    result.ast_nodes,
                "safe_imports": result.safe_imports,
            },
        )
        return result
