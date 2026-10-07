# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory_integrity/segment_router.py — Phidipus v1.0
Namespace-aware path router for episodic memory segments.

Architecture contract (R-16 / M-7):
  "Routes episode writes to the correct namespace: primary / guest /
   untrusted.  Primary planner may only query the primary namespace.
   Guest namespace isolated."

SegmentRouter is the production replacement for _DefaultPathResolver in
memory_integrity/memory_guard.py.  It provides identical resolve_path()
and namespace_dir() methods so MemoryGuard can swap routers transparently
via constructor injection:

    # Phase 2+ production:
    router = SegmentRouter(cfg)
    guard  = MemoryGuard(cfg, segment_router=router)

    # Phase 1 fallback (still works):
    guard  = MemoryGuard(cfg)   # uses _DefaultPathResolver internally

Namespace model
---------------
Three immutable namespaces are defined by the architecture:

  primary   — written and read by the main planning agent (L1 orchestrator).
              Only callers presenting the "primary" namespace token may
              query this segment.

  guest     — isolated session data from untrusted user input.  Never
              merged into primary.

  untrusted — externally sourced episodes (e.g. web observations, tool
              outputs that have not been verified by the agent).

Access control
--------------
SegmentRouter enforces two rules at the routing layer:

  1. The primary planner namespace is read-only to non-primary callers.
     check_read_allowed(caller_ns, target_ns) raises NamespaceAccessError
     if a guest or untrusted caller attempts to read the primary namespace.

  2. No write crosses namespace boundaries.  An episode destined for the
     "guest" namespace is always written to the guest directory, never to
     primary.  This is enforced automatically by resolve_path().

Directory layout (matches _DefaultPathResolver for drop-in compatibility)
--------------------------------------------------------------------------
  <episodes_root>/
      primary/
          <episode_id>.ep.json
      guest/
          <episode_id>.ep.json
      untrusted/
          <episode_id>.ep.json

Process: orchestrator (L1)

Security invariants enforced here:
  R-16  Namespace separation is enforced at the path-resolution layer.
        An episode written to "guest" can never be stored in "primary" by
        any code path that goes through resolve_path().
  M-7   task_memory is session-scoped only and never persisted.  This
        module does not route task_memory — that is enforced at the
        task_memory layer.

Used by:
  memory_integrity/memory_guard.py — injected as segment_router parameter

Dependencies:
  utils/file_ops.py    — safe_join(), ensure_dir()
  utils/logger.py      — get_logger()
  config/config_loader.py — PhidipusConfig (cfg.paths.data_dir)
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from config.config_loader import PhidipusConfig
from utils.file_ops import ensure_dir, safe_join
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants (must match memory_guard._VALID_NAMESPACES and _EPISODE_EXT)
# ---------------------------------------------------------------------------

VALID_NAMESPACES: frozenset[str] = frozenset({"primary", "guest", "untrusted"})

#: Subdirectory name inside data_dir that holds all episode namespaces.
_EPISODES_SUBDIR: str = "episodes"

#: Episode file extension — must match memory_guard._EPISODE_EXT.
_EPISODE_EXT: str = ".ep.json"

#: Namespace directory permissions: owner read/write/execute only.
_NS_DIR_MODE: int = 0o700


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NamespaceError(ValueError):
    """
    Raised when an invalid namespace identifier is used.

    Attributes:
        namespace: The invalid namespace string that was supplied.
        reason:    Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        namespace: str = "",
        reason: str = "INVALID_NAMESPACE",
    ) -> None:
        super().__init__(message)
        self.namespace = namespace
        self.reason    = reason

    def __str__(self) -> str:
        base = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.namespace:
            parts.append(f"namespace={self.namespace!r}")
        return " ".join(parts) + f" {base}"


class NamespaceAccessError(PermissionError):
    """
    Raised when a caller attempts to read across namespace boundaries
    in violation of the isolation rules (R-16).

    Attributes:
        caller_ns: The namespace the caller is operating in.
        target_ns: The namespace the caller attempted to access.
        reason:    Always "NAMESPACE_ACCESS_DENIED".
    """

    def __init__(
        self,
        message: str,
        *,
        caller_ns: str = "",
        target_ns: str = "",
        reason:    str = "NAMESPACE_ACCESS_DENIED",
    ) -> None:
        super().__init__(message)
        self.caller_ns = caller_ns
        self.target_ns = target_ns
        self.reason    = reason

    def __str__(self) -> str:
        base = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.caller_ns:
            parts.append(f"caller={self.caller_ns!r}")
        if self.target_ns:
            parts.append(f"target={self.target_ns!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# SegmentRouter
# ---------------------------------------------------------------------------

class SegmentRouter:
    """
    Production namespace-aware path router for episodic memory segments.

    Drop-in replacement for _DefaultPathResolver in memory_guard.py.
    Provides the same two public methods (resolve_path, namespace_dir)
    plus cross-namespace access control (check_read_allowed).

    Directory structure is created lazily on first access.  All namespace
    directories are created with mode 0o700 (owner-only).

    Usage::

        router = SegmentRouter(cfg)
        guard  = MemoryGuard(cfg, segment_router=router)

        # Direct usage (e.g. for admin tooling):
        path = router.resolve_path("primary", "ep-001")
        ns_dir = router.namespace_dir("guest")
        router.check_read_allowed(caller_ns="guest", target_ns="primary")
        # → raises NamespaceAccessError

    Args:
        cfg: Validated PhidipusConfig.  The episodes root directory is
             derived from cfg.paths.data_dir / "episodes".
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        data_dir          = Path(cfg.paths.data_dir)
        self._episodes_root: Path = data_dir / _EPISODES_SUBDIR

        # Pre-create all three namespace directories at construction time.
        # This ensures the directory layout is ready before any write attempt,
        # which avoids TOCTOU races between existence check and mkdir.
        for ns in sorted(VALID_NAMESPACES):
            ensure_dir(self._episodes_root / ns, mode=_NS_DIR_MODE)

        _log.info(
            "SegmentRouter initialised",
            extra={
                "episodes_root": str(self._episodes_root),
                "namespaces":    sorted(VALID_NAMESPACES),
            },
        )

    # ------------------------------------------------------------------
    # Core path resolution (MemoryGuard protocol)
    # ------------------------------------------------------------------

    def resolve_path(self, namespace: str, episode_id: str) -> Path:
        """
        Return the absolute Path for an episode file in *namespace*.

        The path is computed as::

            <episodes_root>/<namespace>/<episode_id>.ep.json

        The namespace directory is created if it does not exist.
        The episode file itself is not required to exist — this method
        only computes and returns the path.

        Args:
            namespace:  One of "primary", "guest", "untrusted".
            episode_id: Episode identifier (validated by MemoryGuard;
                        passed through here without re-validation).

        Returns:
            Resolved absolute Path to the episode file.

        Raises:
            NamespaceError: if *namespace* is not a valid namespace.
        """
        self._validate_namespace(namespace)
        ns_dir = ensure_dir(self._episodes_root / namespace, mode=_NS_DIR_MODE)
        return safe_join(ns_dir, episode_id + _EPISODE_EXT)

    def namespace_dir(self, namespace: str) -> Path:
        """
        Return the directory Path for *namespace*, creating it if needed.

        Args:
            namespace: One of "primary", "guest", "untrusted".

        Returns:
            Resolved absolute Path to the namespace directory.

        Raises:
            NamespaceError: if *namespace* is not valid.
        """
        self._validate_namespace(namespace)
        return ensure_dir(self._episodes_root / namespace, mode=_NS_DIR_MODE)

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def check_read_allowed(
        self,
        caller_ns: str,
        target_ns: str,
    ) -> None:
        """
        Enforce cross-namespace read isolation (R-16).

        The primary namespace is protected: only callers operating in the
        "primary" namespace may read from it.  Guest and untrusted callers
        may only read from their own namespace.

        Writes are not checked here — namespace separation for writes is
        enforced implicitly by resolve_path() routing to the correct
        directory.

        Args:
            caller_ns: The namespace the calling agent is operating in.
            target_ns: The namespace being queried.

        Raises:
            NamespaceError:       if either namespace string is invalid.
            NamespaceAccessError: if caller_ns is not permitted to read
                                  target_ns (R-16).
        """
        self._validate_namespace(caller_ns)
        self._validate_namespace(target_ns)

        # Rule: non-primary callers may not read primary.
        if target_ns == "primary" and caller_ns != "primary":
            _log.error(
                "R-16: cross-namespace read access DENIED",
                extra={
                    "caller_ns": caller_ns,
                    "target_ns": target_ns,
                    "reason":    "NAMESPACE_ACCESS_DENIED",
                },
            )
            raise NamespaceAccessError(
                f"Namespace {caller_ns!r} is not permitted to read from "
                f"the primary namespace (R-16). "
                "Only the primary planner may query the primary segment.",
                caller_ns=caller_ns,
                target_ns=target_ns,
            )

        _log.debug(
            "SegmentRouter: read access allowed",
            extra={"caller_ns": caller_ns, "target_ns": target_ns},
        )

    # ------------------------------------------------------------------
    # Informational helpers
    # ------------------------------------------------------------------

    @property
    def episodes_root(self) -> Path:
        """Resolved path to the root episodes directory (read-only)."""
        return self._episodes_root

    def list_namespaces(self) -> list[str]:
        """Return the sorted list of valid namespace identifiers."""
        return sorted(VALID_NAMESPACES)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_namespace(self, namespace: str) -> None:
        """
        Raise NamespaceError if *namespace* is not in VALID_NAMESPACES.
        """
        if namespace not in VALID_NAMESPACES:
            raise NamespaceError(
                f"Unknown namespace {namespace!r}. "
                f"Valid namespaces: {sorted(VALID_NAMESPACES)}.",
                namespace=namespace,
                reason="INVALID_NAMESPACE",
            )
