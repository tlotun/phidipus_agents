# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/success_skill_compiler.py — Phidipus v1.0
Compiles successful task episodes into reusable skills.

Architecture contract (R-08 / R-23):
  "Compiled skills MUST pass all three skill_validator gates before
   activation (M-5).  Remove duplicate in learning/."

  "R-23: SuccessSkillCompiler MUST NOT compile episodes that contain
   any VLM-identified actions with confidence < 0.8 without human review."

SuccessSkillCompiler analyses a successful task episode and, if it is
safe to compile (R-23), generates Python skill code from the episode's
action sequence.  The generated code is routed through SkillGenerator
(which uses SkillRouter → 3-gate validator) before being registered.

Compile safety check (R-23)
----------------------------
Before any compilation, check_episode_for_compilation() from ConfidenceGate is
called on the episode.  Episodes containing VLM actions with confidence
< 0.8 are blocked until human review clears them.

Process: orchestrator (L1)

Security invariants enforced here:
  R-08  Generated skill code passes all 3 gates via SkillGenerator.
  R-23  Episodes with low-confidence VLM actions are not compiled
        without human review.

Used by:
  core/agent_loop.py          — compile_episode() after task success
  evolution/skill_evolver.py  — compile_episode() for evolved skills

Dependencies:
  skills/skill_generator.py  — SkillGenerator.generate()
  vision/confidence_gate.py  — ConfidenceGate.check_episode_for_compilation()
  utils/logger.py            — get_logger()
  config/config_loader.py    — PhidipusConfig
"""

from __future__ import annotations

from typing import Any

from config.config_loader import PhidipusConfig
from skill_validator.schema_gate import ValidationResult
from skills.skill_generator import SkillGenerator, SkillGenerationError
from vision.confidence_gate import ConfidenceGate
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CompileBlockedError(RuntimeError):
    """
    Raised when compilation is blocked due to R-23 (low-confidence VLM
    actions in the episode that require human review).

    Attributes:
        episode_id: ID of the blocked episode.
        reason:     Always "LOW_CONFIDENCE_VLM_ACTIONS".
    """

    def __init__(
        self,
        message: str,
        *,
        episode_id: str = "",
        reason:     str = "LOW_CONFIDENCE_VLM_ACTIONS",
    ) -> None:
        super().__init__(message)
        self.episode_id = episode_id
        self.reason     = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.episode_id:
            parts.append(f"episode={self.episode_id!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# SuccessSkillCompiler
# ---------------------------------------------------------------------------

class SuccessSkillCompiler:
    """
    Compiles successful task episodes into validated, signed skills.

    Usage::

        compiler = SuccessSkillCompiler(cfg, skill_generator, conf_gate)

        # After a successful task:
        result = await compiler.compile_episode(episode)
        if result:
            print("New skill:", result.skill_name)

    Args:
        cfg:             Validated PhidipusConfig.
        skill_generator: SkillGenerator instance.
        conf_gate:       ConfidenceGate instance (for R-23 check).
    """

    def __init__(
        self,
        cfg:             PhidipusConfig,
        skill_generator: SkillGenerator,
        conf_gate:       ConfidenceGate,
    ) -> None:
        self._generator = skill_generator
        self._gate      = conf_gate
        _log.info("SuccessSkillCompiler initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compile_episode(
        self,
        episode: dict[str, Any],
        *,
        skill_name: str = "",
    ) -> ValidationResult | None:
        """
        Attempt to compile *episode* into a reusable skill.

        Returns None if:
          - Episode has no action history.
          - Episode has fewer than 2 steps (too simple to generalise).

        Args:
            episode:    Episode dict (as stored by EpisodicMemory).
            skill_name: Override skill name (default: derived from goal).

        Returns:
            ValidationResult if compilation succeeded, None if skipped.

        Raises:
            CompileBlockedError: if episode has low-confidence VLM actions
                                 without human review (R-23).
            SkillGenerationError: if 3-gate validation fails.
        """
        episode_id = episode.get("id", "<unknown>")

        # R-23: block compilation if episode has low-confidence VLM actions
        if not self._gate.check_episode_for_compilation(episode):
            _log.warning(
                "R-23: SuccessSkillCompiler blocked episode — "
                "low-confidence VLM actions require human review",
                extra={"episode_id": episode_id},
            )
            raise CompileBlockedError(
                f"Episode {episode_id!r} contains VLM actions with confidence "
                f"< {self._gate.threshold:.1f} — human review required (R-23).",
                episode_id=episode_id,
            )

        steps = episode.get("steps", [])
        if len(steps) < 2:
            _log.debug(
                "SuccessSkillCompiler: episode too short to compile",
                extra={"episode_id": episode_id, "steps": len(steps)},
            )
            return None

        goal = episode.get("goal", episode_id)
        name = skill_name or _goal_to_skill_name(goal, episode_id)
        description = _build_description(goal, steps)

        _log.info(
            "SuccessSkillCompiler: compiling episode",
            extra={"episode_id": episode_id, "skill_name": name},
        )

        validation = await self._generator.generate(
            skill_name=name,
            description=description,
        )

        _log.info(
            "SuccessSkillCompiler: episode compiled successfully",
            extra={"episode_id": episode_id, "skill_name": name},
        )
        return validation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _goal_to_skill_name(goal: str, fallback: str) -> str:
    """Convert a goal string to a snake_case skill name."""
    import re
    cleaned = re.sub(r"[^A-Za-z0-9\s]", "", goal.lower())
    words   = cleaned.split()[:4]   # first 4 words
    name    = "_".join(words) if words else fallback
    return name[:48] or "compiled_skill"


def _build_description(goal: str, steps: list[dict[str, Any]]) -> str:
    """Build a skill description from goal and step history.

    FIX M-01: goal is sanitized before embedding in the description
    to close the R-24 gap (previously used raw goal in f-string).
    """
    # Strip format-string injection chars and truncate (R-24 light)
    safe_goal = goal.replace("{", "").replace("}", "")[:120]
    actions = [
        s.get("action", "") for s in steps if s.get("action")
    ]
    action_summary = ", ".join(actions[:5])
    return (
        f"Automate: {safe_goal}. "
        f"Actions used: {action_summary or 'various'}."
    )
