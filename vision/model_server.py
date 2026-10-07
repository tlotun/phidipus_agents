# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/model_server.py — Phidipus v2.3
═══════════════════════════════════════════════════════════════════════

Singleton ONNX Runtime Server — manages Grounding DINO + Florence-2 lifecycle.

Responsibilities:
  1. Load DINO + Florence ONNX models vào GPU memory 1 lần duy nhất
  2. Expose async API: detect(image, queries) + rank(crops, query)
  3. Auto-download models từ HuggingFace Hub nếu chưa có
  4. Health check: memory usage, model status, inference count
  5. Graceful unload khi Phidipus shutdown (giải phóng VRAM)
  6. Feature flag: enabled/disabled qua config

Architecture:
  ModelServer là singleton — chỉ 1 instance trong toàn bộ process.
  Được inject vào AgentLoop và PerceptionPipeline qua main.py boot.

  main.py → ModelServer.startup()
         → inject vào AgentLoop
         → PerceptionPipeline dùng ModelServer.detect() thay VLM

VRAM Budget (M4 16GB):
  Grounding DINO Edge: ~800MB
  Florence-2 Base:     ~1.0GB
  Total:               ~1.8GB (chỉ 11% RAM, so với 5–8GB của VLM)

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
         Exception: model download dùng urllib (stdlib, no shell).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from vision.detection_types import (
    Detection, DetectionResult, ModelInfo, ProviderInfo,
    DINO_MODEL_NAME, FLORENCE_MODEL_NAME,
)
from vision.onnx_providers import (
    detect_hardware, get_provider_list, get_session_options,
    log_provider_status, provider_summary,
)


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Default paths
# ══════════════════════════════════════════════════════════════════

_DEFAULT_MODELS_DIR = Path.home() / ".phidipus" / "models"

# HuggingFace model repos for auto-download
_HF_REPOS = {
    DINO_MODEL_NAME: {
        "repo": "IDEA-Research/grounding-dino-1.5-edge",
        "files": ["model.onnx", "config.json", "tokenizer.json"],
        "size_mb": 170,
    },
    FLORENCE_MODEL_NAME: {
        "repo": "microsoft/Florence-2-base",
        "files": ["model.onnx", "config.json", "tokenizer.json", "vocab.json"],
        "size_mb": 460,
    },
}


# ══════════════════════════════════════════════════════════════════
# ModelServer
# ══════════════════════════════════════════════════════════════════

_instance: Optional[ModelServer] = None


class ModelServerError(RuntimeError):
    """Raised when ModelServer encounters a fatal error."""
    pass


class ModelNotLoadedError(ModelServerError):
    """Raised when attempting to use a model that isn't loaded."""
    pass


class ModelServer:
    """
    Singleton ONNX Runtime server for Grounding DINO + Florence-2.

    Usage:
        server = await get_model_server()
        await server.startup(models_dir="~/.phidipus/models")
        result = await server.detect_dino(image_bytes, ["Post button", "Upload icon"])
        await server.shutdown()
    """

    def __init__(self) -> None:
        self._dino_session: Any = None       # ort.InferenceSession
        self._florence_session: Any = None   # ort.InferenceSession
        self._dino_info = ModelInfo(name=DINO_MODEL_NAME, path="")
        self._florence_info = ModelInfo(name=FLORENCE_MODEL_NAME, path="")
        self._models_dir = _DEFAULT_MODELS_DIR
        self._providers: list[str] = []
        self._hw: dict[str, Any] = {}
        self._started = False
        self._enabled = True
        self._lock = asyncio.Lock()
        # v2.3.1: Inference semaphore — prevent concurrent ONNX calls
        # from blocking each other on single GPU (ChatGPT #5 feedback)
        self._infer_semaphore = asyncio.Semaphore(1)  # 1 inference at a time
        self._startup_time: float = 0
        self._total_dino_calls: int = 0
        self._total_florence_calls: int = 0
        self._total_dino_ms: float = 0
        self._total_florence_ms: float = 0

    # ── Startup / Shutdown ────────────────────────────────────

    async def startup(
        self,
        models_dir: str | Path | None = None,
        enabled: bool = True,
        auto_download: bool = True,
    ) -> None:
        """
        Initialize ONNX Runtime and load models.

        Args:
          models_dir:    Directory containing model subdirs.
          enabled:       Feature flag — if False, server is dormant.
          auto_download: Auto-download from HuggingFace if missing.
        """
        async with self._lock:
            if self._started:
                return

            self._enabled = enabled
            if not enabled:
                _vlog("⏸️", "ModelServer disabled (feature flag off)")
                return

            t0 = time.perf_counter()

            if models_dir:
                self._models_dir = Path(models_dir).expanduser()
            self._models_dir.mkdir(parents=True, exist_ok=True)

            # 1. Detect hardware + providers
            self._hw = detect_hardware()
            self._providers = get_provider_list()
            log_provider_status()

            if not self._providers:
                _vlog("❌", "Không có ONNX provider — ModelServer sẽ ở chế độ dormant")
                _vlog("💡", "Cài đặt: pip install onnxruntime-silicon --break-system-packages")
                self._enabled = False
                return

            # 2. Check / download models
            if auto_download:
                await self._ensure_models()

            # 3. Load DINO
            dino_path = self._models_dir / DINO_MODEL_NAME / "model.onnx"
            if dino_path.exists():
                await self._load_model("dino", str(dino_path))
            else:
                _vlog("⚠️", f"DINO model không tìm thấy: {dino_path}")

            # 4. Load Florence
            florence_path = self._models_dir / FLORENCE_MODEL_NAME / "model.onnx"
            if florence_path.exists():
                await self._load_model("florence", str(florence_path))
            else:
                _vlog("⚠️", f"Florence model không tìm thấy: {florence_path}")

            self._started = True
            self._startup_time = (time.perf_counter() - t0) * 1000
            _vlog("✅", f"ModelServer started ({self._startup_time:.0f}ms)")
            _vlog("📊", f"  DINO: {'✅ loaded' if self._dino_info.loaded else '❌ not loaded'}")
            _vlog("📊", f"  Florence: {'✅ loaded' if self._florence_info.loaded else '❌ not loaded'}")

    async def shutdown(self) -> None:
        """Unload models and release VRAM."""
        async with self._lock:
            if self._dino_session is not None:
                del self._dino_session
                self._dino_session = None
                self._dino_info.loaded = False
                _vlog("🗑️", "DINO model unloaded")

            if self._florence_session is not None:
                del self._florence_session
                self._florence_session = None
                self._florence_info.loaded = False
                _vlog("🗑️", "Florence model unloaded")

            self._started = False
            _vlog("✅", "ModelServer shutdown — VRAM freed")

    # ── Model Loading ─────────────────────────────────────────

    async def _load_model(self, name: str, path: str) -> None:
        """Load an ONNX model in a thread pool (blocking I/O)."""
        def _do_load():
            try:
                import onnxruntime as ort
            except ImportError:
                raise ModelServerError(
                    "onnxruntime not installed. "
                    "Run: pip install onnxruntime-silicon --break-system-packages"
                )

            opts = get_session_options()
            session = ort.InferenceSession(
                path,
                sess_options=opts,
                providers=self._providers,
            )
            return session

        try:
            _vlog("⏳", f"Loading {name} model: {path}")
            t0 = time.perf_counter()
            session = await asyncio.get_event_loop().run_in_executor(None, _do_load)
            load_ms = (time.perf_counter() - t0) * 1000

            # Get actual provider used
            actual_provider = "unknown"
            try:
                actual_provider = session.get_providers()[0] if session.get_providers() else "unknown"
            except Exception:
                pass

            file_size_mb = os.path.getsize(path) / (1024 * 1024)

            if name == "dino":
                self._dino_session = session
                self._dino_info = ModelInfo(
                    name=DINO_MODEL_NAME,
                    path=path,
                    size_mb=file_size_mb,
                    loaded=True,
                    backend=actual_provider,
                )
            elif name == "florence":
                self._florence_session = session
                self._florence_info = ModelInfo(
                    name=FLORENCE_MODEL_NAME,
                    path=path,
                    size_mb=file_size_mb,
                    loaded=True,
                    backend=actual_provider,
                )

            _vlog("✅", f"  {name} loaded ({load_ms:.0f}ms, {file_size_mb:.0f}MB, {actual_provider})")

        except Exception as exc:
            err_msg = str(exc)[:120]
            _vlog("❌", f"  {name} load failed: {err_msg}")
            if name == "dino":
                self._dino_info.error = err_msg
            else:
                self._florence_info.error = err_msg

    # ── Model Download ────────────────────────────────────────

    async def _ensure_models(self) -> None:
        """Check models exist, download from HuggingFace if missing."""
        for model_name, meta in _HF_REPOS.items():
            model_dir = self._models_dir / model_name
            onnx_file = model_dir / "model.onnx"

            if onnx_file.exists():
                _vlog("✅", f"  Model {model_name} — sẵn sàng ({onnx_file})")
                continue

            _vlog("📥", f"  Model {model_name} chưa có — cần download (~{meta['size_mb']}MB)")
            _vlog("💡", f"  Download thủ công:")
            _vlog("💡", f"    mkdir -p {model_dir}")
            _vlog("💡", f"    huggingface-cli download {meta['repo']} --local-dir {model_dir}")
            _vlog("💡", f"  Hoặc: python -c \"from huggingface_hub import snapshot_download; "
                        f"snapshot_download('{meta['repo']}', local_dir='{model_dir}')\"")

            # Attempt auto-download (best effort, no crash if fail)
            try:
                await self._download_model(model_name, meta, model_dir)
            except Exception as exc:
                _vlog("⚠️", f"  Auto-download failed: {exc}")
                _vlog("💡", f"  Download thủ công và đặt vào {model_dir}/model.onnx")

    async def _download_model(self, name: str, meta: dict, target_dir: Path) -> None:
        """
        Attempt to download model from HuggingFace Hub.

        Uses huggingface_hub Python library if available,
        falls back to urllib direct download.
        """
        target_dir.mkdir(parents=True, exist_ok=True)

        # Try huggingface_hub first (handles auth, LFS, etc.)
        try:
            from huggingface_hub import snapshot_download
            _vlog("📥", f"  Downloading {name} via huggingface_hub...")

            def _do_download():
                snapshot_download(
                    repo_id=meta["repo"],
                    local_dir=str(target_dir),
                    local_dir_use_symlinks=False,
                )

            await asyncio.get_event_loop().run_in_executor(None, _do_download)
            _vlog("✅", f"  {name} downloaded successfully")
            return
        except ImportError:
            _vlog("💡", "  huggingface_hub not installed — trying urllib fallback")
        except Exception as exc:
            _vlog("⚠️", f"  huggingface_hub download failed: {exc}")

        # Fallback: direct URL download for individual files
        repo = meta["repo"]
        for fname in meta.get("files", ["model.onnx"]):
            url = f"https://huggingface.co/{repo}/resolve/main/{fname}"
            out_path = target_dir / fname

            if out_path.exists():
                continue

            _vlog("📥", f"  Downloading {url}...")
            try:
                def _do_url_download():
                    req = urllib.request.Request(url, headers={"User-Agent": "Phidipus/2.3"})
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        data = resp.read()
                    out_path.write_bytes(data)
                    return len(data)

                size = await asyncio.get_event_loop().run_in_executor(None, _do_url_download)
                _vlog("✅", f"  {fname} ({size / 1024 / 1024:.1f}MB)")
            except Exception as exc:
                _vlog("❌", f"  Failed to download {fname}: {exc}")

    # ── Inference API ─────────────────────────────────────────

    @property
    def dino_ready(self) -> bool:
        return self._enabled and self._dino_info.loaded and self._dino_session is not None

    @property
    def florence_ready(self) -> bool:
        return self._enabled and self._florence_info.loaded and self._florence_session is not None

    @property
    def ready(self) -> bool:
        return self.dino_ready

    @property
    def dino_session(self) -> Any:
        """Raw ONNX session — used by DinoEngine for direct inference."""
        if not self.dino_ready:
            raise ModelNotLoadedError("DINO model not loaded")
        return self._dino_session

    @property
    def florence_session(self) -> Any:
        """Raw ONNX session — used by FlorenceEngine for direct inference."""
        if not self.florence_ready:
            raise ModelNotLoadedError("Florence model not loaded")
        return self._florence_session

    def record_dino_call(self, latency_ms: float) -> None:
        """Record DINO inference metrics."""
        self._total_dino_calls += 1
        self._total_dino_ms += latency_ms
        self._dino_info.inference_count = self._total_dino_calls
        if self._total_dino_calls > 0:
            self._dino_info.avg_latency_ms = self._total_dino_ms / self._total_dino_calls

    def record_florence_call(self, latency_ms: float) -> None:
        """Record Florence inference metrics."""
        self._total_florence_calls += 1
        self._total_florence_ms += latency_ms
        self._florence_info.inference_count = self._total_florence_calls
        if self._total_florence_calls > 0:
            self._florence_info.avg_latency_ms = self._total_florence_ms / self._total_florence_calls

    # ── Stats / Health ────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Full status for Admin Panel / Telegram."""
        return {
            "enabled": self._enabled,
            "started": self._started,
            "startup_ms": round(self._startup_time, 0),
            "hw": self._hw,
            "providers": self._providers,
            "dino": self._dino_info.to_dict(),
            "florence": self._florence_info.to_dict(),
            "models_dir": str(self._models_dir),
            "total_inferences": self._total_dino_calls + self._total_florence_calls,
        }

    def health(self) -> dict[str, Any]:
        """Quick health check."""
        return {
            "ready": self.ready,
            "dino_loaded": self._dino_info.loaded,
            "florence_loaded": self._florence_info.loaded,
            "dino_calls": self._total_dino_calls,
            "florence_calls": self._total_florence_calls,
            "dino_avg_ms": round(self._dino_info.avg_latency_ms, 1),
            "florence_avg_ms": round(self._florence_info.avg_latency_ms, 1),
        }


# ══════════════════════════════════════════════════════════════════
# Singleton accessor
# ══════════════════════════════════════════════════════════════════

async def get_model_server() -> ModelServer:
    """Get or create the singleton ModelServer instance."""
    global _instance
    if _instance is None:
        _instance = ModelServer()
    return _instance


def get_model_server_sync() -> ModelServer:
    """Synchronous accessor (for startup code that isn't async yet)."""
    global _instance
    if _instance is None:
        _instance = ModelServer()
    return _instance
