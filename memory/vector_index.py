# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/vector_index.py — Phidipus v1.0 (Phase 3)
Dual-backend HNSW vector index for semantic episode retrieval.

Phase 3 upgrade: try hnswlib (C++ backend, 10-40x faster search) with
automatic fallback to the pure-Python HNSW implementation when hnswlib
is not installed.

    pip install hnswlib   # optional but recommended

The public interface is unchanged from v9.11:
  - VectorIndex(dim, m, ef_construction, max_items, seed)
  - add(vec, meta) -> int
  - search(query, k, ef) -> list[(score, meta)]
  - to_snapshot() -> dict
  - from_snapshot(snap) -> VectorIndex
  - size, dim properties

Architecture contract (RETAIN):
  This module is pure algorithmic logic.  Integrity enforcement is
  handled at the vector_memory.py layer above.

Process: orchestrator (L1)

Security invariants enforced here:
  (None -- this module is pure algorithmic logic.  Integrity is enforced
   by vector_memory.py which wraps this module.)

Used by:
  memory/vector_memory.py -- wraps VectorIndex with integrity enforcement

Dependencies:
  utils/logger.py -- get_logger()
  hnswlib          -- optional C++ backend (pip install hnswlib)
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# Try to import hnswlib C++ backend
# ---------------------------------------------------------------------------

_HAS_HNSWLIB = False
try:
    import hnswlib as _hnswlib   # type: ignore[import-untyped]
    import numpy as _np
    _HAS_HNSWLIB = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default number of bi-directional links per node (HNSW M parameter).
_DEFAULT_M: int = 16

#: Default ef_construction parameter (controls build-time recall / speed).
_DEFAULT_EF_CONSTRUCTION: int = 200


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class VectorIndexError(RuntimeError):
    """
    Raised on VectorIndex operational errors.

    Attributes:
        reason: Short machine-readable reason code.
    """

    def __init__(self, message: str, *, reason: str = "INDEX_ERROR") -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Pure-Python HNSW data structure (fallback only)
# ---------------------------------------------------------------------------

@dataclass
class _Node:
    """Single node in the pure-Python HNSW graph."""
    vec:       list[float]
    meta:      dict[str, Any]
    neighbors: dict[int, list[int]] = field(default_factory=dict)
    level:     int = 0


# ---------------------------------------------------------------------------
# VectorIndex -- dual backend
# ---------------------------------------------------------------------------

class VectorIndex:
    """
    HNSW approximate nearest-neighbour index with dual backend.

    When ``hnswlib`` is installed, uses the C++ implementation for
    10-40x faster search.  Otherwise falls back to a pure-Python HNSW
    that requires no external dependencies.

    Usage::

        index = VectorIndex(dim=768)
        index.add([0.1, 0.2, ...], {"id": "ep-001"})
        results = index.search([0.1, 0.2, ...], k=5)

    Args:
        dim:              Dimensionality of vectors.
        m:                HNSW M parameter.
        ef_construction:  Build-time candidate list size.
        max_items:        Maximum vectors (LRU eviction when full).
        seed:             Random seed for reproducible layer assignment.
    """

    def __init__(
        self,
        dim:             int,
        m:               int   = _DEFAULT_M,
        ef_construction: int   = _DEFAULT_EF_CONSTRUCTION,
        max_items:       int   = 10_000,
        seed:            int | None = None,
    ) -> None:
        if dim <= 0:
            raise VectorIndexError(
                f"dim must be > 0, got {dim}.", reason="INVALID_DIM"
            )
        self._dim        = dim
        self._m          = m
        self._ef         = ef_construction
        self._max_items  = max_items
        self._use_native = _HAS_HNSWLIB

        if self._use_native:
            # -- hnswlib C++ backend ------------------------------------
            self._hns_index = _hnswlib.Index(space="cosine", dim=dim)
            self._hns_index.init_index(
                max_elements=max_items, M=m, ef_construction=ef_construction,
            )
            self._hns_index.set_ef(max(50, ef_construction // 4))
            self._metadata: dict[int, dict[str, Any]] = {}
            self._next_id: int = 0
            self._live_ids: list[int] = []  # insertion-order for LRU
        else:
            # -- Pure-Python fallback -----------------------------------
            self._rng          = random.Random(seed)
            self._nodes:       list[_Node] = []
            self._entry_point: int | None = None

        _log.debug(
            "VectorIndex initialised",
            extra={
                "dim": dim, "m": m, "ef_construction": ef_construction,
                "max_items": max_items,
                "backend": "hnswlib" if self._use_native else "python",
            },
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        """Current number of vectors in the index."""
        if self._use_native:
            return len(self._live_ids)
        return len(self._nodes)

    @property
    def dim(self) -> int:
        """Vector dimensionality."""
        return self._dim

    # ------------------------------------------------------------------
    # add()
    # ------------------------------------------------------------------

    def add(self, vec: list[float], meta: dict[str, Any]) -> int:
        """
        Add a vector and associated metadata to the index.

        LRU-by-insertion eviction when at max_items (M-4).

        Raises:
            VectorIndexError: on dim mismatch or non-finite values (B-07).
        """
        if len(vec) != self._dim:
            raise VectorIndexError(
                f"Vector length {len(vec)} does not match index dim {self._dim}.",
                reason="DIM_MISMATCH",
            )
        # FIX B-07: reject NaN/Inf
        if any(not math.isfinite(v) for v in vec):
            raise VectorIndexError(
                "Vector contains non-finite values (NaN or Infinity). "
                "All vector components must be finite floats.",
                reason="NON_FINITE_VECTOR",
            )

        if self._use_native:
            return self._add_native(vec, meta)
        return self._add_python(vec, meta)

    # -- hnswlib backend -----------------------------------------------

    def _add_native(self, vec: list[float], meta: dict[str, Any]) -> int:
        if len(self._live_ids) >= self._max_items:
            self._evict_oldest_native()

        # FIX M-07: compact when _next_id drifts far ahead of live count.
        # After many evict+add cycles, _next_id grows indefinitely causing
        # repeated resize_index() calls and wasted memory.
        if self._next_id > len(self._live_ids) * 3 + self._max_items:
            self._compact_native()

        node_id = self._next_id
        self._next_id += 1
        arr = _np.array([vec], dtype=_np.float32)
        ids = _np.array([node_id], dtype=_np.int64)

        # Resize if hnswlib internal capacity is exhausted
        if node_id >= self._hns_index.get_max_elements():
            self._hns_index.resize_index(node_id + self._max_items)

        self._hns_index.add_items(arr, ids)
        self._metadata[node_id] = dict(meta)
        self._live_ids.append(node_id)
        return node_id

    def _evict_oldest_native(self) -> None:
        if not self._live_ids:
            return
        oldest = self._live_ids.pop(0)
        try:
            self._hns_index.mark_deleted(oldest)
        except Exception:
            pass
        self._metadata.pop(oldest, None)

    def _compact_native(self) -> None:
        """FIX M-07: rebuild hnswlib index to reclaim deleted slots.

        Copies all live vectors into a fresh index with reset IDs.
        Called when _next_id >> len(_live_ids) to prevent unbounded growth.
        """
        old_live = list(self._live_ids)
        old_meta = dict(self._metadata)

        # Collect live vectors
        vecs: list[list[float]] = []
        metas: list[dict[str, Any]] = []
        for nid in old_live:
            try:
                vec = self._hns_index.get_items([nid])[0].tolist()
            except Exception:
                continue
            vecs.append(vec)
            metas.append(old_meta.get(nid, {}))

        # Rebuild fresh index
        self._hns_index = _hnswlib.Index(space="cosine", dim=self._dim)
        self._hns_index.init_index(
            max_elements=self._max_items, M=self._m, ef_construction=self._ef,
        )
        self._hns_index.set_ef(max(50, self._ef // 4))
        self._metadata.clear()
        self._live_ids.clear()
        self._next_id = 0

        for vec, meta in zip(vecs, metas):
            arr = _np.array([vec], dtype=_np.float32)
            ids = _np.array([self._next_id], dtype=_np.int64)
            self._hns_index.add_items(arr, ids)
            self._metadata[self._next_id] = meta
            self._live_ids.append(self._next_id)
            self._next_id += 1

        _log.info(
            "VectorIndex: compacted hnswlib index",
            extra={"live_items": len(self._live_ids), "new_next_id": self._next_id},
        )

    # -- Python fallback -----------------------------------------------

    def _add_python(self, vec: list[float], meta: dict[str, Any]) -> int:
        if len(self._nodes) >= self._max_items:
            self._evict_oldest_python()

        level   = self._random_level()
        node    = _Node(vec=list(vec), meta=dict(meta), level=level)
        node_id = len(self._nodes)
        self._nodes.append(node)

        if self._entry_point is None:
            self._entry_point = node_id
        else:
            self._insert_node(node_id, level)

        return node_id

    def _evict_oldest_python(self) -> None:
        if not self._nodes:
            return
        self._nodes.pop(0)
        for node in self._nodes:
            new_nb: dict[int, list[int]] = {}
            for layer, nbs in node.neighbors.items():
                new_nb[layer] = [nid - 1 for nid in nbs if nid > 0]
            node.neighbors = new_nb
        if self._entry_point is not None:
            self._entry_point = max(0, self._entry_point - 1)
        if not self._nodes:
            self._entry_point = None

    # ------------------------------------------------------------------
    # search()
    # ------------------------------------------------------------------

    def search(
        self,
        query: list[float],
        k: int = 5,
        ef: int | None = None,
    ) -> list[tuple[float, dict[str, Any]]]:
        """
        Return the *k* nearest neighbours to *query*.

        Returns:
            List of (cosine_similarity, metadata) sorted descending.
        """
        if self.size == 0:
            return []
        if len(query) != self._dim:
            raise VectorIndexError(
                f"Query length {len(query)} does not match index dim {self._dim}.",
                reason="DIM_MISMATCH",
            )

        if self._use_native:
            return self._search_native(query, k, ef)
        return self._search_python(query, k, ef)

    def _search_native(
        self, query: list[float], k: int, ef: int | None,
    ) -> list[tuple[float, dict[str, Any]]]:
        if ef is not None:
            self._hns_index.set_ef(ef)
        actual_k = min(k, len(self._live_ids))
        if actual_k == 0:
            return []
        q_arr = _np.array([query], dtype=_np.float32)
        labels, distances = self._hns_index.knn_query(q_arr, k=actual_k)

        results: list[tuple[float, dict[str, Any]]] = []
        for label, dist in zip(labels[0], distances[0]):
            lid = int(label)
            sim = 1.0 - float(dist)  # hnswlib cosine: distance = 1 - sim
            meta = dict(self._metadata.get(lid, {}))
            results.append((sim, meta))

        results.sort(key=lambda x: x[0], reverse=True)
        return results

    def _search_python(
        self, query: list[float], k: int, ef: int | None,
    ) -> list[tuple[float, dict[str, Any]]]:
        if not self._nodes:
            return []

        ef_search = ef if ef is not None else max(k, self._ef // 2)
        candidates = self._search_layer(
            query, self._entry_point, ef=ef_search, layer=0
        )

        results: list[tuple[float, dict[str, Any]]] = []
        for node_id in candidates[:k]:
            sim = _cosine_similarity(query, self._nodes[node_id].vec)
            results.append((sim, dict(self._nodes[node_id].meta)))

        results.sort(key=lambda x: x[0], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Snapshot / Restore — always uses portable Python format
    # ------------------------------------------------------------------

    def to_snapshot(self) -> dict[str, Any]:
        """Serialise the entire index to a JSON-compatible dict."""
        nodes_data: list[dict[str, Any]] = []

        if self._use_native:
            for nid in self._live_ids:
                try:
                    vec = self._hns_index.get_items([nid])[0].tolist()
                except Exception:
                    vec = [0.0] * self._dim
                meta = self._metadata.get(nid, {})
                nodes_data.append({
                    "vec": vec, "meta": meta, "level": 0, "neighbors": {},
                })
        else:
            for node in self._nodes:
                nodes_data.append({
                    "vec":       node.vec,
                    "meta":      node.meta,
                    "level":     node.level,
                    "neighbors": {str(lk): v for lk, v in node.neighbors.items()},
                })

        return {
            "version":         2,
            "dim":             self._dim,
            "m":               self._m,
            "ef_construction": self._ef,
            "max_items":       self._max_items,
            "entry_point":     self._entry_point if not self._use_native else None,
            "nodes":           nodes_data,
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> "VectorIndex":
        """Restore a VectorIndex from a snapshot dict.

        Accepts both v1 (v9.11) and v2 (v9.12) snapshot formats.
        """
        try:
            idx = cls(
                dim=snap["dim"],
                m=snap["m"],
                ef_construction=snap["ef_construction"],
                max_items=snap["max_items"],
            )
            for n in snap.get("nodes", []):
                idx.add(n["vec"], n["meta"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VectorIndexError(
                f"Snapshot is malformed: {exc}",
                reason="SNAPSHOT_CORRUPT",
            ) from exc
        return idx

    # ------------------------------------------------------------------
    # Pure-Python HNSW internals (fallback only)
    # ------------------------------------------------------------------

    def _random_level(self) -> int:
        level = 0
        while self._rng.random() < (1.0 / math.e) and level < 4:
            level += 1
        return level

    def _insert_node(self, node_id: int, level: int) -> None:
        node  = self._nodes[node_id]
        entry = self._entry_point

        ep_level = self._nodes[entry].level if entry is not None else 0
        for lc in range(ep_level, level, -1):
            entry = self._greedy_search(node.vec, entry, layer=lc)

        for lc in range(min(level, ep_level), -1, -1):
            candidates = self._search_layer(node.vec, entry, ef=self._ef, layer=lc)
            nearest = sorted(
                candidates,
                key=lambda nid: _cosine_similarity(node.vec, self._nodes[nid].vec),
                reverse=True,
            )[: self._m]
            node.neighbors[lc] = nearest
            for nb_id in nearest:
                nb = self._nodes[nb_id]
                if lc not in nb.neighbors:
                    nb.neighbors[lc] = []
                if node_id not in nb.neighbors[lc]:
                    nb.neighbors[lc].append(node_id)
                    if len(nb.neighbors[lc]) > self._m:
                        nb.neighbors[lc] = sorted(
                            nb.neighbors[lc],
                            key=lambda nid: _cosine_similarity(
                                nb.vec, self._nodes[nid].vec
                            ),
                            reverse=True,
                        )[: self._m]
            if candidates:
                entry = candidates[0]

        if level > self._nodes[self._entry_point].level:
            self._entry_point = node_id

    def _greedy_search(self, query: list[float], entry: int, layer: int) -> int:
        current     = entry
        current_sim = _cosine_similarity(query, self._nodes[current].vec)
        while True:
            improved = False
            for nb_id in self._nodes[current].neighbors.get(layer, []):
                sim = _cosine_similarity(query, self._nodes[nb_id].vec)
                if sim > current_sim:
                    current, current_sim = nb_id, sim
                    improved = True
            if not improved:
                break
        return current

    def _search_layer(
        self, query: list[float], entry: int, ef: int, layer: int,
    ) -> list[int]:
        if entry is None or entry >= len(self._nodes):
            return []

        visited:    set[int] = {entry}
        candidates: list[tuple[float, int]] = []
        entry_sim = _cosine_similarity(query, self._nodes[entry].vec)
        candidates.append((entry_sim, entry))

        heap: list[tuple[float, int]] = [(-entry_sim, entry)]
        while heap:
            neg_sim, node_id = heapq.heappop(heap)
            sim = -neg_sim
            if len(candidates) >= ef and sim < candidates[ef - 1][0]:
                break
            for nb_id in self._nodes[node_id].neighbors.get(layer, []):
                if nb_id in visited:
                    continue
                visited.add(nb_id)
                ph_sim = _cosine_similarity(query, self._nodes[nb_id].vec)
                candidates.append((ph_sim, nb_id))
                heapq.heappush(heap, (-ph_sim, nb_id))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [nid for _, nid in candidates[:ef]]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot    = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
