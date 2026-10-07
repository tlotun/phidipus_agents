#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
scripts/convert_logs_to_training.py — Phidipus AI Forge E6
═══════════════════════════════════════════════════════════════════

Convert production conversation logs → training data (JSONL).
Feedback loop: logs with 'good' feedback become new training samples.

Usage:
  python scripts/convert_logs_to_training.py --logs conversation_logs/ --output training_data/from_production.jsonl
  python scripts/convert_logs_to_training.py --logs conversation_logs/ --min-feedback good --days 7
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")


def load_logs(log_dir: str, days: int = 0) -> list[dict]:
    """Load conversation logs from JSONL files."""
    log_path = Path(log_dir)
    if not log_path.exists():
        warn(f"Log directory not found: {log_dir}")
        return []

    cutoff = None
    if days > 0:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    entries = []
    for f in sorted(log_path.glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if cutoff and entry.get("timestamp", "") < cutoff:
                    continue
                entries.append(entry)
        except (json.JSONDecodeError, Exception) as exc:
            warn(f"Error reading {f.name}: {exc}")

    return entries


def filter_entries(
    entries: list[dict],
    min_feedback: str = "good",
    require_situation: bool = True,
) -> list[dict]:
    """Filter log entries by quality criteria."""
    feedback_levels = {"good": 2, "neutral": 1, "bad": 0, None: -1}

    filtered = []
    for entry in entries:
        fb = entry.get("feedback")
        fb_score = feedback_levels.get(fb, -1)
        min_score = feedback_levels.get(min_feedback, 0)

        if fb_score < min_score:
            continue

        if require_situation and not entry.get("situation_detected"):
            continue

        hallucinate = entry.get("hallucinate", {})
        if isinstance(hallucinate, dict) and hallucinate.get("hallucinate"):
            continue

        user_input = entry.get("input", "").strip()
        response = entry.get("response", "").strip()
        if len(user_input) < 10 or len(response) < 30:
            continue

        filtered.append(entry)

    return filtered


def convert_to_chatml(
    entries: list[dict],
    domain_config: dict | None = None,
) -> list[dict]:
    """Convert filtered log entries to ChatML JSONL format."""
    persona = "Bạn là trợ lý AI chuyên nghiệp, hữu ích và chính xác."
    if domain_config:
        persona = domain_config.get("dataset", {}).get("persona", persona)

    samples = []
    seen_inputs = set()

    for entry in entries:
        user_input = entry.get("input", "").strip()
        response = entry.get("response", "").strip()

        input_hash = hash(user_input[:100])
        if input_hash in seen_inputs:
            continue
        seen_inputs.add(input_hash)

        situation = entry.get("situation_detected", "")
        framework = entry.get("framework_used", "")

        system_parts = [persona]
        if situation:
            system_parts.append(f"Tình huống: {situation}")
        if framework:
            system_parts.append(f"Framework: {framework}")

        sample = {
            "messages": [
                {"role": "system", "content": "\n".join(system_parts)},
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": response},
            ]
        }
        samples.append(sample)

    return samples


def main():
    parser = argparse.ArgumentParser(
        description="Convert conversation logs to training data",
    )
    parser.add_argument("--logs", default="conversation_logs/",
                        help="Directory containing .jsonl log files")
    parser.add_argument("--output", "-o", default="training_data/from_production.jsonl",
                        help="Output JSONL file path")
    parser.add_argument("--min-feedback", default="good",
                        choices=["good", "neutral", "bad"],
                        help="Minimum feedback level to include")
    parser.add_argument("--days", type=int, default=0,
                        help="Only process logs from last N days (0=all)")
    parser.add_argument("--domain", default=None,
                        help="Domain config YAML for persona")
    parser.add_argument("--append", action="store_true",
                        help="Append to existing output file")
    args = parser.parse_args()

    info(f"Loading logs from: {args.logs}")
    entries = load_logs(args.logs, args.days)
    info(f"Loaded {len(entries)} log entries")

    filtered = filter_entries(entries, args.min_feedback)
    info(f"After filtering: {len(filtered)} entries (min_feedback={args.min_feedback})")

    domain_config = None
    if args.domain:
        try:
            import yaml
            domain_config = yaml.safe_load(Path(args.domain).read_text(encoding="utf-8"))
        except Exception as exc:
            warn(f"Could not load domain config: {exc}")

    samples = convert_to_chatml(filtered, domain_config)
    info(f"Generated {len(samples)} training samples")

    if not samples:
        warn("No samples to write. Check logs and feedback criteria.")
        return

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    mode = "a" if args.append else "w"
    existing = 0
    if args.append and output_path.exists():
        existing = sum(1 for _ in output_path.read_text(encoding="utf-8").strip().split("\n")
                       if _.strip())

    with open(output_path, mode, encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    total = existing + len(samples) if args.append else len(samples)
    ok(f"Written to {output_path}: {len(samples)} new samples (total: {total})")

    feedback_dist = {}
    for e in filtered:
        fb = e.get("feedback", "none")
        feedback_dist[fb] = feedback_dist.get(fb, 0) + 1
    info(f"Feedback distribution: {feedback_dist}")

    situation_dist = {}
    for e in filtered:
        sit = e.get("situation_detected", "unknown")
        situation_dist[sit] = situation_dist.get(sit, 0) + 1
    top_situations = sorted(situation_dist.items(), key=lambda x: x[1], reverse=True)[:5]
    info(f"Top situations: {top_situations}")


if __name__ == "__main__":
    main()
