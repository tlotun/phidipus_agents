#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
insight_engine/quality_gate.py — Phidipus AI Forge E2.5
══════════════════════════════════════════════════════════════

Quality Gate: Filter situations kém chất lượng trước khi generate dataset.

Đây là bước bắt buộc — thiếu Quality Gate = noise trong training data.

Tiêu chí filter (tất cả phải pass):
  ✅ Trigger phải có (không empty)
  ✅ Context phải có (không empty)
  ✅ customer_psychology phải có ít nhất 1 item
  ✅ example_response >= 50 ký tự (câu trả lời đủ dài)
  ✅ bad_response phải khác example_response
  ✅ Không duplicate trigger với situations đã có
  ✅ Trigger không quá generic (vd: "customer_asks", "user_question")

Target: Giữ lại 60-80% situations, loại 20-40% kém chất lượng.

Chạy:
  python insight_engine/quality_gate.py --input insights/raw/ --output insights/validated/
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")


# ══════════════════════════════════════════════════════════════════
# Quality criteria
# ══════════════════════════════════════════════════════════════════

GENERIC_TRIGGERS = {
    "customer_asks", "user_question", "general_inquiry", "customer_question",
    "user_asks", "question", "inquiry", "request", "unknown", "other",
    "situation", "case", "scenario",
}

TOO_GENERIC_PATTERNS = [
    r"^(ask|tell|say|want|need|get|have|make|take|give|know|find|go|come|see|think|look)$",
    r"^(customer|user|client|person|people)$",
    r"^(question|answer|response|message|text)$",
]


def score_situation(sit: dict) -> tuple[bool, list[str]]:
    """
    Score 1 situation. Returns (pass, [reasons_for_fail]).
    """
    fails = []

    # 1. Trigger không empty
    trigger = sit.get("trigger", "").strip()
    if not trigger:
        fails.append("trigger: empty")
    elif trigger.lower() in GENERIC_TRIGGERS:
        fails.append(f"trigger: quá generic '{trigger}'")
    elif len(trigger) < 5:
        fails.append(f"trigger: quá ngắn '{trigger}'")
    else:
        # Check generic patterns
        for pat in TOO_GENERIC_PATTERNS:
            if re.match(pat, trigger, re.IGNORECASE):
                fails.append(f"trigger: generic pattern '{trigger}'")
                break

    # 2. Context không empty
    context = sit.get("context", "").strip()
    if not context or len(context) < 15:
        fails.append(f"context: quá ngắn ({len(context)} chars)")

    # 3. Psychology phải có ít nhất 1 item có ý nghĩa
    psychology = sit.get("customer_psychology", [])
    if not psychology:
        fails.append("customer_psychology: empty")
    elif all(len(p.strip()) < 5 for p in psychology):
        fails.append("customer_psychology: tất cả items quá ngắn")

    # 4. example_response đủ dài
    example = sit.get("example_response", "").strip()
    if len(example) < 50:
        fails.append(f"example_response: quá ngắn ({len(example)} chars, cần ≥50)")

    # 5. bad_response phải khác example_response
    bad = sit.get("bad_response", "").strip()
    if bad and example and bad.lower().strip() == example.lower().strip():
        fails.append("bad_response: giống hệt example_response")

    # 6. Strategy không empty
    strategy = sit.get("recommended_strategy", "").strip()
    if not strategy:
        fails.append("recommended_strategy: empty")

    return len(fails) == 0, fails


def dedup_situations(situations: list[dict]) -> list[dict]:
    """
    Loại bỏ duplicate triggers (case-insensitive).
    Khi trùng trigger, giữ cái có example_response dài hơn.
    """
    seen_triggers: dict[str, dict] = {}

    for sit in situations:
        trigger = sit.get("trigger", "").lower().strip()
        if not trigger:
            continue

        if trigger not in seen_triggers:
            seen_triggers[trigger] = sit
        else:
            # Giữ cái có example_response dài hơn
            existing_len = len(seen_triggers[trigger].get("example_response", ""))
            current_len = len(sit.get("example_response", ""))
            if current_len > existing_len:
                seen_triggers[trigger] = sit

    return list(seen_triggers.values())


# ══════════════════════════════════════════════════════════════════
# Main filter function
# ══════════════════════════════════════════════════════════════════

def filter_situations(
    raw_dir: str = "insights/raw",
    out_dir: str = "insights/validated",
    verbose: bool = False,
) -> dict:
    """
    Filter tất cả situation JSONs.
    Returns: stats dict.
    """
    in_path = Path(raw_dir)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    json_files = sorted(in_path.glob("*.json"))
    if not json_files:
        warn(f"Không tìm thấy JSON files trong {raw_dir}")
        return {}

    info(f"Quality Gate: {len(json_files)} files | Input: {raw_dir}")
    print()

    grand_total = 0
    grand_kept = 0
    all_valid_situations = []
    fail_reasons: dict[str, int] = {}

    for f in json_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            situations = data.get("situations", [])
            source = data.get("source", f.name)
        except Exception:
            warn(f"Cannot read: {f.name}")
            continue

        grand_total += len(situations)
        valid = []
        failed = []

        for sit in situations:
            passed, reasons = score_situation(sit)
            if passed:
                valid.append(sit)
            else:
                failed.append((sit, reasons))
                for r in reasons:
                    # Lấy category của lý do
                    cat = r.split(":")[0].strip()
                    fail_reasons[cat] = fail_reasons.get(cat, 0) + 1

        # Dedup trong file
        valid = dedup_situations(valid)
        grand_kept += len(valid)
        all_valid_situations.extend(valid)

        rate = len(valid) / len(situations) * 100 if situations else 0
        status = "✅" if rate >= 60 else "⚠️"
        print(f"  {status} {f.name[:50]:<50} {len(valid)}/{len(situations)} ({rate:.0f}%)")

        if verbose and failed:
            for sit, reasons in failed[:3]:
                print(f"     ❌ '{sit.get('trigger', '?')}': {', '.join(reasons)}")

        # Save validated
        out_file = out_path / f.name
        out_file.write_text(
            json.dumps({"source": source, "situations": valid},
                       ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    # Global dedup across all files
    all_valid_situations = dedup_situations(all_valid_situations)

    # Save merged file
    merged_path = out_path / "_all_situations_merged.json"
    merged_path.write_text(
        json.dumps({"total": len(all_valid_situations), "situations": all_valid_situations},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    overall_rate = grand_kept / grand_total * 100 if grand_total else 0

    print()
    print("═" * 60)
    ok(f"Quality Gate Result: {grand_kept}/{grand_total} situations passed ({overall_rate:.1f}%)")
    ok(f"After global dedup: {len(all_valid_situations)} unique situations")

    if fail_reasons:
        print("\nTop fail reasons:")
        for reason, count in sorted(fail_reasons.items(), key=lambda x: -x[1])[:5]:
            print(f"  {count:3d}x  {reason}")

    print()
    if overall_rate < 50:
        warn(f"⚠️  Pass rate {overall_rate:.1f}% thấp! Xem xét cải thiện extraction prompt.")
    elif overall_rate >= 70:
        ok(f"✅ Pass rate {overall_rate:.1f}% — chất lượng tốt!")
    else:
        info(f"ℹ  Pass rate {overall_rate:.1f}% — acceptable.")

    warn("Tiếp theo: python scripts/generate_domain_dataset.py --input insights/validated/")
    print("═" * 60)

    stats = {
        "total_raw": grand_total,
        "total_kept": len(all_valid_situations),
        "pass_rate": round(overall_rate / 100, 3),
        "fail_reasons": fail_reasons,
        "merged_output": str(merged_path),
    }

    (out_path / "_quality_gate_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return stats


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Quality Gate — Phidipus AI Forge E2.5"
    )
    parser.add_argument("--input",   default="insights/raw/")
    parser.add_argument("--output",  default="insights/validated/")
    parser.add_argument("--verbose", action="store_true",
                        help="Hiện chi tiết fail reasons")
    args = parser.parse_args()

    filter_situations(args.input, args.output, args.verbose)


if __name__ == "__main__":
    main()
