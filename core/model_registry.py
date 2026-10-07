# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/model_registry.py — Phidipus central model-role registry
═══════════════════════════════════════════════════════════════

Problem this solves
-------------------
Model names such as "qwen3:8b" or "qwen3-vl:8b" used to be hard-coded in
~27 files.  When a model was not pulled in Ollama every call failed with
HTTP 404, and editing config.yaml did not help because most call sites
ignored it.

How it works
------------
Every Ollama call asks the registry for a model either by ROLE
(``get_model("reasoning")``) or by the legacy name it used to hard-code
(``resolve_model("qwen3:8b")``).  The registry:

  1. Uses the model configured for that role (config.yaml) when installed.
  2. Otherwise walks a preference list for the role and returns the first
     model that is actually installed in Ollama.
  3. For the "vision" role it can also detect vision-capable models through
     Ollama ``/api/show`` capabilities.
  4. If Ollama is unreachable it returns the configured / requested name
     unchanged, so behaviour degrades gracefully instead of crashing.

Roles
-----
  reasoning — main agent model: planning, ReAct, summarising
  fast      — small model for intent parsing, classification, verification
  coder     — code generation (Skill Forge local fallback, code review)
  vision    — screenshot understanding (VLM)
  heavy     — optional big model for hard multi-step planning
  embed     — text embeddings (memory agent; legacy RAG keeps nomic-embed-text)
  chat      — Telegram chat mode default

v4.3 model generation (verified in the Ollama library, Oct 2026)
---------------------------------------------------------------
Qwen3.5 (0.8b/2b/4b/9b/27b/35b) and Gemma 4 (e2b/e4b/12b/26b-a4b/31b) are
natively multimodal + tool-calling + thinking, so ONE model can serve the
reasoning AND vision roles (less RAM, no model swapping).  Qwen3.6 / Qwen3.8
27B and Qwen3.6 35B-A3B (MoE, ~3B active) cover the heavy role on 32-64 GB
Macs; qwen3-embedding:0.6b / embeddinggemma are the current small multilingual
embedders.  Quantisation variants of a candidate (``qwen3.5:9b-mlx``,
``qwen3.8:27b-q8_0``…) are accepted when the plain tag is not installed.

Config (all optional, config.yaml)::

    llm:
      reasoning_model: qwen3.5:9b
      coder_model: qwen3-coder:30b
      vlm_model: qwen3.5:9b
    memory:
      embedding_model: nomic-embed-text   # legacy RAG / semantic router (768-d)
    models:            # per-role overrides (+ think: false, native_tools: true)
      fast: qwen3.5:4b
      heavy: qwen3.8:27b
      embed: qwen3-embedding:0.6b

Environment overrides: PHIDIPUS_MODEL_<ROLE> (e.g. PHIDIPUS_MODEL_REASONING),
PHIDIPUS_OLLAMA_URL.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parent.parent

# ── Preference lists (first installed wins) ──────────────────────────────
DEFAULT_PREFERENCES: dict[str, list[str]] = {
    "reasoning": ["qwen3.5:9b", "gemma4:12b", "qwen3.6:35b-a3b", "qwen3.5:4b", "gemma4:e4b",
                  "ministral-3:8b", "qwen3:8b", "qwen3:14b", "qwen3:4b", "qwen2.5:7b",
                  "llama3.1:8b", "gemma3:12b"],
    "fast":      ["qwen3.5:4b", "gemma4:e2b", "lfm2.5:8b", "qwen3:4b-instruct", "phi4-mini",
                  "granite4:micro", "qwen3.5:2b", "ministral-3:3b", "qwen3:4b", "qwen2.5:3b",
                  "llama3.2:3b", "qwen3.5:9b", "qwen3:8b"],
    "coder":     ["qwen3-coder:30b", "qwen3.6:27b-coding", "qwen3.6:35b-a3b-coding", "qwen3.5:9b",
                  "qwen2.5-coder:14b", "qwen2.5-coder:7b", "gemma4:12b", "qwen3:8b"],
    "vision":    ["qwen3.5:9b", "gemma4:12b", "qwen3.5:4b", "gemma4:e4b", "qwen3-vl:8b",
                  "ministral-3:8b", "qwen3-vl:4b", "gemma4:e2b", "qwen3-vl:8b-instruct",
                  "qwen2.5vl:7b", "llava:7b", "minicpm-v:8b"],
    "heavy":     ["qwen3.8:27b", "qwen3.6:35b-a3b", "qwen3.6:27b", "gemma4:26b-a4b", "gemma4:31b",
                  "gpt-oss:20b", "qwen3.5:27b", "qwen3:32b", "qwen3.5:9b"],
    "embed":     ["qwen3-embedding:0.6b", "embeddinggemma", "bge-m3", "nomic-embed-text",
                  "mxbai-embed-large"],
    "chat":      ["qwen3.5:9b", "gemma4:12b", "qwen3.5:4b", "gemma4:e4b", "qwen3:8b",
                  "qwen3:4b-instruct", "qwen2.5:7b", "llama3.2:3b"],
}

# Legacy hard-coded names → role (used by resolve_model)
_LEGACY_ROLE: dict[str, str] = {
    "qwen3:8b": "reasoning",
    "qwen3.5:9b": "reasoning",
    "qwen3:14b": "reasoning",
    "qwen3:4b": "fast",
    "qwen3:4b-instruct": "fast",
    "qwen3.5:4b": "fast",
    "qwen2.5-coder:7b": "coder",
    "qwen3-coder:30b": "coder",
    "qwen3-vl:8b": "vision",
    "qwen3-vl:8b-instruct": "vision",
    "qwen3-vl": "vision",
    "nomic-embed-text": "embed",
    "bge-m3": "embed",
}

# config.yaml location of the per-role setting
_CONFIG_PATHS: dict[str, tuple[str, ...]] = {
    "reasoning": ("llm", "reasoning_model"),
    "coder":     ("llm", "coder_model"),
    "vision":    ("llm", "vlm_model"),
    "embed":     ("memory", "embedding_model"),
}

_CACHE_TTL_S = 60.0
_EMBED_HINTS = ("embed", "bge-", "bge:", "e5-", "gte-", "minilm")


def _norm(name: str) -> str:
    """Normalise an Ollama tag: 'foo' and 'foo:latest' are the same model."""
    name = (name or "").strip()
    if name and ":" not in name:
        return f"{name}:latest"
    return name


class ModelRegistry:
    """Thread-safe, cached view of installed Ollama models + role mapping."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installed: set[str] = set()
        self._fetched_at: float = 0.0
        self._reachable: bool = False
        self._config: dict[str, Any] = {}
        self._config_loaded = False
        self._caps_cache: dict[str, tuple[float, frozenset[str]]] = {}
        self._warned: set[str] = set()

    # ── configuration ────────────────────────────────────────────────────
    def configure(self, cfg_raw: dict[str, Any] | None) -> None:
        """Inject the merged config dict (PhidipusConfig.raw)."""
        with self._lock:
            self._config = dict(cfg_raw or {})
            self._config_loaded = True

    def _ensure_config(self) -> None:
        if self._config_loaded:
            return
        try:
            import yaml  # type: ignore
            path = _ROOT / "config.yaml"
            if path.exists():
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    self._config = data
        except Exception:
            pass
        self._config_loaded = True

    def base_url(self) -> str:
        env = os.environ.get("PHIDIPUS_OLLAMA_URL", "").strip()
        if env:
            return env.rstrip("/")
        self._ensure_config()
        url = (self._config.get("llm") or {}).get("base_url") or "http://127.0.0.1:11434"
        return str(url).rstrip("/")

    def configured(self, role: str) -> str:
        """Model explicitly configured for *role* ('' when none)."""
        env = os.environ.get(f"PHIDIPUS_MODEL_{role.upper()}", "").strip()
        if env:
            return env
        self._ensure_config()
        models = self._config.get("models") or {}
        if isinstance(models, dict) and models.get(role):
            return str(models[role])
        path = _CONFIG_PATHS.get(role)
        if path:
            node: Any = self._config
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if node:
                return str(node)
        return ""

    # ── Ollama discovery ─────────────────────────────────────────────────
    def refresh(self, force: bool = False) -> set[str]:
        now = time.time()
        if not force and now - self._fetched_at < _CACHE_TTL_S:
            return self._installed
        try:
            with urllib.request.urlopen(f"{self.base_url()}/api/tags", timeout=2.5) as r:
                data = json.loads(r.read().decode("utf-8"))
            names = {_norm(m.get("name", "")) for m in data.get("models", []) if m.get("name")}
            with self._lock:
                self._installed = names
                self._reachable = True
                self._fetched_at = now
        except Exception:
            with self._lock:
                self._reachable = False
                self._fetched_at = now
        return self._installed

    def installed_models(self) -> set[str]:
        return set(self.refresh())

    def is_reachable(self) -> bool:
        self.refresh()
        return self._reachable

    def is_installed(self, name: str) -> bool:
        return self.installed_variant(name) != ""

    def installed_variant(self, name: str) -> str:
        """Installed tag for *name*: the exact tag, else a quantisation variant
        of the same family/size ("qwen3.8:27b" → "qwen3.8:27b-mlx"). '' if none."""
        installed = self.refresh()
        key = _norm(name)
        if key in installed:
            return name
        if ":" in name and not name.endswith(":latest"):
            variants = sorted(m for m in installed if m.startswith(key + "-"))
            if variants:
                return variants[0]
        return ""

    def capabilities(self, name: str) -> frozenset[str]:
        """Ollama capabilities of *name* (completion, tools, vision, thinking,
        embedding, insert…) from /api/show; cached (failures for 60 s only)."""
        key = _norm(name)
        hit = self._caps_cache.get(key)
        if hit and (hit[1] or time.time() - hit[0] < 60):
            return hit[1]
        caps: frozenset[str] = frozenset()
        try:
            payload = json.dumps({"model": name}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url()}/api/show", data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                info = json.loads(r.read().decode("utf-8"))
            caps = frozenset(str(c) for c in (info.get("capabilities") or []))
        except Exception:
            caps = frozenset()
        self._caps_cache[key] = (time.time(), caps)
        return caps

    def supports(self, name: str, capability: str) -> bool:
        return capability in self.capabilities(name)

    def _supports_vision(self, name: str) -> bool:
        return self.supports(name, "vision")

    # ── resolution ───────────────────────────────────────────────────────
    def candidates(self, role: str) -> list[str]:
        seen: list[str] = []
        for name in [self.configured(role), *DEFAULT_PREFERENCES.get(role, [])]:
            if name and name not in seen:
                seen.append(name)
        return seen

    def get_model(self, role: str, default: str = "") -> str:
        """Best installed model for *role* (never raises)."""
        cands = self.candidates(role)
        if default and default not in cands:
            # configured model (config.yaml / env) first, then the caller's
            # legacy default, then the preference list
            cands.insert(1 if self.configured(role) else 0, default)
        installed = self.refresh()
        if not self._reachable or not installed:
            # Ollama down → return what was asked for; the caller's own error
            # handling reports the connection problem.
            return cands[0] if cands else default
        for name in cands:      # exact tag, else a quantisation variant (…-mlx, …-q8_0)
            variant = self.installed_variant(name)
            if variant:
                return variant
        # Capability-based fallbacks
        if role == "embed":
            for name in sorted(installed):
                if any(h in name for h in _EMBED_HINTS):
                    return name
        elif role == "vision":
            for name in sorted(installed):
                if self._supports_vision(name):
                    return name
        else:
            usable = [n for n in sorted(installed)
                      if not any(h in n for h in _EMBED_HINTS)]
            if usable:
                pick = usable[0]
                self._warn_once(role, f"no preferred '{role}' model installed — using {pick}")
                return pick
        self._warn_once(role, f"no installed model for role '{role}' "
                              f"(tried {', '.join(cands[:4])})")
        return cands[0] if cands else default

    def resolve_model(self, name: str, role: str | None = None) -> str:
        """
        Return *name* if installed, otherwise the best installed model for the
        role *name* used to fill (legacy hard-coded names).
        """
        if not name:
            return self.get_model(role or "reasoning")
        if not self._reachable:
            return name
        variant = self.installed_variant(name)
        if variant:
            return variant
        role = role or _LEGACY_ROLE.get(name) or _LEGACY_ROLE.get(name.split(":")[0])
        if not role:
            return name
        return self.get_model(role, default="")

    def think_default(self) -> bool:
        """config.yaml → models.think (default False: fast, no hidden reasoning)."""
        self._ensure_config()
        models = self._config.get("models") or {}
        return bool(models.get("think", False)) if isinstance(models, dict) else False

    def native_tools_enabled(self) -> bool:
        """config.yaml → models.native_tools (default True)."""
        self._ensure_config()
        models = self._config.get("models") or {}
        return bool(models.get("native_tools", True)) if isinstance(models, dict) else True

    def missing_roles(self, roles: Iterable[str] = ("reasoning", "fast", "vision", "embed")) -> dict[str, str]:
        """Roles whose configured model is not installed → {role: configured}."""
        out: dict[str, str] = {}
        if not self.is_reachable():
            return out
        for role in roles:
            conf = self.configured(role) or (DEFAULT_PREFERENCES.get(role) or [""])[0]
            if conf and not self.is_installed(conf):
                out[role] = conf
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "ollama": self.base_url(),
            "reachable": self.is_reachable(),
            "installed": sorted(self._installed),
            "roles": {r: self.get_model(r) for r in DEFAULT_PREFERENCES},
        }

    def _warn_once(self, key: str, msg: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        print(f"\033[1;33m[MODEL]\033[0m  {msg}")


_REGISTRY = ModelRegistry()


def get_registry() -> ModelRegistry:
    return _REGISTRY


def configure(cfg_raw: dict[str, Any] | None) -> None:
    _REGISTRY.configure(cfg_raw)


def get_model(role: str, default: str = "") -> str:
    return _REGISTRY.get_model(role, default)


def resolve_model(name: str, role: str | None = None) -> str:
    return _REGISTRY.resolve_model(name, role)


def ollama_url() -> str:
    return _REGISTRY.base_url()


def capabilities(name: str) -> frozenset[str]:
    return _REGISTRY.capabilities(name)


def supports(name: str, capability: str) -> bool:
    return _REGISTRY.supports(name, capability)


def config_section(name: str) -> dict[str, Any]:
    """Top-level config.yaml section (e.g. "brain", "memory_agent") as a dict."""
    _REGISTRY._ensure_config()
    sec = _REGISTRY._config.get(name)
    return dict(sec) if isinstance(sec, dict) else {}
