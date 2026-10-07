# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/base_skill.py — Phidipus v1.0
Base skill definitions: schema (orchestrator) and runner (sandbox).

Architecture contract (R-03 / SPLIT):
  "Split: BaseSkillDefinition (orchestrator, schema only) +
   BaseSkillRunner (sandbox container, execution)."

This module provides two base classes that must not be mixed:

  BaseSkillDefinition — lives in the orchestrator process (L1).
    Contains only metadata: name, description, input/output schema.
    Never contains execution logic.  No pyautogui, no subprocess.

  BaseSkillRunner — lives inside Docker containers (L3).
    Contains only the run() method.  Never references L1 imports.
    Instances of this class execute ONLY inside ephemeral containers.

The split enforces the process boundary: orchestrator code can import
and inspect BaseSkillDefinition to understand a skill's interface, but
it never instantiates BaseSkillRunner (which would run skill code on
the host, violating R-07).

Process: orchestrator (L1) for BaseSkillDefinition

Security invariants enforced here:
  R-03  BaseSkillDefinition does NOT import LLMClient.
  R-07  BaseSkillRunner is never instantiated in the orchestrator process.
        It is used only as a base class for skills that run inside Docker.

Used by:
  skills/skill_registry.py    — stores BaseSkillDefinition metadata
  skills/skill_generator.py   — generated skills extend BaseSkillRunner
  skill_validator/docker_gate.py — Docker container runs BaseSkillRunner

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# BaseSkillDefinition — orchestrator side (schema only)
# ---------------------------------------------------------------------------

@dataclass
class BaseSkillDefinition:
    """
    Orchestrator-side skill metadata.  Schema only — no execution logic.

    This class is safe to import and use in the orchestrator process.
    It carries only the information needed to route and describe a skill.

    Attributes:
        name:        Unique skill identifier (filesystem-safe string).
        description: Human-readable description of what the skill does.
        version:     Semantic version string (e.g. "1.0.0").
        tags:        Optional list of category tags for discovery.
        input_schema:  JSON-Schema dict describing expected input dict.
        output_schema: JSON-Schema dict describing expected output dict.
        skill_path:  Path to the validated .py file (set after signing).
        sig_path:    Path to the ed25519 .sig file (set after signing).
        pending_human_approval: True for ALL LLM-generated/evolved skills until
                     an admin explicitly approves via Admin Panel or Telegram.
                     This is the outermost gate — even before quarantine.
                     Skills in this state are NEVER executed by the agent loop,
                     regardless of quarantine status. (FIX #2)
        quarantined: True until the skill has been observed to succeed
                     at least once (new skills start quarantined).
        approved_by: Admin identifier who approved the skill (audit trail).
        approved_at: ISO timestamp when the skill was approved.
    """

    name:          str
    description:   str
    version:       str              = "1.0.0"
    tags:          list[str]        = field(default_factory=list)
    input_schema:  dict[str, Any]   = field(default_factory=dict)
    output_schema: dict[str, Any]   = field(default_factory=dict)
    skill_path:    str              = ""
    sig_path:      str              = ""
    # FIX #2: Human approval gate — outermost gate before quarantine.
    # All LLM-generated and evolved skills start here.
    # Manually written / builtin skills can be pre-approved (pending=False).
    pending_human_approval: bool    = True   # default: require human approval
    quarantined:   bool             = True   # new skills start quarantined
    approved_by:   str              = ""     # audit: who approved
    approved_at:   str              = ""     # audit: when approved (ISO)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict — includes FIX #2 approval fields."""
        return {
            "name":                  self.name,
            "description":           self.description,
            "version":               self.version,
            "tags":                  self.tags,
            "input_schema":          self.input_schema,
            "output_schema":         self.output_schema,
            "skill_path":            self.skill_path,
            "sig_path":              self.sig_path,
            "quarantined":           self.quarantined,
            # FIX #2: human-approval gate fields
            "pending_human_approval": self.pending_human_approval,
            "approved_by":           self.approved_by,
            "approved_at":           self.approved_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BaseSkillDefinition":
        """Restore from a dict (e.g. loaded from skill_versions.json).
        FIX #2: pending_human_approval defaults to True for backward-compat
        (old skills without the field are treated as needing approval).
        """
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            version=d.get("version", "1.0.0"),
            tags=list(d.get("tags", [])),
            input_schema=dict(d.get("input_schema", {})),
            output_schema=dict(d.get("output_schema", {})),
            skill_path=d.get("skill_path", ""),
            sig_path=d.get("sig_path", ""),
            quarantined=bool(d.get("quarantined", True)),
            # FIX #2: default True for backward-compat — existing skills
            # without this field are treated as pending until admin reviews them
            pending_human_approval=bool(d.get("pending_human_approval", True)),
            approved_by=str(d.get("approved_by", "")),
            approved_at=str(d.get("approved_at", "")),
        )


# ---------------------------------------------------------------------------
# BaseSkillRunner — Docker sandbox side (execution only)
# ---------------------------------------------------------------------------

class BaseSkillRunner(ABC):
    """
    Base class for skill execution logic.

    IMPORTANT: This class and its subclasses are ONLY instantiated inside
    ephemeral Docker containers (L3).  They must never be instantiated in
    the orchestrator process (R-07).

    Generated skills should subclass BaseSkillRunner and implement run()::

        class MySkill(BaseSkillRunner):
            def run(self, context: dict) -> dict:
                # ... skill logic ...
                return {"success": True, "result": "done"}

    The Docker Gate harness will call run() and capture the return value
    as structured JSON stdout.
    """

    @abstractmethod
    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the skill with the given context.

        Args:
            context: Input dict matching the skill's input_schema.

        Returns:
            Dict with at minimum {"success": bool, "result": str}.
            Any additional fields must be JSON-serialisable.
        """
        ...

    def validate_output(self, output: dict[str, Any]) -> bool:
        """
        Validate that *output* has the required fields.

        Returns True if {"success", "result"} are both present.
        """
        return isinstance(output, dict) and "success" in output and "result" in output
