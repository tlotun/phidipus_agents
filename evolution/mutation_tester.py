# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
evolution/mutation_tester.py — Phidipus v1.0
Generates and tests mutation candidates via Docker Gate only.

Architecture contract (R-07):
  "Testing only via Docker Gate (skill_validator/docker_gate.py).
   No host execution path."

MutationTester has two responsibilities:
  1. Ask the LLM to produce mutated skill source from a BuiltPrompt.
  2. Verify the candidate executes without error in an ephemeral Docker
     container (Gate 2 only — not a full 3-gate validation).

The full 3-gate validation is performed by EvolutionEngine after
MutationTester confirms the candidate runs.  MutationTester is a
pre-filter that eliminates candidates that crash at runtime before
spending resources on Gate 1 (AST) and Gate 3 (Schema + sign).

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-07  No exec_module(), exec(), or eval() on mutation candidates.
        All execution via DockerGate (ephemeral container).

Used by:
  evolution/evolution_engine.py — generate_mutation(), quick_test()

Dependencies:
  evolution/mutation_generator.py — BuiltPrompt (sanitized prompt)
  skill_validator/docker_gate.py  — DockerGate (Gate 2 execution)
  core/llm_client.py              — LLMClient (actual LLM call)
  utils/logger.py                 — get_logger()

FIX (BUG-1): generate_mutation() was a stub returning the prompt text
  instead of calling the LLM.  Fixed by:
  - Adding llm_client parameter to __init__
  - Implementing the actual LLM call in generate_mutation()
  - Extracting Python code from the response via extract_python_code()
"""

from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

from evolution.mutation_generator import BuiltPrompt
from skill_validator.docker_gate import DockerGate, Gate2RejectedError
from utils.logger import get_logger

if TYPE_CHECKING:
    from core.llm_client import LLMClient
    from core.skill_router import SkillRouter

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# MutationTester
# ---------------------------------------------------------------------------

class MutationTester:
    """
    Generates mutation candidates via LLM and pre-tests them via Docker Gate.

    Usage::

        tester = MutationTester(skill_router, llm_client)
        source = await tester.generate_mutation(built_prompt)

        # Pre-test (optional, fast-fail before full 3-gate validation):
        ok = await tester.quick_test(source, skill_name="my_skill_mut_1")

    Args:
        skill_router: SkillRouter — provides access to DockerGate (gate2).
        llm_client:   LLMClient  — used to call the LLM with the mutation
                                   prompt.  Must not be None.

    Security:
        R-01: orchestrator process — no pyautogui/subprocess imports.
        R-07: no exec/eval/exec_module on generated code; all execution
              is via ephemeral Docker container through DockerGate.
    """

    def __init__(
        self,
        skill_router: "SkillRouter",
        llm_client:   "LLMClient",
    ) -> None:
        # Access gate2 from router for isolated execution tests
        self._gate2: DockerGate = skill_router._gate2
        self._llm = llm_client
        _log.debug(
            "MutationTester initialised",
            extra={"llm_client": type(llm_client).__name__},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_mutation(
        self,
        prompt: BuiltPrompt,
    ) -> str:
        """
        Send *prompt* to the LLM and return the mutated skill source.

        The prompt is already sanitized (R-24, R-25, R-26) by
        MutationGenerator.build_mutation_prompt().  This method only
        calls the LLM and extracts the Python code block from the
        response — no further sanitization is performed here.

        Args:
            prompt: BuiltPrompt from MutationGenerator (R-26 compliant).
                    Its to_api_dict() returns {"messages": [...]}, where
                    messages include a system prompt and a pre-sanitized
                    user message with the goal/traceback/skill context.

        Returns:
            Extracted Python source string, or "" on LLM failure or
            empty response.

        Security:
            R-07: result is never exec'd or eval'd on the host.
                  Caller (EvolutionEngine) writes it to disk and routes
                  it through the 3-gate validator.
        """
        api_dict = prompt.to_api_dict()
        messages: list[dict[str, str]] = api_dict.get("messages", [])

        if not messages:
            _log.warning("MutationTester: empty prompt messages — skipping")
            return ""

        # Extract system prompt and user goal from the pre-built message list.
        # MutationGenerator has already applied R-24/R-25 sanitization.
        system_msg = next(
            (m["content"] for m in messages if m.get("role") == "system"),
            "",
        )
        user_msg = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )

        if not user_msg.strip():
            _log.warning("MutationTester: no user message in prompt — skipping")
            return ""

        _log.debug(
            "MutationTester: sending mutation prompt to LLM",
            extra={
                "message_count": len(messages),
                "user_msg_len":  len(user_msg),
            },
        )

        # Actual LLM call — goal is already sanitized by MutationGenerator
        try:
            response = await self._llm.chat_coder(
                goal=user_msg,
                system_prompt=system_msg,
            )
        except Exception as exc:
            _log.warning(
                "MutationTester: LLM call failed",
                extra={"error": str(exc)},
            )
            return ""

        if not response or not response.content:
            _log.warning("MutationTester: LLM returned empty response")
            return ""

        source = extract_python_code(response.content)
        _log.debug(
            "MutationTester: extracted mutation source",
            extra={"source_len": len(source)},
        )
        return source

    async def quick_test(
        self,
        source: str,
        *,
        skill_name: str = "",
    ) -> bool:
        """
        Run *source* through Gate 2 (Docker) only as a quick smoke-test.

        This is a pre-filter — candidates that fail quick_test are skipped
        before the full 3-gate validation.  All execution is inside an
        ephemeral Docker container (R-07).

        Args:
            source:     Python source to test.
            skill_name: Optional label for logging.

        Returns:
            True if Gate 2 passes, False otherwise.

        Security:
            R-07: source is executed only inside an ephemeral Docker
                  container via DockerGate.  No host execution.
        """
        if not source.strip():
            return False
        try:
            self._gate2.run(source, skill_name=skill_name or "mutation_test")
            return True
        except Gate2RejectedError as exc:
            _log.debug(
                "MutationTester: quick_test failed",
                extra={"skill_name": skill_name, "reason": exc.reason},
            )
            return False
        except Exception as exc:
            _log.warning(
                "MutationTester: quick_test unexpected error",
                extra={"error": str(exc)},
            )
            return False


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def extract_python_code(llm_output: str) -> str:
    """
    Extract Python source from an LLM response string.

    Handles fenced ```python ... ``` blocks, generic ``` ... ``` blocks,
    and raw Python output with no fences.
    """
    # Fenced python block
    m = re.search(r"```python\s*\n(.*?)```", llm_output, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Generic fenced block
    m = re.search(r"```\s*\n(.*?)```", llm_output, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Raw output
    return llm_output.strip()
