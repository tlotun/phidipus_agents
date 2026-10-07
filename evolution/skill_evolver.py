# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
evolution/skill_evolver.py — Phidipus v1.0
Coordinates the full skill evolution lifecycle.

Architecture contract (R-17 / H-6):
  "Sign skill_versions.json entries with ed25519 (H-6, R-17).  Add
   quarantine status for newly evolved skills.  Remove unsigned version
   loading."

SkillEvolver is the top-level coordinator for skill self-improvement.
When a skill fails a task, SkillEvolver:
  1. Retrieves the current skill source.
  2. Runs EvolutionEngine to produce a validated mutation.
  3. Registers the evolved skill via SkillRegistry (quarantined).

R-17 compliance
---------------
skill_versions.json is written and signed EXCLUSIVELY by SkillRegistry
(_persist() + sign_skill_versions() on every write).  SkillEvolver does
NOT write to skill_versions.json directly — this avoids the schema
conflict that existed when _write_version_entry() wrote a different
format than SkillRegistry.persist().

BUG-A FIX: Removed _write_version_entry() method.  The previous
implementation wrote skill_versions.json in a format incompatible with
SkillRegistry's BaseSkillDefinition schema — entries were missing "name",
"description", "tags", and "schema_version", which caused all skills to
reload with name="" after an evolution cycle.

BUG-B FIX: By removing direct writes, the R-17 hole (sign_skill_versions
not wrapped in try/except, leaving unsigned file on disk) is eliminated.
All signing is now handled by SkillRegistry._persist() which already has
proper error handling.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-17  skill_versions.json written and signed exclusively by
        SkillRegistry.register() → _persist() → sign_skill_versions().
        SkillEvolver does not write to this file directly.

Used by:
  core/agent_loop.py — evolve_skill() when a task repeatedly fails

Dependencies:
  evolution/evolution_engine.py     — EvolutionEngine.run_cycle()
  skills/skill_registry.py          — SkillRegistry.register()
  skill_validator/schema_gate.py    — ValidationResult
  utils/logger.py                   — get_logger()
  config/config_loader.py           — PhidipusConfig
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from evolution.evolution_engine import EvolutionEngine
from skill_validator.schema_gate import ValidationResult
# [C-18 FIX] Use SkillRegistryDB (v2) instead of legacy SkillRegistry
# so evolved skills appear in ELO scoring, provider history, and dedup
try:
    from skills.skill_db import SkillRegistryDB as SkillRegistry
    _USING_DB_REGISTRY = True
except ImportError:
    from skills.skill_registry import SkillRegistry
    _USING_DB_REGISTRY = False

# Keep error class compatible
try:
    from skills.skill_registry import SkillRegistryError
except ImportError:
    class SkillRegistryError(Exception):
        pass
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SkillEvolutionError(RuntimeError):
    """
    Raised when skill evolution fails.

    Attributes:
        skill_name: The skill that was being evolved.
        reason:     Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        skill_name: str = "",
        reason:     str = "EVOLUTION_FAILED",
    ) -> None:
        super().__init__(message)
        self.skill_name = skill_name
        self.reason     = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.skill_name:
            parts.append(f"skill={self.skill_name!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# SkillEvolver
# ---------------------------------------------------------------------------

# [C-18] Runtime check
import logging as _evolver_logging
_evolver_log = _evolver_logging.getLogger("phidipus.evolution")

class SkillEvolver:
    """
    Coordinates skill mutation, validation, and version registration.

    R-17 contract: skill_versions.json is written and signed solely by
    SkillRegistry.  SkillEvolver calls register() and trusts the registry
    to handle all persistence — it does NOT write skill_versions.json
    directly (that caused a schema conflict and a potential unsigned-file
    window — see BUG-A / BUG-B fixed in this version).

    Usage::

        evolver = SkillEvolver(cfg, evolution_engine, skill_registry)
        result  = await evolver.evolve(
            skill_name="open_browser",
            skill_source=current_source,
            goal="open browser",
            traceback="AttributeError: ...",
        )
        if result:
            print("Evolved skill registered:", result.skill_name)

    Args:
        cfg:              Validated PhidipusConfig.
        evolution_engine: EvolutionEngine instance.
        skill_registry:   SkillRegistry instance.
    """

    def __init__(
        self,
        cfg:              PhidipusConfig,
        evolution_engine: EvolutionEngine,
        skill_registry:   SkillRegistry,
    ) -> None:
        self._engine   = evolution_engine
        self._registry = skill_registry

        _log.info("SkillEvolver initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evolve(
        self,
        skill_name:   str,
        skill_source: str,
        goal:         str,
        traceback:    str = "",
    ) -> ValidationResult | None:
        """
        Evolve *skill_name* and register the best mutation.

        Pipeline:
          1. Run EvolutionEngine to produce a validated mutation.
          2. Register via SkillRegistry (quarantined, R-09).
             SkillRegistry._persist() handles writing and signing
             skill_versions.json (R-17).

        Args:
            skill_name:   Name of the skill to evolve.
            skill_source: Current skill source code.
            goal:         Task goal (sanitized upstream, R-24).
            traceback:    Error traceback (sanitized upstream, R-25).

        Returns:
            ValidationResult of the winning mutation, or None if no
            mutation beat the acceptance threshold.

        Raises:
            SkillEvolutionError: on engine failure or registration failure.
        """
        _log.info(
            "SkillEvolver: starting evolution cycle",
            extra={"skill_name": skill_name},
        )

        # Step 1: Run evolution cycle
        try:
            validation = await self._engine.run_cycle(
                skill_name=skill_name,
                skill_source=skill_source,
                goal=goal,
                traceback=traceback,
            )
        except Exception as exc:
            raise SkillEvolutionError(
                f"Evolution engine failed for {skill_name!r}: {exc}",
                skill_name=skill_name,
                reason="ENGINE_FAILED",
            ) from exc

        if validation is None:
            _log.info(
                "SkillEvolver: no viable mutation found",
                extra={"skill_name": skill_name},
            )
            return None

        # Step 2: Register evolved skill (quarantined).
        # SkillRegistry.register() verifies the .sig (R-09), writes
        # skill_versions.json in the canonical BaseSkillDefinition format,
        # and signs immediately (R-17) — all handled inside register().
        try:
            self._registry.register(validation)
        except SkillRegistryError as exc:
            _log.error(
                "SkillEvolver: registration failed",
                extra={"skill_name": skill_name, "error": str(exc)},
            )
            raise SkillEvolutionError(
                f"Failed to register evolved skill {skill_name!r}: {exc}",
                skill_name=skill_name,
                reason="REGISTRATION_FAILED",
            ) from exc

        _log.info(
            "SkillEvolver: evolution complete — skill registered (quarantined)",
            extra={
                "evolved_name": validation.skill_name,
                "sig_path":     str(validation.sig_path),
            },
        )
        return validation
