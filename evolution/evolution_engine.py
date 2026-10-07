# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
evolution/evolution_engine.py — Phidipus v1.0
Drives the skill mutation and scoring loop.

Architecture contract (R-07 / R-08):
  "Route all mutation candidates through skill_validator before scoring.
   No direct exec_module()."

EvolutionEngine orchestrates one evolution cycle:
  1. Generate N mutation candidates via MutationGenerator + MutationTester.
  2. Route each candidate through SkillRouter (3-gate validator).
  3. Score passing candidates via MutationScorer.
  4. Accept the highest-scoring candidate above min_acceptance_score.
  5. Return the winning ValidationResult (or None if none pass).

No skill code is ever exec_module()'d on the host (R-07).  All
execution happens inside ephemeral Docker containers via Gate 2.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-07  exec_module() never called on mutation candidates.
  R-08  Every candidate passes all 3 gates before scoring.

Used by:
  evolution/skill_evolver.py — run_cycle() after task failure

Dependencies:
  evolution/mutation_generator.py — MutationGenerator, PromptParams
  evolution/mutation_tester.py    — MutationTester (LLM call + Gate2 test)
  evolution/mutation_scorer.py    — MutationScorer
  core/skill_router.py            — SkillRouter (3-gate validator)
  core/llm_client.py              — LLMClient (passed to MutationTester)
  utils/atomic_file.py            — atomic_write_text()
  utils/file_ops.py               — is_safe_filename(), safe_join()
  utils/logger.py                 — get_logger()
  config/config_loader.py         — PhidipusConfig

BUG-C FIX: self._scorer.score() is now wrapped in try/except.
  Previously, if ValidationResult had an unexpected shape (e.g. gate2
  attribute missing or stdout_raw is None), an unhandled AttributeError
  would propagate out of the for-loop, discarding any best_result
  accumulated in previous iterations.  Now caught, logged, continue.

Also wrapped atomic_write_text() in its own try/except block so a disk
write failure also continues to the next candidate rather than aborting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from core.llm_client import LLMClient
from core.skill_router import SkillRouter, SkillRoutingError
from evolution.mutation_generator import MutationGenerator, PromptParams
from evolution.mutation_tester import MutationTester
from evolution.mutation_scorer import MutationScorer
from skill_validator.schema_gate import ValidationResult
from utils.atomic_file import atomic_write_text
from utils.file_ops import is_safe_filename, safe_join
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# EvolutionEngine
# ---------------------------------------------------------------------------

class EvolutionEngine:
    """
    Drives one skill-mutation evolution cycle.

    Usage::

        engine = EvolutionEngine(cfg, llm_client, skill_router)
        winner = await engine.run_cycle(
            skill_name="open_browser",
            skill_source=original_source,
            goal="open browser",
            traceback="AttributeError: ...",
        )

    Args:
        cfg:          Validated PhidipusConfig.
        llm_client:   LLMClient — passed to MutationTester for actual LLM calls.
        skill_router: SkillRouter for 3-gate validation and DockerGate access.

    Security:
        R-01 / R-02: orchestrator process — no pyautogui/subprocess.
        R-07 / R-08: no exec_module on candidates; all execution in Docker.
    """

    def __init__(
        self,
        cfg:          PhidipusConfig,
        llm_client:   LLMClient,
        skill_router: SkillRouter,
    ) -> None:
        self._router              = skill_router
        self._mutation_gen        = MutationGenerator(cfg)
        self._tester              = MutationTester(skill_router, llm_client)
        self._scorer              = MutationScorer(cfg)
        self._mutations_per_cycle = cfg.evolution.mutations_per_cycle
        self._min_score           = cfg.evolution.min_acceptance_score
        self._skills_dir          = Path(cfg.paths.data_dir) / "skills" / "evolved"
        self._skills_dir.mkdir(parents=True, exist_ok=True)

        _log.info(
            "EvolutionEngine initialised",
            extra={
                "mutations_per_cycle": self._mutations_per_cycle,
                "min_score":           self._min_score,
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_cycle(
        self,
        skill_name:    str,
        skill_source:  str,
        goal:          str,
        traceback:     str = "",
        context:       dict[str, Any] | None = None,
    ) -> ValidationResult | None:
        """
        Run one evolution cycle on *skill_source*.

        Each step is individually guarded — a single failing candidate
        does NOT abort the cycle or discard previously accumulated results.

        Args:
            skill_name:   Skill being evolved.
            skill_source: Original source (for scorer length comparison).
            goal:         Task goal (sanitized upstream, R-24).
            traceback:    Error traceback (sanitized upstream, R-25).
            context:      Optional additional context (unused).

        Returns:
            ValidationResult of the winning mutation, or None.

        Security:
            R-07: no exec_module on candidates.
            R-08: all candidates must pass all 3 gates before scoring.
        """
        params = PromptParams(
            skill_name=skill_name,
            skill_source=skill_source,
            goal=goal,
            traceback=traceback,
        )

        best_result: ValidationResult | None = None
        best_score:  float                   = -1.0

        for i in range(self._mutations_per_cycle):
            _log.debug(
                "EvolutionEngine: mutation attempt",
                extra={"skill_name": skill_name, "attempt": i + 1},
            )

            # Step 1+2: Build sanitized prompt (R-24/R-25/R-26) + LLM call
            try:
                prompt         = self._mutation_gen.build_mutation_prompt(params)
                mutated_source = await self._tester.generate_mutation(prompt)
            except Exception as exc:
                _log.warning(
                    "EvolutionEngine: mutation generation failed",
                    extra={"attempt": i + 1, "error": str(exc)},
                )
                continue

            if not mutated_source.strip():
                _log.debug(
                    "EvolutionEngine: empty mutation source — skipping",
                    extra={"attempt": i + 1},
                )
                continue

            # Step 3: Write candidate to disk atomically (R-07: never exec on host)
            candidate_name = f"{skill_name}_mut_{i + 1}"
            if not is_safe_filename(candidate_name + ".py"):
                _log.warning(
                    "EvolutionEngine: unsafe candidate name — skipping",
                    extra={"candidate_name": candidate_name},
                )
                continue

            try:
                candidate_path = safe_join(self._skills_dir, candidate_name + ".py")
                atomic_write_text(candidate_path, mutated_source, mode=0o644)
            except Exception as exc:
                _log.warning(
                    "EvolutionEngine: failed to write candidate to disk — skipping",
                    extra={"attempt": i + 1, "error": str(exc)},
                )
                continue

            # Step 4: Validate through all 3 gates (R-07, R-08)
            try:
                validation = self._router.validate_only(
                    skill_source=mutated_source,
                    skill_path=candidate_path,
                    skill_name=candidate_name,
                )
            except SkillRoutingError as exc:
                _log.debug(
                    "EvolutionEngine: candidate failed validation",
                    extra={"attempt": i + 1, "gate": exc.gate},
                )
                continue

            # Step 5: Score the candidate (execution-result based, M-2)
            # BUG-C FIX: wrapped in try/except — malformed ValidationResult
            # (e.g. gate2.stdout_raw is None) must not abort the entire cycle.
            try:
                score = self._scorer.score(
                    validation=validation,
                    original_source=skill_source,
                    goal=goal,
                )
            except Exception as exc:
                _log.warning(
                    "EvolutionEngine: scorer raised unexpected error — skipping candidate",
                    extra={"attempt": i + 1, "error": str(exc)},
                )
                continue

            _log.debug(
                "EvolutionEngine: candidate scored",
                extra={"attempt": i + 1, "score": round(score, 3)},
            )

            # Step 6: Track best above acceptance threshold
            if score > best_score and score >= self._min_score:
                best_score  = score
                best_result = validation

        if best_result:
            _log.info(
                "EvolutionEngine: winning mutation found",
                extra={
                    "skill_name": skill_name,
                    "score":      round(best_score, 3),
                },
            )
        else:
            _log.info(
                "EvolutionEngine: no mutation met acceptance threshold",
                extra={
                    "skill_name": skill_name,
                    "min_score":  self._min_score,
                },
            )

        return best_result
