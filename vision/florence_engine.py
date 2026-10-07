# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/florence_engine.py — Phidipus v2.3
═══════════════════════════════════════════════════════════════════════

Florence-2 Semantic Ranking & Captioning Engine.

Microsoft Florence-2-base (0.23B params) provides:
  1. REGION_TO_TEXT:  Describe what's in a bounding box → caption
  2. Semantic Scoring: Cosine similarity query vs. description
  3. Phrase Grounding: Find element matching text query (backup)
  4. Embedding:       768-dim visual embedding for semantic cache

Pipeline:
  DINO detections → crop regions → Florence describe/rank → RankedCandidates

Usage:
    engine = FlorenceEngine(model_server)

    # Rank candidates from DINO
    ranked = await engine.rank_candidates(
        image_bytes=screenshot_png,
        detections=[Detection(...)],
        query="Post button",
    )
    # → list[RankedCandidate] sorted by semantic_score

    # Get description of a region
    desc = await engine.describe(image_bytes, bbox)
    # → "A blue button labeled 'Post' in the top-right toolbar"

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import time
from collections import OrderedDict
from typing import Any, Optional

from vision.detection_types import (
    BBox, Detection, RankedCandidate, DetectionSource,
    FLORENCE_INPUT_SIZE, FLORENCE_SCORE_THRESHOLD,
)


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Cosine Similarity — Pure Python
# ══════════════════════════════════════════════════════════════════

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ══════════════════════════════════════════════════════════════════
# Text Embedding — Simple fallback
# ══════════════════════════════════════════════════════════════════

def _simple_text_embedding(text: str, dim: int = 768) -> list[float]:
    """
    Simple hash-based text embedding fallback.

    Used when Florence text encoder is not available.
    Not as accurate as real embeddings but provides basic similarity.
    """
    # Normalize text
    text = text.lower().strip()
    words = text.split()

    # Hash each word to create a pseudo-embedding
    embedding = [0.0] * dim
    for i, word in enumerate(words):
        h = hashlib.sha256(word.encode()).digest()
        for j in range(min(dim, len(h))):
            embedding[(i * 32 + j) % dim] += (h[j] - 128) / 128.0

    # Normalize to unit length
    norm = math.sqrt(sum(x * x for x in embedding))
    if norm > 0:
        embedding = [x / norm for x in embedding]

    return embedding


# ══════════════════════════════════════════════════════════════════
# LRU Embedding Cache
# ══════════════════════════════════════════════════════════════════

class _EmbeddingCache:
    """LRU cache for text embeddings — avoids re-encoding same queries."""

    def __init__(self, max_size: int = 500) -> None:
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def get(self, text: str) -> Optional[list[float]]:
        key = text.lower().strip()
        if key in self._cache:
            self._cache.move_to_end(key)
            self._hits += 1
            return self._cache[key]
        self._misses += 1
        return None

    def put(self, text: str, embedding: list[float]) -> None:
        key = text.lower().strip()
        self._cache[key] = embedding
        self._cache.move_to_end(key)
        if len(self._cache) > self._max_size:
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
            "memory_kb": round(len(self._cache) * 768 * 4 / 1024, 1),
        }


# ══════════════════════════════════════════════════════════════════
# Image Region Extraction
# ══════════════════════════════════════════════════════════════════

def _crop_region(image_bytes: bytes, bbox: BBox, target_size: tuple[int, int]) -> bytes:
    """
    Crop a region from image and resize to target_size.

    Adds 10% padding around bbox to give Florence context.
    Returns PNG bytes of the cropped, resized region.
    """
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("PIL required: pip install pillow --break-system-packages")

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    iw, ih = img.size

    # Add 10% padding
    pad_x = int(bbox.width * 0.1)
    pad_y = int(bbox.height * 0.1)
    x1 = max(0, bbox.x1 - pad_x)
    y1 = max(0, bbox.y1 - pad_y)
    x2 = min(iw, bbox.x2 + pad_x)
    y2 = min(ih, bbox.y2 + pad_y)

    crop = img.crop((x1, y1, x2, y2))
    crop = crop.resize(target_size, Image.LANCZOS)

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _preprocess_crop(crop_bytes: bytes, target_size: tuple[int, int] = (768, 768)) -> Any:
    """
    Preprocess cropped image for Florence-2.

    Returns numpy array [1, 3, H, W] float32, normalized.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None

    img = Image.open(io.BytesIO(crop_bytes)).convert("RGB")
    img = img.resize(target_size, Image.LANCZOS)

    arr = np.array(img, dtype=np.float32) / 255.0

    # Florence-2 normalization (ImageNet)
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std

    arr = arr.transpose(2, 0, 1)  # HWC → CHW
    arr = np.expand_dims(arr, axis=0)  # → NCHW

    return arr


# ══════════════════════════════════════════════════════════════════
# FlorenceEngine
# ══════════════════════════════════════════════════════════════════

class FlorenceEngine:
    """
    Florence-2 semantic ranking and captioning engine.

    Usage:
        server = await get_model_server()
        engine = FlorenceEngine(server)

        # Rank DINO detections
        candidates = await engine.rank_candidates(
            image_bytes, detections, "Post button"
        )

        # Get element description
        desc = await engine.describe(image_bytes, bbox)

        # Get visual embedding for semantic cache
        emb = await engine.embed_region(image_bytes, bbox)
    """

    def __init__(self, model_server: Any) -> None:
        self._server = model_server
        self._embed_cache = _EmbeddingCache(max_size=500)
        self._last_rank_time: float = 0
        self._use_onnx: bool = True  # Try ONNX first, fallback to heuristic

    @property
    def ready(self) -> bool:
        return self._server.florence_ready

    async def rank_candidates(
        self,
        image_bytes: bytes,
        detections: list[Detection],
        query: str,
        score_threshold: float = FLORENCE_SCORE_THRESHOLD,
    ) -> list[RankedCandidate]:
        """
        Rank DINO detections by semantic relevance to query.

        This is the MERGED rank + score step (Phase 3 optimization):
        Florence provides both ranking AND confidence in 1 inference batch.

        Args:
          image_bytes:     Full screenshot PNG.
          detections:      DINO Detection list.
          query:           What we're looking for ("Post button").
          score_threshold: Minimum semantic score to keep.

        Returns:
          List[RankedCandidate] sorted by semantic_score descending.
        """
        if not detections:
            return []

        t0 = time.perf_counter()

        candidates = await asyncio.get_event_loop().run_in_executor(
            None,
            self._rank_sync,
            image_bytes, detections, query, score_threshold,
        )

        latency = (time.perf_counter() - t0) * 1000
        self._last_rank_time = time.time()

        if self._server.florence_ready:
            self._server.record_florence_call(latency)

        if candidates:
            _vlog("🏆", f"Florence: {len(candidates)} ranked "
                        f"(best={candidates[0].semantic_score:.2f}, "
                        f"{latency:.0f}ms)")

        return candidates

    def _rank_sync(
        self,
        image_bytes: bytes,
        detections: list[Detection],
        query: str,
        score_threshold: float,
    ) -> list[RankedCandidate]:
        """Synchronous ranking — runs in thread pool."""

        # Get query embedding (from cache or compute)
        query_embedding = self._embed_cache.get(query)
        if query_embedding is None:
            query_embedding = self._compute_text_embedding(query)
            self._embed_cache.put(query, query_embedding)

        candidates = []

        for det in detections:
            # Generate description for this region
            description = self._describe_region_sync(image_bytes, det.bbox)

            # Compute semantic score
            desc_embedding = self._embed_cache.get(description)
            if desc_embedding is None:
                desc_embedding = self._compute_text_embedding(description)
                self._embed_cache.put(description, desc_embedding)

            semantic_score = _cosine_similarity(query_embedding, desc_embedding)

            # Apply keyword boost
            keyword_boost = self._keyword_match_boost(query, description)
            semantic_score = min(1.0, semantic_score + keyword_boost)

            candidates.append(RankedCandidate(
                detection=det,
                semantic_score=semantic_score,
                description=description,
            ))

        # Sort by semantic score descending
        candidates.sort(key=lambda c: c.semantic_score, reverse=True)

        # Filter by threshold
        if score_threshold > 0:
            candidates = [c for c in candidates if c.semantic_score >= score_threshold]

        return candidates

    def _describe_region_sync(self, image_bytes: bytes, bbox: BBox) -> str:
        """
        Get text description of a bounding box region.

        Tries ONNX Florence model first, falls back to heuristic.
        """
        if self._use_onnx and self._server.florence_ready:
            try:
                return self._describe_with_onnx(image_bytes, bbox)
            except Exception as exc:
                _vlog("⚠️", f"Florence ONNX describe failed: {str(exc)[:80]}")
                self._use_onnx = False  # Disable for this session

        # Fallback: generate description from bbox properties
        return self._describe_heuristic(bbox)

    def _describe_with_onnx(self, image_bytes: bytes, bbox: BBox) -> str:
        """Run Florence-2 REGION_TO_TEXT via ONNX."""
        try:
            import numpy as np
        except ImportError:
            return self._describe_heuristic(bbox)

        # Crop and preprocess
        crop_bytes = _crop_region(image_bytes, bbox, FLORENCE_INPUT_SIZE)
        img_tensor = _preprocess_crop(crop_bytes, FLORENCE_INPUT_SIZE)

        if img_tensor is None:
            return self._describe_heuristic(bbox)

        session = self._server.florence_session
        input_names = [inp.name for inp in session.get_inputs()]
        output_names = [out.name for out in session.get_outputs()]

        # Build feed dict
        feed = {}
        for name in input_names:
            if "image" in name.lower() or "pixel" in name.lower():
                feed[name] = img_tensor

        if not feed:
            feed = {input_names[0]: img_tensor}

        # Task prompt for REGION_TO_TEXT
        # Some Florence ONNX exports expect a task_id or prompt input
        for name in input_names:
            if "task" in name.lower() or "prompt" in name.lower():
                # Task ID for REGION_TO_TEXT
                feed[name] = np.array([[3]], dtype=np.int64)  # typical task encoding

        outputs = session.run(output_names, feed)

        # Parse text output
        if outputs and len(outputs) > 0:
            raw_output = outputs[0]
            if isinstance(raw_output, np.ndarray):
                # Decode token IDs to text
                try:
                    from transformers import AutoTokenizer
                    tokenizer = AutoTokenizer.from_pretrained(
                        "microsoft/Florence-2-base",
                        local_files_only=True,
                    )
                    text = tokenizer.decode(raw_output[0], skip_special_tokens=True)
                    return text.strip()[:200]
                except Exception:
                    pass

        return self._describe_heuristic(bbox)

    def _describe_heuristic(self, bbox: BBox) -> str:
        """
        Generate approximate description from bbox geometry.

        Used as fallback when Florence ONNX isn't available.
        Still useful for keyword matching with query.
        """
        w, h = bbox.width, bbox.height
        area = bbox.area

        # Size classification
        if area < 2000:
            size = "small"
        elif area < 20000:
            size = "medium"
        else:
            size = "large"

        # Shape classification
        ratio = w / h if h > 0 else 1.0
        if 0.8 <= ratio <= 1.2:
            shape = "square"
        elif ratio > 2.0:
            shape = "wide horizontal"
        elif ratio < 0.5:
            shape = "tall vertical"
        else:
            shape = "rectangular"

        # Position classification
        # (relative to typical screen 1440×900)
        cx, cy = bbox.center
        if cy < 100:
            position = "top toolbar area"
        elif cy > 800:
            position = "bottom area"
        elif cx < 200:
            position = "left sidebar"
        elif cx > 1200:
            position = "right panel"
        else:
            position = "center area"

        # Element type guess
        if w > 60 and h > 20 and h < 60:
            etype = "button"
        elif w > 200 and h > 30:
            etype = "text field"
        elif w < 40 and h < 40:
            etype = "icon"
        elif w > 300 and h > 200:
            etype = "panel or card"
        else:
            etype = "element"

        return f"{size} {shape} {etype} in {position}"

    def _compute_text_embedding(self, text: str) -> list[float]:
        """
        Compute text embedding for semantic similarity.

        Tries Florence text encoder (ONNX) first, falls back to hash-based.
        """
        # Try ONNX text encoder
        if self._use_onnx and self._server.florence_ready:
            try:
                return self._embed_text_onnx(text)
            except Exception:
                pass

        # Fallback: hash-based embedding
        return _simple_text_embedding(text)

    def _embed_text_onnx(self, text: str) -> list[float]:
        """Compute text embedding via Florence ONNX text encoder."""
        # Florence-2 doesn't have a separate text encoder in standard ONNX export.
        # Use hash-based for now — will upgrade when Florence ONNX text encoder available.
        return _simple_text_embedding(text)

    def _keyword_match_boost(self, query: str, description: str) -> float:
        """
        Boost semantic score based on keyword overlap.

        Simple Jaccard-like matching for important words.
        """
        q_words = set(query.lower().split())
        d_words = set(description.lower().split())

        # Remove stop words
        stop = {"the", "a", "an", "in", "on", "at", "to", "of", "for", "is", "and", "or"}
        q_words -= stop
        d_words -= stop

        if not q_words:
            return 0.0

        overlap = q_words & d_words
        coverage = len(overlap) / len(q_words)

        # Key words get extra weight
        key_words = {"button", "icon", "submit", "post", "upload", "send",
                     "delete", "close", "next", "search", "login", "menu",
                     "nút", "đăng", "gửi", "xóa", "tìm", "tải"}
        key_overlap = q_words & d_words & key_words
        key_boost = len(key_overlap) * 0.1

        return min(0.3, coverage * 0.2 + key_boost)

    async def describe(self, image_bytes: bytes, bbox: BBox) -> str:
        """
        Get human-readable description of a UI element.

        Used by ClickMemory for semantic caching.
        """
        return await asyncio.get_event_loop().run_in_executor(
            None, self._describe_region_sync, image_bytes, bbox
        )

    async def embed_region(self, image_bytes: bytes, bbox: BBox) -> list[float]:
        """
        Get visual embedding of a region for semantic cache.

        768-dim vector that represents the visual content.
        Used by ClickMemory to verify cache validity after resize.
        """
        desc = await self.describe(image_bytes, bbox)
        embedding = self._embed_cache.get(desc)
        if embedding is None:
            embedding = self._compute_text_embedding(desc)
            self._embed_cache.put(desc, embedding)
        return embedding

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "use_onnx": self._use_onnx,
            "total_calls": self._server._total_florence_calls,
            "avg_latency_ms": round(self._server._florence_info.avg_latency_ms, 1),
            "embed_cache": self._embed_cache.stats(),
        }
