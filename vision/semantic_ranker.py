# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/semantic_ranker.py — Phidipus v2.3 Phase 3
═══════════════════════════════════════════════════════════════════════

Deep Semantic Ranking — Florence-2 merged rank + score.

Phase 3 upgrades over basic florence_engine.py:
  1. Multi-level matching: exact → fuzzy → semantic (cascade)
  2. Batch crop processing (all Top-K crops in 1 pass)
  3. Description quality scoring (penalize vague descriptions)
  4. Vietnamese + English bilingual keyword matching
  5. Context-aware scoring (button position, toolbar, sidebar)
  6. Integration with ClickMemory descriptions for re-identification

Pipeline:
  DINO Top-K detections
    → batch crop all K regions
    → Florence describe each crop (or heuristic fallback)
    → multi-level match query vs description
    → context-aware score boost/penalty
    → sort by combined semantic score
    → return RankedCandidate list

Process: orchestrator (L1)

Security invariants:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

from vision.detection_types import (
    BBox, Detection, RankedCandidate,
    FLORENCE_SCORE_THRESHOLD,
)


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Text Normalization — Vietnamese + English
# ══════════════════════════════════════════════════════════════════

_STOP_WORDS = frozenset({
    # English
    "the", "a", "an", "in", "on", "at", "to", "of", "for", "is", "are",
    "and", "or", "with", "by", "from", "this", "that", "it", "its",
    # Vietnamese
    "là", "của", "và", "cho", "từ", "với", "trong", "trên", "tại",
    "bằng", "để", "được", "có", "một", "các", "những",
})

# UI keywords with high importance for matching
_UI_KEYWORDS = frozenset({
    # English
    "button", "icon", "menu", "tab", "link", "input", "field", "text",
    "textarea", "checkbox", "radio", "select", "dropdown", "modal",
    "dialog", "popup", "toolbar", "sidebar", "header", "footer",
    "submit", "post", "send", "upload", "download", "save", "delete",
    "close", "cancel", "confirm", "ok", "next", "back", "previous",
    "search", "login", "logout", "signup", "register", "share",
    "like", "comment", "reply", "edit", "copy", "paste", "cut",
    # Vietnamese
    "nút", "đăng", "gửi", "tải", "lưu", "xóa", "đóng", "hủy",
    "xác nhận", "tiếp", "quay", "tìm", "đăng nhập", "đăng xuất",
    "chia sẻ", "thích", "bình luận", "trả lời", "sửa",
    "nhập", "chọn", "mở",
})

# Button type synonyms for cross-matching
_SYNONYMS: dict[str, list[str]] = {
    "post": ["đăng", "publish", "share", "submit", "đăng bài"],
    "send": ["gửi", "submit", "deliver"],
    "upload": ["tải lên", "attach", "add photo", "chọn ảnh", "add file"],
    "save": ["lưu", "download", "tải về"],
    "delete": ["xóa", "remove", "trash", "hủy"],
    "close": ["đóng", "x", "cancel", "dismiss", "hủy bỏ"],
    "search": ["tìm kiếm", "tìm", "find", "lookup"],
    "login": ["đăng nhập", "sign in", "log in"],
    "next": ["tiếp theo", "continue", "proceed", "tiếp tục"],
    "back": ["quay lại", "previous", "trở về"],
    "edit": ["sửa", "modify", "chỉnh sửa"],
    "confirm": ["xác nhận", "ok", "agree", "accept", "đồng ý"],
}


def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, strip accents option, clean."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _tokenize(text: str) -> list[str]:
    """Tokenize and remove stop words."""
    words = _normalize(text).split()
    return [w for w in words if w not in _STOP_WORDS and len(w) > 1]


def _strip_accents(text: str) -> str:
    """Remove Vietnamese diacritics for fuzzy matching."""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


# ══════════════════════════════════════════════════════════════════
# Multi-Level Matching
# ══════════════════════════════════════════════════════════════════

def exact_match(query: str, label: str) -> float:
    """Exact substring match (case-insensitive). Returns 1.0 or 0.0."""
    q = _normalize(query)
    l = _normalize(label)
    if q in l or l in q:
        return 1.0
    # Try without accents
    if _strip_accents(q) in _strip_accents(l):
        return 0.9
    return 0.0


def fuzzy_match(query: str, label: str) -> float:
    """
    Jaccard + keyword coverage fuzzy match.

    Score = 0.5 × Jaccard(q_words, l_words)
          + 0.3 × KeywordCoverage(q_keywords ∩ l_words)
          + 0.2 × SynonymMatch(q, l)
    """
    q_words = set(_tokenize(query))
    l_words = set(_tokenize(label))

    if not q_words:
        return 0.0

    # Jaccard similarity
    intersection = q_words & l_words
    union = q_words | l_words
    jaccard = len(intersection) / len(union) if union else 0.0

    # Keyword coverage (UI-specific keywords)
    q_keys = q_words & _UI_KEYWORDS
    key_coverage = 0.0
    if q_keys:
        matched_keys = q_keys & l_words
        key_coverage = len(matched_keys) / len(q_keys)

    # Synonym match
    synonym_score = _synonym_match_score(query, label)

    return 0.5 * jaccard + 0.3 * key_coverage + 0.2 * synonym_score


def _synonym_match_score(query: str, label: str) -> float:
    """Check if query and label share synonym groups."""
    q_norm = _normalize(query)
    l_norm = _normalize(label)

    for _base, synonyms in _SYNONYMS.items():
        all_forms = [_base] + synonyms
        q_match = any(s in q_norm for s in all_forms)
        l_match = any(s in l_norm for s in all_forms)
        if q_match and l_match:
            return 1.0

    return 0.0


def semantic_match(query_embedding: list[float], desc_embedding: list[float]) -> float:
    """Cosine similarity between embeddings."""
    if not query_embedding or not desc_embedding:
        return 0.0
    if len(query_embedding) != len(desc_embedding):
        return 0.0

    dot = sum(a * b for a, b in zip(query_embedding, desc_embedding))
    norm_a = math.sqrt(sum(a * a for a in query_embedding))
    norm_b = math.sqrt(sum(b * b for b in desc_embedding))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, dot / (norm_a * norm_b))


def multi_level_match(
    query: str,
    description: str,
    query_embedding: Optional[list[float]] = None,
    desc_embedding: Optional[list[float]] = None,
) -> tuple[float, str]:
    """
    Cascade matching: exact → fuzzy → semantic.

    Returns (score, method) where method indicates which level matched.
    Early exit on high-confidence match.
    """
    # Level 1: Exact match
    ex = exact_match(query, description)
    if ex >= 0.9:
        return (min(1.0, ex + 0.05), "exact")

    # Level 2: Fuzzy match
    fz = fuzzy_match(query, description)
    if fz >= 0.7:
        return (fz, "fuzzy")

    # Level 3: Semantic match (embedding-based)
    if query_embedding and desc_embedding:
        sm = semantic_match(query_embedding, desc_embedding)
        best = max(fz, sm)
        method = "semantic" if sm > fz else "fuzzy"
        return (best, method)

    return (fz, "fuzzy")


# ══════════════════════════════════════════════════════════════════
# Description Quality Scoring
# ══════════════════════════════════════════════════════════════════

def description_quality(desc: str) -> float:
    """
    Score description quality (0.0–1.0).

    Penalizes vague or heuristic-generated descriptions.
    Rewards specific, UI-relevant descriptions.
    """
    if not desc:
        return 0.0

    words = desc.split()
    score = 0.5  # neutral start

    # Length: too short = vague, too long = noisy
    if len(words) < 3:
        score -= 0.2
    elif 3 <= len(words) <= 12:
        score += 0.2
    elif len(words) > 20:
        score -= 0.1

    # Contains UI keywords
    desc_lower = desc.lower()
    ui_hits = sum(1 for kw in _UI_KEYWORDS if kw in desc_lower)
    score += min(0.3, ui_hits * 0.1)

    # Contains specific identifiers (aria-label, text content)
    if any(c in desc_lower for c in ["'", '"', "labeled", "titled", "text"]):
        score += 0.1

    # Penalize clearly heuristic descriptions
    if any(h in desc_lower for h in ["small", "medium", "large", "rectangular", "square"]):
        score -= 0.15  # likely from heuristic fallback

    return max(0.0, min(1.0, score))


# ══════════════════════════════════════════════════════════════════
# Context-Aware Position Scoring
# ══════════════════════════════════════════════════════════════════

def position_context_score(
    bbox: BBox,
    query: str,
    window_bounds: Optional[tuple[int, int, int, int]] = None,
) -> float:
    """
    Score based on element position relative to expected UI location.

    Buttons: typically right side, bottom of forms
    Search: typically top, center
    Menu: typically top or left
    Close/Cancel: typically top-right
    Upload: typically center, in forms
    """
    if not window_bounds:
        return 0.5  # neutral

    wx, wy, ww, wh = window_bounds
    if ww == 0 or wh == 0:
        return 0.5

    cx, cy = bbox.center
    rx = (cx - wx) / ww  # relative x (0.0–1.0)
    ry = (cy - wy) / wh  # relative y (0.0–1.0)

    score = 0.5
    q_lower = _normalize(query)

    # Submit/Post/Send → typically bottom-right or right
    if any(kw in q_lower for kw in ["submit", "post", "send", "đăng", "gửi"]):
        if rx > 0.6:
            score += 0.15
        if ry > 0.5:
            score += 0.10

    # Close/Cancel → typically top-right or bottom-left
    elif any(kw in q_lower for kw in ["close", "cancel", "đóng", "hủy", "x"]):
        if rx > 0.8 and ry < 0.2:
            score += 0.20  # top-right X button
        elif rx < 0.4 and ry > 0.7:
            score += 0.10  # bottom-left Cancel

    # Search → typically top, center or right
    elif any(kw in q_lower for kw in ["search", "tìm"]):
        if ry < 0.15:
            score += 0.15  # top toolbar
        if 0.3 < rx < 0.8:
            score += 0.10  # center-ish

    # Menu → typically top or left
    elif any(kw in q_lower for kw in ["menu", "hamburger", "navigation"]):
        if rx < 0.15 or ry < 0.1:
            score += 0.15

    # Upload → typically center
    elif any(kw in q_lower for kw in ["upload", "attach", "tải", "add photo"]):
        if 0.2 < rx < 0.8 and 0.2 < ry < 0.8:
            score += 0.10

    # Element size appropriateness
    w, h = bbox.width, bbox.height
    if 30 <= w <= 250 and 20 <= h <= 70:
        score += 0.05  # typical button size

    return max(0.0, min(1.0, score))


# ══════════════════════════════════════════════════════════════════
# Embedding Cache (shared, upgraded)
# ══════════════════════════════════════════════════════════════════

class EmbeddingCache:
    """
    LRU cache for text/visual embeddings.

    Shared across SemanticRanker calls.
    Memory: 500 × 768 × 4 bytes ≈ 1.5MB.
    """

    def __init__(self, max_size: int = 500) -> None:
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[list[float]]:
        k = key.lower().strip()
        if k in self._cache:
            self._cache.move_to_end(k)
            self._hits += 1
            return self._cache[k]
        self._misses += 1
        return None

    def put(self, key: str, embedding: list[float]) -> None:
        k = key.lower().strip()
        self._cache[k] = embedding
        self._cache.move_to_end(k)
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    def stats(self) -> dict[str, Any]:
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 3),
        }


# Module-level shared cache
_embed_cache = EmbeddingCache(max_size=500)


def get_embed_cache() -> EmbeddingCache:
    return _embed_cache


# ══════════════════════════════════════════════════════════════════
# SemanticRanker
# ══════════════════════════════════════════════════════════════════

class SemanticRanker:
    """
    Deep semantic ranking engine — Phase 3.

    Upgrades over Phase 1-2 basic ranking:
      - Multi-level matching cascade (exact → fuzzy → semantic)
      - Description quality scoring
      - Position context scoring
      - Synonym-aware matching (Vietnamese + English)
      - Integration with ClickMemory descriptions for re-identification

    Usage:
        ranker = SemanticRanker(florence_engine)
        candidates = await ranker.rank(
            image_bytes=png,
            detections=dino_top_k,
            query="Post button",
            window_bounds=(0, 80, 1440, 900),
        )
    """

    def __init__(self, florence_engine: Any) -> None:
        """
        Args:
          florence_engine: FlorenceEngine instance from Phase 1-2.
        """
        self._florence = florence_engine
        self._cache = _embed_cache
        self._total_calls: int = 0
        self._total_exact: int = 0
        self._total_fuzzy: int = 0
        self._total_semantic: int = 0

    async def rank(
        self,
        image_bytes: bytes,
        detections: list[Detection],
        query: str,
        window_bounds: Optional[tuple[int, int, int, int]] = None,
        click_memory_desc: Optional[str] = None,
    ) -> list[RankedCandidate]:
        """
        Rank DINO detections by semantic relevance.

        Returns RankedCandidate list sorted by semantic_score descending.
        """
        if not detections:
            return []

        self._total_calls += 1
        t0 = time.perf_counter()

        # Get descriptions for all detections (batch via Florence or heuristic)
        descriptions = await self._batch_describe(image_bytes, detections)

        # Compute query embedding
        query_emb = self._cache.get(query)
        if query_emb is None:
            query_emb = self._text_embedding(query)
            self._cache.put(query, query_emb)

        # Rank each detection
        candidates = []
        for i, det in enumerate(detections):
            desc = descriptions[i] if i < len(descriptions) else ""

            # Multi-level match
            desc_emb = self._cache.get(desc)
            if desc_emb is None:
                desc_emb = self._text_embedding(desc)
                self._cache.put(desc, desc_emb)

            score, method = multi_level_match(query, desc, query_emb, desc_emb)

            # Track match methods
            if method == "exact":
                self._total_exact += 1
            elif method == "fuzzy":
                self._total_fuzzy += 1
            else:
                self._total_semantic += 1

            # ClickMemory re-identification boost
            if click_memory_desc:
                cm_score, _ = multi_level_match(click_memory_desc, desc)
                if cm_score > 0.7:
                    score = min(1.0, score + 0.1)

            # Description quality modifier
            dq = description_quality(desc)
            score = score * (0.7 + 0.3 * dq)  # quality scales 70–100% of score

            # Position context score
            pos_score = position_context_score(det.bbox, query, window_bounds)

            candidates.append(RankedCandidate(
                detection=det,
                semantic_score=score,
                description=desc,
                heuristic_score=pos_score,
            ))

        # Sort by semantic score descending
        candidates.sort(key=lambda c: c.semantic_score, reverse=True)

        latency = (time.perf_counter() - t0) * 1000
        if candidates:
            _vlog("🏆", f"SemanticRanker: {len(candidates)} ranked, "
                        f"best={candidates[0].semantic_score:.3f} "
                        f"({latency:.0f}ms)")

        return candidates

    async def _batch_describe(
        self,
        image_bytes: bytes,
        detections: list[Detection],
    ) -> list[str]:
        """
        Get descriptions for all detections.

        Uses FlorenceEngine.describe() for each crop.
        Runs concurrently for speed.
        """
        if not self._florence or not self._florence.ready:
            # Fallback: heuristic descriptions
            return [self._florence._describe_heuristic(d.bbox) if self._florence
                    else f"element at ({d.bbox.center_x},{d.bbox.center_y})"
                    for d in detections]

        # Concurrent Florence describe calls
        tasks = [
            self._florence.describe(image_bytes, det.bbox)
            for det in detections
        ]
        descriptions = await asyncio.gather(*tasks, return_exceptions=True)

        result = []
        for desc in descriptions:
            if isinstance(desc, Exception):
                result.append("")
            else:
                result.append(str(desc))

        return result

    def _text_embedding(self, text: str, dim: int = 768) -> list[float]:
        """
        Generate text embedding using improved word-level hashing.

        Better than Phase 1-2: uses word position + character n-grams
        for more distinctive embeddings.
        """
        words = _tokenize(text)
        if not words:
            words = text.lower().split()

        embedding = [0.0] * dim

        for i, word in enumerate(words):
            # Word-level hash
            h = hashlib.sha256(word.encode('utf-8')).digest()
            weight = 2.0 if word in _UI_KEYWORDS else 1.0

            for j in range(min(dim, 32)):
                idx = (i * 37 + j * 13) % dim
                embedding[idx] += weight * (h[j] - 128) / 128.0

            # Character bigram hashes for fuzzy coverage
            for k in range(len(word) - 1):
                bigram = word[k:k+2]
                bh = hashlib.md5(bigram.encode()).digest()
                for j in range(min(dim, 16)):
                    idx2 = (i * 53 + j * 7 + k * 19) % dim
                    embedding[idx2] += 0.3 * (bh[j] - 128) / 128.0

        # Normalize to unit vector
        norm = math.sqrt(sum(x * x for x in embedding))
        if norm > 0:
            embedding = [x / norm for x in embedding]

        return embedding

    def stats(self) -> dict[str, Any]:
        total_matched = self._total_exact + self._total_fuzzy + self._total_semantic
        return {
            "total_calls": self._total_calls,
            "match_methods": {
                "exact": self._total_exact,
                "fuzzy": self._total_fuzzy,
                "semantic": self._total_semantic,
                "total": total_matched,
            },
            "embed_cache": self._cache.stats(),
        }
