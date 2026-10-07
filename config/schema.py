# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
config/schema.py — Phidipus v1.0.2
Configuration schema definition and default values.

Every section of the schema maps to one subsystem in the IMPLEMENTATION_ORDER.
The schema is used by config_loader.py to:
  1. Validate the loaded config dict against required fields and types.
  2. Fill in defaults so callers always receive a fully-populated config.

Schema format: compatible with utils/json_utils.validate() — a strict subset
of JSON Schema (type, required, properties, additionalProperties, minimum,
maximum, minLength, maxLength, enum, items).

Security invariants baked into defaults:
  R-03  daemon process must not enable the LLM subsystem
  R-12  docker.network_mode defaults to "none"
  R-21  vision.confidence_threshold defaults to 0.8 (never lower)
  R-24  agent.goal_max_length defaults to 2048
  R-25  evolution.traceback_max_length defaults to 1024
  R-19  patcher.rate_limit_persist = True (always disk-backed)

Patch v9.11.2:
  TASK-1  Added R-03 invariant to check_security_invariants(): daemon process
          must not enable the LLM subsystem.  llm.enabled field added to
          CONFIG_SCHEMA and DEFAULTS.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: The complete JSON-Schema-compatible structure for a Phidipus v1.0
#: configuration document.  Every sub-dict uses ``additionalProperties: false``
#: so unknown keys cause a hard validation error rather than silent ignore.
CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "process",
        "paths",
        "ipc",
        "agent",
        "llm",
        "sandbox",
        "skill_validator",
        "memory",
        "vision",
        "evolution",
        "patcher",
        "logging",
    ],
    "properties": {

        # ── process ──────────────────────────────────────────────────────────
        # Identifies which process this config file is for.
        # Used to enforce process-specific import restrictions at startup.
        "process": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {
                    "type": "string",
                    # Strict enum: only known process labels accepted.
                    "enum": ["orchestrator", "daemon", "test"],
                },
                # Optional override for the data directory used in tests.
                "instance_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                },
            },
        },

        # ── paths ─────────────────────────────────────────────────────────────
        # All filesystem paths are declared here so no module hard-codes paths.
        "paths": {
            "type": "object",
            "additionalProperties": False,
            "required": ["data_dir", "skills_dir", "keys_dir"],
            "properties": {
                # Root directory for all runtime data (memory, ledger, logs).
                "data_dir": {"type": "string", "minLength": 1},
                # Directory containing generated skill .py and .sig files.
                "skills_dir": {"type": "string", "minLength": 1},
                # Directory containing ed25519 key files (chmod 600).
                "keys_dir": {"type": "string", "minLength": 1},
                # Optional: override for skill_versions.json location.
                "skill_versions_file": {"type": "string", "minLength": 1},
                # Optional: override for patch ledger file location.
                "patch_ledger_file": {"type": "string", "minLength": 1},
                # SHA256_MANIFEST file path (produced by installer, used by R-20).
                "sha256_manifest_file": {"type": "string", "minLength": 1},
            },
        },

        # ── ipc ───────────────────────────────────────────────────────────────
        # Unix domain socket configuration for L1 ↔ L2 trust boundary.
        "ipc": {
            "type": "object",
            "additionalProperties": False,
            "required": ["socket_path"],
            "properties": {
                # Absolute path to the Unix domain socket.
                "socket_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 104,  # POSIX sun_path limit
                },
                # Maximum bytes for a single IPC message before rejection.
                "max_message_bytes": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 65536,
                },
                # Maximum inbound connections queued before accept().
                "backlog": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 128,
                },
                # Seconds the server waits for a well-formed message before
                # dropping the connection.
                "recv_timeout_seconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 30.0,
                },
                # Maximum IPC actions dispatched per second (rate-limit).
                "rate_limit_per_second": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
        },

        # ── agent ─────────────────────────────────────────────────────────────
        # Orchestrator agent loop settings.
        "agent": {
            "type": "object",
            "additionalProperties": False,
            "required": ["max_steps", "goal_max_length"],
            "properties": {
                # Hard limit on ReAct loop iterations per task (R-24 neighbour).
                # Architecture audit BUG-05 identified the 30-step limit.
                "max_steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                },
                # R-24: maximum allowed goal string length before rejection.
                "goal_max_length": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 2048,
                },
                # Seconds before a task is declared stuck and aborted.
                "task_timeout_seconds": {
                    "type": "number",
                    "minimum": 10.0,
                    "maximum": 3600.0,
                },
                # Seconds between runtime_monitor health-check ticks.
                "monitor_interval_seconds": {
                    "type": "number",
                    "minimum": 1.0,
                    "maximum": 60.0,
                },
            },
        },

        # ── llm ───────────────────────────────────────────────────────────────
        # Ollama / LLM client settings (orchestrator_process only; daemon
        # must never read or use this section — R-03).
        "llm": {
            "type": "object",
            "additionalProperties": False,
            "required": ["base_url", "reasoning_model", "coder_model"],
            "properties": {
                # R-03: explicit on/off flag for the LLM subsystem.
                # Daemon configs MUST set this to false.
                # check_security_invariants() enforces this at load time.
                "enabled": {
                    "type": "boolean",
                },
                # Ollama API base URL (no trailing slash).
                "base_url": {
                    "type": "string",
                    "minLength": 7,    # "http://"
                    "maxLength": 256,
                },
                # Model name for reasoning / planning calls.
                "reasoning_model": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                # Model name for code-generation calls.
                "coder_model": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                # Model name for VLM calls (screen understanding).
                "default_system_prompt": {
                    "type": "string",
                    "default": "You are a precise OS agent. Think step-by-step but keep it concise. Use tools only when necessary.",
                    "description": "System prompt injected vào mọi LLM call. Giữ DeepSeek-R1 không overthink.",
                },
                "vlm_model": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                # Seconds before an LLM request is aborted.
                "request_timeout_seconds": {
                    "type": "number",
                    "minimum": 5.0,
                    "maximum": 300.0,
                },
                # Maximum tokens to request in a single completion.
                "max_tokens": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 32768,
                },
                # Temperature for skill generation (0.0 = deterministic).
                "temperature": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 2.0,
                },
                # Phase 1: retry with exponential backoff.
                # Max number of attempts before giving up on a transient error.
                "retry_max_attempts": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                },
                # Initial delay in seconds before first retry (doubled each attempt).
                "retry_base_delay": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 5.0,
                },
            },
        },

        # ── sandbox ───────────────────────────────────────────────────────────
        # Docker sandbox configuration (L3 process).
        "sandbox": {
            "type": "object",
            "additionalProperties": False,
            "required": ["docker_image", "seccomp_profile_path"],
            "properties": {
                # Docker image used for ephemeral skill execution.
                "docker_image": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                # Absolute path to the seccomp profile JSON.
                "seccomp_profile_path": {
                    "type": "string",
                    "minLength": 1,
                },
                # R-12: network mode — MUST be "none" in production.
                # Validated to be "none" by config_loader post-validation.
                "network_mode": {
                    "type": "string",
                    "enum": ["none"],   # only "none" is permitted in v9.11
                },
                # Seconds before a container execution is killed.
                "execution_timeout_seconds": {
                    "type": "number",
                    "minimum": 1.0,
                    "maximum": 120.0,
                },
                # Number of pre-warmed containers in the pool.
                "pool_size": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 16,
                },
                # Maximum stdout bytes read from a container (DoS guard).
                "max_stdout_bytes": {
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": 1048576,  # 1 MiB
                },
                # Memory limit passed to Docker (e.g. "256m").
                "memory_limit": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 16,
                },
                # CPU quota as a fraction of one core (0.0–1.0).
                "cpu_quota": {
                    "type": "number",
                    "minimum": 0.05,
                    "maximum": 1.0,
                },
            },
        },

        # ── skill_validator ───────────────────────────────────────────────────
        # 3-gate skill validation pipeline settings.
        "skill_validator": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "signing_private_key_file",
                "signing_public_key_file",
            ],
            "properties": {
                # Path to the 32-byte raw ed25519 private key (mode 600).
                "signing_private_key_file": {
                    "type": "string",
                    "minLength": 1,
                },
                # Path to the 32-byte raw ed25519 public key.
                "signing_public_key_file": {
                    "type": "string",
                    "minLength": 1,
                },
                # Gate 2 execution timeout (seconds) — independent of sandbox
                # execution timeout so validation can be stricter.
                "gate2_timeout_seconds": {
                    "type": "number",
                    "minimum": 1.0,
                    "maximum": 60.0,
                },
                # Maximum skill source file size accepted by Gate 1 (bytes).
                "max_skill_bytes": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 524288,  # 512 KiB
                },
                # Whether to keep the Gate 2 container on failure for debug.
                # MUST be false in production.
                "keep_failed_container": {
                    "type": "boolean",
                },
            },
        },

        # ── memory ────────────────────────────────────────────────────────────
        # Memory subsystem settings (episodic, vector, task).
        "memory": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "hmac_key_file",
                "episodic_max_episodes",
                "vector_max_items",
            ],
            "properties": {
                # Path to the 32-byte HMAC key file (mode 600).
                "hmac_key_file": {
                    "type": "string",
                    "minLength": 1,
                },
                # R-16: episodic ring buffer size.
                # Audit identified max 1000 episodes in v9.10.
                "episodic_max_episodes": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 100000,
                },
                # M-4: LRU eviction cap for the HNSW vector index.
                # Audit identified 10,000 vectors in v9.10.
                "vector_max_items": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 1000000,
                },
                # HNSW index construction parameter (higher = better recall,
                # slower build).
                "vector_ef_construction": {
                    "type": "integer",
                    "minimum": 4,
                    "maximum": 2048,
                },
                # HNSW M parameter (number of bi-directional links per node).
                "vector_m": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 128,
                },
                # Embedding model name (must support multilingual input).
                "embedding_model": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                # M-7: episode namespace — which segment this process writes to.
                "episode_namespace": {
                    "type": "string",
                    "enum": ["primary", "guest", "untrusted"],
                },
            },
        },

        # ── vision ────────────────────────────────────────────────────────────
        # VLM perception pipeline settings.
        "vision": {
            "type": "object",
            "additionalProperties": False,
            "required": ["confidence_threshold"],
            "properties": {
                # R-21: minimum VLM detection confidence for automated dispatch.
                # Detections below this threshold block IPC emit.
                # MUST NOT be set below 0.8 — post-validated in config_loader.
                "confidence_threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                # Seconds to wait for VLM response before fallback.
                "vlm_timeout_seconds": {
                    "type": "number",
                    "minimum": 1.0,
                    "maximum": 120.0,
                },
                # Whether to use accessibility API as primary (True) or VLM (False).
                "prefer_accessibility": {
                    "type": "boolean",
                },
                # IoU threshold for deduplicating overlapping VLM detections.
                "iou_dedup_threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                # Phase 1: screen fingerprint cache.
                # Skip VLM call when screen has not changed since last perceive().
                "screen_cache_enabled": {
                    "type": "boolean",
                },
                # Max age of cached VLM result before forced refresh (seconds).
                "screen_cache_ttl_seconds": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 10.0,
                },
                # v2.3: DINO+Florence vision pipeline settings.
                # "dino_primary" = DINO+Florence primary, VLM fallback.
                # "vlm_only"     = legacy VLM-only pipeline (default for backward compat).
                # "hybrid"       = run both, log comparison (A/B testing).
                "vision_backend": {
                    "type": "string",
                    "enum": ["vlm_only", "dino_primary", "hybrid"],
                },
                # Directory for DINO + Florence ONNX models.
                "models_dir": {
                    "type": "string",
                },
                # DINO raw detection confidence floor.
                "dino_conf_threshold": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 0.9,
                },
                # Maximum zoom iterations in Active Vision Loop.
                "max_refine_iterations": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5,
                },
            },
        },

        # ── evolution ─────────────────────────────────────────────────────────
        # Autonomous skill evolution pipeline settings.
        "evolution": {
            "type": "object",
            "additionalProperties": False,
            "required": ["traceback_max_length", "goal_max_length"],
            "properties": {
                # R-25: maximum traceback characters interpolated into LLM prompts.
                "traceback_max_length": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 1024,
                },
                # R-24 echo: maximum goal characters used in mutation prompts.
                "goal_max_length": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 2048,
                },
                # Number of LLM mutations generated per evolution cycle.
                "mutations_per_cycle": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                },
                # Minimum validation score to accept a mutation.
                "min_acceptance_score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                # Alias: minimum score to promote a mutation to active skill.
                "min_score_to_promote": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                # Quarantine TTL in seconds — newly evolved skills start quarantined.
                "quarantine_ttl_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 86400,
                },
            },
        },

        # ── patcher ───────────────────────────────────────────────────────────
        # Runtime self-patching settings.
        "patcher": {
            "type": "object",
            "additionalProperties": False,
            "required": ["rate_limit_per_hour", "rate_limit_persist"],
            "properties": {
                # Maximum patches applied per hour (H-2 / R-19 rate-limit).
                "rate_limit_per_hour": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                },
                # R-19: rate-limit counter MUST be persisted to disk.
                # Must always be true in production.
                "rate_limit_persist": {
                    "type": "boolean",
                },
                # SHA-256 algorithm used for patch-ledger entries (R-18).
                # Informational — code always uses SHA-256; this documents it.
                "ledger_hash_algorithm": {
                    "type": "string",
                    "enum": ["sha256"],
                },
            },
        },

        # ── spider_hub ──────────────────────────────────────────────────────────
        # Multi-profile social media automation ("hive" = colony of Chrome profiles).
        # Optional section — omit entirely to use single-profile mode.
        # /postall reads profiles from here; each profile runs in parallel.
        "spider_hub": {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {
                # ── Hive-level settings ──────────────────────────────────
                "hive_settings": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [],
                    "properties": {
                        # Max profiles running concurrently in /postall
                        "max_parallel": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                        },
                        # Seconds to stagger between profile starts (avoid rate limits)
                        "stagger_s": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 300,
                        },
                        # Default platforms for all profiles (can be overridden per profile)
                        "default_platforms": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["facebook", "instagram", "x"],
                            },
                        },
                        # Content language for LLM generation
                        "default_content_lang": {
                            "type": "string",
                            "enum": ["vi", "en", "auto"],
                        },
                    },
                },
                # ── Profile list ──────────────────────────────────────────
                "profiles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name"],
                        "properties": {
                            # Chrome profile name — matches folder in Chrome data dir
                            "name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                            },
                            # Human-readable description
                            "description": {
                                "type": "string",
                                "maxLength": 200,
                            },
                            # Include in /postall (default: true)
                            "active": {
                                "type": "boolean",
                            },
                            # Chrome profile directory name (auto-detect from name if empty)
                            "chrome_dir": {
                                "type": "string",
                                "maxLength": 128,
                            },
                            # Platforms to post to (overrides hive_settings.default_platforms)
                            "platforms": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["facebook", "instagram", "x"],
                                },
                            },
                            # Account URLs (use defaults if not specified)
                            "facebook_url": {
                                "type": "string",
                                "maxLength": 256,
                            },
                            "instagram_url": {
                                "type": "string",
                                "maxLength": 256,
                            },
                            "x_url": {
                                "type": "string",
                                "maxLength": 256,
                            },
                            # Content generation language for this profile
                            "content_lang": {
                                "type": "string",
                                "enum": ["vi", "en", "auto"],
                            },
                            # Extra delay (seconds) before this profile starts in /postall
                            "post_delay_s": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 600,
                            },
                            # Optional tags for filtering (e.g. "marketing", "personal")
                            "tags": {
                                "type": "array",
                                "items": {"type": "string", "maxLength": 32},
                            },
                        },
                    },
                },
            },
        },

        # ── logging ───────────────────────────────────────────────────────────
        # Structured JSON logger settings.
        "logging": {
            "type": "object",
            "additionalProperties": False,
            "required": ["level"],
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                },
                # Optional path to write log output to a file in addition to stderr.
                "file": {
                    "type": "string",
                    "minLength": 1,
                },
                # Log output format: "json" (structured) or "text" (human-readable).
                "format": {
                    "type": "string",
                    "enum": ["json", "text"],
                },
            },
        },

        # ── resource_guard ─────────────────────────────────────────────────────
        # Resource Awareness: RAM/CPU hard limits (Phase 3).
        # Agent pauses new tasks when resource usage exceeds these thresholds.
        "resource_guard": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "Enable resource monitoring and hard limits.",
                },
                "max_ram_percent": {
                    "type": "number",
                    "minimum": 30.0,
                    "maximum": 95.0,
                    "description": "Pause new tasks when RAM usage exceeds this percent.",
                },
                "warn_ram_percent": {
                    "type": "number",
                    "minimum": 20.0,
                    "maximum": 90.0,
                    "description": "Log warning when RAM usage exceeds this percent.",
                },
                "max_cpu_percent": {
                    "type": "number",
                    "minimum": 30.0,
                    "maximum": 100.0,
                    "description": "Pause new tasks when CPU usage exceeds this percent.",
                },
                "warn_cpu_percent": {
                    "type": "number",
                    "minimum": 20.0,
                    "maximum": 95.0,
                    "description": "Log warning when CPU usage exceeds this percent.",
                },
                "max_windows": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 100,
                    "description": "Maximum open windows before blocking new tasks.",
                },
                "max_chrome_profiles": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum concurrent Chrome windows/profiles.",
                },
                "min_disk_free_gb": {
                    "type": "number",
                    "minimum": 0.5,
                    "description": "Minimum free disk space (GB) before pausing tasks.",
                },
                "monitor_interval_s": {
                    "type": "number",
                    "minimum": 1.0,
                    "maximum": 60.0,
                    "description": "How often to poll resource metrics (seconds).",
                },
            },
        },

        # ── skill_forge (v9.21) ────────────────────────────────────────
        "skill_forge": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "gemini_api_key": {"type": "string"},
                "gemini_model": {"type": "string"},
                "mistral_api_key": {"type": "string"},
                "cerebras_api_key": {"type": "string"},
                "openrouter_api_key": {"type": "string"},
                "ollama_base_url": {"type": "string"},
                "ollama_coder_model": {"type": "string"},
                "skills_dir": {"type": "string"},
                "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
                "enabled": {"type": "boolean"},
            },
        },
    },
}
# ---------------------------------------------------------------------------

#: Default configuration values.  Any key absent from the user-supplied config
#: file is filled in from here by config_loader before validation.
#: Defaults are chosen to be maximally restrictive per the security rules.
DEFAULTS: dict[str, Any] = {
    "process": {
        "name": "orchestrator",
        "instance_id": "default",
    },
    "paths": {
        "data_dir": "./data",
        "skills_dir": "./skills/generated",
        "keys_dir": "./keys",
        "skill_versions_file": "./data/skill_versions.json",
        "patch_ledger_file": "./data/patch_ledger.jsonl",
        "sha256_manifest_file": "./data/sha256_manifest.json",
    },
    "ipc": {
        "socket_path": "/tmp/phidipus_ipc.sock",
        "max_message_bytes": 16384,
        "backlog": 8,
        "recv_timeout_seconds": 5.0,
        "rate_limit_per_second": 50,
    },
    "agent": {
        "max_steps": 30,            # BUG-05: hard limit from audit
        "goal_max_length": 2048,    # R-24
        "task_timeout_seconds": 300.0,
        "monitor_interval_seconds": 5.0,
    },
    "llm": {
        "enabled": True,            # R-03: set to False in daemon config
        "base_url": "http://127.0.0.1:11434",
        # Phase 1: split reasoning vs coder model for 3-5x speed gain.
        # qwen3:8b is 3-4x faster than qwen3:30b, sufficient for planning.
        # qwen2.5-coder:7b is specialised for code, ~20% better than general model.
        "reasoning_model": "qwen3:8b",     # DeepSeek-R1-Distill-Qwen-7B
        "coder_model": "qwen2.5-coder:7b",       # Qwen2.5-Coder-7B (không đổi)
        "default_system_prompt": (
            "You are a precise macOS automation agent. "
            "STRATEGY — always prefer keyboard-driven actions over visual clicking.\n"
            "\n"
            "1. OPEN APPS VIA SPOTLIGHT: Use keyboard_hotkey [cmd,space] to open Spotlight, "
            "keyboard_type the app name, then keyboard_press enter. "
            "This is faster than app_launch or clicking icons.\n"
            "\n"
            "2. CHROME PROFILE PICKER (CRITICAL): Chrome profile picker does NOT have a "
            "visible search box, but it DOES support keyboard filtering. "
            "After Chrome opens and the profile picker appears: "
            "immediately use keyboard_type to type the EXACT profile name (e.g. '00fujin'). "
            "Chrome will filter the list as you type. Then keyboard_press enter to open it. "
            "Do NOT try to click, scroll, or look for a search box — just type directly.\n"
            "\n"
            "3. GENERAL PICKERS/DIALOGS: For any list/picker with many items, "
            "always try keyboard_type first to filter, then keyboard_press enter.\n"
            "\n"
            "4. PREFER KEYBOARD: Use hotkeys (cmd+t, cmd+l, tab, arrow keys, enter) "
            "instead of mouse clicks whenever possible.\n"
            "\n"
            "5. WAIT BEFORE OBSERVE: After launching an app, wait 1-2 seconds for it "
            "to fully load before taking the next action.\n"
            "\n"
            "6. DONE CONDITION: Task is complete only when the final expected state is "
            "confirmed (e.g. Chrome is open, correct profile is active, page is visible).\n"
            "\n"
            "Think step-by-step but be concise. Use tools only when necessary."
        ),
        "vlm_model": "qwen3-vl:8b",     # Qwen3-VL-8B-Instruct
        "request_timeout_seconds": 300.0,
        "max_tokens": 4096,
        "temperature": 0.2,
        # Phase 1: retry on transient Ollama errors (timeout, connection).
        "retry_max_attempts": 3,
        "retry_base_delay": 0.5,      # 0.5s → 1.0s → 2.0s exponential
    },
    "sandbox": {
        "docker_image": "python:3.11-slim",
        "seccomp_profile_path": "./sandbox/seccomp_profile.json",
        "network_mode": "none",          # R-12: immutable default
        "execution_timeout_seconds": 30.0,
        "pool_size": 2,
        "max_stdout_bytes": 65536,
        "memory_limit": "256m",
        "cpu_quota": 0.5,
    },
    "skill_validator": {
        "signing_private_key_file": "./keys/skill_signing.priv",
        "signing_public_key_file": "./keys/skill_signing.pub",
        "gate2_timeout_seconds": 20.0,
        "max_skill_bytes": 65536,
        "keep_failed_container": False,  # MUST be False in production
    },
    "memory": {
        "hmac_key_file": "./keys/memory_hmac.key",
        "episodic_max_episodes": 1000,   # ring buffer per audit
        "vector_max_items": 10000,       # LRU cap per audit (M-4)
        "vector_ef_construction": 200,
        "vector_m": 16,
        "embedding_model": "bge-m3",              # BGE-M3 (multilingual, 1024d)
        "episode_namespace": "primary",
    },
    "vision": {
        "confidence_threshold": 0.8,     # R-21: hard lower bound
        "vlm_timeout_seconds": 90.0,
        "prefer_accessibility": True,
        "iou_dedup_threshold": 0.5,
        # Phase 1: screen fingerprint cache — skip VLM when screen unchanged.
        "screen_cache_enabled": True,
        "screen_cache_ttl_seconds": 2.0,
        # v2.3: DINO+Florence vision pipeline.
        "vision_backend": "vlm_only",       # "vlm_only" | "dino_primary" | "hybrid"
        "models_dir": "~/.phidipus/models",
        "dino_conf_threshold": 0.3,
        "max_refine_iterations": 3,
    },
    "evolution": {
        "traceback_max_length": 1024,    # R-25
        "goal_max_length": 2048,         # R-24 echo
        "mutations_per_cycle": 5,        # Phase 2: was 3, more diversity → faster converge
        "min_acceptance_score": 0.6,
        "quarantine_ttl_seconds": 3600,
    },
    "skill_forge": {
        "gemini_api_key": "",                    # Set in config.yaml — v9.23: CRITICAL for Vision speed (15s→1.5s)
        "gemini_model": "gemini-2.5-flash",      # Primary provider
        "skills_dir": "data/skills/forge",
        "max_retries": 2,                        # Auto-fix attempts
        "enabled": True,
        # v9.21: Extended fallback stack (all cloud APIs)
        "mistral_api_key": "",                  # Mistral AI — Codestral free tier
        "cerebras_api_key": "",                 # Cerebras — ultra-fast inference
        "openrouter_api_key": "",               # OpenRouter — free models
        "ollama_base_url": "http://127.0.0.1:11434",
        "ollama_coder_model": "qwen2.5-coder:7b",
    },
    "patcher": {
        "rate_limit_per_hour": 10,
        "rate_limit_persist": True,      # R-19: always disk-backed
        "ledger_hash_algorithm": "sha256",
    },
    "spider_hub": {
        "hive_settings": {
            "max_parallel": 5,
            "stagger_s": 30,
            "default_platforms": ["facebook"],
            "default_content_lang": "vi",
        },
        "profiles": [],
    },
    "logging": {
        "level": "INFO",
    },
    "resource_guard": {
        "enabled": True,
        "max_ram_percent": 70.0,
        "warn_ram_percent": 60.0,
        "max_cpu_percent": 80.0,
        "warn_cpu_percent": 65.0,
        "max_windows": 20,
        "max_chrome_profiles": 10,
        "min_disk_free_gb": 2.0,
        "monitor_interval_s": 5.0,
    },
}


# ---------------------------------------------------------------------------
# Security invariant post-validation rules
# ---------------------------------------------------------------------------
# These checks are enforced AFTER schema validation by config_loader.
# They encode rules that cannot be expressed as pure JSON Schema constraints
# (e.g. "confidence_threshold must be >= 0.8, never lower").

def check_security_invariants(cfg: dict[str, Any]) -> list[str]:
    """
    Check security invariants that cannot be expressed in the JSON Schema.

    Returns a list of violation strings.  An empty list means all invariants
    are satisfied.

    Called by config_loader after schema validation passes.

    Invariants checked:
      R-03  daemon process must not enable LLM subsystem
      R-12  sandbox.network_mode must be "none"
      R-19  patcher.rate_limit_persist must be True
      R-21  vision.confidence_threshold must be >= 0.8
      R-25  evolution.traceback_max_length must be <= 1024
      R-24  agent.goal_max_length and evolution.goal_max_length must be <= 2048
    """
    violations: list[str] = []

    # ── R-03: daemon process must not enable the LLM subsystem ───────────────
    # The automation daemon runs at L2 privilege with no LLM access (C.2).
    # If a daemon config enables the LLM subsystem it could be coerced into
    # making LLM API calls, violating the process isolation boundary.
    process_name = cfg.get("process", {}).get("name", "orchestrator")
    if process_name == "daemon":
        if cfg.get("llm", {}).get("enabled", True):
            violations.append(
                "R-03 VIOLATION: Daemon process must not enable LLM subsystem. "
                "Set llm.enabled = false in the daemon configuration. "
                "The automation daemon must never make LLM API calls."
            )

    # R-12
    if cfg.get("sandbox", {}).get("network_mode") != "none":
        violations.append(
            "R-12 VIOLATION: sandbox.network_mode must be 'none'. "
            "No other network mode is permitted in v9.11."
        )

    # R-19
    if not cfg.get("patcher", {}).get("rate_limit_persist", False):
        violations.append(
            "R-19 VIOLATION: patcher.rate_limit_persist must be true. "
            "In-memory rate-limit counters are not acceptable."
        )

    # R-21
    ct = cfg.get("vision", {}).get("confidence_threshold", 0.0)
    if ct < 0.8:
        violations.append(
            f"R-21 VIOLATION: vision.confidence_threshold is {ct}. "
            "Must be >= 0.8. Detections below threshold must not trigger "
            "automated execution."
        )

    # R-25
    tbl = cfg.get("evolution", {}).get("traceback_max_length", 0)
    if tbl > 1024:
        violations.append(
            f"R-25 VIOLATION: evolution.traceback_max_length is {tbl}. "
            "Must be <= 1024 characters."
        )

    # R-24 (agent)
    gml_agent = cfg.get("agent", {}).get("goal_max_length", 0)
    if gml_agent > 2048:
        violations.append(
            f"R-24 VIOLATION: agent.goal_max_length is {gml_agent}. "
            "Must be <= 2048 characters."
        )

    # R-24 (evolution)
    gml_evo = cfg.get("evolution", {}).get("goal_max_length", 0)
    if gml_evo > 2048:
        violations.append(
            f"R-24 VIOLATION: evolution.goal_max_length is {gml_evo}. "
            "Must be <= 2048 characters."
        )

    # skill_validator.keep_failed_container must be False in non-test processes
    if process_name != "test":
        if cfg.get("skill_validator", {}).get("keep_failed_container", False):
            violations.append(
                "SECURITY VIOLATION: skill_validator.keep_failed_container "
                "must be false in non-test processes. Retaining failed "
                "containers leaks execution context."
            )

    return violations
