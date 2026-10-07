# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/onnx_providers.py — Phidipus v2.3
═══════════════════════════════════════════════════════════════════════

ONNX Runtime Backend Auto-Detection.

Detects hardware → selects optimal Execution Provider:
  Apple Silicon M1–M4  → CoreMLExecutionProvider (fastest)
                       → fallback: CPUExecutionProvider
  Intel Mac            → CPUExecutionProvider
  NVIDIA GPU           → CUDAExecutionProvider (nếu build có CUDA)
  Linux/Windows CPU    → CPUExecutionProvider

Benchmark on startup: chạy 10 inference warm-up, log avg latency.

Usage:
    providers = detect_providers()
    # → [ProviderInfo("CoreMLExecutionProvider", available=True, priority=0)]

    provider_list = get_provider_list()
    # → ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    # Dùng trực tiếp cho onnxruntime.InferenceSession(model, providers=provider_list)

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
         Exception: platform detection dùng platform.machine() (read-only).
"""
from __future__ import annotations

import os
import platform
import time
from typing import Any, Optional

from vision.detection_types import ProviderInfo


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Hardware Detection
# ══════════════════════════════════════════════════════════════════

def detect_hardware() -> dict[str, Any]:
    """
    Detect hardware capabilities.

    Returns dict with:
      platform: "darwin" | "linux" | "windows"
      arch:     "arm64" | "x86_64"
      is_apple_silicon: bool
      chip:     "M1" | "M2" | "M3" | "M4" | "unknown"
      ram_gb:   approximate total RAM
    """
    system = platform.system().lower()
    arch = platform.machine().lower()
    is_apple = system == "darwin" and arch in ("arm64", "aarch64")

    chip = "unknown"
    if is_apple:
        # Detect specific Apple Silicon generation
        # sysctl is read-only, but we avoid subprocess — use platform info
        try:
            cpu_brand = platform.processor()
            if "m4" in cpu_brand.lower():
                chip = "M4"
            elif "m3" in cpu_brand.lower():
                chip = "M3"
            elif "m2" in cpu_brand.lower():
                chip = "M2"
            elif "m1" in cpu_brand.lower():
                chip = "M1"
            else:
                # Apple Silicon but unknown generation
                chip = "Apple Silicon"
        except Exception:
            chip = "Apple Silicon"

    ram_gb = 0
    try:
        import resource
        # This gives soft limit, not total — fallback to os.sysconf
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            ram_gb = round((pages * page_size) / (1024 ** 3), 1)
        except (ValueError, OSError):
            ram_gb = 16  # safe default for M4
    except ImportError:
        ram_gb = 16

    return {
        "platform": system,
        "arch": arch,
        "is_apple_silicon": is_apple,
        "chip": chip,
        "ram_gb": ram_gb,
    }


# ══════════════════════════════════════════════════════════════════
# ONNX Provider Detection
# ══════════════════════════════════════════════════════════════════

def _check_onnxruntime_available() -> bool:
    """Check if onnxruntime is importable."""
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _get_available_providers() -> list[str]:
    """Get list of ONNX Runtime execution providers available on this system."""
    try:
        import onnxruntime as ort
        return ort.get_available_providers()
    except ImportError:
        return []
    except Exception:
        return []


def detect_providers() -> list[ProviderInfo]:
    """
    Detect and rank available ONNX execution providers.

    v2.3.1 priority order (Grok feedback: MLX first on Apple Silicon):
      Apple Silicon: MLX → CoreML → Metal → CPU
      Others:        CUDA → CPU

    Returns list sorted by priority (lower = better).
    """
    hw = detect_hardware()
    available = _get_available_providers()

    providers: list[ProviderInfo] = []

    # ── Apple Silicon: MLX > CoreML > Metal > CPU ─────────────
    if hw["is_apple_silicon"]:
        # MLX native — fastest on Apple Silicon (2.6× over ONNX in some models)
        # v2.3.1: MLX first priority (Grok feedback)
        mlx_available = _check_mlx_available()
        providers.append(ProviderInfo(
            name="MLXProvider",
            available=mlx_available,
            priority=-1,  # highest priority
            note=f"MLX native Apple Silicon ({hw['chip']}) — fastest",
        ))

        # CoreML — uses ANE + GPU
        providers.append(ProviderInfo(
            name="CoreMLExecutionProvider",
            available="CoreMLExecutionProvider" in available,
            priority=0,
            note=f"Apple Neural Engine + GPU ({hw['chip']})",
        ))
        # Metal Performance Shaders
        providers.append(ProviderInfo(
            name="MetalPerformanceShadersExecutionProvider",
            available="MetalPerformanceShadersExecutionProvider" in available,
            priority=1,
            note="Metal GPU compute",
        ))

    # ── CUDA for NVIDIA ────────────────────────────────────────
    providers.append(ProviderInfo(
        name="CUDAExecutionProvider",
        available="CUDAExecutionProvider" in available,
        priority=2 if not hw["is_apple_silicon"] else 99,
        note="NVIDIA GPU (CUDA)",
    ))

    # ── CPU always available ───────────────────────────────────
    providers.append(ProviderInfo(
        name="CPUExecutionProvider",
        available="CPUExecutionProvider" in available,
        priority=10,  # lowest priority, always fallback
        note=f"CPU ({hw['arch']})",
    ))

    # Sort by priority, available first
    providers.sort(key=lambda p: (not p.available, p.priority))
    return providers


def _check_mlx_available() -> bool:
    """Check if MLX framework is available (Apple Silicon only)."""
    try:
        import mlx.core  # noqa: F401
        return True
    except ImportError:
        return False


def get_provider_list() -> list[str]:
    """
    Get ordered list of provider names for onnxruntime.InferenceSession().

    Only includes available providers, sorted by priority.
    Ready to pass directly to:
      session = ort.InferenceSession(model_path, providers=get_provider_list())
    """
    providers = detect_providers()
    return [p.name for p in providers if p.available]


def get_best_provider() -> Optional[ProviderInfo]:
    """Get the single best available provider."""
    providers = detect_providers()
    for p in providers:
        if p.available:
            return p
    return None


# ══════════════════════════════════════════════════════════════════
# Session Options
# ══════════════════════════════════════════════════════════════════

def get_session_options(
    *,
    inter_threads: int = 1,
    intra_threads: int = 4,
    optimization_level: str = "all",
) -> Any:
    """
    Create optimized ONNX Runtime SessionOptions.

    Args:
      inter_threads: Number of threads for inter-op parallelism.
      intra_threads: Number of threads for intra-op parallelism.
      optimization_level: "all" | "basic" | "extended" | "disabled"

    Returns:
      ort.SessionOptions instance, or None if onnxruntime not available.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return None

    opts = ort.SessionOptions()
    opts.inter_op_num_threads = inter_threads
    opts.intra_op_num_threads = intra_threads

    level_map = {
        "disabled": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
        "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
        "extended": ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
        "all": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
    }
    opts.graph_optimization_level = level_map.get(
        optimization_level, ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    )

    # Memory optimization
    opts.enable_mem_pattern = True
    opts.enable_mem_reuse = True

    # Execution mode: sequential for latency-sensitive single-image inference
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

    return opts


# ══════════════════════════════════════════════════════════════════
# Benchmark
# ══════════════════════════════════════════════════════════════════

def benchmark_provider(
    model_path: str,
    provider_name: str,
    warmup_runs: int = 3,
    benchmark_runs: int = 10,
) -> float:
    """
    Benchmark a single provider on a model.

    Args:
      model_path:    Path to ONNX model file.
      provider_name: ONNX Runtime provider name.
      warmup_runs:   Number of warm-up inferences (not timed).
      benchmark_runs: Number of timed inferences.

    Returns:
      Average latency in milliseconds, or -1.0 if failed.
    """
    try:
        import onnxruntime as ort
        import numpy as np
    except ImportError:
        return -1.0

    try:
        opts = get_session_options()
        session = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=[provider_name],
        )

        # Get input shape and create dummy data
        inputs = {}
        for inp in session.get_inputs():
            shape = [s if isinstance(s, int) else 1 for s in inp.shape]
            dtype_map = {
                "tensor(float)": np.float32,
                "tensor(int64)": np.int64,
                "tensor(int32)": np.int32,
            }
            dtype = dtype_map.get(inp.type, np.float32)
            inputs[inp.name] = np.random.randn(*shape).astype(dtype)

        # Warm-up
        for _ in range(warmup_runs):
            session.run(None, inputs)

        # Benchmark
        latencies = []
        for _ in range(benchmark_runs):
            t0 = time.perf_counter()
            session.run(None, inputs)
            latencies.append((time.perf_counter() - t0) * 1000)

        avg_ms = sum(latencies) / len(latencies) if latencies else -1.0
        return round(avg_ms, 2)

    except Exception as exc:
        _vlog("⚠️", f"Benchmark {provider_name} failed: {exc}")
        return -1.0


# ══════════════════════════════════════════════════════════════════
# Summary / Diagnostic
# ══════════════════════════════════════════════════════════════════

def provider_summary() -> dict[str, Any]:
    """
    Full diagnostic summary for Admin Panel / logging.

    Returns:
      hw:         Hardware info dict
      ort_version: onnxruntime version or "not installed"
      providers:  List of ProviderInfo dicts
      best:       Name of best provider or "none"
      ready:      True if at least 1 provider available
    """
    hw = detect_hardware()
    providers = detect_providers()

    ort_version = "not installed"
    try:
        import onnxruntime as ort
        ort_version = ort.__version__
    except ImportError:
        pass

    best = get_best_provider()

    return {
        "hw": hw,
        "ort_version": ort_version,
        "providers": [p.to_dict() for p in providers],
        "best": best.name if best else "none",
        "ready": best is not None,
    }


def log_provider_status() -> None:
    """Print provider status to console (Vietnamese)."""
    summary = provider_summary()
    hw = summary["hw"]

    _vlog("🖥️", f"Hardware: {hw['chip']} | {hw['arch']} | RAM {hw['ram_gb']}GB")
    _vlog("📦", f"ONNX Runtime: {summary['ort_version']}")

    for p in summary["providers"]:
        status = "✅" if p["available"] else "❌"
        _vlog("🔧", f"  {status} {p['name']} — {p['note']}")

    if summary["ready"]:
        _vlog("✅", f"Best provider: {summary['best']}")
    else:
        _vlog("❌", "Không có ONNX provider nào — cần install onnxruntime")
        _vlog("💡", "pip install onnxruntime-silicon --break-system-packages")
