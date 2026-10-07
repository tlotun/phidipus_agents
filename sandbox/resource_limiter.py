# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
sandbox/resource_limiter.py — Phidipus v1.0
Resource governance layer for the sandbox subsystem.

Architecture contract (C.7 / R-16):
  "Every action dispatched to the host OS runs inside an ephemeral container
   with the minimal capability surface required for the action."

This module sits between SandboxManager and DockerSandbox.  It is responsible
for translating high-level resource policy into concrete SandboxConfig values,
ensuring that no container is ever started with resource parameters that exceed
the system-wide policy ceiling.

Design:
  - ResourceLimits encodes the absolute ceilings for all resource parameters.
    It is immutable after construction and may be shared across threads.
  - ResourceMonitor applies limits via three operations:
      validate_config()      — raises if any parameter already exceeds policy
      clamp_config()         — silently clamps every parameter to policy ceiling
      apply_action_profile() — overlay an action-specific resource profile
  - ResourceExceededError is raised only by validate_config(); clamp_config()
    never raises (it silently corrects).
  - Per-action profiles are defined as a module-level constant so they can be
    inspected in tests without instantiating any class.

Layer position:
  SandboxManager → ResourceMonitor → DockerSandbox
  (ResourceMonitor constructs the SandboxConfig that DockerSandbox receives.)

Thread safety:
  ResourceLimits and the ACTION_PROFILES dict are read-only after module load.
  ResourceMonitor is stateless; it is safe to share a single instance across
  threads.

Dependencies:
  sandbox.docker_runner  — SandboxConfig (type reference only; no Docker calls)
  ipc.action_schema      — ALLOWED_ACTIONS (canonical, frozen IPC action name set)
  utils.logger           — structured JSON logging

Correctness guarantees enforced at module-load time:
  - _validate_action_profiles() raises RuntimeError immediately if any action
    declared in ALLOWED_ACTIONS lacks a matching entry in ACTION_PROFILES.
    This prevents silent fallback-to-defaults for unprovisioned actions.
  - clamp_config() always returns a brand-new SandboxConfig object; it never
    mutates its input.  Enforced by dataclasses.replace() + runtime assertion.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

# Import SandboxConfig for type annotations and object construction.
# This is the *only* docker_runner symbol used here; no Docker API is touched.
from sandbox.docker_runner import SandboxConfig

# Import the canonical set of IPC action names from the schema layer so that
# ACTION_PROFILES coverage can be validated at module-load time (Issue 1).
from ipc.action_schema import ALLOWED_ACTIONS

_log = get_logger("sandbox.resource_limiter", process="daemon")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ResourceExceededError(RuntimeError):
    """
    Raised by ResourceMonitor.validate_config() when a SandboxConfig
    parameter exceeds the policy ceiling defined in ResourceLimits.

    Attributes:
        parameter: Name of the offending SandboxConfig field.
        value:     The value that was supplied.
        limit:     The maximum value permitted by policy.
    """

    def __init__(
        self,
        message: str,
        *,
        parameter: str = "",
        value: Any = None,
        limit: Any = None,
    ) -> None:
        super().__init__(message)
        self.parameter = parameter
        self.value = value
        self.limit = limit

    def __str__(self) -> str:
        base = super().__str__()
        if self.parameter:
            return f"[ResourceExceededError] {self.parameter}: {base}"
        return f"[ResourceExceededError] {base}"


# ---------------------------------------------------------------------------
# ResourceLimits — absolute policy ceilings
# ---------------------------------------------------------------------------

# Docker memory strings that the system-wide ceiling is expressed in.
# These are the raw values stored in the dataclass; conversion to bytes is
# done inside ResourceMonitor so that all comparison logic is numeric.
_MEM_UNIT_MULTIPLIERS: dict[str, int] = {
    "b": 1,
    "k": 1024,
    "m": 1024 ** 2,
    "g": 1024 ** 3,
}


def _parse_memory(value: str) -> int:
    """
    Parse a Docker-style memory string (e.g. ``"256m"``, ``"1g"``) into bytes.

    Raises:
        ValueError: if the string does not match the expected format.
    """
    value = value.strip().lower()
    if not value:
        raise ValueError("memory string must not be empty")
    suffix = value[-1]
    if suffix in _MEM_UNIT_MULTIPLIERS:
        try:
            numeric = int(value[:-1])
        except ValueError:
            raise ValueError(f"invalid memory value: {value!r}")
        return numeric * _MEM_UNIT_MULTIPLIERS[suffix]
    # No suffix → interpret as plain bytes
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"invalid memory value: {value!r}")


def _bytes_to_docker_str(bytes_: int) -> str:
    """
    Convert a byte count back to a Docker-style memory string using the
    largest whole-unit representation.  Result always uses ``m`` or ``g``
    to stay consistent with SandboxConfig conventions.
    """
    if bytes_ % (1024 ** 3) == 0:
        return f"{bytes_ // (1024 ** 3)}g"
    if bytes_ % (1024 ** 2) == 0:
        return f"{bytes_ // (1024 ** 2)}m"
    if bytes_ % 1024 == 0:
        return f"{bytes_ // 1024}k"
    return f"{bytes_}b"


@dataclass(frozen=True)
class ResourceLimits:
    """
    Absolute policy ceilings for all sandbox resource parameters.

    All fields mirror the corresponding SandboxConfig fields.  The values
    defined here are the **maximum** values that ResourceMonitor will allow;
    any SandboxConfig that exceeds them will be rejected by validate_config()
    or silently corrected by clamp_config().

    Fields:
        max_memory:    Largest memory limit string accepted (e.g. ``"1g"``).
        max_cpu_quota: Largest CFS CPU quota in microseconds.
        max_pids:      Maximum PID count inside the container.
        max_tmpfs:     Largest /tmp tmpfs size string accepted.
        max_timeout:   Longest container wall-clock timeout in seconds.
    """

    max_memory: str = "1g"
    max_cpu_quota: int = 200_000   # 2 full CPU cores at 100 ms period
    max_pids: int = 256
    max_tmpfs: str = "256m"
    max_timeout: float = 120.0

    def __post_init__(self) -> None:
        # Validate that the string fields can actually be parsed.
        _parse_memory(self.max_memory)
        _parse_memory(self.max_tmpfs)
        if self.max_cpu_quota <= 0:
            raise ValueError(
                f"max_cpu_quota must be positive, got {self.max_cpu_quota}"
            )
        if self.max_pids <= 0:
            raise ValueError(f"max_pids must be positive, got {self.max_pids}")
        if self.max_timeout <= 0:
            raise ValueError(
                f"max_timeout must be positive, got {self.max_timeout}"
            )


# ---------------------------------------------------------------------------
# Per-action resource profiles
# ---------------------------------------------------------------------------

# Each entry is a partial dict of SandboxConfig constructor keyword arguments.
# Fields not present in an entry inherit from the caller-supplied base config.
# All values must remain within the defaults set by ResourceLimits.

#: Mapping of action name → partial SandboxConfig kwargs.
#: Accessed by ResourceMonitor.apply_action_profile().
ACTION_PROFILES: dict[str, dict[str, Any]] = {
    # ── Mouse actions — lightweight; minimal resources ────────────────────
    "mouse_click": {
        "memory_limit":    "64m",
        "cpu_quota":       25_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },
    "mouse_move": {
        "memory_limit":    "64m",
        "cpu_quota":       25_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },
    "mouse_scroll": {
        "memory_limit":    "64m",
        "cpu_quota":       25_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },
    "mouse_drag": {
        "memory_limit":    "64m",
        "cpu_quota":       25_000,
        "pids_limit":      32,
        "timeout_seconds": 15.0,
    },

    # ── Keyboard — minimal text injection ────────────────────────────────
    "keyboard_type": {
        "memory_limit":    "64m",
        "cpu_quota":       20_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },
    "keyboard_press": {
        "memory_limit":    "64m",
        "cpu_quota":       20_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },
    "keyboard_hotkey": {
        "memory_limit":    "64m",
        "cpu_quota":       20_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },

    # ── Clipboard — minimal ───────────────────────────────────────────────
    "clipboard_copy": {
        "memory_limit":    "64m",
        "cpu_quota":       20_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },
    "clipboard_paste": {
        "memory_limit":    "64m",
        "cpu_quota":       20_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },
    "clipboard_get": {
        "memory_limit":    "64m",
        "cpu_quota":       20_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },

    # ── Screenshot — higher memory for image buffering ────────────────────
    "screenshot_capture": {
        "memory_limit":    "512m",
        "cpu_quota":       100_000,
        "pids_limit":      64,
        "tmpfs_size":      "128m",
        "timeout_seconds": 30.0,
    },

    # ── Window management — lightweight ───────────────────────────────────
    "window_focus": {
        "memory_limit":    "64m",
        "cpu_quota":       25_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },
    "window_list": {
        "memory_limit":    "128m",
        "cpu_quota":       50_000,
        "pids_limit":      32,
        "timeout_seconds": 15.0,
    },
    "window_get_info": {
        "memory_limit":    "64m",
        "cpu_quota":       25_000,
        "pids_limit":      32,
        "timeout_seconds": 10.0,
    },

    # ── Accessibility — moderate (AT-SPI tree traversal) ─────────────────
    "element_click": {
        "memory_limit":    "128m",
        "cpu_quota":       50_000,
        "pids_limit":      64,
        "timeout_seconds": 15.0,
    },
    "element_get_text": {
        "memory_limit":    "128m",
        "cpu_quota":       50_000,
        "pids_limit":      64,
        "timeout_seconds": 15.0,
    },
    "element_find": {
        "memory_limit":    "128m",
        "cpu_quota":       50_000,
        "pids_limit":      64,
        "timeout_seconds": 20.0,
    },
    "element_set_value": {
        "memory_limit":    "128m",
        "cpu_quota":       50_000,
        "pids_limit":      64,
        "timeout_seconds": 15.0,
    },

    # ── v4.3 keyboard-first layer: UI state + menu bar (Accessibility) ────
    "ui_snapshot": {
        "memory_limit":    "64m",
        "cpu_quota":       25_000,
        "pids_limit":      32,
        "timeout_seconds": 8.0,
    },
    "menu_list": {
        "memory_limit":    "128m",
        "cpu_quota":       50_000,
        "pids_limit":      64,
        "timeout_seconds": 15.0,
    },
    "menu_select": {
        "memory_limit":    "128m",
        "cpu_quota":       50_000,
        "pids_limit":      64,
        "timeout_seconds": 75.0,     # may wait for a confirmation dialog
    },

    # ── Application launch — longer timeout for startup ───────────────────
    "app_launch": {
        "memory_limit":    "256m",
        "cpu_quota":       100_000,
        "pids_limit":      128,
        "timeout_seconds": 60.0,
    },

    # ── Browser navigation — longer timeout; more memory ─────────────────
    "browser_navigate": {
        "memory_limit":    "512m",
        "cpu_quota":       100_000,
        "pids_limit":      128,
        "timeout_seconds": 60.0,
    },
    "browser_execute_js": {         # v9.34 A1: execute JS in active Chrome tab
        "memory_limit":    "512m",
        "cpu_quota":       100_000,
        "pids_limit":      128,
        "timeout_seconds": 30.0,
    },

    # ── System / control ─────────────────────────────────────────────────
    "ping": {
        "memory_limit":    "64m",
        "cpu_quota":       10_000,
        "pids_limit":      16,
        "timeout_seconds": 5.0,
    },
    "user_confirm": {
        "memory_limit":    "64m",
        "cpu_quota":       10_000,
        "pids_limit":      16,
        "timeout_seconds": 5.0,
    },
}


# ---------------------------------------------------------------------------
# Issue 1 fix — ACTION_PROFILES coverage validation
# ---------------------------------------------------------------------------

def _validate_action_profiles() -> None:
    """
    Verify that every IPC action declared in ``ipc.action_schema.ALLOWED_ACTIONS``
    has a corresponding entry in :data:`ACTION_PROFILES`.

    Called once at module import time so the program fails fast if a new action
    is added to the schema without a matching resource profile.

    Raises:
        RuntimeError: listing every action that is missing a profile entry.
    """
    missing = sorted(ALLOWED_ACTIONS - ACTION_PROFILES.keys())
    if missing:
        missing_list = ", ".join(f"{a!r}" for a in missing)
        raise RuntimeError(
            f"Missing resource profile for action(s): {missing_list}. "
            "Add an entry to ACTION_PROFILES for each action listed above."
        )


# Run validation immediately at import time so any coverage gap is caught
# during daemon startup rather than silently at the first run_action() call.
_validate_action_profiles()


# ---------------------------------------------------------------------------
# ResourceMonitor
# ---------------------------------------------------------------------------


class ResourceMonitor:
    """
    Stateless resource governance engine.

    Applies :class:`ResourceLimits` policy to :class:`SandboxConfig` objects
    before they are handed to :class:`DockerSandbox`.

    All three public methods accept a ``SandboxConfig`` and return either
    ``None`` (for validation) or a *new* ``SandboxConfig`` (for clamping /
    profiling).  The original config object is never mutated.

    Usage::

        limits  = ResourceLimits(max_memory="512m", max_timeout=60.0)
        monitor = ResourceMonitor(limits)

        cfg = SandboxConfig(
            image="python:3.11-slim",
            seccomp_profile_path="sandbox/seccomp_profile.json",
            memory_limit="256m",
        )
        # Validate — raises ResourceExceededError if any parameter is over limit
        monitor.validate_config(cfg)

        # Clamp — returns a new SandboxConfig with all parameters within limits
        safe_cfg = monitor.clamp_config(cfg)

        # Apply per-action profile — returns a new SandboxConfig
        profiled_cfg = monitor.apply_action_profile("screenshot_capture", cfg)
    """

    def __init__(self, limits: Optional[ResourceLimits] = None) -> None:
        """
        Initialise with the given limits or the system-wide defaults.

        Args:
            limits: Policy ceiling.  Defaults to ``ResourceLimits()`` if None.
        """
        self._limits = limits if limits is not None else ResourceLimits()
        _log.debug(
            "ResourceMonitor initialised",
            extra={
                "max_memory":    self._limits.max_memory,
                "max_cpu_quota": self._limits.max_cpu_quota,
                "max_pids":      self._limits.max_pids,
                "max_timeout":   self._limits.max_timeout,
            },
        )

    @property
    def limits(self) -> ResourceLimits:
        """Return the active ResourceLimits (read-only)."""
        return self._limits

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_config(self, config: SandboxConfig) -> None:
        """
        Validate that every resource parameter in *config* is within policy.

        Raises:
            ResourceExceededError: if any parameter exceeds the policy ceiling.
        """
        limits = self._limits

        # Memory
        requested_mem = _parse_memory(config.memory_limit)
        max_mem       = _parse_memory(limits.max_memory)
        if requested_mem > max_mem:
            raise ResourceExceededError(
                f"memory_limit {config.memory_limit!r} exceeds policy "
                f"ceiling {limits.max_memory!r}",
                parameter="memory_limit",
                value=config.memory_limit,
                limit=limits.max_memory,
            )

        # CPU quota
        if config.cpu_quota > limits.max_cpu_quota:
            raise ResourceExceededError(
                f"cpu_quota {config.cpu_quota} exceeds policy ceiling "
                f"{limits.max_cpu_quota}",
                parameter="cpu_quota",
                value=config.cpu_quota,
                limit=limits.max_cpu_quota,
            )

        # PID limit
        if config.pids_limit > limits.max_pids:
            raise ResourceExceededError(
                f"pids_limit {config.pids_limit} exceeds policy ceiling "
                f"{limits.max_pids}",
                parameter="pids_limit",
                value=config.pids_limit,
                limit=limits.max_pids,
            )

        # Tmpfs
        requested_tmpfs = _parse_memory(config.tmpfs_size)
        max_tmpfs       = _parse_memory(limits.max_tmpfs)
        if requested_tmpfs > max_tmpfs:
            raise ResourceExceededError(
                f"tmpfs_size {config.tmpfs_size!r} exceeds policy ceiling "
                f"{limits.max_tmpfs!r}",
                parameter="tmpfs_size",
                value=config.tmpfs_size,
                limit=limits.max_tmpfs,
            )

        # Timeout
        if config.timeout_seconds > limits.max_timeout:
            raise ResourceExceededError(
                f"timeout_seconds {config.timeout_seconds} exceeds policy "
                f"ceiling {limits.max_timeout}",
                parameter="timeout_seconds",
                value=config.timeout_seconds,
                limit=limits.max_timeout,
            )

        _log.debug(
            "resource validation passed",
            extra={"memory": config.memory_limit, "timeout": config.timeout_seconds},
        )

    def clamp_config(self, config: SandboxConfig) -> SandboxConfig:
        """
        Return a **new** :class:`SandboxConfig` with every resource parameter
        clamped to the policy ceiling.

        The original *config* is never mutated.  Non-resource fields (image,
        seccomp_profile_path, working_dir, environment) are preserved exactly.

        The returned object is always a distinct Python object from *config*
        (guaranteed by the ``assert new_config is not config`` at the end of
        this method), even when no clamping was necessary.

        Args:
            config: Source configuration.

        Returns:
            New SandboxConfig with all resources ≤ policy ceiling.
        """
        limits = self._limits

        # Memory
        requested_mem = _parse_memory(config.memory_limit)
        max_mem       = _parse_memory(limits.max_memory)
        clamped_mem   = min(requested_mem, max_mem)
        mem_str       = (
            config.memory_limit
            if clamped_mem == requested_mem
            else _bytes_to_docker_str(clamped_mem)
        )

        # tmpfs
        requested_tmpfs = _parse_memory(config.tmpfs_size)
        max_tmpfs       = _parse_memory(limits.max_tmpfs)
        clamped_tmpfs   = min(requested_tmpfs, max_tmpfs)
        tmpfs_str       = (
            config.tmpfs_size
            if clamped_tmpfs == requested_tmpfs
            else _bytes_to_docker_str(clamped_tmpfs)
        )

        # Issue 2 fix: construct a brand-new SandboxConfig using
        # dataclasses.replace() so that identity (new is not old) is always
        # guaranteed regardless of whether any field value actually changed.
        # dataclasses.replace() always allocates a fresh object.
        new_config = replace(
            config,
            memory_limit=mem_str,
            cpu_quota=min(config.cpu_quota, limits.max_cpu_quota),
            pids_limit=min(config.pids_limit, limits.max_pids),
            tmpfs_size=tmpfs_str,
            timeout_seconds=min(config.timeout_seconds, limits.max_timeout),
        )

        # Safety assertion: the returned object must NEVER be the same object
        # as the input config, even when nothing was clamped.
        assert new_config is not config, (
            "clamp_config() must return a new SandboxConfig object, "
            "not the original — this is an implementation bug."
        )

        if new_config != config:
            _log.warning(
                "resource parameters clamped to policy ceiling",
                extra={
                    "original_memory":  config.memory_limit,
                    "clamped_memory":   mem_str,
                    "original_timeout": config.timeout_seconds,
                    "clamped_timeout":  new_config.timeout_seconds,
                    "original_pids":    config.pids_limit,
                    "clamped_pids":     new_config.pids_limit,
                },
            )

        return new_config

    def apply_action_profile(
        self, action: str, config: SandboxConfig
    ) -> SandboxConfig:
        """
        Return a new :class:`SandboxConfig` with per-action resource overrides
        applied, then clamped to the policy ceiling.

        If *action* has no registered profile, the original config is returned
        unchanged (after clamping).  This makes the method safe to call for
        unknown action names.

        Args:
            action: IPC action name (e.g. ``"screenshot_capture"``).
            config: Base configuration to build on.

        Returns:
            New SandboxConfig with profile applied and clamped.
        """
        profile = ACTION_PROFILES.get(action, {})

        if not profile:
            _log.debug(
                "no action profile found; using base config",
                extra={"action": action},
            )
            return self.clamp_config(config)

        profiled = replace(
            config,
            memory_limit=profile.get("memory_limit", config.memory_limit),
            cpu_quota=profile.get("cpu_quota", config.cpu_quota),
            cpu_period=profile.get("cpu_period", config.cpu_period),
            pids_limit=profile.get("pids_limit", config.pids_limit),
            tmpfs_size=profile.get("tmpfs_size", config.tmpfs_size),
            timeout_seconds=profile.get("timeout_seconds", config.timeout_seconds),
        )

        _log.debug(
            "action profile applied",
            extra={
                "action":   action,
                "memory":   profiled.memory_limit,
                "timeout":  profiled.timeout_seconds,
                "pids":     profiled.pids_limit,
            },
        )

        # Always clamp after applying the profile so a misconfigured profile
        # entry can never bypass the policy ceiling.
        return self.clamp_config(profiled)
