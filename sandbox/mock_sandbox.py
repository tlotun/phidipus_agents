# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
sandbox/mock_sandbox.py — Phidipus v1.0
In-process mock sandbox for unit testing ONLY.

Architecture contract (R-07 / R-12):
  "Remove MockSandbox from production routing.  Retained in test fixtures
   only with explicit guard: if not os.environ.get('PHIDIPUS_TEST_MODE'):
   raise RuntimeError."

This module provides a MockSandbox class for use in unit tests that need
to exercise the sandbox interface without spinning up Docker containers.
MockSandbox is NEVER loaded in production — the runtime guard at the top
of every public method raises RuntimeError if PHIDIPUS_TEST_MODE != "1".

Production routing (sandbox_router.py) does NOT import this module.
The module-load assertion in sandbox_router.py verifies that "MOCK" is
absent from _PERMITTED_TIERS.

Usage (test code only):

    import os
    os.environ["PHIDIPUS_TEST_MODE"] = "1"
    from sandbox.mock_sandbox import MockSandbox

    sandbox = MockSandbox()
    result  = sandbox.run_python("print('hello')")
    assert result.stdout == "hello\\n"

Process: N/A — test fixtures only

Security invariants enforced here:
  R-07  MockSandbox is never called in the host process on LLM-generated
        code in production.  The PHIDIPUS_TEST_MODE guard prevents this.
  R-12  MockSandbox is not reachable through any production code path.
        sandbox_router.py asserts this at import time.

Used by:
  tests/ — unit tests for code that depends on the sandbox interface

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Runtime guard helper
# ---------------------------------------------------------------------------

def _assert_test_mode() -> None:
    """
    Raise RuntimeError if PHIDIPUS_TEST_MODE != "1".

    Called at the start of every public MockSandbox method to prevent
    accidental production use (R-07, R-12).
    """
    if os.environ.get("PHIDIPUS_TEST_MODE") != "1":
        raise RuntimeError(
            "MockSandbox may only be used in test mode.  "
            "Set PHIDIPUS_TEST_MODE=1 in the test environment before "
            "importing or calling MockSandbox.  "
            "This class must never be called from production code (R-12)."
        )


# ---------------------------------------------------------------------------
# Fake ExecutionResult (mirrors docker_runner.ExecutionResult interface)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FakeExecutionResult:
    """
    Test-only drop-in for ExecutionResult with the same field names.

    Attributes:
        stdout:    Captured standard output.
        stderr:    Captured standard error.
        exit_code: Simulated process exit code.
        timed_out: Whether the execution was simulated as timed out.
    """

    stdout:    str  = ""
    stderr:    str  = ""
    exit_code: int  = 0
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        """True iff exit_code == 0 and not timed_out."""
        return self.exit_code == 0 and not self.timed_out


# ---------------------------------------------------------------------------
# MockSandbox
# ---------------------------------------------------------------------------

class MockSandbox:
    """
    In-process mock sandbox for unit testing only.

    Executes Python code in the current process using exec() on a
    restricted namespace.  This is intentionally unsafe and is why the
    PHIDIPUS_TEST_MODE guard exists — production code must never reach
    this class.

    Configurable outcomes:
      - Default: execute code and capture stdout/stderr via StringIO.
      - force_fail: always return exit_code=1 with the given stderr.
      - force_timeout: simulate a timeout (returns timed_out=True).

    Usage (test only)::

        import os
        os.environ["PHIDIPUS_TEST_MODE"] = "1"

        sb = MockSandbox()
        r  = sb.run_python("x = 1 + 1; print(x)")
        assert r.stdout == "2\\n"
        assert r.exit_code == 0

        sb_fail = MockSandbox(force_fail=True, fail_stderr="oops")
        r2 = sb_fail.run_python("print('hello')")
        assert r2.exit_code == 1

    Args:
        force_fail:    If True, every run() returns exit_code=1.
        fail_stderr:   stderr content when force_fail is True.
        force_timeout: If True, every run() returns timed_out=True.
    """

    def __init__(
        self,
        *,
        force_fail:    bool = False,
        fail_stderr:   str  = "MockSandbox: forced failure",
        force_timeout: bool = False,
    ) -> None:
        _assert_test_mode()
        self._force_fail    = force_fail
        self._fail_stderr   = fail_stderr
        self._force_timeout = force_timeout

        _log.debug(
            "MockSandbox initialised (TEST MODE ONLY)",
            extra={
                "force_fail":    force_fail,
                "force_timeout": force_timeout,
            },
        )

    def run_python(self, code: str) -> FakeExecutionResult:
        """
        Execute *code* in-process and return a FakeExecutionResult.

        PHIDIPUS_TEST_MODE must be set to "1" or RuntimeError is raised.

        Args:
            code: Python source to execute.

        Returns:
            FakeExecutionResult with captured stdout/stderr.
        """
        _assert_test_mode()

        if self._force_timeout:
            return FakeExecutionResult(
                stdout="", stderr="", exit_code=-1, timed_out=True
            )

        if self._force_fail:
            return FakeExecutionResult(
                stdout="", stderr=self._fail_stderr, exit_code=1
            )

        import io
        import sys

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        captured_out = io.StringIO()
        captured_err = io.StringIO()
        exit_code = 0

        try:
            sys.stdout = captured_out
            sys.stderr = captured_err
            # FIX M-04: defense-in-depth — double-check test mode before exec().
            # __init__ already checks, but this guard catches monkey-patching.
            _assert_test_mode()
            exec(code, {"__builtins__": __builtins__})  # noqa: S102  # test-only
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:
            captured_err.write(f"{type(exc).__name__}: {exc}\n")
            exit_code = 1
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        return FakeExecutionResult(
            stdout=captured_out.getvalue(),
            stderr=captured_err.getvalue(),
            exit_code=exit_code,
        )

    def run_command(self, cmd: list[str]) -> FakeExecutionResult:
        """
        Simulate running a shell command (returns empty stdout, exit 0).

        Real command execution is never performed — this is for interface
        compatibility in tests only.
        """
        _assert_test_mode()
        if self._force_fail:
            return FakeExecutionResult(stderr=self._fail_stderr, exit_code=1)
        if self._force_timeout:
            return FakeExecutionResult(exit_code=-1, timed_out=True)
        # Simulate success
        return FakeExecutionResult(stdout="", stderr="", exit_code=0)
