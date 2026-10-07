# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/detection_types.py — Phidipus v2.3
═══════════════════════════════════════════════════════════════════════

Shared type definitions for the Grounding DINO + Florence-2 vision pipeline.

All detection dataclasses used across:
  - vision/dino_engine.py         (Grounding DINO output)
  - vision/florence_engine.py     (Florence-2 ranking output)
  - vision/dino_detector.py       (orchestrator)
  - vision/semantic_ranker.py     (combined ranking)
  - vision/confidence_fusion.py   (final scoring)
  - vision/perception_pipeline.py (integration)
  - vision/confidence_gate.py     (gating)

All types are frozen dataclasses — immutable after creation.
No mutable state, no side effects, no imports beyond stdlib.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  (Pure data — no security-sensitive operations.)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ══════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════

class DetectionSource(str, Enum):
    """Source that produced a detection."""
    DINO = "dino"
    FLORENCE = "florence"
    VLM = "vlm"
    ACCESSIBILITY = "a11y"
    DOM_JS = "dom_js"
    CACHE = "cache"
    INVENTORY = "inventory"


class UIComplexity(str, Enum):
    """Estimated UI complexity for adaptive Top-K."""
    SIMPLE = "simple"       # login, dialog, alert — K=2
    NORMAL = "normal"       # form, settings page  — K=3–5
    COMPLEX = "complex"     # feed, dashboard      — K=5–7


class RefineReason(str, Enum):
    """Why precision refine was triggered."""
    LOW_CONFIDENCE = "low_confidence"
    CRITICAL_ACTION = "critical_action"
    AMBIGUITY = "ambiguity"
    NONE = "none"


# ══════════════════════════════════════════════════════════════════
# Core Detection Types
# ══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class BBox:
    """Bounding box in absolute pixel coordinates."""
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(0, self.y2 - self.y1)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center_x(self) -> int:
        return (self.x1 + self.x2) // 2

    @property
    def center_y(self) -> int:
        return (self.y1 + self.y2) // 2

    @property
    def center(self) -> tuple[int, int]:
        return (self.center_x, self.center_y)

    @property
    def aspect_ratio(self) -> float:
        """Width / height. Returns 0.0 if height is 0."""
        return self.width / self.height if self.height > 0 else 0.0

    def iou(self, other: BBox) -> float:
        """Intersection over Union with another BBox."""
        ix1 = max(self.x1, other.x1)
        iy1 = max(self.y1, other.y1)
        ix2 = min(self.x2, other.x2)
        iy2 = min(self.y2, other.y2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def contains_point(self, x: int, y: int) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    def relative_coords(self, win_x: int, win_y: int, win_w: int, win_h: int) -> tuple[float, float]:
        """Convert center to relative (rx, ry) within window bounds."""
        rx = (self.center_x - win_x) / win_w if win_w > 0 else 0.5
        ry = (self.center_y - win_y) / win_h if win_h > 0 else 0.5
        return (max(0.0, min(1.0, rx)), max(0.0, min(1.0, ry)))

    def to_dict(self) -> dict[str, int]:
        return {"x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2}

    @classmethod
    def from_dict(cls, d: dict) -> BBox:
        return cls(x1=int(d["x1"]), y1=int(d["y1"]),
                   x2=int(d["x2"]), y2=int(d["y2"]))


@dataclass(frozen=True)
class Detection:
    """
    Single detection from Grounding DINO.

    Fields:
      bbox:       Absolute pixel bounding box.
      label:      Text label matched by DINO (from query).
      confidence: DINO detection confidence (0.0–1.0).
      source:     Always DetectionSource.DINO for raw detections.
    """
    bbox: BBox
    label: str
    confidence: float
    source: DetectionSource = DetectionSource.DINO

    @property
    def area(self) -> int:
        return self.bbox.area

    @property
    def center(self) -> tuple[int, int]:
        return self.bbox.center

    def to_dict(self) -> dict[str, Any]:
        return {
            "bbox": self.bbox.to_dict(),
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "source": self.source.value,
        }


@dataclass(frozen=True)
class RankedCandidate:
    """
    Detection after Florence-2 semantic ranking + confidence fusion.

    This is the final output consumed by ConfidenceGate and ClickMemory.

    Fields:
      detection:      Original DINO detection.
      semantic_score: Florence-2 cosine similarity (0.0–1.0).
      combined_score: Weighted fusion of all signals (0.0–1.0).
      description:    Florence-2 caption of the element (for semantic cache).
      heuristic_score: Spatial/size heuristic (0.0–1.0).
      history_bonus:  ConfidenceHistory trust adjustment (-0.1–+0.1).
      crossdomain_score: CrossDomain pattern match (0.0–1.0).
    """
    detection: Detection
    semantic_score: float = 0.0
    combined_score: float = 0.0
    description: str = ""
    heuristic_score: float = 0.5
    history_bonus: float = 0.0
    crossdomain_score: float = 0.0

    @property
    def bbox(self) -> BBox:
        return self.detection.bbox

    @property
    def label(self) -> str:
        return self.detection.label

    @property
    def center(self) -> tuple[int, int]:
        return self.detection.center

    @property
    def confidence(self) -> float:
        """Alias for combined_score — used by ConfidenceGate."""
        return self.combined_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "detection": self.detection.to_dict(),
            "semantic_score": round(self.semantic_score, 4),
            "combined_score": round(self.combined_score, 4),
            "description": self.description,
            "heuristic_score": round(self.heuristic_score, 4),
            "history_bonus": round(self.history_bonus, 4),
            "crossdomain_score": round(self.crossdomain_score, 4),
        }


# ══════════════════════════════════════════════════════════════════
# Pipeline Result Types
# ══════════════════════════════════════════════════════════════════

@dataclass
class DetectionResult:
    """
    Complete result from a DINO detection pass.

    Contains raw detections BEFORE ranking/fusion.
    """
    detections: list[Detection] = field(default_factory=list)
    query: str = ""
    latency_ms: float = 0.0
    complexity: UIComplexity = UIComplexity.NORMAL
    k_used: int = 5
    input_resolution: tuple[int, int] = (640, 640)
    total_raw_detections: int = 0  # before filtering
    timestamp: float = field(default_factory=time.time)

    @property
    def count(self) -> int:
        return len(self.detections)

    @property
    def best(self) -> Optional[Detection]:
        if not self.detections:
            return None
        return max(self.detections, key=lambda d: d.confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "query": self.query,
            "latency_ms": round(self.latency_ms, 1),
            "complexity": self.complexity.value,
            "k_used": self.k_used,
            "total_raw": self.total_raw_detections,
            "detections": [d.to_dict() for d in self.detections],
        }


@dataclass
class RankingResult:
    """
    Complete result from Florence-2 ranking + confidence fusion.

    Contains ranked candidates sorted by combined_score descending.
    """
    candidates: list[RankedCandidate] = field(default_factory=list)
    query: str = ""
    dino_latency_ms: float = 0.0
    florence_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    refine_triggered: bool = False
    refine_reason: RefineReason = RefineReason.NONE
    refine_iterations: int = 0
    source_pipeline: str = "dino_florence"  # or "vlm_fallback"
    timestamp: float = field(default_factory=time.time)

    @property
    def count(self) -> int:
        return len(self.candidates)

    @property
    def best(self) -> Optional[RankedCandidate]:
        if not self.candidates:
            return None
        return self.candidates[0]  # already sorted

    @property
    def best_score(self) -> float:
        return self.candidates[0].combined_score if self.candidates else 0.0

    @property
    def is_ambiguous(self) -> bool:
        """Top-2 candidates have score difference < 0.1."""
        if len(self.candidates) < 2:
            return False
        return (self.candidates[0].combined_score -
                self.candidates[1].combined_score) < 0.1

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "query": self.query,
            "best_score": round(self.best_score, 4),
            "dino_ms": round(self.dino_latency_ms, 1),
            "florence_ms": round(self.florence_latency_ms, 1),
            "total_ms": round(self.total_latency_ms, 1),
            "refine": self.refine_triggered,
            "refine_reason": self.refine_reason.value,
            "pipeline": self.source_pipeline,
            "candidates": [c.to_dict() for c in self.candidates[:5]],
        }


# ══════════════════════════════════════════════════════════════════
# Model Server Types
# ══════════════════════════════════════════════════════════════════

@dataclass
class ModelInfo:
    """Metadata about a loaded ONNX model."""
    name: str
    path: str
    size_mb: float = 0.0
    loaded: bool = False
    vram_mb: float = 0.0
    avg_latency_ms: float = 0.0
    inference_count: int = 0
    backend: str = "cpu"  # "metal", "coreml", "cpu", "cuda"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "loaded": self.loaded,
            "size_mb": round(self.size_mb, 1),
            "vram_mb": round(self.vram_mb, 1),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "inference_count": self.inference_count,
            "backend": self.backend,
            "error": self.error,
        }


@dataclass
class ProviderInfo:
    """ONNX execution provider capability."""
    name: str           # "CoreMLExecutionProvider", "MetalPerformanceShadersExecutionProvider", etc.
    available: bool = False
    priority: int = 0   # lower = higher priority
    benchmark_ms: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "priority": self.priority,
            "benchmark_ms": round(self.benchmark_ms, 1),
            "note": self.note,
        }


# ══════════════════════════════════════════════════════════════════
# Light Filter Constants
# ══════════════════════════════════════════════════════════════════

# Bounding box filtering thresholds (Phase 2 Light Filter)
MIN_BBOX_AREA = 100          # < 10×10px → noise
MAX_BBOX_SCREEN_RATIO = 0.8  # > 80% of screen → likely background
MAX_ASPECT_RATIO = 20.0      # extremely elongated → noise
MIN_ASPECT_RATIO = 0.05      # extremely tall/thin → noise
IOU_OVERLAP_THRESHOLD = 0.9  # > 90% overlap → duplicate

# Confidence thresholds
DINO_CONF_THRESHOLD = 0.3       # DINO raw confidence floor
FLORENCE_SCORE_THRESHOLD = 0.4  # Florence semantic score floor
COMBINED_HIGH_CONF = 0.95       # Skip Florence if DINO this confident
COMBINED_GATE_THRESHOLD = 0.75  # Below this → trigger refine
COMBINED_ACCEPT_THRESHOLD = 0.85  # Above this in refine → accept

# Adaptive Top-K settings
TOPK_SIMPLE = 2
TOPK_NORMAL = 5
TOPK_COMPLEX = 7
COMPLEXITY_SIMPLE_MAX = 5    # ≤5 raw detections → simple
COMPLEXITY_COMPLEX_MIN = 15  # ≥15 raw detections → complex

# Active Vision Loop
MAX_REFINE_ITERATIONS = 3
REFINE_CROP_FACTORS = [2.0, 4.0]  # zoom factors per iteration
GLOBAL_PIPELINE_TIMEOUT_MS = 800  # v2.3.1: hard cap toàn pipeline (Grok feedback)
REFINE_COOLDOWN_MS = 500          # v2.3.1: min gap giữa 2 refine calls (ChatGPT feedback)

# Confidence Fusion weights — V1 SIMPLE (ChatGPT #4: start simple, tune later)
# Default: 50/50 DINO+Florence, thêm heuristic sau khi có real data
WEIGHT_DINO = 0.50
WEIGHT_FLORENCE = 0.50
WEIGHT_SPATIAL = 0.0
WEIGHT_HISTORY = 0.0
WEIGHT_CROSSDOMAIN = 0.0

# V2 weights (enable khi có đủ data từ ConfidenceHistory)
WEIGHT_DINO_V2 = 0.35
WEIGHT_FLORENCE_V2 = 0.35
WEIGHT_SPATIAL_V2 = 0.10
WEIGHT_HISTORY_V2 = 0.10
WEIGHT_CROSSDOMAIN_V2 = 0.10

# Action-level confidence policy (ChatGPT feedback: different thresholds per action)
ACTION_CONF_POLICY: dict[str, float] = {
    "click_login": 0.85,       # irreversible auth action
    "click_post": 0.85,        # publish content
    "click_send": 0.85,        # send message/email
    "click_delete": 0.90,      # destructive action
    "click_confirm": 0.80,     # confirm dialog
    "click_button": 0.70,      # generic button
    "click_link": 0.65,        # navigation
    "click_scroll": 0.55,      # scroll is low-risk
    "click_tab": 0.60,         # tab switch
    "default": 0.65,           # fallback
}

# Model paths (relative to models_dir)
DINO_MODEL_NAME = "grounding-dino-1.5-edge"
FLORENCE_MODEL_NAME = "florence-2-base"
DINO_ONNX_FILE = "model.onnx"
FLORENCE_ONNX_FILE = "model.onnx"

# Input resolutions
DINO_INPUT_SIMPLE = (480, 480)
DINO_INPUT_NORMAL = (640, 640)
DINO_INPUT_COMPLEX = (800, 800)
FLORENCE_INPUT_SIZE = (768, 768)
