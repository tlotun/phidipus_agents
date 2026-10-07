# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
sandbox/docker_runner.py — Phidipus v1.0.2
Docker-based execution sandbox for the automation daemon.

Architecture contract:
  "Every action dispatched to the host OS runs inside an ephemeral container
   with the minimal capability surface required for the action and zero
   network access."  (Architecture C.7)

Security enforcement (mapped to project requirements):
  R-11  No namespace escape     — no_new_privileges=True; seccomp blocks setns/unshare
  R-12  No network access       — network_mode="none"
  R-13  No kernel module load   — seccomp profile; CAP_SYS_MODULE dropped
  R-14  No privilege escalation — privileged=False; cap_drop=["ALL"]; no_new_privs
  R-15  Stdin-only container input — action JSON and Python code are piped via
                                     stdin; no payload appears in /proc/cmdline
  R-16  Minimal syscall surface — seccomp_profile applied at container start

Design:
  - All security parameters are validated *before* the Docker API is called.
    A SandboxSecurityError is raised synchronously if any invariant is
    violated; no partial container creation occurs.
  - Containers are always removed in a finally block; auto_remove is not
    used because we need to capture logs after the container exits.
  - Container stdout/stderr are captured in-memory; they are never written
    to disk by this module.
  - No persistent volumes are mounted.  /tmp is a size-limited tmpfs.
  - The root filesystem is mounted read-only; the only writable path is /tmp.
  - Resource limits (memory, CPU, PIDs) are enforced at container level.
  - DockerSandbox is usable as a context manager for deterministic cleanup.

Patch v9.11.1:
  FIX-1  run_action() previously passed the action JSON as a CLI argument:
             cmd = ["python3", "-m", "ipc.action_runner", action_json]
         This violated R-15 because the payload was visible in
         /proc/<pid>/cmdline, ps(1), and docker inspect.  It now pipes the
         JSON to the container via stdin:
             cmd = ["python3", "-m", "ipc.action_runner"]
             stdin_data = action_json.encode("utf-8")
         The container entrypoint must read the action from sys.stdin.

  FIX-1  run_command() gains an optional stdin_data parameter so that
         SandboxManager.run_python() (FIX-2) can pass Python source code
         via stdin rather than via the -c CLI flag.

  FIX-2  _run() now has two execution paths:
         • No stdin_data  → original containers.run() (detach=True) path.
         • With stdin_data → containers.create() + container.start() +
           attach_socket() write path that fulfils R-15.

Used by:
  daemon/action_executor.py — execute automation actions in isolation
  skill_validator/docker_runner.py  — validate generated skill outputs

Dependencies:
  docker  (pip install docker)   — Docker SDK for Python ≥ 6.0
  utils/logger.py                — structured JSON logging
  utils/json_utils.py            — JSON serialisation for action payloads

Thread safety:
  - DockerSandbox instances are NOT thread-safe.  Create one instance per
    worker thread / asyncio task.
  - The Docker SDK client is created per-instance to avoid shared socket
    state across threads.
"""

from __future__ import annotations

import os
import socket as _socket_module
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from utils.json_utils import dumps as json_dumps, loads as json_loads, JsonDecodeError
from utils.logger import get_logger

_log = get_logger("sandbox.docker_runner", process="daemon")

# ---------------------------------------------------------------------------
# Docker SDK — single, centralized import
# ---------------------------------------------------------------------------
# The import is attempted once at module load time so there is exactly one
# place in the codebase that touches "import docker".  If the package is
# absent, _docker is set to None and _DOCKER_IMPORT_ERROR preserves the
# original exception so it can be chained when _get_client() is called.
# Nothing else in this module uses a bare "import docker" statement.
# ---------------------------------------------------------------------------
try:
    import docker as _docker          # type: ignore[import]
    _DOCKER_IMPORT_ERROR: Optional[ImportError] = None
except ImportError as _exc:
    _docker = None                    # type: ignore[assignment]
    _DOCKER_IMPORT_ERROR = _exc

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SandboxSecurityError(RuntimeError):
    """
    Raised when a requested container configuration violates a security
    invariant (R-11 through R-16).  The container is NEVER created when
    this exception is raised.

    Attributes:
        rule:    The requirement code that was violated (e.g. "R-12").
        detail:  Human-readable description of the violation.
    """

    def __init__(self, detail: str, *, rule: str = "") -> None:
        super().__init__(f"[{rule}] {detail}" if rule else detail)
        self.rule = rule
        self.detail = detail


class SandboxTimeoutError(RuntimeError):
    """
    Raised when a container exceeds its allowed execution time and is
    forcibly stopped.

    Attributes:
        timeout_seconds: The limit that was exceeded.
        cmd:             The command that timed out (may be empty for actions).
    """

    def __init__(self, timeout_seconds: float, cmd: str = "") -> None:
        msg = f"Container exceeded timeout of {timeout_seconds:.1f}s"
        if cmd:
            msg += f" while running: {cmd!r}"
        super().__init__(msg)
        self.timeout_seconds = timeout_seconds
        self.cmd = cmd


class SandboxExecutionError(RuntimeError):
    """
    Raised when container execution fails for a non-security, non-timeout
    reason (e.g. Docker daemon unreachable, image not found, OOM kill).

    Attributes:
        exit_code: Container exit code if available, or -1.
    """

    def __init__(self, detail: str, *, exit_code: int = -1) -> None:
        super().__init__(detail)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionResult:
    """
    Immutable record of a completed container execution.

    Attributes:
        stdout:      Captured standard output (UTF-8, errors replaced).
        stderr:      Captured standard error (UTF-8, errors replaced).
        exit_code:   Process exit code from inside the container.
        runtime_ms:  Wall-clock time in milliseconds from container start
                     to exit (or forced stop).
        timed_out:   True if the container was killed due to timeout.
    """

    stdout: str
    stderr: str
    exit_code: int
    runtime_ms: float
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        """Return True iff exit_code == 0 and the container did not time out."""
        return self.exit_code == 0 and not self.timed_out


@dataclass
class SandboxConfig:
    """
    Immutable (post-construction) configuration for a DockerSandbox.

    All security-relevant fields are validated by DockerSandbox.__init__
    before any Docker API call.  Callers may not override security fields
    after construction.

    Args:
        image:                Docker image name, e.g. ``"python:3.11-slim"``.
        seccomp_profile_path: Path to the Phidipus seccomp JSON profile.
                              The file must exist and be readable.
        memory_limit:         Container memory limit in Docker format,
                              e.g. ``"256m"``.  Default: ``"256m"``.
        cpu_quota:            CPU quota in microseconds per *cpu_period*.
                              Default 50000 = 50 % of one core per period.
        cpu_period:           CFS period in microseconds.  Default: 100000.
        pids_limit:           Maximum number of PIDs inside the container.
                              Default: 64.
        tmpfs_size:           Size of the /tmp tmpfs mount.  Default: ``"64m"``.
        timeout_seconds:      Maximum allowed container wall-clock runtime.
                              The container is killed after this many seconds.
                              Default: 30.0.
        working_dir:          Working directory inside the container.
                              Default: ``"/workspace"``.
        environment:          Additional environment variables to inject.
                              Sensitive values must never be passed here.
    """

    image: str
    seccomp_profile_path: Union[str, Path]

    memory_limit: str = "256m"
    cpu_quota: int = 50_000      # 50 % of one CPU core
    cpu_period: int = 100_000    # 100 ms CFS period
    pids_limit: int = 64
    tmpfs_size: str = "64m"
    timeout_seconds: float = 30.0
    working_dir: str = "/workspace"
    environment: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.seccomp_profile_path = Path(self.seccomp_profile_path)


# ---------------------------------------------------------------------------
# Seccomp profile strategy — why a single profile is used for all tiers
# ---------------------------------------------------------------------------
# Phidipus v1.0 uses one shared seccomp profile (seccomp_profile.json) for
# all Docker sandbox tiers rather than separate per-tier profiles.  This is
# a deliberate design decision for the following reasons:
#
#   1. Python runtime requirements — the CPython interpreter requires a
#      non-trivial set of syscalls (clone, futex, mmap, brk, execve, etc.)
#      to start and operate correctly.  A more restrictive profile that
#      removes any of these would break Python execution inside the container,
#      making skill execution impossible regardless of tier.
#
#   2. Container isolation already restricts capabilities — all containers
#      run with --cap-drop=ALL, no_new_privileges=True, read_only root FS,
#      and --network=none.  The seccomp layer provides a second line of
#      defence (blocking raw socket and namespace syscalls) on top of these
#      capability restrictions.  Additional per-tier profiles would add
#      administrative complexity without meaningfully reducing the attack
#      surface beyond what capability dropping already provides.
#
#   3. Tier differentiation via resource limits — STRICT vs LOW-STRICT tiers
#      differ in memory/CPU/PID quotas (enforced by ResourceMonitor), not in
#      syscall surface.  Using ResourceLimits for tier policy keeps the
#      security model auditable and avoids maintaining multiple JSON profiles
#      that could drift out of sync.
#
# The defaultAction is SCMP_ACT_ERRNO (deny-all); only the syscalls explicitly
# listed in the allowlist are permitted.  Network syscalls (socket, connect,
# bind, sendto, recvfrom, etc.) and namespace escape vectors (setns, unshare,
# clone3, ptrace) are explicitly denied for auditability per R-11 through R-16.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Security validation
# ---------------------------------------------------------------------------

_REQUIRED_SECURITY_PARAMS: dict[str, Any] = {
    "network_disabled": True,    # R-12
    "privileged":        False,   # R-14
    "read_only":         True,    # defence-in-depth: read-only root FS
    "cap_drop":          ["ALL"], # R-13, R-14, R-15
}


def _assert_security_invariants(
    run_kwargs: dict[str, Any],
    seccomp_profile_path: Path,
) -> None:
    """
    Validate *run_kwargs* against all security invariants before any
    Docker API call.

    Raises:
        SandboxSecurityError: if any invariant is violated.
        FileNotFoundError:    if the seccomp profile file is absent.
    """
    # --- R-12: network must be fully disabled (both flags required) ---
    network_mode = run_kwargs.get("network_mode", "")
    network_disabled = run_kwargs.get("network_disabled", False)
    if network_mode != "none" or not network_disabled:
        raise SandboxSecurityError(
            "Network access must be fully disabled "
            "(network_mode='none' and network_disabled=True). "
            f"Got network_mode={network_mode!r}, network_disabled={network_disabled!r}.",
            rule="R-12",
        )

    # --- R-14: privileged must be False ---
    if run_kwargs.get("privileged", False):
        raise SandboxSecurityError(
            "Privileged mode is enabled. Containers must never run privileged.",
            rule="R-14",
        )

    # --- R-14: read-only root filesystem ---
    if not run_kwargs.get("read_only", False):
        raise SandboxSecurityError(
            "Root filesystem is not read-only. Set read_only=True.",
            rule="R-14",
        )

    # --- R-13 / R-14 / R-15: capabilities must be dropped ---
    cap_drop = run_kwargs.get("cap_drop", [])
    if "ALL" not in [c.upper() for c in cap_drop]:
        raise SandboxSecurityError(
            f"Capabilities are not fully dropped. "
            f"cap_drop must include 'ALL'; got {cap_drop!r}.",
            rule="R-14",
        )

    # --- R-11 / R-14: seccomp profile must be present and loadable ---
    if not seccomp_profile_path.is_file():
        raise SandboxSecurityError(
            f"Seccomp profile not found at {seccomp_profile_path!r}. "
            "A seccomp profile is mandatory for sandbox execution.",
            rule="R-11",
        )

    try:
        raw = seccomp_profile_path.read_text(encoding="utf-8")
        profile = json_loads(raw)
    except (OSError, JsonDecodeError) as exc:
        raise SandboxSecurityError(
            f"Seccomp profile at {seccomp_profile_path!r} is unreadable "
            f"or invalid JSON: {exc}",
            rule="R-11",
        ) from exc

    default_action = profile.get("defaultAction", "")
    if default_action != "SCMP_ACT_ERRNO":
        raise SandboxSecurityError(
            f"Seccomp profile defaultAction is {default_action!r}; "
            "expected 'SCMP_ACT_ERRNO' (deny-all).",
            rule="R-16",
        )

    # --- seccomp must be in the security_opt list ---
    security_opt: list[str] = run_kwargs.get("security_opt", [])
    has_seccomp = any("seccomp" in opt for opt in security_opt)
    if not has_seccomp:
        raise SandboxSecurityError(
            "seccomp profile is not referenced in security_opt. "
            "Add 'seccomp=<path>' to security_opt.",
            rule="R-11",
        )

    _log.debug(
        "sandbox security invariants passed",
        extra={
            "network_mode":    network_mode or ("none" if network_disabled else "?"),
            "privileged":      run_kwargs.get("privileged", False),
            "read_only":       run_kwargs.get("read_only", False),
            "cap_drop":        cap_drop,
            "seccomp_profile": str(seccomp_profile_path),
        },
    )


# ---------------------------------------------------------------------------
# DockerSandbox
# ---------------------------------------------------------------------------

class DockerSandbox:
    """
    Ephemeral Docker sandbox for executing commands and actions in isolation.

    Each ``run_command`` / ``run_action`` call launches a fresh container,
    waits for it to exit (or kills it on timeout), captures its output, and
    removes it automatically in a ``finally`` block.

    Security properties enforced at construction time (see
    ``_assert_security_invariants``):
      - No network (R-12)
      - Privileged mode disabled (R-14)
      - Read-only root filesystem
      - All capabilities dropped (R-13, R-14, R-15)
      - Seccomp profile applied with ``defaultAction: SCMP_ACT_ERRNO`` (R-16)
      - ``/tmp`` is a size-limited ``tmpfs``

    R-15 compliance (v9.11.1):
      - run_action() passes the action JSON via container stdin, not as a CLI
        argument.  The payload is never visible in /proc/<pid>/cmdline, ps(1),
        or docker inspect.
      - run_command() accepts an optional stdin_data parameter to support
        SandboxManager.run_python() piping Python source via stdin.

    Usage::

        cfg = SandboxConfig(
            image="python:3.11-slim",
            seccomp_profile_path="sandbox/seccomp_profile.json",
        )
        sandbox = DockerSandbox(cfg)

        result = sandbox.run_command(["python3", "--version"])
        print(result.stdout)   # "Python 3.11.x\\n"

        # Or use as a context manager:
        with DockerSandbox(cfg) as sb:
            result = sb.run_command(["python3", "--version"])

    Thread safety:
        DockerSandbox instances are not thread-safe.  Create one per thread.
    """

    def __init__(self, config: SandboxConfig) -> None:
        """
        Initialise the sandbox and validate security configuration.

        The Docker client is NOT connected until the first ``run_*`` call.
        Security invariants ARE checked immediately against the declared
        config, so misconfigured sandboxes fail fast at construction time
        without needing Docker to be running.

        Args:
            config: Validated SandboxConfig instance.

        Raises:
            SandboxSecurityError: if the configuration violates any security
                                   invariant.
            FileNotFoundError:    if the seccomp profile path does not exist.
        """
        self._config = config
        self._client: Optional[Any] = None  # docker.DockerClient, lazy-init
        self._active_container: Optional[Any] = None  # docker.models.Container

        # Build the kwargs dict used for run()-based (no-stdin) executions.
        # Building it here (not inside _run) ensures configuration mistakes
        # are caught immediately at construction time.
        self._base_run_kwargs = self._build_run_kwargs(config)

        # Security gate: raises SandboxSecurityError on any violation.
        _assert_security_invariants(
            self._base_run_kwargs,
            config.seccomp_profile_path,
        )

        _log.info(
            "DockerSandbox initialised",
            extra={
                "image":            config.image,
                "memory_limit":     config.memory_limit,
                "pids_limit":       config.pids_limit,
                "timeout_seconds":  config.timeout_seconds,
                "seccomp_profile":  str(config.seccomp_profile_path),
            },
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "DockerSandbox":
        return self

    def __exit__(self, *_: object) -> None:
        self.stop_container()
        self._close_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_command(
        self,
        cmd: list[str],
        stdin_data: bytes | None = None,
    ) -> ExecutionResult:
        """
        Run *cmd* inside an ephemeral container and return the result.

        A new container is created for every call.  The container is
        automatically removed after it exits (or after timeout kill).

        Args:
            cmd:        Command and arguments, e.g. ``["python3", "--version"]``.
                        Must be a non-empty list of strings.
            stdin_data: Optional bytes to pipe into the container's stdin
                        before it starts executing.  When provided, the
                        container is launched with ``stdin_open=True`` and the
                        data is written via the Docker attach socket (R-15).
                        The write end of stdin is closed after the data is sent
                        so the container process receives EOF.

        Returns:
            ExecutionResult with captured stdout, stderr, exit code, and
            wall-clock runtime in milliseconds.

        Raises:
            SandboxSecurityError:  never raised here (validated at init).
            SandboxTimeoutError:   if the container exceeds timeout_seconds.
            SandboxExecutionError: on Docker API errors (daemon unreachable,
                                   image not found, OOM kill, etc.).
            ValueError:            if *cmd* is empty or not a list of strings.
        """
        if not cmd or not isinstance(cmd, list):
            raise ValueError("cmd must be a non-empty list of strings")
        if not all(isinstance(c, str) for c in cmd):
            raise ValueError("every element of cmd must be a str")

        _log.info(
            "sandbox run_command",
            extra={
                "cmd":          cmd,
                "image":        self._config.image,
                "has_stdin":    stdin_data is not None,
            },
        )
        return self._run(cmd=cmd, stdin_data=stdin_data)

    def run_action(self, action: dict[str, Any]) -> ExecutionResult:
        """
        Serialise *action* to JSON and execute it inside the sandbox.

        Patch v9.11.1 / FIX-1 — R-15 compliance:
            The action JSON is now piped into the container via stdin, NOT
            passed as a CLI argument.  The container entrypoint
            ``python3 -m ipc.action_runner`` reads the action dict from
            ``sys.stdin``::

                import json, sys
                action = json.load(sys.stdin)

            This ensures the payload is never visible in:
              - /proc/<pid>/cmdline
              - ps(1) / ps aux output
              - docker inspect
              - any process-listing tool inside the container

        The caller is responsible for validating the action against
        ``ipc.action_schema`` before calling this method.  This method
        performs no schema validation; it trusts the caller.

        Args:
            action: Action message dict (already schema-validated).

        Returns:
            ExecutionResult — see run_command for details.

        Raises:
            SandboxTimeoutError:   if the container exceeds timeout_seconds.
            SandboxExecutionError: on Docker API errors.
            ValueError:            if *action* is not a dict.
        """
        if not isinstance(action, dict):
            raise ValueError(f"action must be a dict, got {type(action).__name__}")

        try:
            action_json = json_dumps(action)
        except ValueError as exc:
            raise ValueError(f"action is not JSON-serialisable: {exc}") from exc

        # R-15: pass payload via stdin, not as a CLI argument.
        # The container entrypoint reads: action = json.load(sys.stdin)
        cmd = ["python3", "-m", "ipc.action_runner"]
        stdin_data = (action_json + "\n").encode("utf-8")

        _log.info(
            "sandbox run_action",
            extra={
                "action_type":   action.get("type", ""),
                "action_name":   action.get("action", ""),
                "cid":           action.get("correlation_id", ""),
                "image":         self._config.image,
            },
        )
        return self._run(cmd=cmd, stdin_data=stdin_data)

    def stop_container(self) -> None:
        """
        Forcibly stop and remove the currently active container, if any.

        Safe to call when no container is running (no-op).  Called
        automatically by the context manager ``__exit__``.

        Raises:
            SandboxExecutionError: on unexpected Docker API errors.
        """
        if self._active_container is None:
            return

        container = self._active_container
        self._active_container = None

        try:
            container.stop(timeout=2)
            _log.debug(
                "sandbox container stopped",
                extra={"container_id": container.short_id},
            )
        except Exception as exc:
            _log.warning(
                "error stopping sandbox container",
                extra={
                    "container_id": getattr(container, "short_id", "?"),
                    "error":        str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """
        Return the Docker SDK client, creating it lazily on first access.

        Raises:
            RuntimeError:          if the Docker SDK was not installed.
            SandboxExecutionError: if the Docker daemon is unreachable.
        """
        if self._client is not None:
            return self._client

        if _docker is None:
            raise RuntimeError(
                "Docker SDK required for sandbox execution. "
                "Install with: pip install docker"
            ) from _DOCKER_IMPORT_ERROR

        try:
            client = _docker.from_env()
            client.ping()
            self._client = client
            _log.debug("Docker client connected")
            return self._client
        except Exception as exc:
            raise SandboxExecutionError(
                f"Cannot connect to Docker daemon: {exc}"
            ) from exc

    def _close_client(self) -> None:
        """Close the Docker client connection if open."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            finally:
                self._client = None

    @staticmethod
    def _build_run_kwargs(config: SandboxConfig) -> dict[str, Any]:
        """
        Construct the docker.containers.run() keyword arguments from
        *config*, encoding all security constraints.

        This method is intentionally static so it can be exercised in
        unit tests without a live Docker daemon.

        Note on seccomp profile delivery: the profile is referenced by file
        path in security_opt, not by inlining raw JSON.  Passing the path is
        the correct and reliable method across all supported Docker versions
        (≥ 20.10).  JSON parsing for profile validation is performed
        exclusively inside ``_assert_security_invariants()``.

        Returns:
            A dict suitable for unpacking into ``client.containers.run(**kwargs)``
            (for the no-stdin path) or filtering via ``_build_create_kwargs()``
            (for the stdin path).
        """
        return {
            # ── Identity & lifecycle ──────────────────────────────────────
            "image":        config.image,
            "detach":       True,          # Non-blocking; we poll with wait()
            "auto_remove":  False,         # We manage removal after log capture
            "remove":       False,         # Explicit; we call remove() ourselves

            # ── Security: R-12 no network ─────────────────────────────────
            "network_disabled": True,
            "network_mode":     "none",

            # ── Security: R-14 no privilege escalation ────────────────────
            "privileged":       False,
            "cap_drop":         ["ALL"],
            "read_only":        True,      # Read-only root filesystem

            # ── Security: R-11/R-14 no new privileges via seccomp ─────────
            "security_opt": [
                f"seccomp={config.seccomp_profile_path}",
                "no-new-privileges:true",
            ],

            # ── Filesystem: /tmp is the only writable path ────────────────
            "tmpfs": {
                "/tmp": f"size={config.tmpfs_size},mode=1777,noexec",
            },

            # ── Resource limits ───────────────────────────────────────────
            "mem_limit":        config.memory_limit,
            "memswap_limit":    config.memory_limit,  # Disable swap entirely
            "cpu_period":       config.cpu_period,
            "cpu_quota":        config.cpu_quota,
            "pids_limit":       config.pids_limit,

            # ── Runtime ───────────────────────────────────────────────────
            "working_dir":      config.working_dir,
            "environment":      {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED":        "1",
                **config.environment,
            },

            # ── I/O ───────────────────────────────────────────────────────
            "stdout":     True,
            "stderr":     True,
            "stdin_open": False,   # Overridden to True for the stdin path
            "tty":        False,
        }

    @staticmethod
    def _build_create_kwargs(
        run_kwargs: dict[str, Any],
        *,
        stdin_open: bool,
    ) -> dict[str, Any]:
        """
        Build a ``containers.create()``-compatible kwargs dict from the
        run()-oriented *run_kwargs*.

        ``containers.run()`` accepts several python-SDK-level parameters
        (``detach``, ``auto_remove``, ``remove``, ``stdout``, ``stderr``)
        that are not valid Docker API parameters and are therefore rejected
        by ``containers.create()``.  This method strips them and sets
        ``stdin_open`` to the requested value.

        The returned dict is a shallow copy; *run_kwargs* is not mutated.

        Args:
            run_kwargs:  Base dict from ``_build_run_kwargs()``.
            stdin_open:  Value to use for the ``stdin_open`` Docker parameter.

        Returns:
            New dict suitable for ``client.containers.create(**kwargs)``.
        """
        # These keys are docker-py run()-specific and must not be forwarded
        # to containers.create().
        _RUN_ONLY_PARAMS: frozenset[str] = frozenset({
            "detach", "auto_remove", "remove", "stdout", "stderr",
        })
        create_kw: dict[str, Any] = {
            k: v for k, v in run_kwargs.items()
            if k not in _RUN_ONLY_PARAMS
        }
        create_kw["stdin_open"] = stdin_open
        return create_kw

    def _write_container_stdin(
        self,
        container: Any,
        data: bytes,
    ) -> None:
        """
        Write *data* to *container*'s stdin via the Docker attach socket,
        then close the write end to signal EOF to the container process.

        This implements the R-15-compliant stdin delivery mechanism.  The
        container must have been created with ``stdin_open=True``.

        docker-py's ``attach_socket()`` returns a ``SocketIO`` object wrapping
        a raw Unix domain socket to the Docker daemon's multiplexed I/O
        channel.  We access the underlying raw socket via ``._sock`` to call
        ``sendall()`` and ``shutdown(SHUT_WR)`` directly — the only reliable
        way to:
          (a) guarantee all bytes are delivered (sendall vs write),
          (b) signal EOF without closing the read end prematurely
              (SHUT_WR vs close).

        OSError is caught and logged (not re-raised) so that a container that
        exits early before consuming all stdin does not mask the real result.

        Args:
            container: A docker-py container object (already started).
            data:      Bytes to write to stdin.
        """
        try:
            sock = container.attach_socket(params={
                "stdin":  1,
                "stdout": 0,
                "stderr": 0,
                "stream": 1,
            })
        except Exception as exc:
            # A failure to attach stdin is logged but not fatal; the
            # container may already have exited or may not need stdin.
            _log.warning(
                "failed to attach stdin socket to container",
                extra={
                    "container_id": getattr(container, "short_id", "?"),
                    "error":        str(exc),
                },
            )
            return

        try:
            # sendall() retries internally until all bytes are written or an
            # error occurs — unlike write() which may send less than requested.
            sock._sock.sendall(data)
            # SHUT_WR signals EOF to the container process without closing the
            # socket object itself (which would also close the read channel).
            sock._sock.shutdown(_socket_module.SHUT_WR)
        except OSError as exc:
            # The container may have exited before reading all stdin data.
            # This is not an error — log at WARNING level and continue.
            _log.warning(
                "container stdin write error (container may have exited early)",
                extra={
                    "container_id": getattr(container, "short_id", "?"),
                    "error":        str(exc),
                },
            )
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _run(
        self,
        *,
        cmd: list[str],
        stdin_data: bytes | None = None,
    ) -> ExecutionResult:
        """
        Core execution path.

        Two execution sub-paths:

        No stdin_data (original path):
            Uses ``client.containers.run(**kwargs)`` with ``detach=True``.
            This is the high-level docker-py one-shot API.

        With stdin_data (R-15 compliant path — FIX-1):
            Uses ``client.containers.create()`` + ``container.start()`` +
            ``_write_container_stdin()``.  The create/start split is necessary
            because ``containers.run()`` does not support pre-start stdin
            attachment.  The payload is written after ``container.start()``
            so the container process is running and blocked on its stdin.read()
            call when our bytes arrive.

        Both paths use the same wait → capture logs → remove finally block.

        Args:
            cmd:        Command list to run in the container.
            stdin_data: Optional bytes to pipe via stdin.

        Returns:
            ExecutionResult.

        Raises:
            SandboxSecurityError:  if the seccomp profile was deleted since init.
            SandboxTimeoutError:   on timeout.
            SandboxExecutionError: on Docker API errors.
        """
        # Re-verify the seccomp profile still exists.  It was validated at
        # __init__ time but could have been deleted since then.
        if not self._config.seccomp_profile_path.is_file():
            raise SandboxSecurityError(
                f"Seccomp profile not found at "
                f"{self._config.seccomp_profile_path!r}. "
                "The profile file must remain accessible for the full lifetime "
                "of the sandbox instance.",
                rule="R-11",
            )

        client = self._get_client()
        config = self._config
        kwargs = dict(self._base_run_kwargs)  # shallow copy; immutable values
        kwargs["command"] = cmd

        container = None
        timed_out = False
        t_start = time.monotonic()

        try:
            try:
                if stdin_data is not None:
                    # ── R-15 stdin path (FIX-1) ───────────────────────────
                    # create() + start() allows us to attach the stdin socket
                    # before or immediately after start, ensuring the container
                    # process is ready to read when we write.
                    create_kwargs = self._build_create_kwargs(
                        kwargs, stdin_open=True
                    )
                    container = client.containers.create(**create_kwargs)
                    self._active_container = container
                    # Start the container (process blocks on stdin.read()).
                    container.start()
                    # Write payload and close write-end (signals EOF).
                    self._write_container_stdin(container, stdin_data)
                else:
                    # ── Original detach path ──────────────────────────────
                    container = client.containers.run(**kwargs)
                    self._active_container = container

            except (SandboxSecurityError, SandboxExecutionError):
                raise
            except Exception as exc:
                raise SandboxExecutionError(
                    f"Failed to create container for image {config.image!r}: {exc}"
                ) from exc

            _log.debug(
                "container started",
                extra={
                    "container_id": container.short_id,
                    "cmd":          cmd,
                    "has_stdin":    stdin_data is not None,
                },
            )

            # ── Wait for exit ─────────────────────────────────────────────
            try:
                result = container.wait(timeout=config.timeout_seconds)
                exit_code: int = result.get("StatusCode", -1)
            except Exception as exc:
                err_str = str(exc).lower()
                if "timeout" in err_str or "timed out" in err_str:
                    timed_out = True
                    _log.warning(
                        "container timed out — killing",
                        extra={
                            "container_id":    container.short_id,
                            "timeout_seconds": config.timeout_seconds,
                            "cmd":             cmd,
                        },
                    )
                    try:
                        container.kill()
                    except Exception:
                        pass
                    exit_code = -1
                else:
                    raise SandboxExecutionError(
                        f"Unexpected error waiting for container "
                        f"{container.short_id}: {exc}",
                        exit_code=-1,
                    ) from exc

            runtime_ms = (time.monotonic() - t_start) * 1000.0

            # ── Capture output ────────────────────────────────────────────
            stdout_raw = b""
            stderr_raw = b""
            try:
                stdout_raw = container.logs(stdout=True, stderr=False)
                stderr_raw = container.logs(stdout=False, stderr=True)
            except Exception as exc:
                _log.warning(
                    "failed to capture container logs",
                    extra={
                        "container_id": container.short_id,
                        "error":        str(exc),
                    },
                )

            stdout = stdout_raw.decode("utf-8", errors="replace")
            stderr = stderr_raw.decode("utf-8", errors="replace")

            _log.info(
                "container finished",
                extra={
                    "container_id": container.short_id,
                    "exit_code":    exit_code,
                    "runtime_ms":   round(runtime_ms, 1),
                    "timed_out":    timed_out,
                    "stdout_bytes": len(stdout_raw),
                    "stderr_bytes": len(stderr_raw),
                },
            )

            result_obj = ExecutionResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                runtime_ms=round(runtime_ms, 3),
                timed_out=timed_out,
            )

            if timed_out:
                raise SandboxTimeoutError(config.timeout_seconds, " ".join(cmd))

            return result_obj

        finally:
            # Always attempt to remove the container (R-06: no reuse).
            self._active_container = None
            if container is not None:
                try:
                    container.remove(force=True)
                    _log.debug(
                        "container removed",
                        extra={"container_id": container.short_id},
                    )
                except Exception as exc:
                    _log.warning(
                        "failed to remove container",
                        extra={
                            "container_id": getattr(container, "short_id", "?"),
                            "error":        str(exc),
                        },
                    )


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def create_sandbox(
    image: str,
    seccomp_profile_path: Union[str, Path],
    *,
    memory_limit: str = "256m",
    cpu_quota: int = 50_000,
    cpu_period: int = 100_000,
    pids_limit: int = 64,
    tmpfs_size: str = "64m",
    timeout_seconds: float = 30.0,
    working_dir: str = "/workspace",
    environment: Optional[dict[str, str]] = None,
) -> DockerSandbox:
    """
    Convenience factory function for creating a DockerSandbox.

    All keyword arguments mirror :class:`SandboxConfig` fields.  Security
    invariants are validated immediately; the function raises
    :exc:`SandboxSecurityError` before returning if the configuration is
    insecure.

    Example::

        sandbox = create_sandbox(
            image="python:3.11-slim",
            seccomp_profile_path="sandbox/seccomp_profile.json",
            memory_limit="128m",
            timeout_seconds=10.0,
        )
        result = sandbox.run_command(["python3", "-c", "print('ok')"])

    Returns:
        Ready-to-use DockerSandbox instance.
    """
    config = SandboxConfig(
        image=image,
        seccomp_profile_path=Path(seccomp_profile_path),
        memory_limit=memory_limit,
        cpu_quota=cpu_quota,
        cpu_period=cpu_period,
        pids_limit=pids_limit,
        tmpfs_size=tmpfs_size,
        timeout_seconds=timeout_seconds,
        working_dir=working_dir,
        environment=environment or {},
    )
    return DockerSandbox(config)
