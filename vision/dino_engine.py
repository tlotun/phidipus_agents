# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/dino_engine.py — Phidipus v2.3
═══════════════════════════════════════════════════════════════════════

Grounding DINO ONNX Inference Engine.

Wraps the Grounding DINO 1.5 Edge ONNX model for open-vocabulary
object detection on desktop UI screenshots.

Pipeline:
  1. Preprocess: resize image → 640×640, normalize, pad
  2. Encode text: tokenize queries với BERT tokenizer
  3. Inference: ONNX session.run() trên Metal/CoreML/CPU
  4. Postprocess: NMS, filter by confidence, convert to Detection list

Usage:
    engine = DinoEngine(model_server)
    detections = await engine.detect(
        image_bytes=screenshot_png,
        queries=["Post button", "Upload icon", "Text area"],
        conf_threshold=0.3,
        input_size=(640, 640),
    )
    # → list[Detection] with bbox, label, confidence

Multi-query batching:
    Grounding DINO hỗ trợ multiple queries trong 1 inference pass.
    Queries được nối bằng " . " separator:
      "Post button . Upload icon . Text area"
    Model detect tất cả cùng lúc → tiết kiệm GPU time.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
"""
from __future__ import annotations

import asyncio
import io
import math
import time
from typing import Any, Optional

from vision.detection_types import (
    BBox, Detection, DetectionResult, DetectionSource,
    DINO_CONF_THRESHOLD, IOU_OVERLAP_THRESHOLD,
)


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# NMS (Non-Maximum Suppression) — Pure Python
# ══════════════════════════════════════════════════════════════════

def _nms(
    boxes: list[tuple[int, int, int, int]],
    scores: list[float],
    iou_threshold: float = 0.5,
) -> list[int]:
    """
    Non-Maximum Suppression — keep highest-scoring non-overlapping boxes.

    Args:
      boxes:  List of (x1, y1, x2, y2) tuples.
      scores: Corresponding confidence scores.
      iou_threshold: IoU above this → suppress lower-scoring box.

    Returns:
      List of indices to keep.
    """
    if not boxes:
        return []

    # Sort by score descending
    indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    keep = []

    while indices:
        i = indices.pop(0)
        keep.append(i)

        remaining = []
        bx1, by1, bx2, by2 = boxes[i]
        area_i = max(0, bx2 - bx1) * max(0, by2 - by1)

        for j in indices:
            jx1, jy1, jx2, jy2 = boxes[j]
            area_j = max(0, jx2 - jx1) * max(0, jy2 - jy1)

            # Intersection
            ix1 = max(bx1, jx1)
            iy1 = max(by1, jy1)
            ix2 = min(bx2, jx2)
            iy2 = min(by2, jy2)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)

            # IoU
            union = area_i + area_j - inter
            iou = inter / union if union > 0 else 0.0

            if iou < iou_threshold:
                remaining.append(j)

        indices = remaining

    return keep


# ══════════════════════════════════════════════════════════════════
# Image Preprocessing — Pure PIL
# ══════════════════════════════════════════════════════════════════

def _preprocess_image(
    image_bytes: bytes,
    target_size: tuple[int, int] = (640, 640),
) -> tuple[Any, tuple[int, int], float]:
    """
    Preprocess image for Grounding DINO.

    Returns:
      (numpy_array, original_size, scale_factor)

    numpy_array: float32 [1, 3, H, W] normalized with ImageNet mean/std.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        raise RuntimeError("Cần install: pip install pillow numpy --break-system-packages")

    # Load image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    orig_w, orig_h = img.size

    # Resize preserving aspect ratio with padding
    target_w, target_h = target_size
    scale = min(target_w / orig_w, target_h / orig_h)
    new_w = int(orig_w * scale)
    new_h = int(orig_h * scale)

    img_resized = img.resize((new_w, new_h), Image.LANCZOS)

    # Pad to target size (center padding)
    padded = Image.new("RGB", target_size, (128, 128, 128))
    pad_x = (target_w - new_w) // 2
    pad_y = (target_h - new_h) // 2
    padded.paste(img_resized, (pad_x, pad_y))

    # Convert to numpy
    arr = np.array(padded, dtype=np.float32) / 255.0

    # ImageNet normalization
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std

    # HWC → CHW → NCHW
    arr = arr.transpose(2, 0, 1)
    arr = np.expand_dims(arr, axis=0)

    return arr, (orig_w, orig_h), scale


def _postprocess_boxes(
    boxes: Any,  # numpy array [N, 4] normalized (cx, cy, w, h)
    scores: Any,  # numpy array [N]
    labels: list[str],
    orig_size: tuple[int, int],
    input_size: tuple[int, int],
    conf_threshold: float,
) -> list[Detection]:
    """
    Convert DINO output to Detection list.

    Handles coordinate mapping from model space back to original image.
    """
    try:
        import numpy as np
    except ImportError:
        return []

    if boxes is None or len(boxes) == 0:
        return []

    orig_w, orig_h = orig_size
    input_w, input_h = input_size
    scale = min(input_w / orig_w, input_h / orig_h)
    pad_x = (input_w - int(orig_w * scale)) // 2
    pad_y = (input_h - int(orig_h * scale)) // 2

    detections = []
    for i in range(len(boxes)):
        score = float(scores[i]) if i < len(scores) else 0.0
        if score < conf_threshold:
            continue

        # Model outputs normalized coords (0–1) relative to input_size
        box = boxes[i]
        if len(box) == 4:
            # Format: cx, cy, w, h (normalized)
            cx = box[0] * input_w
            cy = box[1] * input_h
            bw = box[2] * input_w
            bh = box[3] * input_h
            x1 = cx - bw / 2
            y1 = cy - bh / 2
            x2 = cx + bw / 2
            y2 = cy + bh / 2

            # Remove padding
            x1 = (x1 - pad_x) / scale
            y1 = (y1 - pad_y) / scale
            x2 = (x2 - pad_x) / scale
            y2 = (y2 - pad_y) / scale

            # Clamp to image bounds
            x1 = max(0, min(orig_w, x1))
            y1 = max(0, min(orig_h, y1))
            x2 = max(0, min(orig_w, x2))
            y2 = max(0, min(orig_h, y2))

            if x2 - x1 < 2 or y2 - y1 < 2:
                continue

            label = labels[i] if i < len(labels) else "unknown"

            detections.append(Detection(
                bbox=BBox(
                    x1=int(round(x1)),
                    y1=int(round(y1)),
                    x2=int(round(x2)),
                    y2=int(round(y2)),
                ),
                label=label,
                confidence=score,
                source=DetectionSource.DINO,
            ))

    return detections


# ══════════════════════════════════════════════════════════════════
# Text Query Encoding
# ══════════════════════════════════════════════════════════════════

def _encode_text_queries(queries: list[str]) -> str:
    """
    Format multiple queries for Grounding DINO.

    DINO accepts queries separated by " . " (period with spaces).
    Each query becomes a detection category.

    Example:
      ["Post button", "Upload icon"] → "Post button . Upload icon"
    """
    cleaned = []
    for q in queries:
        q = q.strip().rstrip(".")
        if q:
            cleaned.append(q)
    return " . ".join(cleaned)


def _build_text_inputs(query_text: str) -> dict[str, Any]:
    """
    Tokenize text query for DINO.

    Uses simple character-level tokenization as fallback.
    If transformers is available, uses BERT tokenizer for accuracy.
    """
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("numpy required")

    # Try using transformers tokenizer (best accuracy)
    try:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(
            "bert-base-uncased",
            local_files_only=True,
        )
        encoded = tokenizer(
            query_text,
            return_tensors="np",
            padding="max_length",
            truncation=True,
            max_length=256,
        )
        return {
            "input_ids": encoded["input_ids"].astype(np.int64),
            "attention_mask": encoded["attention_mask"].astype(np.int64),
            "token_type_ids": encoded.get("token_type_ids",
                                          np.zeros_like(encoded["input_ids"])).astype(np.int64),
        }
    except Exception:
        pass

    # Fallback: simple byte-level encoding
    # This is less accurate but works without transformers
    tokens = [101]  # [CLS]
    for ch in query_text.lower()[:254]:
        tokens.append(ord(ch) % 30000 + 1000)
    tokens.append(102)  # [SEP]

    # Pad to 256
    attention = [1] * len(tokens) + [0] * (256 - len(tokens))
    tokens = tokens + [0] * (256 - len(tokens))

    return {
        "input_ids": np.array([tokens[:256]], dtype=np.int64),
        "attention_mask": np.array([attention[:256]], dtype=np.int64),
        "token_type_ids": np.zeros((1, 256), dtype=np.int64),
    }


# ══════════════════════════════════════════════════════════════════
# DinoEngine
# ══════════════════════════════════════════════════════════════════

class DinoEngine:
    """
    Grounding DINO detection engine.

    Wraps ModelServer's DINO session with preprocessing/postprocessing.

    Usage:
        from vision.model_server import get_model_server
        server = await get_model_server()
        engine = DinoEngine(server)

        detections = await engine.detect(
            image_bytes=png_bytes,
            queries=["Post button", "Upload icon"],
            conf_threshold=0.3,
        )
    """

    def __init__(self, model_server: Any) -> None:
        """
        Args:
          model_server: ModelServer instance (vision/model_server.py).
        """
        self._server = model_server
        self._last_detection_time: float = 0

    @property
    def ready(self) -> bool:
        return self._server.dino_ready

    async def detect(
        self,
        image_bytes: bytes,
        queries: list[str],
        conf_threshold: float = DINO_CONF_THRESHOLD,
        input_size: tuple[int, int] = (640, 640),
        nms_threshold: float = 0.5,
    ) -> DetectionResult:
        """
        Run Grounding DINO detection on a screenshot.

        Args:
          image_bytes:    PNG/JPEG screenshot bytes.
          queries:        Text queries (what to find). E.g. ["Post button", "Upload icon"].
          conf_threshold: Minimum detection confidence (default 0.3).
          input_size:     Model input resolution (default 640×640).
          nms_threshold:  NMS IoU threshold (default 0.5).

        Returns:
          DetectionResult with filtered, NMS'd detections.
        """
        if not self.ready:
            _vlog("⚠️", "DINO not ready — returning empty result")
            return DetectionResult(query=" . ".join(queries))

        t0 = time.perf_counter()

        # Run inference in thread pool (blocking ONNX call)
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            self._detect_sync,
            image_bytes, queries, conf_threshold, input_size, nms_threshold,
        )

        latency = (time.perf_counter() - t0) * 1000
        result.latency_ms = latency
        self._last_detection_time = time.time()

        # Record metrics
        self._server.record_dino_call(latency)

        if result.count > 0:
            _vlog("🔍", f"DINO: {result.count} detections "
                        f"({latency:.0f}ms, K={result.k_used}, "
                        f"res={input_size[0]}×{input_size[1]})")

        return result

    def _detect_sync(
        self,
        image_bytes: bytes,
        queries: list[str],
        conf_threshold: float,
        input_size: tuple[int, int],
        nms_threshold: float,
    ) -> DetectionResult:
        """Synchronous detection — runs in thread pool."""
        try:
            import numpy as np
        except ImportError:
            return DetectionResult(query=" . ".join(queries))

        query_text = _encode_text_queries(queries)
        result = DetectionResult(query=query_text)

        try:
            # 1. Preprocess image
            img_tensor, orig_size, scale = _preprocess_image(image_bytes, input_size)

            # 2. Encode text
            text_inputs = _build_text_inputs(query_text)

            # 3. Build ONNX inputs
            session = self._server.dino_session
            input_names = [inp.name for inp in session.get_inputs()]
            output_names = [out.name for out in session.get_outputs()]

            # Map inputs based on what the model expects
            feed = {}
            for name in input_names:
                if "image" in name.lower() or "pixel" in name.lower():
                    feed[name] = img_tensor
                elif "input_id" in name.lower():
                    feed[name] = text_inputs["input_ids"]
                elif "attention" in name.lower():
                    feed[name] = text_inputs["attention_mask"]
                elif "token_type" in name.lower():
                    feed[name] = text_inputs["token_type_ids"]

            # If model expects specific named inputs, adapt
            if not feed:
                # Generic fallback: pass as positional
                feed = {input_names[0]: img_tensor}
                for i, key in enumerate(text_inputs):
                    if i + 1 < len(input_names):
                        feed[input_names[i + 1]] = text_inputs[key]

            # 4. Run inference
            outputs = session.run(output_names, feed)

            # 5. Parse outputs
            # DINO typically outputs: boxes [N, 4], scores [N], labels [N]
            raw_boxes = outputs[0] if len(outputs) > 0 else None
            raw_scores = outputs[1] if len(outputs) > 1 else None
            raw_label_ids = outputs[2] if len(outputs) > 2 else None

            if raw_boxes is None or raw_scores is None:
                return result

            # Flatten if needed (batch dim)
            if raw_boxes.ndim == 3:
                raw_boxes = raw_boxes[0]
            if raw_scores.ndim == 2:
                raw_scores = raw_scores[0]

            result.total_raw_detections = len(raw_boxes)

            # Map label IDs back to query strings
            labels = []
            if raw_label_ids is not None:
                if raw_label_ids.ndim == 2:
                    raw_label_ids = raw_label_ids[0]
                for lid in raw_label_ids:
                    idx = int(lid) if not isinstance(lid, (str,)) else 0
                    labels.append(queries[idx] if 0 <= idx < len(queries) else query_text)
            else:
                labels = [query_text] * len(raw_boxes)

            # 6. Filter + NMS
            all_detections = _postprocess_boxes(
                raw_boxes, raw_scores, labels,
                orig_size, input_size, conf_threshold,
            )

            if all_detections:
                boxes_for_nms = [(d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2)
                                 for d in all_detections]
                scores_for_nms = [d.confidence for d in all_detections]
                keep_indices = _nms(boxes_for_nms, scores_for_nms, nms_threshold)
                result.detections = [all_detections[i] for i in keep_indices]

            # Sort by confidence descending
            result.detections.sort(key=lambda d: d.confidence, reverse=True)
            result.input_resolution = input_size

        except Exception as exc:
            _vlog("❌", f"DINO inference error: {str(exc)[:120]}")

        return result

    async def detect_crop(
        self,
        image_bytes: bytes,
        queries: list[str],
        crop_bbox: BBox,
        zoom_factor: float = 2.0,
        conf_threshold: float = DINO_CONF_THRESHOLD,
    ) -> DetectionResult:
        """
        Detect on a cropped + zoomed region (for Active Vision Loop).

        Crops around crop_bbox, upscales, runs DINO, then maps coords
        back to original image space.

        Args:
          image_bytes:    Full screenshot PNG.
          queries:        What to find.
          crop_bbox:      Region to crop around.
          zoom_factor:    Upscale factor (2.0 = 2× zoom).
          conf_threshold: Min confidence.

        Returns:
          DetectionResult with coordinates in ORIGINAL image space.
        """
        try:
            from PIL import Image
        except ImportError:
            return DetectionResult(query=" . ".join(queries))

        # Crop region with padding
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        iw, ih = img.size

        pad = int(max(crop_bbox.width, crop_bbox.height) * 0.3)
        cx1 = max(0, crop_bbox.x1 - pad)
        cy1 = max(0, crop_bbox.y1 - pad)
        cx2 = min(iw, crop_bbox.x2 + pad)
        cy2 = min(ih, crop_bbox.y2 + pad)

        crop = img.crop((cx1, cy1, cx2, cy2))

        # Upscale
        cw, ch = crop.size
        new_w = int(cw * zoom_factor)
        new_h = int(ch * zoom_factor)
        crop_upscaled = crop.resize((new_w, new_h), Image.LANCZOS)

        # Convert to bytes
        buf = io.BytesIO()
        crop_upscaled.save(buf, format="PNG")
        crop_bytes = buf.getvalue()

        # Detect on cropped image
        result = await self.detect(
            crop_bytes, queries, conf_threshold,
            input_size=(min(800, new_w), min(800, new_h)),
        )

        # Map coordinates back to original image space
        remapped = []
        for det in result.detections:
            # Scale back to crop space, then add crop offset
            rx1 = det.bbox.x1 / zoom_factor + cx1
            ry1 = det.bbox.y1 / zoom_factor + cy1
            rx2 = det.bbox.x2 / zoom_factor + cx1
            ry2 = det.bbox.y2 / zoom_factor + cy1

            remapped.append(Detection(
                bbox=BBox(
                    x1=int(round(max(0, rx1))),
                    y1=int(round(max(0, ry1))),
                    x2=int(round(min(iw, rx2))),
                    y2=int(round(min(ih, ry2))),
                ),
                label=det.label,
                confidence=det.confidence,
                source=DetectionSource.DINO,
            ))

        result.detections = remapped
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "total_calls": self._server._total_dino_calls,
            "avg_latency_ms": round(self._server._dino_info.avg_latency_ms, 1),
            "last_detection": self._last_detection_time,
        }
