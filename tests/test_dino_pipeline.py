# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tests/test_dino_pipeline.py — Phidipus v2.3 Phase 8
═══════════════════════════════════════════════════════════════════════

Comprehensive test suite + benchmark for the Grounding DINO + Florence-2
vision pipeline.

Covers:
  T1:  Type system (detection_types.py)
  T2:  ONNX providers (onnx_providers.py)
  T3:  Model server lifecycle (model_server.py)
  T4:  DINO engine (dino_engine.py)
  T5:  Florence engine (florence_engine.py)
  T6:  Semantic ranker (semantic_ranker.py)
  T7:  Confidence fusion (confidence_fusion.py)
  T8:  Adaptive Top-K (adaptive_topk.py)
  T9:  Temporal tracker (temporal_tracker.py)
  T10: Differential capture (diff_capture.py)
  T11: Semantic cache (semantic_cache.py)
  T12: DINO detector orchestrator (dino_detector.py)
  T13: DINO integration bridge (dino_integration.py)
  T14: Perception pipeline integration (perception_pipeline.py)
  T15: Config schema (config/schema.py)
  T16: Main.py boot sequence references

Usage:
  python tests/test_dino_pipeline.py          # Run all tests
  python tests/test_dino_pipeline.py --quick   # Skip slow tests
  python tests/test_dino_pipeline.py --bench   # Run benchmarks (needs models)
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ── Ensure project root in path ──
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ══════════════════════════════════════════════════════════════════
# Test framework (minimal, no pytest dependency)
# ══════════════════════════════════════════════════════════════════

_passed = 0
_failed = 0
_errors: list[str] = []


def _ok(name: str) -> None:
    global _passed
    _passed += 1
    print(f"  \033[0;32m✅\033[0m {name}")


def _fail(name: str, reason: str) -> None:
    global _failed
    _failed += 1
    _errors.append(f"{name}: {reason}")
    print(f"  \033[0;31m❌\033[0m {name}: {reason}")


def _assert(cond: bool, name: str, reason: str = "assertion failed") -> None:
    if cond:
        _ok(name)
    else:
        _fail(name, reason)


# ══════════════════════════════════════════════════════════════════
# T1: Type system
# ══════════════════════════════════════════════════════════════════

def test_types():
    print("\n── T1: detection_types ──")
    from vision.detection_types import (
        BBox, Detection, RankedCandidate, DetectionResult, RankingResult,
        UIComplexity, RefineReason, DetectionSource, ModelInfo, ProviderInfo,
    )

    # BBox
    b1 = BBox(10, 20, 110, 70)
    _assert(b1.width == 100 and b1.height == 50, "BBox dimensions")
    _assert(b1.area == 5000, "BBox area")
    _assert(b1.center == (60, 45), "BBox center")
    _assert(b1.aspect_ratio == 2.0, "BBox aspect ratio")

    b2 = BBox(50, 30, 150, 80)
    iou = b1.iou(b2)
    _assert(0.1 < iou < 0.5, f"BBox IoU = {iou:.3f}")

    _assert(b1.contains_point(60, 45), "BBox contains center point")
    _assert(not b1.contains_point(200, 200), "BBox does not contain outside")

    rx, ry = b1.relative_coords(0, 0, 1440, 900)
    _assert(0 < rx < 0.1 and 0 < ry < 0.1, "BBox relative coords")

    d1 = b1.to_dict()
    b3 = BBox.from_dict(d1)
    _assert(b3 == b1, "BBox serialization roundtrip")

    # Detection
    det = Detection(bbox=b1, label="button", confidence=0.85)
    _assert(det.area == 5000, "Detection area proxy")
    _assert(det.source == DetectionSource.DINO, "Detection default source")

    # RankedCandidate
    rc = RankedCandidate(detection=det, semantic_score=0.9, combined_score=0.87)
    _assert(rc.confidence == 0.87, "RankedCandidate.confidence alias")

    # DetectionResult
    dr = DetectionResult(detections=[det], query="test")
    _assert(dr.count == 1 and dr.best.confidence == 0.85, "DetectionResult")

    # RankingResult
    rr = RankingResult(candidates=[rc])
    _assert(rr.best_score == 0.87 and not rr.is_ambiguous, "RankingResult")

    # Enums
    _assert(UIComplexity.SIMPLE.value == "simple", "UIComplexity enum")
    _assert(RefineReason.CRITICAL_ACTION.value == "critical_action", "RefineReason enum")


# ══════════════════════════════════════════════════════════════════
# T2: ONNX providers
# ══════════════════════════════════════════════════════════════════

def test_providers():
    print("\n── T2: onnx_providers ──")
    from vision.onnx_providers import (
        detect_hardware, detect_providers, get_provider_list,
        get_best_provider, provider_summary,
    )

    hw = detect_hardware()
    _assert("platform" in hw and "arch" in hw, "detect_hardware fields")
    _assert(isinstance(hw["ram_gb"], (int, float)), "RAM detection")

    providers = detect_providers()
    _assert(len(providers) >= 1, f"detect_providers found {len(providers)}")

    summary = provider_summary()
    _assert("ort_version" in summary, "provider_summary has ort_version")
    _assert("best" in summary, "provider_summary has best provider")


# ══════════════════════════════════════════════════════════════════
# T3: Model server
# ══════════════════════════════════════════════════════════════════

def test_model_server():
    print("\n── T3: model_server ──")
    from vision.model_server import ModelServer, get_model_server_sync

    server = get_model_server_sync()
    _assert(not server.dino_ready, "DINO not loaded (no model)")
    _assert(not server.florence_ready, "Florence not loaded (no model)")

    h = server.health()
    _assert("ready" in h and "dino_loaded" in h, "health check fields")

    s = server.stats()
    _assert("dino" in s and "florence" in s, "stats fields")


# ══════════════════════════════════════════════════════════════════
# T4: DINO engine
# ══════════════════════════════════════════════════════════════════

def test_dino_engine():
    print("\n── T4: dino_engine ──")
    from vision.dino_engine import (
        _nms, _encode_text_queries, DinoEngine,
    )
    from vision.model_server import get_model_server_sync

    # NMS
    boxes = [(10,10,50,50), (12,12,52,52), (200,200,300,300)]
    scores = [0.9, 0.85, 0.7]
    keep = _nms(boxes, scores, iou_threshold=0.5)
    _assert(len(keep) == 2, f"NMS: 3 → {len(keep)} (expected 2)")
    _assert(keep[0] == 0, "NMS keeps highest score first")

    # Text query encoding
    text = _encode_text_queries(["Post button", "Upload icon"])
    _assert("Post button . Upload icon" == text, f"Text encoding: {text}")

    text2 = _encode_text_queries(["single"])
    _assert(text2 == "single", "Single query encoding")

    # Engine (dry run)
    server = get_model_server_sync()
    engine = DinoEngine(server)
    _assert(not engine.ready, "Engine not ready without model")


# ══════════════════════════════════════════════════════════════════
# T5: Florence engine
# ══════════════════════════════════════════════════════════════════

def test_florence_engine():
    print("\n── T5: florence_engine ──")
    from vision.florence_engine import (
        FlorenceEngine, _cosine_similarity, _simple_text_embedding,
    )
    from vision.model_server import get_model_server_sync

    # Cosine similarity
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    _assert(abs(_cosine_similarity(a, b)) < 0.01, "Orthogonal vectors → cos≈0")
    _assert(abs(_cosine_similarity(a, a) - 1.0) < 0.01, "Identical vectors → cos≈1")

    # Text embedding
    emb1 = _simple_text_embedding("post button")
    emb2 = _simple_text_embedding("upload icon")
    _assert(len(emb1) == 768, "Embedding dimension = 768")
    _assert(_cosine_similarity(emb1, emb1) > 0.99, "Self-similarity ≈ 1.0")

    # Different texts should have lower similarity
    sim = _cosine_similarity(emb1, emb2)
    _assert(sim < 0.9, f"Different texts similarity = {sim:.3f} < 0.9")


# ══════════════════════════════════════════════════════════════════
# T6: Semantic ranker
# ══════════════════════════════════════════════════════════════════

def test_semantic_ranker():
    print("\n── T6: semantic_ranker ──")
    from vision.semantic_ranker import (
        exact_match, fuzzy_match, multi_level_match,
        description_quality, position_context_score,
    )
    from vision.detection_types import BBox

    _assert(exact_match("Post button", "Click Post button") == 1.0, "Exact match substring")
    _assert(exact_match("Đăng bài", "Nút Đăng bài Facebook") == 1.0, "Exact match Vietnamese")
    _assert(exact_match("abc", "xyz") == 0.0, "No match → 0.0")

    fm = fuzzy_match("Post button", "Submit button Facebook")
    _assert(fm > 0.0, f"Fuzzy match synonyms = {fm:.3f}")

    s, m = multi_level_match("Post button", "Post button toolbar")
    _assert(s >= 0.9 and m == "exact", f"Multi-level exact: {s:.3f} {m}")

    dq1 = description_quality("Blue button labeled Post in toolbar")
    dq2 = description_quality("small element")
    _assert(dq1 > dq2, f"Description quality: specific {dq1:.2f} > vague {dq2:.2f}")

    p1 = position_context_score(BBox(1300,30,1400,60), "close button", (0,0,1440,900))
    p2 = position_context_score(BBox(100,500,200,540), "close button", (0,0,1440,900))
    _assert(p1 > p2, f"Position: close@top-right {p1:.2f} > center {p2:.2f}")


# ══════════════════════════════════════════════════════════════════
# T7: Confidence fusion
# ══════════════════════════════════════════════════════════════════

def test_confidence_fusion():
    print("\n── T7: confidence_fusion ──")
    from vision.confidence_fusion import ConfidenceFusion, PROFILE_CRITICAL

    f = ConfidenceFusion()

    r1 = f.fuse(dino_conf=0.85, semantic_score=0.9, position_score=0.7)
    _assert(0.5 < r1.combined_score < 1.0, f"Basic fusion = {r1.combined_score:.4f}")

    r2 = f.fuse(dino_conf=0.85, semantic_score=0.9, position_score=0.7, is_critical=True)
    _assert(r2.combined_score < r1.combined_score, "Critical penalty applied")
    _assert(r2.profile_used == "critical", f"Critical profile: {r2.profile_used}")

    r3 = f.fuse(dino_conf=0.85, semantic_score=0.9, crossdomain_score=0.5, has_history=False)
    _assert(r3.profile_used == "new_domain", f"New domain profile: {r3.profile_used}")

    # Monotonic: higher inputs → higher output
    r_low = f.fuse(dino_conf=0.3, semantic_score=0.3)
    r_high = f.fuse(dino_conf=0.9, semantic_score=0.9)
    _assert(r_high.combined_score > r_low.combined_score, "Monotonic guarantee")

    # Batch
    batch = f.fuse_batch([
        {"dino_conf": 0.9, "semantic_score": 0.85},
        {"dino_conf": 0.4, "semantic_score": 0.3},
    ])
    _assert(batch[0].combined_score > batch[1].combined_score, "Batch ordering")


# ══════════════════════════════════════════════════════════════════
# T8: Adaptive Top-K
# ══════════════════════════════════════════════════════════════════

def test_adaptive_topk():
    print("\n── T8: adaptive_topk ──")
    from vision.adaptive_topk import AdaptiveTopK
    from vision.detection_types import BBox, Detection, DetectionSource

    topk = AdaptiveTopK()

    dets3 = [Detection(BBox(i*50,100,i*50+40,130), f"btn{i}", 0.9, DetectionSource.DINO) for i in range(3)]
    a1 = topk.analyze(dets3, screen_area=1440*900)
    _assert(a1.complexity.value == "simple" and a1.k == 2, f"Simple: K={a1.k}")

    dets25 = [Detection(BBox(i*50,100,i*50+40,130), f"btn{i}", 0.5, DetectionSource.DINO) for i in range(25)]
    a2 = topk.analyze(dets25, screen_area=1440*900)
    _assert(a2.complexity.value == "complex" and a2.k >= 5, f"Complex: K={a2.k}")

    a3 = topk.analyze(dets3, top1_confidence=0.97)
    _assert(a3.early_exit and a3.k == 1, "Early exit at 0.97")

    # Domain learning
    topk.record_outcome("test.com", 5, 0.7, True)
    s = topk.stats()
    _assert(s["total_analyses"] >= 3, f"Analyses count: {s['total_analyses']}")


# ══════════════════════════════════════════════════════════════════
# T9: Temporal tracker
# ══════════════════════════════════════════════════════════════════

def test_temporal_tracker():
    print("\n── T9: temporal_tracker ──")
    from vision.temporal_tracker import TemporalTracker
    from vision.detection_types import BBox

    t = TemporalTracker()
    t.lock(b"", BBox(500,300,600,340), (0,80,1440,900))
    _assert(t._target is not None, "Target locked")

    r1 = t.verify_and_correct(b"", (0,80,1440,900))
    _assert(r1.method == "unchanged" and r1.corrected_x == 550, "Unchanged verify")

    r2 = t.verify_and_correct(b"", (30,80,1440,900))
    _assert(r2.method == "window_offset" and r2.corrected_x != 550, "Window offset")

    _assert(t.stats()["total_locks"] == 1, "Stats tracking")


# ══════════════════════════════════════════════════════════════════
# T10: Differential capture
# ══════════════════════════════════════════════════════════════════

def test_diff_capture():
    print("\n── T10: diff_capture ──")
    from vision.diff_capture import roi_from_hint, _hamming_distance, DiffCapture

    r = roi_from_hint("top toolbar", (0,80,1440,900))
    _assert(r is not None and r.height < 200, "ROI top toolbar")

    r2 = roi_from_hint("right panel", (0,80,1440,900))
    _assert(r2 is not None and r2.x > 900, "ROI right panel")

    _assert(roi_from_hint("full", (0,0,1440,900)) is None, "ROI full → None")

    _assert(_hamming_distance(0xFF, 0x00) == 8, "Hamming 0xFF vs 0x00 = 8")
    _assert(_hamming_distance(0xFF, 0xFF) == 0, "Hamming identical = 0")


# ══════════════════════════════════════════════════════════════════
# T11: Semantic cache
# ══════════════════════════════════════════════════════════════════

def test_semantic_cache():
    print("\n── T11: semantic_cache ──")
    from vision.semantic_cache import SemanticCache

    c = SemanticCache(storage_path=Path("/tmp/_test_scache.json"))

    c.store("fb.com", "post", rx=0.8, ry=0.7,
            visual_embedding=[0.1]*768, window_w=1440, window_h=900)

    r1 = c.lookup("fb.com", "post", 1440, 900)
    _assert(r1.hit and r1.tier == "spatial", f"Tier 0 spatial: {r1.tier}")

    r2 = c.lookup("fb.com", "post", 1280, 800, [0.1]*768)
    _assert(r2.hit and r2.tier == "visual_verify", f"Tier 1 visual: {r2.tier}")

    c.store_anchor("fb.com", "logo", 0.05, 0.05, [0.2]*768)
    c.store("fb.com", "post2", rx=0.8, ry=0.7,
            visual_embedding=[0.1]*768, anchor_label="logo",
            anchor_dx=0.75, anchor_dy=0.65, window_w=1440, window_h=900)
    r3 = c.lookup("fb.com", "post2", 1280, 800)
    _assert(r3.hit and r3.tier == "anchor", f"Tier 2 anchor: {r3.tier}")

    s = c.stats()
    _assert(s["total_entries"] >= 2, f"Entries: {s['total_entries']}")

    try:
        os.remove("/tmp/_test_scache.json")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# T12: DinoDetector orchestrator
# ══════════════════════════════════════════════════════════════════

def test_dino_detector():
    print("\n── T12: dino_detector ──")
    from vision.dino_detector import (
        DinoDetector, light_filter,
    )
    from vision.detection_types import BBox, Detection, DetectionSource
    from vision.model_server import get_model_server_sync

    # Light filter
    dets = [
        Detection(BBox(10,10,15,15), "noise", 0.3, DetectionSource.DINO),
        Detection(BBox(100,100,250,140), "button", 0.85, DetectionSource.DINO),
        Detection(BBox(0,0,1440,900), "bg", 0.9, DetectionSource.DINO),
    ]
    filtered = light_filter(dets, screen_area=1440*900)
    _assert(len(filtered) == 1, f"Light filter: 3 → {len(filtered)}")
    _assert(filtered[0].label == "button", "Light filter keeps button")

    # DinoDetector (dry run)
    server = get_model_server_sync()
    det = DinoDetector(server)
    _assert(det._ranker is not None, "SemanticRanker wired")
    _assert(det._fusion is not None, "ConfidenceFusion wired")
    _assert(det._topk is not None, "AdaptiveTopK wired")

    async def _test():
        r = await det.find_element(b"", "test", domain="test.com")
        return r.count == 0  # no model → 0

    _assert(asyncio.run(_test()), "Dry run returns 0 candidates")


# ══════════════════════════════════════════════════════════════════
# T13: DinoIntegration bridge
# ══════════════════════════════════════════════════════════════════

def test_dino_integration():
    print("\n── T13: dino_integration ──")
    from vision.dino_integration import DinoIntegration, get_dino_integration

    di = get_dino_integration()
    _assert(not di.ready, "Not ready without model server")

    async def _test():
        r = await di.find_element("Post button", domain="fb.com")
        return r.skipped_reason == "not_ready"

    _assert(asyncio.run(_test()), "Graceful skip when not ready")

    s = di.stats()
    _assert("total_calls" in s and "total_fallbacks" in s, "Stats fields")


# ══════════════════════════════════════════════════════════════════
# T14: Perception pipeline integration
# ══════════════════════════════════════════════════════════════════

def test_perception_pipeline():
    print("\n── T14: perception_pipeline ──")
    import inspect
    from vision.perception_pipeline import PerceptionPipeline, PerceptionResult

    # Check new methods exist
    _assert(hasattr(PerceptionPipeline, "inject_model_server"), "inject_model_server method")
    _assert(hasattr(PerceptionPipeline, "perceive_dino"), "perceive_dino method")
    _assert(hasattr(PerceptionPipeline, "vision_stats"), "vision_stats method")

    # Check model_server parameter
    sig = inspect.signature(PerceptionPipeline.__init__)
    _assert("model_server" in sig.parameters, "model_server param in __init__")

    # Check PerceptionResult new fields
    fields = set(PerceptionResult.__dataclass_fields__.keys())
    _assert("dino_count" in fields, "PerceptionResult.dino_count")
    _assert("pipeline_used" in fields, "PerceptionResult.pipeline_used")
    _assert("dino_latency_ms" in fields, "PerceptionResult.dino_latency_ms")


# ══════════════════════════════════════════════════════════════════
# T15: Config schema
# ══════════════════════════════════════════════════════════════════

def test_config_schema():
    print("\n── T15: config_schema ──")
    from config.schema import DEFAULTS, CONFIG_SCHEMA

    v = DEFAULTS["vision"]
    _assert(v["vision_backend"] == "vlm_only", "Default backend: vlm_only")
    _assert(v["models_dir"] == "~/.phidipus/models", "Default models_dir")
    _assert(v["dino_conf_threshold"] == 0.3, "Default DINO threshold")
    _assert(v["max_refine_iterations"] == 3, "Default max refine iterations")

    # Check schema has new properties
    props = CONFIG_SCHEMA["properties"]["vision"]["properties"]
    _assert("vision_backend" in props, "Schema has vision_backend")
    _assert("models_dir" in props, "Schema has models_dir")
    _assert("dino_conf_threshold" in props, "Schema has dino_conf_threshold")


# ══════════════════════════════════════════════════════════════════
# T16: Main.py integration points
# ══════════════════════════════════════════════════════════════════

def test_main_integration():
    print("\n── T16: main.py ──")
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")

    _assert("get_model_server" in main_src, "main.py has ModelServer import")
    _assert("inject_model_server" in main_src, "main.py injects into PerceptionPipeline")
    _assert("_model_server.shutdown()" in main_src, "main.py shutdowns ModelServer")
    _assert("_vision_backend" in main_src, "main.py reads vision_backend config")
    _assert("init_dino_integration" in main_src, "main.py inits DinoIntegration")

    wf_src = (_ROOT / "core" / "workflow_executor.py").read_text(encoding="utf-8")
    _assert("_HAS_DINO" in wf_src, "workflow_executor has DINO flag")
    _assert("get_dino_integration" in wf_src, "workflow_executor uses DinoIntegration")
    _assert("S0c" in wf_src, "workflow_executor has S0c DINO path")


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def run_all():
    print("=" * 60)
    print("  Phidipus v2.3 — DINO+Florence Pipeline Test Suite")
    print("  Phase 8: Comprehensive Testing")
    print("=" * 60)

    tests = [
        test_types,
        test_providers,
        test_model_server,
        test_dino_engine,
        test_florence_engine,
        test_semantic_ranker,
        test_confidence_fusion,
        test_adaptive_topk,
        test_temporal_tracker,
        test_diff_capture,
        test_semantic_cache,
        test_dino_detector,
        test_dino_integration,
        test_perception_pipeline,
        test_config_schema,
        test_main_integration,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except Exception as exc:
            _fail(test_fn.__name__, str(exc)[:100])

    print("\n" + "=" * 60)
    total = _passed + _failed
    if _failed == 0:
        print(f"  \033[0;32m🎉 ALL {total} TESTS PASSED\033[0m")
    else:
        print(f"  \033[0;31m❌ {_failed}/{total} FAILED\033[0m")
        for err in _errors:
            print(f"    → {err}")
    print("=" * 60)

    return _failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
