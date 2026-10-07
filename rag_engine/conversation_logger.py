#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
rag_engine/conversation_logger.py — Phidipus AI Forge E6
══════════════════════════════════════════════════════════════

Conversation Logger & Feedback Loop.

Quy tắc #5: Phải có Logger ngay từ ngày đầu deploy.
Sau 2-3 tuần production, đây là training data quý giá nhất.

Log format:
  {
    "id": "uuid8",
    "timestamp": "ISO8601",
    "input": "user query",
    "situation_detected": "price_objection",
    "framework_used": "SPIN",
    "response": "assistant response",
    "hallucinate_check": {"blocked": false, "tier": 0},
    "feedback": "good" | "bad" | null
  }

Feedback Loop:
  python scripts/convert_logs_to_training.py --logs conversation_logs/ --min-feedback good
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Optional


LOG_DIR = Path("conversation_logs")


class ConversationLogger:
    """
    Thread-safe conversation logger với daily rotation.
    """

    def __init__(self, log_dir: str = "conversation_logs", domain: str = "unknown"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.domain = domain

    def _log_file(self) -> Path:
        """Daily log file."""
        today = datetime.date.today().isoformat()
        return self.log_dir / f"{today}.jsonl"

    def log(
        self,
        user_input: str,
        response: str,
        situation_detected: str = "",
        framework_used: str = "",
        hallucinate_check: Optional[dict] = None,
        feedback: Optional[str] = None,      # "good" | "bad" | None
        metadata: Optional[dict] = None,
    ) -> str:
        """
        Log 1 conversation turn.
        Returns: entry ID
        """
        entry_id = str(uuid.uuid4())[:8]
        entry = {
            "id":                  entry_id,
            "timestamp":           datetime.datetime.now().isoformat(),
            "domain":              self.domain,
            "input":               user_input,
            "response":            response,
            "situation_detected":  situation_detected,
            "framework_used":      framework_used,
            "hallucinate_check":   hallucinate_check or {"blocked": False, "tier": 0},
            "feedback":            feedback,
            "metadata":            metadata or {},
        }

        log_file = self._log_file()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return entry_id

    def update_feedback(self, entry_id: str, feedback: str):
        """Update feedback cho 1 entry (tìm trong log ngày hôm nay)."""
        log_file = self._log_file()
        if not log_file.exists():
            return

        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        updated = []
        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get("id") == entry_id:
                    entry["feedback"] = feedback
                updated.append(json.dumps(entry, ensure_ascii=False))
            except Exception:
                updated.append(line)

        log_file.write_text("\n".join(updated) + "\n", encoding="utf-8")

    def get_stats(self, days: int = 7) -> dict:
        """Lấy stats cho N ngày gần nhất."""
        stats = {
            "total": 0, "good": 0, "bad": 0, "no_feedback": 0,
            "hallucinate_blocked": 0, "domains": {}, "situations": {},
        }

        for i in range(days):
            date = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            log_file = self.log_dir / f"{date}.jsonl"
            if not log_file.exists():
                continue

            for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
                try:
                    entry = json.loads(line)
                    stats["total"] += 1
                    fb = entry.get("feedback")
                    if fb == "good":
                        stats["good"] += 1
                    elif fb == "bad":
                        stats["bad"] += 1
                    else:
                        stats["no_feedback"] += 1

                    if entry.get("hallucinate_check", {}).get("blocked"):
                        stats["hallucinate_blocked"] += 1

                    sit = entry.get("situation_detected", "unknown")
                    stats["situations"][sit] = stats["situations"].get(sit, 0) + 1
                except Exception:
                    continue

        return stats


# ══════════════════════════════════════════════════════════════════
# convert_logs_to_training.py (standalone script)
# ══════════════════════════════════════════════════════════════════

def convert_logs_to_training(
    logs_dir: str = "conversation_logs",
    output_file: str = "training_data/from_production.jsonl",
    min_feedback: Optional[str] = "good",     # None = tất cả, "good" = chỉ good
    min_response_len: int = 50,
    system_prompt: str = "Bạn là trợ lý AI hữu ích.",
) -> int:
    """
    Convert conversation logs → training JSONL.
    Returns: số samples được tạo.
    """
    logs_path = Path(logs_dir)
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    samples = []
    for log_file in sorted(logs_path.glob("*.jsonl")):
        for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
            try:
                entry = json.loads(line)

                # Filter
                if min_feedback and entry.get("feedback") != min_feedback:
                    continue
                if entry.get("hallucinate_check", {}).get("blocked"):
                    continue  # Không dùng hallucinate responses làm training data
                if len(entry.get("response", "")) < min_response_len:
                    continue

                sample = {
                    "messages": [
                        {"role": "system",    "content": system_prompt},
                        {"role": "user",      "content": entry["input"]},
                        {"role": "assistant", "content": entry["response"]},
                    ]
                }
                samples.append(sample)
            except Exception:
                continue

    # Deduplicate by user input
    seen = set()
    unique_samples = []
    for s in samples:
        key = s["messages"][1]["content"].strip()[:100]
        if key not in seen:
            seen.add(key)
            unique_samples.append(s)

    with open(out_path, "w", encoding="utf-8") as f:
        for s in unique_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(f"✅ Converted {len(unique_samples)} conversation logs → {output_file}")
    return len(unique_samples)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs",         default="conversation_logs/")
    parser.add_argument("--output",       default="training_data/from_production.jsonl")
    parser.add_argument("--min-feedback", default="good", choices=["good", "bad", "any"])
    parser.add_argument("--system",       default="Bạn là trợ lý AI hữu ích.")
    args = parser.parse_args()

    min_fb = None if args.min_feedback == "any" else args.min_feedback
    count = convert_logs_to_training(
        logs_dir=args.logs,
        output_file=args.output,
        min_feedback=min_fb,
        system_prompt=args.system,
    )
    print(f"Done: {count} training samples từ production logs.")
