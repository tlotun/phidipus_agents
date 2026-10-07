#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
data_engine/data_cleaner.py — Phidipus AI Forge E1
════════════════════════════════════════════════════════

Data Cleaner: Deduplicate + Normalize toàn bộ raw corpus.

Vấn đề hay gặp với raw corpus:
  - Duplicate paragraphs từ captions bị loop
  - Boilerplate: "Subscribe kênh", "Like video", quảng cáo
  - Câu quá ngắn (< 5 từ) hoặc quá dài (> 200 từ)
  - Mix tiếng Anh quá nhiều → lọc bỏ
  - Ký tự rác: HTML entities, emoji, timestamps

Output: cleaned/ thư mục với text sạch, ready cho E2 Insight Engine.

Chạy:
  python data_engine/data_cleaner.py --input raw_corpus/cleaned/ --output corpus/final/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterator


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")


# ══════════════════════════════════════════════════════════════════
# Noise patterns (bỏ đi)
# ══════════════════════════════════════════════════════════════════

BOILERPLATE_PATTERNS = [
    r"subscribe.*kênh",
    r"like.*video",
    r"đăng ký.*kênh",
    r"nhấn chuông",
    r"comment.*bên dưới",
    r"xem video.*tiếp theo",
    r"link.*mô tả",
    r"tài trợ.*bởi",
    r"quảng cáo",
    r"click.*link",
    r"affiliate",
    r"\[music\]",
    r"\[applause\]",
    r"\[laughter\]",
    r"http[s]?://",
    r"www\.",
    r"@\w+",             # @mentions
    r"#\w+",             # hashtags (cuối video)
]

BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PATTERNS), re.IGNORECASE)


def is_predominantly_vietnamese(text: str, min_ratio: float = 0.30) -> bool:
    """
    Check text có đủ tiếng Việt không (dựa vào ký tự có dấu).
    """
    vn_chars = set("àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
                   "ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ")
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return False
    vn_count = sum(1 for c in alpha_chars if c in vn_chars)
    return (vn_count / len(alpha_chars)) >= min_ratio


def clean_text(text: str) -> str:
    """
    Làm sạch 1 đoạn text:
    1. Normalize Unicode (NFC)
    2. Bỏ HTML entities, timestamps
    3. Normalize whitespace
    4. Capitalize câu đầu
    """
    # Unicode normalize
    text = unicodedata.normalize("NFC", text)

    # HTML entities
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)

    # Timestamps (00:00:00, 1:23:45)
    text = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\b", " ", text)

    # Emoji / special symbols (giữ dấu câu cơ bản)
    text = re.sub(r"[^\w\s\.\,\!\?\:\;\-\(\)\[\]\"\'\u00C0-\u024F\u1E00-\u1EFF]", " ", text)

    # Multiple spaces → single
    text = re.sub(r"\s+", " ", text).strip()

    # Capitalize nếu chưa
    if text and text[0].islower():
        text = text[0].upper() + text[1:]

    return text


def split_into_paragraphs(text: str, min_words: int = 10, max_words: int = 300) -> list[str]:
    """
    Split text thành paragraphs có kích thước phù hợp cho training.
    Ghép các câu ngắn lại với nhau.
    """
    # Split by newlines or ". " as sentence boundary
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    sentences = [s.strip() for s in sentences if s.strip()]

    paragraphs = []
    current = []
    current_words = 0

    for sent in sentences:
        words = len(sent.split())
        if words < 3:  # Skip câu quá ngắn
            continue
        if BOILERPLATE_RE.search(sent):  # Skip boilerplate
            continue

        current.append(sent)
        current_words += words

        if current_words >= min_words:
            para = " ".join(current)
            para_words = len(para.split())
            if min_words <= para_words <= max_words:
                paragraphs.append(para)
            current = []
            current_words = 0

    # Flush remainder
    if current and current_words >= min_words:
        paragraphs.append(" ".join(current))

    return paragraphs


def dedup_by_hash(paragraphs: list[str]) -> list[str]:
    """Loại bỏ paragraphs trùng lặp (exact hash)."""
    seen = set()
    unique = []
    for p in paragraphs:
        h = hashlib.md5(p.lower().encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            unique.append(p)
    return unique


def near_dedup_by_prefix(paragraphs: list[str], prefix_len: int = 50) -> list[str]:
    """Near-dedup: loại bỏ paragraphs có cùng 50 ký tự đầu."""
    seen_prefixes = set()
    unique = []
    for p in paragraphs:
        prefix = p[:prefix_len].lower().strip()
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            unique.append(p)
    return unique


def process_file(
    input_file: Path,
    output_file: Path,
    stats: dict,
) -> bool:
    """Process 1 text file."""
    try:
        raw_text = input_file.read_text(encoding="utf-8", errors="ignore")
        if not raw_text.strip():
            stats["empty"] += 1
            return False

        # Check tiếng Việt
        if not is_predominantly_vietnamese(raw_text):
            warn(f"   Ít tiếng Việt: {input_file.name} — skip")
            stats["non_vn"] += 1
            return False

        cleaned_text = clean_text(raw_text)
        paragraphs = split_into_paragraphs(cleaned_text)
        paragraphs = dedup_by_hash(paragraphs)
        paragraphs = near_dedup_by_prefix(paragraphs)

        if not paragraphs:
            stats["empty"] += 1
            return False

        # Thống kê
        stats["input_words"] += len(raw_text.split())
        stats["output_words"] += sum(len(p.split()) for p in paragraphs)
        stats["paragraphs"] += len(paragraphs)

        output_file.write_text("\n\n".join(paragraphs), encoding="utf-8")
        return True

    except Exception as e:
        warn(f"   Error {input_file.name}: {e}")
        stats["errors"] += 1
        return False


def clean_corpus(
    input_dir: str,
    output_dir: str,
    resume: bool = True,
):
    """Batch clean toàn bộ corpus."""
    in_path = Path(input_dir)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    txt_files = sorted(list(in_path.glob("*.txt")) + list(in_path.rglob("*_extracted.txt")))

    if not txt_files:
        warn(f"Không tìm thấy .txt files trong {input_dir}")
        return

    info(f"Corpus Cleaner: {len(txt_files)} files")
    info(f"Input: {input_dir} → Output: {output_dir}")
    print()

    stats = {
        "total": len(txt_files),
        "success": 0, "skipped": 0, "empty": 0,
        "non_vn": 0, "errors": 0,
        "input_words": 0, "output_words": 0, "paragraphs": 0,
    }

    for i, f in enumerate(txt_files, 1):
        out_f = out_path / f.name
        print(f"[{i}/{len(txt_files)}] {f.name[:50]}", end=" ")

        if resume and out_f.exists():
            print("⏭  skip")
            stats["skipped"] += 1
            continue

        ok_flag = process_file(f, out_f, stats)
        if ok_flag:
            words_out = len(out_f.read_text(encoding="utf-8").split())
            print(f"✓  {words_out:,}w")
            stats["success"] += 1
        else:
            print("✗  skip")

    # Summary
    print()
    print("═" * 55)
    ok(f"Success: {stats['success']}/{stats['total']}")
    ok(f"Words: {stats['input_words']:,} → {stats['output_words']:,} ({100*stats['output_words']//max(stats['input_words'],1)}% kept)")
    ok(f"Paragraphs: {stats['paragraphs']:,}")
    if stats["non_vn"]:
        warn(f"Non-Vietnamese skip: {stats['non_vn']}")
    print("═" * 55)

    # Save stats
    (out_path / "_corpus_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    ok(f"Corpus ready: {output_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="Data Cleaner — Phidipus AI Forge E1"
    )
    parser.add_argument("--input",     default="raw_corpus/cleaned/")
    parser.add_argument("--output",    default="corpus/final/")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    clean_corpus(args.input, args.output, resume=not args.no_resume)


if __name__ == "__main__":
    main()
