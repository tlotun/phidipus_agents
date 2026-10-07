# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/dino_detector.py — Phidipus v2.3
═══════════════════════════════════════════════════════════════════════

High-Level Detection Orchestrator — DINO + Florence unified pipeline.

This is the PRIMARY entry point for the new vision pipeline.
Replaces VLM-based element detection with 50–80× lower latency.

Full pipeline per detection request:
  S0: ClickMemory check      (0ms)   → if HIT → return immediately
  S1: DINO detect             (50–100ms)
  S2: Light filter            (<1ms)
  S3: Adaptive Top-K          (<1ms)
  S4: Florence rank+score     (80–150ms)  → merged Semantic Ranking + Confidence
  S5: Confidence Fusion       (<1ms)
  S6: [Conditional] Precision Refine (0–300ms)

Total: 150–650ms (vs. 1500–25000ms with VLM)

Usage:
    from vision.dino_detector import DinoDetector
    from vision.model_server import get_model_server

    server = await get_model_server()
    detector = DinoDetector(server)

    # Find an element
    result = await detector.find_element(
        image_bytes=screenshot_png,
        query="Post button",
        is_critical=False,
        window_bounds=(0, 80, 1440, 900),
    )
    if result.best:
        x, y = result.best.center
        # → IPC click at (x, y)

Architecture:
  DinoDetector orchestrates DinoEngine + FlorenceEngine + LightFilter + AdaptiveTopK.
  Does NOT import or call VLM (that's the fallback path in PerceptionPipeline).
  Does NOT dispatch IPC actions (that's AgentLoop's job).
  Pure detection + ranking logic only.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-21  Output goes through ConfidenceGate before IPC dispatch.
        DinoDetector does NOT gate — PerceptionPipeline does.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from vision.detection_types import (
    BBox, Detection, DetectionResult, RankedCandidate, RankingResult,
    UIComplexity, RefineReason,
    # Constants
    MIN_BBOX_AREA, MAX_BBOX_SCREEN_RATIO, MAX_ASPECT_RATIO,
    MIN_ASPECT_RATIO, IOU_OVERLAP_THRESHOLD,
    COMBINED_HIGH_CONF, COMBINED_GATE_THRESHOLD, COMBINED_ACCEPT_THRESHOLD,
    MAX_REFINE_ITERATIONS, REFINE_CROP_FACTORS,
    GLOBAL_PIPELINE_TIMEOUT_MS, REFINE_COOLDOWN_MS,
    ACTION_CONF_POLICY,
    DINO_INPUT_NORMAL,
    # v4.3: used by the complexity / top-k / fusion helpers below but never
    # imported (NameError on the DINO fast path)
    DINO_INPUT_SIMPLE, DINO_INPUT_COMPLEX,
    TOPK_SIMPLE, TOPK_NORMAL, TOPK_COMPLEX,
    COMPLEXITY_SIMPLE_MAX, COMPLEXITY_COMPLEX_MIN,
    WEIGHT_DINO, WEIGHT_FLORENCE, WEIGHT_SPATIAL, WEIGHT_HISTORY, WEIGHT_CROSSDOMAIN,
)
from vision.dino_engine import DinoEngine
from vision.florence_engine import FlorenceEngine
from vision.semantic_ranker import SemanticRanker
from vision.confidence_fusion import ConfidenceFusion, get_confidence_fusion
from vision.adaptive_topk import AdaptiveTopK, get_adaptive_topk


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;32m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Light Filter
# ══════════════════════════════════════════════════════════════════

def light_filter(
    detections: list[Detection],
    screen_area: int = 1440 * 900,
    window_bounds: Optional[tuple[int, int, int, int]] = None,
) -> list[Detection]:
    """
    Fast filter (<1ms) to remove obviously invalid detections.

    Filters:
      1. Area too small (< MIN_BBOX_AREA)
      2. Area too large (> MAX_BBOX_SCREEN_RATIO of screen)
      3. Extreme aspect ratio (> MAX_ASPECT_RATIO or < MIN_ASPECT_RATIO)
      4. Completely outside window bounds
      5. Duplicate overlap (IoU > IOU_OVERLAP_THRESHOLD)

    Args:
      detections:    Raw DINO detections.
      screen_area:   Total screen pixels (for size ratio check).
      window_bounds: (x, y, w, h) of focused window (optional).

    Returns:
      Filtered list (may be smaller than input).
    """
    if not detections:
        return []

    filtered = []

    for det in detections:
        bbox = det.bbox

        # 1. Area check
        if bbox.area < MIN_BBOX_AREA:
            continue
        if screen_area > 0 and bbox.area > screen_area * MAX_BBOX_SCREEN_RATIO:
            continue

        # 2. Aspect ratio check
        ar = bbox.aspect_ratio
        if ar > MAX_ASPECT_RATIO or (ar > 0 and ar < MIN_ASPECT_RATIO):
            continue

        # 3. Window bounds check
        if window_bounds:
            wx, wy, ww, wh = window_bounds
            # Detection must overlap with window by at least 30%
            overlap_x = max(0, min(bbox.x2, wx + ww) - max(bbox.x1, wx))
            overlap_y = max(0, min(bbox.y2, wy + wh) - max(bbox.y1, wy))
            overlap_area = overlap_x * overlap_y
            if bbox.area > 0 and overlap_area / bbox.area < 0.3:
                continue

        filtered.append(det)

    # 4. Dedup by IoU
    if len(filtered) > 1:
        filtered = _dedup_by_iou(filtered, IOU_OVERLAP_THRESHOLD)

    return filtered


def _dedup_by_iou(detections: list[Detection], threshold: float) -> list[Detection]:
    """Remove overlapping detections, keeping higher confidence."""
    # Sort by confidence descending
    sorted_dets = sorted(detections, key=lambda d: d.confidence, reverse=True)
    keep = []

    for det in sorted_dets:
        should_keep = True
        for kept in keep:
            if det.bbox.iou(kept.bbox) > threshold:
                should_keep = False
                break
        if should_keep:
            keep.append(det)

    return keep


# ══════════════════════════════════════════════════════════════════
# Adaptive Top-K
# ══════════════════════════════════════════════════════════════════

def estimate_complexity(total_detections: int) -> UIComplexity:
    """
    Estimate UI complexity from number of raw DINO detections.

    Args:
      total_detections: Number of detections BEFORE filtering (conf > 0.25).

    Returns:
      UIComplexity enum.
    """
    if total_detections <= COMPLEXITY_SIMPLE_MAX:
        return UIComplexity.SIMPLE
    elif total_detections >= COMPLEXITY_COMPLEX_MIN:
        return UIComplexity.COMPLEX
    else:
        return UIComplexity.NORMAL


def adaptive_topk(complexity: UIComplexity, total: int) -> int:
    """
    Select K based on UI complexity.

    Args:
      complexity: Estimated UIComplexity.
      total:      Total detections available.

    Returns:
      K value (how many top detections to send to Florence).
    """
    if complexity == UIComplexity.SIMPLE:
        k = TOPK_SIMPLE
    elif complexity == UIComplexity.COMPLEX:
        k = TOPK_COMPLEX
    else:
        k = TOPK_NORMAL

    return min(k, total)


def select_input_resolution(complexity: UIComplexity) -> tuple[int, int]:
    """Select DINO input resolution based on UI complexity."""
    if complexity == UIComplexity.SIMPLE:
        return DINO_INPUT_SIMPLE
    elif complexity == UIComplexity.COMPLEX:
        return DINO_INPUT_COMPLEX
    else:
        return DINO_INPUT_NORMAL


# ══════════════════════════════════════════════════════════════════
# Confidence Fusion
# ══════════════════════════════════════════════════════════════════

def fuse_confidence(
    dino_conf: float,
    florence_score: float,
    heuristic_score: float = 0.5,
    history_bonus: float = 0.0,
    crossdomain_score: float = 0.0,
) -> float:
    """
    Combine multiple signals into a single confidence score.

    Formula:
      combined = w_d × dino + w_f × florence + w_s × spatial
               + w_h × history + w_c × crossdomain

    Returns:
      Combined score (0.0–1.0), clamped.
    """
    combined = (
        WEIGHT_DINO * dino_conf
        + WEIGHT_FLORENCE * florence_score
        + WEIGHT_SPATIAL * heuristic_score
        + WEIGHT_HISTORY * max(-0.1, min(0.1, history_bonus))
        + WEIGHT_CROSSDOMAIN * crossdomain_score
    )
    return max(0.0, min(1.0, combined))


def compute_spatial_heuristic(bbox: BBox, window_bounds: Optional[tuple[int, int, int, int]] = None) -> float:
    """
    Spatial/size heuristic for UI elements.

    Interactive elements (buttons, inputs, links) typically:
      - 30–200px wide
      - In the visible window area
      - Not too close to screen edges (toolbar/dock overlap)

    Returns score 0.0–1.0.
    """
    score = 0.5  # neutral start

    w, h = bbox.width, bbox.height
    area = bbox.area

    # Good size range for interactive elements
    if 30 <= w <= 300 and 20 <= h <= 80:
        score += 0.2
    elif w > 500 or h > 200:
        score -= 0.1

    # Buttons typically have moderate aspect ratio
    ar = bbox.aspect_ratio
    if 1.5 <= ar <= 5.0:
        score += 0.1  # typical button shape
    elif ar > 15.0 or ar < 0.1:
        score -= 0.2  # extreme shapes unlikely to be buttons

    # Position within window
    if window_bounds:
        wx, wy, ww, wh = window_bounds
        cx, cy = bbox.center

        # Center of window area gets slight boost
        rx = (cx - wx) / ww if ww > 0 else 0.5
        ry = (cy - wy) / wh if wh > 0 else 0.5
        if 0.1 <= rx <= 0.9 and 0.1 <= ry <= 0.9:
            score += 0.1

        # Right side often has action buttons
        if rx > 0.7:
            score += 0.05

    return max(0.0, min(1.0, score))


# ══════════════════════════════════════════════════════════════════
# DinoDetector — Main Orchestrator
# ══════════════════════════════════════════════════════════════════

class DinoDetector:
    """
    High-level detection orchestrator.

    Coordinates DINO detection → Light Filter → Florence ranking →
    Confidence Fusion → [optional] Precision Refine.

    This class is the drop-in replacement for VLM-based detection
    in PerceptionPipeline.

    Usage:
        server = await get_model_server()
        detector = DinoDetector(server)

        result = await detector.find_element(
            image_bytes=png,
            query="Post button",
        )
        if result.best and result.best_score > 0.8:
            x, y = result.best.center
    """

    def __init__(self, model_server: Any) -> None:
        self._dino = DinoEngine(model_server)
        self._florence = FlorenceEngine(model_server)
        self._ranker = SemanticRanker(self._florence)
        self._fusion = get_confidence_fusion()
        self._topk = get_adaptive_topk()
        self._server = model_server
        self._total_calls: int = 0
        self._total_cache_hits: int = 0
        self._avg_latency_ms: float = 0
        # v2.3.1: Refine cooldown (ChatGPT feedback)
        self._last_refine_time: float = 0

    @property
    def ready(self) -> bool:
        return self._dino.ready

    @property
    def florence_ready(self) -> bool:
        return self._florence.ready

    async def find_element(
        self,
        image_bytes: bytes,
        query: str,
        is_critical: bool = False,
        window_bounds: Optional[tuple[int, int, int, int]] = None,
        history_bonus: float = 0.0,
        crossdomain_score: float = 0.0,
        screen_area: int = 1440 * 900,
        domain: str = "",
        url: str = "",
        action_type: str = "default",
    ) -> RankingResult:
        """
        Find a UI element in a screenshot.

        v2.3.1 improvements (from AI feedback):
          - Global pipeline timeout 800ms (Grok #5)
          - Action-level confidence policy (ChatGPT)
          - Refine cooldown 500ms (ChatGPT)

        Args:
          image_bytes:       PNG screenshot.
          query:             What to find ("Post button", "Upload icon").
          is_critical:       True if action is irreversible (always refine).
          window_bounds:     (x, y, w, h) of focused window.
          history_bonus:     ConfidenceHistory trust bonus (-0.1 to +0.1).
          crossdomain_score: CrossDomain pattern match score (0.0–1.0).
          screen_area:       Total screen pixels.
          action_type:       Action category for confidence policy.

        Returns:
          RankingResult with ranked candidates.
        """
        # v2.3.1: Global pipeline timeout (Grok #5 + ChatGPT #6)
        try:
            return await asyncio.wait_for(
                self._find_element_inner(
                    image_bytes, query, is_critical, window_bounds,
                    history_bonus, crossdomain_score, screen_area,
                    domain, url, action_type,
                ),
                timeout=GLOBAL_PIPELINE_TIMEOUT_MS / 1000.0,
            )
        except asyncio.TimeoutError:
            self._total_calls += 1
            _vlog("⏱️", f"DinoDetector TIMEOUT {GLOBAL_PIPELINE_TIMEOUT_MS}ms for '{query[:40]}'")
            return RankingResult(query=query, source_pipeline="timeout")

    async def _find_element_inner(
        self,
        image_bytes: bytes,
        query: str,
        is_critical: bool,
        window_bounds: Optional[tuple[int, int, int, int]],
        history_bonus: float,
        crossdomain_score: float,
        screen_area: int,
        domain: str,
        url: str,
        action_type: str,
    ) -> RankingResult:
        """Inner find_element without timeout wrapper."""
        t0 = time.perf_counter()
        self._total_calls += 1

        result = RankingResult(query=query)

        # ── Step 1: DINO detection ────────────────────────────
        queries = self._expand_query(query)
        dino_result = await self._dino.detect(
            image_bytes=image_bytes,
            queries=queries,
            input_size=DINO_INPUT_NORMAL,
        )
        result.dino_latency_ms = dino_result.latency_ms

        if dino_result.count == 0:
            _vlog("⚠️", f"DINO: 0 detections cho '{query}' — fallback cần thiết")
            result.total_latency_ms = (time.perf_counter() - t0) * 1000
            result.source_pipeline = "dino_empty"
            return result

        # ── Step 2: Light filter ──────────────────────────────
        filtered = light_filter(
            dino_result.detections,
            screen_area=screen_area,
            window_bounds=window_bounds,
        )
        if not filtered:
            _vlog("⚠️", f"Light filter loại hết {dino_result.count} detections")
            result.total_latency_ms = (time.perf_counter() - t0) * 1000
            return result

        # ── Step 3: Adaptive Top-K (Phase 4 module) ─────────────
        top1_conf = filtered[0].confidence if filtered else 0.0
        analysis = self._topk.analyze(
            detections=filtered,
            domain=domain,
            url=url,
            screen_area=screen_area,
            top1_confidence=top1_conf,
        )
        k = analysis.k
        result.dino_latency_ms = dino_result.latency_ms

        # Early exit: AdaptiveTopK detected very high DINO confidence
        if analysis.early_exit:
            _vlog("⚡", f"Early exit: DINO conf={analysis.early_exit_conf:.2f}")
            det = filtered[0]
            fusion_result = self._fusion.fuse(
                dino_conf=det.confidence,
                semantic_score=det.confidence,
                position_score=0.7,
                history_bonus=history_bonus,
                crossdomain_score=crossdomain_score,
                is_critical=is_critical,
                domain=domain,
                has_history=abs(history_bonus) > 0.001,
            )
            candidate = RankedCandidate(
                detection=det,
                semantic_score=det.confidence,
                combined_score=fusion_result.combined_score,
                description=f"high-confidence {det.label}",
                heuristic_score=0.7,
                history_bonus=history_bonus,
                crossdomain_score=crossdomain_score,
            )
            result.candidates = [candidate]
            result.total_latency_ms = (time.perf_counter() - t0) * 1000
            return result

        top_k = filtered[:k]

        # ── Step 4: Semantic Ranking (Phase 3 deep ranker) ────
        t_rank = time.perf_counter()
        ranked = await self._ranker.rank(
            image_bytes=image_bytes,
            detections=top_k,
            query=query,
            window_bounds=window_bounds,
        )
        result.florence_latency_ms = (time.perf_counter() - t_rank) * 1000

        # ── Step 5: Confidence Fusion (Phase 4 module) ────────
        fused_candidates = []
        has_history = abs(history_bonus) > 0.001
        for rc in ranked:
            fusion_result = self._fusion.fuse(
                dino_conf=rc.detection.confidence,
                semantic_score=rc.semantic_score,
                position_score=rc.heuristic_score,
                history_bonus=history_bonus,
                crossdomain_score=crossdomain_score,
                is_critical=is_critical,
                domain=domain,
                has_history=has_history,
            )
            fused_candidates.append(RankedCandidate(
                detection=rc.detection,
                semantic_score=rc.semantic_score,
                combined_score=fusion_result.combined_score,
                description=rc.description,
                heuristic_score=rc.heuristic_score,
                history_bonus=history_bonus,
                crossdomain_score=crossdomain_score,
            ))

        # Sort by combined score
        fused_candidates.sort(key=lambda c: c.combined_score, reverse=True)
        result.candidates = fused_candidates

        # ── Step 6: Precision Refine (conditional) ────────────
        refine_reason = self._should_refine(result, is_critical, action_type)
        if refine_reason != RefineReason.NONE:
            result.refine_triggered = True
            result.refine_reason = refine_reason
            await self._precision_refine(image_bytes, result, queries, window_bounds,
                                         history_bonus, crossdomain_score,
                                         is_critical=is_critical, domain=domain)

        result.total_latency_ms = (time.perf_counter() - t0) * 1000

        # Update running average
        self._avg_latency_ms = (
            self._avg_latency_ms * (self._total_calls - 1) + result.total_latency_ms
        ) / self._total_calls

        _vlog("🎯", f"DinoDetector: {result.count} candidates, "
                     f"best={result.best_score:.2f}, "
                     f"{result.total_latency_ms:.0f}ms "
                     f"(DINO {result.dino_latency_ms:.0f} + "
                     f"Florence {result.florence_latency_ms:.0f}"
                     f"{' + Refine' if result.refine_triggered else ''})")

        return result

    # ── Query Expansion ───────────────────────────────────────

    def _expand_query(self, query: str) -> list[str]:
        """
        Expand a single query into DINO-friendly multi-query format.

        Examples:
          "Post button" → ["Post button", "Submit button", "Đăng"]
          "Upload icon" → ["Upload icon", "Attach", "Add photo"]
        """
        queries = [query]

        # Common Vietnamese ↔ English equivalents
        _translations = {
            "post button": ["Submit button", "Đăng", "Post"],
            "upload": ["Attach", "Tải lên", "Add photo", "Upload icon"],
            "send": ["Gửi", "Submit", "Send button"],
            "close": ["Đóng", "X button", "Close", "Cancel"],
            "search": ["Tìm kiếm", "Search box", "Search icon"],
            "login": ["Đăng nhập", "Sign in", "Login button"],
            "next": ["Tiếp theo", "Continue", "Next button"],
            "save": ["Lưu", "Save button", "Download"],
            "delete": ["Xóa", "Remove", "Delete button"],
        }

        query_lower = query.lower()
        for key, expansions in _translations.items():
            if key in query_lower:
                queries.extend(expansions[:2])  # Max 2 extra queries
                break

        return queries[:5]  # Max 5 total queries

    # ── Refine Logic ──────────────────────────────────────────

    def _should_refine(self, result: RankingResult, is_critical: bool,
                       action_type: str = "default") -> RefineReason:
        """
        Determine if precision refine is needed.

        v2.3.1: action-level policy + cooldown (ChatGPT feedback).
        """
        if not result.candidates:
            return RefineReason.NONE

        best_score = result.best_score

        # v2.3.1: Cooldown — don't spam refine calls (ChatGPT feedback)
        now = time.perf_counter()
        if (now - self._last_refine_time) * 1000 < REFINE_COOLDOWN_MS:
            return RefineReason.NONE

        # v2.3.1: Action-level confidence policy (ChatGPT feedback)
        # Different actions need different confidence thresholds
        required_conf = ACTION_CONF_POLICY.get(action_type,
                                                ACTION_CONF_POLICY["default"])

        if is_critical:
            self._last_refine_time = now
            return RefineReason.CRITICAL_ACTION

        if best_score < min(COMBINED_GATE_THRESHOLD, required_conf):
            self._last_refine_time = now
            return RefineReason.LOW_CONFIDENCE

        if result.is_ambiguous:
            self._last_refine_time = now
            return RefineReason.AMBIGUITY

        return RefineReason.NONE

    async def _precision_refine(
        self,
        image_bytes: bytes,
        result: RankingResult,
        queries: list[str],
        window_bounds: Optional[tuple[int, int, int, int]],
        history_bonus: float,
        crossdomain_score: float,
        is_critical: bool = False,
        domain: str = "",
    ) -> None:
        """
        Active Vision Loop — zoom + re-detect for higher precision.

        Max MAX_REFINE_ITERATIONS iterations:
          Iter 1: crop 2× around best candidate → DINO + Florence re-rank
          Iter 2: crop 4× → DINO + Florence re-rank
          Iter 3: (if still low) report best available

        Modifies result.candidates in-place.
        """
        if not result.candidates:
            return

        best = result.candidates[0]
        iters = 0

        for zoom in REFINE_CROP_FACTORS:
            if best.combined_score >= COMBINED_ACCEPT_THRESHOLD:
                break
            if iters >= MAX_REFINE_ITERATIONS:
                break

            iters += 1
            _vlog("🔬", f"Refine iter {iters}: zoom {zoom}× around "
                        f"({best.detection.bbox.center_x}, {best.detection.bbox.center_y})")

            # Re-detect on zoomed crop
            crop_result = await self._dino.detect_crop(
                image_bytes=image_bytes,
                queries=queries,
                crop_bbox=best.detection.bbox,
                zoom_factor=zoom,
            )

            if crop_result.count == 0:
                continue

            # Re-rank with SemanticRanker
            re_ranked = await self._ranker.rank(
                image_bytes=image_bytes,
                detections=crop_result.detections,
                query=result.query,
                window_bounds=window_bounds,
            )

            if not re_ranked:
                continue

            # Fuse with ConfidenceFusion module
            top = re_ranked[0]
            fusion_r = self._fusion.fuse(
                dino_conf=top.detection.confidence,
                semantic_score=top.semantic_score,
                position_score=top.heuristic_score,
                history_bonus=history_bonus,
                crossdomain_score=crossdomain_score,
                is_critical=is_critical,
                domain=domain,
            )
            combined = fusion_r.combined_score

            if combined > best.combined_score:
                new_candidate = RankedCandidate(
                    detection=top.detection,
                    semantic_score=top.semantic_score,
                    combined_score=combined,
                    description=top.description + f" (refined ×{zoom})",
                    heuristic_score=top.heuristic_score,
                    history_bonus=history_bonus,
                    crossdomain_score=crossdomain_score,
                )
                result.candidates.insert(0, new_candidate)
                best = new_candidate
                _vlog("✅", f"Refine improved: {combined:.2f} (was {result.candidates[1].combined_score:.2f})")

        result.refine_iterations = iters

    # ── Stats ─────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "florence_ready": self.florence_ready,
            "total_calls": self._total_calls,
            "avg_latency_ms": round(self._avg_latency_ms, 1),
            "dino": self._dino.stats(),
            "florence": self._florence.stats(),
            "semantic_ranker": self._ranker.stats(),
            "confidence_fusion": self._fusion.stats(),
            "adaptive_topk": self._topk.stats(),
        }
