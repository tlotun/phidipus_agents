# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
config/config_loader.py — Phidipus v1.0.1
Configuration loader: reads JSON or YAML, merges defaults, validates schema,
enforces security invariants, and exposes typed accessor properties.

Load order:
  1. Read the config file (JSON or YAML; stdlib json, or PyYAML if available).
  2. Deep-merge with DEFAULTS so every field is populated.
  3. Validate the merged dict against CONFIG_SCHEMA (via json_utils.validate).
  4. Run security invariant checks (check_security_invariants).
  5. Reject if any invariant is violated — fail fast.

Security decisions:
  - Files are opened read-only (os.O_RDONLY) so even a buggy open() call
    cannot create or truncate the config.
  - No eval(), exec(), or importlib — YAML is loaded with yaml.safe_load()
    only (never yaml.load() with a full Loader).
  - Unknown top-level keys cause a hard error (additionalProperties: false).
  - NaN / Infinity are rejected by json_utils.loads().
  - Path values are stored as strings; callers must resolve them via file_ops.
  - The loader is stateless after construction: the resolved config dict is
    frozen into a private attribute at construction time.
  - No singleton / global state — callers create PhidipusConfig instances
    directly; tests can construct them without touching the filesystem.

Patch v9.11.1:
  FIX-3  Added cpu_fraction_to_quota() to convert the config-layer CPU
         fraction (0.05–1.0) to Docker CFS quota microseconds (integer).
         config/schema.py defines sandbox.cpu_quota as a float fraction for
         human-readability, but DockerSandbox and SandboxManagerConfig expect
         an integer in µs.  Without this conversion, a config value of 0.5
         would be passed as ``int(0.5) == 0`` microseconds to Docker,
         producing zero CPU allocation.  _SandboxCfg.cpu_quota now returns
         int (µs) instead of float (fraction).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from utils.json_utils import loads, validate, JsonDecodeError, JsonSchemaError
from utils.logger import get_logger
from config.schema import CONFIG_SCHEMA, DEFAULTS, check_security_invariants

_log = get_logger("config.config_loader", process="phidipus")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """Raised when the configuration is invalid or fails a security check."""


# ---------------------------------------------------------------------------
# CPU quota unit conversion (FIX-3)
# ---------------------------------------------------------------------------

def cpu_fraction_to_quota(fraction: float, period: int = 100_000) -> int:
    """
    Convert a CPU fraction (0.05–1.0) to Docker CFS quota microseconds.

    Docker's ``--cpu-quota`` is expressed in µs per ``--cpu-period``
    (default 100 ms = 100,000 µs).  The config layer stores CPU allocation
    as a human-readable fraction (e.g. 0.5 = 50 % of one core) to avoid
    exposing an obscure kernel scheduling unit to operators.  This function
    converts that fraction to the integer µs value that DockerSandbox and
    SandboxManagerConfig expect.

    Examples::

        cpu_fraction_to_quota(0.5)           # → 50_000
        cpu_fraction_to_quota(1.0)           # → 100_000
        cpu_fraction_to_quota(0.5, 200_000)  # → 100_000 (2× period)

    Args:
        fraction: CPU fraction from the config file (0.05–1.0, inclusive).
        period:   CFS period in µs.  Must match the ``--cpu-period`` passed
                  to Docker (default 100_000 µs = 100 ms).

    Returns:
        CFS quota in µs as a non-negative integer.  The minimum returned
        value is 1_000 µs (1 ms) to avoid a quota of 0 which Docker
        interprets as «no limit».

    Raises:
        ValueError: if *fraction* is not in the range (0.0, 1.0].
    """
    if not (0.0 < fraction <= 1.0):
        raise ValueError(
            f"cpu_quota fraction must be in (0.0, 1.0], got {fraction!r}"
        )
    return max(1_000, int(fraction * period))


# ---------------------------------------------------------------------------
# YAML loader (optional)
# ---------------------------------------------------------------------------

def _try_load_yaml(text: str) -> dict[str, Any]:
    """
    Parse *text* as YAML using yaml.safe_load().

    safe_load() forbids Python-specific tags (!!python/object, etc.) and
    cannot execute arbitrary code.  This is the ONLY permitted YAML loader.

    Raises:
        ImportError:  if PyYAML is not installed.
        ConfigError:  if the YAML is invalid or does not produce a dict.
    """
    try:
        import yaml  # PyYAML — optional dependency
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required to load YAML config files. "
            "Install it with: pip install pyyaml"
        ) from exc

    try:
        result = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error: {exc}") from exc

    if not isinstance(result, dict):
        raise ConfigError(
            f"Config file must contain a YAML mapping at the top level, "
            f"got {type(result).__name__!r}"
        )
    return result


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Return a new dict that is *base* deep-merged with *override*.

    For keys that exist in both and whose values are both dicts, the merge
    recurses.  For all other types, *override* wins.

    Neither *base* nor *override* is mutated.
    """
    result: dict[str, Any] = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

class PhidipusConfig:
    """
    Immutable, validated Phidipus v1.0 configuration object.

    Typical usage::

        cfg = PhidipusConfig.from_file("./phidipus.json")
        cfg = PhidipusConfig.from_file("./phidipus.yaml")
        cfg = PhidipusConfig.from_dict({"process": {"name": "daemon"}, ...})

    Access typed values via properties::

        cfg.ipc.socket_path
        cfg.sandbox.execution_timeout_seconds
        cfg.sandbox.cpu_quota          # int µs (converted from fraction)
        cfg.vision.confidence_threshold
        cfg.agent.max_steps

    Or access the raw dict::

        cfg.raw["memory"]["episodic_max_episodes"]
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        """
        Construct a PhidipusConfig from a raw (already merged + validated) dict.

        Prefer :meth:`from_file` or :meth:`from_dict` over calling this
        constructor directly.
        """
        self._raw: dict[str, Any] = copy.deepcopy(raw)

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_file(cls, path: str | Path) -> "PhidipusConfig":
        """
        Load configuration from a JSON or YAML file at *path*.

        The file format is determined by extension:
          .json  → JSON (stdlib)
          .yaml / .yml → YAML (PyYAML safe_load)

        The file is opened read-only; the loader cannot create or modify it.

        Args:
            path: Absolute or relative path to the config file.

        Returns:
            Validated PhidipusConfig instance.

        Raises:
            ConfigError: if the file cannot be read, parsed, or validated.
            FileNotFoundError: if the file does not exist.
        """
        path = Path(path)
        suffix = path.suffix.lower()

        # Open read-only to prevent accidental creation/truncation.
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ConfigError(f"Cannot open config file {path}: {exc}") from exc

        try:
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise ConfigError(f"Cannot read config file {path}: {exc}") from exc

        if suffix == ".json":
            try:
                user_cfg = loads(text)
            except JsonDecodeError as exc:
                raise ConfigError(f"JSON parse error in {path}: {exc}") from exc
            if not isinstance(user_cfg, dict):
                raise ConfigError(
                    f"Config file {path} must contain a JSON object at the "
                    f"top level, got {type(user_cfg).__name__!r}"
                )
        elif suffix in (".yaml", ".yml"):
            user_cfg = _try_load_yaml(text)
        else:
            raise ConfigError(
                f"Unsupported config file extension {suffix!r} for {path}. "
                "Use .json, .yaml, or .yml"
            )

        _log.info("Config file loaded", extra={"path": str(path), "format": suffix})
        return cls.from_dict(user_cfg)

    @classmethod
    def from_dict(cls, user_cfg: dict[str, Any]) -> "PhidipusConfig":
        """
        Construct a PhidipusConfig from a plain dict.

        Steps:
          1. Deep-merge *user_cfg* over DEFAULTS.
          2. Validate merged dict against CONFIG_SCHEMA.
          3. Enforce security invariants.
          4. Return the validated instance.

        Args:
            user_cfg: Partial or complete configuration dict.

        Returns:
            Validated PhidipusConfig instance.

        Raises:
            ConfigError: on schema or invariant violation.
        """
        if not isinstance(user_cfg, dict):
            raise ConfigError(
                f"Configuration must be a dict, got {type(user_cfg).__name__!r}"
            )

        merged = _deep_merge(DEFAULTS, user_cfg)

        # ── Strip runtime-only top-level keys before schema validation ──
        # These keys are written by the Admin Panel / runtime at startup
        # (e.g. custom_providers added via Ollama/Custom API panel).
        # They are NOT part of the static CONFIG_SCHEMA, so we save them
        # separately and restore after validation to avoid ConfigError.
        # FIX v4.3: admin_auth (documented way to set Admin Panel credentials)
        # used to raise ConfigError because it was not listed here.
        _RUNTIME_KEYS = {
            "custom_providers", "company_name", "telegram",
            "admin_auth", "memory_agent", "models", "privacy", "license",
            "workflow_variables", "brain", "admin",
        }
        _saved_runtime = {k: merged.pop(k) for k in _RUNTIME_KEYS if k in merged}

        # Schema validation
        try:
            validate(merged, CONFIG_SCHEMA)
        except JsonSchemaError as exc:
            raise ConfigError(f"Configuration schema violation: {exc}") from exc

        # Restore runtime-only keys after validation (so they remain accessible)
        merged.update(_saved_runtime)

        # Security invariant post-validation
        violations = check_security_invariants(merged)
        if violations:
            detail = "\n  ".join(violations)
            raise ConfigError(
                f"Configuration fails security invariant checks:\n  {detail}"
            )

        _log.debug(
            "Config validated",
            extra={
                "process_name": merged["process"]["name"],
                "violations": 0,
            },
        )
        return cls(merged)

    @classmethod
    def defaults(cls) -> "PhidipusConfig":
        """
        Return a config built entirely from defaults (useful in tests).
        """
        return cls.from_dict({})

    # ── Raw access ───────────────────────────────────────────────────────────

    @property
    def raw(self) -> dict[str, Any]:
        """Return a deep copy of the full configuration dict."""
        return copy.deepcopy(self._raw)

    def get(self, *keys: str, default: Any = None) -> Any:
        """
        Retrieve a nested config value by key path.

        Usage::

            cfg.get("sandbox", "execution_timeout_seconds")
            cfg.get("vision", "confidence_threshold")

        Returns *default* if the key path does not exist.
        """
        node: Any = self._raw
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # ── Typed section accessors ───────────────────────────────────────────────

    @property
    def process(self) -> "_ProcessCfg":
        return _ProcessCfg(self._raw["process"])

    @property
    def paths(self) -> "_PathsCfg":
        return _PathsCfg(self._raw["paths"])

    @property
    def ipc(self) -> "_IpcCfg":
        return _IpcCfg(self._raw["ipc"])

    @property
    def agent(self) -> "_AgentCfg":
        return _AgentCfg(self._raw["agent"])

    @property
    def llm(self) -> "_LlmCfg":
        return _LlmCfg(self._raw["llm"])

    @property
    def sandbox(self) -> "_SandboxCfg":
        return _SandboxCfg(self._raw["sandbox"])

    @property
    def skill_validator(self) -> "_SkillValidatorCfg":
        return _SkillValidatorCfg(self._raw["skill_validator"])

    @property
    def memory(self) -> "_MemoryCfg":
        return _MemoryCfg(self._raw["memory"])

    @property
    def vision(self) -> "_VisionCfg":
        return _VisionCfg(self._raw["vision"])

    @property
    def evolution(self) -> "_EvolutionCfg":
        return _EvolutionCfg(self._raw["evolution"])

    @property
    def patcher(self) -> "_PatcherCfg":
        return _PatcherCfg(self._raw["patcher"])

    @property
    def skill_forge(self) -> "_SkillForgeCfg":
        return _SkillForgeCfg(self._raw.get("skill_forge", {}))

    @property
    def spider_hub(self) -> "_SpiderHubCfg":
        """Spider Hub multi-profile config. Returns empty hive if section absent."""
        raw = self._cfg.get("spider_hub", {})
        return _SpiderHubCfg(raw)

    @property
    def logging(self) -> "_LoggingCfg":
        return _LoggingCfg(self._raw["logging"])

    @property
    def resource_guard(self) -> "_ResourceGuardCfg":
        return _ResourceGuardCfg(self._raw.get("resource_guard", {}))

    def __repr__(self) -> str:
        return (
            f"PhidipusConfig(process={self.process.name!r}, "
            f"ipc_socket={self.ipc.socket_path!r})"
        )


# ---------------------------------------------------------------------------
# Typed section views
# ---------------------------------------------------------------------------
# Each _*Cfg class is a lightweight read-only view over a section dict.
# They provide IDE-friendly named attributes with correct type annotations.
# They hold a reference to the original section dict — not a copy — because
# the parent PhidipusConfig already owns a private deep copy.

class _SectionView:
    __slots__ = ("_d",)

    def __init__(self, d: dict[str, Any]) -> None:
        object.__setattr__(self, "_d", d)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Config sections are read-only")

    def _get(self, key: str) -> Any:
        return object.__getattribute__(self, "_d")[key]

    def _getopt(self, key: str, default: Any = None) -> Any:
        return object.__getattribute__(self, "_d").get(key, default)


class _ProcessCfg(_SectionView):
    @property
    def name(self) -> str:
        return self._get("name")

    @property
    def instance_id(self) -> str:
        return self._getopt("instance_id", "default")


class _PathsCfg(_SectionView):
    @property
    def data_dir(self) -> str:
        return self._get("data_dir")

    @property
    def skills_dir(self) -> str:
        return self._get("skills_dir")

    @property
    def keys_dir(self) -> str:
        return self._get("keys_dir")

    @property
    def skill_versions_file(self) -> str:
        return self._getopt("skill_versions_file", "./data/skill_versions.json")

    @property
    def patch_ledger_file(self) -> str:
        return self._getopt("patch_ledger_file", "./data/patch_ledger.jsonl")

    @property
    def sha256_manifest_file(self) -> str:
        return self._getopt("sha256_manifest_file", "./data/sha256_manifest.json")


class _IpcCfg(_SectionView):
    @property
    def socket_path(self) -> str:
        return self._get("socket_path")

    @property
    def max_message_bytes(self) -> int:
        return self._getopt("max_message_bytes", 16384)

    @property
    def backlog(self) -> int:
        return self._getopt("backlog", 8)

    @property
    def recv_timeout_seconds(self) -> float:
        return self._getopt("recv_timeout_seconds", 5.0)

    @property
    def rate_limit_per_second(self) -> int:
        return self._getopt("rate_limit_per_second", 50)


class _AgentCfg(_SectionView):
    @property
    def max_steps(self) -> int:
        return self._get("max_steps")

    @property
    def goal_max_length(self) -> int:
        return self._get("goal_max_length")

    @property
    def task_timeout_seconds(self) -> float:
        return self._getopt("task_timeout_seconds", 300.0)

    @property
    def monitor_interval_seconds(self) -> float:
        return self._getopt("monitor_interval_seconds", 5.0)


class _LlmCfg(_SectionView):
    @property
    def base_url(self) -> str:
        return self._get("base_url")

    @property
    def reasoning_model(self) -> str:
        return self._get("reasoning_model")

    @property
    def coder_model(self) -> str:
        return self._get("coder_model")

    @property
    def vlm_model(self) -> str:
        return self._getopt("vlm_model", "qwen3-vl:8b-instruct")

    @property
    def default_system_prompt(self) -> str:
        return self._getopt(
            "default_system_prompt",
            "You are a precise OS agent. Think step-by-step but keep it concise. Use tools only when necessary.",
        )

    @property
    def request_timeout_seconds(self) -> float:
        return self._getopt("request_timeout_seconds", 120.0)

    @property
    def max_tokens(self) -> int:
        return self._getopt("max_tokens", 4096)

    @property
    def temperature(self) -> float:
        return self._getopt("temperature", 0.2)

    @property
    def retry_max_attempts(self) -> int:
        return self._getopt("retry_max_attempts", 3)

    @property
    def retry_base_delay(self) -> float:
        return self._getopt("retry_base_delay", 0.5)


class _SandboxCfg(_SectionView):
    @property
    def docker_image(self) -> str:
        return self._get("docker_image")

    @property
    def seccomp_profile_path(self) -> str:
        return self._get("seccomp_profile_path")

    @property
    def network_mode(self) -> str:
        return self._getopt("network_mode", "none")

    @property
    def execution_timeout_seconds(self) -> float:
        return self._getopt("execution_timeout_seconds", 30.0)

    @property
    def pool_size(self) -> int:
        return self._getopt("pool_size", 2)

    @property
    def max_stdout_bytes(self) -> int:
        return self._getopt("max_stdout_bytes", 65536)

    @property
    def memory_limit(self) -> str:
        return self._getopt("memory_limit", "256m")

    @property
    def cpu_quota(self) -> int:
        """
        CPU quota as Docker CFS microseconds.

        Patch v9.11.1 / FIX-3:
            The config schema stores ``sandbox.cpu_quota`` as a float fraction
            (0.05–1.0) for human readability, but DockerSandbox and
            SandboxManagerConfig require an integer in µs.  This property
            converts the stored fraction using the module-level
            ``cpu_fraction_to_quota()`` function with the standard 100 ms CFS
            period (100_000 µs).

            Before this fix, a fraction of 0.5 would be silently truncated to
            ``int(0.5) == 0`` µs by any caller that cast the float to int,
            effectively granting unlimited CPU to every container.
        """
        fraction = self._getopt("cpu_quota", 0.5)
        return cpu_fraction_to_quota(fraction)



# ---------------------------------------------------------------------------
# Spider Hub profile accessor
# ---------------------------------------------------------------------------

class _SpiderHubProfileCfg:
    """Typed accessor cho một profile trong spider_hub.profiles."""

    def __init__(self, raw: dict, hive_settings: dict) -> None:
        self._r  = raw
        self._hs = hive_settings

    @property
    def name(self) -> str:
        return self._r.get("name", "")

    @property
    def description(self) -> str:
        return self._r.get("description", "")

    @property
    def active(self) -> bool:
        return bool(self._r.get("active", True))

    @property
    def chrome_dir(self) -> str:
        """Chrome profile directory name (defaults to profile name)."""
        return self._r.get("chrome_dir", "") or self.name

    @property
    def platforms(self) -> list[str]:
        """Platforms to post to — profile override or hive default."""
        explicit = self._r.get("platforms")
        if explicit is not None:
            return list(explicit)
        return list(self._hs.get("default_platforms", ["facebook"]))

    @property
    def facebook_url(self) -> str:
        return self._r.get("facebook_url", "https://www.facebook.com")

    @property
    def instagram_url(self) -> str:
        return self._r.get("instagram_url", "https://www.instagram.com")

    @property
    def x_url(self) -> str:
        return self._r.get("x_url", "https://x.com")

    @property
    def content_lang(self) -> str:
        return self._r.get("content_lang", "") or self._hs.get("default_content_lang", "vi")

    @property
    def post_delay_s(self) -> float:
        return float(self._r.get("post_delay_s", 0))

    @property
    def tags(self) -> list[str]:
        return list(self._r.get("tags", []))

    def to_dict(self) -> dict:
        return {
            "name":          self.name,
            "description":   self.description,
            "active":        self.active,
            "chrome_dir":    self.chrome_dir,
            "platforms":     self.platforms,
            "facebook_url":  self.facebook_url,
            "instagram_url": self.instagram_url,
            "x_url":         self.x_url,
            "content_lang":  self.content_lang,
            "post_delay_s":  self.post_delay_s,
            "tags":          self.tags,
        }


class _SpiderHubCfg:
    """Typed accessor cho spider_hub config section."""

    def __init__(self, raw: dict) -> None:
        self._r  = raw
        self._hs = raw.get("hive_settings", {})

    @property
    def max_parallel(self) -> int:
        return int(self._hs.get("max_parallel", 5))

    @property
    def stagger_s(self) -> float:
        return float(self._hs.get("stagger_s", 30))

    @property
    def default_platforms(self) -> list[str]:
        return list(self._hs.get("default_platforms", ["facebook"]))

    @property
    def default_content_lang(self) -> str:
        return self._hs.get("default_content_lang", "vi")

    @property
    def profiles(self) -> list[_SpiderHubProfileCfg]:
        """All profiles (active and inactive)."""
        return [
            _SpiderHubProfileCfg(p, self._hs)
            for p in self._r.get("profiles", [])
        ]

    @property
    def active_profiles(self) -> list[_SpiderHubProfileCfg]:
        """Only profiles with active=True."""
        return [p for p in self.profiles if p.active]

    def get_profile(self, name: str) -> _SpiderHubProfileCfg | None:
        """Find a profile by name (case-insensitive)."""
        name_l = name.lower()
        for p in self.profiles:
            if p.name.lower() == name_l:
                return p
        return None

    def to_dict(self) -> dict:
        return {
            "hive_settings": {
                "max_parallel":        self.max_parallel,
                "stagger_s":           self.stagger_s,
                "default_platforms":   self.default_platforms,
                "default_content_lang": self.default_content_lang,
            },
            "profiles": [p.to_dict() for p in self.profiles],
        }


class _SkillValidatorCfg(_SectionView):
    @property
    def signing_private_key_file(self) -> str:
        return self._get("signing_private_key_file")

    @property
    def signing_public_key_file(self) -> str:
        return self._get("signing_public_key_file")

    @property
    def gate2_timeout_seconds(self) -> float:
        return self._getopt("gate2_timeout_seconds", 20.0)

    @property
    def max_skill_bytes(self) -> int:
        return self._getopt("max_skill_bytes", 65536)

    @property
    def keep_failed_container(self) -> bool:
        return self._getopt("keep_failed_container", False)


class _MemoryCfg(_SectionView):
    @property
    def hmac_key_file(self) -> str:
        return self._get("hmac_key_file")

    @property
    def episodic_max_episodes(self) -> int:
        return self._get("episodic_max_episodes")

    @property
    def vector_max_items(self) -> int:
        return self._get("vector_max_items")

    @property
    def vector_ef_construction(self) -> int:
        return self._getopt("vector_ef_construction", 200)

    @property
    def vector_m(self) -> int:
        return self._getopt("vector_m", 16)

    @property
    def embedding_model(self) -> str:
        return self._getopt("embedding_model", "bge-m3")

    @property
    def episode_namespace(self) -> str:
        return self._getopt("episode_namespace", "primary")


class _VisionCfg(_SectionView):
    @property
    def confidence_threshold(self) -> float:
        return self._get("confidence_threshold")

    @property
    def vlm_timeout_seconds(self) -> float:
        return self._getopt("vlm_timeout_seconds", 90.0)

    @property
    def prefer_accessibility(self) -> bool:
        return self._getopt("prefer_accessibility", True)

    @property
    def iou_dedup_threshold(self) -> float:
        return self._getopt("iou_dedup_threshold", 0.5)

    @property
    def screen_cache_enabled(self) -> bool:
        return self._getopt("screen_cache_enabled", True)

    @property
    def screen_cache_ttl_seconds(self) -> float:
        return self._getopt("screen_cache_ttl_seconds", 2.0)


class _EvolutionCfg(_SectionView):
    @property
    def traceback_max_length(self) -> int:
        return self._get("traceback_max_length")

    @property
    def goal_max_length(self) -> int:
        return self._get("goal_max_length")

    @property
    def mutations_per_cycle(self) -> int:
        return self._getopt("mutations_per_cycle", 3)

    @property
    def min_acceptance_score(self) -> float:
        return self._getopt("min_acceptance_score", 0.6)

    @property
    def quarantine_ttl_seconds(self) -> int:
        return self._getopt("quarantine_ttl_seconds", 3600)


class _PatcherCfg(_SectionView):
    @property
    def rate_limit_per_hour(self) -> int:
        return self._get("rate_limit_per_hour")

    @property
    def rate_limit_persist(self) -> bool:
        return self._get("rate_limit_persist")

    @property
    def ledger_hash_algorithm(self) -> str:
        return self._getopt("ledger_hash_algorithm", "sha256")


class _SkillForgeCfg(_SectionView):
    @property
    def gemini_api_key(self) -> str:
        return self._getopt("gemini_api_key", "")

    @property
    def gemini_model(self) -> str:
        return self._getopt("gemini_model", "gemini-2.5-flash")

    @property
    def skills_dir(self) -> str:
        return self._getopt("skills_dir", "data/skills/forge")

    @property
    def max_retries(self) -> int:
        return self._getopt("max_retries", 2)

    @property
    def enabled(self) -> bool:
        return self._getopt("enabled", True)

    # Fallback stack providers (Phase 4 LLM Fallback Stack)

    @property
    def mistral_api_key(self) -> str:
        return self._getopt("mistral_api_key", "")

    @property
    def cerebras_api_key(self) -> str:
        return self._getopt("cerebras_api_key", "")

    @property
    def openrouter_api_key(self) -> str:
        return self._getopt("openrouter_api_key", "")

    @property
    def ollama_base_url(self) -> str:
        return self._getopt("ollama_base_url", "http://127.0.0.1:11434")

    @property
    def ollama_coder_model(self) -> str:
        return self._getopt("ollama_coder_model", "qwen2.5-coder:7b")

    def as_forge_kwargs(self) -> dict:
        return {
            "gemini_api_key": self.gemini_api_key,
            "gemini_model": self.gemini_model,
            "skills_dir": self.skills_dir,
            "max_retries": self.max_retries,
            "mistral_api_key": self.mistral_api_key,
            "cerebras_api_key": self.cerebras_api_key,
            "openrouter_api_key": self.openrouter_api_key,
            "ollama_base_url": self.ollama_base_url,
            "ollama_coder_model": self.ollama_coder_model,
        }


class _LoggingCfg(_SectionView):
    @property
    def level(self) -> str:
        return self._get("level")

    @property
    def file(self) -> str | None:
        return self._getopt("file", None)


class _ResourceGuardCfg(_SectionView):
    """Typed view for resource_guard config section (Phase 3)."""

    @property
    def enabled(self) -> bool:
        return self._getopt("enabled", True)

    @property
    def max_ram_percent(self) -> float:
        return self._getopt("max_ram_percent", 70.0)

    @property
    def warn_ram_percent(self) -> float:
        return self._getopt("warn_ram_percent", 60.0)

    @property
    def max_cpu_percent(self) -> float:
        return self._getopt("max_cpu_percent", 80.0)

    @property
    def warn_cpu_percent(self) -> float:
        return self._getopt("warn_cpu_percent", 65.0)

    @property
    def max_windows(self) -> int:
        return self._getopt("max_windows", 20)

    @property
    def max_chrome_profiles(self) -> int:
        return self._getopt("max_chrome_profiles", 10)

    @property
    def min_disk_free_gb(self) -> float:
        return self._getopt("min_disk_free_gb", 2.0)

    @property
    def monitor_interval_s(self) -> float:
        return self._getopt("monitor_interval_s", 5.0)

    def as_limits_dict(self) -> dict:
        """Return limits dict for ResourceGuard constructor."""
        return {
            "max_ram_percent":   self.max_ram_percent,
            "warn_ram_percent":  self.warn_ram_percent,
            "max_cpu_percent":   self.max_cpu_percent,
            "warn_cpu_percent":  self.warn_cpu_percent,
            "max_windows":       self.max_windows,
            "max_chrome_profiles": self.max_chrome_profiles,
            "min_disk_free_gb":  self.min_disk_free_gb,
            "monitor_interval_s": self.monitor_interval_s,
        }


# ---------------------------------------------------------------------------
# Convenience loader (FIX v4.3)
# ---------------------------------------------------------------------------
# core/llm_client.get_llm_client() and other fallbacks imported
# ``load_config`` but the function did not exist, so every fallback path
# raised ImportError.  It resolves config.yaml relative to the project root
# (or PHIDIPUS_CONFIG) instead of the current working directory.

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> PhidipusConfig:
    """Load the project config (PHIDIPUS_CONFIG → ./config.yaml → defaults)."""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    env_path = os.environ.get("PHIDIPUS_CONFIG", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates += [_PROJECT_ROOT / "config.yaml", _PROJECT_ROOT / "config.yml"]
    for cand in candidates:
        if cand.exists():
            return PhidipusConfig.from_file(cand)
    return PhidipusConfig.defaults()
