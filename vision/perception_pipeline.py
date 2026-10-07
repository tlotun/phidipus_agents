# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/perception_pipeline.py — Phidipus v2.3
Orchestrates the vision pipeline: capture → detect → gate → IPC.

Architecture contract (R-21 / F-5 fix):
  "Output routed to confidence_gate.py before any IPC dispatch.
   VLM fallback does not auto-execute (C-9)."

v2.3 upgrade: Dual-backend pipeline
  vision_backend = "dino_primary" (NEW):
    1. ScreenCapture.capture()
    2. DinoDetector.find_element()  — DINO + Florence (150–650ms)
    3. ConfidenceGate.check()       — gate R-21
    4. Return gated elements
    5. Fallback to VLM if DINO unavailable or ModelServer down

  vision_backend = "vlm_only" (LEGACY):
    1. ScreenCapture → ScreenParser → VLM → UIParser → ConfidenceGate
    (unchanged from v1.0)

  vision_backend = "hybrid" (A/B TEST):
    Run both, log comparison, use VLM result (safe rollout)

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess calls.
  R-21  ALL elements returned by perceive() have been through
        ConfidenceGate.check().
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from dataclasses import dataclass, field
from typing import Any

from config.config_loader import PhidipusConfig
from vision.confidence_gate import ConfidenceBelowThreshold, ConfidenceGate
from vision.screen_capture import CaptureRegion, ScreenCapture
from vision.screen_parser import ScreenLayout, ScreenParser
from vision.ui_parser import UIElement, UIParser
from vision.vlm_interface import VLMInterface
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;32m[{icon}]\033[0m  {msg}")

# ---------------------------------------------------------------------------
# Perception result
# ---------------------------------------------------------------------------

@dataclass
class PerceptionResult:
    """
    Output of one PerceptionPipeline.perceive() call.

    Attributes:
        elements:           Gated UIElements ready for IPC dispatch.
        layout:             ScreenLayout from ScreenParser.
        image_bytes:        Raw PNG screenshot bytes (for VLM input / logging).
        low_confidence_count: Number of VLM elements blocked by ConfidenceGate.
        a11y_count:         Elements from accessibility API.
        vlm_count:          Elements from VLM (after gating).
        dino_count:         Elements from DINO+Florence pipeline.
        pipeline_used:      "dino" | "vlm" | "a11y" | "hybrid"
        dino_latency_ms:    DINO+Florence total latency.
    """

    elements:             list[UIElement]
    layout:               ScreenLayout
    image_bytes:          bytes
    low_confidence_count: int = 0
    a11y_count:           int = 0
    vlm_count:            int = 0
    dino_count:           int = 0
    scale_factor:         float = 1.0
    pipeline_used:        str = "vlm"
    dino_latency_ms:      float = 0.0


# ---------------------------------------------------------------------------
# PerceptionPipeline
# ---------------------------------------------------------------------------

class PerceptionPipeline:
    """
    Full vision processing pipeline with confidence gating.

    Usage::

        pipeline = PerceptionPipeline(cfg, llm_client)
        result   = await pipeline.perceive()

        for elem in result.elements:
            # elem has been through ConfidenceGate — safe to dispatch
            payload = elem.to_click_payload()
            await ipc_client.send_action("mouse_click", payload)

    Args:
        cfg:      Validated PhidipusConfig.
        a11y_fn:  Optional async callable that returns a11y UIElements.
                  Signature: async () -> list[UIElement].
                  Pass None to skip a11y and rely on VLM only.
    """

    def __init__(
        self,
        cfg:     PhidipusConfig,
        a11y_fn: Any | None = None,
        model_server: Any | None = None,
    ) -> None:
        self._screen_capture = ScreenCapture()
        self._screen_parser  = ScreenParser()
        self._vlm            = VLMInterface(cfg)
        self._ui_parser      = UIParser(iou_threshold=cfg.vision.iou_dedup_threshold)
        self._gate           = ConfidenceGate(threshold=cfg.vision.confidence_threshold)
        self._prefer_a11y    = cfg.vision.prefer_accessibility
        self._a11y_fn        = a11y_fn

        # v2.3: DINO+Florence vision pipeline
        self._vision_backend: str = getattr(cfg.vision, "vision_backend", "vlm_only")
        self._model_server   = model_server
        self._dino_detector  = None  # lazy init when model_server injected
        self._dino_conf_threshold: float = getattr(cfg.vision, "dino_conf_threshold", 0.3)

        if model_server is not None and self._vision_backend != "vlm_only":
            try:
                from vision.dino_detector import DinoDetector
                self._dino_detector = DinoDetector(model_server)
                _vlog("🔬", f"PerceptionPipeline: DINO detector initialized "
                             f"(backend={self._vision_backend})")
            except Exception as exc:
                _vlog("⚠️", f"DINO detector init failed: {exc} — using VLM only")
                self._vision_backend = "vlm_only"

        # Phase 1: screen fingerprint cache — skip VLM when screen unchanged.
        self._cache_enabled:     bool                    = cfg.vision.screen_cache_enabled
        self._cache_ttl:         float                   = cfg.vision.screen_cache_ttl_seconds
        self._last_screen_hash:  str                     = ""
        self._last_percept:      PerceptionResult | None = None
        self._last_percept_time: float                   = 0.0

        # VLM circuit breaker: skip VLM after N consecutive timeouts.
        # Prevents agent from being stuck blind when model is too slow.
        self._vlm_consec_timeouts: int = 0
        self._vlm_skip_counter:    int = 0
        self._VLM_BREAK_AFTER:     int = 2  # open circuit after 2 timeouts
        self._VLM_RETRY_AFTER:     int = 5  # retry probe every 5 skips

        _log.info(
            "PerceptionPipeline initialised",
            extra={
                "prefer_a11y":          self._prefer_a11y,
                "confidence_threshold": self._gate.threshold,
                "has_a11y":             a11y_fn is not None,
                "screen_cache":         self._cache_enabled,
                "cache_ttl_s":          self._cache_ttl,
                "vision_backend":       self._vision_backend,
                "dino_ready":           self._dino_detector is not None,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_jpeg(image_obj: Any) -> tuple:
        """
        Synchronous JPEG encode + resize for VLM.

        Returns (jpeg_bytes, scale_factor) where scale_factor is used to
        convert VLM/LLM coordinates back to real screen coordinates.
        e.g. if image was 3440px wide → resized to 1920px, scale_factor = 1.792
        → real_x = vlm_x * scale_factor
        """
        buf = io.BytesIO()
        img = image_obj
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        # Resize if too large for VLM (ultrawide / dual monitor)
        # FIX: reduced 1920→1280 to cut VLM time on 3440px ultrawide screens
        MAX_VLM_WIDTH = 1280
        w, h = img.size
        scale_factor = 1.0
        if w > MAX_VLM_WIDTH:
            scale_factor = w / MAX_VLM_WIDTH
            new_h = int(h / scale_factor)
            img = img.resize((MAX_VLM_WIDTH, new_h), resample=1)  # LANCZOS

        img.save(buf, format="JPEG", quality=65, optimize=True)
        return buf.getvalue(), scale_factor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def perceive(
        self,
        region: CaptureRegion | None = None,
    ) -> PerceptionResult:
        """
        Run the full vision pipeline and return gated UI elements.

        Processing:
          1. Capture screenshot (or region).
          2. Parse screen layout.
          3. Call a11y API if available.
          4. Call VLM if a11y insufficient or prefer_accessibility=False.
          5. Merge elements via UIParser.
          6. Gate every VLM element through ConfidenceGate (R-21).
          7. Return PerceptionResult with only gated elements.

        Low-confidence VLM elements are EXCLUDED from the result
        (ConfidenceBelowThreshold is caught and counted, not re-raised).
        Accessibility elements (confidence=1.0) always pass through.

        Args:
            region: Optional screen region to capture.

        Returns:
            PerceptionResult with gated elements.
        """
        # ── 1. Capture ────────────────────────────────────────────────────
        # Capture ONCE and derive bytes from the same PIL image to ensure
        # VLM and ScreenParser operate on the exact same frame.
        # Two separate capture() calls yield slightly different frames,
        # desyncing layout ↔ VLM detections.
        # FIX NEW-1: capture() calls PIL.ImageGrab.grab() which blocks 10-50ms.
        # Wrap in to_thread to keep event loop responsive during parallel execution.
        image_obj = await asyncio.to_thread(self._screen_capture.capture, region)

        # Phase 1: PNG → JPEG for ~15× smaller payload to VLM.
        # FIX H-01: PIL JPEG encode (50–200ms for 1080p) runs in a thread
        # to avoid blocking the asyncio event loop.
        # FIX VLM-01: Returns (bytes, scale_factor) for coordinate correction.
        image_bytes, scale_factor = await asyncio.to_thread(
            self._encode_jpeg, image_obj
        )

        # ── 1b. Screen fingerprint cache (Phase 1) ───────────────────────
        # If the screen has not changed since the last perceive() call
        # (same MD5 hash) and the cache has not expired, reuse the
        # previous VLM result — saving 2–6s per step.
        if self._cache_enabled:
            screen_hash = hashlib.md5(image_bytes).hexdigest()
            now = time.monotonic()
            cache_age = now - self._last_percept_time

            if (screen_hash == self._last_screen_hash
                    and self._last_percept is not None
                    and cache_age < self._cache_ttl):
                _log.debug(
                    "PerceptionPipeline: screen unchanged — reusing cached result",
                    extra={
                        "hash":     screen_hash[:12],
                        "age_ms":   int(cache_age * 1000),
                        "ttl_s":    self._cache_ttl,
                    },
                )
                return self._last_percept

            # Update hash for next comparison (even if cache miss)
            self._last_screen_hash = screen_hash

        # ── 2. Parse layout ───────────────────────────────────────────────
        layout = self._screen_parser.parse(image_obj)

        # ── 3. Accessibility API ──────────────────────────────────────────
        a11y_elements: list[UIElement] = []
        if self._a11y_fn is not None:
            try:
                a11y_elements = await self._a11y_fn()
            except Exception as exc:
                _log.warning(
                    "PerceptionPipeline: a11y API failed — falling back to VLM",
                    extra={"error": str(exc)},
                )

        # ── 4. VLM detection (C-9: only when a11y is insufficient) ────────
        vlm_raw: list[dict[str, Any]] = []
        use_vlm = (
            not self._prefer_a11y or len(a11y_elements) < 3
        )

        # Circuit breaker: skip VLM when it has timed out too many times
        # consecutively. Every _VLM_RETRY_AFTER skips, attempt one probe.
        _circuit_open = self._vlm_consec_timeouts >= self._VLM_BREAK_AFTER
        if _circuit_open:
            self._vlm_skip_counter += 1
            _probe = (self._vlm_skip_counter % self._VLM_RETRY_AFTER == 0)
            if not _probe:
                use_vlm = False
                _log.info(
                    "PerceptionPipeline: VLM circuit open — skipping VLM",
                    extra={
                        "consec_timeouts": self._vlm_consec_timeouts,
                        "skip_count":      self._vlm_skip_counter,
                        "retry_after":     self._VLM_RETRY_AFTER,
                    },
                )

        if use_vlm:
            try:
                vlm_raw = await self._vlm.detect_elements(image_bytes)
                # Success: reset circuit breaker
                self._vlm_consec_timeouts = 0
                self._vlm_skip_counter    = 0
            except Exception as exc:
                self._vlm_consec_timeouts += 1
                _log.warning(
                    "PerceptionPipeline: VLM detection failed — using a11y only",
                    extra={
                        "error":           str(exc),
                        "consec_timeouts": self._vlm_consec_timeouts,
                        "circuit_opens_at": self._VLM_BREAK_AFTER,
                    },
                )
                # Circuit now open — log immediately if threshold just reached
                if self._vlm_consec_timeouts >= self._VLM_BREAK_AFTER:
                    _log.warning(
                        "PerceptionPipeline: VLM circuit OPENED — "
                        "skipping VLM for next steps to unblock agent",
                        extra={"consec_timeouts": self._vlm_consec_timeouts},
                    )

        vlm_elements_raw = self._ui_parser.from_vlm_detections(vlm_raw)

        # ── 5. Merge ──────────────────────────────────────────────────────
        merged = self._ui_parser.parse(
            a11y_elements=a11y_elements,
            vlm_detections=vlm_elements_raw,
        )

        # ── 6. Gate all elements through ConfidenceGate (R-21) ────────────
        gated:          list[UIElement] = []
        low_conf_count: int             = 0
        vlm_gated:      int             = 0

        for elem in merged:
            if elem.source == "a11y":
                # Accessibility elements have confidence=1.0 — always pass
                gated.append(elem)
                continue

            # VLM-sourced element — must go through ConfidenceGate (R-21)
            try:
                safe_payload = self._gate.check(
                    "element_click",
                    elem.to_click_payload(),
                )
                # Update element confidence from gated payload
                elem.confidence = safe_payload.get("confidence", elem.confidence)
                gated.append(elem)
                vlm_gated += 1
            except ConfidenceBelowThreshold as exc:
                low_conf_count += 1
                _log.debug(
                    "PerceptionPipeline: VLM element blocked by confidence gate",
                    extra={
                        "label":      exc.label,
                        "confidence": exc.confidence,
                        "threshold":  exc.threshold,
                    },
                )

        if low_conf_count:
            _log.info(
                "R-21: PerceptionPipeline blocked low-confidence VLM elements",
                extra={"blocked": low_conf_count, "passed": vlm_gated},
            )

        result = PerceptionResult(
            elements             = gated,
            layout               = layout,
            image_bytes          = image_bytes,
            low_confidence_count = low_conf_count,
            a11y_count           = len(a11y_elements),
            vlm_count            = vlm_gated,
            scale_factor         = scale_factor,
            pipeline_used        = "vlm",
        )

        _log.debug(
            "PerceptionPipeline: perceive() complete",
            extra={
                "total_elements": len(gated),
                "a11y":           len(a11y_elements),
                "vlm_gated":      vlm_gated,
                "vlm_blocked":    low_conf_count,
                "image_kb":       len(image_bytes) // 1024,
            },
        )

        # Phase 1: update screen cache
        if self._cache_enabled:
            self._last_percept      = result
            self._last_percept_time = time.monotonic()

        return result

    # ------------------------------------------------------------------
    # v2.3: Model Server injection (late binding from main.py)
    # ------------------------------------------------------------------

    def inject_model_server(self, model_server: Any) -> None:
        """
        Inject ModelServer after construction (called from main.py boot).

        If vision_backend is "dino_primary" or "hybrid", this creates the
        DinoDetector. If ModelServer is not ready, falls back to vlm_only.
        """
        self._model_server = model_server
        if self._vision_backend == "vlm_only":
            return

        try:
            from vision.dino_detector import DinoDetector
            self._dino_detector = DinoDetector(model_server)
            _vlog("🔬", f"PerceptionPipeline: DINO detector injected "
                         f"(backend={self._vision_backend})")
        except Exception as exc:
            _vlog("⚠️", f"DINO detector injection failed: {exc}")
            self._vision_backend = "vlm_only"

    # ------------------------------------------------------------------
    # v2.3: DINO-based targeted element detection
    # ------------------------------------------------------------------

    async def perceive_dino(
        self,
        query: str,
        region: CaptureRegion | None = None,
        is_critical: bool = False,
        window_bounds: tuple[int, int, int, int] | None = None,
    ) -> PerceptionResult:
        """
        Find a specific UI element using Grounding DINO + Florence-2.

        This is the NEW fast path (150–650ms vs. 1500–25000ms VLM).
        Used by SmartRouter/WorkflowExecutor for targeted element clicks.

        Falls back to legacy perceive() if DINO is not available.

        Args:
            query:          What to find ("Post button", "Upload icon").
            region:         Optional screen region to capture.
            is_critical:    True if action is irreversible (forces refine).
            window_bounds:  (x, y, w, h) of Chrome window.

        Returns:
            PerceptionResult with gated elements from DINO pipeline.
        """
        # Fallback to VLM if DINO not available
        if self._dino_detector is None or not self._dino_detector.ready:
            _vlog("⚠️", f"DINO not ready — fallback to VLM for '{query}'")
            return await self.perceive(region)

        # ── 1. Capture ────────────────────────────────────────
        image_obj = await asyncio.to_thread(self._screen_capture.capture, region)

        # Encode as PNG for DINO (lossless, better for detection)
        image_bytes = await asyncio.to_thread(
            self._screen_capture.capture_bytes, region
        )

        # Also get JPEG for layout parser
        jpeg_bytes, scale_factor = await asyncio.to_thread(
            self._encode_jpeg, image_obj
        )

        # ── 1b. Screen cache check ───────────────────────────
        if self._cache_enabled:
            screen_hash = hashlib.md5(jpeg_bytes).hexdigest()
            now = time.monotonic()
            cache_age = now - self._last_percept_time

            if (screen_hash == self._last_screen_hash
                    and self._last_percept is not None
                    and cache_age < self._cache_ttl):
                return self._last_percept

            self._last_screen_hash = screen_hash

        # ── 2. Parse layout ───────────────────────────────────
        layout = self._screen_parser.parse(image_obj)

        # ── 3. DINO + Florence detection ──────────────────────
        from vision.dino_detector import DinoDetector

        ranking_result = await self._dino_detector.find_element(
            image_bytes=image_bytes,
            query=query,
            is_critical=is_critical,
            window_bounds=window_bounds,
            screen_area=layout.width * layout.height if hasattr(layout, 'width') else 1440 * 900,
        )

        # ── 4. Convert to UIElements + Gate (R-21) ────────────
        gated: list[UIElement] = []
        low_conf_count = 0
        dino_count = 0

        for candidate in ranking_result.candidates:
            bbox = candidate.bbox
            # Create UIElement compatible with existing pipeline
            elem = UIElement(
                x=bbox.center_x,
                y=bbox.center_y,
                width=bbox.width,
                height=bbox.height,
                label=candidate.description or candidate.label,
                element_type="button",  # DINO detects interactive elements
                confidence=candidate.combined_score,
                source="dino",
            )

            # Gate through ConfidenceGate (R-21) — same as VLM path
            try:
                safe_payload = self._gate.check(
                    "element_click",
                    elem.to_click_payload(),
                )
                elem.confidence = safe_payload.get("confidence", elem.confidence)
                gated.append(elem)
                dino_count += 1
            except ConfidenceBelowThreshold:
                low_conf_count += 1

        if low_conf_count:
            _log.info(
                "R-21: DINO elements blocked by confidence gate",
                extra={"blocked": low_conf_count, "passed": dino_count},
            )

        result = PerceptionResult(
            elements             = gated,
            layout               = layout,
            image_bytes          = jpeg_bytes,
            low_confidence_count = low_conf_count,
            dino_count           = dino_count,
            scale_factor         = scale_factor,
            pipeline_used        = "dino",
            dino_latency_ms      = ranking_result.total_latency_ms,
        )

        _vlog("🎯", f"perceive_dino('{query}'): {dino_count} elements, "
                     f"{ranking_result.total_latency_ms:.0f}ms "
                     f"(best={ranking_result.best_score:.2f})")

        # Update screen cache
        if self._cache_enabled:
            self._last_percept      = result
            self._last_percept_time = time.monotonic()

        return result

    # ------------------------------------------------------------------
    # v2.3: Stats
    # ------------------------------------------------------------------

    def vision_stats(self) -> dict[str, Any]:
        """Vision pipeline stats for Admin Panel."""
        stats = {
            "vision_backend": self._vision_backend,
            "dino_available": self._dino_detector is not None,
        }
        if self._dino_detector is not None:
            stats["dino"] = self._dino_detector.stats()
        return stats
