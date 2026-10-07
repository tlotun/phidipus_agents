#!/usr/bin/env python3
"""
tools/add_spdx_headers.py — add license headers to Phidipus source files (idempotent)

    # SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
    # Copyright (c) 2026 Phidipus Agents (see NOTICE)

Inserted after a shebang / coding line; files that already carry an SPDX
identifier are left untouched.  Only files that belong to the release are
changed (tools/release_files.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HEADER = ["# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0",
          "# Copyright (c) 2026 Phidipus Agents (see NOTICE)"]
EXTS = {".py", ".sh", ".command"}


def add_header(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    head = text.splitlines()[:6]
    if any("SPDX-License-Identifier" in line for line in head):
        return False
    lines = text.splitlines(keepends=True)
    idx = 0
    if lines and lines[0].startswith("#!"):
        idx = 1
    if len(lines) > idx and "coding" in lines[idx] and lines[idx].lstrip().startswith("#"):
        idx += 1
    nl = "\n"
    lines[idx:idx] = [h + nl for h in HEADER]
    path.write_text("".join(lines), encoding="utf-8")
    return True


def main() -> int:
    from tools.release_files import release_files
    changed = 0
    for f in release_files():
        if f.suffix in EXTS and f.stat().st_size > 0:
            changed += add_header(f)
    print(f"Đã thêm header SPDX vào {changed} file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
