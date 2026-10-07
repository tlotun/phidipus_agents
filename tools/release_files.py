# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tools/release_files.py — list the files that belong in a public release.

Uses `git ls-files` when the project is a git repository; otherwise applies
.gitignore with a small matcher (enough for the patterns this project uses:
dir/, *.ext, path/*, !negation).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _rules() -> list[tuple[bool, str]]:
    rules = []
    gi = ROOT / ".gitignore"
    for line in (gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        rules.append((neg, line[1:] if neg else line))
    return rules


def _glob_re(pat: str) -> "re.Pattern[str]":
    """git-style glob: '*' and '?' never cross '/', '**' does."""
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile(out + r"\Z")


def _match(rel: str, is_dir: bool, pattern: str) -> bool:
    dir_only = pattern.endswith("/")
    pat = pattern.rstrip("/")
    if dir_only and not is_dir:
        return False
    if "/" in pat:                       # anchored to the root
        return bool(_glob_re(pat.lstrip("/")).match(rel))
    return bool(_glob_re(pat).match(rel.rsplit("/", 1)[-1]))


def ignored(rel: str, is_dir: bool, rules: list[tuple[bool, str]] | None = None) -> bool:
    rules = _rules() if rules is None else rules
    state = False
    for neg, pat in rules:
        if _match(rel, is_dir, pat):
            state = not neg
    return state


def release_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return sorted(ROOT / p for p in out.stdout.splitlines() if p)
    except (OSError, subprocess.SubprocessError):
        pass
    rules = _rules()
    files: list[Path] = []

    def walk(d: Path, rel_dir: str) -> None:
        for p in sorted(d.iterdir()):
            rel = f"{rel_dir}{p.name}"
            if p.is_symlink():
                continue
            if p.is_dir():
                if p.name == ".git" or ignored(rel, True, rules):
                    continue
                walk(p, rel + "/")
            elif not ignored(rel, False, rules):
                files.append(p)

    walk(ROOT, "")
    return files


if __name__ == "__main__":
    fs = release_files()
    total = sum(f.stat().st_size for f in fs)
    print(f"{len(fs)} files, {total / 1e6:.1f} MB")
