# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/episodic_memory.py — Phidipus v1.0
Ring-buffer episodic memory with HMAC-SHA256 integrity and namespace isolation.

Architecture contract (R-16 / H-5):
  "All writes: HMAC-SHA256 sign via memory_guard.py.  All reads: verify
   signature, reject invalid.  Append-only with hash chaining."

EpisodicMemory is the primary interface for the agent loop and planner to
persist and retrieve task episodes.  It enforces a ring-buffer size cap
(cfg.memory.episodic_max_episodes) and routes all disk I/O through
MemoryGuard — no direct file access occurs in this module.

Ring-buffer behaviour
---------------------
When the number of episodes in the namespace reaches episodic_max_episodes,
the oldest episode (by creation order stored in the index) is evicted before
the new episode is written.  The index is a lightweight JSON file that maps
episode_id → write_timestamp and preserves insertion order.

Index file
----------
A per-namespace index file is maintained at::

    <episodes_root>/<namespace>/index.json

It is written atomically via atomic_write_bytes() and contains::

    {
      "schema_version": 1,
      "entries": [
        {"id": "ep-001", "ts": "2026-03-14T01:23:45.123Z"},
        ...
      ]
    }

The index is NOT HMAC-signed — it is reconstructable from the episode files
and serves only as an ordered eviction queue.  MemoryGuard handles the
cryptographic integrity of individual episode files.

Process: orchestrator (L1)

Security invariants enforced here:
  R-16  All episode writes go through MemoryGuard.write_episode() —
        never directly to disk.  All reads go through
        MemoryGuard.read_episode() which verifies the HMAC.
  M-7   task_memory is not stored here.  EpisodicMemory stores only
        completed-task episodes.

Used by:
  core/agent_loop.py          — store_episode(), retrieve_recent()
  skills/success_skill_compiler.py — retrieve episodes for compilation

Dependencies:
  memory_integrity/memory_guard.py  — MemoryGuard (all disk R/W)
  utils/atomic_file.py              — atomic_write_bytes() for index
  utils/json_utils.py               — loads(), dumps()
  utils/logger.py                   — get_logger()
  config/config_loader.py           — PhidipusConfig
"""

from __future__ import annotations

import uuid as _uuid  # FIX B-06: auto-generate episode ID
from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from ipc.action_schema import _utc_now   # single source of truth for UTC timestamps
from memory_integrity.memory_guard import (
    EpisodeNotFoundError,
    EpisodeValidationError,
    IntegrityError,
    MemoryGuard,
)
from utils.atomic_file import atomic_write_bytes, safe_read_bytes
from utils.json_utils import JsonDecodeError, dumps, loads
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INDEX_FILENAME: str = "index.json"
_INDEX_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EpisodicMemoryError(RuntimeError):
    """
    Raised when EpisodicMemory encounters an unrecoverable error.

    Attributes:
        episode_id: ID of the episode involved (may be empty).
        reason:     Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        episode_id: str = "",
        reason: str = "EPISODIC_ERROR",
    ) -> None:
        super().__init__(message)
        self.episode_id = episode_id
        self.reason     = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.episode_id:
            parts.append(f"id={self.episode_id!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


def _load_index(index_path: Path) -> list[dict[str, str]]:
    """
    Read and parse the namespace index file.

    Returns an empty list if the file does not exist or cannot be parsed.
    Index corruption is non-fatal — MemoryGuard still enforces episode
    integrity via HMAC on each individual file.
    """
    raw = safe_read_bytes(index_path)
    if raw is None:
        return []
    try:
        obj = loads(raw.decode("utf-8"))
        if isinstance(obj, dict) and isinstance(obj.get("entries"), list):
            return [
                e for e in obj["entries"]
                if isinstance(e, dict) and "id" in e and "ts" in e
            ]
    except (JsonDecodeError, UnicodeDecodeError):
        _log.warning(
            "EpisodicMemory: index file corrupt — rebuilding as empty",
            extra={"index_path": str(index_path)},
        )
    return []


def _save_index(index_path: Path, entries: list[dict[str, str]]) -> None:
    """Write the namespace index atomically."""
    payload = dumps(
        {"schema_version": _INDEX_SCHEMA_VERSION, "entries": entries},
        sort_keys=True,
    ).encode("utf-8")
    atomic_write_bytes(index_path, payload, mode=0o600)


# ---------------------------------------------------------------------------
# EpisodicMemory
# ---------------------------------------------------------------------------

class EpisodicMemory:
    """
    Ring-buffer episodic memory with HMAC-SHA256 integrity.

    Stores completed-task episodes as HMAC-signed JSON files via
    MemoryGuard.  Enforces a configurable size cap by evicting the oldest
    episode when the ring buffer is full.

    All disk I/O is delegated to MemoryGuard — this class never writes
    files directly.

    Usage::

        memory = EpisodicMemory(cfg, guard)
        memory.store({"id": "ep-001", "task": "open browser", "outcome": "success"})
        episode = memory.retrieve("ep-001")
        recent  = memory.retrieve_recent(n=10)

    Args:
        cfg:       Validated PhidipusConfig.
        guard:     MemoryGuard instance (injected for testability).
        namespace: Episode namespace to operate in.  Defaults to
                   cfg.memory.episode_namespace ("primary").
    """

    def __init__(
        self,
        cfg:       PhidipusConfig,
        guard:     MemoryGuard,
        namespace: str | None = None,
    ) -> None:
        self._guard     = guard
        self._namespace = namespace or cfg.memory.episode_namespace
        self._max_eps   = cfg.memory.episodic_max_episodes

        # Index file lives in the namespace directory managed by the guard's
        # router.  We resolve it via namespace_dir() which creates it if absent.
        ns_dir = guard._router.namespace_dir(self._namespace)
        self._index_path: Path = ns_dir / _INDEX_FILENAME

        _log.info(
            "EpisodicMemory initialised",
            extra={
                "namespace":  self._namespace,
                "max_eps":    self._max_eps,
                "index_path": str(self._index_path),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store(self, episode: dict[str, Any]) -> Path:
        """
        Persist *episode* to disk via MemoryGuard (HMAC-signed, R-16).

        If the ring buffer is full, the oldest episode is evicted first.

        Args:
            episode: Dict with at minimum an ``"id"`` key (non-empty str).
                     All values must be JSON-serialisable.

        Returns:
            Path to the written episode file.

        Raises:
            EpisodeValidationError: if episode structure is invalid.
            EpisodicMemoryError:    on unexpected I/O failure.
        """
        if not isinstance(episode, dict):
            raise EpisodeValidationError(
                f"Episode must be a dict, got {type(episode).__name__!r}.",
                field="<root>",
                reason="INVALID_TYPE",
            )

        episode_id: str = episode.get("id", "")  # type: ignore[assignment]
        if not isinstance(episode_id, str) or not episode_id:
            # FIX B-06: auto-generate UUID if "id" field is missing or empty,
            # rather than raising an error that forces every caller to
            # pre-generate IDs manually.
            episode_id = str(_uuid.uuid4())
            episode = {"id": episode_id, **episode}

        # Add write timestamp if absent
        if "ts" not in episode:
            episode = {**episode, "ts": _utc_now()}

        # Load index, evict if needed
        index = _load_index(self._index_path)
        index = self._evict_if_full(index)

        # Write via guard (HMAC-signed, R-16)
        try:
            dest = self._guard.write_episode(episode, namespace=self._namespace)
        except Exception as exc:
            raise EpisodicMemoryError(
                f"Failed to store episode {episode_id!r}: {exc}",
                episode_id=episode_id,
                reason="WRITE_FAILED",
            ) from exc

        # Update index
        index.append({"id": episode_id, "ts": episode.get("ts", _utc_now())})
        _save_index(self._index_path, index)

        _log.debug(
            "R-16: episode stored",
            extra={
                "episode_id": episode_id,
                "namespace":  self._namespace,
                "index_size": len(index),
            },
        )
        return dest

    def retrieve(self, episode_id: str) -> dict[str, Any]:
        """
        Retrieve and HMAC-verify an episode by ID (R-16).

        Args:
            episode_id: Episode identifier.

        Returns:
            Verified episode dict.

        Raises:
            EpisodeNotFoundError: if the episode does not exist.
            IntegrityError:       if the HMAC check fails.
        """
        return self._guard.read_episode(episode_id, namespace=self._namespace)

    def retrieve_recent(self, n: int = 10) -> list[dict[str, Any]]:
        """
        Return the *n* most recently stored episodes, newest first.

        Episodes that fail HMAC verification are skipped with a warning.

        Args:
            n: Maximum number of episodes to return.  Must be >= 1.

        Returns:
            List of verified episode dicts, newest first.
        """
        if n < 1:
            n = 1
        index = _load_index(self._index_path)
        # Newest first — index is stored oldest-first
        recent_ids = [entry["id"] for entry in reversed(index)][:n]

        results: list[dict[str, Any]] = []
        for ep_id in recent_ids:
            try:
                results.append(self._guard.read_episode(ep_id, namespace=self._namespace))
            except IntegrityError as exc:
                _log.warning(
                    "R-16: skipping episode with invalid HMAC during retrieve_recent",
                    extra={"episode_id": ep_id, "reason": exc.reason},
                )
            except EpisodeNotFoundError:
                _log.warning(
                    "EpisodicMemory: index references missing episode file",
                    extra={"episode_id": ep_id},
                )
        return results

    def delete(self, episode_id: str) -> bool:
        """
        Delete an episode from disk and remove it from the index.

        Args:
            episode_id: Episode identifier.

        Returns:
            True if deleted, False if not found.
        """
        deleted = self._guard.delete_episode(episode_id, namespace=self._namespace)
        if deleted:
            index = _load_index(self._index_path)
            index = [e for e in index if e["id"] != episode_id]
            _save_index(self._index_path, index)
            _log.debug(
                "EpisodicMemory: episode deleted",
                extra={"episode_id": episode_id, "namespace": self._namespace},
            )
        return deleted

    def count(self) -> int:
        """Return the number of episodes currently stored in this namespace."""
        return len(self._guard.list_episodes(self._namespace))

    def list_ids(self) -> list[str]:
        """Return all episode IDs in insertion order (oldest first)."""
        index = _load_index(self._index_path)
        return [e["id"] for e in index]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_if_full(
        self, index: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """
        If the ring buffer is at capacity, evict the oldest episode.

        Eviction removes the file via guard.delete_episode() and removes
        the corresponding entry from the index.  A single eviction per
        store() call is sufficient since max_episodes is enforced on every
        write.

        Returns the updated index list.
        """
        while len(index) >= self._max_eps:
            oldest = index[0]
            ep_id  = oldest["id"]
            try:
                self._guard.delete_episode(ep_id, namespace=self._namespace)
                _log.debug(
                    "EpisodicMemory: ring buffer eviction",
                    extra={
                        "evicted_id": ep_id,
                        "namespace":  self._namespace,
                        "max_eps":    self._max_eps,
                    },
                )
            except Exception as exc:
                _log.warning(
                    "EpisodicMemory: could not evict episode",
                    extra={"episode_id": ep_id, "error": str(exc)},
                )
            # Remove from index regardless of delete success to avoid loop
            index = index[1:]
        return index
