#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
data_engine/youtube_scraper.py — Phidipus AI Forge E1
═══════════════════════════════════════════════════════

YouTube Scraper: Tìm và tải video/audio từ YouTube theo domain keywords.

Workflow:
  1. Tìm video theo keywords trong domain YAML
  2. Filter: tiếng Việt, duration 10-60 phút, view > 10K
  3. Tải audio MP3 (không cần download video)
  4. Hoặc lấy auto-caption làm fallback

Cài đặt:
  pip install yt-dlp

Chạy:
  python data_engine/youtube_scraper.py --domain domains/ban_hang.yaml --audio-only
  python data_engine/youtube_scraper.py --domain domains/ban_hang.yaml --captions-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

AUDIO_DIR   = "raw_corpus/audio"
CAPTION_DIR = "raw_corpus/youtube"
LOG_FILE    = "raw_corpus/youtube_log.jsonl"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")
def fail(msg: str) -> None: print(f"{_c('0;31', '[FAIL]')}  {msg}")


def check_ytdlp() -> bool:
    """Check yt-dlp đã cài chưa."""
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        ok(f"yt-dlp {result.stdout.strip()}")
        return True
    except FileNotFoundError:
        fail("yt-dlp chưa cài! Chạy: pip install yt-dlp")
        return False


def load_domain_config(domain_path: str) -> dict:
    """Load domain YAML config."""
    path = Path(domain_path)
    if not path.exists():
        fail(f"Domain config không tồn tại: {domain_path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def search_youtube_videos(keyword: str, max_results: int = 10) -> list[dict]:
    """
    Tìm video YouTube theo keyword.
    Dùng yt-dlp --flat-playlist để lấy danh sách không tải.
    """
    info(f"🔍 Tìm: '{keyword}' (max {max_results})")
    url = f"ytsearch{max_results}:{keyword} tiếng việt"

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--print", "%(id)s|||%(title)s|||%(duration)s|||%(view_count)s|||%(channel)s",
        "--no-warnings",
        "--quiet",
        url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        videos = []
        for line in result.stdout.strip().split("\n"):
            if "|||" not in line:
                continue
            parts = line.split("|||")
            if len(parts) < 4:
                continue
            vid_id, title, duration, views, *rest = parts
            channel = rest[0] if rest else ""
            try:
                dur_s = int(duration) if duration and duration != "None" else 0
                view_n = int(views) if views and views != "None" else 0
            except (ValueError, TypeError):
                dur_s, view_n = 0, 0

            # Filter: 5-90 phút, views > 1K
            if 300 <= dur_s <= 5400 and view_n >= 1000:
                videos.append({
                    "id": vid_id,
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "title": title,
                    "duration_s": dur_s,
                    "view_count": view_n,
                    "channel": channel,
                    "keyword": keyword,
                })

        info(f"   → {len(videos)} videos phù hợp (sau filter)")
        return videos

    except subprocess.TimeoutExpired:
        warn(f"Timeout khi tìm: {keyword}")
        return []
    except Exception as e:
        warn(f"Lỗi search: {e}")
        return []


def download_audio(
    video_url: str,
    output_dir: str = AUDIO_DIR,
    video_id: str = "",
) -> Optional[str]:
    """
    Tải audio MP3 từ YouTube video.
    Returns: path đến file mp3 nếu thành công, None nếu lỗi.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Check đã tải chưa
    if video_id:
        existing = list(out_dir.glob(f"{video_id}.*"))
        if existing:
            info(f"⏭  Audio đã tải: {existing[0].name}")
            return str(existing[0])

    cmd = [
        "yt-dlp",
        "-x",                           # Extract audio
        "--audio-format", "mp3",
        "--audio-quality", "5",          # 5 = 128kbps (đủ cho speech)
        "-o", str(out_dir / "%(id)s.%(ext)s"),
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--progress",
        video_url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            # Tìm file vừa tải
            if video_id:
                files = list(out_dir.glob(f"{video_id}.*"))
                if files:
                    ok(f"   Audio: {files[0].name}")
                    return str(files[0])
            return output_dir
        else:
            warn(f"   yt-dlp error: {result.stderr[:200]}")
            return None
    except subprocess.TimeoutExpired:
        warn("   Timeout khi tải audio")
        return None
    except Exception as e:
        warn(f"   Error: {e}")
        return None


def download_captions(
    video_url: str,
    output_dir: str = CAPTION_DIR,
    video_id: str = "",
) -> Optional[str]:
    """
    Tải auto-captions từ YouTube (tiếng Việt).
    CẢNH BÁO: Auto-caption VN lỗi dấu ~30-40% — cần youtube_cleaner.py sau.
    Returns: path đến file txt nếu thành công.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Check đã tải chưa
    if video_id:
        existing = list(out_dir.glob(f"{video_id}*.txt"))
        if existing:
            info(f"⏭  Caption đã tải: {existing[0].name}")
            return str(existing[0])

    cmd = [
        "yt-dlp",
        "--write-auto-subs",
        "--sub-lang", "vi",
        "--sub-format", "vtt",
        "--skip-download",              # Không tải video
        "-o", str(out_dir / "%(id)s.%(ext)s"),
        "--no-warnings",
        "--quiet",
        video_url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            # Convert VTT → plain text
            vtt_files = list(out_dir.glob(f"{video_id}*.vtt")) if video_id else []
            if vtt_files:
                txt_path = _vtt_to_txt(vtt_files[0])
                ok(f"   Caption: {txt_path.name}")
                return str(txt_path)
            warn("   Caption không tìm thấy VTT file")
        else:
            warn(f"   Không có caption: {result.stderr[:100]}")
        return None
    except Exception as e:
        warn(f"   Error: {e}")
        return None


def _vtt_to_txt(vtt_path: Path) -> Path:
    """Convert VTT subtitle → plain text (bỏ timestamps)."""
    content = vtt_path.read_text(encoding="utf-8", errors="ignore")
    lines = []
    for line in content.split("\n"):
        # Bỏ timestamp lines và header
        if re.match(r"^\d{2}:\d{2}:", line): continue
        if line.strip() in ("WEBVTT", ""): continue
        if re.match(r"^\d+$", line.strip()): continue
        if "<" in line: line = re.sub(r"<[^>]+>", "", line)
        line = line.strip()
        if line:
            lines.append(line)

    # Dedup adjacent identical lines
    deduped = [lines[0]] if lines else []
    for l in lines[1:]:
        if l != deduped[-1]:
            deduped.append(l)

    txt_path = vtt_path.with_suffix(".txt")
    txt_path.write_text("\n".join(deduped), encoding="utf-8")
    vtt_path.unlink(missing_ok=True)
    return txt_path


def run_scraper(
    domain_config: dict,
    mode: str = "audio",         # "audio" | "captions" | "both"
    max_videos: Optional[int] = None,
    output_audio: str = AUDIO_DIR,
    output_captions: str = CAPTION_DIR,
):
    """
    Main scraper: dùng domain config để tìm và tải video.
    """
    if not check_ytdlp():
        sys.exit(1)

    domain = domain_config.get("domain", {})
    sources = domain_config.get("data_sources", {})
    keywords = sources.get("youtube_keywords", [])
    per_keyword = sources.get("youtube_max_videos", 5) // max(len(keywords), 1)
    if max_videos:
        per_keyword = max_videos // max(len(keywords), 1)

    domain_name = domain.get("name", "unknown")
    info(f"Domain: {domain_name}")
    info(f"Keywords: {len(keywords)} | Videos/keyword: {per_keyword} | Mode: {mode}")
    print()

    all_videos = []
    seen_ids = set()

    for keyword in keywords:
        videos = search_youtube_videos(keyword, max_results=per_keyword + 5)
        for v in videos:
            if v["id"] not in seen_ids:
                seen_ids.add(v["id"])
                all_videos.append(v)

    info(f"\nTổng: {len(all_videos)} videos unique (sau dedup)")

    # Log manifest
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for i, video in enumerate(all_videos, 1):
        print(f"\n[{i}/{len(all_videos)}] {video['title'][:60]}")
        print(f"        {video['url']} | {video['duration_s']//60}m | {video['view_count']:,} views")

        result = {"video": video, "audio": None, "caption": None}

        if mode in ("audio", "both"):
            result["audio"] = download_audio(video["url"], output_audio, video["id"])

        if mode in ("captions", "both"):
            result["caption"] = download_captions(video["url"], output_captions, video["id"])

        downloaded.append(result)

        # Log
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**video, "audio": result["audio"]}, ensure_ascii=False) + "\n")

        time.sleep(1)  # Polite delay

    print()
    audio_ok  = sum(1 for r in downloaded if r["audio"])
    cap_ok    = sum(1 for r in downloaded if r["caption"])
    ok(f"✅ Hoàn thành: {audio_ok} audios | {cap_ok} captions từ {len(all_videos)} videos")
    ok(f"Log: {LOG_FILE}")


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="YouTube Scraper — Phidipus AI Forge E1"
    )
    parser.add_argument("--domain",       required=True, help="Path đến domain YAML config")
    parser.add_argument("--mode",         default="audio",
                        choices=["audio", "captions", "both"],
                        help="audio=tải MP3 | captions=tải phụ đề | both=cả hai")
    parser.add_argument("--max-videos",   type=int, default=None, help="Giới hạn số video")
    parser.add_argument("--audio-out",    default=AUDIO_DIR)
    parser.add_argument("--caption-out",  default=CAPTION_DIR)
    parser.add_argument("--urls",         help="File txt chứa URLs (1 URL/dòng) thay vì search")
    args = parser.parse_args()

    cfg = load_domain_config(args.domain)

    if args.urls:
        # Mode: download từ danh sách URL có sẵn
        urls = Path(args.urls).read_text(encoding="utf-8").strip().split("\n")
        urls = [u.strip() for u in urls if u.strip()]
        info(f"URLs mode: {len(urls)} URLs từ {args.urls}")
        for i, url in enumerate(urls, 1):
            vid_id = re.search(r"(?:v=|youtu\.be/)([^&\n]+)", url)
            vid_id = vid_id.group(1) if vid_id else f"vid_{i}"
            print(f"\n[{i}/{len(urls)}] {url}")
            if args.mode in ("audio", "both"):
                download_audio(url, args.audio_out, vid_id)
            if args.mode in ("captions", "both"):
                download_captions(url, args.caption_out, vid_id)
    else:
        run_scraper(
            domain_config=cfg,
            mode=args.mode,
            max_videos=args.max_videos,
            output_audio=args.audio_out,
            output_captions=args.caption_out,
        )


if __name__ == "__main__":
    main()
