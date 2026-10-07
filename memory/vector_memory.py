# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/vector_memory.py — Phidipus v1.0
Semantic vector memory with integrity manifest and LRU eviction cap.

Architecture contract (R-16 / M-4):
  "Add integrity manifest for HNSW index snapshots.  Enforce embed_cache
   LRU cap (M-4).  Route through memory_guard.py."

VectorMemory stores episode embeddings in an in-memory VectorIndex and
persists index snapshots to disk with a SHA-256 integrity manifest.  The
manifest is written alongside the snapshot and verified before any reload.

Embedding model
---------------
VectorMemory calls an embedding function (embed_fn) injected at
construction time.  This keeps the module decoupled from any specific
embedding library.  The agent loop injects the appropriate model wrapper.

Embedding cache
---------------
A bounded LRU cache of (text → vector) mappings is maintained in memory
to avoid re-embedding identical strings.  The cache is capped at
embed_cache_max entries (M-4).

Snapshot files
--------------
  <data_dir>/vector_memory/
      index_snapshot.json   — VectorIndex.to_snapshot() output
      index_manifest.json   — {"sha256": "<hex>", "ts": "<iso>"}

The snapshot is written atomically; the manifest is written after the
snapshot is stable on disk.  On reload, the manifest SHA-256 is verified
against the snapshot file before the index is loaded.

Process: orchestrator (L1)

Security invariants enforced here:
  R-16  Index snapshot is integrity-checked via SHA-256 manifest before
        loading.  A tampered or corrupt snapshot is rejected.

Used by:
  core/agent_loop.py          — add_embedding(), search_similar()
  memory/episodic_memory.py   — semantic retrieval support

Dependencies:
  memory/vector_index.py     — VectorIndex (HNSW implementation)
  utils/atomic_file.py       — atomic_write_bytes()
  utils/hash_utils.py        — sha256_hex(), sha256_verify_file()
  utils/json_utils.py        — dumps(), loads()
  utils/logger.py            — get_logger()
  config/config_loader.py    — PhidipusConfig
"""

from __future__ import annotations

import collections
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config.config_loader import PhidipusConfig
from memory.vector_index import VectorIndex, VectorIndexError
from utils.atomic_file import atomic_write_bytes, safe_read_bytes
from utils.hash_utils import sha256_hex, sha256_verify_file
from utils.json_utils import JsonDecodeError, dumps, loads
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SNAPSHOT_FILENAME: str  = "index_snapshot.json"
_MANIFEST_FILENAME: str  = "index_manifest.json"
_VECTOR_MEM_SUBDIR: str  = "vector_memory"
_DEFAULT_CACHE_MAX: int  = 1024


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class VectorMemoryError(RuntimeError):
    """
    Raised on VectorMemory integrity or operational errors.

    Attributes:
        reason: Short machine-readable reason code.
    """

    def __init__(self, message: str, *, reason: str = "VECTOR_MEM_ERROR") -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"[{self.reason}] {super().__str__()}"


class SnapshotIntegrityError(VectorMemoryError):
    """
    Raised when the SHA-256 manifest does not match the snapshot file (R-16).

    Attributes:
        snapshot_path: Path to the corrupt snapshot file.
        reason:        Always "SNAPSHOT_INTEGRITY".
    """

    def __init__(
        self,
        message: str,
        *,
        snapshot_path: str = "",
        reason: str = "SNAPSHOT_INTEGRITY",
    ) -> None:
        super().__init__(message, reason=reason)
        self.snapshot_path = snapshot_path


# ---------------------------------------------------------------------------
# VectorMemory
# ---------------------------------------------------------------------------

class VectorMemory:
    """
    Semantic vector memory backed by an in-memory HNSW index.

    Provides add/search semantics with an LRU embedding cache and
    integrity-checked snapshot persistence.

    Usage::

        def embed(text: str) -> list[float]:
            return model.encode(text).tolist()

        vm = VectorMemory(cfg, embed_fn=embed)
        vm.add_embedding("ep-001", "User opened the browser and searched X")
        results = vm.search("search query", k=5)

    Args:
        cfg:            Validated PhidipusConfig.
        embed_fn:       Callable that takes a str and returns a list[float].
        embed_cache_max: LRU cache size cap (M-4).  Defaults to
                         _DEFAULT_CACHE_MAX.
    """

    def __init__(
        self,
        cfg:             PhidipusConfig,
        embed_fn:        Callable[[str], list[float]],
        embed_cache_max: int = _DEFAULT_CACHE_MAX,
    ) -> None:
        self._embed_fn   = embed_fn
        self._cache: collections.OrderedDict[str, list[float]] = (
            collections.OrderedDict()
        )
        self._cache_max  = max(1, embed_cache_max)

        # Snapshot storage directory
        data_dir          = Path(cfg.paths.data_dir)
        self._store_dir   = data_dir / _VECTOR_MEM_SUBDIR
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_path = self._store_dir / _SNAPSHOT_FILENAME
        self._manifest_path = self._store_dir / _MANIFEST_FILENAME

        # Config for index construction
        self._max_items  = cfg.memory.vector_max_items
        self._m          = getattr(cfg.memory, "vector_m", 16)
        self._ef         = getattr(cfg.memory, "vector_ef_construction", 200)

        # Attempt to restore from snapshot; build empty index on failure
        self._index: VectorIndex = self._load_or_init()

        _log.info(
            "VectorMemory initialised",
            extra={
                "max_items":  self._max_items,
                "cache_max":  self._cache_max,
                "index_size": self._index.size,
                "store_dir":  str(self._store_dir),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_embedding(self, episode_id: str, text: str) -> None:
        """
        Embed *text* and add it to the index with *episode_id* as metadata.

        If the index is at capacity the oldest entry is evicted (M-4).

        Args:
            episode_id: Episode identifier stored as metadata.
            text:       Text to embed and index.
        """
        vec = self._get_embedding(text)
        self._index.add(vec, {"id": episode_id, "text": text[:256]})
        _log.debug(
            "VectorMemory: embedding added",
            extra={"episode_id": episode_id, "index_size": self._index.size},
        )

    def search(
        self,
        query: str,
        k: int = 5,
    ) -> list[tuple[float, dict[str, Any]]]:
        """
        Search for the *k* most semantically similar episodes.

        Args:
            query: Query text.
            k:     Number of results.

        Returns:
            List of (cosine_similarity, metadata) sorted descending.
        """
        vec = self._get_embedding(query)
        return self._index.search(vec, k=k)

    def save_snapshot(self) -> None:
        """
        Persist the current index to disk with a SHA-256 manifest (R-16).

        Writes:
          1. index_snapshot.json  — VectorIndex snapshot (atomic write)
          2. index_manifest.json  — {"sha256": "…", "ts": "…"} (atomic write)
        """
        snap_bytes = dumps(self._index.to_snapshot()).encode("utf-8")
        atomic_write_bytes(self._snapshot_path, snap_bytes, mode=0o600)

        sha = sha256_hex(snap_bytes)
        # Use millisecond precision (3 digits) consistent with action_schema._utc_now()
        now = datetime.now(tz=timezone.utc)
        ts  = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        manifest = dumps({"sha256": sha, "ts": ts}).encode("utf-8")
        atomic_write_bytes(self._manifest_path, manifest, mode=0o600)

        _log.info(
            "R-16: VectorMemory snapshot saved with manifest",
            extra={
                "snapshot_path": str(self._snapshot_path),
                "sha256":        sha,
                "index_size":    self._index.size,
            },
        )

    @property
    def size(self) -> int:
        """Current number of vectors in the index."""
        return self._index.size

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_embedding(self, text: str) -> list[float]:
        """Return cached or freshly computed embedding for *text* (M-4 LRU)."""
        if text in self._cache:
            # Move to end = most recently used
            self._cache.move_to_end(text)
            return self._cache[text]
        vec = self._embed_fn(text)
        self._cache[text] = vec
        self._cache.move_to_end(text)
        # Evict least recently used when over cap (M-4)
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return vec

    def _load_or_init(self) -> VectorIndex:
        """
        Load snapshot from disk if valid manifest exists; otherwise return
        a fresh empty VectorIndex.
        """
        if not self._snapshot_path.exists() or not self._manifest_path.exists():
            _log.debug("VectorMemory: no snapshot found — starting fresh")
            return self._new_index()

        # Verify manifest SHA-256 (R-16)
        try:
            raw_manifest = safe_read_bytes(self._manifest_path)
            if raw_manifest is None:
                raise VectorMemoryError("Manifest file empty", reason="SNAPSHOT_INTEGRITY")
            manifest = loads(raw_manifest.decode("utf-8"))
            expected_sha = manifest.get("sha256", "")
        except (JsonDecodeError, UnicodeDecodeError, VectorMemoryError) as exc:
            _log.error(
                "R-16: VectorMemory manifest unreadable — discarding snapshot",
                extra={"error": str(exc)},
            )
            return self._new_index()

        if not sha256_verify_file(self._snapshot_path, expected_sha):
            _log.error(
                "R-16: VectorMemory snapshot SHA-256 mismatch — discarding",
                extra={"snapshot_path": str(self._snapshot_path)},
            )
            return self._new_index()

        # Load snapshot
        try:
            raw_snap = self._snapshot_path.read_bytes()
            snap_obj = loads(raw_snap.decode("utf-8"))
            index    = VectorIndex.from_snapshot(snap_obj)
            _log.info(
                "R-16: VectorMemory snapshot loaded and verified",
                extra={"index_size": index.size},
            )
            return index
        except (VectorIndexError, JsonDecodeError, UnicodeDecodeError) as exc:
            _log.error(
                "R-16: VectorMemory snapshot corrupt — starting fresh",
                extra={"error": str(exc)},
            )
            return self._new_index()

    def _new_index(self) -> VectorIndex:
        """Create a blank VectorIndex. Dim is derived from a probe embedding."""
        try:
            probe = self._embed_fn("probe")
            dim   = len(probe)
        except Exception:
            dim = 384   # fallback: paraphrase-multilingual-MiniLM-L12-v2
        return VectorIndex(
            dim=dim,
            m=self._m,
            ef_construction=self._ef,
            max_items=self._max_items,
        )
