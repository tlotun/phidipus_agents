# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/memory_agent.py — Phidipus Memory Agent (v4.3)
═══════════════════════════════════════════════════════

Long-term, local-first memory for the agent, following the 2025-26 agent
memory patterns:

  * Mem0-style write path — every new memory is reconciled against similar
    ones: ADD / UPDATE (merge) / INVALIDATE (supersede) / NOOP (duplicate).
    A small local model decides when available, deterministic rules otherwise.
  * Zep-style temporal memory — superseded facts are not deleted but get
    ``valid_to`` + ``superseded_by``; recall returns the current truth and the
    history stays auditable.
  * Letta-style core memory blocks — a short "user_profile" / "preferences"
    summary that is always in context, refreshed in the background.
  * Hybrid retrieval — FTS5 BM25 (accent-insensitive Vietnamese) + embedding
    cosine + recency decay + importance.
  * "Sleep-time" consolidation — pruning, re-embedding, semantic de-dup and
    core-block refresh run periodically in the background.
  * Episodic + procedural learning — every task outcome becomes an episode,
    and "which method worked for this kind of request" is tracked as a
    procedure memory.

Privacy: everything is stored in one local SQLite file (chmod 600); secrets
are scrubbed before storage; memories are only injected into prompts sent
to cloud LLMs when ``memory_agent.share_with_cloud: true``.

Public API (async unless noted):
    agent = get_memory_agent()                 # sync, may return None (disabled)
    await agent.remember("Sếp tên là Minh")    # → {"op": "ADD", "id": …}
    await agent.recall("sếp tên gì", k=5)      # → [MemoryRecord]
    await agent.build_context("…")             # → prompt-ready text
    await agent.forget("…" | id)
    agent.observe_task_nowait(goal=…, success=…, method=…)   # sync, non-blocking
    await agent.consolidate()
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from memory.memory_store import KINDS, MemoryRecord, MemoryStore, fold
from memory.secret_filter import is_mostly_secret, scrub

_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "db_path": "data/memory/agent_memory.db",
    "embed_model": "",            # "" → model registry role "embed"
    "observe_tasks": True,
    "llm_reconcile": True,        # local "fast" model decides ADD/UPDATE/INVALIDATE/NOOP
    "share_with_cloud": False,    # never inject memories into prompts for cloud LLMs
    "consolidate_interval_h": 6,
    "episode_ttl_days": 90,
    "max_episodes": 2000,
    "context_max_chars": 1200,
}

# Only function words that stay unambiguous after accent folding ("minh" could
# be the name Minh, "ban" could be "bán", "ma" could be "mã", "cho" could be "chợ"…)
# ("an" = Long An, "in" = in ấn, "me" = mẹ, "the" = thẻ, "cua" = cửa … are NOT stop words)
_STOP = frozenset({
    "toi", "la", "va", "voi", "nhe", "nha", "di", "giup", "dum", "hay", "rang",
    "mot", "cac", "nhung", "of", "and", "is", "are", "for", "my", "please",
})
_PROFILE_CUES = re.compile(
    r"\b(toi la|toi ten|ten toi|ten cua toi|minh la|minh ten|my name|i am|i'm|cong ty (toi|minh)|"
    r"toi lam|minh lam|lam viec tai|email (cua )?(toi|minh)|so dien thoai|sdt|dia chi (cua )?(toi|minh)|"
    r"sinh nhat|nghe nghiep|chuc vu)\b")
_PREF_CUES = re.compile(
    r"\b(thich|khong thich|ghet|uu tien|luon luon|luon|dung bao gio|khong bao gio|muon|mac dinh|"
    r"prefer|always|never|like|dislike|hate|favorite|yeu thich)\b")
_CHANGE_CUES = re.compile(
    r"\b(khong con|khong dung nua|da chuyen|da doi|doi sang|doi thanh|doi ve|chuyen sang|chuyen ve|"
    r"chuyen den|chuyen qua|thay vi|thay bang|bay gio|gio la|nay da|tu nay|from now|no longer|"
    r"instead|changed|switched|moved|relocated|now)\b")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[0-9a-z]+", fold(text))
            if (len(t) > 1 or t.isdigit()) and t not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", text))


def _ollama_base() -> str:
    try:
        from core.model_registry import ollama_url
        return ollama_url()
    except Exception:
        return "http://127.0.0.1:11434"


# ══════════════════════════════════════════════════════════════════
# Embeddings (Ollama)
# ══════════════════════════════════════════════════════════════════
class OllamaEmbedder:
    """Batch embeddings via /api/embed (fallback /api/embeddings), LRU-cached."""

    def __init__(self, model: str = "", timeout: float = 10.0, cache_size: int = 512) -> None:
        self._model_cfg = model
        self.timeout = timeout
        self._cache: "collections.OrderedDict[str, np.ndarray]" = collections.OrderedDict()
        self._cache_size = cache_size
        self._down_until = 0.0
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        if self._model_cfg:
            return self._model_cfg
        try:
            from core.model_registry import get_model
            return get_model("embed", "nomic-embed-text")
        except Exception:
            return "nomic-embed-text"

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{_ollama_base()}{path}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _embed_api(self, model: str, texts: list[str]) -> list[np.ndarray]:
        try:
            data = self._post("/api/embed", {"model": model, "input": texts, "truncate": True})
            vecs = data.get("embeddings") or []
            if len(vecs) == len(texts):
                return [np.asarray(v, dtype=np.float32) for v in vecs]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        out = []
        for t in texts:   # legacy endpoint (Ollama < 0.3.4)
            data = self._post("/api/embeddings", {"model": model, "prompt": t})
            out.append(np.asarray(data.get("embedding") or [], dtype=np.float32))
        return out

    def embed(self, texts: list[str]) -> Optional[list[np.ndarray]]:
        """Unit-normalised vectors, or None when the embedder is unavailable."""
        if not texts:
            return []
        if time.time() < self._down_until:
            return None
        model = self.model
        keys = [f"{model}|{hashlib.sha1(t.encode('utf-8')).hexdigest()}" for t in texts]
        out: list[Optional[np.ndarray]] = [None] * len(texts)
        todo = []
        with self._lock:
            for i, k in enumerate(keys):
                if k in self._cache:
                    self._cache.move_to_end(k)
                    out[i] = self._cache[k]
                else:
                    todo.append(i)
        if todo:
            try:
                vecs = self._embed_api(model, [texts[i][:4000] for i in todo])
            except Exception as exc:
                self._down_until = time.time() + 60
                _vlog("⚠️", f"Memory embeddings unavailable ({str(exc)[:80]}) — lexical recall only")
                return None
            with self._lock:
                for i, v in zip(todo, vecs):
                    n = float(np.linalg.norm(v)) if v.size else 0.0
                    if n == 0.0:
                        return None
                    v = (v / n).astype(np.float32)
                    out[i] = v
                    self._cache[keys[i]] = v
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
        return out  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════════════
# Local LLM (reconcile + consolidation) — never a cloud model
# ══════════════════════════════════════════════════════════════════
class LocalLLM:
    def __init__(self, role: str = "fast", timeout: float = 25.0) -> None:
        self.role = role
        self.timeout = timeout
        self._down_until = 0.0

    @property
    def model(self) -> str:
        try:
            from core.model_registry import get_model
            return get_model(self.role, "qwen3:4b-instruct")
        except Exception:
            return "qwen3:4b-instruct"

    def chat(self, system: str, user: str, as_json: bool = False, max_tokens: int = 400) -> Optional[str]:
        if time.time() < self._down_until:
            return None
        body: dict[str, Any] = {
            "model": self.model, "stream": False, "think": False, "keep_alive": "5m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.0, "num_predict": max_tokens},
        }
        if as_json:
            body["format"] = "json"
        try:
            req = urllib.request.Request(
                f"{_ollama_base()}/api/chat", data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            self._down_until = time.time() + 120
            return None
        text = (data.get("message") or {}).get("content", "")
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()

    def chat_json(self, system: str, user: str, max_tokens: int = 300) -> Optional[dict]:
        text = self.chat(system, user, as_json=True, max_tokens=max_tokens)
        if not text:
            return None
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.S)
            try:
                return json.loads(m.group(0)) if m else None
            except json.JSONDecodeError:
                return None


_RECONCILE_SYSTEM = (
    "You maintain the long-term memory of a personal AI assistant. Decide how a NEW memory "
    "relates to EXISTING similar memories and answer with JSON only:\n"
    '{"op": "ADD|UPDATE|INVALIDATE|NOOP", "target": "<existing id or empty>", "content": "<text>"}\n'
    "- NOOP: the NEW memory is already fully contained in one existing memory (target = it).\n"
    "- UPDATE: same fact, the NEW one adds details → content = ONE merged sentence keeping every "
    "detail, same language as the input; target = that id.\n"
    "- INVALIDATE: the NEW memory contradicts or replaces an existing one (changed name, number, "
    "preference, address…) → target = the outdated id; content = the NEW memory.\n"
    "- ADD: anything else; target empty; content = the NEW memory.\n"
    "Never invent facts. Keep content under 300 characters."
)

_BLOCK_SYSTEM = (
    "Summarise the memories into a compact profile for an AI assistant. Use the same language as "
    "the memories (usually Vietnamese), short bullet lines starting with '- ', newest information "
    "wins when items conflict, no speculation, maximum {limit} characters."
)


# ══════════════════════════════════════════════════════════════════
# Memory Agent
# ══════════════════════════════════════════════════════════════════
class MemoryAgent:
    def __init__(self, store: MemoryStore, embedder: Any = None, llm: Any = None,
                 config: Optional[dict] = None) -> None:
        self.store = store
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.embedder = embedder if embedder is not None else OllamaEmbedder(self.cfg.get("embed_model", ""))
        if llm is None and self.cfg.get("llm_reconcile", True):
            llm = LocalLLM()
        self.llm = llm
        self._queue: "collections.deque[dict]" = collections.deque(maxlen=500)
        self._worker: Optional[asyncio.Task] = None
        self._consolidator: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        stored_flag = self.store.get_meta("observe_tasks", "")
        self._observing = bool(self.cfg.get("observe_tasks", True)) if not stored_flag else stored_flag == "1"

    # ── small helpers ─────────────────────────────────────────────────
    @property
    def embed_model(self) -> str:
        return getattr(self.embedder, "model", "")

    async def _embed(self, texts: list[str]) -> Optional[list[np.ndarray]]:
        if self.embedder is None:
            return None
        return await asyncio.to_thread(self.embedder.embed, texts)

    async def _embed_one(self, text: str) -> Optional[np.ndarray]:
        vecs = await self._embed([text])
        return vecs[0] if vecs else None

    @staticmethod
    def classify_kind(text: str) -> str:
        f = fold(text)
        if _PROFILE_CUES.search(f):
            return "profile"
        if _PREF_CUES.search(f):
            return "preference"
        return "fact"

    @staticmethod
    def default_importance(kind: str, source: str) -> float:
        base = {"profile": 0.85, "preference": 0.8, "fact": 0.6, "procedure": 0.6, "episode": 0.3}
        imp = base.get(kind, 0.5)
        return imp if source == "user" else max(0.2, imp - 0.15)

    @staticmethod
    def _recency(ts: float, half_life_days: float = 30.0) -> float:
        age_days = max(0.0, (time.time() - ts) / 86400)
        return math.exp(-age_days * math.log(2) / half_life_days)

    # ── observing on / off ────────────────────────────────────────────
    @property
    def observing(self) -> bool:
        return self._observing

    def set_observing(self, on: bool) -> None:
        self._observing = bool(on)
        self.store.set_meta("observe_tasks", "1" if on else "0")

    # ══════════════════════════════════════════════════════════════
    # WRITE PATH
    # ══════════════════════════════════════════════════════════════
    async def remember(self, content: str, kind: Optional[str] = None, subject: str = "user",
                       importance: Optional[float] = None, source: str = "user",
                       tags: Optional[list[str]] = None, pinned: bool = False) -> dict[str, Any]:
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        if len(text) < 3:
            return {"op": "REJECT", "reason": "Nội dung quá ngắn"}
        clean, findings = scrub(text)
        if findings and is_mostly_secret(text, clean):
            return {"op": "REJECT", "reason": "Không lưu thông tin bí mật (API key, mật khẩu, OTP, số thẻ…)",
                    "findings": findings}
        text = clean[:1000]
        kind = kind if kind in KINDS else self.classify_kind(text)
        imp = self.default_importance(kind, source) if importance is None else float(importance)

        async with self._write_lock:
            dup = await asyncio.to_thread(self.store.find_valid_by_hash, text, subject)
            if dup is not None:
                await asyncio.to_thread(self.store.touch, [dup.id])
                if imp > dup.importance or pinned:
                    await asyncio.to_thread(self.store.update, dup.id, importance=max(imp, dup.importance),
                                            pinned=pinned or None, detail="duplicate → importance bump")
                return {"op": "NOOP", "id": dup.id, "content": dup.content, "redacted": findings}

            vec = await self._embed_one(text)
            decision: Optional[dict] = None
            candidates: list[tuple[MemoryRecord, float]] = []
            if kind != "episode":
                compatible = ["fact", "preference", "profile"] if kind in ("fact", "preference", "profile") else [kind]
                candidates = await self._similar(text, vec, compatible, k=5)
                if candidates and self.llm is not None:
                    decision = await self._llm_decide(text, candidates)
                if decision is None:
                    decision = self._heuristic_decide(text, candidates)
            else:
                decision = {"op": "ADD"}
            return await self._apply(decision, text, vec, kind, subject, imp, source, tags or [],
                                     pinned, findings, candidates)

    async def _similar(self, text: str, vec: Optional[np.ndarray], kinds: list[str],
                       k: int = 5) -> list[tuple[MemoryRecord, float]]:
        """Most similar valid memories: cosine when vectors exist, token Jaccard otherwise."""
        sims: dict[str, float] = {}
        recs: dict[str, MemoryRecord] = {}
        if vec is not None:
            ids, models, mat = await asyncio.to_thread(self.store.vectors, False, kinds)
            if mat is not None and mat.shape[1] == vec.shape[0]:
                cos = mat @ vec
                for i in np.argsort(-cos)[:k * 2]:
                    if models[i] == self.embed_model:
                        sims[ids[i]] = float(cos[i])
        tok = _tokens(text)
        for rec, _rank in await asyncio.to_thread(self.store.fts_search, text, 20, False, kinds):
            recs[rec.id] = rec
            if rec.id not in sims:
                sims[rec.id] = _jaccard(tok, _tokens(rec.content)) * (0.95 if vec is not None else 1.0)
        missing = [i for i in sims if i not in recs]
        for i in missing:
            r = await asyncio.to_thread(self.store.get, i)
            if r is not None:
                recs[i] = r
        ranked = sorted(((recs[i], s) for i, s in sims.items() if i in recs), key=lambda x: -x[1])
        return ranked[:k]

    async def _llm_decide(self, text: str, candidates: list[tuple[MemoryRecord, float]]) -> Optional[dict]:
        relevant = [(r, s) for r, s in candidates if s >= 0.3][:5]
        if not relevant:
            return {"op": "ADD"}
        listing = "\n".join(
            f"[{r.id}] ({time.strftime('%Y-%m-%d', time.localtime(r.updated_at))}) {r.content}"
            for r, _ in relevant)
        user = f"NEW: {text}\n\nEXISTING:\n{listing}"
        obj = await asyncio.to_thread(self.llm.chat_json, _RECONCILE_SYSTEM, user)
        if not obj:
            return None
        op = str(obj.get("op", "")).upper()
        target = str(obj.get("target", "") or "")
        valid_ids = {r.id for r, _ in relevant}
        if op not in ("ADD", "UPDATE", "INVALIDATE", "NOOP"):
            return None
        if op in ("UPDATE", "INVALIDATE", "NOOP") and target not in valid_ids:
            return None
        merged = str(obj.get("content", "") or "").strip()
        if op == "UPDATE":
            merged, found = scrub(merged)
            if not merged or len(merged) > 600 or found:
                return None
        return {"op": op, "target": target, "content": merged, "by": "llm"}

    @staticmethod
    def _heuristic_decide(text: str, candidates: list[tuple[MemoryRecord, float]]) -> dict:
        if not candidates:
            return {"op": "ADD", "by": "rule"}
        best, sim = candidates[0]
        new_tok, old_tok = _tokens(text), _tokens(best.content)
        jac = _jaccard(new_tok, old_tok)
        change = bool(_CHANGE_CUES.search(fold(text)))
        if sim >= 0.95 or (new_tok and new_tok <= old_tok and (sim >= 0.8 or jac >= 0.8)):
            return {"op": "NOOP", "target": best.id, "by": "rule"}
        # the new statement fully contains the old one and adds details → merge
        if len(old_tok) >= 2 and old_tok < new_tok and not change and (sim >= 0.5 or jac >= 0.25):
            return {"op": "UPDATE", "target": best.id, "content": text, "by": "rule"}
        # explicit change wording about the same subject ("kho hàng chính chuyển về …")
        inter = new_tok & old_tok
        overlap = len(inter) / max(1, min(len(new_tok), len(old_tok)))
        if change and len(inter) >= 2 and (overlap >= 0.5 or sim >= 0.7):
            return {"op": "INVALIDATE", "target": best.id, "content": text, "by": "rule"}
        if not (sim >= 0.80 or jac >= 0.6):
            return {"op": "ADD", "by": "rule"}
        # Same topic with a changed detail (name, number, "không còn…") → the newer
        # statement supersedes; the old one is kept as history.  Two different
        # preferences ("thích trà" / "thích cà phê") are allowed to coexist.
        nums_new, nums_old = _numbers(text), _numbers(best.content)
        small_diff = len(new_tok - old_tok) <= 2 and len(old_tok - new_tok) <= 2 and jac >= 0.5
        if change or (nums_new and nums_old and nums_new != nums_old) or (small_diff and sim >= 0.88):
            return {"op": "INVALIDATE", "target": best.id, "content": text, "by": "rule"}
        return {"op": "ADD", "by": "rule"}

    async def _apply(self, decision: dict, text: str, vec: Optional[np.ndarray], kind: str,
                     subject: str, importance: float, source: str, tags: list[str], pinned: bool,
                     findings: list[str], candidates: list[tuple[MemoryRecord, float]]) -> dict[str, Any]:
        op = decision.get("op", "ADD")
        target = decision.get("target", "")
        by = decision.get("by", "rule")
        if op == "NOOP" and target:
            await asyncio.to_thread(self.store.touch, [target])
            old = await asyncio.to_thread(self.store.get, target)
            return {"op": "NOOP", "id": target, "content": old.content if old else "", "by": by,
                    "redacted": findings}
        if op == "UPDATE" and target:
            merged = decision.get("content") or text
            mvec = vec if merged == text else await self._embed_one(merged)
            old = await asyncio.to_thread(self.store.get, target)
            await asyncio.to_thread(
                self.store.update, target, content=merged, embedding=mvec, embed_model=self.embed_model,
                importance=max(importance, old.importance if old else 0.0),
                detail=f"merge ← {text[:120]}")
            return {"op": "UPDATE", "id": target, "content": merged, "previous": old.content if old else "",
                    "by": by, "redacted": findings}
        rec = MemoryRecord(content=text, kind=kind, subject=subject, tags=tags, importance=importance,
                           source=source, pinned=pinned, embed_model=self.embed_model if vec is not None else "")
        await asyncio.to_thread(self.store.add, rec, vec)
        result: dict[str, Any] = {"op": "ADD", "id": rec.id, "content": text, "kind": kind, "by": by,
                                  "redacted": findings}
        if op == "INVALIDATE" and target:
            old = await asyncio.to_thread(self.store.get, target)
            await asyncio.to_thread(self.store.invalidate, target, rec.id, f"superseded: {text[:120]}")
            result.update(op="INVALIDATE", replaced_id=target, previous=old.content if old else "")
        return result

    # ══════════════════════════════════════════════════════════════
    # READ PATH
    # ══════════════════════════════════════════════════════════════
    async def recall(self, query: str, k: int = 5, kinds: Optional[Iterable[str]] = None,
                     include_history: bool = False, touch: bool = True,
                     min_score: float = 0.15) -> list[MemoryRecord]:
        query = str(query or "").strip()
        if not query:
            return []
        kinds = list(kinds or [])
        fts = await asyncio.to_thread(self.store.fts_search, query, 50, include_history, kinds or None)
        lex: dict[str, float] = {}
        recs: dict[str, MemoryRecord] = {}
        if fts:
            best = max(-r for _, r in fts) or 1.0
            for rec, rank in fts:
                lex[rec.id] = max(0.0, -rank) / best if best > 0 else 0.0
                recs[rec.id] = rec
        cos: dict[str, float] = {}
        qvec = await self._embed_one(query)
        if qvec is not None:
            ids, models, mat = await asyncio.to_thread(self.store.vectors, include_history, kinds or None)
            if mat is not None and mat.shape[1] == qvec.shape[0]:
                sims = mat @ qvec
                for i in np.argsort(-sims)[:50]:
                    if models[i] == self.embed_model:
                        cos[ids[i]] = float(sims[i])
        for i in [i for i in cos if i not in recs]:
            r = await asyncio.to_thread(self.store.get, i)
            if r is not None:
                recs[i] = r
        scored: list[MemoryRecord] = []
        for i, rec in recs.items():
            c, l = cos.get(i), lex.get(i, 0.0)
            if c is None and l == 0.0:
                continue
            if c is not None and l == 0.0 and c < 0.55:
                continue   # vector-only evidence must be strong
            rec_score = self._recency(rec.updated_at)
            if c is not None:
                score = 0.45 * max(0.0, c) + 0.35 * l + 0.10 * rec_score + 0.10 * rec.importance
            else:
                score = 0.70 * l + 0.12 * rec_score + 0.18 * rec.importance
            if rec.pinned:
                score += 0.05
            if not rec.is_valid:
                score *= 0.6
            rec.score = round(score, 4)
            if score >= min_score:
                scored.append(rec)
        scored.sort(key=lambda r: -r.score)
        top = scored[:k]
        if touch and top:
            await asyncio.to_thread(self.store.touch, [r.id for r in top])
        return top

    async def build_context(self, query: str, max_chars: Optional[int] = None,
                            for_cloud: bool = False) -> str:
        """Prompt-ready memory context ('' when disabled / nothing relevant)."""
        if not self.cfg.get("enabled", True):
            return ""
        if for_cloud and not self.cfg.get("share_with_cloud", False):
            return ""
        limit = int(max_chars or self.cfg.get("context_max_chars", 1200))
        parts: list[str] = []
        blocks = await asyncio.to_thread(self.store.list_blocks)
        if blocks.get("user_profile"):
            parts.append("Hồ sơ người dùng:\n" + blocks["user_profile"])
        if blocks.get("preferences"):
            parts.append("Sở thích / quy ước:\n" + blocks["preferences"])
        mems = await self.recall(query, k=6, kinds=["fact", "preference", "profile", "procedure"])
        episodes = [e for e in await self.recall(query, k=3, kinds=["episode"], touch=False)
                    if e.score >= 0.45][:2]
        lines = []
        for m in mems + episodes:
            day = time.strftime("%d/%m/%Y", time.localtime(m.updated_at))
            lines.append(f"- [{day}] {m.content}")
        if lines:
            parts.append("Ký ức liên quan:\n" + "\n".join(lines))
        if not parts:
            return ""
        text = "[BỘ NHỚ DÀI HẠN — thông tin đã biết về người dùng, dùng khi phù hợp]\n" + "\n\n".join(parts)
        return text if len(text) <= limit else text[:limit - 1].rsplit("\n", 1)[0] + "\n…"

    async def forget(self, target: str) -> dict[str, Any]:
        """Delete by id, or delete memories matching every content word of a query."""
        t = str(target or "").strip()
        if not t:
            return {"deleted": []}
        if re.fullmatch(r"[0-9a-f]{12}", t):
            rec = await asyncio.to_thread(self.store.get, t)
            ok = await asyncio.to_thread(self.store.delete, t, "forget by id")
            return {"deleted": [t] if ok else [], "contents": [rec.content] if (ok and rec) else []}
        toks = _tokens(t)
        hits = await self.recall(t, k=10, touch=False, min_score=0.0)
        victims = [h for h in hits if toks and toks <= _tokens(h.content)]
        for v in victims:
            await asyncio.to_thread(self.store.delete, v.id, f"forget: {t[:80]}")
        return {"deleted": [v.id for v in victims], "contents": [v.content for v in victims]}

    async def forget_all(self) -> int:
        return await asyncio.to_thread(self.store.delete_all)

    # ══════════════════════════════════════════════════════════════
    # EPISODIC + PROCEDURAL LEARNING
    # ══════════════════════════════════════════════════════════════
    @staticmethod
    def signature(goal: str) -> str:
        toks = [t for t in re.findall(r"[a-z]+", fold(goal)) if t not in _STOP and len(t) > 1]
        return " ".join(toks[:8])

    def observe_task_nowait(self, goal: str, success: bool, method: str = "", error: str = "",
                            detail: str = "", duration_s: float = 0.0) -> None:
        """Queue a task outcome; returns immediately (safe from any coroutine)."""
        if not self.cfg.get("enabled", True) or not self._observing or not goal:
            return
        self._queue.append({"goal": str(goal)[:400], "success": bool(success), "method": str(method or "")[:120],
                            "error": str(error or "")[:300], "detail": str(detail or "")[:300],
                            "duration_s": float(duration_s or 0.0), "ts": time.time()})
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._drain())

    async def _drain(self) -> None:
        while self._queue:
            item = self._queue.popleft()
            try:
                await self._record_observation(item)
            except Exception as exc:
                _vlog("⚠️", f"Memory observe error: {str(exc)[:100]}")

    async def _record_observation(self, it: dict) -> None:
        goal, findings = scrub(it["goal"])
        if findings and is_mostly_secret(it["goal"], goal):
            return
        method = it["method"] or "agent"
        ok = it["success"]
        dur = f" ({it['duration_s']:.0f}s)" if it["duration_s"] >= 1 else ""
        err = scrub(it["error"])[0][:120] if (it["error"] and not ok) else ""
        content = (f"{'Thành công' if ok else 'Thất bại'}: «{goal[:160]}» — cách: {method}{dur}"
                   + (f" — lỗi: {err}" if err else ""))
        rec = MemoryRecord(content=content, kind="episode", subject="task",
                           tags=[method.split(":")[0], "success" if ok else "failure"],
                           importance=0.3 if ok else 0.45, source="task")
        vec = await self._embed_one(goal)
        await asyncio.to_thread(self.store.add, rec, vec)

        sig = self.signature(goal)
        if not sig:
            return
        proc = await asyncio.to_thread(self.store.upsert_procedure, sig, goal[:200], method, ok, err)
        s, f = int(proc["successes"]), int(proc["failures"])
        if s and proc["method"]:
            text = (f"Với yêu cầu kiểu «{proc['example'][:90]}», cách hiệu quả: {proc['method']} "
                    f"(thành công {s}/{s + f} lần).")
        else:
            text = (f"Yêu cầu kiểu «{proc['example'][:90]}» đã thất bại {f} lần"
                    + (f" (lỗi gần nhất: {proc['last_error'][:80]})." if proc["last_error"] else "."))
        imp = min(0.9, 0.5 + 0.1 * min(s, 4)) if s else 0.4
        pvec = await self._embed_one(text)
        if proc["memory_id"] and await asyncio.to_thread(self.store.get, proc["memory_id"]):
            await asyncio.to_thread(self.store.update, proc["memory_id"], content=text, embedding=pvec,
                                    embed_model=self.embed_model, importance=imp, detail="procedure stats")
        else:
            prec = MemoryRecord(content=text, kind="procedure", subject=f"proc:{sig[:60]}",
                                tags=["procedure"], importance=imp, source="task",
                                embed_model=self.embed_model if pvec is not None else "")
            await asyncio.to_thread(self.store.add, prec, pvec)
            await asyncio.to_thread(self.store.set_procedure_memory, sig, prec.id)

    def suggest_method(self, goal: str) -> Optional[dict]:
        """Method that worked before for this kind of request (sync, cheap)."""
        proc = self.store.get_procedure(self.signature(goal))
        if proc and proc["method"] and proc["successes"] > proc["failures"]:
            return {"method": proc["method"], "successes": proc["successes"], "failures": proc["failures"]}
        return None

    # ══════════════════════════════════════════════════════════════
    # CORE BLOCKS
    # ══════════════════════════════════════════════════════════════
    def blocks(self) -> dict[str, str]:
        return self.store.list_blocks()

    def set_block(self, name: str, content: str) -> str:
        scrubbed, _ = scrub(content)
        return self.store.set_block(name, scrubbed)

    async def _refresh_core_blocks(self, force: bool = False) -> list[str]:
        refreshed = []
        for block, kind in (("user_profile", "profile"), ("preferences", "preference")):
            mems = await asyncio.to_thread(self.store.list, kind, None, False, 40, 0, "importance DESC")
            if not mems:
                continue
            newest = max(m.updated_at for m in mems)
            if not force and newest <= float(self.store.get_meta(f"block_ts_{block}", "0") or 0):
                continue
            limit = 900
            text = None
            if self.llm is not None:
                listing = "\n".join(f"- ({time.strftime('%Y-%m-%d', time.localtime(m.updated_at))}) {m.content}"
                                    for m in sorted(mems, key=lambda m: m.updated_at))
                text = await asyncio.to_thread(self.llm.chat, _BLOCK_SYSTEM.format(limit=limit), listing,
                                               False, 500)
                if text:
                    text, found = scrub(text)
                    if found:
                        text = None
            if not text:
                text = "\n".join(f"- {m.content}" for m in mems[:12])
            await asyncio.to_thread(self.store.set_block, block, text[:limit])
            self.store.set_meta(f"block_ts_{block}", str(newest))
            refreshed.append(block)
        return refreshed

    # ══════════════════════════════════════════════════════════════
    # CONSOLIDATION ("sleep-time compute")
    # ══════════════════════════════════════════════════════════════
    async def _reembed_missing(self, limit: int = 64) -> int:
        if self.embedder is None:
            return 0
        model = self.embed_model
        recs = await asyncio.to_thread(self.store.missing_embeddings, model, limit)
        if not recs:
            return 0
        vecs = await self._embed([r.content for r in recs])
        if not vecs:
            return 0
        for r, v in zip(recs, vecs):
            await asyncio.to_thread(self.store.update, r.id, embedding=v, embed_model=model,
                                    detail="re-embed")
        return len(recs)

    async def _dedupe_semantic(self, threshold: float = 0.96) -> int:
        ids, models, mat = await asyncio.to_thread(self.store.vectors, False, ["fact", "preference", "profile"])
        if mat is None or len(ids) < 2 or len(ids) > 5000:
            return 0
        keep = [i for i, m in enumerate(models) if m == self.embed_model]
        if len(keep) < 2:
            return 0
        sub = mat[keep]
        sims = sub @ sub.T
        np.fill_diagonal(sims, 0.0)
        merged = 0
        gone: set[int] = set()
        for a in range(len(keep)):
            if a in gone:
                continue
            for b in np.where(sims[a] >= threshold)[0]:
                if b in gone or b == a:
                    continue
                ra = await asyncio.to_thread(self.store.get, ids[keep[a]])
                rb = await asyncio.to_thread(self.store.get, ids[keep[int(b)]])
                if not ra or not rb or ra.pinned or rb.pinned:
                    continue
                older, newer = (ra, rb) if ra.updated_at <= rb.updated_at else (rb, ra)
                await asyncio.to_thread(self.store.invalidate, older.id, newer.id, "semantic duplicate")
                gone.add(a if older is ra else int(b))
                merged += 1
                if older is ra:
                    break
        return merged

    async def consolidate(self, force_blocks: bool = False) -> dict[str, Any]:
        t0 = time.time()
        report: dict[str, Any] = {}
        report["pruned"] = await asyncio.to_thread(
            self.store.prune_episodes, float(self.cfg.get("episode_ttl_days", 90)),
            int(self.cfg.get("max_episodes", 2000)))
        report["reembedded"] = await self._reembed_missing()
        report["deduplicated"] = await self._dedupe_semantic()
        report["blocks"] = await self._refresh_core_blocks(force=force_blocks)
        report["seconds"] = round(time.time() - t0, 2)
        self.store.set_meta("last_consolidation", str(time.time()))
        self.store.log_event("CONSOLIDATE", "", json.dumps(report, ensure_ascii=False))
        return report

    async def _consolidation_loop(self) -> None:
        await asyncio.sleep(600)
        interval = max(0.5, float(self.cfg.get("consolidate_interval_h", 6))) * 3600
        while True:
            try:
                rep = await self.consolidate()
                if any(rep.get(k) for k in ("pruned", "reembedded", "deduplicated", "blocks")):
                    _vlog("🧠", f"Memory consolidation: {rep}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _vlog("⚠️", f"Memory consolidation error: {str(exc)[:120]}")
            await asyncio.sleep(interval)

    async def start(self) -> None:
        if self._consolidator is None or self._consolidator.done():
            self._consolidator = asyncio.create_task(self._consolidation_loop())

    async def stop(self) -> None:
        for task in (self._consolidator, self._worker):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ── status ────────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        st = self.store.stats()
        last = float(self.store.get_meta("last_consolidation", "0") or 0)
        st.update(
            enabled=bool(self.cfg.get("enabled", True)),
            observing=self._observing,
            embed_model=self.embed_model,
            share_with_cloud=bool(self.cfg.get("share_with_cloud", False)),
            last_consolidation=last,
            queue=len(self._queue),
        )
        return st


# ══════════════════════════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════════════════════════
_AGENT: Optional[MemoryAgent] = None
_AGENT_LOCK = threading.Lock()


def _load_config() -> dict[str, Any]:
    try:
        from core.model_registry import config_section
        cfg = {**DEFAULT_CONFIG, **config_section("memory_agent")}
    except Exception:
        cfg = dict(DEFAULT_CONFIG)
    import os
    if os.environ.get("PHIDIPUS_MEMORY_DB"):          # tests / alternative profiles
        cfg["db_path"] = os.environ["PHIDIPUS_MEMORY_DB"]
    return cfg


def get_memory_agent() -> Optional[MemoryAgent]:
    """Process-wide Memory Agent, or None when disabled in config.yaml."""
    global _AGENT
    with _AGENT_LOCK:
        if _AGENT is not None:
            return _AGENT
        cfg = _load_config()
        if not cfg.get("enabled", True):
            return None
        path = Path(str(cfg.get("db_path") or DEFAULT_CONFIG["db_path"])).expanduser()
        if not path.is_absolute():
            path = _ROOT / path
        _AGENT = MemoryAgent(MemoryStore(path), config=cfg)
        return _AGENT
