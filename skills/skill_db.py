# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/skill_db.py — Phidipus Skill Registry DB v9.20
══════════════════════════════════════════════════════

Anti Skill-Explosion: dedup, metadata, smart routing, versioning, rollback.

Thiết kế thực tế (PRAGMATIC):
  - Storage: SQLite (data/skills/registry.db) — đủ cho < 5000 skills
  - Fallback: JSON (data/skills/registry.json) — nếu sqlite3 lỗi
  - Similarity: TF-IDF cosine (stdlib only — không cần sklearn)
  - Dedup: similarity > 0.85 → merge vào skill cũ
  - Max 200 skills: vượt → auto-consolidation
  - Versioning: v1 → v2 → v3, rollback nếu cần

Router score = 0.5 × text_sim + 0.3 × reliability + 0.2 × recency
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Generator


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class SkillMeta:
    id: str
    name: str
    description: str
    code_hash: str
    capability_tags: list[str] = field(default_factory=list)
    trigger_patterns: list[str] = field(default_factory=list)
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    version: int = 1
    reliability_score: float = 1.0
    usage_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    avg_runtime_ms: float = 0.0
    source: str = "forge"          # forge|manual|evolved|compiled
    is_active: bool = True
    code_path: str = ""
    parent_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    last_success_at: float = 0.0
    provider_history: list[str] = field(default_factory=list)  # Providers that generated/ran this skill

    @property
    def success_rate(self) -> float:
        return self.success_count / self.usage_count if self.usage_count else 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SkillMeta":
        for lf in ("capability_tags", "trigger_patterns"):
            if isinstance(d.get(lf), str):
                try:
                    d[lf] = json.loads(d[lf])
                except Exception:
                    d[lf] = []
        for df in ("input_schema", "output_schema"):
            if isinstance(d.get(df), str):
                try:
                    d[df] = json.loads(d[df])
                except Exception:
                    d[df] = {}
        if "is_active" in d:
            d["is_active"] = bool(d["is_active"])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class SearchResult:
    skill: SkillMeta
    score: float
    match_reason: str = ""


# ══════════════════════════════════════════════════════════════════
# Similarity engine: Embedding (Ollama) primary, TF-IDF fallback
# ══════════════════════════════════════════════════════════════════

# ── TF-IDF fallback (stdlib only, used when Ollama unavailable) ──

def _tokenize(text: str) -> list[str]:
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return [t for t in text.split() if len(t) >= 2]


def _tf_idf_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0

    def tf(tokens: list[str]) -> dict[str, float]:
        freq: dict[str, int] = {}
        for t in tokens:
            freq[t] = freq.get(t, 0) + 1
        n = len(tokens)
        return {t: c / n for t, c in freq.items()}

    fa, fb = tf(ta), tf(tb)
    vocab = set(fa) | set(fb)
    dot = sum(fa.get(t, 0.0) * fb.get(t, 0.0) for t in vocab)
    mag_a = math.sqrt(sum(v * v for v in fa.values()))
    mag_b = math.sqrt(sum(v * v for v in fb.values()))
    if not mag_a or not mag_b:
        return 0.0
    return min(1.0, dot / (mag_a * mag_b))


# ── Embedding engine (Ollama /api/embeddings) ───────────────────

import urllib.request
import urllib.error
import threading

_OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embeddings"
_EMBED_MODEL = "nomic-embed-text"  # 137MB, fast, good quality
_EMBED_TIMEOUT = 5  # seconds — embedding should be fast

# Thread-safe embedding cache: text_hash → vector
_embed_cache: dict[str, list[float]] = {}
_embed_cache_lock = threading.Lock()
_embed_cache_max = 500

# Track if Ollama embedding is available (tested once, cached)
_embed_available: bool | None = None  # None = not tested yet


def _test_embedding_available() -> bool:
    """Test if Ollama has the embedding model loaded."""
    global _embed_available
    if _embed_available is not None:
        return _embed_available
    try:
        data = json.dumps({"model": _EMBED_MODEL, "prompt": "test"}).encode()
        req = urllib.request.Request(
            _OLLAMA_EMBED_URL, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read().decode())
            if result.get("embedding") and len(result["embedding"]) > 10:
                _embed_available = True
                _vlog("🧮", f"Embedding: {_EMBED_MODEL} sẵn sàng "
                      f"(dim={len(result['embedding'])})")
                return True
    except Exception:
        pass
    _embed_available = False
    _vlog("⚠️", f"Embedding: {_EMBED_MODEL} không sẵn sàng → dùng TF-IDF fallback")
    return False


def _get_embedding(text: str) -> list[float] | None:
    """Get embedding vector for text from Ollama."""
    if not _test_embedding_available():
        return None

    # Check cache
    text_key = hashlib.md5(text.encode()).hexdigest()
    with _embed_cache_lock:
        if text_key in _embed_cache:
            return _embed_cache[text_key]

    try:
        data = json.dumps({
            "model": _EMBED_MODEL,
            "prompt": text[:512],  # Truncate to avoid slow processing
        }).encode()
        req = urllib.request.Request(
            _OLLAMA_EMBED_URL, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
            embedding = result.get("embedding")
            if embedding and isinstance(embedding, list) and len(embedding) > 10:
                # Cache it
                with _embed_cache_lock:
                    if len(_embed_cache) >= _embed_cache_max:
                        # Evict oldest (first inserted)
                        oldest = next(iter(_embed_cache))
                        del _embed_cache[oldest]
                    _embed_cache[text_key] = embedding
                return embedding
    except Exception:
        pass
    return None


def _cosine_similarity_vec(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if not mag_a or not mag_b:
        return 0.0
    return max(0.0, min(1.0, dot / (mag_a * mag_b)))


def smart_similarity(a: str, b: str) -> float:
    """
    v9.21: Best-effort similarity — embedding if available, TF-IDF fallback.

    Embedding: semantic match (understands "tồn kho" ≈ "inventory")
    TF-IDF:    keyword match (only matches exact words)
    """
    # Try embedding first
    vec_a = _get_embedding(a)
    vec_b = _get_embedding(b)
    if vec_a and vec_b:
        return _cosine_similarity_vec(vec_a, vec_b)

    # Fallback to TF-IDF
    return _tf_idf_similarity(a, b)


# ══════════════════════════════════════════════════════════════════
# SQLite DDL
# ══════════════════════════════════════════════════════════════════

_DDL = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    code_hash TEXT NOT NULL, capability_tags TEXT DEFAULT '[]',
    trigger_patterns TEXT DEFAULT '[]', input_schema TEXT DEFAULT '{}',
    output_schema TEXT DEFAULT '{}', version INTEGER DEFAULT 1,
    reliability_score REAL DEFAULT 1.0, usage_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0,
    avg_runtime_ms REAL DEFAULT 0.0, source TEXT DEFAULT 'forge',
    is_active INTEGER DEFAULT 1, code_path TEXT DEFAULT '',
    parent_id TEXT DEFAULT '', created_at REAL NOT NULL,
    last_used_at REAL DEFAULT 0.0, last_success_at REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_active ON skills(is_active);
CREATE INDEX IF NOT EXISTS idx_reliability ON skills(reliability_score DESC);
CREATE TABLE IF NOT EXISTS skill_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL, event_type TEXT NOT NULL,
    detail TEXT DEFAULT '', recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_skill ON skill_history(skill_id);
"""


# ══════════════════════════════════════════════════════════════════
# SkillRegistryDB
# ══════════════════════════════════════════════════════════════════

class SkillRegistryDB:
    """
    Skill Registry với SQLite backend + JSON fallback.

    Anti-explosion:
      - Dedup: similarity > 0.85 → merge
      - Max 200 active skills → auto-consolidate
      - Versioning + rollback
      - Routing: composite score

    Usage::
        db = SkillRegistryDB()
        meta = SkillMeta(id="...", name="...", description="...", code_hash="...")
        db.register(meta)
        results = db.search("tổng hợp giá bóng đèn", top_k=3)
        db.record_usage(skill_id, success=True, runtime_ms=1200)
    """

    MAX_ACTIVE_SKILLS = 200
    DEDUP_THRESHOLD = 0.85
    MIN_RELIABILITY = 0.3

    def __init__(self, db_dir: str = "data/skills") -> None:
        self._dir = Path(db_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / "registry.db"
        self._json_path = self._dir / "registry.json"
        self._use_sqlite = True
        self._json_cache: list[dict] = []

        try:
            self._init_db()
        except Exception as exc:
            _vlog("⚠️", f"SQLite unavailable ({str(exc)[:40]}), dùng JSON fallback")
            self._use_sqlite = False
            self._json_cache = self._load_json()

    # ─ DB init ────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_DDL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ─ Register ───────────────────────────────────────────────────

    def register(self, meta: SkillMeta) -> SkillMeta:
        """
        Register skill. Returns existing if duplicate detected.
        Dedup: exact hash → text similarity > 0.85 → new.
        """
        # 1. Exact code hash
        existing = self._find_by_hash(meta.code_hash)
        if existing:
            _vlog("♻️", f"Dedup exact: '{meta.name}' = '{existing.name}'")
            self._record_history(existing.id, "dedup_exact")
            return existing

        # 2. Text similarity (v9.21: embedding → TF-IDF fallback)
        active = self._load_all_active()
        if active:
            best_sim, best_match = 0.0, None
            q = f"{meta.name} {meta.description}"
            for sk in active:
                sim = smart_similarity(q, f"{sk.name} {sk.description}")
                if sim > best_sim:
                    best_sim, best_match = sim, sk
            if best_sim >= self.DEDUP_THRESHOLD and best_match:
                _vlog("♻️", f"Dedup sim={best_sim:.2f}: '{meta.name}' → '{best_match.name}'")
                merged = self._bump_version(best_match, meta)
                self._record_history(merged.id, "dedup_merge", f"sim={best_sim:.2f}")
                return merged

        # 3. Insert new
        self._insert(meta)
        self._record_history(meta.id, "registered")
        _vlog("📚", f"Skill: '{meta.name}' v{meta.version} [{meta.id[:8]}]")

        if self._count_active() > self.MAX_ACTIVE_SKILLS:
            _vlog("🔧", f"Skills > {self.MAX_ACTIVE_SKILLS} → consolidate")
            self._auto_consolidate()

        return meta

    # ─ Search ─────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """
        Top-K skills cho goal dùng composite score.
        v9.21: Embedding similarity (Ollama) → TF-IDF fallback
        score = 0.5×text_sim + 0.3×reliability + 0.2×recency
        """
        active = self._load_all_active()
        if not active:
            return []

        now = time.time()
        scored: list[SearchResult] = []
        _using_embed = _test_embedding_available()

        for sk in active:
            target = " ".join([
                sk.name, sk.description,
                " ".join(sk.capability_tags),
                " ".join(sk.trigger_patterns[:3]),
            ])
            text_sim = smart_similarity(query, target)
            reliability = sk.reliability_score
            days = (now - sk.last_used_at) / 86400 if sk.last_used_at else 30
            recency = max(0.0, 1.0 - days / 30)
            score = 0.5 * text_sim + 0.3 * reliability + 0.2 * recency
            if score > 0.05:
                _mode = "emb" if _using_embed else "tfidf"
                scored.append(SearchResult(
                    skill=sk, score=score,
                    match_reason=f"[{_mode}] sim={text_sim:.2f} rel={reliability:.2f}",
                ))

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    def find_by_trigger(self, goal: str) -> SkillMeta | None:
        """Fast regex trigger check — called first."""
        for sk in self._load_all_active():
            for pat in sk.trigger_patterns:
                try:
                    if re.search(pat, goal, re.IGNORECASE):
                        return sk
                except re.error:
                    continue
        return None

    # ─ Usage ──────────────────────────────────────────────────────

    def record_usage(self, skill_id: str, success: bool, runtime_ms: float = 0.0) -> None:
        """Update stats after execution. Auto-deactivate if reliability < MIN."""
        if self._use_sqlite:
            try:
                with self._connect() as conn:
                    conn.execute("""
                        UPDATE skills SET
                            usage_count = usage_count + 1,
                            success_count = success_count + ?,
                            fail_count = fail_count + ?,
                            avg_runtime_ms = (avg_runtime_ms * usage_count + ?) / (usage_count + 1),
                            last_used_at = ?,
                            last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                            reliability_score = CAST(success_count + ? AS REAL) / (usage_count + 1)
                        WHERE id = ?
                    """, (
                        1 if success else 0, 0 if success else 1,
                        runtime_ms, time.time(),
                        success, time.time(),
                        1 if success else 0,
                        skill_id,
                    ))
            except Exception as exc:
                _vlog("⚠️", f"record_usage error: {str(exc)[:60]}")
                return
        else:
            self._json_update_usage(skill_id, success, runtime_ms)

        self._record_history(skill_id, "success" if success else "fail",
                             f"ms={runtime_ms:.0f}")

        sk = self.get(skill_id)
        if sk and sk.usage_count >= 5 and sk.reliability_score < self.MIN_RELIABILITY:
            _vlog("🪦", f"Deactivate low-reliability: '{sk.name}' ({sk.reliability_score:.2f})")
            self._deactivate(skill_id)

    # ─ Versioning ─────────────────────────────────────────────────

    def evolve(self, skill_id: str, new_meta: SkillMeta) -> SkillMeta:
        """Create v+1 (from evolution engine). Deactivates old version."""
        old = self.get(skill_id)
        if not old:
            return self.register(new_meta)
        new_meta.version = old.version + 1
        new_meta.parent_id = skill_id
        new_meta.source = "evolved"
        self._deactivate(skill_id)
        self._insert(new_meta)
        self._record_history(new_meta.id, "evolved", f"from={skill_id[:8]}")
        _vlog("🧬", f"'{old.name}' v{old.version} → v{new_meta.version}")
        return new_meta

    def rollback(self, skill_id: str) -> SkillMeta | None:
        """Rollback to parent version."""
        current = self.get(skill_id)
        if not current or not current.parent_id:
            _vlog("⚠️", "No parent version to rollback to")
            return None
        self._deactivate(skill_id)
        self._reactivate(current.parent_id)
        self._record_history(current.parent_id, "rollback", f"from={skill_id[:8]}")
        parent = self.get(current.parent_id)
        if parent:
            _vlog("⏪", f"Rolled back to '{parent.name}' v{parent.version}")
        return parent

    # ─ Get / list ─────────────────────────────────────────────────

    def get(self, skill_id: str) -> SkillMeta | None:
        if self._use_sqlite:
            try:
                with sqlite3.connect(str(self._db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM skills WHERE id=?", (skill_id,)
                    ).fetchone()
                    return SkillMeta.from_dict(dict(row)) if row else None
            except Exception:
                pass
        for d in self._json_cache:
            if d.get("id") == skill_id:
                return SkillMeta.from_dict(d)
        return None

    def list_active(self, source: str | None = None) -> list[SkillMeta]:
        active = self._load_all_active()
        return [s for s in active if s.source == source] if source else active

    # ─ Stats ──────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        active = self._load_all_active()
        by_source: dict[str, int] = {}
        total_usage = 0
        for sk in active:
            by_source[sk.source] = by_source.get(sk.source, 0) + 1
            total_usage += sk.usage_count
        avg_rel = sum(sk.reliability_score for sk in active) / len(active) if active else 0.0
        hist_count = 0
        if self._use_sqlite:
            try:
                with sqlite3.connect(str(self._db_path)) as conn:
                    r = conn.execute("SELECT COUNT(*) FROM skill_history").fetchone()
                    hist_count = r[0] if r else 0
            except Exception:
                pass
        return {
            "active_skills": len(active),
            "max_skills": self.MAX_ACTIVE_SKILLS,
            "by_source": by_source,
            "total_usage": total_usage,
            "avg_reliability": round(avg_rel, 3),
            "history_events": hist_count,
            "storage": "sqlite" if self._use_sqlite else "json",
            "similarity_engine": "embedding" if _test_embedding_available() else "tfidf",
            "embed_cache_size": len(_embed_cache),
        }

    # ─ Internal CRUD ──────────────────────────────────────────────

    def _insert(self, meta: SkillMeta) -> None:
        if self._use_sqlite:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO skills VALUES "
                    "(:id,:name,:description,:code_hash,"
                    ":capability_tags,:trigger_patterns,:input_schema,:output_schema,"
                    ":version,:reliability_score,:usage_count,:success_count,:fail_count,"
                    ":avg_runtime_ms,:source,:is_active,:code_path,:parent_id,"
                    ":created_at,:last_used_at,:last_success_at)",
                    {
                        **meta.to_dict(),
                        "capability_tags": json.dumps(meta.capability_tags),
                        "trigger_patterns": json.dumps(meta.trigger_patterns),
                        "input_schema": json.dumps(meta.input_schema),
                        "output_schema": json.dumps(meta.output_schema),
                        "is_active": 1 if meta.is_active else 0,
                    }
                )
        else:
            self._json_cache.append(meta.to_dict())
            self._save_json()

    def _find_by_hash(self, code_hash: str) -> SkillMeta | None:
        if self._use_sqlite:
            try:
                with sqlite3.connect(str(self._db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM skills WHERE code_hash=? AND is_active=1",
                        (code_hash,)
                    ).fetchone()
                    return SkillMeta.from_dict(dict(row)) if row else None
            except Exception:
                pass
        for d in self._json_cache:
            if d.get("code_hash") == code_hash and d.get("is_active", True):
                return SkillMeta.from_dict(d)
        return None

    def _load_all_active(self) -> list[SkillMeta]:
        if self._use_sqlite:
            try:
                with sqlite3.connect(str(self._db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT * FROM skills WHERE is_active=1 ORDER BY reliability_score DESC"
                    ).fetchall()
                    return [SkillMeta.from_dict(dict(r)) for r in rows]
            except Exception:
                pass
        return [SkillMeta.from_dict(d) for d in self._json_cache if d.get("is_active", True)]

    def _count_active(self) -> int:
        if self._use_sqlite:
            try:
                with sqlite3.connect(str(self._db_path)) as conn:
                    r = conn.execute("SELECT COUNT(*) FROM skills WHERE is_active=1").fetchone()
                    return r[0] if r else 0
            except Exception:
                pass
        return sum(1 for d in self._json_cache if d.get("is_active", True))

    def _deactivate(self, skill_id: str) -> None:
        if self._use_sqlite:
            try:
                with self._connect() as conn:
                    conn.execute("UPDATE skills SET is_active=0 WHERE id=?", (skill_id,))
            except Exception:
                pass
        else:
            for d in self._json_cache:
                if d.get("id") == skill_id:
                    d["is_active"] = False
            self._save_json()

    def _reactivate(self, skill_id: str) -> None:
        if self._use_sqlite:
            try:
                with self._connect() as conn:
                    conn.execute("UPDATE skills SET is_active=1 WHERE id=?", (skill_id,))
            except Exception:
                pass
        else:
            for d in self._json_cache:
                if d.get("id") == skill_id:
                    d["is_active"] = True
            self._save_json()

    def _bump_version(self, existing: SkillMeta, new_meta: SkillMeta) -> SkillMeta:
        if self._use_sqlite:
            try:
                merged_tags = json.dumps(list(set(existing.capability_tags + new_meta.capability_tags)))
                merged_pats = json.dumps(list(set(existing.trigger_patterns + new_meta.trigger_patterns)))
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE skills SET version=version+1, description=?, "
                        "capability_tags=?, trigger_patterns=?, code_hash=? WHERE id=?",
                        (new_meta.description or existing.description,
                         merged_tags, merged_pats, new_meta.code_hash, existing.id)
                    )
                existing.version += 1
            except Exception:
                pass
        return existing

    def _auto_consolidate(self) -> int:
        active = self._load_all_active()
        deactivated = 0
        candidates = sorted(active, key=lambda s: s.reliability_score)
        for i, sk_a in enumerate(candidates):
            if not any(s.id == sk_a.id for s in self._load_all_active()):
                continue
            for sk_b in candidates[i + 1:]:
                sim = _tf_idf_similarity(
                    f"{sk_a.name} {sk_a.description}",
                    f"{sk_b.name} {sk_b.description}",
                )
                if sim >= self.DEDUP_THRESHOLD:
                    victim = sk_a if sk_a.reliability_score <= sk_b.reliability_score else sk_b
                    self._deactivate(victim.id)
                    deactivated += 1
                    break
            if self._count_active() <= self.MAX_ACTIVE_SKILLS:
                break
        if deactivated:
            _vlog("🔧", f"Consolidation: -{deactivated} skills")
        return deactivated

    def _record_history(self, skill_id: str, event_type: str, detail: str = "") -> None:
        if not self._use_sqlite:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO skill_history(skill_id,event_type,detail,recorded_at) VALUES(?,?,?,?)",
                    (skill_id, event_type, detail[:200], time.time())
                )
        except Exception:
            pass

    def _load_json(self) -> list[dict]:
        if self._json_path.exists():
            try:
                return json.loads(self._json_path.read_text("utf-8"))
            except Exception:
                pass
        return []

    def _save_json(self) -> None:
        try:
            self._json_path.write_text(
                json.dumps(self._json_cache, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception:
            pass

    def _json_update_usage(self, skill_id: str, success: bool, runtime_ms: float) -> None:
        for d in self._json_cache:
            if d.get("id") == skill_id:
                uc = d.get("usage_count", 0) + 1
                d["usage_count"] = uc
                if success:
                    d["success_count"] = d.get("success_count", 0) + 1
                    d["last_success_at"] = time.time()
                else:
                    d["fail_count"] = d.get("fail_count", 0) + 1
                d["last_used_at"] = time.time()
                d["reliability_score"] = d["success_count"] / uc
                old_avg = d.get("avg_runtime_ms", 0.0)
                d["avg_runtime_ms"] = (old_avg * (uc - 1) + runtime_ms) / uc
                break
        self._save_json()
