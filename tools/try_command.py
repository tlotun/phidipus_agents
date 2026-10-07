#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
tools/try_command.py — how would Phidipus Agents run this command? (dry run)
═══════════════════════════════════════════════════════════════════════════════

    ./venv/bin/python tools/try_command.py "mở tab mới trong chrome"
    ./venv/bin/python tools/try_command.py --app finder "đi tới thư mục ~/Downloads"
    ./venv/bin/python tools/try_command.py --app preview          # interactive

Looks the command up in data/shortcuts/macos.yaml exactly like the keyboard-first
layer does, but presses nothing: no Accessibility / Screen Recording permission,
Ollama or running agent needed. Handy when adding shortcuts (see CONTRIBUTING.md).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.keyboard_first import KeyboardFirst, ShortcutCatalog, UIContext, fold  # noqa: E402


def _step_text(value: object, params: dict[str, str]) -> str:
    if isinstance(value, list):     # menu step: alternative paths
        return " | ".join(" › ".join(p) for p in value)
    text = str(value)
    for k, v in params.items():
        text = text.replace("{" + k + "}", v)
    return text


def explain(catalog: ShortcutCatalog, command: str, app_key: str = "") -> str:
    ui = UIContext()
    if app_key:
        ui = UIContext(ok=True, bundle_id=catalog.app_bundle(app_key), app=catalog.app_name(app_key))
    started = time.perf_counter()
    m = catalog.find(command, ui)
    ms = (time.perf_counter() - started) * 1000
    if m is None:
        menu = KeyboardFirst._MENU_CMD.match(fold(command))
        if menu:
            return (f"☰  lệnh menu \"{menu.group(1)}\" → agent đọc thanh menu của ứng dụng qua "
                    f"Accessibility rồi chọn mục khớp nhất [{ms:.1f} ms]")
        return (f"✗  không có trong danh mục phím tắt → agent chuyển sang các tầng sau "
                f"(Brain / workflow / ReAct) [{ms:.1f} ms]")
    sc = m.shortcut
    where = catalog.app_name(m.app_key) if m.app_key else (ui.app or "ứng dụng đang mở")
    lines = []
    if sc.is_macro:
        params = ", ".join(f'{k}="{v}"' for k, v in m.params.items())
        lines.append(f"⌨  macro {sc.qualified}({params}) · {where}")
        for step in sc.steps:
            for kind, value in step.items():
                lines.append(f"     {kind:<15}{_step_text(value, m.params)}")
    else:
        lines.append(f"⌨  {sc.qualified}  {sc.pretty_keys()}  · {where} — {sc.desc}")
        for path in sc.menu:
            lines.append(f"     dự phòng: menu {' › '.join(path)}")
    if sc.risk in ("high", "critical"):
        lines.append(f"     ⚠  risk={sc.risk}: agent hỏi bạn xác nhận trước khi làm")
    if sc.expect:
        lines.append(f"     kiểm tra kết quả: {sc.expect}")
    lines.append(f"     [{ms:.1f} ms · không cần AI nhìn màn hình]")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dry-run a command against the keyboard-first catalog")
    ap.add_argument("command", nargs="*", help="Vietnamese or English command")
    ap.add_argument("--app", default="", help="app in front, e.g. chrome, finder, preview, mail")
    a = ap.parse_args(argv)
    catalog = ShortcutCatalog()
    if a.app and a.app not in catalog.apps:
        print(f"Không biết app '{a.app}'. Có: {', '.join(sorted(catalog.apps))}")
        return 2
    if a.command:
        print(explain(catalog, " ".join(a.command), a.app))
        return 0
    print(f"Gõ lệnh (Enter trống để thoát){' — app: ' + catalog.app_name(a.app) if a.app else ''}")
    while True:
        try:
            line = input("lệnh> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            return 0
        print(explain(catalog, line, a.app))


if __name__ == "__main__":
    sys.exit(main())
