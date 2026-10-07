#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
rag_engine/vector_db.py — Phidipus AI Forge E6
═══════════════════════════════════════════════════════════════════

Vector Database wrapper for RAG pipeline.
Supports ChromaDB (primary) with in-memory fallback.

Usage:
  from rag_engine.vector_db import VectorDB
  db = VectorDB(collection_name="sales_frameworks")
  db.add_documents(docs, metadatas)
  results = db.search("khách hỏi giá", top_k=3)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

try:
    import chromadb
    from chromadb.config import Settings
    _HAS_CHROMADB = True
except ImportError:
    _HAS_CHROMADB = False


PERSIST_DIR = os.environ.get("VECTOR_DB_DIR", "data/vector_db")


class VectorDB:
    """Lightweight vector DB wrapper with ChromaDB backend + in-memory fallback."""

    def __init__(
        self,
        collection_name: str = "frameworks",
        persist_dir: str = PERSIST_DIR,
        embedding_model: str = "default",
    ):
        self.collection_name = collection_name
        self.persist_dir = persist_dir
        self._backend = "chromadb" if _HAS_CHROMADB else "memory"
        self._collection = None
        self._memory_store: list[dict] = []
        self._init_db()

    def _init_db(self):
        """Initialize backend."""
        if self._backend == "chromadb":
            try:
                Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(
                    path=self.persist_dir,
                    settings=Settings(anonymized_telemetry=False),
                )
                self._collection = self._client.get_or_create_collection(
                    name=self.collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
                print(f"[VectorDB] ChromaDB ready: {self.collection_name} "
                      f"({self._collection.count()} docs)")
            except Exception as exc:
                print(f"[VectorDB] ChromaDB init failed: {exc}, falling back to memory")
                self._backend = "memory"
                self._collection = None
        else:
            print(f"[VectorDB] Using in-memory store (chromadb not installed)")

    @property
    def count(self) -> int:
        if self._backend == "chromadb" and self._collection:
            return self._collection.count()
        return len(self._memory_store)

    @property
    def backend(self) -> str:
        return self._backend

    def add_documents(
        self,
        documents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ) -> int:
        """Add documents to the vector store. Returns number added."""
        if not documents:
            return 0

        if ids is None:
            ids = [
                hashlib.md5(doc.encode("utf-8")).hexdigest()[:12]
                for doc in documents
            ]

        if metadatas is None:
            metadatas = [{}] * len(documents)

        if self._backend == "chromadb" and self._collection:
            try:
                self._collection.upsert(
                    documents=documents,
                    metadatas=metadatas,
                    ids=ids,
                )
                return len(documents)
            except Exception as exc:
                print(f"[VectorDB] ChromaDB upsert error: {exc}")
                return 0
        else:
            for doc, meta, doc_id in zip(documents, metadatas, ids):
                self._memory_store.append({
                    "id": doc_id,
                    "document": doc,
                    "metadata": meta,
                })
            return len(documents)

    def search(
        self,
        query: str,
        top_k: int = 3,
        where: dict | None = None,
    ) -> list[dict]:
        """Search for similar documents. Returns list of {document, metadata, score}."""
        if not query.strip():
            return []

        if self._backend == "chromadb" and self._collection:
            try:
                kwargs = {
                    "query_texts": [query],
                    "n_results": min(top_k, self._collection.count() or 1),
                }
                if where:
                    kwargs["where"] = where

                results = self._collection.query(**kwargs)
                output = []
                for i in range(len(results["documents"][0])):
                    output.append({
                        "document": results["documents"][0][i],
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                        "score": 1.0 - (results["distances"][0][i] if results["distances"] else 0),
                        "id": results["ids"][0][i] if results["ids"] else "",
                    })
                return output
            except Exception as exc:
                print(f"[VectorDB] ChromaDB search error: {exc}")
                return []
        else:
            return self._memory_search(query, top_k)

    def _memory_search(self, query: str, top_k: int) -> list[dict]:
        """Simple keyword-based fallback search for in-memory store."""
        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored = []
        for item in self._memory_store:
            doc_lower = item["document"].lower()
            doc_words = set(doc_lower.split())
            overlap = len(query_words & doc_words)
            if overlap > 0:
                score = overlap / max(len(query_words), 1)
                scored.append({
                    "document": item["document"],
                    "metadata": item["metadata"],
                    "score": round(score, 3),
                    "id": item["id"],
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def delete_collection(self):
        """Delete entire collection."""
        if self._backend == "chromadb" and self._client:
            try:
                self._client.delete_collection(self.collection_name)
                self._collection = None
                print(f"[VectorDB] Deleted collection: {self.collection_name}")
            except Exception:
                pass
        else:
            self._memory_store.clear()

    def get_stats(self) -> dict:
        """Return stats about the vector store."""
        return {
            "backend": self._backend,
            "collection": self.collection_name,
            "count": self.count,
            "persist_dir": self.persist_dir,
            "ready": self._collection is not None or self._backend == "memory",
        }


def load_frameworks_into_db(
    domain_config: dict,
    db: VectorDB | None = None,
) -> VectorDB:
    """Load domain frameworks into vector DB for RAG retrieval."""
    from rag_engine.framework_store import get_domain_frameworks, format_framework_for_rag

    if db is None:
        domain_name = domain_config.get("domain", {}).get("name", "unknown")
        db = VectorDB(collection_name=f"frameworks_{domain_name.replace(' ', '_')[:20]}")

    frameworks = get_domain_frameworks(domain_config)
    if not frameworks:
        print("[VectorDB] No frameworks found for domain")
        return db

    docs = []
    metas = []
    ids = []
    for fw in frameworks:
        text = format_framework_for_rag(fw)
        docs.append(text)
        metas.append({
            "framework_id": fw.id,
            "framework_name": fw.name,
            "type": "framework",
        })
        ids.append(f"fw_{fw.id}")

        for i, sit in enumerate(fw.situations):
            docs.append(f"Situation: {sit}\nFramework: {fw.name}\n{text[:200]}")
            metas.append({
                "framework_id": fw.id,
                "situation": sit,
                "type": "situation",
            })
            ids.append(f"fw_{fw.id}_sit_{i}")

    added = db.add_documents(docs, metas, ids)
    print(f"[VectorDB] Loaded {added} documents ({len(frameworks)} frameworks)")
    return db


if __name__ == "__main__":
    db = VectorDB(collection_name="test")
    db.add_documents(
        ["Khách hỏi giá ngay câu đầu tiên", "Khách so sánh với đối thủ", "Khách cần suy nghĩ thêm"],
        [{"type": "sales"}, {"type": "sales"}, {"type": "sales"}],
    )
    results = db.search("khách hỏi giá", top_k=2)
    print(f"\nSearch results ({db.backend}):")
    for r in results:
        print(f"  [{r['score']:.2f}] {r['document'][:60]}")
    print(f"\nStats: {db.get_stats()}")
