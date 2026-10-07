#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
elite_pipeline/export_transcripts.py — Export YouTube transcripts
═══════════════════════════════════════════════════════════════════

Tìm YouTube → tải audio → Whisper transcribe → xuất file sạch
để user copy-paste vào Gemini/Sonnet tạo dataset.

Cách dùng:
  # Export transcripts theo keywords
  python elite_pipeline/export_transcripts.py \
    --keywords "kỹ năng bán hàng,xử lý từ chối khách" \
    --max-videos 10 \
    --output transcripts_export/

  # Export từ URL cụ thể
  python elite_pipeline/export_transcripts.py \
    --urls "https://youtube.com/watch?v=xxx,https://youtube.com/watch?v=yyy" \
    --output transcripts_export/
"""

from __future__ import annotations
import argparse, json, os, re, sys, time, subprocess
from pathlib import Path
from datetime import datetime


def _c(code, text):
    return f"\033[{code}m{text}\033[0m"

def ok(m): print(f"  {_c('0;32','[OK]')}  {m}")
def info(m): print(f"  {_c('0;36','[..]')}  {m}")
def warn(m): print(f"  {_c('1;33','[!!]')}  {m}")


def search_youtube(keywords: list[str], max_per_keyword: int = 5) -> list[dict]:
    """Search YouTube for videos matching keywords."""
    videos = []
    try:
        import yt_dlp
    except ImportError:
        warn("yt-dlp chưa cài. Chạy: pip install yt-dlp")
        return videos

    for kw in keywords:
        info(f"Tìm YouTube: \"{kw}\"")
        ydl_opts = {
            'quiet': True, 'no_warnings': True,
            'extract_flat': True, 'force_generic_extractor': False,
            'default_search': f'ytsearch{max_per_keyword}',
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(kw, download=False)
                for entry in result.get('entries', []):
                    if entry and entry.get('url'):
                        videos.append({
                            'url': f"https://youtube.com/watch?v={entry.get('id', entry.get('url',''))}",
                            'title': entry.get('title', 'Unknown'),
                            'duration': entry.get('duration', 0),
                            'keyword': kw,
                        })
                ok(f"  Tìm thấy {len(result.get('entries',[]))} video cho \"{kw}\"")
        except Exception as e:
            warn(f"  Lỗi tìm kiếm: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for v in videos:
        if v['url'] not in seen:
            seen.add(v['url'])
            unique.append(v)
    return unique


def download_audio(url: str, output_dir: str) -> str | None:
    """Download audio from YouTube video."""
    try:
        import yt_dlp
    except ImportError:
        return None

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_dir, '%(id)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'wav',
            'preferredquality': '16',
        }],
        'quiet': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            video_id = info_dict.get('id', '')
            wav_path = os.path.join(output_dir, f"{video_id}.wav")
            if os.path.exists(wav_path):
                return wav_path
            # Try other extensions
            for ext in ['wav', 'mp3', 'webm', 'm4a']:
                p = os.path.join(output_dir, f"{video_id}.{ext}")
                if os.path.exists(p):
                    return p
    except Exception as e:
        warn(f"  Download lỗi: {e}")
    return None


def transcribe_audio(audio_path: str, model_size: str = "base") -> str:
    """Transcribe audio using Whisper."""
    try:
        import whisper
        info(f"Whisper transcribing ({model_size})...")
        model = whisper.load_model(model_size)
        result = model.transcribe(audio_path, language="vi")
        return result.get("text", "")
    except ImportError:
        # Try whisper CLI
        try:
            result = subprocess.run(
                ["whisper", audio_path, "--language", "vi", "--model", model_size, "--output_format", "txt"],
                capture_output=True, text=True, timeout=600,
            )
            txt_path = audio_path.rsplit('.', 1)[0] + ".txt"
            if os.path.exists(txt_path):
                return Path(txt_path).read_text(encoding="utf-8")
        except Exception:
            pass
        warn("Whisper chưa cài. Chạy: pip install openai-whisper")
        return ""


def export_transcripts(
    videos: list[dict],
    output_dir: str = "transcripts_export",
    whisper_model: str = "base",
    include_prompt: bool = True,
) -> dict:
    """Download, transcribe, and export all videos."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    audio_dir = os.path.join(output_dir, "_audio_temp")

    stats = {"total": len(videos), "transcribed": 0, "failed": 0, "total_words": 0}
    transcripts = []

    for i, video in enumerate(videos):
        print(f"\n  [{i+1}/{len(videos)}] {video['title'][:60]}")
        print(f"  URL: {video['url']}")

        # Download
        info("Đang tải audio...")
        audio_path = download_audio(video['url'], audio_dir)
        if not audio_path:
            warn("Không tải được audio — bỏ qua")
            stats["failed"] += 1
            continue

        # Transcribe
        info("Đang chuyển thành text (Whisper)...")
        text = transcribe_audio(audio_path, whisper_model)
        if not text or len(text) < 50:
            warn(f"Transcript quá ngắn ({len(text)} chars) — bỏ qua")
            stats["failed"] += 1
            continue

        word_count = len(text.split())
        stats["transcribed"] += 1
        stats["total_words"] += word_count
        ok(f"Transcript: {word_count} từ")

        transcripts.append({
            "title": video["title"],
            "url": video["url"],
            "keyword": video.get("keyword", ""),
            "text": text,
            "word_count": word_count,
        })

        # Save individual transcript
        safe_name = re.sub(r'[^\w\s-]', '', video['title'])[:50].strip().replace(' ', '_')
        txt_path = Path(output_dir) / f"{safe_name}.txt"
        txt_path.write_text(
            f"# {video['title']}\n# URL: {video['url']}\n# Từ khóa: {video.get('keyword','')}\n"
            f"# Transcribed: {datetime.now().isoformat()}\n\n{text}",
            encoding="utf-8"
        )

    # ── Export combined file ──
    combined_path = Path(output_dir) / "ALL_TRANSCRIPTS.md"
    lines = [
        f"# YouTube Transcripts — {stats['transcribed']} video",
        f"Xuất lúc: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Tổng: {stats['total_words']:,} từ\n",
    ]
    for t in transcripts:
        lines.append(f"\n---\n## {t['title']}")
        lines.append(f"URL: {t['url']} | Keywords: {t['keyword']} | {t['word_count']} từ\n")
        lines.append(t['text'])

    if include_prompt:
        lines.append("\n\n" + "=" * 60)
        lines.append("# HƯỚNG DẪN: Copy nội dung trên và paste vào chatbot")
        lines.append("=" * 60)
        lines.append("""
Mở Gemini Pro (gemini.google.com) hoặc Claude (claude.ai), paste prompt sau:

---
Bạn là chuyên gia phân tích nội dung đào tạo bán hàng.
Đọc transcript sau và trích xuất TẤT CẢ tình huống CSKH/Sales.
Với mỗi tình huống, tạo JSON có: trigger, customer_psychology (3 tầng: surface/deep/hidden_motive),
psychology_principles, strategy (objective + tactics + levers), good_example, bad_example, bad_reason.

Transcript:
[PASTE NỘI DUNG TRANSCRIPT VÀO ĐÂY]

Output JSON array.
---
""")

    combined_path.write_text("\n".join(lines), encoding="utf-8")
    ok(f"\nĐã xuất: {combined_path}")

    # ── Export metadata JSON ──
    meta_path = Path(output_dir) / "metadata.json"
    meta_path.write_text(json.dumps({
        "exported_at": datetime.now().isoformat(),
        "stats": stats,
        "videos": [{k: v for k, v in t.items() if k != "text"} for t in transcripts],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # Summary
    print(f"\n{'═' * 50}")
    print(f"  XUẤT TRANSCRIPT HOÀN TẤT")
    print(f"  Thành công: {stats['transcribed']}/{stats['total']}")
    print(f"  Tổng: {stats['total_words']:,} từ")
    print(f"  Thư mục: {output_dir}/")
    print(f"  File tổng hợp: {combined_path}")
    print(f"{'═' * 50}")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Export YouTube transcripts for dataset creation")
    parser.add_argument("--keywords", help="Từ khóa tìm kiếm (phân cách bằng dấu phẩy)")
    parser.add_argument("--urls", help="URL YouTube cụ thể (phân cách bằng dấu phẩy)")
    parser.add_argument("--max-videos", type=int, default=5, help="Số video tối đa mỗi keyword")
    parser.add_argument("--whisper-model", default="base", choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model (base=nhanh, medium=tốt, large=tốt nhất nhưng chậm)")
    parser.add_argument("--output", "-o", default="transcripts_export/")
    args = parser.parse_args()

    videos = []
    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        videos.extend(search_youtube(keywords, args.max_videos))
    if args.urls:
        for url in args.urls.split(","):
            url = url.strip()
            if url:
                videos.append({"url": url, "title": url.split("=")[-1], "keyword": "direct"})

    if not videos:
        print("Cần --keywords hoặc --urls")
        print('\nVí dụ:')
        print('  python elite_pipeline/export_transcripts.py --keywords "kỹ năng bán hàng,chốt sale"')
        print('  python elite_pipeline/export_transcripts.py --urls "https://youtube.com/watch?v=xxx"')
        sys.exit(1)

    export_transcripts(videos, args.output, args.whisper_model)


if __name__ == "__main__":
    main()
