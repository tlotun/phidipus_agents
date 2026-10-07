#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
data_engine/whisper_transcriber.py — Phidipus AI Forge E1
══════════════════════════════════════════════════════════════

Whisper Local Transcriber: Audio → Text tiếng Việt (hoàn toàn offline)

Ưu tiên 1 (tốt nhất): Whisper large-v3 local
  - Chất lượng cao nhất cho tiếng Việt
  - Hoàn toàn offline, không tốn API
  - Trên M4 Pro 64GB chạy ~1.5x realtime với large-v3

Fallback: Whisper medium (nhanh hơn, chất lượng vẫn tốt)

Cài đặt:
  pip install openai-whisper yt-dlp

Chạy:
  python data_engine/whisper_transcriber.py --input raw_corpus/audio/ --model large-v3
  python data_engine/whisper_transcriber.py --input raw_corpus/audio/ --model medium
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict


# ══════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════

SUPPORTED_AUDIO = {".mp3", ".mp4", ".wav", ".m4a", ".webm", ".ogg", ".flac"}


# ══════════════════════════════════════════════════════════════════
# Top-level worker (phải ở module-level để multiprocessing pickle được)
# ══════════════════════════════════════════════════════════════════

def _transcribe_worker(args: tuple) -> dict:
    """
    PERF-06 FIX: Top-level function để multiprocessing có thể pickle.

    Mỗi worker process load model Whisper riêng → tránh thread-safety issues.
    Chạy trong subprocess độc lập → không share state với main process.
    """
    audio_path_str, model_size, language, output_dir, resume = args
    audio_path = Path(audio_path_str)
    out_dir = Path(output_dir)

    # Check resume — skip nếu đã có output
    out_txt = out_dir / audio_path.with_suffix(".txt").name
    if resume and out_txt.exists() and out_txt.stat().st_size > 10:
        return {
            "file": audio_path_str, "success": True, "skipped": True,
            "text": out_txt.read_text(encoding="utf-8"),
            "language": language, "duration_s": 0.0,
            "word_count": len(out_txt.read_text(encoding="utf-8").split()),
        }

    try:
        import whisper as _whisper
        model = _whisper.load_model(model_size)
    except ImportError:
        return {"file": audio_path_str, "success": False,
                "error": "openai-whisper not installed"}

    t0 = time.time()
    try:
        result = model.transcribe(
            str(audio_path),
            language=language,
            verbose=False,
            condition_on_previous_text=True,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
        )
        text = result["text"].strip()
        detected_lang = result.get("language", language)
        duration = time.time() - t0
        word_count = len(text.split())

        out_txt.write_text(text, encoding="utf-8")

        # Save metadata JSON
        import json as _json
        meta = {
            "file": audio_path.name, "model": model_size,
            "language": detected_lang, "duration_s": round(duration, 1),
            "word_count": word_count,
            "segments": len(result.get("segments", [])),
        }
        (out_dir / audio_path.with_suffix(".meta.json").name).write_text(
            _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return {
            "file": audio_path_str, "success": True, "skipped": False,
            "text": text, "language": detected_lang,
            "duration_s": duration, "word_count": word_count,
        }

    except Exception as exc:
        return {"file": audio_path_str, "success": False, "error": str(exc),
                "text": "", "language": language, "duration_s": 0.0, "word_count": 0}

WHISPER_MODELS = {
    "tiny":     {"size": "~75MB",  "quality": "⭐⭐",    "speed": "5x realtime"},
    "base":     {"size": "~145MB", "quality": "⭐⭐⭐",   "speed": "4x realtime"},
    "small":    {"size": "~465MB", "quality": "⭐⭐⭐",   "speed": "3x realtime"},
    "medium":   {"size": "~1.5GB", "quality": "⭐⭐⭐⭐",  "speed": "0.5x realtime"},
    "large-v3": {"size": "~3GB",   "quality": "⭐⭐⭐⭐⭐", "speed": "~1.5x realtime M4"},
}


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")
def fail(msg: str) -> None: print(f"{_c('0;31', '[FAIL]')}  {msg}")


# ══════════════════════════════════════════════════════════════════
# Result dataclass
# ══════════════════════════════════════════════════════════════════

@dataclass
class TranscriptResult:
    file:        str
    text:        str
    language:    str
    duration_s:  float
    word_count:  int
    model_used:  str
    success:     bool
    error:       str = ""


# ══════════════════════════════════════════════════════════════════
# Core Transcriber
# ══════════════════════════════════════════════════════════════════

class WhisperTranscriber:
    """
    Batch transcribe audio files → tiếng Việt text.

    Tự động:
      - Skip files đã transcribe (incremental processing)
      - Save progress sau mỗi file (resume nếu crash)
      - Generate summary report JSON
    """

    def __init__(
        self,
        model_size: str = "large-v3",
        language: str = "vi",
        output_dir: str = "raw_corpus/whisper",
        resume: bool = True,
    ):
        self.model_size = model_size
        self.language = language
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.resume = resume
        self._model = None          # lazy load
        self.results: list[TranscriptResult] = []

    def _load_model(self):
        """Lazy-load Whisper model (chỉ load 1 lần)."""
        if self._model is None:
            try:
                import whisper
            except ImportError:
                fail("Whisper chưa cài! Chạy: pip install openai-whisper")
                sys.exit(1)

            info(f"Loading Whisper {self.model_size} ({WHISPER_MODELS[self.model_size]['size']})...")
            t0 = time.time()
            self._model = whisper.load_model(self.model_size)
            ok(f"Whisper {self.model_size} loaded ({time.time()-t0:.1f}s)")

    def _already_done(self, audio_path: Path) -> bool:
        """Check xem file này đã transcribe chưa."""
        out = self.output_dir / audio_path.with_suffix(".txt").name
        return out.exists() and out.stat().st_size > 10

    def transcribe_file(self, audio_path: Path) -> TranscriptResult:
        """Transcribe 1 file audio → text."""
        self._load_model()

        out_txt = self.output_dir / audio_path.with_suffix(".txt").name

        if self.resume and self._already_done(audio_path):
            text = out_txt.read_text(encoding="utf-8")
            info(f"⏭  Skip (done): {audio_path.name}")
            return TranscriptResult(
                file=audio_path.name, text=text,
                language=self.language, duration_s=0.0,
                word_count=len(text.split()), model_used=self.model_size,
                success=True
            )

        info(f"🎙  Transcribing: {audio_path.name}")
        t0 = time.time()

        try:
            result = self._model.transcribe(
                str(audio_path),
                language=self.language,
                verbose=False,
                condition_on_previous_text=True,   # Cải thiện coherence
                no_speech_threshold=0.6,
                compression_ratio_threshold=2.4,
            )
            text = result["text"].strip()
            detected_lang = result.get("language", self.language)
            duration = time.time() - t0
            word_count = len(text.split())

            # Save transcript
            out_txt.write_text(text, encoding="utf-8")

            # Save metadata JSON
            meta = {
                "file": audio_path.name,
                "model": self.model_size,
                "language": detected_lang,
                "duration_s": round(duration, 1),
                "word_count": word_count,
                "segments": len(result.get("segments", [])),
            }
            (self.output_dir / audio_path.with_suffix(".meta.json").name).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            ok(f"   Done: {word_count} words | {duration:.1f}s | lang={detected_lang}")

            return TranscriptResult(
                file=audio_path.name, text=text,
                language=detected_lang, duration_s=duration,
                word_count=word_count, model_used=self.model_size,
                success=True
            )

        except Exception as e:
            fail(f"   Error: {audio_path.name}: {e}")
            return TranscriptResult(
                file=audio_path.name, text="",
                language=self.language, duration_s=0.0,
                word_count=0, model_used=self.model_size,
                success=False, error=str(e)
            )

    def transcribe_batch(self, input_dir: str) -> list[TranscriptResult]:
        """Transcribe tất cả audio files trong thư mục (sequential — legacy)."""
        input_path = Path(input_dir)
        if not input_path.exists():
            fail(f"Input dir không tồn tại: {input_dir}")
            return []

        audio_files = sorted([
            f for f in input_path.iterdir()
            if f.suffix.lower() in SUPPORTED_AUDIO
        ])

        if not audio_files:
            warn(f"Không tìm thấy audio files trong {input_dir}")
            warn(f"Hỗ trợ: {', '.join(SUPPORTED_AUDIO)}")
            return []

        info(f"Tìm thấy {len(audio_files)} audio files")
        info(f"Model: Whisper {self.model_size} | Language: {self.language}")
        info(f"Output: {self.output_dir}")
        print()

        results = []
        for i, f in enumerate(audio_files, 1):
            print(f"[{i}/{len(audio_files)}] ", end="")
            r = self.transcribe_file(f)
            results.append(r)
            self.results.append(r)

        self._save_summary(results)
        return results

    def transcribe_batch_parallel(
        self,
        input_dir: str,
        max_workers: int | None = None,
    ) -> list[TranscriptResult]:
        """
        PERF-06 FIX: Parallel Whisper transcription dùng multiprocessing.

        Whisper không thread-safe ở Python level → phải dùng multiprocessing.
        Mỗi worker process load model riêng → các processes chạy đồng thời.

        Apple Silicon note:
          Neural Engine bị saturate ở ~2 concurrent Whisper large-v3.
          max_workers=2 là optimal cho M4 Pro 64GB (2 × 3GB = 6GB RAM).
          Dùng max_workers >= 3 sẽ chậm hơn do memory bandwidth contention.

        Speedup ước tính: ~1.8× faster với max_workers=2 (không phải 2× vì overhead).
        """
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor, as_completed

        input_path = Path(input_dir)
        if not input_path.exists():
            fail(f"Input dir không tồn tại: {input_dir}")
            return []

        audio_files = sorted([
            f for f in input_path.iterdir()
            if f.suffix.lower() in SUPPORTED_AUDIO
        ])

        if not audio_files:
            warn(f"Không tìm thấy audio files trong {input_dir}")
            return []

        # Apple Silicon: Neural Engine saturates at ~2 concurrent Whisper large-v3 processes
        n_workers = max_workers or min(2, mp.cpu_count())

        info(f"Tìm thấy {len(audio_files)} audio files")
        info(f"Model: Whisper {self.model_size} | Workers: {n_workers} | Language: {self.language}")
        info(f"Output: {self.output_dir}")
        print()

        args_list = [
            (str(f), self.model_size, self.language, str(self.output_dir), self.resume)
            for f in audio_files
        ]

        raw_results: list[dict] = []
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_transcribe_worker, args): args[0]
                for args in args_list
            }
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    r = future.result()
                except Exception as exc:
                    r = {
                        "file": futures[future], "success": False,
                        "error": str(exc), "text": "", "language": self.language,
                        "duration_s": 0.0, "word_count": 0,
                    }
                status = "✓" if r.get("success") else "✗"
                skipped = " (skip)" if r.get("skipped") else ""
                print(f"[{i}/{len(audio_files)}] {Path(r['file']).name}: {status}{skipped}")
                raw_results.append(r)

        # Convert raw dicts → TranscriptResult
        results = [
            TranscriptResult(
                file=Path(r["file"]).name,
                text=r.get("text", ""),
                language=r.get("language", self.language),
                duration_s=r.get("duration_s", 0.0),
                word_count=r.get("word_count", 0),
                model_used=self.model_size,
                success=r.get("success", False),
                error=r.get("error", ""),
            )
            for r in raw_results
        ]
        self.results.extend(results)
        self._save_summary(results)
        return results

    def _save_summary(self, results: list[TranscriptResult]):
        """Lưu summary report."""
        success = [r for r in results if r.success]
        total_words = sum(r.word_count for r in success)

        summary = {
            "total_files": len(results),
            "success": len(success),
            "failed": len(results) - len(success),
            "total_words": total_words,
            "model": self.model_size,
            "language": self.language,
            "files": [asdict(r) for r in results],
        }

        summary_path = self.output_dir / "_transcription_summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print()
        print("═" * 50)
        ok(f"Hoàn thành: {len(success)}/{len(results)} files")
        ok(f"Tổng words: {total_words:,}")
        ok(f"Summary: {summary_path}")
        print("═" * 50)


# ══════════════════════════════════════════════════════════════════
# OpenAI Whisper API fallback (khi cần tốc độ)
# ══════════════════════════════════════════════════════════════════

def transcribe_via_openai_api(audio_path: str, output_dir: str = "raw_corpus/whisper") -> Optional[str]:
    """
    Fallback: Dùng OpenAI Whisper API khi local quá chậm.
    Chi phí: ~$0.006/phút audio.
    """
    try:
        import openai
        client = openai.OpenAI()
    except ImportError:
        fail("Cần cài: pip install openai")
        return None

    path = Path(audio_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    info(f"[OpenAI API] Transcribing: {path.name}")
    try:
        with open(audio_path, "rb") as f:
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="vi",
            )
        text = response.text
        out_path = out_dir / path.with_suffix(".txt").name
        out_path.write_text(text, encoding="utf-8")
        ok(f"Done: {len(text.split())} words → {out_path}")
        return text
    except Exception as e:
        fail(f"OpenAI API error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Whisper Transcriber — Phidipus AI Forge E1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Chất lượng cao nhất (khuyến nghị M4 Pro 64GB):
  python data_engine/whisper_transcriber.py --input raw_corpus/audio/ --model large-v3

  # Nhanh hơn, chất lượng vẫn tốt:
  python data_engine/whisper_transcriber.py --input raw_corpus/audio/ --model medium

  # Dùng OpenAI API (fallback):
  python data_engine/whisper_transcriber.py --input audio.mp3 --api openai

  # Xem model options:
  python data_engine/whisper_transcriber.py --list-models
        """
    )
    parser.add_argument("--input",       default="raw_corpus/audio/", help="Input dir hoặc file")
    parser.add_argument("--output",      default="raw_corpus/whisper/", help="Output dir")
    parser.add_argument("--model",       default="large-v3",
                        choices=list(WHISPER_MODELS.keys()), help="Whisper model size")
    parser.add_argument("--language",    default="vi", help="Language code (vi=tiếng Việt)")
    parser.add_argument("--no-resume",   action="store_true", help="Không skip files đã done")
    parser.add_argument("--api",         choices=["openai"], help="Dùng API thay local")
    parser.add_argument("--list-models", action="store_true", help="Hiện model options")
    args = parser.parse_args()

    if args.list_models:
        print("\nWhisper Models:")
        for name, info_d in WHISPER_MODELS.items():
            print(f"  {name:<12} {info_d['size']:<10} {info_d['quality']:<12} {info_d['speed']}")
        print()
        return

    if args.api == "openai":
        transcribe_via_openai_api(args.input, args.output)
        return

    transcriber = WhisperTranscriber(
        model_size=args.model,
        language=args.language,
        output_dir=args.output,
        resume=not args.no_resume,
    )

    input_path = Path(args.input)
    if input_path.is_file():
        transcriber.transcribe_file(input_path)
    else:
        transcriber.transcribe_batch(args.input)


if __name__ == "__main__":
    main()
