# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/openclaw_bridge.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

OpenClaw Bridge — Kết nối Phidipus với ClawHub skill registry (3,286 skills).

API Endpoints:
  ClawHub:  https://registry.clawhub.net/api/v1/
  Search:   GET /skills?q=...&limit=...
  Detail:   GET /skills/{name}
  Download: GET /skills/{name}/versions/{version}/download

Security: mọi skill phải qua 5-gate pipeline trước khi install.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════

CLAWHUB_API = "https://registry.clawhub.net/api/v1"
CLAWHUB_SEARCH = f"{CLAWHUB_API}/skills"

# Phidipus directories
_BASE_DIR = Path(__file__).parent.parent
_OPENCLAW_DIR = _BASE_DIR / "data" / "openclaw"
_QUARANTINE_DIR = _OPENCLAW_DIR / "quarantine"
_INSTALLED_DIR = _OPENCLAW_DIR / "installed"
_REGISTRY_FILE = _OPENCLAW_DIR / "registry.json"

for _d in [_OPENCLAW_DIR, _QUARANTINE_DIR, _INSTALLED_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════
# Registry — local index of installed/reviewed skills
# ══════════════════════════════════════════════════════════════

def _load_registry() -> dict:
    try:
        return json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"installed": {}, "rejected": {}, "pending": {}}

def _save_registry(data: dict):
    _REGISTRY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# ClawHub API Client
# ══════════════════════════════════════════════════════════════

class ClawHubClient:
    """Client for ClawHub skill registry API."""

    def __init__(self, api_base: str = CLAWHUB_API, timeout: float = 10.0):
        self._api = api_base.rstrip("/")
        self._timeout = timeout
        self._cache: dict[str, Any] = {}
        self._cache_ts: dict[str, float] = {}

    async def search(self, query: str, limit: int = 20,
                     category: str = "", min_stars: float = 0,
                     min_downloads: int = 0) -> list[dict]:
        """
        Search ClawHub skills.
        Returns list of skill metadata dicts.
        Falls back to demo data if API unavailable.
        """
        params = [f"q={urllib.request.quote(query)}", f"limit={limit}"]
        if category:
            params.append(f"category={category}")
        url = f"{self._api}/skills?{'&'.join(params)}"

        _vlog("🦞", f"ClawHub search: {query}")
        try:
            data = await self._get(url)
        except Exception as exc:
            _vlog("⚠️", f"ClawHub API unavailable: {str(exc)[:60]} — using demo data")
            return self._demo_search(query, limit)

        skills = data.get("skills", data.get("results", []))
        if not isinstance(skills, list):
            skills = []

        # Client-side filter
        filtered = []
        for s in skills:
            stars = s.get("stars", s.get("rating", 0))
            downloads = s.get("downloads", s.get("download_count", 0))
            if stars >= min_stars and downloads >= min_downloads:
                filtered.append(self._normalize_skill(s))

        _vlog("🦞", f"Found {len(filtered)} skills (filtered from {len(skills)})")
        return filtered if filtered else self._demo_search(query, limit)

    async def get_skill_detail(self, name: str) -> dict | None:
        """Get full detail of a specific skill."""
        url = f"{self._api}/skills/{urllib.request.quote(name)}"
        try:
            data = await self._get(url)
            return self._normalize_skill(data)
        except Exception as exc:
            _vlog("⚠️", f"Skill detail error: {exc}")
            return None

    async def download_skill(self, name: str, version: str = "latest") -> str | None:
        """
        Download skill package to quarantine directory.
        Returns path to quarantine folder, or None on failure.

        Falls back to generating a local demo package if server unreachable.
        """
        _vlog("📥", f"Downloading: {name}@{version}")

        # Create quarantine folder
        qdir = _QUARANTINE_DIR / f"{name}_{int(time.time())}"
        qdir.mkdir(parents=True, exist_ok=True)

        # ── Try 1: Download from ClawHub server ───────────────
        try:
            url = f"{self._api}/skills/{urllib.request.quote(name)}/versions/{version}/download"
            zip_path = qdir / f"{name}.zip"

            req = urllib.request.Request(url, headers={"User-Agent": "Phidipus/9.40"})
            def _dl():
                with urllib.request.urlopen(req, timeout=15) as resp:
                    zip_path.write_bytes(resp.read())

            await asyncio.get_event_loop().run_in_executor(None, _dl)

            if zip_path.exists() and zip_path.stat().st_size > 0:
                with zipfile.ZipFile(str(zip_path), 'r') as zf:
                    zf.extractall(str(qdir))
                zip_path.unlink()
                _vlog("📥", f"Downloaded to quarantine: {qdir}")
                return str(qdir)

        except Exception as exc:
            _vlog("⚠️", f"Server download failed: {str(exc)[:60]}")

        # ── Try 2: Fetch SKILL.md directly ────────────────────
        try:
            md_url = f"{self._api}/skills/{urllib.request.quote(name)}/skill-md"
            md_data = await self._get(md_url)
            if md_data:
                md_text = md_data.get("content", md_data.get("skill_md", ""))
                if md_text:
                    (qdir / "SKILL.md").write_text(md_text, encoding="utf-8")
                    _vlog("📥", f"Downloaded SKILL.md to: {qdir}")
                    return str(qdir)
        except Exception:
            pass

        # ── Try 3: Generate local demo package (offline mode) ─
        demo = self._generate_demo_package(name, version, qdir)
        if demo:
            return demo

        # Cleanup on failure
        shutil.rmtree(str(qdir), ignore_errors=True)
        return None

    def _generate_demo_package(self, name: str, version: str, qdir: Path) -> str | None:
        """
        Generate a local demo skill package when server is unreachable.

        Creates SKILL.md + main.py from demo metadata.
        This allows the full OpenClaw flow (download → scan → approve → install)
        to work offline for testing and demonstration.
        """
        # Find demo metadata
        demos = self._demo_search("", 50)
        meta = next((d for d in demos if d.get("name") == name), None)
        if not meta:
            meta = {"name": name, "description": f"Community skill: {name}",
                    "author": "community", "version": version, "category": "automation",
                    "emoji": "📦"}

        desc = meta.get("description", name)
        author = meta.get("author", "community")
        category = meta.get("category", "automation")
        emoji = meta.get("emoji", "📦")

        # Generate SKILL.md
        skill_md = f"""---
name: {name}
version: {version}
author: {author}
category: {category}
license: MIT
---

# {emoji} {name}

{desc}

## Overview

This skill automates: **{desc.lower()}**.

Installed via OpenClaw Store (offline demo package).

## Usage

When user asks to:
- "{name.replace('-', ' ')}"
- "run {name}"
- "{desc.lower()[:50]}"

The skill will execute the automation using Phidipus's AI pipeline.

## Configuration

No additional configuration required. The skill uses Phidipus's
built-in LLM and automation capabilities.

## Requirements

- Phidipus v2.4+
- Ollama with qwen3:8b (or any supported model)

## Examples

```
# Via Telegram:
/task {name.replace('-', ' ')}

# Via Admin Panel:
Type "{name.replace('-', ' ')}" in task input
```
"""
        # Generate main.py
        main_py = f'''"""
{name} — OpenClaw Skill (offline demo)
Author: {author}
Category: {category}
"""
import asyncio

SKILL_NAME = "{name}"
SKILL_DESC = """{desc}"""

async def run(goal: str, context: dict = None) -> dict:
    """Execute skill with Phidipus AI pipeline."""
    context = context or {{}}
    try:
        from core.llm_client import get_llm_client
        llm = get_llm_client()
        prompt = (
            f"You are a specialized AI skill called '{name}'.\\n"
            f"Skill description: {desc}\\n\\n"
            f"User request: {{goal}}\\n\\n"
            f"Execute the skill and provide a helpful response."
        )
        response = await llm.complete(prompt, max_tokens=500)
        return {{"success": True, "result": response, "skill": SKILL_NAME}}
    except Exception as exc:
        return {{"success": False, "error": str(exc), "skill": SKILL_NAME}}

def get_info() -> dict:
    return {{"name": SKILL_NAME, "description": SKILL_DESC, "type": "openclaw"}}
'''
        # Generate metadata.json
        metadata = {
            "name": name,
            "version": version,
            "author": author,
            "description": desc,
            "category": category,
            "emoji": emoji,
            "source": "openclaw_demo",
            "offline_generated": True,
        }

        try:
            (qdir / "SKILL.md").write_text(skill_md, encoding="utf-8")
            (qdir / "main.py").write_text(main_py, encoding="utf-8")
            (qdir / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            _vlog("📦", f"Generated demo package: {name}@{version} → {qdir}")
            return str(qdir)
        except Exception as exc:
            _vlog("❌", f"Demo package generation failed: {exc}")
            return None

    async def get_trending(self, limit: int = 10) -> list[dict]:
        """Get trending/popular skills."""
        url = f"{self._api}/skills?sort=downloads&limit={limit}"
        try:
            data = await self._get(url)
            skills = data.get("skills", data.get("results", []))
            result = [self._normalize_skill(s) for s in (skills if isinstance(skills, list) else [])]
            return result if result else self._demo_trending(limit)
        except Exception:
            return self._demo_trending(limit)

    def _demo_trending(self, limit: int = 10) -> list[dict]:
        """Demo skills when ClawHub API is unavailable."""
        return self._demo_search("", limit)

    def _demo_search(self, query: str, limit: int = 20) -> list[dict]:
        """Fallback demo skills for offline/demo mode."""
        demos = [
            {"name":"auto-email-responder","description":"AI auto-respond to emails based on rules","author":"clawhub","version":"2.1.0","stars":4.8,"downloads":12500,"category":"productivity","emoji":"📧"},
            {"name":"smart-screenshot","description":"Capture, annotate, and share screenshots with AI description","author":"devtools","version":"1.5.0","stars":4.6,"downloads":8900,"category":"automation","emoji":"📸"},
            {"name":"pdf-summarizer","description":"AI-powered PDF document summarization and Q&A","author":"docai","version":"3.0.1","stars":4.9,"downloads":15200,"category":"productivity","emoji":"📄"},
            {"name":"social-scheduler","description":"Schedule and auto-post to multiple social platforms","author":"socialkit","version":"2.3.0","stars":4.7,"downloads":11000,"category":"social","emoji":"📱"},
            {"name":"code-reviewer","description":"AI code review with security analysis and suggestions","author":"devtools","version":"1.8.0","stars":4.5,"downloads":7600,"category":"devops","emoji":"🔍"},
            {"name":"web-scraper-pro","description":"Intelligent web scraping with anti-detection and data extraction","author":"datalab","version":"2.0.0","stars":4.4,"downloads":9800,"category":"automation","emoji":"🕷️"},
            {"name":"voice-notes","description":"Convert voice recordings to structured notes with AI","author":"audioai","version":"1.2.0","stars":4.3,"downloads":5400,"category":"productivity","emoji":"🎤"},
            {"name":"expense-tracker","description":"Auto-categorize expenses from receipts and bank statements","author":"fintools","version":"1.6.0","stars":4.6,"downloads":8100,"category":"finance","emoji":"💰"},
            {"name":"meeting-assistant","description":"Auto-transcribe meetings, extract action items, send summaries","author":"worktools","version":"2.5.0","stars":4.8,"downloads":13700,"category":"productivity","emoji":"📝"},
            {"name":"image-optimizer","description":"Batch optimize, resize, convert images with AI enhancement","author":"mediakit","version":"1.4.0","stars":4.2,"downloads":6200,"category":"automation","emoji":"🖼️"},
            {"name":"git-automator","description":"Smart git workflows: auto-commit, PR creation, branch management","author":"devtools","version":"1.9.0","stars":4.7,"downloads":10500,"category":"devops","emoji":"🔧"},
            {"name":"backup-scheduler","description":"Automated file backup with encryption and cloud sync","author":"sysadmin","version":"2.1.0","stars":4.5,"downloads":7800,"category":"automation","emoji":"💾"},
        ]
        q = query.lower()
        if q:
            demos = [d for d in demos if q in d["name"] or q in d["description"].lower() or q in str(d.get("category",""))]
        return demos[:limit]

    async def get_categories(self) -> list[str]:
        """Get available skill categories."""
        try:
            data = await self._get(f"{self._api}/categories")
            return data.get("categories", [])
        except Exception:
            return ["productivity", "devops", "automation", "search",
                    "social", "home", "finance", "writing", "code"]

    # ── Internal ──────────────────────────────────────────────

    async def _get(self, url: str) -> dict:
        """HTTP GET with caching."""
        # Cache for 5 minutes
        if url in self._cache and time.time() - self._cache_ts.get(url, 0) < 300:
            return self._cache[url]

        req = urllib.request.Request(url, headers={
            "User-Agent": "Phidipus/9.40",
            "Accept": "application/json",
        })

        def _fetch():
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode())

        data = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        self._cache[url] = data
        self._cache_ts[url] = time.time()
        return data

    def _normalize_skill(self, raw: dict) -> dict:
        """Normalize different API response formats into consistent dict."""
        return {
            "name": raw.get("name", raw.get("slug", "")),
            "description": raw.get("description", raw.get("desc", "")),
            "author": raw.get("author", raw.get("owner", "")),
            "version": raw.get("version", raw.get("latest_version", "1.0.0")),
            "stars": raw.get("stars", raw.get("rating", 0)),
            "downloads": raw.get("downloads", raw.get("download_count", 0)),
            "category": raw.get("category", raw.get("tags", ["other"])),
            "created_at": raw.get("created_at", ""),
            "updated_at": raw.get("updated_at", ""),
            "has_virustotal": raw.get("has_virustotal", False),
            "vt_clean": raw.get("vt_clean", None),
            "source_url": raw.get("source_url", raw.get("repository", "")),
            "emoji": raw.get("emoji", "📦"),
        }


# ══════════════════════════════════════════════════════════════
# Installed skills management
# ══════════════════════════════════════════════════════════════

def get_installed_skills() -> list[dict]:
    """List all installed OpenClaw skills."""
    reg = _load_registry()
    return list(reg.get("installed", {}).values())

def get_pending_skills() -> list[dict]:
    """List skills pending review."""
    reg = _load_registry()
    return list(reg.get("pending", {}).values())

def mark_skill_installed(name: str, metadata: dict, security_result: dict):
    """Mark skill as installed after passing security review."""
    reg = _load_registry()
    reg["installed"][name] = {
        **metadata,
        "installed_at": time.time(),
        "security_score": security_result.get("score", 0),
        "gates_passed": security_result.get("gates_passed", []),
        "enabled": True,
    }
    # Remove from pending
    reg.get("pending", {}).pop(name, None)
    _save_registry(reg)

def mark_skill_rejected(name: str, reason: str):
    """Mark skill as rejected."""
    reg = _load_registry()
    reg["rejected"][name] = {
        "name": name,
        "rejected_at": time.time(),
        "reason": reason,
    }
    reg.get("pending", {}).pop(name, None)
    _save_registry(reg)

def mark_skill_pending(name: str, metadata: dict, quarantine_path: str):
    """Mark skill as pending review."""
    reg = _load_registry()
    reg["pending"][name] = {
        **metadata,
        "quarantine_path": quarantine_path,
        "queued_at": time.time(),
    }
    _save_registry(reg)

def uninstall_skill(name: str):
    """Remove installed skill."""
    reg = _load_registry()
    reg.get("installed", {}).pop(name, None)
    _save_registry(reg)
    # Remove files
    skill_dir = _INSTALLED_DIR / name
    if skill_dir.exists():
        shutil.rmtree(str(skill_dir), ignore_errors=True)

def toggle_skill(name: str, enabled: bool):
    """Enable/disable installed skill."""
    reg = _load_registry()
    if name in reg.get("installed", {}):
        reg["installed"][name]["enabled"] = enabled
        _save_registry(reg)


# Singleton
_client: Optional[ClawHubClient] = None

def get_clawhub_client() -> ClawHubClient:
    global _client
    if _client is None:
        _client = ClawHubClient()
    return _client
