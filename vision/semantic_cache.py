# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/semantic_cache.py — Phidipus v2.3 Phase 6C
═══════════════════════════════════════════════════════════════════════

Semantic Cache — Visual embedding + anchor relations for ClickMemory.

Mở rộng ClickMemory hiện tại từ Spatial Cache → Semantic Cache:

  Spatial Cache (cũ):
    Nhớ (rx, ry) tọa độ tương đối → hỏng khi resize/responsive layout.

  Semantic Cache (mới):
    Nhớ (rx, ry) + visual_embedding + anchor_relations + description.
    Khi window resize → verify bằng visual embedding cosine > 0.85.
    Khi spatial miss → tìm theo visual anchor proximity.

3 tầng cache:

  Tier 0: Spatial Match (0ms)
    (domain, action_key) → (rx, ry) trực tiếp từ ClickMemory.
    Nếu hit và window chưa resize → dùng ngay.

  Tier 1: Visual Verify (<5ms)
    Spatial hit NHƯNG window đã resize.
    → Lấy visual_embedding từ cache → so sánh với embedding vùng (rx,ry) mới.
    → Cosine > 0.85 → spatial vẫn đúng → dùng.
    → Cosine < 0.85 → spatial hỏng → fallback Tier 2.

  Tier 2: Anchor Proximity (<10ms)
    Spatial hỏng sau resize.
    → Tìm visual anchor gần nhất (logo, navigation bar) bằng embedding.
    → Tính position tương đối từ anchor → coordinate mới.

Storage: data/memory/semantic_cache.json
Memory: ~500 entries × (768 floats + metadata) ≈ 3MB

Process: orchestrator (L1)

Security invariants:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data Types
# ══════════════════════════════════════════════════════════════════

@dataclass
class VisualAnchor:
    """A stable visual reference point on a page (logo, nav bar, etc.)."""
    label: str                      # "facebook_logo", "navigation_bar"
    rx: float                       # relative x (0–1) within window
    ry: float                       # relative y (0–1) within window
    embedding: list[float]          # 768-dim visual embedding (truncated to 128 for storage)
    confidence: float = 0.9
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "rx": round(self.rx, 4),
            "ry": round(self.ry, 4),
            "embedding": [round(v, 4) for v in self.embedding[:128]],
            "confidence": round(self.confidence, 3),
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict) -> VisualAnchor:
        return cls(
            label=d.get("label", ""),
            rx=d.get("rx", 0.5),
            ry=d.get("ry", 0.5),
            embedding=d.get("embedding", []),
            confidence=d.get("confidence", 0.9),
            last_seen=d.get("last_seen", 0.0),
        )


@dataclass
class SemanticEntry:
    """Extended ClickMemory entry with visual embedding and anchors."""
    domain: str
    action_key: str
    rx: float                       # spatial cache (relative x)
    ry: float                       # spatial cache (relative y)
    visual_embedding: list[float]   # 768-dim → stored as 128-dim truncated
    description: str = ""           # Florence description of element
    anchor_label: str = ""          # nearest visual anchor label
    anchor_dx: float = 0.0         # delta x from anchor (relative)
    anchor_dy: float = 0.0         # delta y from anchor (relative)
    window_w: int = 1440           # window width at creation time
    window_h: int = 900            # window height at creation time
    hit_count: int = 0
    success_rate: float = 1.0
    created_at: float = 0.0
    last_used: float = 0.0

    @property
    def age_days(self) -> float:
        return (time.time() - self.created_at) / 86400 if self.created_at else 0

    @property
    def is_stale(self) -> bool:
        return self.age_days > 14  # 2 weeks

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "action_key": self.action_key,
            "rx": round(self.rx, 4),
            "ry": round(self.ry, 4),
            "visual_embedding": [round(v, 4) for v in self.visual_embedding[:128]],
            "description": self.description,
            "anchor_label": self.anchor_label,
            "anchor_dx": round(self.anchor_dx, 4),
            "anchor_dy": round(self.anchor_dy, 4),
            "window_w": self.window_w,
            "window_h": self.window_h,
            "hit_count": self.hit_count,
            "success_rate": round(self.success_rate, 4),
            "created_at": self.created_at,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SemanticEntry:
        return cls(
            domain=d.get("domain", ""),
            action_key=d.get("action_key", ""),
            rx=d.get("rx", 0.5),
            ry=d.get("ry", 0.5),
            visual_embedding=d.get("visual_embedding", []),
            description=d.get("description", ""),
            anchor_label=d.get("anchor_label", ""),
            anchor_dx=d.get("anchor_dx", 0.0),
            anchor_dy=d.get("anchor_dy", 0.0),
            window_w=d.get("window_w", 1440),
            window_h=d.get("window_h", 900),
            hit_count=d.get("hit_count", 0),
            success_rate=d.get("success_rate", 1.0),
            created_at=d.get("created_at", 0.0),
            last_used=d.get("last_used", 0.0),
        )


@dataclass
class CacheLookupResult:
    """Result of semantic cache lookup."""
    hit: bool = False
    rx: float = 0.5
    ry: float = 0.5
    confidence: float = 0.0
    tier: str = "miss"             # "spatial" | "visual_verify" | "anchor" | "miss"
    description: str = ""
    entry: Optional[SemanticEntry] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hit": self.hit,
            "rx": round(self.rx, 4),
            "ry": round(self.ry, 4),
            "confidence": round(self.confidence, 3),
            "tier": self.tier,
        }


# ══════════════════════════════════════════════════════════════════
# Cosine Similarity (truncated embeddings)
# ══════════════════════════════════════════════════════════════════

def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between truncated embedding vectors."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(n))
    norm_a = math.sqrt(sum(a[i] ** 2 for i in range(n)))
    norm_b = math.sqrt(sum(b[i] ** 2 for i in range(n)))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ══════════════════════════════════════════════════════════════════
# SemanticCache
# ══════════════════════════════════════════════════════════════════

_STORAGE_PATH = Path(__file__).parent.parent / "data" / "memory" / "semantic_cache.json"


class SemanticCache:
    """
    Visual embedding cache for resize-resilient element re-identification.

    Usage:
        cache = SemanticCache()

        # Store after successful click
        cache.store(
            domain="facebook.com",
            action_key="post_button",
            rx=0.82, ry=0.75,
            visual_embedding=florence_embedding,
            description="Blue Post button in top-right toolbar",
            window_w=1440, window_h=900,
        )

        # Lookup before detection
        result = cache.lookup(
            domain="facebook.com",
            action_key="post_button",
            current_window_w=1280,  # window resized!
            current_window_h=800,
            current_region_embedding=new_embedding_at_cached_pos,
        )
        if result.hit:
            click_at(result.rx * window_w, result.ry * window_h)
    """

    VISUAL_VERIFY_THRESHOLD = 0.80   # cosine sim for visual verify
    ANCHOR_MATCH_THRESHOLD = 0.75    # cosine sim for anchor matching
    MAX_ENTRIES = 500
    MAX_ANCHORS_PER_DOMAIN = 10

    def __init__(self, storage_path: Path = _STORAGE_PATH) -> None:
        self._path = storage_path
        self._entries: dict[str, dict[str, SemanticEntry]] = {}  # domain → action_key → entry
        self._anchors: dict[str, list[VisualAnchor]] = {}        # domain → list of anchors
        self._total_lookups: int = 0
        self._tier_hits: dict[str, int] = {
            "spatial": 0, "visual_verify": 0, "anchor": 0, "miss": 0,
        }
        self._load()

    # ── Store ─────────────────────────────────────────────────

    def store(
        self,
        domain: str,
        action_key: str,
        rx: float,
        ry: float,
        visual_embedding: list[float],
        description: str = "",
        anchor_label: str = "",
        anchor_dx: float = 0.0,
        anchor_dy: float = 0.0,
        window_w: int = 1440,
        window_h: int = 900,
    ) -> None:
        """Store or update a semantic cache entry."""
        if domain not in self._entries:
            self._entries[domain] = {}

        existing = self._entries[domain].get(action_key)
        now = time.time()

        if existing:
            # Update existing entry
            existing.rx = rx
            existing.ry = ry
            existing.visual_embedding = visual_embedding[:128]
            existing.description = description or existing.description
            existing.anchor_label = anchor_label or existing.anchor_label
            existing.anchor_dx = anchor_dx
            existing.anchor_dy = anchor_dy
            existing.window_w = window_w
            existing.window_h = window_h
            existing.hit_count += 1
            existing.last_used = now
        else:
            self._entries[domain][action_key] = SemanticEntry(
                domain=domain,
                action_key=action_key,
                rx=rx, ry=ry,
                visual_embedding=visual_embedding[:128],
                description=description,
                anchor_label=anchor_label,
                anchor_dx=anchor_dx,
                anchor_dy=anchor_dy,
                window_w=window_w,
                window_h=window_h,
                created_at=now,
                last_used=now,
            )

        self._enforce_limits()
        self._save()

    def store_anchor(
        self,
        domain: str,
        label: str,
        rx: float,
        ry: float,
        embedding: list[float],
    ) -> None:
        """Store a visual anchor (logo, nav bar, etc.)."""
        if domain not in self._anchors:
            self._anchors[domain] = []

        # Update existing or add new
        for anchor in self._anchors[domain]:
            if anchor.label == label:
                anchor.rx = rx
                anchor.ry = ry
                anchor.embedding = embedding[:128]
                anchor.last_seen = time.time()
                self._save()
                return

        self._anchors[domain].append(VisualAnchor(
            label=label, rx=rx, ry=ry,
            embedding=embedding[:128],
            last_seen=time.time(),
        ))

        # Limit anchors per domain
        if len(self._anchors[domain]) > self.MAX_ANCHORS_PER_DOMAIN:
            self._anchors[domain].sort(key=lambda a: a.last_seen, reverse=True)
            self._anchors[domain] = self._anchors[domain][:self.MAX_ANCHORS_PER_DOMAIN]

        self._save()

    # ── Lookup ────────────────────────────────────────────────

    def lookup(
        self,
        domain: str,
        action_key: str,
        current_window_w: int = 1440,
        current_window_h: int = 900,
        current_region_embedding: Optional[list[float]] = None,
    ) -> CacheLookupResult:
        """
        3-tier semantic cache lookup.

        Tier 0: Spatial match (same window size → rx,ry still valid)
        Tier 1: Visual verify (window resized → check embedding at cached position)
        Tier 2: Anchor proximity (spatial failed → find via nearest anchor)
        """
        self._total_lookups += 1

        entry = self._get_entry(domain, action_key)
        if entry is None:
            self._tier_hits["miss"] += 1
            return CacheLookupResult(tier="miss")

        if entry.is_stale:
            self._tier_hits["miss"] += 1
            return CacheLookupResult(tier="miss")

        # ── Tier 0: Spatial Match ─────────────────────────────
        size_unchanged = (
            abs(current_window_w - entry.window_w) < 20
            and abs(current_window_h - entry.window_h) < 20
        )

        if size_unchanged:
            self._tier_hits["spatial"] += 1
            entry.hit_count += 1
            entry.last_used = time.time()
            return CacheLookupResult(
                hit=True, rx=entry.rx, ry=entry.ry,
                confidence=min(1.0, 0.85 + entry.success_rate * 0.1),
                tier="spatial",
                description=entry.description,
                entry=entry,
            )

        # ── Tier 1: Visual Verify ─────────────────────────────
        if current_region_embedding and entry.visual_embedding:
            sim = _cosine_sim(current_region_embedding, entry.visual_embedding)

            if sim >= self.VISUAL_VERIFY_THRESHOLD:
                self._tier_hits["visual_verify"] += 1
                entry.hit_count += 1
                entry.last_used = time.time()
                # Update window size for next time
                entry.window_w = current_window_w
                entry.window_h = current_window_h
                _vlog("🔍", f"SemanticCache visual verify: {domain}:{action_key} "
                             f"sim={sim:.3f} → hit")
                return CacheLookupResult(
                    hit=True, rx=entry.rx, ry=entry.ry,
                    confidence=sim,
                    tier="visual_verify",
                    description=entry.description,
                    entry=entry,
                )

        # ── Tier 2: Anchor Proximity ──────────────────────────
        if entry.anchor_label and domain in self._anchors:
            for anchor in self._anchors[domain]:
                if anchor.label == entry.anchor_label:
                    # Found the anchor — compute new position relative to it
                    new_rx = anchor.rx + entry.anchor_dx
                    new_ry = anchor.ry + entry.anchor_dy
                    new_rx = max(0.0, min(1.0, new_rx))
                    new_ry = max(0.0, min(1.0, new_ry))

                    self._tier_hits["anchor"] += 1
                    entry.hit_count += 1
                    entry.last_used = time.time()
                    _vlog("⚓", f"SemanticCache anchor proximity: {domain}:{action_key} "
                                 f"via {anchor.label} → ({new_rx:.3f},{new_ry:.3f})")
                    return CacheLookupResult(
                        hit=True, rx=new_rx, ry=new_ry,
                        confidence=anchor.confidence * 0.8,
                        tier="anchor",
                        description=entry.description,
                        entry=entry,
                    )

        self._tier_hits["miss"] += 1
        return CacheLookupResult(tier="miss")

    def record_outcome(self, domain: str, action_key: str, success: bool) -> None:
        """Record click outcome to update success_rate."""
        entry = self._get_entry(domain, action_key)
        if entry:
            n = entry.hit_count
            if n > 0:
                entry.success_rate = (entry.success_rate * (n - 1) + (1.0 if success else 0.0)) / n
            self._save()

    # ── Internal ──────────────────────────────────────────────

    def _get_entry(self, domain: str, action_key: str) -> Optional[SemanticEntry]:
        return self._entries.get(domain, {}).get(action_key)

    def _enforce_limits(self) -> None:
        """Remove oldest entries if over MAX_ENTRIES."""
        total = sum(len(v) for v in self._entries.values())
        if total <= self.MAX_ENTRIES:
            return

        # Flatten, sort by last_used, keep newest
        all_entries = []
        for domain, actions in self._entries.items():
            for ak, entry in actions.items():
                all_entries.append(entry)
        all_entries.sort(key=lambda e: e.last_used, reverse=True)

        # Rebuild
        self._entries = {}
        for entry in all_entries[:self.MAX_ENTRIES]:
            if entry.domain not in self._entries:
                self._entries[entry.domain] = {}
            self._entries[entry.domain][entry.action_key] = entry

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for domain, actions in data.get("entries", {}).items():
                    self._entries[domain] = {}
                    for ak, d in actions.items():
                        self._entries[domain][ak] = SemanticEntry.from_dict(d)
                for domain, anchors in data.get("anchors", {}).items():
                    self._anchors[domain] = [VisualAnchor.from_dict(a) for a in anchors]
        except Exception:
            self._entries = {}
            self._anchors = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "entries": {
                    domain: {ak: e.to_dict() for ak, e in actions.items()}
                    for domain, actions in self._entries.items()
                },
                "anchors": {
                    domain: [a.to_dict() for a in anchors]
                    for domain, anchors in self._anchors.items()
                },
            }
            self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        except Exception:
            pass

    def stats(self) -> dict[str, Any]:
        total_entries = sum(len(v) for v in self._entries.values())
        total_anchors = sum(len(v) for v in self._anchors.values())
        return {
            "total_entries": total_entries,
            "total_anchors": total_anchors,
            "total_lookups": self._total_lookups,
            "tier_hits": dict(self._tier_hits),
            "domains": len(self._entries),
            "hit_rate": round(
                (self._tier_hits["spatial"] + self._tier_hits["visual_verify"]
                 + self._tier_hits["anchor"]) / max(1, self._total_lookups), 3
            ),
        }


# Module singleton
_instance: Optional[SemanticCache] = None

def get_semantic_cache() -> SemanticCache:
    global _instance
    if _instance is None:
        _instance = SemanticCache()
    return _instance
