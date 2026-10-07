# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/openclaw_adapter.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

OpenClaw → Phidipus Skill Adapter.
Converts SKILL.md format to Phidipus skill_registry compatible format.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")

_BASE_DIR = Path(__file__).parent.parent
_INSTALLED_DIR = _BASE_DIR / "data" / "openclaw" / "installed"


def convert_skill(quarantine_dir: str, metadata: dict) -> dict:
    """
    Convert OpenClaw skill to Phidipus format.
    Moves from quarantine → installed.
    Returns Phidipus skill registration dict.
    """
    if not quarantine_dir or not Path(quarantine_dir).exists():
        raise ValueError(f"Invalid quarantine directory: {quarantine_dir!r}")
    qpath = Path(quarantine_dir)
    if not qpath.is_dir():
        raise ValueError(f"Quarantine path is not a directory: {quarantine_dir}")
    name = metadata.get("name", qpath.name)

    # Parse SKILL.md
    skill_md_path = qpath / "SKILL.md"
    skill_md = ""
    if skill_md_path.exists():
        skill_md = skill_md_path.read_text(encoding="utf-8", errors="ignore")

    # Extract info from SKILL.md
    description = metadata.get("description", "")
    if not description:
        lines = [l.strip() for l in skill_md.split("\n") if l.strip() and not l.startswith(("#", "---", "metadata"))]
        description = lines[0][:200] if lines else name

    # Extract trigger patterns from ## Usage section
    triggers = _extract_triggers(skill_md)

    # Move to installed directory
    install_dir = _INSTALLED_DIR / name
    if install_dir.exists():
        shutil.rmtree(str(install_dir))
    shutil.copytree(str(qpath), str(install_dir))

    # Create Phidipus wrapper
    wrapper_code = _generate_wrapper(name, metadata, skill_md)
    (install_dir / "_phidipus_wrapper.py").write_text(wrapper_code, encoding="utf-8")

    # Registration info
    reg_info = {
        "name": f"openclaw/{name}",
        "display_name": metadata.get("emoji", "📦") + " " + name,
        "description": description,
        "trigger_patterns": triggers,
        "type": "openclaw_imported",
        "source": "clawhub",
        "version": metadata.get("version", "1.0.0"),
        "author": metadata.get("author", "community"),
        "install_path": str(install_dir),
        "wrapper_path": str(install_dir / "_phidipus_wrapper.py"),
        "security_score": metadata.get("security_score", 0),
        "permissions": metadata.get("permissions", []),
        "enabled": True,
    }

    _vlog("✅", f"Skill converted: {name} → {install_dir}")
    return reg_info


def _extract_triggers(skill_md: str) -> list[str]:
    """Extract trigger phrases from SKILL.md Usage section."""
    triggers = []
    in_usage = False
    for line in skill_md.split("\n"):
        if re.match(r'^##\s+Usage', line, re.IGNORECASE):
            in_usage = True
            continue
        if in_usage and line.startswith("## "):
            break
        if in_usage and line.strip():
            # Look for "When user asks to..." patterns
            m = re.search(r'(?:when|if).*(?:ask|say|type|request)[^.]*["\']([^"\']+)["\']', line, re.IGNORECASE)
            if m:
                triggers.append(m.group(1))
            # Bullet points as triggers
            m2 = re.match(r'^[\s*-]+(.+)', line)
            if m2 and len(m2.group(1).strip()) < 60:
                triggers.append(m2.group(1).strip())
    return triggers[:10]  # Max 10 triggers


def _generate_wrapper(name: str, metadata: dict, skill_md: str) -> str:
    """Generate Phidipus-compatible Python wrapper for the skill."""
    return f'''"""
Auto-generated Phidipus wrapper for OpenClaw skill: {name}
Source: ClawHub
Version: {metadata.get("version", "1.0.0")}
Security Score: {metadata.get("security_score", 0)}/100
"""
from __future__ import annotations
import asyncio
from pathlib import Path

SKILL_DIR = Path(__file__).parent
SKILL_NAME = "{name}"
SKILL_MD = SKILL_DIR / "SKILL.md"

async def run(goal: str, context: dict = None) -> dict:
    """
    Execute OpenClaw skill.
    Reads SKILL.md instructions and delegates to appropriate Phidipus subsystem.
    """
    context = context or {{}}

    # Read skill instructions
    instructions = ""
    if SKILL_MD.exists():
        instructions = SKILL_MD.read_text(encoding="utf-8")[:3000]

    # Use Phidipus's LLM to interpret skill instructions + goal
    try:
        from core.llm_client import get_llm_client
        llm = get_llm_client()
        prompt = f"""You are executing an OpenClaw skill.

Skill instructions:
{{instructions[:2000]}}

User goal: {{goal}}

Execute the skill and return the result. Respond with a JSON:
{{"success": true/false, "result": "...", "error": ""}}"""

        response = await llm.complete(prompt, max_tokens=500)
        return {{"success": True, "result": response, "skill": SKILL_NAME}}
    except Exception as exc:
        return {{"success": False, "error": str(exc), "skill": SKILL_NAME}}

def get_info() -> dict:
    return {{
        "name": SKILL_NAME,
        "type": "openclaw",
        "path": str(SKILL_DIR),
    }}
'''
