#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
insight_engine/claude_extractor.py — Phidipus AI Forge E2
═══════════════════════════════════════════════════════════════

Insight Engine: Dùng Claude Haiku để extract situation/psychology/strategy từ corpus.

Đây là bước quan trọng nhất — chất lượng dataset phụ thuộc 80% vào đây.
Một situation tốt = trigger rõ + psychology đúng + strategy + example response.

Chi phí: ~$0.002/video (Claude Haiku ~$0.0008/1K input tokens)
Thời gian: ~2-3 phút cho 30MB corpus

Output: insights/raw/*.json — danh sách situation JSONs

Chạy:
  python insight_engine/claude_extractor.py --input corpus/final/ --domain domains/ban_hang.yaml
  python insight_engine/claude_extractor.py --input corpus/final/ --domain domains/suc_khoe.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import yaml


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")
def fail(msg: str) -> None: print(f"{_c('0;31', '[FAIL]')}  {msg}")


# ══════════════════════════════════════════════════════════════════
# System Prompt (domain-agnostic core)
# ══════════════════════════════════════════════════════════════════

BASE_SYSTEM_PROMPT = """Bạn là chuyên gia phân tích tình huống và chiến lược xử lý.

Nhiệm vụ: Đọc đoạn text và extract tất cả tình huống thực tế thành structured JSON.

Format output (JSON duy nhất, KHÔNG có text thừa, KHÔNG markdown):
{{
  "situations": [
    {{
      "id": "sit_001",
      "trigger": "mô tả ngắn tình huống kích hoạt (snake_case, 3-6 từ tiếng Anh)",
      "context": "mô tả bối cảnh cụ thể (1-2 câu tiếng Việt)",
      "customer_psychology": ["trạng thái tâm lý 1", "trạng thái tâm lý 2"],
      "recommended_strategy": "tên framework hoặc chiến thuật",
      "framework_step": "bước cụ thể trong framework",
      "example_response": "ví dụ câu trả lời/xử lý tốt (50-150 từ tiếng Việt, tự nhiên)",
      "bad_response": "ví dụ câu trả lời/xử lý kém (20-50 từ)",
      "why_bad": "giải thích ngắn tại sao bad_response là sai",
      "language": "vi",
      "domain": "{domain_name}"
    }}
  ]
}}

QUY TẮC BẮT BUỘC:
1. Chỉ extract situation khi có đủ TRIGGER + PSYCHOLOGY + STRATEGY + EXAMPLE
2. example_response phải >= 50 từ, thực tế, không sách vở
3. Không bịa tình huống không có trong text
4. Mỗi chunk text extract 2-6 situations (không cố gắng ép nhiều hơn)
5. situation_type phải thuộc danh sách được cung cấp

{domain_extra_prompt}
"""


def build_system_prompt(domain_config: dict) -> str:
    """Build system prompt với domain-specific instructions."""
    domain = domain_config.get("domain", {})
    domain_name = domain.get("name", "general")
    insight_cfg = domain_config.get("insight_extraction", {})
    extra_prompt = insight_cfg.get("extra_prompt", "")
    situation_types = insight_cfg.get("situation_types", [])

    domain_extra = ""
    if situation_types:
        types_str = ", ".join(f'"{t}"' for t in situation_types)
        domain_extra += f"\nSituation types (trigger phải thuộc): [{types_str}]\n"
    if extra_prompt:
        domain_extra += f"\nLưu ý đặc biệt:\n{extra_prompt}"

    return BASE_SYSTEM_PROMPT.format(
        domain_name=domain_name,
        domain_extra_prompt=domain_extra,
    )


# ══════════════════════════════════════════════════════════════════
# Extractor
# ══════════════════════════════════════════════════════════════════

class SituationExtractor:
    """
    Extract situations từ text corpus.
    
    Hỗ trợ 3 backend:
    - ollama: Miễn phí, chạy local (qwen3:8b, llama3.2, etc.)
    - gemini: Miễn phí (free tier), cần API key
    - anthropic: Trả phí (~$0.002/video), chất lượng cao nhất
    """

    def __init__(
        self,
        domain_config: dict,
        output_dir: str = "insights/raw",
        chunk_words: int = 1200,
        resume: bool = True,
        max_concurrent: int = 5,
        backend: str = "ollama",          # "ollama" | "gemini" | "anthropic"
        ollama_model: str = "qwen3:8b",   # model name khi backend=ollama
        gemini_key: str = "",              # API key khi backend=gemini
    ):
        self.domain_config = domain_config
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.chunk_words = chunk_words
        self.resume = resume
        self.system_prompt = build_system_prompt(domain_config)
        self._max_concurrent = max_concurrent
        self.backend = backend
        self.ollama_model = ollama_model
        self.gemini_key = gemini_key or os.environ.get("GEMINI_API_KEY", "")

        self.client = None
        self._async_client = None

        if backend == "anthropic":
            try:
                import anthropic
                self.client = anthropic.Anthropic()
                self._async_client = anthropic.AsyncAnthropic()
                ok(f"Backend: Claude Haiku (trả phí ~$0.002/video)")
            except ImportError:
                fail("Cần cài: pip install anthropic")
                fail("Hoặc đổi backend: --backend ollama (miễn phí)")
                sys.exit(1)
        elif backend == "gemini":
            ok(f"Backend: Gemini Flash (miễn phí)")
            if not self.gemini_key:
                warn("GEMINI_API_KEY chưa set — export GEMINI_API_KEY=AIza...")
        elif backend == "ollama":
            ok(f"Backend: Ollama local ({ollama_model}) — miễn phí, offline")
        else:
            fail(f"Backend không hỗ trợ: {backend}")
            sys.exit(1)

        self.stats = {
            "files_processed": 0, "chunks_processed": 0,
            "situations_extracted": 0, "api_calls": 0,
            "api_errors": 0, "cost_estimate_usd": 0.0,
            "backend": backend,
        }

    def _already_processed(self, file_path: Path) -> Optional[Path]:
        """Check xem file đã được extract chưa."""
        out_file = self.output_dir / (file_path.stem + "_situations.json")
        if self.resume and out_file.exists():
            try:
                data = json.loads(out_file.read_text(encoding="utf-8"))
                if data.get("situations"):
                    return out_file
            except Exception:
                pass
        return None

    def _call_llm(self, text_chunk: str) -> str:
        """Call LLM backend and return raw response text."""
        user_msg = f"Extract situations từ đây:\n\n{text_chunk}"

        if self.backend == "anthropic":
            resp = self.client.messages.create(
                model="claude-haiku-4-5", max_tokens=4096,
                system=self.system_prompt,
                messages=[{"role": "user", "content": user_msg}]
            )
            input_tokens = len(text_chunk.split()) * 1.3
            output_tokens = len(resp.content[0].text.split()) * 1.3
            self.stats["cost_estimate_usd"] += (input_tokens / 1000 * 0.0008 + output_tokens / 1000 * 0.004)
            return resp.content[0].text.strip()

        elif self.backend == "ollama":
            import urllib.request
            payload = json.dumps({
                "model": self.ollama_model, "stream": False,
                "system": self.system_prompt,
                "prompt": user_msg,
                "options": {"temperature": 0.3, "num_predict": 4096},
            }).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/generate",
                data=payload, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("response", "").strip()

        elif self.backend == "gemini":
            import urllib.request
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
            payload = json.dumps({
                "system_instruction": {"parts": [{"text": self.system_prompt}]},
                "contents": [{"parts": [{"text": user_msg}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json", "x-goog-api-key": self.gemini_key})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()

        raise ValueError(f"Unknown backend: {self.backend}")

    def _extract_chunk(self, text_chunk: str, chunk_idx: int) -> list[dict]:
        """Extract situations từ 1 chunk dùng LLM backend đã chọn."""
        try:
            raw = self._call_llm(text_chunk)
            self.stats["api_calls"] += 1

            # Strip markdown if any
            if raw.startswith("```"):
                raw = re.sub(r"^```\w*\n?|```$", "", raw, flags=re.MULTILINE).strip()

            data = json.loads(raw)
            situations = data.get("situations", [])

            # Add IDs
            for j, sit in enumerate(situations):
                sit["id"] = f"chunk{chunk_idx:03d}_sit{j:03d}"
                sit.setdefault("language", "vi")

            return situations

        except json.JSONDecodeError as e:
            warn(f"     JSON parse error chunk {chunk_idx}: {e}")
            self.stats["api_errors"] += 1
            return []
        except Exception as e:
            warn(f"     API error chunk {chunk_idx}: {e}")
            self.stats["api_errors"] += 1
            time.sleep(5)  # Backoff
            return []

    def extract_file(self, text_file: Path) -> list[dict]:
        """Extract tất cả situations từ 1 text file (sync wrapper → async engine)."""
        return asyncio.run(self._extract_file_async(text_file))

    async def _extract_chunk_async(self, text_chunk: str, chunk_idx: int,
                                    semaphore: asyncio.Semaphore) -> list[dict]:
        """
        Async chunk extraction với Semaphore để giới hạn concurrency.
        Hỗ trợ Ollama/Gemini/Anthropic backends.
        """
        async with semaphore:
            try:
                # Anthropic has native async; Ollama/Gemini run sync in executor
                if self.backend == "anthropic" and self._async_client:
                    user_msg = f"Extract situations từ đây:\n\n{text_chunk}"
                    resp = await self._async_client.messages.create(
                        model="claude-haiku-4-5", max_tokens=4096,
                        system=self.system_prompt,
                        messages=[{"role": "user", "content": user_msg}]
                    )
                    raw = resp.content[0].text.strip()
                    input_tokens = len(text_chunk.split()) * 1.3
                    output_tokens = len(raw.split()) * 1.3
                    self.stats["cost_estimate_usd"] += (
                        input_tokens / 1000 * 0.0008 + output_tokens / 1000 * 0.004
                    )
                else:
                    # Run sync _call_llm in thread pool for Ollama/Gemini
                    loop = asyncio.get_event_loop()
                    raw = await loop.run_in_executor(None, self._call_llm, text_chunk)

                self.stats["api_calls"] += 1

                if raw.startswith("```"):
                    raw = re.sub(r"^```\w*\n?|```$", "", raw, flags=re.MULTILINE).strip()

                data = json.loads(raw)
                situations = data.get("situations", [])

                for j, sit in enumerate(situations):
                    sit["id"] = f"chunk{chunk_idx:03d}_sit{j:03d}"
                    sit.setdefault("language", "vi")

                self.stats["chunks_processed"] += 1
                return situations

            except json.JSONDecodeError as e:
                warn(f"     JSON parse error chunk {chunk_idx}: {e}")
                self.stats["api_errors"] += 1
                return []
            except Exception as e:
                warn(f"     API error chunk {chunk_idx}: {e}")
                self.stats["api_errors"] += 1
                await asyncio.sleep(2)  # backoff khi error
                return []

    async def _extract_file_async(self, text_file: Path) -> list[dict]:
        """Async version — tất cả chunks của 1 file chạy đồng thời."""
        existing = self._already_processed(text_file)
        if existing:
            info(f"   ⏭  Skip: {text_file.name} (đã extract)")
            data = json.loads(existing.read_text(encoding="utf-8"))
            return data.get("situations", [])

        text = text_file.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            return []

        words = text.split()
        chunks = [
            " ".join(words[i:i+self.chunk_words])
            for i in range(0, len(words), self.chunk_words)
            if len(words[i:i+self.chunk_words]) >= 50
        ]

        info(f"   {text_file.name}: {len(words):,} words → {len(chunks)} chunks "
             f"(max {self._max_concurrent} concurrent)")

        # Semaphore giới hạn concurrent calls → tránh rate limit
        semaphore = asyncio.Semaphore(self._max_concurrent)

        tasks = [
            self._extract_chunk_async(chunk, i, semaphore)
            for i, chunk in enumerate(chunks)
        ]
        results = await asyncio.gather(*tasks)
        all_situations = [sit for chunk_sits in results for sit in chunk_sits]

        # Save
        out_file = self.output_dir / (text_file.stem + "_situations.json")
        out_file.write_text(
            json.dumps({"source": text_file.name, "situations": all_situations},
                       ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        self.stats["files_processed"] += 1
        self.stats["situations_extracted"] += len(all_situations)
        ok(f"   → {len(all_situations)} situations saved")
        return all_situations

    def extract_directory(self, input_dir: str) -> list[dict]:
        """Extract từ toàn bộ text files trong thư mục."""
        in_path = Path(input_dir)
        txt_files = sorted(list(in_path.glob("*.txt")) + list(in_path.rglob("*_extracted.txt")))

        if not txt_files:
            warn(f"Không tìm thấy .txt files trong {input_dir}")
            return []

        domain_name = self.domain_config.get("domain", {}).get("name", "?")
        info(f"Extract situations: domain='{domain_name}' | {len(txt_files)} files")
        print()

        all_situations = []
        for i, f in enumerate(txt_files, 1):
            print(f"[{i}/{len(txt_files)}] {f.name[:60]}")
            sits = self.extract_file(f)
            all_situations.extend(sits)
            print()

        self._print_summary(all_situations)
        return all_situations

    def _print_summary(self, all_situations: list[dict]):
        domain_name = self.domain_config.get("domain", {}).get("name", "?")
        print("═" * 55)
        ok(f"Domain: {domain_name}")
        ok(f"Situations extracted: {len(all_situations):,}")
        ok(f"Files processed: {self.stats['files_processed']}")
        ok(f"API calls: {self.stats['api_calls']} | Errors: {self.stats['api_errors']}")
        ok(f"Estimated cost: ${self.stats['cost_estimate_usd']:.4f}")
        ok(f"Output: {self.output_dir}")
        print("═" * 55)
        print()
        warn("⚠  Tiếp theo: chạy quality_gate.py để filter situations kém chất lượng")


import re  # needed for _extract_chunk


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Insight Engine — Phidipus AI Forge E2 (multi-backend)"
    )
    parser.add_argument("--input",         default="corpus/final/",
                        help="Input dir chứa cleaned text files")
    parser.add_argument("--domain",        required=True,
                        help="Domain YAML config")
    parser.add_argument("--output",        default="insights/raw/",
                        help="Output dir cho situation JSONs")
    parser.add_argument("--chunk-words",   type=int, default=1200,
                        help="Số words mỗi API call (default: 1200)")
    parser.add_argument("--no-resume",     action="store_true")
    parser.add_argument("--backend",       default="ollama",
                        choices=["ollama", "gemini", "anthropic"],
                        help="LLM backend: ollama (miễn phí), gemini (miễn phí), anthropic (trả phí)")
    parser.add_argument("--ollama-model",  default="qwen3:8b",
                        help="Model Ollama cho extraction (default: qwen3:8b)")
    parser.add_argument("--gemini-key",    default="",
                        help="Gemini API key (hoặc dùng GEMINI_API_KEY env)")
    args = parser.parse_args()

    with open(args.domain, encoding="utf-8") as f:
        domain_cfg = yaml.safe_load(f)

    extractor = SituationExtractor(
        domain_config=domain_cfg,
        output_dir=args.output,
        chunk_words=args.chunk_words,
        resume=not args.no_resume,
        backend=args.backend,
        ollama_model=args.ollama_model,
        gemini_key=args.gemini_key,
    )

    input_path = Path(args.input)
    if input_path.is_file():
        extractor.extract_file(input_path)
    else:
        extractor.extract_directory(args.input)


if __name__ == "__main__":
    main()
