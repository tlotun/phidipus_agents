# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/ui_parser.py — Phidipus v1.0
UI element parser — extracts interactive elements from screen layout.

Architecture contract (RETAIN):
  "No security concerns in parsing logic.  Retained as-is."

UIParser takes a ScreenLayout (from ScreenParser) and VLM detections
(from VLMInterface) and produces a unified list of UIElement objects that
the agent loop can reason about and dispatch actions against.

Each UIElement has:
  - A bounding box (x, y, width, height)
  - An element type (button, text_field, link, icon, unknown)
  - A text label (from accessibility API or VLM caption)
  - A confidence score (from VLM or 1.0 for a11y elements)

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  (Pure data transformation — no security-sensitive operations.)

Used by:
  vision/perception_pipeline.py  — parse_elements() for element list

Dependencies:
  vision/screen_parser.py  — ScreenLayout, ScreenRegion
  utils/logger.py          — get_logger()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from utils.logger import get_logger
from vision.screen_parser import ScreenRegion

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Element type constants
# ---------------------------------------------------------------------------

ELEM_BUTTON:     str = "button"
ELEM_TEXT_FIELD: str = "text_field"
ELEM_LINK:       str = "link"
ELEM_ICON:       str = "icon"
ELEM_IMAGE:      str = "image"
ELEM_TEXT:       str = "text"
ELEM_UNKNOWN:    str = "unknown"

_VALID_ELEM_TYPES = frozenset({
    ELEM_BUTTON, ELEM_TEXT_FIELD, ELEM_LINK,
    ELEM_ICON, ELEM_IMAGE, ELEM_TEXT, ELEM_UNKNOWN,
})


# ---------------------------------------------------------------------------
# UIElement
# ---------------------------------------------------------------------------

@dataclass
class UIElement:
    """
    A single interactive or informational UI element detected on screen.

    Attributes:
        x, y:         Top-left corner in screen coordinates.
        width, height: Bounding box dimensions.
        elem_type:    One of the ELEM_* constants.
        label:        Text label (from a11y tree or VLM caption).
        confidence:   Detection confidence (0.0–1.0).
        source:       Where this element was detected ("a11y" or "vlm").
        element_id:   Optional accessibility element ID.
        metadata:     Additional element-specific data.
    """

    x:          int
    y:          int
    width:      int
    height:     int
    elem_type:  str   = ELEM_UNKNOWN
    label:      str   = ""
    confidence: float = 1.0
    source:     str   = "unknown"
    element_id: str   = ""
    metadata:   dict[str, Any] = field(default_factory=dict)

    @property
    def center_x(self) -> int:
        return self.x + self.width // 2

    @property
    def center_y(self) -> int:
        return self.y + self.height // 2

    def to_click_payload(self) -> dict[str, Any]:
        """
        Return an IPC mouse_click payload targeting this element's center.

        The caller must pass this through ConfidenceGate before dispatch.
        """
        return {
            "x":           self.center_x,
            "y":           self.center_y,
            "button":      "left",
            "confidence":  self.confidence,
            "target_label": self.label,
        }

    def overlaps(self, other: "UIElement") -> bool:
        """Return True if this element's bounding box overlaps *other*'s."""
        return not (
            self.x + self.width  <= other.x or
            other.x + other.width  <= self.x or
            self.y + self.height <= other.y or
            other.y + other.height <= self.y
        )


# ---------------------------------------------------------------------------
# UIParser
# ---------------------------------------------------------------------------

class UIParser:
    """
    Merge accessibility API elements and VLM detections into UIElement list.

    Priority:
      1. Accessibility API elements (confidence=1.0, source="a11y")
         are always preferred — they are exact by definition.
      2. VLM detections (source="vlm") fill in elements not found via a11y.
         Low-confidence VLM detections are included but flagged.

    IoU-based deduplication removes overlapping elements from different
    sources (configurable threshold).

    Usage::

        parser = UIParser(iou_threshold=0.5)
        elements = parser.parse(
            a11y_elements=[...],     # from accessibility API
            vlm_detections=[...],    # from VLMInterface
        )

    Args:
        iou_threshold: Intersection-over-union threshold for deduplication.
    """

    def __init__(self, iou_threshold: float = 0.5) -> None:
        self._iou_threshold = iou_threshold
        _log.debug(
            "UIParser initialised",
            extra={"iou_threshold": iou_threshold},
        )

    def parse(
        self,
        a11y_elements:  list[UIElement] | None = None,
        vlm_detections: list[UIElement] | None = None,
    ) -> list[UIElement]:
        """
        Merge and deduplicate UI elements from a11y and VLM sources.

        Args:
            a11y_elements:  Elements from the accessibility API (source="a11y").
            vlm_detections: VLM-detected elements (source="vlm").

        Returns:
            Deduplicated, sorted list of UIElement objects.
        """
        a11y = list(a11y_elements or [])
        vlm  = list(vlm_detections or [])

        # Start with all a11y elements (high confidence, exact)
        result: list[UIElement] = list(a11y)

        # Add VLM elements that don't overlap significantly with a11y
        for vlm_elem in vlm:
            if not self._overlaps_any(vlm_elem, result):
                result.append(vlm_elem)

        # Sort: top-to-bottom, left-to-right
        result.sort(key=lambda e: (e.y, e.x))

        _log.debug(
            "UIParser: elements merged",
            extra={
                "a11y_count": len(a11y),
                "vlm_count":  len(vlm),
                "total":      len(result),
            },
        )
        return result

    def from_vlm_detections(
        self,
        detections: list[dict[str, Any]],
    ) -> list[UIElement]:
        """
        Convert raw VLM detection dicts into UIElement objects.

        Expected dict format (from VLMInterface)::

            {
              "x": int, "y": int, "width": int, "height": int,
              "label": str, "confidence": float,
              "elem_type": str (optional)
            }
        """
        elements: list[UIElement] = []
        for det in detections:
            if not isinstance(det, dict):
                continue
            try:
                elem = UIElement(
                    x=int(det.get("x", 0)),
                    y=int(det.get("y", 0)),
                    width=int(det.get("width", 10)),
                    height=int(det.get("height", 10)),
                    elem_type=det.get("elem_type", ELEM_UNKNOWN),
                    label=str(det.get("label", "")),
                    confidence=float(det.get("confidence", 0.0)),
                    source="vlm",
                )
                elements.append(elem)
            except (TypeError, ValueError) as exc:
                _log.warning(
                    "UIParser: skipping malformed VLM detection",
                    extra={"error": str(exc)},
                )
        return elements

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _overlaps_any(self, elem: UIElement, existing: list[UIElement]) -> bool:
        """Return True if *elem* has IoU > threshold with any existing element."""
        for ex in existing:
            if self._iou(elem, ex) > self._iou_threshold:
                return True
        return False

    @staticmethod
    def _iou(a: UIElement, b: UIElement) -> float:
        """Compute intersection-over-union of two bounding boxes."""
        ix = max(0, min(a.x + a.width,  b.x + b.width)  - max(a.x, b.x))
        iy = max(0, min(a.y + a.height, b.y + b.height) - max(a.y, b.y))
        inter = ix * iy
        if inter == 0:
            return 0.0
        union = a.width * a.height + b.width * b.height - inter
        return inter / union if union > 0 else 0.0
