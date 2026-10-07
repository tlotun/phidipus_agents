# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/semantic_router.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

P1: Semantic Router — Vector Embedding Routing (<50ms)

Thay thế UniversalParser Tier 2 (LLM ~2s) bằng cosine similarity:

  goal text → nomic-embed-text → 768-dim vector
            → compare vs SkillRegistry vectors
            → best match > 0.85 → Fast Lane (<50ms total)

Công thức:
  cosine_similarity(A, B) = (A·B) / (||A|| × ||B||)

Tiers:
  Tier 0: Cache (0ms)        — goal đã từng route → cache hit
  Tier 1: Rules (<5ms)       — 16 rules hiện tại trong UniversalParser
  Tier 2: Semantic (<50ms)   — nomic-embed-text cosine sim ← MỚI
  Tier 3: LLM (~2s)          — Ollama qwen3:8b text (fallback)
  Tier 4: Fallback (0ms)     — Explorer Lane unconditionally

SkillRegistry:
  Mỗi skill có list "examples" — câu lệnh mẫu đại diện cho skill đó.
  Lần đầu startup → embed tất cả examples → cache vào JSON.
  Khi skill mới được compile → tự add examples → re-embed.

Thresholds:
  sim >= 0.88 → Fast Lane (rất tự tin)
  sim >= 0.72 → Explorer Lane với intent hint
  sim <  0.72 → LLM fallback hoặc Clarify
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")
_CACHE_PATH = Path.home() / ".phidipus" / "skill_vectors.json"
# FIX v9.39: bump version khi examples thay đổi → tự invalidate cache cũ
_CACHE_VERSION = "v1.0.1"


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# SkillEntry — một skill trong registry
# ══════════════════════════════════════════════════════════════════

@dataclass
class SkillEntry:
    """Một skill với examples và cached embeddings."""
    skill_id:       str
    intent_type:    str        # "social_post" | "file_op" | "terminal" | ...
    workflow_skill: str        # "workflow_spider_social_post" | ""
    lane:           str        # "fast" | "explorer"
    examples:       list[str]  = field(default_factory=list)
    embeddings:     list[list] = field(default_factory=list)  # cached vectors
    requires_confirm: bool     = False

    @property
    def has_embeddings(self) -> bool:
        return len(self.embeddings) > 0

    def avg_embedding(self) -> list[float] | None:
        """Tính vector trung bình của tất cả examples."""
        if not self.embeddings:
            return None
        n   = len(self.embeddings)
        dim = len(self.embeddings[0])
        avg = [sum(self.embeddings[i][d] for i in range(n)) / n
               for d in range(dim)]
        return _normalize(avg)


# ══════════════════════════════════════════════════════════════════
# Math helpers
# ══════════════════════════════════════════════════════════════════

def _dot(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))

def _norm(v: list) -> float:
    return math.sqrt(sum(x * x for x in v)) + 1e-9

def _normalize(v: list) -> list:
    n = _norm(v)
    return [x / n for x in v]

def cosine_similarity(a: list, b: list) -> float:
    """Cosine similarity = (A·B) / (||A|| × ||B||)"""
    return _dot(a, b) / (_norm(a) * _norm(b))


# ══════════════════════════════════════════════════════════════════
# Default SkillRegistry — pre-built examples
# ══════════════════════════════════════════════════════════════════

DEFAULT_REGISTRY: list[SkillEntry] = [
    SkillEntry(
        skill_id="spider_social_post",
        intent_type="social_post",
        workflow_skill="workflow_spider_social_post",
        lane="fast",
        requires_confirm=True,
        examples=[
            "tạo ảnh chatgpt đăng facebook",
            "tạo hình AI bằng chatgpt rồi viết content gemini đăng fb",
            "generate image chatgpt create content gemini post facebook",
            "chatgpt vẽ ảnh con ong robot gemini viết content đăng bài",
            "tạo ảnh AI đăng lên mạng xã hội",
            "create AI image and post to social media",
            "tạo post facebook với ảnh AI",
            "đăng bài lên facebook với ảnh chatgpt",
        ],
    ),
    SkillEntry(
        skill_id="file_operation",
        intent_type="file_op",
        workflow_skill="workflow_finder_task",
        lane="fast",
        examples=[
            "copy file vào thư mục backup",
            "di chuyển ảnh từ downloads vào desktop",
            "tìm file pdf trong documents",
            "xóa file tạm vào trash",
            "nén thư mục thành zip",
            "tạo thư mục mới",
            "list danh sách file trong desktop",
            "copy all PDF files to backup folder",
            "move photos from downloads to desktop",
            "find excel files in documents",
            "tìm kiếm báo cáo tháng 3",
            "xem thông tin file dung lượng",
            "sao chép file ảnh vào album",
            # FIX v9.39: thêm examples PDF và extensions
            "copy PDF vào Desktop/Backup",
            "copy tất cả PDF trong Downloads vào Desktop",
            "di chuyển file jpg vào thư mục archive",
            "copy pdf files to backup",
            "sao chép pdf vào backup",
            "move all jpg to photos folder",
        ],
    ),
    SkillEntry(
        skill_id="terminal_command",
        intent_type="terminal",
        workflow_skill="workflow_terminal_task",
        lane="fast",
        examples=[
            "git status trong thư mục project",
            "chạy npm run build",
            "git commit và push",
            "xem git log 10 commits",
            "npm install dependencies",
            "python3 chạy script",
            "git pull từ remote",
            "xem disk usage thư mục",
            "đọc file log server",
            "chạy lệnh terminal",
            "run git status in project folder",
            "execute npm build command",
            "pip install package",
        ],
    ),
    SkillEntry(
        skill_id="web_browse",
        intent_type="web_task",
        workflow_skill="workflow_chrome_task",   # FIX v9.39: ChromeSkills đã có
        lane="fast",                             # FIX v9.39: explorer→fast
        examples=[
            "mở trang web google",
            "tìm kiếm thông tin trên google",
            "vào website để xem tin tức",
            "browse to website",
            "search google for information",
            "navigate to url",
            "mở chrome vào trang",
            "tìm kiếm trên internet",
            # FIX v9.39: thêm examples tiếng Việt để tăng sim score
            "tìm kiếm AI news trên google",
            "search AI agents on google",
            "google tìm thông tin về AI",
            "tìm trên google",
            "search thông tin trên web",
            "lấy nội dung trang web",
            "đọc bài trên website",
            # FIX v1.0: thêm examples với URL để tăng sim cho "lấy nội dung + URL"
            "lấy nội dung trang web https://vnexpress.net",
            "đọc nội dung website https://example.com",
            "get content from https://news.ycombinator.com",
            "mở trang https://github.com",
            "vào https://google.com tìm kiếm",
            "xem nội dung trang https://dantri.com.vn",
            "lấy text từ trang web",
            "đọc trang web lấy nội dung",
            "extract text from webpage",
            "read page content",
        ],
    ),
    SkillEntry(
        skill_id="email_task",
        intent_type="email",
        workflow_skill="",
        lane="explorer",
        requires_confirm=True,
        examples=[
            "gửi email cho sếp báo cáo tuần",
            "đọc email chưa đọc trong inbox",
            "reply email của khách hàng",
            "soạn email và đính kèm file",
            "check email hôm nay",
            "send email to boss with weekly report",
            "read unread emails",
            "compose email with attachment",
            "tìm email từ sếp",
            "forward email cho team",
        ],
    ),
    SkillEntry(
        skill_id="calendar_task",
        intent_type="calendar",
        workflow_skill="",
        lane="explorer",
        examples=[
            "đặt lịch họp team thứ 2 9 giờ sáng",
            "tạo event meeting với khách hàng",
            "xem lịch hôm nay",
            "nhắc nhở lúc 3 giờ chiều",
            "create calendar event",
            "schedule meeting Monday 9am",
            "check today schedule",
            "add reminder tomorrow morning",
        ],
    ),
    SkillEntry(
        skill_id="screenshot",
        intent_type="system",
        workflow_skill="",
        lane="explorer",
        examples=[
            "chụp màn hình",
            "screenshot toàn bộ màn hình",
            "capture screen",
            "take screenshot",
            "chụp ảnh màn hình",
        ],
    ),
    # FIX v1.0: Gmail skill
    SkillEntry(
        skill_id="gmail_task",
        intent_type="email",
        workflow_skill="workflow_gmail_task",
        lane="fast",
        requires_confirm=True,
        examples=[
            "gửi email cho sếp báo cáo tuần",
            "đọc email chưa đọc trong inbox",
            "reply email của khách hàng",
            "soạn email và đính kèm file",
            "check email hôm nay",
            "send email to boss with weekly report",
            "gửi mail cho boss@company.com tiêu đề họp sáng mai",
            "đọc inbox gmail",
            "tìm email từ khách hàng",
            "gửi file report.pdf qua email cho john@example.com",
        ],
    ),
    # FIX v1.0: Messenger skill
    SkillEntry(
        skill_id="messenger_task",
        intent_type="messenger",
        workflow_skill="workflow_messenger_task",
        lane="fast",
        requires_confirm=True,
        examples=[
            "gửi tin nhắn qua messenger cho Lan",
            "nhắn zalo cho sếp: họp lúc 3h",
            "gửi instagram DM cho @johndoe",
            "telegram cho @teamlead file báo cáo",
            "nhắn wechat cho đối tác",
            "gửi file Desktop/report.pdf qua zalo cho sếp",
            "send message on facebook messenger to John",
            "gửi ảnh qua instagram cho bạn",
            "nhắn tin telegram cho team",
            "gửi zalo cho khách hàng",
        ],
    ),
]


# ══════════════════════════════════════════════════════════════════
# SemanticRouter
# ══════════════════════════════════════════════════════════════════

@dataclass
class SemanticRouteDecision:
    """Kết quả semantic routing."""
    skill_id:       str   = ""
    intent_type:    str   = "general"
    workflow_skill: str   = ""
    lane:           str   = "explorer"
    similarity:     float = 0.0
    tier:           str   = "fallback"   # "cache" | "semantic" | "fallback"
    latency_ms:     int   = 0
    requires_confirm: bool = False


class SemanticRouter:
    """
    Routing <50ms bằng nomic-embed-text cosine similarity.

    Thresholds:
      >= FAST_THRESHOLD  (0.75): Fast Lane — match tốt
      >= EXPLORER_THRESHOLD (0.72): Explorer Lane với hint
      <  EXPLORER_THRESHOLD: không dùng semantic, LLM fallback

    Cache: goal hash → decision (tránh re-embed cùng goal)
    """

    FAST_THRESHOLD     = 0.75   # FIX v9.39: 0.88→0.75, nomic-embed tiếng Việt max ~0.79
    EXPLORER_THRESHOLD = 0.72
    EMBED_MODEL        = "nomic-embed-text"
    EMBED_URL          = "http://127.0.0.1:11434/api/embeddings"
    EMBED_TIMEOUT      = 3.0   # giây — fail fast nếu Ollama chậm

    def __init__(
        self,
        registry: list[SkillEntry] | None = None,
        cache_path: Path = _CACHE_PATH,
    ) -> None:
        self._registry   = registry or DEFAULT_REGISTRY
        self._cache_path = cache_path
        self._goal_cache: dict[str, SemanticRouteDecision] = {}  # in-memory
        self._embeddings_ready = False
        self._call_count = 0
        self._cache_hits = 0

        # Load cached embeddings từ disk
        self._load_cached_embeddings()

    # ── Public API ────────────────────────────────────────────────

    async def route(self, goal: str) -> SemanticRouteDecision | None:
        """
        Route goal qua semantic similarity.

        Returns:
            SemanticRouteDecision nếu similarity >= EXPLORER_THRESHOLD
            None nếu không đủ tự tin (caller nên dùng LLM fallback)
        """
        t0 = time.time()
        self._call_count += 1

        # ── Cache hit ────────────────────────────────────────────
        goal_hash = hashlib.md5(goal.lower().strip().encode()).hexdigest()[:12]
        if goal_hash in self._goal_cache:
            cached = self._goal_cache[goal_hash]
            cached.tier       = "cache"
            cached.latency_ms = int((time.time()-t0)*1000)
            self._cache_hits += 1
            _vlog("⚡", f"SemanticRouter cache hit: {cached.skill_id} "
                  f"sim={cached.similarity:.2f} ({cached.latency_ms}ms)")
            return cached

        # ── Embed goal ────────────────────────────────────────────
        goal_vec = await self._embed(goal)
        if goal_vec is None:
            return None  # Ollama unavailable → caller uses LLM

        # ── Find best match ───────────────────────────────────────
        best_skill: SkillEntry | None = None
        best_sim   = 0.0

        for skill in self._registry:
            avg_vec = skill.avg_embedding()
            if avg_vec is None:
                continue
            sim = cosine_similarity(goal_vec, avg_vec)
            if sim > best_sim:
                best_sim   = sim
                best_skill = skill

        ms = int((time.time()-t0)*1000)

        if best_skill is None or best_sim < self.EXPLORER_THRESHOLD:
            _vlog("🔍", f"SemanticRouter: no match (best={best_sim:.2f}) → LLM fallback ({ms}ms)")
            return None

        lane = "fast" if best_sim >= self.FAST_THRESHOLD else "explorer"
        decision = SemanticRouteDecision(
            skill_id=best_skill.skill_id,
            intent_type=best_skill.intent_type,
            workflow_skill=best_skill.workflow_skill,
            lane=lane,
            similarity=round(best_sim, 3),
            tier="semantic",
            latency_ms=ms,
            requires_confirm=best_skill.requires_confirm,
        )
        _vlog("🎯", f"SemanticRouter: {best_skill.skill_id} "
              f"sim={best_sim:.2f} lane={lane} ({ms}ms)")

        # Cache result
        self._goal_cache[goal_hash] = decision
        return decision

    def register_skill(
        self,
        skill_id: str,
        intent_type: str,
        examples: list[str],
        workflow_skill: str = "",
        lane: str = "explorer",
        requires_confirm: bool = False,
    ) -> None:
        """
        Đăng ký skill mới khi SkillForge compile xong.
        Tự embed examples và thêm vào registry.
        """
        entry = SkillEntry(
            skill_id=skill_id,
            intent_type=intent_type,
            workflow_skill=workflow_skill,
            lane=lane,
            requires_confirm=requires_confirm,
            examples=examples,
        )
        # Embedding sẽ được lazy-load lần sau
        self._registry.append(entry)
        # Xóa disk cache để force re-embed
        if self._cache_path.exists():
            try:
                data = json.loads(self._cache_path.read_text())
                data[skill_id] = None  # mark for re-embed
                self._cache_path.write_text(json.dumps(data))
            except Exception:
                pass
        _vlog("📚", f"SemanticRouter: registered skill '{skill_id}'")

    def stats(self) -> dict:
        """Thống kê để debug."""
        return {
            "total_calls": self._call_count,
            "cache_hits":  self._cache_hits,
            "cache_rate":  f"{self._cache_hits/max(self._call_count,1):.0%}",
            "skills":      len(self._registry),
            "embeddings_ready": self._embeddings_ready,
        }

    async def ensure_embeddings(self) -> bool:
        """
        Pre-compute embeddings cho tất cả skills.
        Gọi lúc startup (background task).
        Returns True nếu thành công.
        """
        if self._embeddings_ready:
            return True

        any_embedded = False
        for skill in self._registry:
            if skill.has_embeddings:
                any_embedded = True
                continue
            # Embed từng example
            vecs = []
            for example in skill.examples:
                vec = await self._embed(example)
                if vec:
                    vecs.append(vec)
                    await __import__("asyncio").sleep(0.01)  # yield
            if vecs:
                skill.embeddings = vecs
                any_embedded = True
                _log.debug("SemanticRouter: embedded %d examples for '%s'",
                           len(vecs), skill.skill_id)

        if any_embedded:
            self._save_cached_embeddings()
            self._embeddings_ready = True
        return any_embedded

    # ── Internal ──────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float] | None:
        """
        Gọi nomic-embed-text qua Ollama API.
        Fail fast: timeout 3s, trả None nếu unavailable.
        """
        try:
            import urllib.request
            import asyncio

            payload = json.dumps({
                "model": self.EMBED_MODEL,
                "prompt": text[:512],
            }).encode()

            def _call():
                req = urllib.request.Request(
                    self.EMBED_URL,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=int(self.EMBED_TIMEOUT)) as r:
                    return json.loads(r.read().decode())

            result = await asyncio.wait_for(
                asyncio.to_thread(_call),
                timeout=self.EMBED_TIMEOUT + 1,
            )
            vec = result.get("embedding", [])
            return _normalize(vec) if vec else None

        except Exception:
            return None  # Ollama unavailable — không log vì expected in fallback

    def _load_cached_embeddings(self) -> None:
        """Load embeddings từ disk cache."""
        if not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text())
            for skill in self._registry:
                if skill.skill_id in data and data[skill.skill_id]:
                    skill.embeddings = data[skill.skill_id]
            embedded = sum(1 for s in self._registry if s.has_embeddings)
            if embedded > 0:
                self._embeddings_ready = True
                _vlog("📦", f"SemanticRouter: loaded {embedded}/{len(self._registry)} "
                      f"skill embeddings from cache")
        except Exception as exc:
            _log.debug("SemanticRouter: cache load error: %s", exc)

    def _save_cached_embeddings(self) -> None:
        """Lưu embeddings ra disk để dùng lại."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {s.skill_id: s.embeddings for s in self._registry}
            self._cache_path.write_text(json.dumps(data))
        except Exception as exc:
            _log.debug("SemanticRouter: cache save error: %s", exc)
