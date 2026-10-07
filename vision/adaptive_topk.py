# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/adaptive_topk.py — Phidipus v2.3 Phase 4
═══════════════════════════════════════════════════════════════════════

Adaptive Top-K Selection — dynamic K based on UI complexity + domain learning.

Phase 4 upgrades:
  1. Multi-signal complexity estimation (not just detection count)
  2. Domain-specific K learning (Facebook → K=5, Login → K=2)
  3. Early-exit optimization (skip Florence when DINO very confident)
  4. Dynamic DINO input resolution tied to complexity
  5. Complexity cache per URL pattern (don't re-estimate same page)

Complexity signals:
  ┌──────────────────────────────────────────────────────────┐
  │ Signal                    │ Weight │ Source              │
  │ Total raw DINO detections │ 0.40   │ DINO output         │
  │ Detection density         │ 0.20   │ dets / screen area  │
  │ Size variance             │ 0.20   │ std(bbox areas)     │
  │ Domain pattern            │ 0.20   │ learned from history │
  └──────────────────────────────────────────────────────────┘

Domain learning:
  After each workflow, record (domain, K_used, success).
  Over time, learn optimal K per domain/page_type.
  Storage: data/memory/topk_learning.json

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

from vision.detection_types import (
    BBox, Detection, UIComplexity,
    TOPK_SIMPLE, TOPK_NORMAL, TOPK_COMPLEX,
    COMPLEXITY_SIMPLE_MAX, COMPLEXITY_COMPLEX_MIN,
    DINO_INPUT_SIMPLE, DINO_INPUT_NORMAL, DINO_INPUT_COMPLEX,
)


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Complexity Analysis
# ══════════════════════════════════════════════════════════════════

@dataclass
class ComplexityAnalysis:
    """Result of UI complexity estimation."""
    complexity: UIComplexity = UIComplexity.NORMAL
    k: int = TOPK_NORMAL
    resolution: tuple[int, int] = DINO_INPUT_NORMAL
    total_detections: int = 0
    detection_density: float = 0.0    # detections per 1000px²
    size_variance: float = 0.0        # normalized variance of bbox areas
    domain_factor: float = 0.5        # learned domain adjustment (0=simple, 1=complex)
    raw_score: float = 0.5            # combined complexity score (0–1)
    early_exit: bool = False          # True if DINO top-1 is very confident
    early_exit_conf: float = 0.0      # confidence that triggered early exit
    source: str = "computed"          # "computed" | "cached" | "domain_learned"

    def to_dict(self) -> dict[str, Any]:
        return {
            "complexity": self.complexity.value,
            "k": self.k,
            "resolution": list(self.resolution),
            "total_detections": self.total_detections,
            "density": round(self.detection_density, 4),
            "size_variance": round(self.size_variance, 3),
            "domain_factor": round(self.domain_factor, 3),
            "raw_score": round(self.raw_score, 3),
            "early_exit": self.early_exit,
            "source": self.source,
        }


def _compute_size_variance(detections: list[Detection]) -> float:
    """Normalized variance of bounding box areas."""
    if len(detections) < 2:
        return 0.0

    areas = [d.bbox.area for d in detections]
    mean_area = sum(areas) / len(areas)
    if mean_area == 0:
        return 0.0

    variance = sum((a - mean_area) ** 2 for a in areas) / len(areas)
    # Normalize by mean² to get coefficient of variation squared
    cv_sq = variance / (mean_area ** 2)
    # Map to 0–1 range (cv² > 4 → 1.0)
    return min(1.0, cv_sq / 4.0)


def _compute_detection_density(
    detections: list[Detection],
    screen_area: int = 1440 * 900,
) -> float:
    """Detections per 1000 pixels²."""
    if screen_area <= 0:
        return 0.0
    return len(detections) / (screen_area / 1000.0)


def _compute_image_entropy(image_bytes: bytes) -> float:
    """
    v2.3.1: Image entropy as complexity signal (Grok #4 feedback).

    Higher entropy = more visual complexity = more UI elements likely.
    Normalized to 0.0–1.0 range (max entropy for 8-bit grayscale ≈ 8.0).

    Uses thumbnail for speed (<2ms).
    """
    try:
        from PIL import Image
        import math as _math

        img = Image.open(__import__("io").BytesIO(image_bytes)).convert("L")
        img = img.resize((64, 64), Image.BILINEAR)
        histogram = img.histogram()
        total = sum(histogram)
        if total == 0:
            return 0.5

        entropy = 0.0
        for count in histogram:
            if count > 0:
                p = count / total
                entropy -= p * _math.log2(p)

        # Normalize: 8.0 = max entropy for 8-bit → 0.0-1.0
        return min(1.0, entropy / 8.0)
    except Exception:
        return 0.5  # neutral default


# ══════════════════════════════════════════════════════════════════
# Domain Learning
# ══════════════════════════════════════════════════════════════════

_LEARNING_PATH = Path(__file__).parent.parent / "data" / "memory" / "topk_learning.json"


@dataclass
class DomainEntry:
    """Learned K stats for a domain."""
    domain: str = ""
    total_success: int = 0
    total_fail: int = 0
    k_sum: int = 0
    k_count: int = 0
    avg_k: float = TOPK_NORMAL
    complexity_sum: float = 0.0
    last_updated: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.total_success + self.total_fail
        return self.total_success / total if total > 0 else 0.5

    @property
    def domain_factor(self) -> float:
        """0.0 = always simple, 1.0 = always complex."""
        if self.k_count == 0:
            return 0.5
        avg = self.complexity_sum / self.k_count
        return max(0.0, min(1.0, avg))

    def record(self, k: int, complexity_score: float, success: bool) -> None:
        self.k_sum += k
        self.k_count += 1
        self.complexity_sum += complexity_score
        self.avg_k = self.k_sum / self.k_count
        if success:
            self.total_success += 1
        else:
            self.total_fail += 1
        self.last_updated = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "avg_k": round(self.avg_k, 1),
            "success_rate": round(self.success_rate, 3),
            "samples": self.k_count,
            "domain_factor": round(self.domain_factor, 3),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DomainEntry:
        entry = cls(domain=d.get("domain", ""))
        entry.total_success = d.get("total_success", 0)
        entry.total_fail = d.get("total_fail", 0)
        entry.k_sum = d.get("k_sum", 0)
        entry.k_count = d.get("k_count", 0)
        entry.avg_k = d.get("avg_k", TOPK_NORMAL)
        entry.complexity_sum = d.get("complexity_sum", 0.0)
        entry.last_updated = d.get("last_updated", 0.0)
        return entry


class DomainLearning:
    """
    Persistent storage for domain-specific K learning.

    Learns over time: "facebook.com is complex → K=5–7"
    """

    def __init__(self, path: Path = _LEARNING_PATH) -> None:
        self._path = path
        self._domains: dict[str, DomainEntry] = {}
        self._load()

    def get(self, domain: str) -> Optional[DomainEntry]:
        return self._domains.get(domain)

    def record(self, domain: str, k: int, complexity_score: float, success: bool) -> None:
        if not domain:
            return
        if domain not in self._domains:
            self._domains[domain] = DomainEntry(domain=domain)
        self._domains[domain].record(k, complexity_score, success)
        self._save()

    def stats(self) -> dict[str, Any]:
        return {
            "domains": len(self._domains),
            "entries": {d: e.to_dict() for d, e in self._domains.items()},
        }

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for domain, d in data.items():
                    self._domains[domain] = DomainEntry.from_dict(d)
        except Exception:
            self._domains = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for domain, entry in self._domains.items():
                d = entry.to_dict()
                d["total_success"] = entry.total_success
                d["total_fail"] = entry.total_fail
                d["k_sum"] = entry.k_sum
                d["k_count"] = entry.k_count
                d["complexity_sum"] = entry.complexity_sum
                d["last_updated"] = entry.last_updated
                data[domain] = d
            self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# URL Pattern Cache
# ══════════════════════════════════════════════════════════════════

class ComplexityCache:
    """
    Cache complexity analysis per URL pattern.

    Avoids re-estimating same page type (e.g., facebook.com/feed is always complex).
    TTL: 5 minutes.
    """

    def __init__(self, ttl_s: float = 300.0, max_entries: int = 50) -> None:
        self._cache: dict[str, tuple[ComplexityAnalysis, float]] = {}
        self._ttl_s = ttl_s
        self._max_entries = max_entries

    def _url_pattern(self, url: str) -> str:
        """Extract URL pattern (domain + path, no query params)."""
        if not url:
            return ""
        # Remove protocol
        url = url.split("://", 1)[-1]
        # Remove query params
        url = url.split("?", 1)[0]
        # Remove trailing slash
        url = url.rstrip("/")
        return url[:100]

    def get(self, url: str) -> Optional[ComplexityAnalysis]:
        pattern = self._url_pattern(url)
        if pattern in self._cache:
            analysis, ts = self._cache[pattern]
            if time.time() - ts < self._ttl_s:
                return ComplexityAnalysis(
                    complexity=analysis.complexity,
                    k=analysis.k,
                    resolution=analysis.resolution,
                    total_detections=analysis.total_detections,
                    raw_score=analysis.raw_score,
                    source="cached",
                )
            else:
                del self._cache[pattern]
        return None

    def put(self, url: str, analysis: ComplexityAnalysis) -> None:
        pattern = self._url_pattern(url)
        if not pattern:
            return
        self._cache[pattern] = (analysis, time.time())
        # Evict oldest if over limit
        while len(self._cache) > self._max_entries:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]


# ══════════════════════════════════════════════════════════════════
# AdaptiveTopK — Main Class
# ══════════════════════════════════════════════════════════════════

_EARLY_EXIT_THRESHOLD = 0.95  # Skip Florence if DINO top-1 this confident


class AdaptiveTopK:
    """
    Smart Top-K selection with domain learning and complexity analysis.

    Usage:
        topk = AdaptiveTopK()
        analysis = topk.analyze(
            detections=dino_raw_detections,
            domain="facebook.com",
            url="facebook.com/feed",
            screen_area=1440*900,
        )
        k = analysis.k
        resolution = analysis.resolution
        if analysis.early_exit:
            # Skip Florence, use DINO top-1 directly
            ...
    """

    def __init__(self) -> None:
        self._learning = DomainLearning()
        self._cache = ComplexityCache()
        self._total_analyses: int = 0
        self._total_early_exits: int = 0

    def analyze(
        self,
        detections: list[Detection],
        domain: str = "",
        url: str = "",
        screen_area: int = 1440 * 900,
        top1_confidence: float = 0.0,
        image_bytes: bytes = b"",
    ) -> ComplexityAnalysis:
        """
        Analyze UI complexity and determine optimal K.

        v2.3.1: Added image entropy signal (Grok #4 feedback).
        """
        self._total_analyses += 1

        # Check URL pattern cache first
        if url:
            cached = self._cache.get(url)
            if cached is not None:
                if top1_confidence >= _EARLY_EXIT_THRESHOLD:
                    cached.early_exit = True
                    cached.early_exit_conf = top1_confidence
                    cached.k = 1
                    self._total_early_exits += 1
                return cached

        # ── Multi-signal complexity estimation ────────────────
        n_dets = len(detections)
        density = _compute_detection_density(detections, screen_area)
        size_var = _compute_size_variance(detections)

        # v2.3.1: Image entropy signal (Grok #4)
        entropy = _compute_image_entropy(image_bytes) if image_bytes else 0.5

        # Domain factor from learning
        domain_factor = 0.5  # neutral default
        domain_entry = self._learning.get(domain) if domain else None
        if domain_entry and domain_entry.k_count >= 3:
            domain_factor = domain_entry.domain_factor

        # Weighted complexity score (0–1)
        det_score = min(1.0, n_dets / 30.0)
        density_score = min(1.0, density / 0.02)

        raw_score = (
            0.30 * det_score
            + 0.20 * density_score
            + 0.15 * size_var
            + 0.15 * entropy       # v2.3.1: entropy signal
            + 0.20 * domain_factor
        )

        # Map score to complexity level
        if raw_score < 0.25:
            complexity = UIComplexity.SIMPLE
        elif raw_score > 0.60:
            complexity = UIComplexity.COMPLEX
        else:
            complexity = UIComplexity.NORMAL

        # Determine K
        if complexity == UIComplexity.SIMPLE:
            k = TOPK_SIMPLE
        elif complexity == UIComplexity.COMPLEX:
            k = TOPK_COMPLEX
        else:
            k = TOPK_NORMAL

        # Domain learning override: if domain consistently needs more/fewer K
        if domain_entry and domain_entry.k_count >= 5:
            learned_k = round(domain_entry.avg_k)
            # Blend: 60% computed + 40% learned
            k = round(0.6 * k + 0.4 * learned_k)
            k = max(TOPK_SIMPLE, min(TOPK_COMPLEX, k))

        # Clamp K to available detections
        k = min(k, n_dets) if n_dets > 0 else k

        # Select input resolution
        if complexity == UIComplexity.SIMPLE:
            resolution = DINO_INPUT_SIMPLE
        elif complexity == UIComplexity.COMPLEX:
            resolution = DINO_INPUT_COMPLEX
        else:
            resolution = DINO_INPUT_NORMAL

        # Early exit check
        early_exit = False
        if top1_confidence >= _EARLY_EXIT_THRESHOLD:
            early_exit = True
            k = 1  # Only need top-1
            self._total_early_exits += 1
            _vlog("⚡", f"AdaptiveTopK early exit: conf={top1_confidence:.2f} ≥ {_EARLY_EXIT_THRESHOLD}")

        analysis = ComplexityAnalysis(
            complexity=complexity,
            k=k,
            resolution=resolution,
            total_detections=n_dets,
            detection_density=density,
            size_variance=size_var,
            domain_factor=domain_factor,
            raw_score=raw_score,
            early_exit=early_exit,
            early_exit_conf=top1_confidence if early_exit else 0.0,
            source="computed",
        )

        # Cache for this URL pattern
        if url:
            self._cache.put(url, analysis)

        _vlog("📊", f"AdaptiveTopK: {complexity.value} (score={raw_score:.2f}), "
                     f"K={k}, res={resolution[0]}×{resolution[1]}"
                     f"{' [domain learned]' if domain_entry and domain_entry.k_count >= 5 else ''}"
                     f"{' [EARLY EXIT]' if early_exit else ''}")

        return analysis

    def record_outcome(
        self,
        domain: str,
        k_used: int,
        complexity_score: float,
        success: bool,
    ) -> None:
        """
        Record outcome for domain learning.

        Called after a click succeeds/fails.
        """
        self._learning.record(domain, k_used, complexity_score, success)

    def stats(self) -> dict[str, Any]:
        return {
            "total_analyses": self._total_analyses,
            "total_early_exits": self._total_early_exits,
            "early_exit_rate": (
                round(self._total_early_exits / self._total_analyses, 3)
                if self._total_analyses > 0 else 0.0
            ),
            "domain_learning": self._learning.stats(),
        }


# ══════════════════════════════════════════════════════════════════
# Module singleton
# ══════════════════════════════════════════════════════════════════

_instance: Optional[AdaptiveTopK] = None


def get_adaptive_topk() -> AdaptiveTopK:
    global _instance
    if _instance is None:
        _instance = AdaptiveTopK()
    return _instance
