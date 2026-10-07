# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
sandbox/sandbox_manager.py — Phidipus v1.0.1
High-level sandbox orchestration layer.

Architecture contract (C.7):
  "Every action dispatched to the host OS runs inside an ephemeral container
   with the minimal capability surface required for the action and zero
   network access."

SandboxManager is the single entry point for all callers that want to execute
something in the sandbox.  It sits between the IPC layer and DockerSandbox,
providing three conveniences:

  1. Resource governance — delegates to ResourceMonitor to apply per-action
     resource profiles and clamp parameters to the policy ceiling before any
     DockerSandbox is created.
  2. Lifecycle management — creates ephemeral DockerSandbox instances, runs
     the requested command / action, and disposes of the sandbox.
  3. Error translation — catches SandboxTimeoutError and SandboxExecutionError
     from DockerSandbox and re-raises them unchanged, so callers only need to
     depend on this module's exception types (which are re-exported from
     docker_runner for convenience).

Security invariants:
  SandboxManager enforces R-11 through R-16 exclusively through the
  SandboxConfig that it passes to DockerSandbox.  It never calls Docker
  directly.  The security contract is: if DockerSandbox accepts the config
  (i.e. does not raise SandboxSecurityError at construction time), all
  invariants are satisfied.

Layer position:
  IPC layer → SandboxManager → ResourceMonitor → DockerSandbox → Docker

Thread safety:
  SandboxManager is NOT thread-safe.  Create one instance per worker thread
  or asyncio task, matching the thread-safety contract of DockerSandbox.

Dependencies:
  sandbox.docker_runner      — DockerSandbox, SandboxConfig, ExecutionResult
  sandbox.resource_limiter   — ResourceLimits, ResourceMonitor
  utils.logger               — structured JSON logging

Patch v9.11.1:
  FIX-2  run_python() previously passed Python source code as a ``-c`` CLI
         argument::

             cmd = ["python3", "-c", code]

         This made the code visible in /proc/<pid>/cmdline and ps(1), violating
         R-15.  It now passes the source code via stdin::

             cmd = ["python3", "-"]   # python3 reads source from stdin
             stdin_data = code.encode("utf-8")

         _execute() gains an optional ``stdin_data`` parameter so the bytes
         can be forwarded through to DockerSandbox.run_command() which handles
         the actual stdin attachment (see docker_runner.py FIX-1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from utils.logger import get_logger

from sandbox.docker_runner import (
    DockerSandbox,
    ExecutionResult,
    SandboxConfig,
    SandboxExecutionError,
    SandboxSecurityError,
    SandboxTimeoutError,
    create_sandbox,
)
from sandbox.resource_limiter import (
    ResourceExceededError,
    ResourceLimits,
    ResourceMonitor,
)

_log = get_logger("sandbox.sandbox_manager", process="daemon")

# ---------------------------------------------------------------------------
# Re-export exception types so callers can catch them from this module.
# ---------------------------------------------------------------------------
__all__ = [
    "SandboxManager",
    "SandboxManagerConfig",
    "SandboxExecutionError",
    "SandboxSecurityError",
    "SandboxTimeoutError",
    "ResourceExceededError",
]

# ---------------------------------------------------------------------------
# SandboxManagerConfig — top-level configuration for the manager
# ---------------------------------------------------------------------------


class SandboxManagerConfig:
    """
    Configuration bundle for :class:`SandboxManager`.

    Combines Docker-level parameters (image, seccomp profile) with
    resource-policy parameters (ResourceLimits) and behavioural flags.

    Args:
        image:                Docker image used for all containers.
        seccomp_profile_path: Path to the Phidipus seccomp JSON profile.
        limits:               Resource policy ceilings.  Defaults to the
                              system-wide ResourceLimits defaults.
        default_memory:       Base memory limit for containers (before any
                              per-action profile is applied).
        default_cpu_quota:    Base CPU quota in µs per period.
        default_cpu_period:   CFS period in µs.
        default_pids_limit:   Base PID limit.
        default_tmpfs_size:   Base /tmp size.
        default_timeout:      Base wall-clock timeout in seconds.
        working_dir:          Working directory inside the container.
        environment:          Extra environment variables injected into every
                              container.  Sensitive values must not be passed.
        apply_action_profiles: If True (default), per-action resource profiles
                              from ACTION_PROFILES are applied before running.
    """

    def __init__(
        self,
        image: str,
        seccomp_profile_path: Union[str, Path],
        *,
        limits: Optional[ResourceLimits] = None,
        default_memory: str = "256m",
        default_cpu_quota: int = 50_000,
        default_cpu_period: int = 100_000,
        default_pids_limit: int = 64,
        default_tmpfs_size: str = "64m",
        default_timeout: float = 30.0,
        working_dir: str = "/workspace",
        environment: Optional[dict[str, str]] = None,
        apply_action_profiles: bool = True,
    ) -> None:
        self.image = image
        self.seccomp_profile_path = Path(seccomp_profile_path)
        self.limits = limits if limits is not None else ResourceLimits()
        self.default_memory = default_memory
        self.default_cpu_quota = default_cpu_quota
        self.default_cpu_period = default_cpu_period
        self.default_pids_limit = default_pids_limit
        self.default_tmpfs_size = default_tmpfs_size
        self.default_timeout = default_timeout
        self.working_dir = working_dir
        self.environment = environment or {}
        self.apply_action_profiles = apply_action_profiles


# ---------------------------------------------------------------------------
# SandboxManager
# ---------------------------------------------------------------------------


class SandboxManager:
    """
    High-level orchestration layer for sandbox execution.

    Every public method creates a fresh ephemeral :class:`DockerSandbox`,
    runs the requested operation, and disposes of the sandbox on completion
    (regardless of success or failure).  Resource limits are applied via
    :class:`ResourceMonitor` before any sandbox is created.

    Usage::

        cfg = SandboxManagerConfig(
            image="python:3.11-slim",
            seccomp_profile_path="sandbox/seccomp_profile.json",
        )
        manager = SandboxManager(cfg)

        result = manager.run_command(["python3", "--version"])
        print(result.stdout)   # "Python 3.11.x\\n"

        result = manager.run_python("print(1 + 1)")
        print(result.stdout)   # "2\\n"

        action = {
            "type": "REQUEST",
            "action": "mouse_click",
            "payload": {"x": 100, "y": 200},
        }
        result = manager.run_action(action)

    Thread safety:
        Not thread-safe.  Create one instance per thread.
    """

    def __init__(self, config: SandboxManagerConfig) -> None:
        """
        Initialise the manager.

        Security invariants are enforced at the DockerSandbox level; this
        constructor validates only that the manager config is internally
        consistent.

        Args:
            config: Manager configuration.

        Raises:
            SandboxSecurityError: if the seccomp profile is absent or invalid
                                   (propagated from DockerSandbox validation
                                   performed at sandbox-creation time).
        """
        self._config = config
        self._monitor = ResourceMonitor(config.limits)

        _log.info(
            "SandboxManager initialised",
            extra={
                "image":                  config.image,
                "seccomp_profile":        str(config.seccomp_profile_path),
                "default_memory":         config.default_memory,
                "default_timeout":        config.default_timeout,
                "apply_action_profiles":  config.apply_action_profiles,
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_command(self, cmd: list[str]) -> ExecutionResult:
        """
        Execute *cmd* in an ephemeral sandbox container.

        Resources are taken from the manager's default config (no per-action
        profile is applied; use :meth:`run_action` for action-specific tuning).

        ``run_command`` is for explicit, non-payload commands only (e.g.
        ``["python3", "--version"]``).  Never embed user-controlled data in
        *cmd*; use :meth:`run_python` or :meth:`run_action` instead.

        Args:
            cmd: Command and arguments, e.g. ``["python3", "--version"]``.

        Returns:
            :class:`ExecutionResult` with stdout, stderr, exit code and timing.

        Raises:
            ValueError:            if *cmd* is empty or contains non-strings.
            SandboxTimeoutError:   if the container exceeds the timeout.
            SandboxExecutionError: on Docker API or container errors.
            SandboxSecurityError:  if the seccomp profile has been removed
                                   since initialisation.
        """
        if not cmd or not isinstance(cmd, list):
            raise ValueError("cmd must be a non-empty list of strings")
        if not all(isinstance(c, str) for c in cmd):
            raise ValueError("every element of cmd must be a str")

        _log.info(
            "SandboxManager.run_command",
            extra={"cmd": cmd},
        )

        sandbox_cfg = self._build_base_sandbox_config()
        # run_command carries no payload in stdin; pass stdin_data=None.
        return self._execute(sandbox_cfg, cmd=cmd, stdin_data=None)

    def run_python(self, code: str) -> ExecutionResult:
        """
        Execute a Python code snippet inside an ephemeral sandbox container.

        Patch v9.11.1 / FIX-2 — R-15 compliance:
            The Python source code is now passed via container stdin using the
            ``python3 -`` idiom (Python reads source from stdin when the script
            name is ``-``).  The previous implementation passed the code as a
            ``-c`` CLI argument::

                cmd = ["python3", "-c", code]   # OLD: visible in /proc/cmdline

            The new implementation::

                cmd = ["python3", "-"]           # NEW: reads source from stdin
                stdin_data = code.encode("utf-8")

            This ensures the code is never visible in /proc/<pid>/cmdline,
            ps(1), or any process-listing tool.

        Args:
            code: Python source code string.

        Returns:
            :class:`ExecutionResult`.

        Raises:
            ValueError:            if *code* is empty or not a string.
            SandboxTimeoutError:   if the container exceeds the timeout.
            SandboxExecutionError: on Docker API or container errors.
        """
        if not isinstance(code, str):
            raise ValueError(
                f"code must be a str, got {type(code).__name__}"
            )
        if not code.strip():
            raise ValueError("code must not be empty or whitespace-only")

        _log.info(
            "SandboxManager.run_python",
            extra={"code_length": len(code)},
        )

        # R-15: python3 - reads Python source from stdin.
        # The code is encoded to bytes and passed via _execute → run_command
        # → DockerSandbox._run (stdin path).
        cmd = ["python3", "-"]
        stdin_data = code.encode("utf-8")

        sandbox_cfg = self._build_base_sandbox_config()
        return self._execute(sandbox_cfg, cmd=cmd, stdin_data=stdin_data)

    def run_action(self, action: dict[str, Any]) -> ExecutionResult:
        """
        Serialise *action* and execute it inside an ephemeral sandbox via
        the Phidipus action-runner entry point.

        Per-action resource profiles are applied when
        ``SandboxManagerConfig.apply_action_profiles`` is True (the default).

        The caller is responsible for validating the action against
        ``ipc.action_schema`` before calling this method.

        R-15 compliance is handled at the :class:`DockerSandbox` layer
        (``run_action()`` in docker_runner.py FIX-1) — the action JSON is
        passed via stdin, not as a CLI argument.

        Args:
            action: Action message dict (already schema-validated).

        Returns:
            :class:`ExecutionResult`.

        Raises:
            ValueError:            if *action* is not a dict.
            SandboxTimeoutError:   if the container exceeds the timeout.
            SandboxExecutionError: on Docker API or container errors.
        """
        if not isinstance(action, dict):
            raise ValueError(
                f"action must be a dict, got {type(action).__name__}"
            )

        action_name: str = action.get("action", "")

        _log.info(
            "SandboxManager.run_action",
            extra={
                "action_name": action_name,
                "action_type": action.get("type", ""),
                "cid":         action.get("correlation_id", ""),
            },
        )

        sandbox_cfg = self._build_sandbox_config_for_action(action_name)

        with DockerSandbox(sandbox_cfg) as sandbox:
            return sandbox.run_action(action)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_base_sandbox_config(self) -> SandboxConfig:
        """
        Construct a base SandboxConfig from the manager defaults and clamp it
        to the resource policy ceiling.

        Returns:
            Validated, clamped SandboxConfig.
        """
        base = SandboxConfig(
            image=self._config.image,
            seccomp_profile_path=self._config.seccomp_profile_path,
            memory_limit=self._config.default_memory,
            cpu_quota=self._config.default_cpu_quota,
            cpu_period=self._config.default_cpu_period,
            pids_limit=self._config.default_pids_limit,
            tmpfs_size=self._config.default_tmpfs_size,
            timeout_seconds=self._config.default_timeout,
            working_dir=self._config.working_dir,
            environment=dict(self._config.environment),
        )
        return self._monitor.clamp_config(base)

    def _build_sandbox_config_for_action(self, action_name: str) -> SandboxConfig:
        """
        Construct a SandboxConfig tailored for *action_name*.

        If action profiles are enabled and a profile exists for this action,
        the profile overrides the default resource parameters (then clamped).
        Otherwise falls back to the base config.

        Args:
            action_name: IPC action name string.

        Returns:
            Validated, clamped SandboxConfig.
        """
        base = self._build_base_sandbox_config()

        if self._config.apply_action_profiles and action_name:
            return self._monitor.apply_action_profile(action_name, base)

        return base

    def _execute(
        self,
        sandbox_cfg: SandboxConfig,
        *,
        cmd: list[str],
        stdin_data: bytes | None = None,
    ) -> ExecutionResult:
        """
        Create an ephemeral DockerSandbox, run *cmd* (optionally with
        *stdin_data*), and return the result.

        The sandbox is always disposed of in a ``finally`` block, whether
        execution succeeds, times out, or raises an unexpected error.

        Patch v9.11.1 / FIX-2:
            Added ``stdin_data`` parameter.  When provided, it is forwarded
            to ``DockerSandbox.run_command()`` which activates the R-15
            stdin delivery path (create + start + attach_socket write).

        Args:
            sandbox_cfg: Pre-built, clamped SandboxConfig.
            cmd:         Command list to execute.
            stdin_data:  Optional bytes to pipe into container stdin (R-15).

        Returns:
            ExecutionResult.

        Raises:
            SandboxTimeoutError:   propagated from DockerSandbox.
            SandboxExecutionError: propagated from DockerSandbox.
            SandboxSecurityError:  propagated from DockerSandbox.
        """
        with DockerSandbox(sandbox_cfg) as sandbox:
            result = sandbox.run_command(cmd, stdin_data=stdin_data)

        _log.debug(
            "SandboxManager execution complete",
            extra={
                "exit_code":  result.exit_code,
                "runtime_ms": result.runtime_ms,
                "timed_out":  result.timed_out,
            },
        )
        return result
