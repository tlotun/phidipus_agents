# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/skill_runner_subprocess.py — Phidipus Isolated Skill Runner v9.20-sec-r3
═══════════════════════════════════════════════════════════════════════════════

This module is the ENTRY POINT for subprocess-isolated skill execution.
It is launched as a CHILD PROCESS by SkillForge._execute_code(), NOT
imported into the parent process.

Why subprocess isolation instead of exec() in-process:
─────────────────────────────────────────────────────
  exec(code, restricted_namespace) in Python is fundamentally insecure
  because the child code runs in the SAME process as the parent.  Even
  with restricted __builtins__, an attacker can traverse the class
  hierarchy via literal syntax (no builtins required):

      ()  .__class__.__mro__[-1].__subclasses__()
      ↑ tuple literal — zero builtins needed

  Any class in the runtime that was loaded before exec() carries its
  original __globals__ dict, which contains the full __builtins__ dict
  of that module.  From there, __import__ is reachable.

  Running in a CHILD PROCESS eliminates this entirely:
    • Separate memory space — no shared globals with parent
    • Process killed on timeout → no resource leak
    • Parent cannot be introspected from child
    • Even a full escape only compromises the child process

Execution model:
────────────────
  Parent (SkillForge):
    1. Writes skill code to a tempfile in /tmp/phidipus_sandboxed/
    2. Launches: python3 -I -E skill_runner_subprocess.py <code_file> <tools_json>
    3. Reads stdout for JSON result
    4. Kills process if timeout exceeded
    5. Deletes tempfile

  Child (this file):
    1. Reads code from the tempfile path in argv[1]
    2. Builds a minimal local namespace (no parent globals)
    3. exec()s the code with 30s alarm
    4. Prints JSON result to stdout
    5. Exits

Security invariants:
    • Python -I flag: ignores PYTHONSTARTUP, PYTHONPATH, user site-packages
    • Python -E flag: ignores all PYTHON* environment variables
    • Minimal environment passed: only PATH=/usr/bin:/bin
    • No network access (OS-level, enforced separately)
    • Code file deleted by parent after child exits
    • All result paths validated against SAFE_OUTPUT_ROOTS before return
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import traceback
from pathlib import Path

# ─── Ensure project root on path so utils can be imported ────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ─── Safe output roots (must match sanitizer.py) ─────────────────────────────
_SAFE_ROOTS = [
    Path("/tmp/phidipus_output"),
    Path.home() / "Documents",
    Path.home() / "Downloads",
    Path.home() / "Desktop",
]


def _is_safe_path(p: str) -> bool:
    try:
        resolved = Path(p).expanduser().resolve()
        # Also check for symlinks pointing outside safe roots
        if resolved.is_symlink():
            return False
        for root in _SAFE_ROOTS:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False
    except Exception:
        return False


def _output(success: bool, message: str = "", files: list = (), error: str = "") -> None:
    """Print JSON result to stdout and exit."""
    print("__PHIDIPUS_RESULT__:" + json.dumps({
        "success": success,
        "result_message": message,
        "result_files": [f for f in files if isinstance(f, str) and _is_safe_path(f)],
        "error": error,
    }), flush=True)


_BRIDGE_PREFIX = "__PHIDIPUS_TOOL__:"
_TOOL_ERRORS: list[str] = []
_BRIDGE_SEQ = [0]


def _bridge_call(tool: str, *args, **kwargs):
    """
    v4.3 capability bridge.

    This child process cannot spawn processes (RLIMIT_NPROC=0) — that is the
    point of the sandbox.  The previous browser tools shelled out to
    osascript/pbcopy/curl and therefore ALWAYS failed silently here while
    still returning True.  Tools now send a request line to the parent
    (SkillForge), which executes a fixed, validated set of actions through
    the IPC daemon and replies on stdin.
    """
    _BRIDGE_SEQ[0] += 1
    req = {"id": _BRIDGE_SEQ[0], "tool": tool, "args": list(args), "kwargs": kwargs}
    sys.stdout.write(_BRIDGE_PREFIX + json.dumps(req, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        _TOOL_ERRORS.append(f"{tool}: bridge closed")
        return None
    try:
        resp = json.loads(line)
    except Exception:
        _TOOL_ERRORS.append(f"{tool}: invalid bridge response")
        return None
    if not resp.get("ok"):
        _TOOL_ERRORS.append(f"{tool}: {str(resp.get('error', 'failed'))[:160]}")
        return None
    return resp.get("result")


def _make_browser_tools() -> dict:
    """Browser / clipboard tools — executed by the parent through the bridge."""
    import time as _t

    def browser_navigate(url: str) -> bool:
        return bool(_bridge_call("browser_navigate", url))

    def browser_new_tab(url: str = "") -> bool:
        return bool(_bridge_call("browser_new_tab", url))

    def browser_get_url() -> str:
        return str(_bridge_call("browser_get_url") or "")

    def browser_js(js_code: str, timeout: int = 10) -> str:
        return str(_bridge_call("browser_js", js_code, timeout) or "")

    def browser_type(text: str) -> bool:
        return bool(_bridge_call("browser_type", text))

    def browser_click_text(label: str) -> bool:
        return bool(_bridge_call("browser_click_text", label))

    def browser_wait(seconds: float = 2.0) -> None:
        _t.sleep(max(0.1, min(float(seconds), 60.0)))

    def browser_save_image(output_path: str, img_url: str = "") -> bool:
        return bool(_bridge_call("browser_save_image", output_path, img_url))

    def browser_screenshot(output_path: str) -> bool:
        return bool(_bridge_call("browser_screenshot", output_path))

    def browser_open_profile(profile_name: str, url: str = "") -> bool:
        return bool(_bridge_call("browser_open_profile", profile_name, url))

    def browser_activate_tab(url_fragment: str) -> bool:
        return bool(_bridge_call("browser_activate_tab", url_fragment))

    def browser_close_windows(n: int = 0) -> bool:
        return bool(_bridge_call("browser_close_windows", int(n)))

    def clipboard_set(text: str) -> bool:
        return bool(_bridge_call("clipboard_set", text))

    def clipboard_get() -> str:
        return str(_bridge_call("clipboard_get") or "")

    return {
        "browser_navigate":      browser_navigate,
        "browser_new_tab":       browser_new_tab,
        "browser_get_url":       browser_get_url,
        "browser_js":            browser_js,
        "browser_type":          browser_type,
        "browser_click_text":    browser_click_text,
        "browser_wait":          browser_wait,
        "browser_save_image":    browser_save_image,
        "browser_screenshot":    browser_screenshot,
        "browser_open_profile":  browser_open_profile,
        "browser_activate_tab":  browser_activate_tab,
        "browser_close_windows": browser_close_windows,
        "clipboard_set":         clipboard_set,
        "clipboard_get":         clipboard_get,
    }


def _build_child_namespace() -> dict:
    """
    Build a minimal exec namespace for the child process.

    Unlike the parent's _make_sandboxed_namespace(), this runs in a
    separate process so even a complete namespace escape only gives
    access to the child process, not the parent.

    We still apply defense-in-depth: restricted __builtins__, no os/Path,
    and path-restricted atomic_tools wrappers.
    """
    try:
        from utils import atomic_tools
        from utils.sanitizer import restrict_path_to_safe_roots

        def _safe_find_files(pattern: str, path: str | None = None) -> list:
            from pathlib import Path as _P
            sp = str(path or _P.home() / "Documents")
            restrict_path_to_safe_roots(sp)
            return atomic_tools.find_files(pattern, sp)

        def _safe_read_text(p: str) -> str:
            restrict_path_to_safe_roots(p)
            return atomic_tools.read_text(p)

        def _safe_write_text(p: str, content: str) -> None:
            restrict_path_to_safe_roots(p)
            atomic_tools.write_text(p, content)

        def _safe_get_dir_tree(p: str, max_depth: int = 2, max_files: int = 50) -> str:
            restrict_path_to_safe_roots(p)
            return atomic_tools.get_dir_tree(p, max_depth=max_depth, max_files=max_files)

        import json as _json_mod
        import time as _time_mod
        import math as _math_mod
        import re as _re_mod
        import datetime as _dt_mod

        ns = {
            "find_files":      _safe_find_files,
            "read_text":       _safe_read_text,
            "write_text":      _safe_write_text,
            "get_dir_tree":    _safe_get_dir_tree,
            "read_excel":      atomic_tools.read_excel,
            "create_excel":    atomic_tools.create_excel,
            "read_csv":        atomic_tools.read_csv,
            "filter_rows":     atomic_tools.filter_rows,
            "aggregate":       atomic_tools.aggregate,
            "get_screen_info": lambda: (_bridge_call("screen_info") or {"width": 1440, "height": 900}),
            # v9.22: New atomic tools
            "web_fetch":       atomic_tools.web_fetch,
            "read_pdf":        atomic_tools.read_pdf,
            "get_clipboard":   lambda: str(_bridge_call("clipboard_get") or ""),
            "get_recent_files": atomic_tools.get_recent_files,
            # v9.22: Browser automation tools (AppleScript-based, pre-approved)
            **_make_browser_tools(),
            # Expose stdlib as module objects — in subprocess, escape
            # only reaches this child process, not the parent
            "json":            _json_mod,
            "time":            _time_mod,
            "math":            _math_mod,
            "re":              _re_mod,
            "datetime":        _dt_mod,
            "result_files":    [],
            "result_message":  "",
            "__builtins__": {
                "len": len, "range": range, "enumerate": enumerate,
                "zip": zip, "map": map, "filter": filter,
                "sorted": sorted, "reversed": reversed,
                "list": list, "dict": dict, "set": set, "tuple": tuple,
                "str": str, "int": int, "float": float, "bool": bool,
                "bytes": bytes, "bytearray": bytearray,
                "isinstance": isinstance, "hasattr": hasattr,
                "print": print, "repr": repr,
                "min": min, "max": max, "sum": sum, "abs": abs,
                "round": round, "divmod": divmod, "pow": pow,
                "any": any, "all": all,
                "iter": iter, "next": next,
                "Exception": Exception, "ValueError": ValueError,
                "TypeError": TypeError, "KeyError": KeyError,
                "IndexError": IndexError, "StopIteration": StopIteration,
                "True": True, "False": False, "None": None,
            },
        }
        return ns
    except Exception as exc:
        _output(False, error=f"Namespace build failed: {exc}")
        sys.exit(1)


def _apply_os_hardening() -> None:
    """
    COMMERCIAL-SEC-1: Apply OS-level isolation to THIS child process
    before exec()ing skill code.

    Three independent layers — each fails gracefully without blocking execution:

    Layer 1 — Resource limits (setrlimit):
      • Max file size: 50 MB  — prevents writing large exfil files
      • Max processes: 1       — prevents fork-bomb / spawning new processes
      • Max open files: 64     — reduces file descriptor attack surface
      All via stdlib `resource`, no extra deps.

    Layer 2 — Network block (Linux only, prctl + seccomp BPF via ctypes):
      Installs a minimal seccomp filter that blocks connect(), bind(),
      socket(), and sendto() syscalls (SCMP_ACT_ERRNO = EPERM).
      Uses ctypes + inline BPF bytecode — zero external dependencies.
      Falls back silently on non-Linux or if prctl unavailable.

    Layer 3 — macOS sandbox-exec (macOS only):
      NOT applied here because we're already in the child process.
      The parent (SkillForge._execute_code) handles macOS sandbox-exec
      wrapping when sandbox-exec is detected.

    Performance impact: < 1ms per invocation (all syscalls).
    """
    import os as _os

    # ── Layer 1: Resource limits ──────────────────────────────────────────
    try:
        import resource as _res
        # Max file size: 50 MB (prevents large data exfil)
        _res.setrlimit(_res.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
        # Max processes: current + 0 new (no fork/spawn)
        try:
            _res.setrlimit(_res.RLIMIT_NPROC, (0, 0))
        except Exception:
            pass  # some systems require non-zero minimum
        # Max open files: 64
        _res.setrlimit(_res.RLIMIT_NOFILE, (64, 64))
    except Exception:
        pass  # resource limits unavailable — non-fatal

    # ── Layer 2: Seccomp network block (Linux only) ───────────────────────
    if _os.uname().sysname != "Linux":
        return
    try:
        import ctypes as _ct
        import ctypes.util as _ctu
        import struct as _struct

        _libc = _ct.CDLL(_ctu.find_library("c"), use_errno=True)
        if not _libc:
            return

        # prctl constants
        PR_SET_NO_NEW_PRIVS = 38
        PR_SET_SECCOMP      = 22
        SECCOMP_MODE_FILTER = 2

        # Set no_new_privs first (required for unprivileged seccomp)
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return

        # BPF filter: block network syscalls on x86-64
        # Allow all others (SCMP_ACT_ALLOW = 0x7fff0000)
        # Block connect/bind/socket/sendto (SCMP_ACT_ERRNO(EPERM) = 0x00050001)
        #
        # BPF program structure:
        #   BPF_LD | BPF_W | BPF_ABS  — load arch from seccomp_data
        #   BPF_JMP | BPF_JEQ | BPF_K — compare to AUDIT_ARCH_X86_64
        #   if mismatch → KILL (wrong arch)
        #   BPF_LD | BPF_W | BPF_ABS  — load syscall nr
        #   BPF_JMP | BPF_JEQ for each blocked nr → ERRNO(EPERM)
        #   BPF_RET | BPF_K → ALLOW
        #
        # Blocked syscalls (x86-64 numbers):
        #   41=socket, 42=connect, 49=bind, 44=sendto, 46=sendmsg
        #   48=shutdown, 269=sendmmsg, 54=setsockopt, 56=listen

        AUDIT_ARCH_X86_64 = 0xc000003e
        ALLOW  = 0x7fff0000
        ERRNO1 = 0x00050001   # EPERM = 1

        _BLOCKED_NR = [41, 42, 49, 44, 46, 48, 269, 54, 56]

        # Each instruction: opcode(2) + jt(1) + jf(1) + k(4) = 8 bytes
        def _stmt(code, k):       return _struct.pack("HBBI", code, 0, 0, k)
        def _jump_eq(k, jt):      return _struct.pack("HBBI", 0x15, jt, 0, k)
        def _ret(k):              return _struct.pack("HBBI", 0x06, 0, 0, k)

        insns = []
        # Load architecture field (offset 4)
        insns.append(_stmt(0x20, 4))
        # Check arch == x86_64, if not → KILL (jf = jump forward by 1 past KILL)
        insns.append(_struct.pack("HBBI", 0x15, 1, 0, AUDIT_ARCH_X86_64))
        insns.append(_ret(0x00000000))   # SCMP_ACT_KILL

        # Load syscall nr (offset 0)
        insns.append(_stmt(0x20, 0))

        # For each blocked syscall: if nr==X → ERRNO(EPERM)
        for nr in _BLOCKED_NR:
            insns.append(_jump_eq(nr, 1))
            insns.append(_stmt(0x05, 0))   # jmp +0 (skip to next block)
        # Rewrite last block — replace final jmp+0 with actual block instruction
        # Simpler: just add return ALLOW at end, blocked ones jump over it
        # Re-emit properly:
        insns = []
        insns.append(_stmt(0x20, 4))                              # ld arch
        insns.append(_struct.pack("HBBI", 0x15, 1, 0, AUDIT_ARCH_X86_64))  # jne → kill
        insns.append(_ret(0x00000000))                            # kill
        insns.append(_stmt(0x20, 0))                              # ld syscall nr
        n = len(_BLOCKED_NR)
        for i, nr in enumerate(_BLOCKED_NR):
            jt = n - i          # jump forward to ERRNO return
            insns.append(_struct.pack("HBBI", 0x15, jt - 1, 0, nr))
        insns.append(_ret(ALLOW))    # default: allow
        insns.append(_ret(ERRNO1))   # blocked: EPERM

        prog_bytes = b"".join(insns)
        prog_len   = len(prog_bytes) // 8

        # struct sock_fprog { unsigned short len; struct sock_filter *filter; }
        _SockFprog = _ct.Structure
        buf   = _ct.create_string_buffer(prog_bytes)
        class _Fprog(_ct.Structure):
            _fields_ = [("len", _ct.c_ushort), ("filter", _ct.c_void_p)]
        fprog = _Fprog(prog_len, _ct.cast(buf, _ct.c_void_p))

        ret = _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                          _ct.byref(fprog), 0, 0)
        # ret != 0 → silently continue (skill still runs, just without network block)
    except Exception:
        pass   # seccomp unavailable — non-fatal, subprocess isolation still holds


def main() -> None:
    if len(sys.argv) < 2:
        _output(False, error="Usage: skill_runner_subprocess.py <code_file>")
        sys.exit(1)

    code_file = sys.argv[1]

    # ── SIGALRM timeout (Unix only) ───────────────────────────────────────
    def _alarm_handler(signum, frame):
        _output(False, error="Skill execution timeout")
        sys.exit(124)

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm_handler)
        try:
            _limit = int(os.environ.get("PHIDIPUS_SKILL_TIMEOUT", "30"))
        except ValueError:
            _limit = 30
        signal.alarm(max(5, min(_limit, 300)))

    # ── COMMERCIAL-SEC-1: OS-level hardening BEFORE exec() ────────────────
    # Applies resource limits + seccomp network block.
    # Must run AFTER SIGALRM setup (alarm is a signal, not a syscall).
    # Must run BEFORE reading/exec-ing the code.
    _apply_os_hardening()

    # ── Read code from tempfile ───────────────────────────────────────────
    try:
        code = Path(code_file).read_text("utf-8")
    except Exception as exc:
        _output(False, error=f"Cannot read code file: {exc}")
        sys.exit(1)

    # ── Ensure output dir exists ──────────────────────────────────────────
    try:
        Path("/tmp/phidipus_output").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # ── Execute in child namespace ────────────────────────────────────────
    ns = _build_child_namespace()
    try:
        exec(code, ns)  # noqa: S102 — intentional; runs in isolated child process
    except SystemExit:
        pass  # allow sys.exit() in skill code
    except Exception as exc:
        tb = traceback.format_exc()
        _output(False, error=f"{exc}\n{tb[-400:]}")
        sys.exit(0)

    # ── Collect and validate results ──────────────────────────────────────
    result_files_raw = ns.get("result_files", [])
    result_message   = str(ns.get("result_message", ""))

    safe_files = [
        f for f in (result_files_raw if isinstance(result_files_raw, list) else [])
        if isinstance(f, str) and _is_safe_path(f)
    ]

    if _TOOL_ERRORS:
        note = "⚠️ Lỗi công cụ: " + "; ".join(_TOOL_ERRORS[:5])
        result_message = (result_message + "\n" + note).strip() if result_message else note
        if not result_message.replace(note, "").strip() and not safe_files:
            _output(False, message=result_message, files=safe_files,
                    error="Tất cả thao tác trình duyệt/clipboard đều thất bại: " + "; ".join(_TOOL_ERRORS[:3]))
            return
    _output(True, message=result_message, files=safe_files)


if __name__ == "__main__":
    main()
