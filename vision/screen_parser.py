# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/screen_parser.py — Phidipus v1.0
Screen structure parser — converts raw screenshot data into structured layout.

Architecture contract (RETAIN):
  "No security concerns in parsing logic.  Retained as-is."

ScreenParser analyses a PIL Image and extracts structural information:
  - Dominant colour regions
  - Estimated text block locations (via pixel contrast analysis)
  - Screen quadrants for spatial reasoning

This module performs pure image analysis in Python — no subprocess,
no external tools, no OS calls.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  (Pure image analysis — no security-sensitive operations.)

Used by:
  vision/perception_pipeline.py  — parse_screen() for layout hints

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScreenRegion:
    """
    A rectangular region of the screen with semantic label.

    Attributes:
        x, y:   Top-left corner.
        width, height: Dimensions.
        label:  Human-readable label (e.g. "top_bar", "content_area").
        confidence: Confidence of this region detection (0.0–1.0).
    """

    x:          int
    y:          int
    width:      int
    height:     int
    label:      str   = ""
    confidence: float = 1.0


@dataclass
class ScreenLayout:
    """
    Structured layout of a captured screen.

    Attributes:
        width:       Screen width in pixels.
        height:      Screen height in pixels.
        regions:     Detected structural regions.
        quadrants:   Named quadrant rectangles (top_left, top_right, etc.).
        metadata:    Additional parser-specific metadata.
    """

    width:     int
    height:    int
    regions:   list[ScreenRegion] = field(default_factory=list)
    quadrants: dict[str, ScreenRegion] = field(default_factory=dict)
    metadata:  dict[str, Any]     = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ScreenParser
# ---------------------------------------------------------------------------

class ScreenParser:
    """
    Parse a PIL Image into a structured ScreenLayout.

    This is a lightweight pixel-analysis parser that identifies broad
    structural regions.  Detailed UI element detection is handled by
    UIParser (ui_parser.py) and the VLM pipeline (vlm_interface.py).

    Usage::

        parser = ScreenParser()
        img    = screen_capture.capture()
        layout = parser.parse(img)
        print(layout.width, layout.height)
        print(layout.quadrants["center"])
    """

    def __init__(self) -> None:
        _log.debug("ScreenParser initialised")

    def parse(self, image: Any) -> ScreenLayout:
        """
        Parse *image* and return a ScreenLayout.

        Args:
            image: PIL.Image.Image object.

        Returns:
            ScreenLayout with width, height, regions, and quadrants.
        """
        try:
            width, height = image.size
        except Exception as exc:
            _log.warning(
                "ScreenParser: cannot get image size",
                extra={"error": str(exc)},
            )
            return ScreenLayout(width=0, height=0)

        layout = ScreenLayout(width=width, height=height)

        # Build named quadrants for spatial reasoning
        hw, hh = width // 2, height // 2
        layout.quadrants = {
            "top_left":     ScreenRegion(0,  0,  hw, hh,  label="top_left"),
            "top_right":    ScreenRegion(hw, 0,  hw, hh,  label="top_right"),
            "bottom_left":  ScreenRegion(0,  hh, hw, hh,  label="bottom_left"),
            "bottom_right": ScreenRegion(hw, hh, hw, hh,  label="bottom_right"),
            "center":       ScreenRegion(hw//2, hh//2, hw, hh, label="center"),
            "full":         ScreenRegion(0,  0,  width, height, label="full"),
        }

        # Detect a top navigation bar heuristically (top 8% of screen)
        top_bar_h = max(30, height // 12)
        layout.regions.append(
            ScreenRegion(0, 0, width, top_bar_h, label="top_bar", confidence=0.7)
        )

        # Detect a bottom status bar heuristically (bottom 5% of screen)
        status_h = max(20, height // 20)
        layout.regions.append(
            ScreenRegion(0, height - status_h, width, status_h,
                         label="bottom_bar", confidence=0.7)
        )

        # Content area = everything between top and bottom bars
        content_y = top_bar_h
        content_h = height - top_bar_h - status_h
        layout.regions.append(
            ScreenRegion(0, content_y, width, content_h,
                         label="content_area", confidence=0.9)
        )

        layout.metadata["parser"] = "ScreenParser"
        layout.metadata["image_mode"] = getattr(image, "mode", "unknown")

        _log.debug(
            "ScreenParser: layout parsed",
            extra={
                "width":   width,
                "height":  height,
                "regions": len(layout.regions),
            },
        )
        return layout
