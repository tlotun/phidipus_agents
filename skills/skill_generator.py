# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/skill_generator.py — Phidipus v1.0
LLM-based skill code generator with mandatory validator pipeline routing.

Architecture contract (R-07 / R-08 / C-5):
  "Set allow_network=False (C-5).  Generated code sent to skill_validator,
   never exec_module() on host."

SkillGenerator asks the LLM to produce Python skill code for a given goal,
then immediately routes the generated source through SkillRouter (the
3-gate validator: AST → Docker → Schema + sign).  The generated code never
reaches exec_module(), eval(), or exec() in the host process.

The skill is only considered valid after SkillRouter returns a signed
ValidationResult.  The caller receives the ValidationResult and may then
register the skill via SkillRegistry.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-07  Generated skill code is never exec_module()'d on the host.
        It is written to disk and routed through SkillRouter (3 gates).
  R-08  All generated skill code MUST pass all three gates.

Used by:
  evolution/evolution_engine.py — generate_skill() for mutation candidates
  core/agent_loop.py            — on-demand skill creation

Dependencies:
  core/llm_client.py             — LLMClient.chat_coder()
  core/skill_router.py           — SkillRouter.validate_and_load()
  skill_validator/schema_gate.py — ValidationResult
  utils/atomic_file.py           — atomic_write_text()
  utils/file_ops.py              — safe_join(), is_safe_filename()
  utils/logger.py                — get_logger()
  config/config_loader.py        — PhidipusConfig
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from core.llm_client import LLMClient
from core.skill_router import SkillRouter, SkillRoutingError
from skill_validator.schema_gate import ValidationResult
from utils.atomic_file import atomic_write_text
from utils.file_ops import is_safe_filename, safe_join
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SKILL_SYSTEM_PROMPT = """\
You are a Python code generator for a desktop automation agent.
Write a Python skill function for the given task description.

Rules:
- Only import from: json, re, math, decimal, collections, itertools,
  functools, typing, dataclasses, datetime, time, string, textwrap,
  hashlib, hmac, base64, struct, io, copy, enum, abc, contextlib,
  warnings, traceback, difflib, statistics, random
- Do NOT import: os, sys, subprocess, socket, pathlib, asyncio,
  importlib, ctypes, threading, urllib, requests, pickle
- The function must be named `run(context: dict) -> dict`
- Return a dict with at least {"success": bool, "result": str}
- No exec(), eval(), open(), or __import__() calls
- Keep code under 200 lines
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SkillGenerationError(RuntimeError):
    """
    Raised when skill generation or validation fails.

    Attributes:
        skill_name: Name of the skill being generated.
        reason:     Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        skill_name: str = "",
        reason:     str = "GENERATION_FAILED",
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
# SkillGenerator
# ---------------------------------------------------------------------------

class SkillGenerator:
    """
    Generates and validates Python skill code via LLM + 3-gate pipeline.

    Usage::

        generator = SkillGenerator(cfg, llm_client, skill_router)
        result    = await generator.generate(
            skill_name="open_browser",
            description="Open the default web browser application",
        )
        # result.sig_path is the signed .sig file

    Args:
        cfg:          Validated PhidipusConfig.
        llm_client:   LLMClient instance.
        skill_router: SkillRouter instance (3-gate validator).
    """

    def __init__(
        self,
        cfg:          PhidipusConfig,
        llm_client:   LLMClient,
        skill_router: SkillRouter,
    ) -> None:
        self._llm          = llm_client
        self._router       = skill_router
        self._skills_dir   = Path(cfg.paths.data_dir) / "skills" / "generated"
        self._skills_dir.mkdir(parents=True, exist_ok=True)

        _log.info(
            "SkillGenerator initialised",
            extra={"skills_dir": str(self._skills_dir)},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        skill_name:  str,
        description: str,
    ) -> ValidationResult:
        """
        Generate, validate, and sign a skill for *description*.

        Pipeline:
          1. Sanitize skill_name for filesystem safety.
          2. Ask LLM (coder model) to produce skill Python source.
          3. Extract code from LLM response.
          4. Write source to disk atomically.
          5. Route through SkillRouter (3 gates + sign).
          6. Return ValidationResult.

        Args:
            skill_name:  Filesystem-safe skill identifier.
            description: Task description for LLM prompt.

        Returns:
            ValidationResult — skill is signed and ready for registration.

        Raises:
            SkillGenerationError: on LLM failure, unsafe name, or gate rejection.
        """
        # Validate skill name
        safe_name = _make_safe_skill_name(skill_name)
        if not is_safe_filename(safe_name + ".py"):
            raise SkillGenerationError(
                f"Skill name {skill_name!r} produces unsafe filename.",
                skill_name=skill_name,
                reason="UNSAFE_NAME",
            )

        _log.info(
            "SkillGenerator: generating skill",
            extra={"skill_name": safe_name, "description_len": len(description)},
        )

        # Generate source via LLM
        try:
            response = await self._llm.chat_coder(
                goal=description,
                system_prompt=_SKILL_SYSTEM_PROMPT,
            )
        except Exception as exc:
            raise SkillGenerationError(
                f"LLM call failed for skill {safe_name!r}: {exc}",
                skill_name=safe_name,
                reason="LLM_FAILED",
            ) from exc

        source = _extract_code(response.content)
        if not source.strip():
            raise SkillGenerationError(
                f"LLM produced no Python code for skill {safe_name!r}.",
                skill_name=safe_name,
                reason="EMPTY_SOURCE",
            )

        # Write source to disk atomically (R-07: never exec on host)
        skill_path = safe_join(self._skills_dir, safe_name + ".py")
        atomic_write_text(skill_path, source, mode=0o644)

        # Route through 3-gate validator (R-07, R-08)
        try:
            validation = self._router.validate_only(
                skill_source=source,
                skill_path=skill_path,
                skill_name=safe_name,
            )
        except SkillRoutingError as exc:
            raise SkillGenerationError(
                f"Skill {safe_name!r} failed validation: {exc}",
                skill_name=safe_name,
                reason=f"VALIDATION_{exc.gate.upper()}_FAILED",
            ) from exc

        _log.info(
            "SkillGenerator: skill generated and validated",
            extra={
                "skill_name": safe_name,
                "sig_path":   str(validation.sig_path),
            },
        )
        return validation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_safe_skill_name(name: str) -> str:
    """Convert *name* to a safe filesystem component."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", name)
    cleaned = cleaned.strip("_-")
    return cleaned[:64] if cleaned else "skill"


def _extract_code(llm_output: str) -> str:
    """
    Extract Python source from LLM response.

    Handles:
      - Raw Python code (no fences)
      - ```python ... ``` fenced blocks
      - ``` ... ``` fenced blocks
    """
    # Try fenced python block first
    m = re.search(r"```python\s*\n(.*?)```", llm_output, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Generic fenced block
    m = re.search(r"```\s*\n(.*?)```", llm_output, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Raw output — return as-is
    return llm_output.strip()
