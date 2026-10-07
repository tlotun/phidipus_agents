# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/skill_router.py — Phidipus v1.0
Routes skill execution requests through the 3-gate validator pipeline.

Architecture contract (R-07 / R-08):
  "Route to skill_validator service, not exec_module().  Remove
   builtin-skill fast path that bypasses sandbox."

SkillRouter is the single entry point for all skill execution requests in
the orchestrator process.  It enforces the three-gate validation pipeline:

    Gate 1 — ASTGate     (skill_validator/ast_gate.py)
    Gate 2 — DockerGate  (skill_validator/docker_gate.py)
    Gate 3 — SchemaGate  (skill_validator/schema_gate.py)

No skill source code reaches exec_module() or the host Python interpreter.
Every skill passes through all three gates and receives an ed25519 .sig
file before it is eligible for loading by SkillLoader (R-09).

Fast-path removal
-----------------
v9.10 had a "builtin-skill fast path" that allowed pre-approved skills to
bypass the sandbox.  This path is removed in v9.11 (R-07, R-08).  There
are no exceptions — every skill, including built-in ones, goes through the
validator pipeline before the first execution.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-07  exec_module() is never called on skill source code.
  R-08  All skill code passes all three gates before registration.
  R-09  Only skills with a valid .sig file are passed to SkillLoader.

Used by:
  core/agent_loop.py  — validate_and_register_skill()

Dependencies:
  skill_validator/ast_gate.py    — ASTGate, Gate1RejectedError
  skill_validator/docker_gate.py — DockerGate, Gate2RejectedError
  skill_validator/schema_gate.py — SchemaGate, Gate3RejectedError, ValidationResult
  skills/skill_loader.py         — SkillLoader.load_skill()
  config/config_loader.py        — PhidipusConfig
  utils/logger.py                — get_logger()
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from skill_validator.ast_gate import ASTGate, Gate1RejectedError
from skill_validator.docker_gate import DockerGate, Gate2RejectedError
from skill_validator.schema_gate import Gate3RejectedError, SchemaGate, ValidationResult
from skills.skill_loader import SecurityError, SkillLoader
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SkillRoutingError(RuntimeError):
    """
    Raised when SkillRouter cannot validate or load a skill.

    Attributes:
        skill_name:  Name of the skill that failed.
        gate:        Which gate failed ("gate1", "gate2", "gate3", "load",
                     or "unknown").
        reason:      Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        skill_name: str = "",
        gate:       str = "unknown",
        reason:     str = "ROUTING_FAILED",
    ) -> None:
        super().__init__(message)
        self.skill_name = skill_name
        self.gate       = gate
        self.reason     = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.skill_name:
            parts.append(f"skill={self.skill_name!r}")
        if self.gate:
            parts.append(f"gate={self.gate!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# SkillRouter
# ---------------------------------------------------------------------------

class SkillRouter:
    """
    Routes skill code through the 3-gate validation pipeline.

    A skill must pass all three gates before its module is loaded into
    the orchestrator.  The router never calls exec() or exec_module()
    on skill code — Docker handles all execution (R-07).

    Usage::

        router = SkillRouter(cfg)

        # Validate a generated skill and get a loaded module:
        module = router.validate_and_load(
            skill_source="def run(): return True",
            skill_path=Path("skills/generated/my_skill.py"),
            skill_name="my_skill",
        )

    Args:
        cfg: Validated PhidipusConfig.
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._gate1  = ASTGate(cfg)
        self._gate2  = DockerGate(cfg)
        self._gate3  = SchemaGate(cfg)
        self._loader = SkillLoader(
            skills_dir      = Path(cfg.paths.data_dir) / "skills" / "generated",
            public_key_path = Path(cfg.skill_validator.signing_public_key_file),
        )

        _log.info(
            "SkillRouter initialised (3-gate pipeline)",
            extra={
                "signing_pub": cfg.skill_validator.signing_public_key_file,
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_and_load(
        self,
        skill_source: str,
        skill_path:   Path,
        skill_name:   str = "",
    ) -> Any:
        """
        Run the full 3-gate validation pipeline and load the skill module.

        Pipeline:
          Gate 1 — AST static analysis (R-11)
          Gate 2 — Ephemeral Docker execution (R-12, R-15)
          Gate 3 — Output schema validation + ed25519 signing (R-09)
          Load   — SkillLoader.load_skill() verifies .sig before import

        Args:
            skill_source: Skill Python source code.
            skill_path:   Path where the .py file is written on disk.
                          Must exist before calling this method.
            skill_name:   Human-readable identifier for logging.

        Returns:
            Loaded Python module object ready for use.

        Raises:
            SkillRoutingError: wrapping Gate1/2/3RejectedError, SecurityError,
                               or any unexpected failure.
        """
        label = skill_name or skill_path.stem

        _log.info(
            "SkillRouter: starting 3-gate pipeline",
            extra={"skill_name": label, "skill_path": str(skill_path)},
        )

        # ── Gate 1 ────────────────────────────────────────────────────────
        try:
            gate1_result = self._gate1.run(skill_source, skill_name=label)
        except Gate1RejectedError as exc:
            _log.error(
                "SkillRouter: Gate 1 REJECTED",
                extra={
                    "skill_name": label,
                    "violations": exc.violation_count,
                    "first_rule": exc.first_rule,
                },
            )
            raise SkillRoutingError(
                str(exc), skill_name=label, gate="gate1", reason="GATE1_REJECTED"
            ) from exc

        # ── Gate 2 ────────────────────────────────────────────────────────
        try:
            gate2_result = self._gate2.run(skill_source, skill_name=label)
        except Gate2RejectedError as exc:
            _log.error(
                "SkillRouter: Gate 2 REJECTED",
                extra={"skill_name": label, "reason": exc.reason},
            )
            raise SkillRoutingError(
                str(exc), skill_name=label, gate="gate2", reason="GATE2_REJECTED"
            ) from exc

        # ── Gate 3 (+ sign) ───────────────────────────────────────────────
        try:
            validation: ValidationResult = self._gate3.run(
                gate1_result, gate2_result, skill_path
            )
        except Gate3RejectedError as exc:
            _log.error(
                "SkillRouter: Gate 3 REJECTED",
                extra={"skill_name": label, "reason": exc.reason},
            )
            raise SkillRoutingError(
                str(exc), skill_name=label, gate="gate3", reason="GATE3_REJECTED"
            ) from exc

        # ── Load (verifies .sig) ──────────────────────────────────────────
        try:
            module = self._loader.load_skill(label)
        except SecurityError as exc:
            _log.error(
                "SkillRouter: SkillLoader rejected skill — sig invalid",
                extra={"skill_name": label, "reason": exc.reason},
            )
            raise SkillRoutingError(
                str(exc), skill_name=label, gate="load", reason="SIG_REJECTED"
            ) from exc
        except Exception as exc:
            raise SkillRoutingError(
                f"SkillLoader failed to load {label!r}: {exc}",
                skill_name=label,
                gate="load",
                reason="LOAD_FAILED",
            ) from exc

        _log.info(
            "SkillRouter: skill validated and loaded",
            extra={
                "skill_name": label,
                "sig_path":   str(validation.sig_path),
            },
        )
        return module

    def validate_only(
        self,
        skill_source: str,
        skill_path:   Path,
        skill_name:   str = "",
    ) -> ValidationResult:
        """
        Run all three gates and sign the skill without loading the module.

        Used when registering a skill for future use without immediately
        importing it into the orchestrator's namespace.

        Returns:
            ValidationResult — skill is signed and ready for SkillRegistry.

        Raises:
            SkillRoutingError: on any gate failure.
        """
        label = skill_name or skill_path.stem

        try:
            gate1 = self._gate1.run(skill_source, skill_name=label)
        except Gate1RejectedError as exc:
            raise SkillRoutingError(
                str(exc), skill_name=label, gate="gate1", reason="GATE1_REJECTED"
            ) from exc

        try:
            gate2 = self._gate2.run(skill_source, skill_name=label)
        except Gate2RejectedError as exc:
            raise SkillRoutingError(
                str(exc), skill_name=label, gate="gate2", reason="GATE2_REJECTED"
            ) from exc

        try:
            return self._gate3.run(gate1, gate2, skill_path)
        except Gate3RejectedError as exc:
            raise SkillRoutingError(
                str(exc), skill_name=label, gate="gate3", reason="GATE3_REJECTED"
            ) from exc
