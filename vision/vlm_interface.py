# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/vlm_interface.py — Phidipus v1.0
VLM (Vision Language Model) interface with confidence score enforcement.

Architecture contract (R-21 / F-5):
  "Add confidence score to all returned detections.  Expose confidence
   field to confidence_gate.py."

  "VLM output (bounding boxes) directly controls OS actions with no
   confidence gate — this is the F-5 flaw.  v9.11 fix: every detection
   returned by VLMInterface carries a numeric confidence field that
   ConfidenceGate checks before any IPC dispatch."

VLMInterface calls the Ollama VLM API (e.g. qwen3-vl:8b) with a
screenshot encoded as base64 PNG and returns a list of detection dicts.
Every detection includes a ``confidence`` field (0.0–1.0) derived from
the VLM response.  If the VLM does not provide a confidence score, a
conservative default of 0.5 is used (which will fail ConfidenceGate's
0.8 threshold, requiring human review).

The output of this module is NEVER routed directly to OSController.
It always passes through ConfidenceGate first (R-21).

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-21  Every returned detection contains a numeric ``confidence`` field.
        Detections without a confidence score receive 0.5 (conservative
        default that will fail the 0.8 gate).

Used by:
  vision/perception_pipeline.py — detect_elements() output routed to
                                   ConfidenceGate before IPC dispatch

Dependencies:
  core/llm_client.py      — LLMClient for VLM API calls
  utils/json_utils.py     — loads(), JsonDecodeError
  utils/logger.py         — get_logger()
  config/config_loader.py — PhidipusConfig (cfg.llm.vlm_model,
                             cfg.vision.vlm_timeout_seconds)
"""

from __future__ import annotations

import asyncio
import base64
import re
import urllib.error
import urllib.request
from typing import Any

from config.config_loader import PhidipusConfig
from utils.json_utils import JsonDecodeError, dumps, loads
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Conservative default confidence when VLM does not report one.
#: 0.5 is below ConfidenceGate's 0.8 threshold, ensuring human review.
_DEFAULT_CONFIDENCE: float = 0.5

#: VLM prompt for UI element detection.
_DETECTION_PROMPT = (
    "Analyse this screenshot and identify all interactive UI elements "
    "(buttons, text fields, links, icons, menus).  "
    "For each element return a JSON object with fields: "
    "x (int), y (int), width (int), height (int), "
    "label (str), elem_type (str: button|text_field|link|icon|image|text|unknown), "
    "confidence (float 0.0-1.0).  "
    "Return ONLY a JSON array of these objects and nothing else."
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class VLMError(RuntimeError):
    """
    Raised when the VLM API call fails.

    Attributes:
        model:  VLM model name.
        reason: Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        model:  str = "",
        reason: str = "VLM_ERROR",
    ) -> None:
        super().__init__(message)
        self.model  = model
        self.reason = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.model:
            parts.append(f"model={self.model!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# VLMInterface
# ---------------------------------------------------------------------------

class VLMInterface:
    """
    Calls the Ollama VLM API to detect UI elements in screenshots.

    Every returned detection dict contains a ``confidence`` field so
    ConfidenceGate can enforce the 0.8 threshold (R-21).

    Usage::

        vlm = VLMInterface(cfg)
        detections = await vlm.detect_elements(image_bytes)
        # detections: [{"x":…,"y":…,"width":…,"height":…,
        #               "label":…,"elem_type":…,"confidence":float}, …]

        # Always pass through ConfidenceGate before dispatch:
        for det in detections:
            safe_payload = confidence_gate.check("mouse_click", det)
            await ipc_client.send_action("mouse_click", safe_payload)

    Args:
        cfg: Validated PhidipusConfig.
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._base_url  = cfg.llm.base_url.rstrip("/")
        self._model     = cfg.llm.vlm_model
        self._timeout   = cfg.vision.vlm_timeout_seconds

        _log.info(
            "VLMInterface initialised",
            extra={"model": self._model, "timeout": self._timeout},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def detect_elements(
        self,
        image_bytes: bytes,
        *,
        image_format: str = "png",
    ) -> list[dict[str, Any]]:
        """
        Detect UI elements in *image_bytes* using the VLM.

        Every returned dict contains a ``confidence`` field (R-21).
        If the VLM response cannot be parsed, returns an empty list
        rather than raising, so the pipeline can fall back to a11y API.

        Args:
            image_bytes:  Raw image bytes (PNG recommended).
            image_format: MIME sub-type for base64 encoding ("png" / "jpeg").

        Returns:
            List of detection dicts, each with x, y, width, height,
            label, elem_type, and confidence fields.
        """
        b64_image = base64.b64encode(image_bytes).decode("ascii")

        payload = dumps({
            "model":  self._model,
            "stream": False,
            "messages": [
                {
                    "role":    "user",
                    "content": _DETECTION_PROMPT,
                    "images":  [b64_image],
                }
            ],
        }).encode("utf-8")

        url = f"{self._base_url}/api/chat"

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._http_post, url, payload),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            _log.warning(
                "VLMInterface: VLM API timed out",
                extra={"model": self._model, "timeout": self._timeout},
            )
            # Raise so PerceptionPipeline circuit breaker can count timeouts.
            raise VLMError(
                f"VLM timed out after {self._timeout}s",
                model=self._model,
                reason="TIMEOUT",
            )
        except VLMError as exc:
            _log.error(
                "VLMInterface: VLM API call failed",
                extra={"model": self._model, "error": str(exc)},
            )
            return []

        detections = self._parse_response(raw)
        # R-21: ensure every detection has a confidence field
        detections = self._ensure_confidence(detections)

        _log.debug(
            "VLMInterface: detections returned",
            extra={"count": len(detections), "model": self._model},
        )
        return detections

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str) -> list[dict[str, Any]]:
        """
        Parse the Ollama API response and extract the detection array.

        Returns empty list on any parse failure.
        """
        try:
            resp = loads(raw)
        except JsonDecodeError:
            _log.warning("VLMInterface: API response is not valid JSON")
            return []

        if not isinstance(resp, dict):
            return []

        content = ""
        try:
            content = resp["message"]["content"]
        except (KeyError, TypeError):
            return []

        # Extract JSON array from content (VLM may wrap in markdown)
        json_match = re.search(r"\[.*\]", content, re.DOTALL)
        if not json_match:
            _log.warning(
                "VLMInterface: no JSON array found in VLM response",
                extra={"content_snippet": content[:200]},
            )
            return []

        try:
            detections = loads(json_match.group(0))
        except JsonDecodeError:
            _log.warning("VLMInterface: VLM JSON array is not parseable")
            return []

        if not isinstance(detections, list):
            return []

        # Filter to valid dicts
        return [d for d in detections if isinstance(d, dict)]

    @staticmethod
    def _ensure_confidence(
        detections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Ensure every detection dict has a numeric ``confidence`` field (R-21).

        Missing or non-numeric confidence values are replaced with
        _DEFAULT_CONFIDENCE (0.5) which will fail ConfidenceGate's threshold.
        """
        result: list[dict[str, Any]] = []
        for det in detections:
            d = dict(det)
            raw_conf = d.get("confidence")
            try:
                d["confidence"] = float(raw_conf) if raw_conf is not None else _DEFAULT_CONFIDENCE
            except (TypeError, ValueError):
                d["confidence"] = _DEFAULT_CONFIDENCE
            result.append(d)
        return result

    @staticmethod
    def _http_post(url: str, payload: bytes) -> str:
        """Synchronous HTTP POST — called via asyncio.to_thread."""
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise VLMError(
                f"VLM HTTP {exc.code}: {exc.reason}",
                reason="HTTP_ERROR",
            ) from exc
        except urllib.error.URLError as exc:
            raise VLMError(
                f"VLM connection error: {exc.reason}",
                reason="CONNECTION_ERROR",
            ) from exc
