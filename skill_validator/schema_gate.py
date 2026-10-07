# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skill_validator/schema_gate.py — Phidipus v1.0
Gate 3 of 3: output schema validation and skill signing for the validation pipeline.

Architecture contract (R-08 / R-09 / R-10):
  "R-08: All generated skill code MUST pass skill_validator Gates 1, 2, and
   3 before registration.  No exceptions."

  "R-09: SkillLoader MUST verify ed25519 signature (.sig file) before loading
   any skill.  Unsigned skills are rejected with an ERROR log entry."

  "R-10: The only permitted output from a Docker skill execution is structured
   JSON on stdout.  The host MUST NOT read from any container filesystem path."

This module is Gate 3 — the final gate in the three-gate skill validation
pipeline:

    Gate 1 — ASTGate     (ast_gate.py)        ← static AST analysis
    Gate 2 — DockerGate  (docker_gate.py)     ← ephemeral execution
    Gate 3 — SchemaGate  (this module)        ← output schema validation + sign

Gate 3 has two responsibilities:

  1. Schema validation
     Re-validates the Gate 2 harness output JSON to confirm it satisfies
     the required structure: a JSON object with ``status == "pass"``,
     a null ``error`` field, and no unexpected fields.  This catches the
     (unlikely but possible) case where the harness ran but produced
     semantically inconsistent output that Gate 2 accepted.

  2. Skill signing (R-09)
     After schema validation passes, SchemaGate calls
     skill_validator/skill_signer.sign_skill() to write the ed25519 .sig
     file alongside the skill .py.  Only after the .sig is written is the
     skill considered fully validated and eligible for registration by
     SkillRegistry.

ValidationResult
----------------
If all three gates have passed, run() returns a ValidationResult that
aggregates metadata from all three gate results plus the path to the
signed .sig file.  This result is the single artefact that SkillRegistry
(or any caller) needs to confirm a skill is fully validated.

Process: orchestrator (L1)

Security invariants enforced here:
  R-07  SchemaGate never calls exec(), eval(), exec_module(), or any
        importlib execution primitive.  It reads the .py file as bytes for
        signing purposes only.
  R-08  Gate3RejectedError is raised on any schema validation failure.
        sign_skill() is only called after schema validation passes.
  R-09  sign_skill() writes a 64-byte ed25519 .sig file atomically
        (mode 0600) via skill_signer.sign_skill().  If signing fails, the
        skill is not registered.
  R-10  Schema validation operates on the stdout string from Gate2Result —
        it never reads from a container filesystem path.

Used by:
  (skill validation pipeline caller — e.g. a SkillValidatorService that
   chains Gate1 → Gate2 → Gate3.run() and passes ValidationResult to
   SkillRegistry)

Dependencies:
  skill_validator/docker_gate.py   — Gate2Result (input to this gate)
  skill_validator/skill_signer.py  — sign_skill(), SkillSignatureError
  skill_validator/ast_gate.py      — Gate1Result (carried into ValidationResult)
  config/config_loader.py          — PhidipusConfig (cfg.skill_validator.*)
  utils/json_utils.py              — loads(), validate(), JsonSchemaError
  utils/logger.py                  — get_logger()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

from config.config_loader import PhidipusConfig
from skill_validator.ast_gate import Gate1Result
from skill_validator.docker_gate import Gate2Result
from skill_validator.skill_signer import SkillSignatureError, sign_skill
from utils.json_utils import JsonDecodeError, JsonSchemaError, loads as json_loads, validate
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Harness output schema (Gate 3 validates Gate 2 stdout against this)
# ---------------------------------------------------------------------------
# This schema mirrors the harness structure in docker_gate.py.
# "status" must be "pass"; "error" must be null; "traceback" must be null.
# additionalProperties: False ensures no unexpected fields sneak through.

_HARNESS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "error", "traceback"],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["pass", "fail"],
        },
        "error": {
            "type": ["string", "null"],
        },
        "traceback": {
            "type": ["string", "null"],
        },
    },
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate3Result:
    """
    Immutable record of a successful Gate 3 schema validation.

    Attributes:
        skill_name:     Identifier of the validated skill.
        stdout_raw:     The raw stdout string from Gate 2 (echoed for record).
        harness_status: Validated ``status`` field from harness JSON
                        (always ``"pass"`` on a Gate3Result).
    """

    skill_name:     str
    stdout_raw:     str
    harness_status: str = "pass"


@dataclass(frozen=True)
class ValidationResult:
    """
    Immutable record of a fully validated and signed skill.

    Returned by SchemaGate.run() when all three gates have passed and the
    skill .sig file has been written.  This is the single authoritative
    artefact that SkillRegistry (or any caller) needs to confirm a skill
    is fully validated and safe to load.

    Attributes:
        skill_name:    Identifier of the validated skill.
        skill_path:    Resolved path to the validated .py file.
        sig_path:      Resolved path to the written .sig file (mode 0600).
        gate1:         Gate1Result from the AST gate.
        gate2:         Gate2Result from the Docker gate.
        gate3:         Gate3Result from the schema gate.
    """

    skill_name: str
    skill_path: Path
    sig_path:   Path
    gate1:      Gate1Result
    gate2:      Gate2Result
    gate3:      Gate3Result


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class Gate3RejectedError(RuntimeError):
    """
    Raised by SchemaGate.run() when Gate 3 schema validation fails.

    A caller that receives this exception MUST NOT sign or register the
    skill.

    Attributes:
        skill_name:  Identifier of the rejected skill.
        reason:      Short machine-readable reason code.  One of:
                       SCHEMA_INVALID      — stdout JSON does not match schema
                       STATUS_NOT_PASS     — harness status field != "pass"
                       STDOUT_REPARSE_ERR  — stdout could not be re-parsed
                       SIGN_FAILED         — ed25519 signing failed after
                                            schema validation passed
        field:       Dot-path of the offending field (if applicable).
    """

    def __init__(
        self,
        message: str,
        *,
        skill_name: str = "",
        reason:     str = "SCHEMA_INVALID",
        field:      str = "",
    ) -> None:
        super().__init__(message)
        self.skill_name = skill_name
        self.reason     = reason
        self.field      = field

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.skill_name:
            parts.append(f"skill={self.skill_name!r}")
        if self.field:
            parts.append(f"field={self.field!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# SchemaGate
# ---------------------------------------------------------------------------

class SchemaGate:
    """
    Gate 3 of the skill validation pipeline: output schema validation
    and ed25519 skill signing.

    SchemaGate is stateless.  A single instance may be reused across
    multiple validation requests within the orchestrator process.

    Usage::

        gate = SchemaGate(cfg)

        try:
            validation = gate.run(
                gate1_result=gate1_result,
                gate2_result=gate2_result,
                skill_path=Path("skills/generated/open_browser.py"),
            )
        except Gate3RejectedError as exc:
            logger.error("Gate 3 rejected skill", extra={"reason": exc.reason})
            raise   # skill must not be registered

        # All 3 gates passed — skill is signed and ready for SkillRegistry.
        skill_registry.register(validation)

    Args:
        cfg: Validated PhidipusConfig.  Gate 3 reads:
               cfg.skill_validator.signing_private_key_file — for sign_skill()
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._private_key_path = Path(cfg.skill_validator.signing_private_key_file)

        _log.info(
            "SchemaGate initialised (Gate 3 of 3)",
            extra={
                "private_key_path": str(self._private_key_path),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        gate1_result: Gate1Result,
        gate2_result: Gate2Result,
        skill_path:   Union[str, Path],
    ) -> ValidationResult:
        """
        Run Gate 3: validate Gate 2 harness output and sign the skill.

        Processing pipeline:
          1. Re-parse Gate 2 stdout as JSON (paranoia re-parse — the raw
             string from Gate2Result is re-validated from scratch).
          2. Validate parsed JSON against _HARNESS_OUTPUT_SCHEMA.
          3. Confirm ``status == "pass"`` (defence-in-depth; Gate 2
             already checked this, but Gate 3 verifies independently).
          4. Call sign_skill() to write the .sig file atomically (R-09).
          5. Return ValidationResult aggregating all three gate results.

        Args:
            gate1_result: Gate1Result from a successful Gate 1 run.
            gate2_result: Gate2Result from a successful Gate 2 run.
            skill_path:   Path to the .py file to be signed.  Must exist
                          on disk and be the exact file that was validated.

        Returns:
            ValidationResult — skill is signed and ready for SkillRegistry.

        Raises:
            Gate3RejectedError: on schema validation failure or signing
                                failure (R-08).
            TypeError:          if gate1_result or gate2_result are wrong type.
        """
        if not isinstance(gate1_result, Gate1Result):
            raise TypeError(
                f"gate1_result must be Gate1Result, got "
                f"{type(gate1_result).__name__!r}."
            )
        if not isinstance(gate2_result, Gate2Result):
            raise TypeError(
                f"gate2_result must be Gate2Result, got "
                f"{type(gate2_result).__name__!r}."
            )

        skill_path = Path(skill_path).resolve()
        skill_name = gate2_result.skill_name or gate1_result.skill_name

        _log.debug(
            "Gate 3: starting schema validation",
            extra={
                "skill_name": skill_name,
                "skill_path": str(skill_path),
            },
        )

        # ── Step 1: re-parse Gate 2 stdout ────────────────────────────────
        # Gate 2 already parsed this; we re-parse from the raw string in
        # Gate2Result to ensure Gate 3 validates independently (R-10).
        try:
            harness_output = json_loads(gate2_result.stdout_raw)
        except JsonDecodeError as exc:
            # Should be unreachable — Gate 2 rejected non-JSON stdout.
            # Treat as Gate 3 rejection for defence-in-depth.
            _log.error(
                "R-08: Gate 3 REJECTED — cannot re-parse Gate 2 stdout",
                extra={
                    "skill_name": skill_name,
                    "error":      str(exc),
                    "reason":     "STDOUT_REPARSE_ERR",
                },
            )
            raise Gate3RejectedError(
                f"Skill {skill_name!r} Gate 3 cannot re-parse Gate 2 stdout: {exc}",
                skill_name=skill_name,
                reason="STDOUT_REPARSE_ERR",
            ) from exc

        # ── Step 2: validate against harness output schema ────────────────
        try:
            validate(harness_output, _HARNESS_OUTPUT_SCHEMA)
        except (JsonSchemaError, TypeError) as exc:
            offending_field = _extract_field(str(exc))
            _log.error(
                "R-08: Gate 3 REJECTED — harness output schema validation failed",
                extra={
                    "skill_name": skill_name,
                    "field":      offending_field,
                    "error":      str(exc),
                    "reason":     "SCHEMA_INVALID",
                },
            )
            raise Gate3RejectedError(
                f"Skill {skill_name!r} Gate 3 harness output failed schema "
                f"validation: {exc}",
                skill_name=skill_name,
                reason="SCHEMA_INVALID",
                field=offending_field,
            ) from exc

        # ── Step 3: confirm status == "pass" (independent check) ──────────
        status: str = harness_output.get("status", "fail")
        if status != "pass":
            skill_error = str(harness_output.get("error") or "")
            _log.error(
                "R-08: Gate 3 REJECTED — harness status is not 'pass'",
                extra={
                    "skill_name":  skill_name,
                    "status":      status,
                    "skill_error": skill_error[:256],
                    "reason":      "STATUS_NOT_PASS",
                },
            )
            raise Gate3RejectedError(
                f"Skill {skill_name!r} Gate 3: harness status is {status!r}, "
                f"expected 'pass'.  Skill error: {skill_error}",
                skill_name=skill_name,
                reason="STATUS_NOT_PASS",
                field="status",
            )

        # ── Step 4: sign the skill (R-09) ─────────────────────────────────
        try:
            sig_path = sign_skill(skill_path, self._private_key_path)
        except (SkillSignatureError, FileNotFoundError, OSError,
                ImportError) as exc:
            _log.error(
                "R-09: Gate 3 signing FAILED — skill not registered",
                extra={
                    "skill_name":       skill_name,
                    "skill_path":       str(skill_path),
                    "private_key_path": str(self._private_key_path),
                    "error":            str(exc),
                    "reason":           "SIGN_FAILED",
                },
            )
            raise Gate3RejectedError(
                f"Skill {skill_name!r} Gate 3 signing failed: {exc}. "
                "The skill cannot be registered without a valid .sig file (R-09).",
                skill_name=skill_name,
                reason="SIGN_FAILED",
            ) from exc

        # ── Gate 3 PASS ───────────────────────────────────────────────────
        gate3_result = Gate3Result(
            skill_name=skill_name,
            stdout_raw=gate2_result.stdout_raw,
            harness_status="pass",
        )

        validation = ValidationResult(
            skill_name=skill_name,
            skill_path=skill_path,
            sig_path=sig_path,
            gate1=gate1_result,
            gate2=gate2_result,
            gate3=gate3_result,
        )

        _log.info(
            "R-08 / R-09: ALL 3 GATES PASSED — skill signed and ready for registration",
            extra={
                "skill_name":   skill_name,
                "skill_path":   str(skill_path),
                "sig_path":     str(sig_path),
                "source_bytes": gate1_result.source_bytes,
                "ast_nodes":    gate1_result.ast_nodes,
                "exec_ms":      gate2_result.execution_ms,
            },
        )
        return validation


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _extract_field(error_msg: str) -> str:
    """
    Extract a dotted field path from a JsonSchemaError message string.

    JsonSchemaError format: ``$.<field>: <detail>``
    Returns the path portion before the first colon, stripping leading ``$``.
    Falls back to empty string.
    """
    if ":" in error_msg:
        candidate = error_msg.split(":")[0].strip().lstrip("$").lstrip(".")
        if candidate:
            return candidate
    return ""
