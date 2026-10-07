# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skill_validator/docker_gate.py — Phidipus v1.0
Gate 2 of 3: ephemeral Docker execution for the skill validation pipeline.

Architecture contract (R-07 / R-08 / R-12):
  "R-07: exec_module() MUST NOT be called on any LLM-generated or evolved
   code in the host Python process."

  "R-08: All generated skill code MUST pass skill_validator Gates 1, 2, and
   3 before registration.  No exceptions."

  "R-12: All Docker containers MUST launch with --network=none."

This module is Gate 2 in the three-gate skill validation pipeline:

    Gate 1 — ASTGate     (ast_gate.py)        ← static AST analysis
    Gate 2 — DockerGate  (this module)        ← ephemeral execution
    Gate 3 — SchemaGate  (schema_gate.py)     ← output schema validation

Gate 2 executes the skill source code inside an ephemeral, network-isolated
Docker container by wrapping it in a thin harness that captures execution
outcome as structured JSON on stdout.  The host process reads only the
JSON stdout — it never touches the container filesystem or evaluates the
skill code directly (R-07).

Execution harness
-----------------
The harness is prepended/appended to the skill source and the combined
code is passed to SandboxManager.run_python() via stdin (R-15):

    [HARNESS HEADER]       — import json, set up try/except
    [SKILL SOURCE]         — verbatim, between header and footer
    [HARNESS FOOTER]       — mark status=pass, print JSON result

On success the container prints exactly one JSON line to stdout:

    {"status": "pass", "error": null, "traceback": null}

On any exception during skill execution:

    {"status": "fail", "error": "<str(exc)[:512]>",
     "traceback": "<traceback[:1024]>"}

The harness is constructed by string concatenation (not .format() or
f-string interpolation) to avoid format-string injection.

Gate 2 rejection cases
-----------------------
  - Container times out (SandboxTimeoutError from SandboxManager).
  - Container exits with non-zero code and stdout is empty or unparseable.
  - Stdout is not valid JSON (Docker may print error messages).
  - Parsed JSON has status != "pass".
  - Stdout exceeds max_stdout_bytes (parsed as empty → rejected).

Process: orchestrator (L1)

Security invariants enforced here:
  R-07  Skill source is never passed to exec(), eval(), or exec_module()
        in the host process.  It is encoded as bytes and piped to a Docker
        container via stdin only.
  R-08  Gate2RejectedError is raised on any failure — the skill never
        reaches Gate 3 (schema_gate.py) unless run() returns Gate2Result.
  R-12  SandboxManager creates containers with --network=none.  DockerGate
        does not override or soften any sandbox security parameter.
  R-15  Skill source is passed to the container via stdin (via
        SandboxManager.run_python → DockerSandbox stdin path, FIX-1/FIX-2).

Used by:
  skill_validator/schema_gate.py — chains Gate 2 result into Gate 3

Dependencies:
  sandbox/sandbox_manager.py  — SandboxManager, SandboxManagerConfig,
                                 SandboxTimeoutError, SandboxExecutionError,
                                 SandboxSecurityError
  config/config_loader.py     — PhidipusConfig (cfg.skill_validator.*,
                                 cfg.sandbox.*)
  utils/json_utils.py         — loads() for stdout JSON parsing
  utils/logger.py             — get_logger()
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from sandbox.sandbox_manager import (
    SandboxManager,
    SandboxManagerConfig,
    SandboxExecutionError,
    SandboxSecurityError,
    SandboxTimeoutError,
)
from utils.json_utils import JsonDecodeError, loads as json_loads
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum characters of skill error message stored in Gate2Result.
_MAX_SKILL_ERROR_CHARS: int = 512

#: Maximum characters of skill traceback stored in Gate2Result.
_MAX_SKILL_TRACEBACK_CHARS: int = 1024

#: Maximum characters of stdout included in rejection log messages.
_MAX_STDOUT_IN_LOG: int = 256

#: Expected "status" field value in a successful harness output.
_STATUS_PASS: str = "pass"
_STATUS_FAIL: str = "fail"

# ---------------------------------------------------------------------------
# Execution harness
# ---------------------------------------------------------------------------
# The harness wraps skill source with a try/except that captures outcome
# as a single JSON line on stdout.
#
# Design rules:
#   - Harness identifiers are prefixed with ``_gate2_`` to avoid name
#     collisions with skill-local variables.
#   - All harness imports use ``as`` aliases for the same reason.
#   - The harness NEVER uses .format() or f-strings to inject skill source —
#     it is assembled by plain string concatenation.
#   - Output is printed with flush=True so it appears before the container
#     exits, even if the skill code has buffered I/O.

_HARNESS_HEADER: str = textwrap.dedent("""\
    import json as _gate2_json
    import traceback as _gate2_tb
    _gate2_result = {"status": "fail", "error": None, "traceback": None}
    try:
""")

# Each line of skill source is indented by 4 spaces inside the try block.
_HARNESS_SKILL_INDENT: str = "    "

_HARNESS_FOOTER: str = (
    '        _gate2_result["status"] = "pass"\n'
    '    except Exception as _gate2_exc:\n'
    f'        _gate2_result["error"] = str(_gate2_exc)[:{_MAX_SKILL_ERROR_CHARS}]\n'
    f'        _gate2_result["traceback"] = _gate2_tb.format_exc()[:{_MAX_SKILL_TRACEBACK_CHARS}]\n'
    '    finally:\n'
    '        print(_gate2_json.dumps(_gate2_result), flush=True)\n'
)


def _build_harness(skill_source: str) -> str:
    """
    Wrap *skill_source* in the Gate 2 execution harness.

    The skill source is indented by 4 spaces and placed inside a try block.
    Assembly is done by string concatenation — no .format() or f-string
    injection of untrusted content.

    Args:
        skill_source: Skill Python source that has already passed Gate 1.

    Returns:
        Complete Python source ready for SandboxManager.run_python().
    """
    # Indent every line of skill source to sit inside the try block.
    indented_skill = "\n".join(
        _HARNESS_SKILL_INDENT + line
        for line in skill_source.splitlines()
    )
    # Plain concatenation — not .format(), not f-string (R-26 spirit).
    return _HARNESS_HEADER + indented_skill + "\n" + _HARNESS_FOOTER


# ---------------------------------------------------------------------------
# Result and exception dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate2Result:
    """
    Immutable record of a successful Gate 2 execution.

    Returned by DockerGate.run() when the skill executes without error
    inside the Docker container.  The caller may advance to Gate 3.

    Attributes:
        skill_name:       Identifier of the skill that was executed.
        exit_code:        Container exit code (always 0 on success).
        stdout_raw:       Raw stdout string from the container.
        execution_ms:     Wall-clock execution time in milliseconds.
        harness_status:   Value of the ``status`` field from the harness
                          JSON output (always ``"pass"`` on a Gate2Result).
    """

    skill_name:     str
    exit_code:      int
    stdout_raw:     str
    execution_ms:   float
    harness_status: str = _STATUS_PASS


class Gate2RejectedError(RuntimeError):
    """
    Raised by DockerGate.run() when the skill fails Gate 2 execution.

    A caller that receives this exception MUST NOT advance the skill to
    Gate 3.  The skill source must be discarded.

    Attributes:
        skill_name:    Identifier of the rejected skill.
        reason:        Short machine-readable reason code.  One of:
                         EXECUTION_FAILED   — skill raised an exception
                         TIMEOUT            — container exceeded time limit
                         DOCKER_ERROR       — Docker API / container error
                         SECURITY_ERROR     — seccomp / sandbox config error
                         STDOUT_EMPTY       — container produced no stdout
                         STDOUT_INVALID_JSON — stdout is not parseable JSON
                         STDOUT_SCHEMA_ERR  — stdout JSON missing required fields
        skill_error:   Error message from the skill (if status=="fail").
        skill_traceback: Truncated traceback from the skill (if available).
        exit_code:     Container exit code (-1 if unavailable).
    """

    def __init__(
        self,
        message: str,
        *,
        skill_name:      str = "",
        reason:          str = "EXECUTION_FAILED",
        skill_error:     str = "",
        skill_traceback: str = "",
        exit_code:       int = -1,
    ) -> None:
        super().__init__(message)
        self.skill_name      = skill_name
        self.reason          = reason
        self.skill_error     = skill_error
        self.skill_traceback = skill_traceback
        self.exit_code       = exit_code

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.skill_name:
            parts.append(f"skill={self.skill_name!r}")
        if self.exit_code != -1:
            parts.append(f"exit={self.exit_code}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# DockerGate
# ---------------------------------------------------------------------------

class DockerGate:
    """
    Gate 2 of the skill validation pipeline: ephemeral Docker execution.

    Executes skill source inside a network-isolated, read-only Docker
    container and verifies that the skill runs without raising an exception.
    The skill source must have passed Gate 1 (ASTGate) before being passed
    to this gate.

    A single DockerGate instance builds a new SandboxManager for every
    run() call to guarantee a fresh ephemeral container with no cross-task
    state (R-12: no container reuse).

    Usage::

        gate = DockerGate(cfg)

        try:
            result = gate.run(skill_source, skill_name="open_browser")
        except Gate2RejectedError as exc:
            logger.error("Gate 2 rejected skill", extra={"reason": exc.reason})
            raise   # do NOT advance to Gate 3

        # Gate 2 passed — proceed to Gate 3.
        schema_result = schema_gate.run(result, skill_path, skill_source)

    Args:
        cfg: Validated PhidipusConfig.  Gate 2 reads:
               cfg.skill_validator.gate2_timeout_seconds — execution timeout
               cfg.sandbox.*                             — Docker parameters
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._cfg = cfg
        self._gate2_timeout: float = cfg.skill_validator.gate2_timeout_seconds

        # Build a SandboxManagerConfig from the Phidipus config.
        # Gate 2 uses a stricter timeout than the general sandbox timeout.
        self._manager_cfg = SandboxManagerConfig(
            image                = cfg.sandbox.docker_image,
            seccomp_profile_path = Path(cfg.sandbox.seccomp_profile_path),
            default_memory       = cfg.sandbox.memory_limit,
            default_cpu_quota    = cfg.sandbox.cpu_quota,   # already int µs (FIX-3)
            default_timeout      = self._gate2_timeout,
        )

        _log.info(
            "DockerGate initialised (Gate 2 of 3)",
            extra={
                "docker_image":   cfg.sandbox.docker_image,
                "gate2_timeout":  self._gate2_timeout,
                "network_mode":   cfg.sandbox.network_mode,  # always "none"
                "memory_limit":   cfg.sandbox.memory_limit,
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
    ) -> Gate2Result:
        """
        Execute *source* inside an ephemeral Docker container and verify
        that it runs without error.

        Processing pipeline:
          1. Wrap skill source in the execution harness (_build_harness).
          2. Create a fresh SandboxManager (new instance → new container).
          3. Run via SandboxManager.run_python() — source delivered via
             stdin to avoid /proc/cmdline exposure (R-15 / FIX-1).
          4. Check container exit code and stdout presence.
          5. Parse stdout as JSON.
          6. Verify harness output schema (status, error, traceback fields).
          7. Check status == "pass".

        Only skills that survive all 7 steps without error may advance to
        Gate 3.  Any failure raises Gate2RejectedError.

        Args:
            source:     Skill source that has already passed Gate 1.
                        Must be a non-empty str.
            skill_name: Optional identifier used in log messages.

        Returns:
            Gate2Result on success.

        Raises:
            Gate2RejectedError: on any execution, timeout, Docker, or
                                harness-output failure (R-08).
            TypeError:          if source is not a str.
        """
        if not isinstance(source, str):
            raise TypeError(
                f"DockerGate.run() requires a str source, "
                f"got {type(source).__name__!r}."
            )

        _log.debug(
            "Gate 2: starting Docker execution",
            extra={
                "skill_name":  skill_name,
                "source_chars": len(source),
                "timeout":     self._gate2_timeout,
            },
        )

        # ── Step 1: build harness ─────────────────────────────────────────
        harness_code = _build_harness(source)

        # ── Steps 2-3: run in ephemeral container ─────────────────────────
        # A new SandboxManager is created per run() call.  SandboxManager
        # creates a new DockerSandbox per execution, which guarantees a
        # fresh container with no cross-task state (R-12).
        manager = SandboxManager(self._manager_cfg)

        try:
            result = manager.run_python(harness_code)
        except SandboxTimeoutError as exc:
            _log.error(
                "R-08: Gate 2 REJECTED — container timed out",
                extra={
                    "skill_name": skill_name,
                    "timeout":    self._gate2_timeout,
                    "reason":     "TIMEOUT",
                },
            )
            raise Gate2RejectedError(
                f"Skill {skill_name!r} Gate 2 execution timed out after "
                f"{self._gate2_timeout:.1f}s.",
                skill_name=skill_name,
                reason="TIMEOUT",
            ) from exc

        except SandboxSecurityError as exc:
            _log.error(
                "R-08: Gate 2 REJECTED — sandbox security error",
                extra={
                    "skill_name": skill_name,
                    "error":      str(exc),
                    "reason":     "SECURITY_ERROR",
                },
            )
            raise Gate2RejectedError(
                f"Skill {skill_name!r} Gate 2 sandbox security error: {exc}",
                skill_name=skill_name,
                reason="SECURITY_ERROR",
            ) from exc

        except SandboxExecutionError as exc:
            _log.error(
                "R-08: Gate 2 REJECTED — Docker execution error",
                extra={
                    "skill_name": skill_name,
                    "error":      str(exc),
                    "exit_code":  exc.exit_code,
                    "reason":     "DOCKER_ERROR",
                },
            )
            raise Gate2RejectedError(
                f"Skill {skill_name!r} Gate 2 Docker execution error: {exc}",
                skill_name=skill_name,
                reason="DOCKER_ERROR",
                exit_code=exc.exit_code,
            ) from exc

        except Exception as exc:
            # Catch-all for unexpected errors from SandboxManager or Docker SDK
            # (e.g. DockerException, ConnectionError, OSError) that are not
            # covered by the three typed sandbox exceptions above.
            # Without this guard, an untyped exception would propagate out of
            # run() and break the caller's assumption that failures always
            # arrive as Gate2RejectedError (R-08 contract).
            _log.error(
                "R-08: Gate 2 REJECTED — unexpected error during Docker execution",
                extra={
                    "skill_name": skill_name,
                    "error":      str(exc),
                    "error_type": type(exc).__name__,
                    "reason":     "DOCKER_ERROR",
                },
                exc_info=True,
            )
            raise Gate2RejectedError(
                f"Skill {skill_name!r} Gate 2 unexpected error "
                f"({type(exc).__name__}): {exc}",
                skill_name=skill_name,
                reason="DOCKER_ERROR",
                exit_code=-1,
            ) from exc

        execution_ms = result.runtime_ms

        # ── Step 4: check stdout presence ─────────────────────────────────
        stdout = result.stdout.strip() if result.stdout else ""
        if not stdout:
            _log.error(
                "R-08: Gate 2 REJECTED — container produced no stdout",
                extra={
                    "skill_name": skill_name,
                    "exit_code":  result.exit_code,
                    "stderr":     (result.stderr or "")[:_MAX_STDOUT_IN_LOG],
                    "reason":     "STDOUT_EMPTY",
                },
            )
            raise Gate2RejectedError(
                f"Skill {skill_name!r} Gate 2 produced no stdout "
                f"(exit_code={result.exit_code}). "
                "Container may have crashed before the harness could print output.",
                skill_name=skill_name,
                reason="STDOUT_EMPTY",
                exit_code=result.exit_code,
            )

        # ── Step 5: parse stdout as JSON ──────────────────────────────────
        try:
            harness_output = json_loads(stdout)
        except JsonDecodeError as exc:
            _log.error(
                "R-08: Gate 2 REJECTED — stdout is not valid JSON",
                extra={
                    "skill_name":     skill_name,
                    "stdout_snippet": stdout[:_MAX_STDOUT_IN_LOG],
                    "error":          str(exc),
                    "reason":         "STDOUT_INVALID_JSON",
                },
            )
            raise Gate2RejectedError(
                f"Skill {skill_name!r} Gate 2 stdout is not valid JSON: {exc}. "
                f"Stdout snippet: {stdout[:_MAX_STDOUT_IN_LOG]!r}",
                skill_name=skill_name,
                reason="STDOUT_INVALID_JSON",
                exit_code=result.exit_code,
            ) from exc

        # ── Step 6: verify harness output schema ──────────────────────────
        if not isinstance(harness_output, dict):
            _log.error(
                "R-08: Gate 2 REJECTED — harness output is not a JSON object",
                extra={
                    "skill_name":     skill_name,
                    "stdout_snippet": stdout[:_MAX_STDOUT_IN_LOG],
                    "reason":         "STDOUT_SCHEMA_ERR",
                },
            )
            raise Gate2RejectedError(
                f"Skill {skill_name!r} Gate 2 harness output is not a JSON object.",
                skill_name=skill_name,
                reason="STDOUT_SCHEMA_ERR",
                exit_code=result.exit_code,
            )

        for required_field in ("status", "error", "traceback"):
            if required_field not in harness_output:
                _log.error(
                    "R-08: Gate 2 REJECTED — harness output missing required field",
                    extra={
                        "skill_name":    skill_name,
                        "missing_field": required_field,
                        "reason":        "STDOUT_SCHEMA_ERR",
                    },
                )
                raise Gate2RejectedError(
                    f"Skill {skill_name!r} Gate 2 harness output is missing "
                    f"required field {required_field!r}.",
                    skill_name=skill_name,
                    reason="STDOUT_SCHEMA_ERR",
                    exit_code=result.exit_code,
                )

        # ── Step 7: check harness status ──────────────────────────────────
        status: str = harness_output.get("status", _STATUS_FAIL)

        if status != _STATUS_PASS:
            skill_error     = str(harness_output.get("error") or "")
            skill_traceback = str(harness_output.get("traceback") or "")

            _log.error(
                "R-08: Gate 2 REJECTED — skill raised an exception",
                extra={
                    "skill_name":     skill_name,
                    "skill_error":    skill_error[:_MAX_STDOUT_IN_LOG],
                    "exit_code":      result.exit_code,
                    "reason":         "EXECUTION_FAILED",
                },
            )
            raise Gate2RejectedError(
                f"Skill {skill_name!r} Gate 2 execution failed: {skill_error}",
                skill_name=skill_name,
                reason="EXECUTION_FAILED",
                skill_error=skill_error,
                skill_traceback=skill_traceback,
                exit_code=result.exit_code,
            )

        # ── Gate 2 PASS ───────────────────────────────────────────────────
        gate2_result = Gate2Result(
            skill_name=skill_name,
            exit_code=result.exit_code,
            stdout_raw=stdout,
            execution_ms=execution_ms,
            harness_status=_STATUS_PASS,
        )

        _log.info(
            "R-08: Gate 2 PASSED — skill may advance to Gate 3",
            extra={
                "skill_name":   gate2_result.skill_name,
                "exit_code":    gate2_result.exit_code,
                "execution_ms": gate2_result.execution_ms,
            },
        )
        return gate2_result
