#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tools/check_secrets.py — block secrets and personal data from reaching GitHub
═══════════════════════════════════════════════════════════════════════════════

    ./venv/bin/python tools/check_secrets.py            # scan files that would be published
    ./venv/bin/python tools/check_secrets.py --staged   # scan `git diff --cached` (pre-commit hook)
    ./venv/bin/python tools/check_secrets.py --install-hook

Detects API keys / tokens / private keys / passwords (memory/secret_filter.py
patterns), files that must never be published (config.yaml, keys/, bot
configs, license signing keys, databases) and absolute home-directory paths
that reveal your user name.  Exit code 1 when something is found.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# never publish these, whatever their content
FORBIDDEN = [
    "config.yaml", "keys/*", "*.priv", "*.key", "*.pem", "*.p12", "data/license.json",
    "telegram/bot_config.json", "wechat/wechat_config.json", "*.sqlite", "*.sqlite3", "*.db",
    ".env", ".env.*", "install.log", "logs/*", ".phidipus_state.json", "data/memory/*",
]
# directories that are never part of a release
SKIP_DIRS = {".git", "venv", ".venv", "__pycache__", ".backup", ".tools", "node_modules",
             "models", "adapters_v7", "logs", "dist", ".claude", ".pytest_cache"}
TEXT_EXT = {".py", ".md", ".txt", ".yaml", ".yml", ".json", ".sh", ".command", ".html", ".js",
            ".css", ".toml", ".cfg", ".ini", ".jinja", ".plist", ".applescript", ""}
_HOME_RE = re.compile(r"/Users/(?!Shared/|\{|\$|<|USER|you|me/|\.\.\.)[A-Za-z0-9._-]+/")


def published_files() -> list[Path]:
    """Files git would publish (respects .gitignore) or a filesystem walk."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return [ROOT / p for p in out.stdout.splitlines() if p]
    except (OSError, subprocess.SubprocessError):
        pass
    from tools.release_files import release_files
    return release_files()


def staged_files() -> list[Path]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                         cwd=ROOT, capture_output=True, text=True, timeout=30)
    return [ROOT / p for p in out.stdout.splitlines() if p]


_STRONG = {"private_key", "anthropic_key", "openai_key", "google_key", "github_token", "slack_token",
           "aws_key", "telegram_token", "jwt", "url_credentials"}
# a quoted literal assigned to a secret-looking name (code / JSON / JS)
_LITERAL = re.compile(r"""(?i)(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|bearer)["']?\s*[:=]\s*["']([^"'\s]{12,})["']""")
# YAML / .env style: key: value
_KV = re.compile(r"(?i)^\s*[\w.-]*(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*([^#\s]+)")
_PLACEHOLDER = re.compile(r"(?i)^(?:[\"']{2}|<.*>|\$\{.*\}|x{3,}|\.{3}|none|null|true|false|changeme|your[_-].*|"
                          r".*\.\.\..*|[a-z_]+\.[a-z_]+.*|scrypt\$\.\.\.)$")


def _looks_secret(value: str) -> bool:
    v = value.strip().strip("\"'")
    if len(v) < 8 or _PLACEHOLDER.match(v) or v.upper() == v and "_" in v:
        return False
    classes = sum(bool(re.search(p, v)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2 and len(set(v)) >= 6


def scan(files: list[Path]) -> list[str]:
    from memory.secret_filter import _PATTERNS
    strong = [(label, rx) for label, rx in _PATTERNS if label in _STRONG]
    problems: list[str] = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in Path(rel).parts[:-1]):
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in FORBIDDEN):
            if not rel.endswith((".example.json", ".example.yaml")):
                problems.append(f"{rel}: file này không bao giờ được đưa lên (chứa bí mật/dữ liệu cá nhân)")
            continue
        if f.suffix.lower() not in TEXT_EXT or not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        is_conf = f.suffix.lower() in (".yaml", ".yml", ".env", ".ini", ".cfg", ".toml")
        fixture = Path(rel).name.startswith("test_") or rel.startswith("tests/") or "secret_filter" in rel
        for no, line in enumerate(text.splitlines(), 1):
            hits = [label for label, rx in strong if rx.search(line)]
            if fixture:
                # fake credentials are fine in tests, but not in provider format:
                # GitHub secret scanning raises a public alert for those too
                if hits:
                    problems.append(f"{rel}:{no}: chuỗi có định dạng khoá thật ({', '.join(sorted(set(hits)))}) — "
                                    "GitHub sẽ cảnh báo dù là khoá giả; hãy ghép chuỗi lúc chạy")
            else:
                m = _LITERAL.search(line)
                if m and _looks_secret(m.group(1)):
                    hits.append("literal")
                if is_conf:
                    kv = _KV.match(line)
                    if kv and _looks_secret(kv.group(1)) or (kv and kv.group(1).strip("\"'") in ("phidipus", "admin", "password")):
                        hits.append("config value")
                if hits:
                    problems.append(f"{rel}:{no}: có thể chứa bí mật ({', '.join(sorted(set(hits)))})")
            if _HOME_RE.search(line):
                problems.append(f"{rel}:{no}: đường dẫn tuyệt đối lộ tên người dùng")
    return problems


def install_hook() -> int:
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    if not hook.parent.exists():
        print("Chưa có .git — chạy `git init` trước.")
        return 1
    hook.write_text("#!/bin/sh\nexec ./venv/bin/python tools/check_secrets.py --staged\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    print(f"Đã cài hook: {hook}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--install-hook", action="store_true")
    a = ap.parse_args(argv)
    if a.install_hook:
        return install_hook()
    files = staged_files() if a.staged else published_files()
    problems = scan(files)
    for p in problems:
        print("✗", p)
    if problems:
        print(f"\n{len(problems)} vấn đề — sửa hoặc thêm vào .gitignore trước khi đưa lên GitHub.")
        return 1
    print(f"✓ Không thấy bí mật trong {len(files)} file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
