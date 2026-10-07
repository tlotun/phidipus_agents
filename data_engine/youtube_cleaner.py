#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
data_engine/youtube_cleaner.py — Phidipus AI Forge E1
══════════════════════════════════════════════════════════

YouTube Auto-Caption Cleaner: Dùng Claude để sửa lỗi dấu tiếng Việt.

VẤN ĐỀ: Auto-caption YouTube tiếng Việt lỗi dấu ~30-40%
VÍ DỤ lỗi:
  "khach hang khong biet minh can gi" → "khách hàng không biết mình cần gì"
  "ban hang la ki nang quan trong" → "bán hàng là kỹ năng quan trọng"

CÁCH DÙNG:
  - Ưu tiên: Dùng whisper_transcriber.py (offline, tốt hơn)
  - Fallback: Dùng file này khi chỉ có auto-caption

Chi phí: ~$0.002/video (claude-haiku-4-5)

Chạy:
  python data_engine/youtube_cleaner.py --input raw_corpus/youtube/ --output raw_corpus/cleaned/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")
def fail(msg: str) -> None: print(f"{_c('0;31', '[FAIL]')}  {msg}")


# ══════════════════════════════════════════════════════════════════
# Pre-clean: rule-based fixes không cần LLM
# ══════════════════════════════════════════════════════════════════

COMMON_FIXES = {
    # Lỗi phổ biến từ auto-caption
    r"\bkhach hang\b": "khách hàng",
    r"\bban hang\b": "bán hàng",
    r"\bki nang\b": "kỹ năng",
    r"\bchot sale\b": "chốt sale",
    r"\btu choi\b": "từ chối",
    r"\btam ly\b": "tâm lý",
    r"\bquyet dinh\b": "quyết định",
    r"\bsan pham\b": "sản phẩm",
    r"\bdich vu\b": "dịch vụ",
    r"\bdoanh thu\b": "doanh thu",
    r"\bkhong co\b": "không có",
    r"\bkhong biet\b": "không biết",
    r"\bchien luoc\b": "chiến lược",
    r"\bgio hang\b": "giỏ hàng",
    r"\bdon hang\b": "đơn hàng",
}


def rule_based_clean(text: str) -> str:
    """Sửa các lỗi phổ biến bằng regex (nhanh, miễn phí)."""
    result = text
    for pattern, replacement in COMMON_FIXES.items():
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


def detect_error_rate(text: str) -> float:
    """
    Ước tính tỷ lệ lỗi dấu bằng cách đếm từ không có dấu.
    Nếu > 20% từ không có dấu → cần clean.
    """
    words = text.split()
    if not words:
        return 0.0

    # Từ tiếng Việt có dấu thường có các ký tự đặc biệt
    vn_chars = set("àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
                   "ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ")
    # Bỏ qua từ tiếng Anh/số
    vn_words = [w for w in words if re.search(r"[a-zđ]", w, re.IGNORECASE)
                and not re.match(r"^\d+$", w) and len(w) > 1]
    if not vn_words:
        return 0.0

    no_diacritic = [w for w in vn_words if not any(c in vn_chars for c in w)]
    return len(no_diacritic) / len(vn_words)


# ══════════════════════════════════════════════════════════════════
# Claude-powered clean
# ══════════════════════════════════════════════════════════════════

CLEAN_SYSTEM_PROMPT = """Bạn là chuyên gia tiếng Việt. Nhiệm vụ: sửa transcript YouTube bị lỗi dấu.

QUY TẮC:
1. Chỉ thêm dấu tiếng Việt (thanh điệu, dấu nguyên âm) — KHÔNG thay đổi từ ngữ
2. Giữ nguyên 100% cấu trúc câu, thứ tự từ, ý nghĩa
3. Giữ nguyên tên riêng, thuật ngữ tiếng Anh
4. Bỏ ký tự rác (■□▪▫●○) nhưng giữ dấu câu
5. Bỏ các đoạn lặp lại y chang nhau (caption artifact)
6. Output: chỉ text đã clean, KHÔNG giải thích, KHÔNG markdown

VÍ DỤ:
Input: "khach hang khong biet minh can gi nen nguoi ban hang phai kham pha"
Output: "Khách hàng không biết mình cần gì nên người bán hàng phải khám phá"
"""


def claude_clean_chunk(text: str, client) -> Optional[str]:
    """Dùng Claude Haiku để clean 1 chunk text."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            system=CLEAN_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Sửa dấu transcript này:\n\n{text}"
            }]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        warn(f"Claude API error: {e}")
        return None


def clean_transcript_with_claude(
    text: str,
    client,
    chunk_words: int = 600,
) -> str:
    """
    Clean toàn bộ transcript:
    1. Rule-based pre-clean (nhanh)
    2. Detect nếu vẫn còn nhiều lỗi → Claude clean
    3. Chunk text để tránh context limit
    """
    # Step 1: Rule-based
    pre_cleaned = rule_based_clean(text)

    # Step 2: Check còn cần Claude không
    error_rate = detect_error_rate(pre_cleaned)
    if error_rate < 0.10:
        info(f"   Lỗi dấu {error_rate:.1%} — rule-based đủ, skip Claude")
        return pre_cleaned

    info(f"   Lỗi dấu {error_rate:.1%} — cần Claude clean...")

    # Step 3: Chunk và clean
    words = pre_cleaned.split()
    chunks = []
    for i in range(0, len(words), chunk_words):
        chunk = " ".join(words[i:i+chunk_words])
        chunks.append(chunk)

    cleaned_parts = []
    for i, chunk in enumerate(chunks):
        result = claude_clean_chunk(chunk, client)
        if result:
            cleaned_parts.append(result)
        else:
            cleaned_parts.append(chunk)  # Fallback: giữ nguyên nếu Claude fail
        time.sleep(0.3)  # Rate limit

    return "\n".join(cleaned_parts)


# ══════════════════════════════════════════════════════════════════
# Batch processor
# ══════════════════════════════════════════════════════════════════

def process_directory(
    input_dir: str,
    output_dir: str,
    use_claude: bool = True,
    resume: bool = True,
):
    """Batch clean tất cả txt files trong thư mục."""
    in_path = Path(input_dir)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    txt_files = sorted(in_path.glob("*.txt"))
    if not txt_files:
        warn(f"Không tìm thấy .txt files trong {input_dir}")
        return

    client = None
    if use_claude:
        try:
            import anthropic
            client = anthropic.Anthropic()
            ok("Claude Haiku ready")
        except ImportError:
            warn("anthropic chưa cài → dùng rule-based only")
            use_claude = False
        except Exception as e:
            warn(f"Claude API error: {e} → dùng rule-based only")
            use_claude = False

    info(f"Processing {len(txt_files)} files | Claude: {'ON' if use_claude else 'OFF'}")
    print()

    stats = {"total": len(txt_files), "success": 0, "skipped": 0, "failed": 0,
             "total_words_in": 0, "total_words_out": 0}

    for i, f in enumerate(txt_files, 1):
        out_file = out_path / f.name
        print(f"[{i}/{len(txt_files)}] {f.name}")

        if resume and out_file.exists() and out_file.stat().st_size > 10:
            info("   ⏭  Skip (đã clean)")
            stats["skipped"] += 1
            continue

        try:
            text = f.read_text(encoding="utf-8", errors="ignore").strip()
            if not text:
                warn("   Empty file — skip")
                stats["failed"] += 1
                continue

            words_in = len(text.split())
            stats["total_words_in"] += words_in

            if use_claude and client:
                cleaned = clean_transcript_with_claude(text, client)
            else:
                cleaned = rule_based_clean(text)

            words_out = len(cleaned.split())
            stats["total_words_out"] += words_out

            out_file.write_text(cleaned, encoding="utf-8")
            ok(f"   {words_in} → {words_out} words")
            stats["success"] += 1

        except Exception as e:
            fail(f"   Error: {e}")
            stats["failed"] += 1

    print()
    print("═" * 50)
    ok(f"Hoàn thành: {stats['success']}/{stats['total']} | Skip: {stats['skipped']}")
    ok(f"Words: {stats['total_words_in']:,} → {stats['total_words_out']:,}")
    print("═" * 50)

    # Save stats
    (out_path / "_cleaning_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="YouTube Caption Cleaner — Phidipus AI Forge E1"
    )
    parser.add_argument("--input",     default="raw_corpus/youtube/")
    parser.add_argument("--output",    default="raw_corpus/cleaned/")
    parser.add_argument("--no-claude", action="store_true",
                        help="Chỉ dùng rule-based (không gọi Claude API)")
    parser.add_argument("--no-resume", action="store_true",
                        help="Không skip files đã clean")
    args = parser.parse_args()

    process_directory(
        input_dir=args.input,
        output_dir=args.output,
        use_claude=not args.no_claude,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()
