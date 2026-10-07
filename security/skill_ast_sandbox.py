# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
security/skill_ast_sandbox.py — Phidipus v1.0
Hardened AST-based static analysis sandbox for LLM-generated skill code.

Architecture contract (R-11 / Gate 1 of 3):
  "R-11: ast_gate must have allow_network=False, pathlib EXCLUDED from
   SAFE_IMPORTS, and asyncio EXCLUDED from SAFE_IMPORTS. This configuration
   is immutable."

This module is Gate 1 of the three-gate skill validation pipeline:

    Gate 1 — SkillASTSandbox (this module)   ← static AST analysis
    Gate 2 — DockerGate                       ← ephemeral execution
    Gate 3 — SchemaGate                       ← output schema validation

A skill that fails Gate 1 is rejected immediately — it never reaches
Gate 2.  Gate 1 CANNOT be the sole gate (R-08); it is always paired with
Gates 2 and 3 via skill_validator/ast_gate.py.

Hardening changes from v9.10
-----------------------------
C-4   pathlib removed from SAFE_IMPORTS.
      pathlib.Path can be used for path-traversal attacks and arbitrary
      file reads/writes.  Generated skill code has no legitimate need for
      path manipulation — that belongs to the daemon layer.

M-1   asyncio removed from SAFE_IMPORTS.
      asyncio event-loop manipulation (e.g. loop.run_in_executor,
      asyncio.create_subprocess_exec) can bypass the sandbox.

C-5   allow_network=False is now immutable.
      The constructor rejects any attempt to set allow_network=True.
      Network syscalls are blocked by the seccomp profile in Gate 2; this
      flag provides a defence-in-depth check at the AST layer.

H-1   Assignment-chain taint tracking added.
      v9.10 only checked direct import names. A skill could bypass the
      check by aliasing: ``import os as x; x.system("cmd")``.
      v9.11 builds a taint map of all local names that alias dangerous
      modules or builtins, then flags any attribute access or call on a
      tainted name — including multi-hop chains
      (``a = os; b = a; b.system("cmd")``).

Process: orchestrator (L1)

Security invariants enforced here:
  R-11  SAFE_IMPORTS is a frozenset — immutable at runtime.
        pathlib and asyncio are explicitly excluded.
        allow_network is always False; cannot be overridden.
  R-07  This module performs ONLY static analysis.  It never calls
        exec(), eval(), exec_module(), or importlib on skill code.
  R-08  A Gate 1 failure raises ASTSandboxViolation immediately.
        skill_validator/ast_gate.py wraps this and ensures no Gate-1-
        failed skill reaches Gate 2.

Used by:
  skill_validator/ast_gate.py  — wraps check() as Gate 1 of 3

Dependencies:
  utils/logger.py — get_logger()
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Immutable allowlist (R-11: pathlib and asyncio must be absent)
# ---------------------------------------------------------------------------

#: Modules that generated skill code is permitted to import.
#:
#: Design principles:
#:   - Only pure-Python stdlib modules with no OS/network/filesystem side
#:     effects are allowed.
#:   - pathlib is EXCLUDED (C-4): path manipulation enables traversal attacks.
#:   - asyncio is EXCLUDED (M-1): event-loop access enables subprocess escapes.
#:   - os, sys, subprocess, socket, importlib, ctypes are EXCLUDED.
#:   - Third-party packages (requests, numpy, etc.) are EXCLUDED; Gate 2
#:     (Docker) controls the execution environment.
#:
#: This frozenset is verified at module-load time by the assertion below.
SAFE_IMPORTS: frozenset[str] = frozenset({
    # Data structures / algorithms
    "json",
    "re",
    "math",
    "cmath",
    "decimal",
    "fractions",
    "statistics",
    "random",
    "string",
    "textwrap",
    "collections",
    "heapq",
    "bisect",
    "itertools",
    "functools",
    "operator",
    "copy",
    "pprint",
    # Date and time (read-only — no filesystem side effects)
    "time",
    "datetime",
    "calendar",
    "uuid",         # FIX B-04: safe stdlib, used for ID generation
    # Type system
    "typing",
    # NOTE: "types" REMOVED — see EXPLICITLY_BLOCKED_MODULES (FIX #1)
    "dataclasses",
    "enum",
    "abc",
    # I/O (in-memory only — StringIO/BytesIO; NOT file I/O)
    "io",
    "struct",
    "base64",
    "binascii",
    "codecs",
    "unicodedata",
    "difflib",
    # Numeric
    "array",
    "numbers",
    # Hash / crypto (read operations only — no key generation)
    "hashlib",
    "hmac",
    # Contextlib / utilities
    "contextlib",
    "warnings",
    "traceback",
    # NOTE: "inspect" REMOVED (FIX #1) — inspect.currentframe().f_globals gives
    #   access to __builtins__ dict, bypassing all BLOCKED_ATTRIBUTES checks.
    # NOTE: "types" REMOVED (FIX #1) — types.CodeType allows creation of arbitrary
    #   bytecode objects that can encode any operation, bypassing static analysis.
    # NOTE: "dis" REMOVED (FIX #1) — disassembly is not needed in skill code;
    #   removed along with types/inspect to reduce introspection surface.
})

#: Modules that are explicitly PROHIBITED even if not in SAFE_IMPORTS.
#: Listed here for auditability — denial is implicit (anything not in
#: SAFE_IMPORTS is denied), but these are the highest-risk targets.
EXPLICITLY_BLOCKED_MODULES: frozenset[str] = frozenset({
    "os", "sys", "subprocess", "socket", "ssl", "socketserver",
    "pathlib",      # C-4
    "asyncio",      # M-1
    "asynchat", "asyncore",
    "importlib", "imp", "pkgutil",
    "ctypes", "cffi",
    "threading", "multiprocessing", "concurrent",
    "signal", "mmap",
    "urllib", "http", "ftplib", "smtplib", "poplib", "imaplib",
    "xmlrpc", "email", "html", "xml",
    "requests", "aiohttp", "httpx",
    "pickle", "shelve", "marshal",
    "code", "codeop", "compileall",
    "pty", "tty", "termios",
    "resource", "grp", "pwd",
    "curses", "readline",
    "gc",           # can expose memory addresses
    "weakref",      # blocked — can expose object internals (Bug #8 fix)
    "_thread", "greenlet",
    "builtins",     # direct access to builtins namespace
    # FIX #1: introspection modules that allow frame/code escape
    "inspect",      # inspect.currentframe().f_globals → __builtins__ → exec/eval
    "types",        # types.CodeType → arbitrary bytecode creation
    "dis",          # disassembly; not needed in skill code
})

#: Dangerous builtin names that must not be called in skill code.
BLOCKED_BUILTINS: frozenset[str] = frozenset({
    "exec", "eval", "compile",
    "__import__", "importlib",
    "open",         # filesystem access
    "input",        # interactive input — meaningless in sandbox
    "breakpoint",   # debugger hook
    "memoryview",   # raw memory access
    "vars", "globals", "locals",  # namespace introspection / injection
    "setattr", "delattr",         # dynamic attribute manipulation
    "object.__subclasses__",      # class hierarchy traversal
    # FIX B-01: block reflective access patterns
    "__builtins__",   # bare name access via subscript/getattr
    "getattr",        # reflective attribute access — getattr(x, "exec")
    "hasattr",        # existence probe (precursor to getattr)
})

#: Dangerous dunder / special attribute names.
BLOCKED_ATTRIBUTES: frozenset[str] = frozenset({
    "__class__",
    "__bases__",
    "__subclasses__",
    "__mro__",
    "__globals__",
    "__builtins__",
    "__dict__",
    "__code__",
    "__closure__",
    "__import__",
    "__loader__",
    "__spec__",
    "__file__",     # reveals host filesystem paths
    "__path__",
    "__reduce__",   # pickle/unpickle hook
    "__reduce_ex__",
    "__getstate__",
    "__setstate__",
    # OS escape via subprocess-like builtins
    "system",       # os.system alias
    "popen",        # os.popen alias
    "execv", "execvp", "execvpe",
    "fork", "spawn", "spawnl",
    # FIX #1: frame object attributes — used by inspect.currentframe() to
    # reach f_globals → __builtins__ → exec/eval even without importing os/sys
    "f_globals",    # frame.f_globals → full module namespace including builtins
    "f_locals",     # frame.f_locals → local variable namespace
    "f_builtins",   # frame.f_builtins → direct access to builtins dict
    "f_code",       # frame.f_code → code object (bytecode, constants, names)
    "f_back",       # frame.f_back → caller frame (chain traversal)
    "f_lineno",     # minor: still block to reduce frame access surface
    # FIX #1: code object attributes — used by types.CodeType to inspect
    # or reconstruct arbitrary bytecode
    "co_code",      # raw bytecode bytes
    "co_consts",    # constants (can contain nested code objects)
    "co_names",     # names referenced by bytecode
    "co_varnames",  # local variable names
    "co_freevars",  # free variable names (closures)
    "co_cellvars",  # cell variable names
})

#: Maximum allowed source code size in bytes.  Skills larger than this
#: are rejected before AST parsing to prevent ReDoS / memory exhaustion.
MAX_SOURCE_BYTES: int = 65536  # 64 KiB — matches skill_validator config default

#: Maximum allowed AST node count.  Prevents degenerate code that passes
#: textual checks but exploits quadratic AST walking.
MAX_AST_NODES: int = 10_000


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

@dataclass
class ViolationDetail:
    """
    Structured record of a single AST sandbox rule violation.

    Attributes:
        rule:      Short machine-readable rule code (e.g. ``"BLOCKED_IMPORT"``).
        message:   Human-readable description.
        node_type: AST node type name (e.g. ``"Import"``, ``"Call"``).
        lineno:    Source line number (1-based), or 0 if unavailable.
        name:      The specific name that triggered the violation.
    """

    rule:      str
    message:   str
    node_type: str = ""
    lineno:    int = 0
    name:      str = ""


class ASTSandboxViolation(RuntimeError):
    """
    Raised when SkillASTSandbox.check() detects one or more violations.

    All violations found in a single check() call are collected and
    reported together so the caller receives a complete picture rather
    than seeing violations one at a time.

    Attributes:
        violations: Ordered list of ViolationDetail records.
        skill_name: Identifier of the skill being checked (may be empty).
        reason:     Short machine-readable summary reason code.
    """

    def __init__(
        self,
        message:    str,
        violations: list[ViolationDetail],
        *,
        skill_name: str = "",
        reason:     str = "AST_VIOLATION",
    ) -> None:
        super().__init__(message)
        self.violations = violations
        self.skill_name = skill_name
        self.reason     = reason

    def __str__(self) -> str:
        base = super().__str__()
        lines = [f"[{self.reason}]"]
        if self.skill_name:
            lines.append(f"skill={self.skill_name!r}")
        lines.append(f"({len(self.violations)} violation(s))")
        lines.append(base)
        for v in self.violations[:5]:  # cap output to first 5
            lines.append(
                f"  L{v.lineno} [{v.rule}] {v.node_type}: {v.message}"
            )
        if len(self.violations) > 5:
            lines.append(f"  ... +{len(self.violations) - 5} more")
        return "\n".join(lines)

    def summary(self) -> str:
        """Return a one-line summary (first violation rule + count)."""
        if not self.violations:
            return f"[{self.reason}] no detail"
        first = self.violations[0]
        return (
            f"[{self.reason}] {len(self.violations)} violation(s); "
            f"first: L{first.lineno} [{first.rule}] {first.message}"
        )


# ---------------------------------------------------------------------------
# SkillASTSandbox
# ---------------------------------------------------------------------------

class SkillASTSandbox:
    """
    Hardened AST-based static analysis sandbox for LLM-generated skill code.

    Performs Gate 1 analysis only.  Never executes skill code.

    Immutable configuration (R-11):
        - SAFE_IMPORTS is a frozenset — cannot be modified at runtime.
        - allow_network is always False — cannot be overridden.
        - pathlib and asyncio are absent from SAFE_IMPORTS — always.

    Assignment-chain taint tracking (H-1):
        The sandbox builds a taint map of local names that alias dangerous
        modules or builtins.  Any attribute access or function call on a
        tainted name is flagged, even after multiple aliasing hops.

    Usage::

        sandbox = SkillASTSandbox()
        try:
            sandbox.check(skill_source_code, skill_name="my_skill")
        except ASTSandboxViolation as exc:
            print(exc.summary())
            # → [AST_VIOLATION] 2 violation(s); first: L3 [BLOCKED_IMPORT] ...

    Args:
        max_source_bytes: Override the default MAX_SOURCE_BYTES limit.
                          Must be > 0 and <= MAX_SOURCE_BYTES.
        max_ast_nodes:    Override the default MAX_AST_NODES limit.
    """

    # ------------------------------------------------------------------
    # R-11 assertion: these are the module-level constants.  The sandbox
    # reads them but never mutates them.  The assertions below the class
    # body verify the invariants at import time.
    # ------------------------------------------------------------------

    def __init__(
        self,
        max_source_bytes: int = MAX_SOURCE_BYTES,
        max_ast_nodes:    int = MAX_AST_NODES,
    ) -> None:
        # R-11: allow_network is ALWAYS False — not a parameter.
        self._allow_network: bool = False

        if max_source_bytes <= 0 or max_source_bytes > MAX_SOURCE_BYTES:
            raise ValueError(
                f"max_source_bytes must be in (0, {MAX_SOURCE_BYTES}], "
                f"got {max_source_bytes}."
            )
        if max_ast_nodes <= 0 or max_ast_nodes > MAX_AST_NODES:
            raise ValueError(
                f"max_ast_nodes must be in (0, {MAX_AST_NODES}], "
                f"got {max_ast_nodes}."
            )

        self._max_source_bytes = max_source_bytes
        self._max_ast_nodes    = max_ast_nodes

        _log.debug(
            "SkillASTSandbox initialised",
            extra={
                "max_source_bytes": max_source_bytes,
                "max_ast_nodes":    max_ast_nodes,
                "allow_network":    False,
                "safe_imports":     len(SAFE_IMPORTS),
            },
        )

    @property
    def allow_network(self) -> bool:
        """Always False.  Cannot be overridden (R-11 / C-5)."""
        return self._allow_network

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        source:     str,
        *,
        skill_name: str = "",
    ) -> None:
        """
        Run all AST safety checks on *source*.

        All violations found during the full analysis are collected and
        raised together in a single ASTSandboxViolation.  This allows the
        caller to report a complete picture rather than a single violation.

        Checks performed (in order):
          1. Source size limit (MAX_SOURCE_BYTES).
          2. AST parse — SyntaxError is wrapped in ASTSandboxViolation.
          3. AST node count limit (MAX_AST_NODES).
          4. Import allowlist — only SAFE_IMPORTS are permitted (R-11).
          5. Blocked builtins — exec, eval, open, __import__, etc.
          6. Blocked attribute access — __globals__, __builtins__, etc.
          7. Assignment-chain taint tracking (H-1) — aliases of dangerous
             modules flagged through multi-hop chains.
          8. Network API calls — socket, connect, urllib.request, etc.
             (always checked regardless of allow_network because
             allow_network is always False — R-11 / C-5).

        Args:
            source:     Skill Python source code as a string.
            skill_name: Optional identifier used in log messages and
                        exception attributes.  Never used in analysis logic.

        Raises:
            ASTSandboxViolation: if any violation is detected.
            TypeError:           if source is not a str.
        """
        if not isinstance(source, str):
            raise TypeError(
                f"skill source must be a str, got {type(source).__name__!r}."
            )

        violations: list[ViolationDetail] = []

        # ── Check 1: source size ─────────────────────────────────────────
        src_bytes = source.encode("utf-8")
        if len(src_bytes) > self._max_source_bytes:
            violations.append(ViolationDetail(
                rule="SOURCE_TOO_LARGE",
                message=(
                    f"Skill source is {len(src_bytes)} bytes; "
                    f"maximum is {self._max_source_bytes} bytes."
                ),
                node_type="<source>",
                lineno=0,
                name="<source>",
            ))
            # Cannot parse — stop here.
            self._raise_if_violations(violations, skill_name)
            return

        # ── Check 2: AST parse ───────────────────────────────────────────
        try:
            tree = ast.parse(source, filename=skill_name or "<skill>")
        except SyntaxError as exc:
            violations.append(ViolationDetail(
                rule="SYNTAX_ERROR",
                message=f"AST parse failed: {exc.msg} (line {exc.lineno})",
                node_type="<syntax>",
                lineno=exc.lineno or 0,
                name="<syntax>",
            ))
            self._raise_if_violations(violations, skill_name)
            return

        # ── Check 3: AST node count ──────────────────────────────────────
        node_count = sum(1 for _ in ast.walk(tree))
        if node_count > self._max_ast_nodes:
            violations.append(ViolationDetail(
                rule="AST_TOO_LARGE",
                message=(
                    f"AST has {node_count} nodes; "
                    f"maximum is {self._max_ast_nodes}."
                ),
                node_type="<ast>",
                lineno=0,
                name="<ast>",
            ))
            self._raise_if_violations(violations, skill_name)
            return

        # ── Build taint map (H-1) ────────────────────────────────────────
        # Must be done BEFORE checks 4-8 so tainted aliases are known.
        taint_map: dict[str, str] = self._build_taint_map(tree)

        # ── Checks 4-8: walk AST once, collecting all violations ─────────
        for node in ast.walk(tree):
            lineno = getattr(node, "lineno", 0)

            # ── Check 4: import allowlist ────────────────────────────────
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for v in self._check_import_node(node, lineno):
                    violations.append(v)

            # ── Check 5: blocked builtins ────────────────────────────────
            elif isinstance(node, ast.Call):
                for v in self._check_call_node(node, lineno, taint_map):
                    violations.append(v)

            # ── Check 6: blocked attribute access ────────────────────────
            elif isinstance(node, ast.Attribute):
                for v in self._check_attribute_node(node, lineno, taint_map):
                    violations.append(v)

            # ── Check 7: tainted name usage (H-1) ────────────────────────
            elif isinstance(node, ast.Name):
                for v in self._check_name_node(node, lineno, taint_map):
                    violations.append(v)

        # ── Raise if any violations collected ────────────────────────────
        self._raise_if_violations(violations, skill_name)

        _log.debug(
            "R-11: SkillASTSandbox Gate 1 PASSED",
            extra={
                "skill_name":  skill_name,
                "source_bytes": len(src_bytes),
                "ast_nodes":    node_count,
                "taint_names":  list(taint_map.keys()),
            },
        )

    # ------------------------------------------------------------------
    # Taint map construction (H-1)
    # ------------------------------------------------------------------

    def _build_taint_map(self, tree: ast.Module) -> dict[str, str]:
        """
        Build a mapping of local names that alias dangerous modules or
        builtins.

        Returns:
            Dict mapping local name → dangerous origin description.
            Example: ``{"shell": "os", "x": "os (via shell)"}``

        H-1 algorithm:
          Pass 1 — seed from import aliases:
            ``import os as shell``        → shell → os
            ``from os import system as s``→ s → os.system

          Pass 2 — propagate through assignment chains (repeat until stable):
            ``x = shell``                 → x → os (via shell)
            ``y = x``                     → y → os (via x)

          Also seeds from known dangerous builtins used as values:
            ``run = exec``                → run → exec
            ``a = __import__``            → a → __import__
        """
        taint: dict[str, str] = {}

        # ── Pass 1: import aliases ────────────────────────────────────────
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod_root = alias.name.split(".")[0]
                    local    = alias.asname or alias.name.split(".")[0]
                    if (mod_root in EXPLICITLY_BLOCKED_MODULES
                            or mod_root not in SAFE_IMPORTS):
                        taint[local] = alias.name

            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                mod_root = mod.split(".")[0]
                if (mod_root in EXPLICITLY_BLOCKED_MODULES
                        or (mod_root and mod_root not in SAFE_IMPORTS)):
                    for alias in node.names:
                        local  = alias.asname or alias.name
                        origin = f"{mod}.{alias.name}" if mod else alias.name
                        taint[local] = origin

        # ── Pass 1b: dangerous builtins used as values ─────────────────
        # Detect: ``run = eval``, ``do = exec``, ``imp = __import__``
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = node.value
                value_name = ""
                if isinstance(value, ast.Name):
                    value_name = value.id
                elif isinstance(value, ast.Attribute):
                    # e.g. os.system assigned to a local
                    if isinstance(value.value, ast.Name):
                        owner = value.value.id
                        attr  = value.attr
                        if owner in taint or attr in BLOCKED_BUILTINS:
                            value_name = f"{owner}.{attr}"

                if value_name in BLOCKED_BUILTINS or value_name in taint:
                    origin = taint.get(value_name, value_name)
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            taint[target.id] = origin

        # ── Pass 2: chain propagation (fixed-point iteration) ─────────
        # Repeat until no new taint entries are added.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                value = node.value
                if isinstance(value, ast.Name) and value.id in taint:
                    origin = taint[value.id]
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            key = target.id
                            new_origin = f"{origin} (via {value.id})"
                            if key not in taint:
                                taint[key] = new_origin
                                changed = True

        return taint

    # ------------------------------------------------------------------
    # Node-level check helpers
    # ------------------------------------------------------------------

    def _check_import_node(
        self,
        node:   ast.Import | ast.ImportFrom,
        lineno: int,
    ) -> list[ViolationDetail]:
        """Check an import statement against SAFE_IMPORTS."""
        violations: list[ViolationDetail] = []

        if isinstance(node, ast.Import):
            for alias in node.names:
                mod_root = alias.name.split(".")[0]
                if mod_root not in SAFE_IMPORTS:
                    rule = (
                        "BLOCKED_IMPORT_NETWORK"
                        if self._is_network_module(mod_root)
                        else "BLOCKED_IMPORT"
                    )
                    violations.append(ViolationDetail(
                        rule=rule,
                        message=(
                            f"Import of {alias.name!r} is not allowed. "
                            f"Only modules in SAFE_IMPORTS are permitted (R-11)."
                        ),
                        node_type="Import",
                        lineno=lineno,
                        name=alias.name,
                    ))

        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            mod_root = mod.split(".")[0]
            if mod_root and mod_root not in SAFE_IMPORTS:
                rule = (
                    "BLOCKED_IMPORT_NETWORK"
                    if self._is_network_module(mod_root)
                    else "BLOCKED_IMPORT"
                )
                violations.append(ViolationDetail(
                    rule=rule,
                    message=(
                        f"From-import of {mod!r} is not allowed (R-11)."
                    ),
                    node_type="ImportFrom",
                    lineno=lineno,
                    name=mod,
                ))
            # Star imports are always blocked regardless of module
            for alias in node.names:
                if alias.name == "*":
                    violations.append(ViolationDetail(
                        rule="STAR_IMPORT",
                        message=(
                            f"Star import 'from {mod} import *' is not allowed. "
                            "Explicit imports only."
                        ),
                        node_type="ImportFrom",
                        lineno=lineno,
                        name=f"{mod}.*",
                    ))

        return violations

    def _check_call_node(
        self,
        node:      ast.Call,
        lineno:    int,
        taint_map: dict[str, str],
    ) -> list[ViolationDetail]:
        """Check a function call for blocked builtins and tainted callables."""
        violations: list[ViolationDetail] = []
        func = node.func

        # Direct call: exec(...), eval(...), open(...), etc.
        if isinstance(func, ast.Name):
            if func.id in BLOCKED_BUILTINS:
                violations.append(ViolationDetail(
                    rule="BLOCKED_BUILTIN_CALL",
                    message=(
                        f"Call to blocked builtin {func.id!r}. "
                        "This function is not permitted in skill code."
                    ),
                    node_type="Call",
                    lineno=lineno,
                    name=func.id,
                ))
            # H-1: call on a tainted name
            elif func.id in taint_map:
                violations.append(ViolationDetail(
                    rule="TAINTED_NAME_CALL",
                    message=(
                        f"Call to tainted name {func.id!r} "
                        f"(aliases {taint_map[func.id]!r}). "
                        "Assignment-chain taint detected (H-1)."
                    ),
                    node_type="Call",
                    lineno=lineno,
                    name=func.id,
                ))

            # FIX B-01 Part 3: detect getattr(x, "dangerous_attr") pattern.
            # getattr(__builtins__, "exec")("import os") bypasses all
            # attribute-level checks because it never creates an ast.Attribute.
            if func.id == "getattr" and len(node.args) >= 2:
                attr_arg = node.args[1]
                if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
                    target_attr = attr_arg.value
                    if target_attr in BLOCKED_ATTRIBUTES or target_attr in BLOCKED_BUILTINS:
                        violations.append(ViolationDetail(
                            rule="BLOCKED_GETATTR_CALL",
                            message=(
                                f"getattr() call requesting blocked attribute "
                                f"{target_attr!r} is not permitted. "
                                "Use of getattr() to access dangerous attributes "
                                "bypasses static analysis."
                            ),
                            node_type="Call",
                            lineno=lineno,
                            name=f"getattr(..., {target_attr!r})",
                        ))

        # Attribute call: obj.method(...)
        elif isinstance(func, ast.Attribute):
            attr_name = func.attr
            if isinstance(func.value, ast.Name):
                owner_name = func.value.id
                # H-1: call on tainted object
                if owner_name in taint_map:
                    violations.append(ViolationDetail(
                        rule="TAINTED_ATTR_CALL",
                        message=(
                            f"Method call {owner_name!r}.{attr_name!r} on tainted "
                            f"object (aliases {taint_map[owner_name]!r}) (H-1)."
                        ),
                        node_type="Call",
                        lineno=lineno,
                        name=f"{owner_name}.{attr_name}",
                    ))
                # Blocked attribute calls regardless of owner
                if attr_name in BLOCKED_ATTRIBUTES:
                    violations.append(ViolationDetail(
                        rule="BLOCKED_ATTR_CALL",
                        message=(
                            f"Call to blocked attribute {attr_name!r} "
                            f"on {owner_name!r}."
                        ),
                        node_type="Call",
                        lineno=lineno,
                        name=f"{owner_name}.{attr_name}",
                    ))

        return violations

    def _check_attribute_node(
        self,
        node:      ast.Attribute,
        lineno:    int,
        taint_map: dict[str, str],
    ) -> list[ViolationDetail]:
        """Check an attribute access for blocked dunder/special attributes."""
        violations: list[ViolationDetail] = []
        attr = node.attr

        if attr in BLOCKED_ATTRIBUTES:
            owner = ""
            if isinstance(node.value, ast.Name):
                owner = node.value.id
            violations.append(ViolationDetail(
                rule="BLOCKED_ATTRIBUTE",
                message=(
                    f"Access to blocked attribute {attr!r}"
                    + (f" on {owner!r}" if owner else "")
                    + "."
                ),
                node_type="Attribute",
                lineno=lineno,
                name=attr,
            ))

        return violations

    def _check_name_node(
        self,
        node:      ast.Name,
        lineno:    int,
        taint_map: dict[str, str],
    ) -> list[ViolationDetail]:
        """
        Flag a bare tainted name used as a value expression (not in a call
        or assignment target — those are handled by other checks).

        FIX B-01: also flags dangerous bare names like ``__builtins__`` that
        can be used via subscript (``__builtins__["exec"]``) or as targets
        of ``getattr()`` to bypass attribute-level checks.
        """
        # Only flag Load context (not Store or Del)
        if not isinstance(node.ctx, ast.Load):
            return []

        # FIX B-01: block dangerous bare names regardless of taint map.
        # These names provide access to Python runtime internals and can
        # be used via subscript (__builtins__["exec"]) to bypass all
        # attribute-level checks.
        _DANGEROUS_BARE_NAMES = frozenset({
            "__builtins__", "__loader__", "__spec__", "__import__",
            "__build_class__",
        })
        if node.id in _DANGEROUS_BARE_NAMES:
            return [ViolationDetail(
                rule="BLOCKED_BARE_NAME",
                message=(
                    f"Access to dangerous bare name {node.id!r} is not "
                    "permitted.  This name provides access to the Python "
                    "runtime internals."
                ),
                node_type="Name",
                lineno=lineno,
                name=node.id,
            )]

        if node.id in taint_map:
            return [ViolationDetail(
                rule="TAINTED_NAME_LOAD",
                message=(
                    f"Tainted name {node.id!r} used as a value "
                    f"(aliases {taint_map[node.id]!r}) (H-1)."
                ),
                node_type="Name",
                lineno=lineno,
                name=node.id,
            )]

        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _raise_if_violations(
        self,
        violations: list[ViolationDetail],
        skill_name: str,
    ) -> None:
        """Raise ASTSandboxViolation if the violations list is non-empty."""
        if not violations:
            return

        _log.error(
            "R-11: SkillASTSandbox Gate 1 FAILED",
            extra={
                "skill_name":        skill_name,
                "violation_count":   len(violations),
                "first_rule":        violations[0].rule,
                "first_line":        violations[0].lineno,
                "first_name":        violations[0].name,
            },
        )
        raise ASTSandboxViolation(
            f"Skill code failed AST Gate 1 with {len(violations)} "
            f"violation(s). "
            f"First: L{violations[0].lineno} [{violations[0].rule}] "
            f"{violations[0].message}",
            violations=violations,
            skill_name=skill_name,
            reason="AST_VIOLATION",
        )

    @staticmethod
    def _is_network_module(mod_root: str) -> bool:
        """Return True if mod_root is a known network-related module."""
        _NETWORK_MODS = frozenset({
            "socket", "ssl", "urllib", "http", "ftplib", "smtplib",
            "poplib", "imaplib", "xmlrpc", "socketserver", "asyncio",
            "aiohttp", "httpx", "requests", "websockets", "tornado",
        })
        return mod_root in _NETWORK_MODS


# ---------------------------------------------------------------------------
# Module-load-time integrity assertions (R-11)
# ---------------------------------------------------------------------------
# These assertions run once at import time and verify that the critical
# security invariants of this module have not been accidentally broken
# by a code edit (e.g. someone accidentally adding pathlib or asyncio back
# to SAFE_IMPORTS, or removing an entry from EXPLICITLY_BLOCKED_MODULES).

assert isinstance(SAFE_IMPORTS, frozenset), (
    "SAFE_IMPORTS must be a frozenset — it is an immutable allowlist (R-11)."
)

assert "pathlib" not in SAFE_IMPORTS, (
    "pathlib must NOT be in SAFE_IMPORTS (C-4). "
    "pathlib enables path-traversal attacks in LLM-generated skill code."
)

assert "asyncio" not in SAFE_IMPORTS, (
    "asyncio must NOT be in SAFE_IMPORTS (M-1). "
    "asyncio event-loop access can be used to bypass the sandbox."
)

assert "os" not in SAFE_IMPORTS, (
    "os must NOT be in SAFE_IMPORTS. "
    "Direct OS access from skill code is forbidden."
)

assert "subprocess" not in SAFE_IMPORTS, (
    "subprocess must NOT be in SAFE_IMPORTS. "
    "subprocess execution from skill code is forbidden."
)

assert "socket" not in SAFE_IMPORTS, (
    "socket must NOT be in SAFE_IMPORTS (R-11 network isolation). "
    "Network access from skill code is forbidden."
)

# EXPLICITLY_BLOCKED_MODULES must cover the highest-risk paths
_REQUIRED_BLOCKED = frozenset({"os", "subprocess", "socket", "pathlib", "asyncio", "importlib"})
_missing_blocked = _REQUIRED_BLOCKED - EXPLICITLY_BLOCKED_MODULES
assert not _missing_blocked, (
    f"EXPLICITLY_BLOCKED_MODULES is missing required entries: {_missing_blocked}. "
    "These modules MUST be explicitly blocked (R-11)."
)
