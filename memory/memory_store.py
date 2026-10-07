# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/memory_store.py — Phidipus Memory Agent storage layer (v4.3)
═══════════════════════════════════════════════════════════════════════

One local SQLite file (WAL) holds the whole long-term memory:

  memories      facts / preferences / profile / episodes / procedures
                + temporal validity (valid_from / valid_to, superseded_by)
                + float32 unit-length embedding (BLOB) and the model that made it
  memories_fts  FTS5 index, tokenizer "unicode61 remove_diacritics 2"
                → BM25 search that matches Vietnamese with or without accents
  core_blocks   small always-in-context blocks (Letta-style "core memory")
  events        audit log of every memory operation (ADD/UPDATE/INVALIDATE/…)
  meta          key/value settings (schema version, last consolidation, …)

Nothing leaves the machine; the store has no network code.  All methods are
synchronous and thread-safe (one connection guarded by an RLock); the agent
layer calls them through asyncio.to_thread.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

SCHEMA_VERSION = 1

KINDS = ("fact", "preference", "profile", "episode", "procedure")
BLOCK_LIMITS = {"user_profile": 1200, "preferences": 1200, "persona": 800, "environment": 800}


def fold(text: str) -> str:
    """Lower-case, strip diacritics (đ → d), collapse whitespace."""
    t = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "D")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"\s+", " ", t).strip()


def content_hash(text: str) -> str:
    norm = re.sub(r"[^\w ]+", "", fold(text))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


@dataclass
class MemoryRecord:
    content: str
    kind: str = "fact"
    subject: str = "user"
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.8
    source: str = "user"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_accessed_at: float = 0.0
    access_count: int = 0
    valid_from: float = field(default_factory=time.time)
    valid_to: Optional[float] = None
    superseded_by: str = ""
    pinned: bool = False
    embed_model: str = ""
    score: float = 0.0          # search score (not stored)

    @property
    def is_valid(self) -> bool:
        return self.valid_to is None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_valid"] = self.is_valid
        return d


_COLUMNS = ("id", "kind", "content", "subject", "tags", "importance", "confidence", "source",
            "created_at", "updated_at", "last_accessed_at", "access_count", "valid_from",
            "valid_to", "superseded_by", "pinned", "embed_model")


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
        try:  # memory can contain personal data → owner-only file permissions
            self.path.chmod(0o600)
        except OSError:
            pass

    # ── schema ────────────────────────────────────────────────────────
    def _create_schema(self) -> None:
        c = self._conn
        c.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
            id               TEXT UNIQUE NOT NULL,
            kind             TEXT NOT NULL,
            content          TEXT NOT NULL,
            subject          TEXT NOT NULL DEFAULT 'user',
            tags             TEXT NOT NULL DEFAULT '[]',
            importance       REAL NOT NULL DEFAULT 0.5,
            confidence       REAL NOT NULL DEFAULT 0.8,
            source           TEXT NOT NULL DEFAULT 'user',
            created_at       REAL NOT NULL,
            updated_at       REAL NOT NULL,
            last_accessed_at REAL NOT NULL DEFAULT 0,
            access_count     INTEGER NOT NULL DEFAULT 0,
            valid_from       REAL NOT NULL,
            valid_to         REAL,
            superseded_by    TEXT NOT NULL DEFAULT '',
            pinned           INTEGER NOT NULL DEFAULT 0,
            content_hash     TEXT NOT NULL,
            embedding        BLOB,
            embed_model      TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_mem_valid   ON memories(valid_to);
        CREATE INDEX IF NOT EXISTS idx_mem_kind    ON memories(kind, valid_to);
        CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject, valid_to);
        CREATE INDEX IF NOT EXISTS idx_mem_hash    ON memories(content_hash);

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, tags, subject,
            content='memories', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, tags, subject)
            VALUES (new.rowid, new.content, new.tags, new.subject);
        END;
        CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, tags, subject)
            VALUES ('delete', old.rowid, old.content, old.tags, old.subject);
        END;
        CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE OF content, tags, subject ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, tags, subject)
            VALUES ('delete', old.rowid, old.content, old.tags, old.subject);
            INSERT INTO memories_fts(rowid, content, tags, subject)
            VALUES (new.rowid, new.content, new.tags, new.subject);
        END;

        CREATE TABLE IF NOT EXISTS core_blocks (
            name       TEXT PRIMARY KEY,
            content    TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            op         TEXT NOT NULL,
            memory_id  TEXT NOT NULL DEFAULT '',
            detail     TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS procedures (
            signature  TEXT PRIMARY KEY,
            example    TEXT NOT NULL,
            method     TEXT NOT NULL DEFAULT '',
            successes  INTEGER NOT NULL DEFAULT 0,
            failures   INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            memory_id  TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                  (str(SCHEMA_VERSION),))
        c.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"], kind=row["kind"], content=row["content"], subject=row["subject"],
            tags=json.loads(row["tags"] or "[]"), importance=row["importance"],
            confidence=row["confidence"], source=row["source"], created_at=row["created_at"],
            updated_at=row["updated_at"], last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"], valid_from=row["valid_from"],
            valid_to=row["valid_to"], superseded_by=row["superseded_by"],
            pinned=bool(row["pinned"]), embed_model=row["embed_model"],
        )

    @staticmethod
    def _vec_blob(vec: Optional[np.ndarray]) -> Optional[bytes]:
        if vec is None:
            return None
        v = np.asarray(vec, dtype=np.float32).ravel()
        n = float(np.linalg.norm(v))
        if n == 0.0:
            return None
        return (v / n).astype(np.float32).tobytes()

    # ── writes ────────────────────────────────────────────────────────
    def add(self, rec: MemoryRecord, embedding: Optional[np.ndarray] = None) -> MemoryRecord:
        rec.kind = rec.kind if rec.kind in KINDS else "fact"
        rec.importance = max(0.0, min(1.0, float(rec.importance)))
        blob = self._vec_blob(embedding)
        if blob is None:
            rec.embed_model = ""
        with self._lock:
            self._conn.execute(
                "INSERT INTO memories(id, kind, content, subject, tags, importance, confidence, source,"
                " created_at, updated_at, last_accessed_at, access_count, valid_from, valid_to,"
                " superseded_by, pinned, content_hash, embedding, embed_model)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.id, rec.kind, rec.content, rec.subject, json.dumps(rec.tags, ensure_ascii=False),
                 rec.importance, rec.confidence, rec.source, rec.created_at, rec.updated_at,
                 rec.last_accessed_at, rec.access_count, rec.valid_from, rec.valid_to,
                 rec.superseded_by, int(rec.pinned), content_hash(rec.content), blob, rec.embed_model),
            )
            self._log("ADD", rec.id, rec.content[:200])
            self._conn.commit()
        return rec

    def update(self, mem_id: str, *, content: Optional[str] = None,
               embedding: Optional[np.ndarray] = None, embed_model: str = "",
               importance: Optional[float] = None, tags: Optional[list[str]] = None,
               pinned: Optional[bool] = None, detail: str = "") -> bool:
        sets, args = ["updated_at=?"], [time.time()]
        if content is not None:
            sets += ["content=?", "content_hash=?", "embedding=?", "embed_model=?"]
            blob = self._vec_blob(embedding)
            args += [content, content_hash(content), blob, embed_model if blob else ""]
        elif embedding is not None:
            blob = self._vec_blob(embedding)
            sets += ["embedding=?", "embed_model=?"]
            args += [blob, embed_model if blob else ""]
        if importance is not None:
            sets.append("importance=?")
            args.append(max(0.0, min(1.0, float(importance))))
        if tags is not None:
            sets.append("tags=?")
            args.append(json.dumps(tags, ensure_ascii=False))
        if pinned is not None:
            sets.append("pinned=?")
            args.append(int(pinned))
        args.append(mem_id)
        with self._lock:
            cur = self._conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", args)
            if cur.rowcount:
                self._log("UPDATE", mem_id, detail or (content or "")[:200])
            self._conn.commit()
            return bool(cur.rowcount)

    def invalidate(self, mem_id: str, superseded_by: str = "", reason: str = "") -> bool:
        """Temporal invalidation: keep the row as history, stop returning it."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET valid_to=?, superseded_by=?, updated_at=? "
                "WHERE id=? AND valid_to IS NULL", (now, superseded_by, now, mem_id))
            if cur.rowcount:
                self._log("INVALIDATE", mem_id, reason or (f"superseded by {superseded_by}" if superseded_by else ""))
            self._conn.commit()
            return bool(cur.rowcount)

    def delete(self, mem_id: str, reason: str = "") -> bool:
        """Hard delete (user asked to forget)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id=?", (mem_id,))
            if cur.rowcount:
                self._log("DELETE", mem_id, reason)
            self._conn.commit()
            return bool(cur.rowcount)

    def delete_all(self) -> int:
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            self._conn.execute("DELETE FROM memories")
            self._conn.execute("DELETE FROM procedures")
            self._conn.execute("UPDATE core_blocks SET content='', updated_at=?", (time.time(),))
            self._log("DELETE_ALL", "", f"{n} memories")
            self._conn.commit()
            return int(n)

    def touch(self, ids: Iterable[str]) -> None:
        ids = list(ids)
        if not ids:
            return
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "UPDATE memories SET access_count=access_count+1, last_accessed_at=? WHERE id=?",
                [(now, i) for i in ids])
            self._conn.commit()

    # ── reads ─────────────────────────────────────────────────────────
    def get(self, mem_id: str) -> Optional[MemoryRecord]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE id=?", (mem_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def find_valid_by_hash(self, text: str, subject: Optional[str] = None) -> Optional[MemoryRecord]:
        q = "SELECT * FROM memories WHERE content_hash=? AND valid_to IS NULL"
        args: list[Any] = [content_hash(text)]
        if subject:
            q += " AND subject=?"
            args.append(subject)
        with self._lock:
            row = self._conn.execute(q + " LIMIT 1", args).fetchone()
        return self._row_to_record(row) if row else None

    def list(self, kind: Optional[str] = None, subject: Optional[str] = None,
             include_invalid: bool = False, limit: int = 50, offset: int = 0,
             order: str = "updated_at DESC") -> list[MemoryRecord]:
        if order not in ("updated_at DESC", "created_at DESC", "importance DESC", "created_at ASC"):
            order = "updated_at DESC"
        where, args = [], []
        if not include_invalid:
            where.append("valid_to IS NULL")
        if kind:
            where.append("kind=?")
            args.append(kind)
        if subject:
            where.append("subject=?")
            args.append(subject)
        q = "SELECT * FROM memories"
        if where:
            q += " WHERE " + " AND ".join(where)
        q += f" ORDER BY {order} LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count(self, kind: Optional[str] = None, include_invalid: bool = False) -> int:
        q, args = "SELECT COUNT(*) FROM memories", []
        conds = []
        if not include_invalid:
            conds.append("valid_to IS NULL")
        if kind:
            conds.append("kind=?")
            args.append(kind)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        with self._lock:
            return int(self._conn.execute(q, args).fetchone()[0])

    @staticmethod
    def fts_query(text: str) -> str:
        """User text → safe FTS5 MATCH expression (OR of prefix terms)."""
        toks = [t for t in re.findall(r"[0-9a-z]+", fold(text)) if len(t) >= 2][:24]
        if not toks:
            return ""
        return " OR ".join(f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in dict.fromkeys(toks))

    def fts_search(self, text: str, limit: int = 50, include_invalid: bool = False,
                   kinds: Optional[Iterable[str]] = None) -> list[tuple[MemoryRecord, float]]:
        """BM25 search. Returns (record, bm25) — lower bm25 = better match."""
        match = self.fts_query(text)
        if not match:
            return []
        q = ("SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts "
             "JOIN memories m ON m.rowid = memories_fts.rowid WHERE memories_fts MATCH ?")
        args: list[Any] = [match]
        if not include_invalid:
            q += " AND m.valid_to IS NULL"
        kinds = list(kinds or [])
        if kinds:
            q += f" AND m.kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        q += " ORDER BY rank LIMIT ?"
        args.append(int(limit))
        with self._lock:
            try:
                rows = self._conn.execute(q, args).fetchall()
            except sqlite3.OperationalError:
                return []
        return [(self._row_to_record(r), float(r["rank"])) for r in rows]

    def vectors(self, include_invalid: bool = False, kinds: Optional[Iterable[str]] = None,
                ids: Optional[Iterable[str]] = None) -> tuple[list[str], list[str], Optional[np.ndarray]]:
        """(ids, embed_models, matrix[n, d]) of stored unit vectors (same dimension only)."""
        q, args, conds = "SELECT id, embed_model, embedding FROM memories", [], ["embedding IS NOT NULL"]
        if not include_invalid:
            conds.append("valid_to IS NULL")
        kinds = list(kinds or [])
        if kinds:
            conds.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += kinds
        idl = list(ids or [])
        if idl:
            conds.append(f"id IN ({','.join('?' * len(idl))})")
            args += idl
        q += " WHERE " + " AND ".join(conds)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        if not rows:
            return [], [], None
        vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
        dim = max(set(v.shape[0] for v in vecs), key=[v.shape[0] for v in vecs].count)
        keep = [i for i, v in enumerate(vecs) if v.shape[0] == dim]
        return ([rows[i]["id"] for i in keep], [rows[i]["embed_model"] for i in keep],
                np.vstack([vecs[i] for i in keep]))

    def missing_embeddings(self, model: str, limit: int = 64) -> list[MemoryRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE valid_to IS NULL AND (embedding IS NULL OR embed_model != ?)"
                " ORDER BY importance DESC LIMIT ?", (model, int(limit))).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ── procedures (method that worked for a kind of goal) ────────────
    def get_procedure(self, signature: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM procedures WHERE signature=?", (signature,)).fetchone()
        return dict(row) if row else None

    def upsert_procedure(self, signature: str, example: str, method: str, success: bool,
                         error: str = "", memory_id: str = "") -> dict:
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT * FROM procedures WHERE signature=?", (signature,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO procedures(signature, example, method, successes, failures, last_error,"
                    " memory_id, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (signature, example, method if success else "", int(success), int(not success),
                     "" if success else error[:200], memory_id, now))
            else:
                self._conn.execute(
                    "UPDATE procedures SET example=?, method=CASE WHEN ? THEN ? ELSE method END,"
                    " successes=successes+?, failures=failures+?, last_error=CASE WHEN ? THEN last_error ELSE ? END,"
                    " memory_id=CASE WHEN ?='' THEN memory_id ELSE ? END, updated_at=? WHERE signature=?",
                    (example, int(success), method, int(success), int(not success), int(success),
                     error[:200], memory_id, memory_id, now, signature))
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM procedures WHERE signature=?", (signature,)).fetchone())

    def set_procedure_memory(self, signature: str, memory_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE procedures SET memory_id=? WHERE signature=?", (memory_id, signature))
            self._conn.commit()

    # ── core blocks ───────────────────────────────────────────────────
    def get_block(self, name: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT content FROM core_blocks WHERE name=?", (name,)).fetchone()
        return row["content"] if row else ""

    def set_block(self, name: str, content: str) -> str:
        limit = BLOCK_LIMITS.get(name, 800)
        content = (content or "").strip()[:limit]
        with self._lock:
            self._conn.execute(
                "INSERT INTO core_blocks(name, content, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                (name, content, time.time()))
            self._log("BLOCK", name, content[:200])
            self._conn.commit()
        return content

    def list_blocks(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT name, content FROM core_blocks ORDER BY name").fetchall()
        return {r["name"]: r["content"] for r in rows}

    # ── events / meta ─────────────────────────────────────────────────
    def _log(self, op: str, memory_id: str, detail: str) -> None:
        self._conn.execute("INSERT INTO events(ts, op, memory_id, detail) VALUES (?,?,?,?)",
                           (time.time(), op, memory_id, detail[:500]))

    def log_event(self, op: str, memory_id: str = "", detail: str = "") -> None:
        with self._lock:
            self._log(op, memory_id, detail)
            self._conn.commit()

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO meta(key, value) VALUES (?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self._conn.commit()

    # ── maintenance ───────────────────────────────────────────────────
    def prune_episodes(self, ttl_days: float, max_episodes: int) -> int:
        """Delete old, never-recalled, unpinned episodes; cap the episode count."""
        cutoff = time.time() - ttl_days * 86400
        removed = 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memories WHERE kind='episode' AND pinned=0 AND access_count=0 "
                "AND created_at < ?", (cutoff,))
            removed += cur.rowcount
            n = self._conn.execute("SELECT COUNT(*) FROM memories WHERE kind='episode'").fetchone()[0]
            if n > max_episodes:
                cur = self._conn.execute(
                    "DELETE FROM memories WHERE rowid IN (SELECT rowid FROM memories WHERE kind='episode'"
                    " AND pinned=0 ORDER BY importance ASC, created_at ASC LIMIT ?)", (n - max_episodes,))
                removed += cur.rowcount
            # history rows (invalidated) older than a year are dropped as well
            cur = self._conn.execute(
                "DELETE FROM memories WHERE valid_to IS NOT NULL AND valid_to < ?",
                (time.time() - 365 * 86400,))
            removed += cur.rowcount
            if removed:
                self._log("PRUNE", "", f"{removed} rows")
            self._conn.commit()
        return removed

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_kind = {r[0]: r[1] for r in self._conn.execute(
                "SELECT kind, COUNT(*) FROM memories WHERE valid_to IS NULL GROUP BY kind")}
            history = self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE valid_to IS NOT NULL").fetchone()[0]
            embedded = self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE valid_to IS NULL AND embedding IS NOT NULL").fetchone()[0]
            procs = self._conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"by_kind": by_kind, "total": sum(by_kind.values()), "history": history,
                "embedded": embedded, "procedures": procs, "db_bytes": size, "path": str(self.path)}

    def export_all(self, include_invalid: bool = True) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "exported_at": time.time(),
            "memories": [r.to_dict() for r in self.list(include_invalid=include_invalid, limit=100000)],
            "core_blocks": self.list_blocks(),
        }
