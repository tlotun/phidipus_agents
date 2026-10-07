# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/node_registry.py — Phidipus v2.4 Priority 3 / C3
═══════════════════════════════════════════════════════════════════

Plugin system for workflow node types.

Allows registering new node types without editing workflow_executor.py.
Each plugin provides: handler function, config schema, default config,
palette info (icon, color, label), and i18n descriptions.

Usage — Register a custom node:

    from core.node_registry import NodeRegistry

    @NodeRegistry.register(
        "my_custom_node",
        schema={"required": ["url"], "values": {"method": ["GET", "POST"]}},
        defaults={"url": "", "method": "GET", "timeout": 10},
        palette={"icon": "🔧", "color": "#f59e0b", "label": "Custom Node"},
    )
    async def exec_my_custom_node(config: dict, prev_result: dict) -> dict:
        url = config.get("url", "")
        # ... do stuff ...
        return {"success": True, "result": "done"}

Usage — Execute from WorkflowExecutor:

    from core.node_registry import NodeRegistry

    if NodeRegistry.has(ntype):
        return await NodeRegistry.execute(ntype, config, prev_result)

Architecture:
  - NodeRegistry is a singleton class-level registry
  - WorkflowExecutor checks registry BEFORE its built-in dispatcher
  - Plugins can be loaded from core/plugins/ directory on startup
  - Each plugin is self-contained: handler + schema + UI info
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional

_log = logging.getLogger("phidipus.node_registry")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════
# Data types
# ══════════════════════════════════════════════════════════════

@dataclass
class NodePlugin:
    """A registered node type plugin."""
    node_type: str
    handler: Callable[..., Awaitable[dict]]
    schema: dict = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    palette: dict = field(default_factory=dict)  # icon, color, label
    i18n: dict = field(default_factory=dict)      # {desc:{vi,en}, hint:{vi,en}, tips:{vi,en}}
    version: str = "1.0"
    author: str = ""


@dataclass
class ValidationResult:
    """Result of config validation."""
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════

class NodeRegistry:
    """
    Central registry for workflow node type plugins.

    Class-level singleton — all methods are classmethods.
    Thread-safe for reads (dict lookups are atomic in CPython).
    """

    _plugins: dict[str, NodePlugin] = {}
    _load_errors: list[str] = []
    # PLUGIN-02 FIX: _plugins is a class-level mutable dict — concurrent plugin
    # loading (e.g. from multiple threads during Spider Hub startup) can race on
    # writes. A threading.Lock serialises all register/load write operations.
    _lock: threading.Lock = threading.Lock()

    # ── Registration ──────────────────────────────────────────

    @classmethod
    def register(
        cls,
        node_type: str,
        schema: dict = None,
        defaults: dict = None,
        palette: dict = None,
        i18n: dict = None,
        version: str = "1.0",
        author: str = "",
    ):
        """
        Decorator to register a node type handler.

        Example:
            @NodeRegistry.register("http_request", schema={...})
            async def exec_http_request(config, prev_result):
                ...
        """
        def decorator(fn):
            plugin = NodePlugin(
                node_type=node_type,
                handler=fn,
                schema=schema or {},
                defaults=defaults or {},
                palette=palette or {},
                i18n=i18n or {},
                version=version,
                author=author,
            )
            with cls._lock:  # PLUGIN-02 FIX: thread-safe write
                # PLUGIN-03 FIX: warn when a duplicate node_type is registered
                # silently overwriting the previous handler caused hard-to-debug issues.
                if node_type in cls._plugins:
                    existing = cls._plugins[node_type]
                    _vlog("⚠️", (
                        f"[PLUGIN-03] Duplicate node type '{node_type}' — "
                        f"overwriting v{existing.version} (author={existing.author or 'unknown'}) "
                        f"with v{version} (author={author or 'unknown'}). "
                        f"Check for conflicting plugin files."
                    ))
                cls._plugins[node_type] = plugin
            _vlog("🔌", f"Node plugin registered: {node_type} (v{version})")
            return fn
        return decorator

    @classmethod
    def register_plugin(cls, plugin: NodePlugin) -> None:
        """Register a pre-built NodePlugin object."""
        with cls._lock:  # PLUGIN-02 FIX: thread-safe write
            if plugin.node_type in cls._plugins:
                existing = cls._plugins[plugin.node_type]
                _vlog("⚠️", (
                    f"[PLUGIN-03] Duplicate node type '{plugin.node_type}' — "
                    f"overwriting v{existing.version} with v{plugin.version}."
                ))
            cls._plugins[plugin.node_type] = plugin
        _vlog("🔌", f"Node plugin registered: {plugin.node_type}")

    # ── Query ─────────────────────────────────────────────────

    @classmethod
    def has(cls, node_type: str) -> bool:
        return node_type in cls._plugins

    @classmethod
    def get(cls, node_type: str) -> Optional[NodePlugin]:
        return cls._plugins.get(node_type)

    @classmethod
    def all_types(cls) -> list[str]:
        return list(cls._plugins.keys())

    @classmethod
    def all_plugins(cls) -> list[NodePlugin]:
        return list(cls._plugins.values())

    @classmethod
    def count(cls) -> int:
        return len(cls._plugins)

    # ── Validation ────────────────────────────────────────────

    @classmethod
    def validate(cls, node_type: str, config: dict) -> ValidationResult:
        """Validate config against plugin schema."""
        plugin = cls._plugins.get(node_type)
        if not plugin:
            return ValidationResult(valid=True)  # unknown = pass through

        schema = plugin.schema
        errors = []
        warnings = []

        # Required fields
        for key in schema.get("required", []):
            val = config.get(key)
            if val is None or (isinstance(val, str) and not val.strip()):
                errors.append(f'{node_type}: thiếu field bắt buộc "{key}"')

        # Value constraints
        for key, allowed in schema.get("values", {}).items():
            val = config.get(key, "")
            if val and val not in allowed:
                errors.append(f'{node_type}: "{key}" = "{val}" — cho phép: {", ".join(allowed)}')

        # Type checks
        for key, expected in schema.get("types", {}).items():
            val = config.get(key)
            if val is not None and not isinstance(val, expected):
                warnings.append(f'{node_type}: "{key}" nên là {expected}')

        # Warn empty
        for key in schema.get("warn_empty", []):
            if not config.get(key):
                warnings.append(f'{node_type}: nên điền "{key}"')

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    # ── Execution ─────────────────────────────────────────────

    @classmethod
    async def execute(
        cls,
        node_type: str,
        config: dict,
        prev_result: Any = None,
        timeout: float = 30.0,
    ) -> dict:
        """
        Execute a registered plugin node.

        Args:
            node_type: Registered node type string
            config: Node configuration dict
            prev_result: Result from previous node
            timeout: Max execution time in seconds

        Returns:
            dict with at least {"success": bool}

        Raises:
            KeyError if node_type not registered
        """
        plugin = cls._plugins.get(node_type)
        if not plugin:
            raise KeyError(f"Node type '{node_type}' not registered in NodeRegistry")

        # Validate first
        vr = cls.validate(node_type, config)
        if not vr.valid:
            return {"success": False, "error": "\n".join(vr.errors), "validation_errors": vr.errors}

        # Execute with timeout
        try:
            result = await asyncio.wait_for(
                plugin.handler(config, prev_result),
                timeout=timeout,
            )
            if not isinstance(result, dict):
                result = {"success": True, "result": str(result)}
            return result
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Plugin {node_type} timeout ({timeout}s)"}
        except Exception as exc:
            return {"success": False, "error": f"Plugin {node_type} error: {str(exc)}"}

    # ── Default config ────────────────────────────────────────

    @classmethod
    def get_defaults(cls, node_type: str) -> dict:
        """Get default config for a plugin node type."""
        plugin = cls._plugins.get(node_type)
        return dict(plugin.defaults) if plugin else {}

    # ── Palette info (for Admin Panel) ────────────────────────

    @classmethod
    def get_palette_info(cls) -> list[dict]:
        """Get palette info for all registered plugins (for Admin Panel)."""
        result = []
        for p in cls._plugins.values():
            result.append({
                "type": p.node_type,
                "icon": p.palette.get("icon", "🔌"),
                "color": p.palette.get("color", "#6b7280"),
                "label": p.palette.get("label", p.node_type),
                "desc": p.i18n.get("desc", {}).get("vi", ""),
                "hint": p.i18n.get("hint", {}).get("vi", ""),
                "tips": p.i18n.get("tips", {}).get("vi", ""),
                "version": p.version,
                "author": p.author,
                "is_plugin": True,
            })
        return result

    # ── Auto-load plugins from directory ──────────────────────

    @classmethod
    def _is_plugin_safe(cls, source: str, filename: str = "") -> bool:
        """
        AST safety scan — block plugins with dangerous patterns.

        Blocks: os.system, subprocess, eval, exec, open(write),
                __import__, importlib.*, socket, pathlib file ops,
                os.environ (secret leakage), tempfile, io.

        SEC-09 FIX: Added pathlib, open(), os.environ, tempfile, io
        to close bypass gaps where plugins could read/write arbitrary
        files or exfiltrate environment secrets despite the original scan.
        """
        import ast as _ast

        # SEC-09 FIX: expanded blocked built-in function names
        BLOCKED_NAMES = {
            "eval", "exec", "__import__", "compile",
            "globals", "locals", "vars", "dir",
            "open",           # SEC-09 FIX: direct file open()
        }

        # SEC-09 FIX: expanded blocked module imports
        BLOCKED_MODULES = {
            "subprocess", "socket", "ctypes", "importlib", "shutil",
            "tempfile",   # SEC-09 FIX: temp files can bypass path restrictions
            "io",         # SEC-09 FIX: io.open() is alias for open()
        }

        BLOCKED_ATTRS = {
            "system", "popen", "exec_module", "run", "Popen", "call",
            # SEC-09 FIX: pathlib dangerous method names
            "write_text", "write_bytes", "open",
            # SEC-09 FIX: os dangerous attributes beyond system/popen
            "environ", "getenv", "putenv",
            # PLUGIN-01 FIX: asyncio.create_subprocess_exec/shell bypass the entire
            # blocklist because `asyncio` is not in BLOCKED_MODULES.
            # A plugin can import asyncio (allowed) then call create_subprocess_shell("rm -rf /")
            "create_subprocess_shell", "create_subprocess_exec",
        }

        # SEC-09 FIX: block access to os.environ / os.getenv etc.
        BLOCKED_OS_ATTRS = {"environ", "getenv", "putenv", "system", "popen"}

        # SEC-09 FIX: block pathlib dangerous method calls
        BLOCKED_PATHLIB_ATTRS = {"write_text", "write_bytes", "open", "read_text", "read_bytes"}

        try:
            tree = _ast.parse(source)
        except SyntaxError:
            _vlog("🛡️", f"Plugin {filename}: syntax error — blocked")
            return False

        for node in _ast.walk(tree):
            # Block dangerous function calls
            if isinstance(node, _ast.Call):
                func = node.func

                # Block bare dangerous names: eval(), exec(), open(), etc.
                if isinstance(func, _ast.Name) and func.id in BLOCKED_NAMES:
                    _vlog("🛡️", f"Plugin {filename}: blocked call to {func.id}()")
                    return False

                if isinstance(func, _ast.Attribute):
                    attr = func.attr

                    # Block os.* dangerous attributes
                    if (isinstance(func.value, _ast.Name)
                            and func.value.id == "os"
                            and attr in BLOCKED_OS_ATTRS):
                        _vlog("🛡️", f"Plugin {filename}: blocked os.{attr}()")
                        return False

                    # Block subprocess.* dangerous calls
                    if (isinstance(func.value, _ast.Name)
                            and func.value.id == "subprocess"
                            and attr in BLOCKED_ATTRS):
                        _vlog("🛡️", f"Plugin {filename}: blocked subprocess.{attr}()")
                        return False

                    # SEC-09 FIX: Block pathlib.Path.write_text / write_bytes / open / read_*
                    if attr in BLOCKED_PATHLIB_ATTRS:
                        _vlog("🛡️", f"Plugin {filename}: blocked pathlib .{attr}() call")
                        return False

                    # Block any other listed dangerous attrs
                    if attr in BLOCKED_ATTRS:
                        _vlog("🛡️", f"Plugin {filename}: blocked .{attr}()")
                        return False

            # Block dangerous module imports: import subprocess, import pathlib, etc.
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in BLOCKED_MODULES:
                        if alias.name == "urllib":
                            continue  # Allow urllib for http_request
                        _vlog("🛡️", f"Plugin {filename}: blocked import {alias.name}")
                        return False

            # Block from-imports: from subprocess import run, from pathlib import Path, etc.
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                top = mod.split(".")[0]
                if top in BLOCKED_MODULES:
                    _vlog("🛡️", f"Plugin {filename}: blocked from {mod} import ...")
                    return False
                # SEC-09 FIX: block "from pathlib import Path" specifically
                if mod == "pathlib":
                    _vlog("🛡️", f"Plugin {filename}: blocked pathlib import")
                    return False

            # SEC-09 FIX: Block os.environ[] subscript access (e.g. os.environ["KEY"])
            if isinstance(node, _ast.Subscript):
                val = node.value
                if (isinstance(val, _ast.Attribute)
                        and val.attr == "environ"
                        and isinstance(val.value, _ast.Name)
                        and val.value.id == "os"):
                    _vlog("🛡️", f"Plugin {filename}: blocked os.environ[] access")
                    return False

        return True

    @classmethod
    def load_plugins_dir(cls, directory: str = "core/plugins") -> int:
        """
        Auto-load all .py files from plugins directory.

        Each file should call @NodeRegistry.register() at module level.
        Returns number of plugins loaded.
        """
        plugins_path = Path(directory)
        if not plugins_path.exists():
            return 0

        before = cls.count()
        with cls._lock:  # PLUGIN-02 FIX: reset load_errors under lock
            cls._load_errors = []

        for py_file in sorted(plugins_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                # FIX BUG#2: AST safety scan before exec_module
                source = py_file.read_text("utf-8")
                if not cls._is_plugin_safe(source, py_file.name):
                    err = f"Plugin BLOCKED (unsafe): {py_file.name}"
                    cls._load_errors.append(err)
                    _vlog("🛡️", err)
                    continue

                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    f"phidipus_plugin_{py_file.stem}", py_file
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as exc:
                err = f"Plugin load error: {py_file.name}: {exc}"
                cls._load_errors.append(err)
                _vlog("❌", err)

        loaded = cls.count() - before
        if loaded > 0:
            _vlog("🔌", f"Loaded {loaded} plugins from {directory}/")
        return loaded

    # ── Stats ─────────────────────────────────────────────────

    @classmethod
    def stats(cls) -> dict:
        return {
            "registered_plugins": cls.count(),
            "plugin_types": cls.all_types(),
            "load_errors": cls._load_errors,
        }
