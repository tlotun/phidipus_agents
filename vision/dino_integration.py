# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/dino_integration.py — Phidipus v2.3 Phase 7
═══════════════════════════════════════════════════════════════════════

DINO Pipeline Integration Bridge.

Connects Phase 1-6 modules (Grounding DINO + Florence-2) into the
existing VisionActor and WorkflowExecutor pipeline as strategy S0c.

Strategy Stack after integration:
  S0a: ClickMemory       (0ms)    — tọa độ đã ghi nhớ
  S0b: ScreenInventory   (0ms)    — B4 proactive scan
  ★ S0c: DINO+Florence   (150ms)  — NEW: fast object detection ← Phase 7
  S1:  DOM/JS selector   (5ms)
  S1.5: ZoomAction       (3-8s)
  S2:  MultiModal        (2-5s)
  S3:  CropLocator       (2-4s)
  S4:  VLM Fallback      (3-25s)

Flow for S0c:
  1. Check if ModelServer + DinoDetector ready
  2. Get window bounds (reuse from workflow context)
  3. Capture focused window only (DiffCapture)
  4. Check frame diff — skip if unchanged
  5. Run DinoDetector.find_element() (DINO → filter → Florence → fusion)
  6. TemporalTracker.lock() the target
  7. Return coords + confidence to caller
  8. Before click: TemporalTracker.verify_and_correct()
  9. After click success: update SemanticCache + ClickMemory
  10. After click fail: ConfidenceFusion.record_outcome(False)

Caller integration:
  workflow_executor._exec_vision_click() → try dino_find_and_click() first
  vision_actor.find_and_click_v2()       → insert S0c between S0b and S1

Process: orchestrator (L1)

Security invariants:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-21  All detections go through ConfidenceGate via PerceptionPipeline.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;32m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Result Type
# ══════════════════════════════════════════════════════════════════

@dataclass
class DinoClickResult:
    """Result of DINO-based element detection for click dispatch."""
    success: bool = False
    x: int = 0
    y: int = 0
    confidence: float = 0.0
    method: str = "dino_florence"
    description: str = ""
    latency_ms: float = 0.0
    dino_latency_ms: float = 0.0
    florence_latency_ms: float = 0.0
    refine_triggered: bool = False
    cache_tier: str = ""           # "spatial" | "visual_verify" | "anchor" | ""
    temporal_correction: str = ""  # "template" | "window_offset" | "unchanged" | ""
    error: str = ""
    skipped_reason: str = ""       # "not_ready" | "frame_unchanged" | "cache_hit" | ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "x": self.x, "y": self.y,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "description": self.description,
            "latency_ms": round(self.latency_ms, 1),
            "dino_ms": round(self.dino_latency_ms, 1),
            "florence_ms": round(self.florence_latency_ms, 1),
            "refine": self.refine_triggered,
            "cache_tier": self.cache_tier,
            "temporal": self.temporal_correction,
            "error": self.error,
        }


# ══════════════════════════════════════════════════════════════════
# DinoIntegration — Main Bridge
# ══════════════════════════════════════════════════════════════════

class DinoIntegration:
    """
    Bridge between DINO pipeline and VisionActor/WorkflowExecutor.

    Wraps DinoDetector + DiffCapture + TemporalTracker + SemanticCache
    into a single find_and_click()-compatible API.

    Usage:
        integration = DinoIntegration(model_server)

        # In workflow_executor._exec_vision_click():
        result = await integration.find_element(
            query="Post button",
            domain="facebook.com",
            action_key="fb_post_button",
            window_bounds=(0, 80, 1440, 900),
        )
        if result.success:
            await ipc.send_action("mouse_click", {"x": result.x, "y": result.y})
    """

    # Confidence threshold below which DINO result is not trusted
    MIN_CONFIDENCE = 0.65

    def __init__(self, model_server: Any = None) -> None:
        self._server = model_server
        self._detector = None
        self._diff_capture = None
        self._tracker = None
        self._semantic_cache = None
        self._ready = False
        self._total_calls: int = 0
        self._total_hits: int = 0
        self._total_cache_hits: int = 0
        self._total_fallbacks: int = 0
        self._avg_latency_ms: float = 0

        if model_server is not None:
            self._init_modules(model_server)

    def _init_modules(self, model_server: Any) -> None:
        """Initialize all Phase 1-6 modules."""
        try:
            from vision.dino_detector import DinoDetector
            from vision.diff_capture import DiffCapture
            from vision.temporal_tracker import TemporalTracker
            from vision.semantic_cache import SemanticCache

            self._detector = DinoDetector(model_server)
            self._diff_capture = DiffCapture()
            self._tracker = TemporalTracker()
            self._semantic_cache = SemanticCache()
            self._ready = self._detector.ready
            _vlog("🔬", "DinoIntegration: all modules initialized")
        except Exception as exc:
            _vlog("❌", f"DinoIntegration init failed: {exc}")
            self._ready = False

    def inject_model_server(self, model_server: Any) -> None:
        """Late injection from main.py."""
        self._server = model_server
        self._init_modules(model_server)

    @property
    def ready(self) -> bool:
        return self._ready and self._detector is not None and self._detector.ready

    # ── Main API ──────────────────────────────────────────────

    async def find_element(
        self,
        query: str,
        domain: str = "",
        action_key: str = "",
        window_bounds: Optional[tuple[int, int, int, int]] = None,
        is_critical: bool = False,
        url: str = "",
        roi_hint: str = "",
    ) -> DinoClickResult:
        """
        Find a UI element using the full DINO+Florence pipeline.

        Integrates: SemanticCache → DiffCapture → DinoDetector →
                    TemporalTracker → return coords.

        Args:
            query:          What to find ("Post button").
            domain:         Website domain for caching.
            action_key:     Cache key for ClickMemory/SemanticCache.
            window_bounds:  (wx, wy, ww, wh) of focused window.
            is_critical:    True → force precision refine.
            url:            Full URL for AdaptiveTopK.
            roi_hint:       Optional ROI hint ("top toolbar", etc.).

        Returns:
            DinoClickResult with coordinates and metadata.
        """
        self._total_calls += 1
        t0 = time.perf_counter()

        if not self.ready:
            self._total_fallbacks += 1
            return DinoClickResult(
                error="DINO not ready",
                skipped_reason="not_ready",
            )

        wb = window_bounds or (0, 0, 1440, 900)

        # ── Step 1: SemanticCache check ───────────────────────
        if self._semantic_cache and domain and action_key:
            _, _, ww, wh = wb
            cache_result = self._semantic_cache.lookup(
                domain=domain,
                action_key=action_key,
                current_window_w=ww,
                current_window_h=wh,
            )
            if cache_result.hit and cache_result.confidence >= self.MIN_CONFIDENCE:
                wx, wy, ww, wh = wb
                abs_x = int(wx + cache_result.rx * ww)
                abs_y = int(wy + cache_result.ry * wh)
                self._total_cache_hits += 1
                latency = (time.perf_counter() - t0) * 1000
                _vlog("⚡", f"S0c SemanticCache {cache_result.tier}: "
                             f"({abs_x},{abs_y}) conf={cache_result.confidence:.2f} "
                             f"{latency:.0f}ms")
                return DinoClickResult(
                    success=True,
                    x=abs_x, y=abs_y,
                    confidence=cache_result.confidence,
                    method=f"dino_cache_{cache_result.tier}",
                    description=cache_result.description,
                    latency_ms=latency,
                    cache_tier=cache_result.tier,
                )

        # ── Step 2: DiffCapture — focused window + frame skip ─
        frame = None
        if self._diff_capture:
            frame, diff = self._diff_capture.capture(
                window_bounds=wb,
                roi_hint=roi_hint or None,
            )
            if diff.skip_detection:
                # Screen unchanged — but we don't have previous DINO result
                # Only skip if we had a recent cache hit for this action_key
                pass  # proceed to detection
        else:
            # Fallback: capture full screen as bytes
            from vision.screen_capture import ScreenCapture, CaptureRegion
            sc = ScreenCapture()
            wx, wy, ww, wh = wb
            region = CaptureRegion(x=wx, y=wy, width=ww, height=wh)
            try:
                image_bytes = sc.capture_bytes(region, fmt="PNG")
            except Exception:
                image_bytes = sc.capture_bytes(fmt="PNG")

            @dataclass
            class _FakeFrame:
                image_bytes: bytes
            frame = _FakeFrame(image_bytes=image_bytes)

        if not frame or not frame.image_bytes:
            return DinoClickResult(error="Capture failed")

        # ── Step 3: Get ConfidenceHistory bonus ───────────────
        history_bonus = 0.0
        try:
            from memory.confidence_history import get_confidence_history
            ch = get_confidence_history()
            adjusted = ch.adjust(domain, action_key, 0.5)  # dummy conf
            history_bonus = adjusted.trust_bonus
        except Exception:
            pass

        # ── Step 4: Get CrossDomain hint score ────────────────
        crossdomain_score = 0.0
        try:
            from memory.cross_domain_memory import get_cross_domain_memory
            cdm = get_cross_domain_memory()
            from memory.cross_domain_memory import infer_button_type
            btn_type = infer_button_type(action_key, query)
            hint = cdm.get_hint(domain, btn_type)
            if hint:
                crossdomain_score = hint.confidence
        except Exception:
            pass

        # ── Step 5: DinoDetector.find_element() ───────────────
        ranking = await self._detector.find_element(
            image_bytes=frame.image_bytes,
            query=query,
            is_critical=is_critical,
            window_bounds=wb,
            history_bonus=history_bonus,
            crossdomain_score=crossdomain_score,
            domain=domain,
            url=url,
        )

        if not ranking.candidates or ranking.best_score < self.MIN_CONFIDENCE:
            self._total_fallbacks += 1
            latency = (time.perf_counter() - t0) * 1000
            return DinoClickResult(
                error=f"DINO: no confident candidate (best={ranking.best_score:.2f})",
                latency_ms=latency,
                dino_latency_ms=ranking.dino_latency_ms,
                florence_latency_ms=ranking.florence_latency_ms,
            )

        best = ranking.best
        cx, cy = best.center

        # ── Step 6: TemporalTracker.lock() ────────────────────
        if self._tracker:
            self._tracker.lock(
                image_bytes=frame.image_bytes,
                bbox=best.bbox,
                window_bounds=wb,
            )

        self._total_hits += 1
        latency = (time.perf_counter() - t0) * 1000
        self._avg_latency_ms = (
            self._avg_latency_ms * (self._total_hits - 1) + latency
        ) / self._total_hits

        _vlog("🎯", f"S0c DINO hit: '{query[:40]}' → ({cx},{cy}) "
                     f"conf={best.combined_score:.2f} {latency:.0f}ms")

        return DinoClickResult(
            success=True,
            x=cx, y=cy,
            confidence=best.combined_score,
            method="dino_florence",
            description=best.description,
            latency_ms=latency,
            dino_latency_ms=ranking.dino_latency_ms,
            florence_latency_ms=ranking.florence_latency_ms,
            refine_triggered=ranking.refine_triggered,
        )

    # ── Pre-click verification ────────────────────────────────

    async def verify_before_click(
        self,
        new_screenshot: bytes = b"",
        new_window_bounds: Optional[tuple[int, int, int, int]] = None,
    ) -> tuple[int, int, bool]:
        """
        Verify target position just before IPC click dispatch.

        Returns:
            (corrected_x, corrected_y, needs_redetect)
        """
        if not self._tracker:
            return (0, 0, True)

        if not new_screenshot and self._diff_capture:
            last = self._diff_capture.get_last_frame()
            new_screenshot = last.image_bytes if last else b""

        result = self._tracker.verify_and_correct(
            new_screenshot=new_screenshot,
            new_window_bounds=new_window_bounds,
        )

        return (result.corrected_x, result.corrected_y, result.needs_redetect)

    # ── Post-click feedback ───────────────────────────────────

    async def record_click_success(
        self,
        domain: str,
        action_key: str,
        rx: float,
        ry: float,
        visual_embedding: Optional[list[float]] = None,
        description: str = "",
        window_w: int = 1440,
        window_h: int = 900,
    ) -> None:
        """Record successful click for SemanticCache + ConfidenceFusion learning."""
        # Update SemanticCache
        if self._semantic_cache and domain and action_key:
            self._semantic_cache.store(
                domain=domain,
                action_key=action_key,
                rx=rx, ry=ry,
                visual_embedding=visual_embedding or [],
                description=description,
                window_w=window_w,
                window_h=window_h,
            )
            self._semantic_cache.record_outcome(domain, action_key, success=True)

        # Update ConfidenceFusion calibration
        try:
            from vision.confidence_fusion import get_confidence_fusion
            fusion = get_confidence_fusion()
            fusion.record_outcome(predicted=0.85, success=True)
        except Exception:
            pass

        # Update AdaptiveTopK domain learning
        try:
            from vision.adaptive_topk import get_adaptive_topk
            topk = get_adaptive_topk()
            topk.record_outcome(domain, k_used=5, complexity_score=0.5, success=True)
        except Exception:
            pass

    async def record_click_failure(
        self,
        domain: str,
        action_key: str,
        confidence: float = 0.0,
    ) -> None:
        """Record failed click for learning."""
        if self._semantic_cache and domain and action_key:
            self._semantic_cache.record_outcome(domain, action_key, success=False)

        try:
            from vision.confidence_fusion import get_confidence_fusion
            fusion = get_confidence_fusion()
            fusion.record_outcome(predicted=confidence, success=False)
        except Exception:
            pass

        try:
            from vision.adaptive_topk import get_adaptive_topk
            topk = get_adaptive_topk()
            topk.record_outcome(domain, k_used=5, complexity_score=0.5, success=False)
        except Exception:
            pass

    # ── Stats ─────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        result = {
            "ready": self.ready,
            "total_calls": self._total_calls,
            "total_hits": self._total_hits,
            "total_cache_hits": self._total_cache_hits,
            "total_fallbacks": self._total_fallbacks,
            "hit_rate": round(self._total_hits / max(1, self._total_calls), 3),
            "avg_latency_ms": round(self._avg_latency_ms, 1),
        }
        if self._detector:
            result["detector"] = self._detector.stats()
        if self._diff_capture:
            result["diff_capture"] = self._diff_capture.stats()
        if self._tracker:
            result["tracker"] = self._tracker.stats()
        if self._semantic_cache:
            result["semantic_cache"] = self._semantic_cache.stats()
        return result


# ══════════════════════════════════════════════════════════════════
# Module singleton
# ══════════════════════════════════════════════════════════════════

_instance: Optional[DinoIntegration] = None


def get_dino_integration() -> DinoIntegration:
    global _instance
    if _instance is None:
        _instance = DinoIntegration()
    return _instance


def init_dino_integration(model_server: Any) -> DinoIntegration:
    """Initialize singleton with model server (called from main.py)."""
    global _instance
    if _instance is None:
        _instance = DinoIntegration(model_server)
    elif not _instance.ready and model_server:
        _instance.inject_model_server(model_server)
    return _instance
