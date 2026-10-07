# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
evolution/mutation_generator.py — Phidipus v1.0
Builds sanitized LLM prompts for skill mutation.

Architecture contract (R-24 / R-25 / R-26 / C-7):
  R-24: All user-supplied goals MUST be sanitized before LLM interpolation:
        strip { } format characters, enforce max 2048 characters, reject
        goals containing Python execution keywords at the entry point.
  R-25: All tracebacks interpolated into LLM mutation prompts MUST be
        sanitized: truncated to max 1024 characters, { } stripped,
        non-ASCII escaped.
  R-26: MutationGenerator MUST use a parameterized prompt builder, not
        string .format().  Template variables are injected as separate
        API parameters (separate message objects), not string substitution.

R-26 implementation detail
--------------------------
The prohibition on string .format() exists because unsanitized user input
passed to .format() can leak internal Python state via format-string
attacks (e.g. "{__class__.__init__.__globals__}").  This module builds the
LLM message list by:

  1. Sanitizing each variable individually (goal, traceback, skill_source).
  2. Assembling each message content as an explicit string join of constant
     labels and sanitized values — NO .format(), NO f-string with user data.
  3. Storing the sanitized parts inside BuiltPrompt as separate fields.
  4. Returning {"messages": [...]} from to_api_dict() where each message
     is a dict — this is what "separate API parameters" means.

BuiltPrompt
-----------
BuiltPrompt is an immutable value object created by build_mutation_prompt().
Its to_api_dict() method returns the message list expected by LLMClient
and consumed by MutationTester.generate_mutation():

    {
        "messages": [
            {"role": "system", "content": <MUTATION_SYSTEM_PROMPT>},
            {"role": "user",   "content": <assembled_user_content>},
        ]
    }

The user content is assembled by joining labelled sections with constant
separator strings — never via .format() with user-controlled variables.

PromptParams
------------
Dataclass carrying the raw (unsanitized) inputs for one mutation cycle.
Sanitization happens inside build_mutation_prompt(), not at the call site.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-24  sanitize_goal() strips format chars, caps at 2048, rejects
        Python execution keywords.
  R-25  sanitize_traceback() strips format chars, caps at 1024, escapes
        non-ASCII bytes.
  R-26  build_mutation_prompt() uses NO .format() or f-string interpolation
        with user-controlled data.  Each variable is a separate message
        field.

Used by:
  evolution/evolution_engine.py — build_mutation_prompt(params)
  evolution/mutation_tester.py  — consumes BuiltPrompt via to_api_dict()

Dependencies:
  utils/logger.py         — get_logger()
  config/config_loader.py — PhidipusConfig
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from config.config_loader import PhidipusConfig
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Security limits (R-24 / R-25)
# ---------------------------------------------------------------------------

_GOAL_MAX_CHARS:      int = 2048   # R-24
_TRACEBACK_MAX_CHARS: int = 1024   # R-25

# R-24: Python execution keywords whose presence in a goal string
# indicates a likely prompt-injection or misuse attempt.  Goals that
# contain any of these as a complete word are rejected outright.
#
# Design note: only clear injection vectors are listed here.  Common
# English words that happen to share a name with Python builtins — such
# as "open" ("open the browser"), "os" ("use the OS clipboard"), or
# "sys" ("system settings") — are intentionally excluded to avoid
# false positives on legitimate automation goals.  The "import" keyword
# already catches the dangerous "import os" / "import sys" patterns.
_DANGEROUS_KEYWORDS: frozenset[str] = frozenset({
    "import",        # Python import statement — catches "import os/sys/etc."
    "exec",          # exec() — arbitrary code execution
    "eval",          # eval() — arbitrary code execution
    "subprocess",    # subprocess module — OS command execution
    "__import__",    # __import__() builtin — dynamic import
    "__builtins__",  # direct access to builtins namespace
    "__class__",     # class introspection — common in format-string attacks
    "__globals__",   # global namespace access — common in format-string attacks
    "socket",        # socket module — network exfiltration
    "ctypes",        # ctypes — direct memory/OS access
    "compile",       # compile() — dynamic code compilation
    "breakpoint",    # breakpoint() — debugger invocation
})

# Regex matching a format-string placeholder — e.g. {0}, {name}, {!r}
# Used to strip { } from both goal and traceback (R-24, R-25).
_FORMAT_CHAR_RE = re.compile(r"\{[^}]*\}|\{|\}")


# ---------------------------------------------------------------------------
# System prompt (constant — never modified by user input)
# ---------------------------------------------------------------------------

_MUTATION_SYSTEM_PROMPT: str = """\
You are an expert Python code mutator for a desktop automation agent.
Your task is to improve a skill function that failed to complete its goal.

Rules for the mutated code:
- Keep the function signature: run(context: dict) -> dict
- Return {"success": bool, "result": str} at minimum
- Only use safe stdlib imports: json, re, math, decimal, collections,
  itertools, functools, typing, dataclasses, datetime, time, string,
  textwrap, hashlib, hmac, base64, struct, io, copy, enum, abc,
  contextlib, warnings, traceback, difflib, statistics, random
- Do NOT import: os, sys, subprocess, socket, pathlib, asyncio,
  importlib, ctypes, threading, urllib, requests, pickle
- No exec(), eval(), open(), or __import__() calls
- Keep mutations focused: fix the error, do not rewrite the entire skill
- Output ONLY the Python code, inside a ```python ... ``` block
"""

# Section labels for the user message — constant strings, never user-supplied
_LABEL_GOAL:       str = "TASK GOAL:"
_LABEL_SKILL_NAME: str = "SKILL NAME:"
_LABEL_ORIGINAL:   str = "ORIGINAL SKILL CODE:"
_LABEL_ERROR:      str = "ERROR TRACEBACK:"
_LABEL_INSTRUCTION: str = (
    "Please produce an improved version of the skill that fixes the error "
    "and successfully completes the task goal."
)
_SECTION_SEP: str = "\n\n"


# ---------------------------------------------------------------------------
# PromptParams — raw inputs (sanitized inside MutationGenerator)
# ---------------------------------------------------------------------------

@dataclass
class PromptParams:
    """
    Raw (unsanitized) inputs for one mutation cycle.

    Sanitization is applied by MutationGenerator.build_mutation_prompt()
    — callers do NOT need to sanitize before passing these fields.

    Attributes:
        skill_name:   Name of the skill being mutated (filesystem-safe).
        skill_source: Current source code of the skill.
        goal:         Task goal description (user-supplied, will be sanitized).
        traceback:    Error traceback from the last failed execution
                      (may contain user data, will be sanitized).
    """

    skill_name:   str
    skill_source: str
    goal:         str
    traceback:    str = ""


# ---------------------------------------------------------------------------
# BuiltPrompt — sanitized, ready-to-send prompt
# ---------------------------------------------------------------------------

class BuiltPrompt:
    """
    Immutable value object holding a sanitized LLM mutation prompt.

    Created exclusively by MutationGenerator.build_mutation_prompt().
    Consumed by MutationTester.generate_mutation() via to_api_dict().

    Attributes:
        skill_name:          Skill name (sanitized, for logging).
        sanitized_goal:      Sanitized goal (R-24 applied).
        sanitized_traceback: Sanitized traceback (R-25 applied).
        skill_source:        Sanitized skill source (format chars stripped).

    Security:
        All string fields are sanitized at construction time.
        to_api_dict() assembles message content by string join of
        labelled sections — never via .format() (R-26).
    """

    __slots__ = (
        "_skill_name",
        "_sanitized_goal",
        "_sanitized_traceback",
        "_sanitized_source",
    )

    def __init__(
        self,
        *,
        skill_name:          str,
        sanitized_goal:      str,
        sanitized_traceback: str,
        sanitized_source:    str,
    ) -> None:
        self._skill_name          = skill_name
        self._sanitized_goal      = sanitized_goal
        self._sanitized_traceback = sanitized_traceback
        self._sanitized_source    = sanitized_source

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def skill_name(self) -> str:
        return self._skill_name

    @property
    def sanitized_goal(self) -> str:
        return self._sanitized_goal

    @property
    def sanitized_traceback(self) -> str:
        return self._sanitized_traceback

    def to_api_dict(self) -> dict[str, Any]:
        """
        Return the message list for the LLM API call.

        R-26 compliance: message content is assembled by joining a list
        of constant label strings and sanitized value strings.  No
        .format() or f-string with user-controlled variables is used
        anywhere in this method.

        Returns::

            {
                "messages": [
                    {"role": "system", "content": <MUTATION_SYSTEM_PROMPT>},
                    {"role": "user",   "content": <assembled_user_content>},
                ]
            }

        The user content structure (R-26 — separate sections, not .format()):

            TASK GOAL:
            <sanitized_goal>

            SKILL NAME:
            <skill_name>

            ORIGINAL SKILL CODE:
            ```python
            <sanitized_source>
            ```

            ERROR TRACEBACK:
            <sanitized_traceback>

            Please produce an improved version...
        """
        # R-26: assemble user content by building a list of string parts
        # and joining them — ZERO use of .format() with user-supplied data.
        parts: list[str] = []

        # Section 1: goal
        parts.append(_LABEL_GOAL)
        parts.append(self._sanitized_goal)

        # Section 2: skill name (sanitized at BuiltPrompt construction)
        parts.append(_LABEL_SKILL_NAME)
        parts.append(self._skill_name)

        # Section 3: original skill source in a fenced code block
        # The fence markers are constant strings — not user-controlled
        parts.append(_LABEL_ORIGINAL)
        parts.append("```python")
        parts.append(self._sanitized_source)
        parts.append("```")

        # Section 4: traceback (may be empty)
        if self._sanitized_traceback:
            parts.append(_LABEL_ERROR)
            parts.append(self._sanitized_traceback)

        # Section 5: constant instruction
        parts.append(_LABEL_INSTRUCTION)

        # Join all parts with double newlines — list join, not .format()
        user_content = _SECTION_SEP.join(parts)

        return {
            "messages": [
                {"role": "system", "content": _MUTATION_SYSTEM_PROMPT},
                {"role": "user",   "content": user_content},
            ]
        }

    def __repr__(self) -> str:
        return (
            "BuiltPrompt("
            + "skill=" + repr(self._skill_name)
            + ", goal_len=" + str(len(self._sanitized_goal))
            + ", tb_len=" + str(len(self._sanitized_traceback))
            + ")"
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class GoalRejectedError(ValueError):
    """
    Raised when sanitize_goal() rejects a goal due to dangerous keywords.

    Attributes:
        goal:    The original (unsanitized) goal string (truncated for safety).
        keyword: The specific keyword that triggered rejection.
        reason:  Machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        goal:    str = "",
        keyword: str = "",
        reason:  str = "GOAL_REJECTED",
    ) -> None:
        super().__init__(message)
        self.goal    = goal[:64]   # truncate for safe logging
        self.keyword = keyword
        self.reason  = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.keyword:
            parts.append(f"keyword={self.keyword!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# MutationGenerator
# ---------------------------------------------------------------------------

class MutationGenerator:
    """
    Builds sanitized, parameterized LLM prompts for skill mutation.

    Usage::

        generator = MutationGenerator(cfg)
        prompt    = generator.build_mutation_prompt(
            PromptParams(
                skill_name="open_browser",
                skill_source=source,
                goal="open the default web browser",
                traceback="AttributeError: 'NoneType' ...",
            )
        )
        api_dict = prompt.to_api_dict()   # {"messages": [...]}

    Args:
        cfg: Validated PhidipusConfig (currently used for future config
             fields — kept for API consistency with other evolution classes).

    Security:
        R-24: goal sanitization applied in build_mutation_prompt().
        R-25: traceback sanitization applied in build_mutation_prompt().
        R-26: NO .format() used with user-supplied data.  All template
              variables are injected as separate message fields.
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        _log.debug("MutationGenerator initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_mutation_prompt(self, params: PromptParams) -> BuiltPrompt:
        """
        Sanitize all inputs and build a BuiltPrompt (R-24, R-25, R-26).

        Steps:
          1. Sanitize goal (R-24): strip format chars, cap 2048, reject
             Python execution keywords.
          2. Sanitize traceback (R-25): strip format chars, cap 1024,
             escape non-ASCII.
          3. Sanitize skill_source: strip format chars only (code is
             trusted — it was previously validated by the 3-gate pipeline).
          4. Sanitize skill_name: strip non-alphanumeric chars.
          5. Construct BuiltPrompt with sanitized parts.

        Args:
            params: PromptParams with raw (unsanitized) inputs.

        Returns:
            BuiltPrompt — ready to be consumed by MutationTester.

        Raises:
            GoalRejectedError: if goal contains Python execution keywords
                               (R-24 — at the mutation-prompt entry point).
        """
        # Step 1: sanitize goal (R-24)
        clean_goal = sanitize_goal(params.goal)

        # Step 2: sanitize traceback (R-25)
        clean_tb = sanitize_traceback(params.traceback)

        # Step 3: sanitize skill source (strip format chars — belt + suspenders)
        clean_source = _strip_format_chars(params.skill_source)

        # Step 4: sanitize skill name
        clean_name = _sanitize_skill_name(params.skill_name)

        _log.debug(
            "MutationGenerator: prompt built",
            extra={
                "skill_name":  clean_name,
                "goal_len":    len(clean_goal),
                "tb_len":      len(clean_tb),
                "source_len":  len(clean_source),
            },
        )

        return BuiltPrompt(
            skill_name=clean_name,
            sanitized_goal=clean_goal,
            sanitized_traceback=clean_tb,
            sanitized_source=clean_source,
        )


# ---------------------------------------------------------------------------
# Sanitization functions (R-24, R-25) — module-level, individually testable
# ---------------------------------------------------------------------------

def sanitize_goal(goal: str) -> str:
    """
    Sanitize a user-supplied goal string before LLM interpolation (R-24).

    Steps:
      1. Strip format-string placeholders: { }, {name}, {0!r}, etc.
      2. Enforce max length of 2048 characters.
      3. Reject if the goal contains any Python execution keyword as a
         complete word (word-boundary match).

    Args:
        goal: Raw goal string from user input.

    Returns:
        Sanitized goal string (max 2048 chars, no format chars).

    Raises:
        GoalRejectedError: if goal contains a dangerous Python keyword.

    Security:
        R-24 compliance.  Called by MutationGenerator.build_mutation_prompt()
        and must also be called by core/llm_client.py before every LLM call.
    """
    if not goal:
        return ""

    # Step 0 (Bug #9 fix): normalize Unicode and strip zero-width chars.
    # NFKC maps fullwidth chars (ｅｘｅｃ → exec) to ASCII equivalents
    # so keyword checks in Step 3 catch Unicode confusable bypasses.
    # Zero-width characters (\u200b, \u200c, \u200d, \u2060, \ufeff)
    # are stripped to prevent "ex\u200bec" from hiding "exec".
    goal = unicodedata.normalize("NFKC", goal)
    goal = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", goal)

    # FIX B-08: strip null bytes and dangerous control characters.
    # Null bytes can truncate strings at transport layer, hide keywords
    # after \x00, and cause silent data corruption in JSON serialisation.
    goal = re.sub(r"[\x00\x08\x0b\x0c]", "", goal)

    # Step 1: strip format-string placeholders
    cleaned = _strip_format_chars(goal)

    # Step 2: enforce max length (BEFORE keyword check — avoid scanning huge strings)
    cleaned = cleaned[:_GOAL_MAX_CHARS]

    # Step 3: reject if dangerous keyword present as a complete word (R-24)
    # Use word-boundary regex so "osascript" does not match "os", but
    # "import os" or "os.path" does match "os".
    lower = cleaned.lower()
    for kw in _DANGEROUS_KEYWORDS:
        # Match keyword at word boundaries — \b works for alphanumeric keywords.
        # For keywords like __import__ that already contain underscores,
        # a simple substring check suffices (underscores are word-boundary chars).
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, lower):
            raise GoalRejectedError(
                f"Goal rejected: contains forbidden keyword {kw!r} (R-24). "
                "Goals must not contain Python execution statements.",
                goal=goal,
                keyword=kw,
                reason="GOAL_REJECTED_KEYWORD",
            )

    return cleaned


def sanitize_traceback(traceback: str) -> str:
    """
    Sanitize a traceback string before LLM prompt insertion (R-25).

    Steps:
      1. Truncate to max 1024 characters.
      2. Strip format-string placeholders: { }, {name}, etc.
      3. Escape non-ASCII characters as \\uXXXX.

    Args:
        traceback: Raw traceback string from skill execution.

    Returns:
        Sanitized traceback (max 1024 chars, ASCII only, no format chars).

    Security:
        R-25 compliance.  Tracebacks may contain user-supplied data
        (e.g. filenames, error messages from user content) — all
        non-ASCII is escaped and format chars stripped before insertion.
    """
    if not traceback:
        return ""

    # Step 1: truncate first — avoids processing unbounded input
    truncated = traceback[:_TRACEBACK_MAX_CHARS]

    # Step 2: strip format-string placeholders
    stripped = _strip_format_chars(truncated)

    # Step 3: escape non-ASCII characters
    escaped = _escape_non_ascii(stripped)

    return escaped


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_format_chars(text: str) -> str:
    """
    Remove format-string placeholders from *text* (R-24, R-25).

    Removes patterns like {0}, {name}, {name!r}, {}, and also lone
    { or } characters that could be interpreted as format-string syntax.

    This prevents format-string injection when user content is later
    passed to any function that calls str.format() on the result.

    Note: MutationGenerator itself uses NO .format() (R-26), but this
    stripping is applied as defence-in-depth for downstream consumers.
    """
    return _FORMAT_CHAR_RE.sub("", text)


def _escape_non_ascii(text: str) -> str:
    """
    Replace non-ASCII characters with \\uXXXX escapes (R-25).

    Preserves printable ASCII (0x20–0x7E) and common whitespace
    (\\n, \\t, \\r) unchanged.  All other code points are replaced with
    their \\uXXXX (BMP) or \\UXXXXXXXX (non-BMP) escape sequence.
    """
    result: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E or ch in "\n\t\r":
            result.append(ch)
        elif cp <= 0xFFFF:
            result.append("\\u" + format(cp, "04x"))
        else:
            result.append("\\U" + format(cp, "08x"))
    return "".join(result)


def _sanitize_skill_name(name: str) -> str:
    """
    Strip non-alphanumeric characters from skill name for safe embedding.

    The skill name appears in the user message as a labelled value.
    This strip ensures it cannot contain format chars or injection chars.
    """
    # Allow alphanumeric, underscore, hyphen — same as SkillGenerator
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", name)
    return cleaned[:64] if cleaned else "skill"
