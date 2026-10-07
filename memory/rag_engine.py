# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/rag_engine.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

RAG Memory Engine — Agent nhớ lâu dài qua ChromaDB vector database.

Giải quyết vấn đề gốc: "Agent ngáo lần đầu"
  → Trước: mỗi task chạy từ zero, không nhớ gì
  → Sau:   trước khi chạy, agent TÌM KIẾM task tương tự đã làm trước đó
           → lấy context, steps, kết quả → chạy chính xác hơn

3 collections trong ChromaDB:
  1. task_history    — Mọi task đã chạy (goal, steps, result, duration)
  2. knowledge_base  — Kiến thức tích lũy (skill patterns, website info, user prefs)
  3. conversations   — Hội thoại Telegram (context, follow-up questions)

Flow:
  User gõ: "lấy nội dung vnexpress"
  ① RAG search "lấy nội dung vnexpress" → tìm 3 task tương tự trước đó
  ② Context: "Lần trước chạy ChromeSkills.get_page_text, thành công, 3.2s"
  ③ SmartRouter dùng context → route chính xác hơn
  ④ Sau khi chạy → lưu result vào ChromaDB → lần sau càng giỏi hơn

Dependencies:
  pip install chromadb   # persistent vector DB
  Ollama nomic-embed-text đã có sẵn
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;32m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════

_BASE_DIR = Path(__file__).parent.parent
_CHROMA_DIR = _BASE_DIR / "data" / "chromadb"
_CHROMA_DIR.mkdir(parents=True, exist_ok=True)

EMBED_MODEL = "nomic-embed-text"
EMBED_URL = "http://127.0.0.1:11434/api/embeddings"
EMBED_DIM = 768  # nomic-embed-text output dimension


# ══════════════════════════════════════════════════════════════
# Embedding via Ollama (reuse existing nomic-embed-text)
# ══════════════════════════════════════════════════════════════

async def embed_text(text: str, timeout: float = 5.0) -> list[float] | None:
    """Embed text using nomic-embed-text via Ollama API."""
    if not text or not text.strip():
        return None
    try:
        payload = json.dumps({
            "model": EMBED_MODEL,
            "prompt": text[:512],
        }).encode()

        def _call():
            req = urllib.request.Request(
                EMBED_URL, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return data.get("embedding", [])

        vec = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _call),
            timeout=timeout + 1,
        )
        return vec if vec and len(vec) == EMBED_DIM else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# RAG Engine — ChromaDB wrapper
# ══════════════════════════════════════════════════════════════

class RAGEngine:
    """
    Persistent RAG Memory using ChromaDB.

    3 collections:
      task_history   — mọi task đã chạy
      knowledge_base — kiến thức tích lũy
      conversations  — hội thoại context
    """

    def __init__(self, persist_dir: str = ""):
        self._persist_dir = persist_dir or str(_CHROMA_DIR)
        self._client = None
        self._collections = {}
        self._ready = False
        self._use_chromadb = False
        self._fallback_data: dict[str, list[dict]] = {
            "task_history": [],
            "knowledge_base": [],
            "conversations": [],
        }
        self._fallback_file = Path(self._persist_dir) / "rag_fallback.json"

    async def initialize(self) -> bool:
        """Initialize ChromaDB. Falls back to JSON if ChromaDB unavailable."""
        # Try ChromaDB
        try:
            import chromadb
            from chromadb.config import Settings

            self._client = chromadb.PersistentClient(
                path=self._persist_dir,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                ),
            )

            # Create/get collections
            for name in ("task_history", "knowledge_base", "conversations"):
                self._collections[name] = self._client.get_or_create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine"},
                )

            self._use_chromadb = True
            self._ready = True
            counts = {n: c.count() for n, c in self._collections.items()}
            _vlog("🧠", f"RAG Engine ready (ChromaDB) — {counts}")
            return True

        except ImportError:
            _vlog("⚠️", "chromadb not installed — using JSON fallback")
            _vlog("💡", "Install for full RAG: pip install chromadb --break-system-packages")
        except Exception as exc:
            _vlog("⚠️", f"ChromaDB init failed: {str(exc)[:60]} — JSON fallback")

        # Fallback: JSON file-based storage
        self._load_fallback()
        self._ready = True
        total = sum(len(v) for v in self._fallback_data.values())
        _vlog("🧠", f"RAG Engine ready (JSON fallback) — {total} records")
        return True

    # ══════════════════════════════════════════════════════════
    # STORE — Add records to memory
    # ══════════════════════════════════════════════════════════

    async def store_task(self, goal: str, result: dict, steps: list[str] = None,
                         duration_s: float = 0, metadata: dict = None) -> str:
        """
        Store completed task in memory.
        Called by agent_loop after each task finishes.
        """
        doc_id = f"task_{int(time.time())}_{hash(goal) % 10000}"
        text = self._build_task_text(goal, result, steps)
        meta = {
            "type": "task",
            "goal": goal[:200],
            "success": result.get("success", False),
            "duration_s": round(duration_s, 1),
            "timestamp": time.time(),
            "steps_count": len(steps) if steps else 0,
            "lane": result.get("lane", ""),
            "skill": result.get("skill", ""),
            **(metadata or {}),
        }

        await self._add(
            collection="task_history",
            doc_id=doc_id,
            text=text,
            metadata=meta,
        )

        _vlog("🧠", f"Stored task: '{goal[:50]}' ({'✅' if meta['success'] else '❌'})")
        return doc_id

    async def store_knowledge(self, topic: str, content: str,
                               source: str = "", metadata: dict = None) -> str:
        """
        Store knowledge (skill pattern, website info, user preference).
        """
        doc_id = f"kb_{int(time.time())}_{hash(topic) % 10000}"
        text = f"Topic: {topic}\n{content}"
        meta = {
            "type": "knowledge",
            "topic": topic[:200],
            "source": source,
            "timestamp": time.time(),
            **(metadata or {}),
        }

        await self._add(collection="knowledge_base", doc_id=doc_id,
                        text=text, metadata=meta)
        return doc_id

    async def store_conversation(self, user_msg: str, agent_reply: str,
                                  metadata: dict = None) -> str:
        """
        Store conversation turn for context continuity.
        """
        doc_id = f"conv_{int(time.time())}_{hash(user_msg) % 10000}"
        text = f"User: {user_msg}\nAgent: {agent_reply}"
        meta = {
            "type": "conversation",
            "user_msg": user_msg[:200],
            "timestamp": time.time(),
            **(metadata or {}),
        }

        await self._add(collection="conversations", doc_id=doc_id,
                        text=text, metadata=meta)
        return doc_id

    # ══════════════════════════════════════════════════════════
    # RETRIEVE — Search similar records
    # ══════════════════════════════════════════════════════════

    async def recall_similar_tasks(self, goal: str, k: int = 5,
                                    success_only: bool = False) -> list[dict]:
        """
        Find similar tasks from history.
        This is the KEY function — called before routing to provide context.

        Returns list of: {goal, success, duration_s, steps, skill, score}
        """
        results = await self._search("task_history", goal, k=k)

        if success_only:
            results = [r for r in results if r.get("metadata", {}).get("success")]

        return [
            {
                "goal": r.get("metadata", {}).get("goal", ""),
                "success": r.get("metadata", {}).get("success", False),
                "duration_s": r.get("metadata", {}).get("duration_s", 0),
                "skill": r.get("metadata", {}).get("skill", ""),
                "lane": r.get("metadata", {}).get("lane", ""),
                "steps_count": r.get("metadata", {}).get("steps_count", 0),
                "score": r.get("score", 0),
                "text": r.get("text", "")[:300],
            }
            for r in results
        ]

    async def recall_knowledge(self, query: str, k: int = 3) -> list[dict]:
        """Find relevant knowledge for a query."""
        results = await self._search("knowledge_base", query, k=k)
        return [
            {
                "topic": r.get("metadata", {}).get("topic", ""),
                "text": r.get("text", "")[:500],
                "source": r.get("metadata", {}).get("source", ""),
                "score": r.get("score", 0),
            }
            for r in results
        ]

    async def recall_conversations(self, query: str, k: int = 3) -> list[dict]:
        """Find relevant past conversations."""
        results = await self._search("conversations", query, k=k)
        return [
            {
                "user_msg": r.get("metadata", {}).get("user_msg", ""),
                "text": r.get("text", "")[:400],
                "score": r.get("score", 0),
            }
            for r in results
        ]

    async def build_context(self, goal: str) -> str:
        """
        Build rich context string for LLM from all memory sources.
        This is injected into the LLM prompt before task execution.
        """
        parts = []

        # Similar past tasks
        tasks = await self.recall_similar_tasks(goal, k=3, success_only=True)
        if tasks:
            parts.append("=== KINH NGHIỆM TỪ TASKS TRƯỚC ===")
            for t in tasks:
                icon = "✅" if t["success"] else "❌"
                parts.append(
                    f"{icon} \"{t['goal']}\" → {t['skill'] or t['lane']} "
                    f"({t['duration_s']}s, {t['steps_count']} bước)"
                )

        # Relevant knowledge
        knowledge = await self.recall_knowledge(goal, k=2)
        if knowledge:
            parts.append("\n=== KIẾN THỨC LIÊN QUAN ===")
            for k_item in knowledge:
                parts.append(f"• {k_item['topic']}: {k_item['text'][:200]}")

        # Recent conversations
        convs = await self.recall_conversations(goal, k=2)
        if convs:
            parts.append("\n=== HỘI THOẠI GẦN ĐÂY ===")
            for c in convs:
                parts.append(f"User: {c['user_msg'][:100]}")

        context = "\n".join(parts)
        if context:
            _vlog("🧠", f"RAG context: {len(parts)} items for '{goal[:40]}'")
        return context

    # ══════════════════════════════════════════════════════════
    # LEARN — Auto-extract knowledge from task results
    # ══════════════════════════════════════════════════════════

    async def learn_from_task(self, goal: str, result: dict,
                               steps: list[str] = None):
        """
        Auto-extract and store knowledge from completed task.
        Called after each successful task to build knowledge base.
        """
        if not result.get("success"):
            # Store failure pattern too — to avoid same mistake
            await self.store_knowledge(
                topic=f"failure_pattern: {goal[:100]}",
                content=f"Task failed: {result.get('error', 'unknown')}. "
                        f"Avoid this approach next time.",
                source="auto_learn",
                metadata={"success": False},
            )
            return

        # Extract successful patterns
        skill = result.get("skill", "")
        lane = result.get("lane", "")
        duration = result.get("duration_s", 0)

        await self.store_knowledge(
            topic=f"successful_pattern: {goal[:100]}",
            content=(
                f"Goal: {goal}\n"
                f"Best approach: {skill or lane}\n"
                f"Duration: {duration}s\n"
                f"Steps: {', '.join(steps[:5]) if steps else 'N/A'}\n"
                f"→ This approach works well. Use it again for similar tasks."
            ),
            source="auto_learn",
            metadata={"success": True, "skill": skill, "lane": lane},
        )

    # ══════════════════════════════════════════════════════════
    # STATS
    # ══════════════════════════════════════════════════════════

    def stats(self) -> dict:
        """Get memory statistics."""
        if self._use_chromadb:
            return {
                "backend": "chromadb",
                "ready": self._ready,
                "collections": {
                    n: c.count() for n, c in self._collections.items()
                },
                "persist_dir": self._persist_dir,
            }
        else:
            return {
                "backend": "json_fallback",
                "ready": self._ready,
                "collections": {
                    n: len(v) for n, v in self._fallback_data.items()
                },
            }

    # ══════════════════════════════════════════════════════════
    # Internal: ChromaDB or JSON fallback
    # ══════════════════════════════════════════════════════════

    async def _add(self, collection: str, doc_id: str, text: str,
                   metadata: dict):
        """Add document to collection."""
        if self._use_chromadb:
            # Embed and add to ChromaDB
            vec = await embed_text(text)
            coll = self._collections.get(collection)
            if coll and vec:
                try:
                    coll.add(
                        ids=[doc_id],
                        documents=[text[:5000]],
                        metadatas=[self._clean_metadata(metadata)],
                        embeddings=[vec],
                    )
                except Exception as exc:
                    # Duplicate ID — update instead
                    try:
                        coll.update(
                            ids=[doc_id],
                            documents=[text[:5000]],
                            metadatas=[self._clean_metadata(metadata)],
                            embeddings=[vec],
                        )
                    except Exception:
                        _vlog("⚠️", f"ChromaDB add error: {str(exc)[:50]}")
        else:
            # JSON fallback
            self._fallback_data.setdefault(collection, []).append({
                "id": doc_id,
                "text": text[:5000],
                "metadata": metadata,
                "timestamp": time.time(),
            })
            # Cap at 1000 per collection
            if len(self._fallback_data[collection]) > 1000:
                self._fallback_data[collection] = self._fallback_data[collection][-1000:]
            self._save_fallback()

    async def _search(self, collection: str, query: str, k: int = 5) -> list[dict]:
        """Search collection by semantic similarity."""
        if self._use_chromadb:
            vec = await embed_text(query)
            coll = self._collections.get(collection)
            if not coll or not vec:
                return []
            try:
                results = coll.query(
                    query_embeddings=[vec],
                    n_results=min(k, coll.count() or 1),
                    include=["documents", "metadatas", "distances"],
                )
                items = []
                if results and results.get("ids"):
                    for i, doc_id in enumerate(results["ids"][0]):
                        distance = results["distances"][0][i] if results.get("distances") else 0
                        score = max(0, 1.0 - distance)  # cosine distance → similarity
                        items.append({
                            "id": doc_id,
                            "text": results["documents"][0][i] if results.get("documents") else "",
                            "metadata": results["metadatas"][0][i] if results.get("metadatas") else {},
                            "score": round(score, 3),
                        })
                return items
            except Exception as exc:
                _vlog("⚠️", f"ChromaDB search error: {str(exc)[:50]}")
                return []
        else:
            # JSON fallback: keyword matching (no vector search)
            records = self._fallback_data.get(collection, [])
            query_words = set(query.lower().split())
            scored = []
            for rec in records:
                text = rec.get("text", "").lower()
                meta_goal = rec.get("metadata", {}).get("goal", "").lower()
                combined = text + " " + meta_goal
                overlap = sum(1 for w in query_words if w in combined)
                if overlap > 0:
                    score = overlap / max(len(query_words), 1)
                    scored.append({**rec, "score": round(score, 3)})
            scored.sort(key=lambda x: x["score"], reverse=True)
            return scored[:k]

    def _clean_metadata(self, meta: dict) -> dict:
        """ChromaDB only accepts str/int/float/bool metadata values."""
        clean = {}
        for k, v in meta.items():
            if isinstance(v, (str, int, float, bool)):
                clean[k] = v
            elif isinstance(v, list):
                clean[k] = json.dumps(v)
            elif v is None:
                clean[k] = ""
            else:
                clean[k] = str(v)[:200]
        return clean

    def _build_task_text(self, goal: str, result: dict, steps: list[str] = None) -> str:
        """Build searchable text document from task result."""
        parts = [
            f"Goal: {goal}",
            f"Success: {result.get('success', False)}",
            f"Skill: {result.get('skill', '')}",
            f"Lane: {result.get('lane', '')}",
            f"Error: {result.get('error', '')}" if result.get("error") else "",
        ]
        if steps:
            parts.append(f"Steps: {'; '.join(steps[:10])}")
        output = result.get("output", result.get("result", ""))
        if output:
            parts.append(f"Output: {str(output)[:500]}")
        return "\n".join(p for p in parts if p)

    def _load_fallback(self):
        """Load JSON fallback data."""
        try:
            if self._fallback_file.exists():
                self._fallback_data = json.loads(
                    self._fallback_file.read_text(encoding="utf-8")
                )
        except Exception:
            pass

    def _save_fallback(self):
        """Save JSON fallback data."""
        try:
            self._fallback_file.write_text(
                json.dumps(self._fallback_data, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════════════════════

_engine: Optional[RAGEngine] = None

async def get_rag_engine() -> RAGEngine:
    """Get or create RAG engine singleton."""
    global _engine
    if _engine is None:
        _engine = RAGEngine()
        await _engine.initialize()
    return _engine

def get_rag_engine_sync() -> RAGEngine | None:
    """Get RAG engine if already initialized (non-async)."""
    return _engine
