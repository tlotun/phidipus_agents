# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/confidence_gate.py — Phidipus v1.0
Confidence gate for VLM detections — enforces the F-5 architectural fix.

Architecture contract (R-21 / R-22 / R-23 / F-5):
  "VLM output (bounding boxes) directly controls OS actions with no
   confidence gate, confirmation, or anomaly check — this is the F-5
   flaw."

  "R-21: VLM detection output MUST pass through vision/confidence_gate.py
   before any IPC action is emitted.  Detections with confidence < 0.8
   MUST NOT trigger automated execution."

  "R-22: Actions on UI elements outside the currently focused application
   window MUST require explicit user confirmation before dispatch."

  "R-23: SuccessSkillCompiler MUST NOT compile episodes that contain any
   VLM-identified actions with confidence < 0.8 without human review."

ConfidenceGate is the mandatory filter between the VLM pipeline and the
IPC dispatch layer.  It is the sole enforcement point for R-21:

  VLMInterface → PerceptionPipeline → ConfidenceGate → IPCClient

NO VLM-sourced payload may reach IPCClient without passing through this
gate.  Accessibility API elements (confidence=1.0 by definition) are
not required to pass through this gate, but may do so safely.

Gate behaviour
--------------
  confidence >= threshold (0.8):
    If require_confirm=False (element is inside focused window):
      → payload returned unchanged — safe to dispatch via IPC
    If require_confirm=True (element is outside focused window, R-22):
      → payload returned with require_confirm=True preserved
        (Automation Daemon will prompt user before executing)

  confidence < threshold:
    → ConfidenceBelowThreshold raised — caller MUST NOT dispatch this
      action.  The exception carries enough context for logging and for
      SuccessSkillCompiler's review filter (R-23).

Immutability
------------
CONFIDENCE_THRESHOLD = 0.8 is an architectural constant.  ConfidenceGate
refuses to be constructed with a threshold below this value (R-21).
An operator may raise the threshold for stricter environments, but never
lower it below 0.8.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-21  Detections with confidence < CONFIDENCE_THRESHOLD raise
        ConfidenceBelowThreshold.  Caller MUST NOT dispatch them.
  R-22  Payloads with require_confirm=True are returned with that flag
        preserved.  Automation Daemon handles the user-confirmation flow.
  R-23  ConfidenceBelowThreshold carries the confidence value so
        SuccessSkillCompiler can filter episodes containing low-confidence
        VLM actions.

Used by:
  vision/perception_pipeline.py  — check() on every VLM-sourced element
  skills/success_skill_compiler.py — inspect confidence of episode actions

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# Architectural constant — IMMUTABLE (R-21)
# ---------------------------------------------------------------------------

#: Minimum VLM confidence required for automated IPC dispatch (R-21).
#: This value is an architectural invariant — it may not be lowered.
CONFIDENCE_THRESHOLD: float = 0.8


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfidenceBelowThreshold(Exception):
    """
    Raised when a VLM detection confidence is below CONFIDENCE_THRESHOLD.

    Callers MUST NOT dispatch the associated IPC action.  The exception
    carries enough context for logging and for SuccessSkillCompiler's
    episode-review filter (R-23).

    Attributes:
        action:     The IPC action name that was blocked (e.g. "mouse_click").
        confidence: The actual confidence value of the detection (0.0–1.0).
        threshold:  The configured threshold that was not met.
        label:      Human-readable label of the UI element (if available).
        reason:     Short machine-readable reason code.
    """

    def __init__(
        self,
        action:     str,
        confidence: float,
        *,
        threshold: float = CONFIDENCE_THRESHOLD,
        label:     str   = "",
        reason:    str   = "CONFIDENCE_BELOW_THRESHOLD",
    ) -> None:
        super().__init__(
            f"VLM confidence {confidence:.3f} < threshold {threshold:.3f} "
            f"for action {action!r}"
            + (f" (label={label!r})" if label else "")
        )
        self.action     = action
        self.confidence = confidence
        self.threshold  = threshold
        self.label      = label
        self.reason     = reason

    def __str__(self) -> str:
        base  = super().__str__()
        return f"[{self.reason}] {base}"


class ConfidenceGateConfigError(ValueError):
    """
    Raised when ConfidenceGate is constructed with an invalid threshold.

    Attributes:
        requested_threshold: The threshold value that was rejected.
        reason: Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        requested_threshold: float = 0.0,
        reason: str = "GATE_CONFIG_ERROR",
    ) -> None:
        super().__init__(message)
        self.requested_threshold = requested_threshold
        self.reason              = reason

    def __str__(self) -> str:
        base = super().__str__()
        return (
            f"[{self.reason}] "
            f"requested_threshold={self.requested_threshold:.3f} {base}"
        )


# ---------------------------------------------------------------------------
# ConfidenceGate
# ---------------------------------------------------------------------------

class ConfidenceGate:
    """
    Mandatory confidence filter between the VLM pipeline and IPC dispatch.

    ConfidenceGate.check() MUST be called on every VLM-sourced action
    payload before it is sent to IPCClient.  If confidence < threshold,
    ConfidenceBelowThreshold is raised and the action is blocked (R-21).

    The threshold may not be set below CONFIDENCE_THRESHOLD (0.8) — this
    would be an architectural regression to the v9.10 F-5 flaw (R-21).

    Usage::

        gate = ConfidenceGate()   # uses default 0.8 threshold

        # Inside PerceptionPipeline — called for every VLM element:
        try:
            safe_payload = gate.check("mouse_click", elem.to_click_payload())
            # safe_payload is cleared for IPC dispatch
        except ConfidenceBelowThreshold as exc:
            # Log and skip — never dispatch
            log.debug("Blocked: %s", exc)

        # Raising threshold for stricter environments:
        gate = ConfidenceGate(threshold=0.9)   # OK — higher than 0.8
        gate = ConfidenceGate(threshold=0.7)   # RAISES ConfidenceGateConfigError

    Args:
        threshold: Minimum confidence for automated dispatch.
                   Must be >= CONFIDENCE_THRESHOLD (0.8).  Default: 0.8.
    """

    def __init__(self, threshold: float = CONFIDENCE_THRESHOLD) -> None:
        # R-21: threshold must not be below the architectural constant
        if threshold < CONFIDENCE_THRESHOLD:
            raise ConfidenceGateConfigError(
                f"ConfidenceGate threshold may not be below the architectural "
                f"minimum of {CONFIDENCE_THRESHOLD} (R-21). "
                f"Requested: {threshold:.3f}",
                requested_threshold=threshold,
                reason="THRESHOLD_TOO_LOW",
            )
        self._threshold = threshold

        _log.info(
            "ConfidenceGate initialised",
            extra={
                "threshold":  self._threshold,
                "minimum":    CONFIDENCE_THRESHOLD,
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def threshold(self) -> float:
        """The configured confidence threshold (read-only)."""
        return self._threshold

    def check(
        self,
        action:  str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Gate a VLM-sourced IPC payload by its confidence score (R-21).

        If the payload has no ``confidence`` field, full trust is assumed
        (confidence=1.0).  This allows non-VLM payloads (e.g. from the
        accessibility API where confidence is always 1.0) to pass through
        without modification.

        If ``require_confirm=True`` is set in the payload (R-22 — element
        is outside the focused window), the flag is preserved in the
        returned dict.  The Automation Daemon will prompt the user before
        executing the action.

        Args:
            action:  IPC action name (e.g. "mouse_click", "element_click").
            payload: IPC payload dict containing at minimum a ``confidence``
                     field (float 0.0–1.0) and optionally ``require_confirm``
                     (bool) and ``target_label`` (str).

        Returns:
            A copy of *payload* cleared for IPC dispatch.  If
            require_confirm=True, that flag is preserved verbatim.

        Raises:
            ConfidenceBelowThreshold: if payload["confidence"] < threshold.
                                       Caller MUST NOT dispatch the action.
        """
        confidence   = float(payload.get("confidence", 1.0))
        label        = str(payload.get("target_label", ""))
        require_conf = bool(payload.get("require_confirm", False))

        # R-21: enforce confidence threshold
        if confidence < self._threshold:
            exc = ConfidenceBelowThreshold(
                action,
                confidence,
                threshold=self._threshold,
                label=label,
            )
            _log.debug(
                "ConfidenceGate: BLOCKED — confidence below threshold (R-21)",
                extra={
                    "action":     action,
                    "confidence": confidence,
                    "threshold":  self._threshold,
                    "label":      label,
                },
            )
            raise exc

        # R-22: out-of-focus-window elements require confirmation
        if require_conf:
            _log.debug(
                "ConfidenceGate: PASSED with require_confirm=True (R-22)",
                extra={
                    "action":     action,
                    "confidence": confidence,
                    "label":      label,
                },
            )
        else:
            _log.debug(
                "ConfidenceGate: PASSED",
                extra={
                    "action":     action,
                    "confidence": confidence,
                    "label":      label,
                },
            )

        # Return a copy with require_confirm preserved (R-22)
        result = dict(payload)
        result["require_confirm"] = require_conf
        return result

    def check_episode_for_compilation(
        self,
        episode: dict[str, Any],
    ) -> bool:
        """
        Check whether an episode is safe to compile into a skill (R-23).

        SuccessSkillCompiler MUST call this before compiling any episode.
        Returns False if any VLM-sourced action in the episode has
        confidence < threshold (requiring human review first).

        Args:
            episode: Episode dict from EpisodicMemory containing a
                     ``steps`` list.  Each step may have an ``action``
                     dict with ``confidence`` and ``source`` fields.

        Returns:
            True  — episode may be compiled safely.
            False — episode contains low-confidence VLM actions; human
                    review required before compilation (R-23).
        """
        steps = episode.get("steps", [])
        for step in steps:
            action_dict = step.get("action", {})
            if not isinstance(action_dict, dict):
                continue
            source     = action_dict.get("source", "unknown")
            confidence = float(action_dict.get("confidence", 1.0))

            # Only VLM-sourced actions are subject to confidence gating
            if source == "vlm" and confidence < self._threshold:
                _log.warning(
                    "R-23: episode contains low-confidence VLM action — "
                    "compilation blocked pending human review",
                    extra={
                        "episode_id": episode.get("id", "?"),
                        "confidence": confidence,
                        "threshold":  self._threshold,
                        "label":      action_dict.get("target_label", ""),
                    },
                )
                return False

        return True
