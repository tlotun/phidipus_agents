#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
scripts/pipeline.py — Phidipus AI Forge v3.0
═══════════════════════════════════════════════════════════════════════

🕷️ Phidipus AI Training Factory — Main Entry Point

Full pipeline E1→E6 cho bất kỳ domain nào.
Domain = Config — chỉ cần swap YAML, không viết lại code.

Usage:
  # Train chatbot bán hàng (đầy đủ):
  python scripts/pipeline.py --domain domains/ban_hang.yaml

  # Train chatbot y tế:
  python scripts/pipeline.py --domain domains/suc_khoe.yaml

  # Chỉ chạy từ bước cụ thể:
  python scripts/pipeline.py --domain domains/ban_hang.yaml --from-step E3

  # Skip train (chỉ prep data):
  python scripts/pipeline.py --domain domains/ban_hang.yaml --skip-train

  # Dry run (kiểm tra config, không thực hiện):
  python scripts/pipeline.py --domain domains/ban_hang.yaml --dry-run

Pipeline Steps:
  E1  — Data Engine      (YouTube scrape + Whisper + HuggingFace + clean)
  E2  — Insight Engine   (Claude extract situations)
  E2.5— Quality Gate     (filter situations kém)
  E3  — Dataset Gen      (ChatML JSONL + augment)
  E4  — Training Engine  (MLX LoRA anchor→deploy→lite)
  E5  — Evaluation       (OOD eval + hallucinate guard test)
  E6  — Deploy           (RAG server + conversation logger)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml


# ══════════════════════════════════════════════════════════════════
# Color helpers
# ══════════════════════════════════════════════════════════════════

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)    -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str)  -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str)  -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")
def fail(msg: str)  -> None: print(f"{_c('0;31', '[FAIL]')}  {msg}")
def head(msg: str)  -> None: print(f"\n{_c('1;35', '══')} {_c('1;37', msg)} {_c('1;35', '══')}")


STEP_ORDER = ["E1", "E2", "E2.5", "E3", "E4", "E5", "E6"]


# ══════════════════════════════════════════════════════════════════
# Pipeline runner
# ══════════════════════════════════════════════════════════════════

class Pipeline:
    def __init__(
        self,
        domain_config_path: str,
        from_step: str = "E1",
        skip_train: bool = False,
        dry_run: bool = False,
        whisper_model: str = "large-v3",
        skip_steps: list[str] = None,
    ):
        self.domain_path = Path(domain_config_path)
        self.from_step = from_step
        self.skip_train = skip_train
        self.dry_run = dry_run
        self.whisper_model = whisper_model
        self.skip_steps = skip_steps or []
        self.root = Path(__file__).parent.parent

        with open(self.domain_path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)

        self.domain = self.cfg.get("domain", {})
        self.domain_name = self.domain.get("name", "unknown")
        self.domain_slug = self.domain_name.replace(" ", "_").replace("/", "_")[:20]

        train_cfg = self.cfg.get("training", {})
        self.anchor_model = train_cfg.get("anchor_model", "Qwen/Qwen3-4B")
        self.anchor_iters  = train_cfg.get("anchor_iters", 600)
        self.deploy_model  = train_cfg.get("deploy_model", "Qwen/Qwen3-1.7B")
        self.deploy_iters  = train_cfg.get("deploy_iters", 400)
        self.lite_model    = train_cfg.get("lite_model", "Qwen/Qwen3-0.6B")
        self.lite_iters    = train_cfg.get("lite_iters", 300)
        self.lora_rank     = train_cfg.get("lora_rank", 16)
        self.lr            = train_cfg.get("learning_rate", 1e-5)
        self.ollama_name   = self.cfg.get("ollama", {}).get("model_name", f"{self.domain_slug}-1.7b")

        self.results: dict[str, dict] = {}
        self.start_time = time.time()

    def should_run(self, step: str) -> bool:
        """Check xem step có nên chạy không."""
        if step in self.skip_steps:
            return False
        idx_from = STEP_ORDER.index(self.from_step) if self.from_step in STEP_ORDER else 0
        idx_step = STEP_ORDER.index(step) if step in STEP_ORDER else 0
        return idx_step >= idx_from

    def run_cmd(self, cmd: list[str], label: str = "", check: bool = True) -> int:
        """Chạy command và log output (blocking — dùng cho steps không thể async)."""
        cmd_str = " ".join(str(c) for c in cmd)
        info(f"$ {cmd_str[:100]}")
        if self.dry_run:
            info("  [DRY RUN — skip]")
            return 0

        t0 = time.time()
        result = subprocess.run(cmd, cwd=str(self.root))
        elapsed = int(time.time() - t0)

        if result.returncode == 0:
            ok(f"  Done ({elapsed}s)")
        else:
            fail(f"  Failed (exit {result.returncode}, {elapsed}s)")
            if check:
                raise RuntimeError(f"Command failed: {label or cmd_str[:60]}")

        return result.returncode

    async def run_cmd_async(
        self, cmd: list[str], label: str = "", check: bool = True
    ) -> int:
        """
        PERF-06 FIX: Async version của run_cmd.
        Không block asyncio event loop → có thể chạy nhiều subprocess song song.
        Stream output trực tiếp để user thấy progress real-time.
        """
        cmd_str = " ".join(str(c) for c in cmd)
        info(f"$ {cmd_str[:100]}")
        if self.dry_run:
            info("  [DRY RUN — skip]")
            return 0

        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Stream output live
        if proc.stdout:
            async for line in proc.stdout:
                print(f"  {line.decode(errors='replace').rstrip()}")

        await proc.wait()
        elapsed = int(time.time() - t0)

        if proc.returncode == 0:
            ok(f"  Done ({elapsed}s)")
        else:
            fail(f"  Failed (exit {proc.returncode}, {elapsed}s)")
            if check:
                raise RuntimeError(f"Command failed: {label or cmd_str[:60]}")

        return proc.returncode or 0

    # ── E1: Data Engine ───────────────────────────────────────────

    def run_e1(self):
        """Sync wrapper — gọi async parallel implementation."""
        asyncio.run(self._run_e1_async())

    async def _run_e1_async(self):
        """
        PERF-06 FIX: E1 parallel data engine.

        Thứ tự phụ thuộc:
          E1a (YouTube audio) ──┐
                                ├─→ E1b (Whisper) ──┐
          E1c (HuggingFace)  ──┘                    ├─→ E1d (Cleaner)
          (chạy song song — không có dependency)     │
                                                     └─→ (done)

        E1a + E1c: hoàn toàn độc lập → chạy song song → tiết kiệm 5-20 phút.
        E1b: cần E1a audio xong trước.
        E1d: cần E1b + E1c output.
        """
        head("E1: Data Engine (parallel)")

        # ── Phase A: YouTube + HuggingFace song song ──────────────
        info("Phase A: YouTube audio + HuggingFace datasets (song song)...")

        if self.dry_run:
            info("  [DRY RUN] skip E1a+E1c parallel")
        else:
            yt_proc = await asyncio.create_subprocess_exec(
                sys.executable, "data_engine/youtube_scraper.py",
                "--domain", str(self.domain_path), "--mode", "audio",
                cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            hf_proc = await asyncio.create_subprocess_exec(
                sys.executable, "data_engine/hf_downloader.py",
                "--domain", str(self.domain_path),
                cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            yt_rc, hf_rc = await asyncio.gather(
                yt_proc.wait(), hf_proc.wait()
            )
            ok(f"YouTube {'✓' if yt_rc == 0 else '✗'} | HuggingFace {'✓' if hf_rc == 0 else '✗'}")

        # ── Phase B: Whisper parallel (requires E1a audio) ────────
        info(f"Phase B: Whisper {self.whisper_model} — parallel workers (max 2 cho Apple Silicon)")
        if not self.dry_run:
            from data_engine.whisper_transcriber import WhisperTranscriber
            transcriber = WhisperTranscriber(
                model_size=self.whisper_model,
                language="vi",
                output_dir="raw_corpus/whisper",
                resume=True,
            )
            # Apple Silicon optimal: max_workers=2 cho large-v3
            transcriber.transcribe_batch_parallel("raw_corpus/audio/", max_workers=2)
        else:
            info("  [DRY RUN] skip E1b Whisper")

        # ── Phase C: Data cleaner (requires E1b + E1c output) ─────
        info("Phase C: Clean + deduplicate corpus")
        await self.run_cmd_async([
            sys.executable, "data_engine/data_cleaner.py",
            "--input", "raw_corpus/whisper/",
            "--output", "corpus/final/",
        ], label="data_cleaner")

        self.results["E1"] = {"status": "done"}

    # ── E2: Insight Engine ────────────────────────────────────────

    def run_e2(self):
        head("E2: Insight Engine (Claude extract situations)")

        if not os.environ.get("ANTHROPIC_API_KEY"):
            warn("ANTHROPIC_API_KEY chưa set! Export trước: export ANTHROPIC_API_KEY=sk-ant-...")
            if not self.dry_run:
                raise RuntimeError("ANTHROPIC_API_KEY required for E2")

        self.run_cmd([
            sys.executable, "insight_engine/claude_extractor.py",
            "--input", "corpus/final/",
            "--domain", str(self.domain_path),
            "--output", "insights/raw/",
        ], label="claude_extractor")

        self.results["E2"] = {"status": "done"}

    # ── E2.5: Quality Gate ────────────────────────────────────────

    def run_e25(self):
        head("E2.5: Quality Gate")

        self.run_cmd([
            sys.executable, "insight_engine/quality_gate.py",
            "--input", "insights/raw/",
            "--output", "insights/validated/",
        ], label="quality_gate")

        # Check pass rate
        stats_file = Path("insights/validated/_quality_gate_stats.json")
        if not self.dry_run and stats_file.exists():
            stats = json.loads(stats_file.read_text(encoding="utf-8"))
            pass_rate = stats.get("pass_rate", 0)
            total_kept = stats.get("total_kept", 0)
            ok(f"Quality Gate: {total_kept} situations | pass rate {pass_rate*100:.1f}%")
            if total_kept < 100:
                warn(f"⚠️  Chỉ có {total_kept} situations — có thể không đủ cho training tốt")
                warn("    Cân nhắc thêm YouTube videos hoặc cải thiện extraction prompt")

        self.results["E2.5"] = {"status": "done"}

    # ── E3: Dataset Generator ─────────────────────────────────────

    def run_e3(self):
        head("E3: Dataset Generator")

        self.run_cmd([
            sys.executable, "scripts/generate_domain_dataset.py",
            "--domain", str(self.domain_path),
            "--input", "insights/validated/",
            "--output", "training_data/",
        ], label="generate_dataset")

        # Check stats
        stats_file = Path("training_data/_dataset_stats.json")
        if not self.dry_run and stats_file.exists():
            stats = json.loads(stats_file.read_text(encoding="utf-8"))
            ok(f"Dataset: {stats.get('train', 0)} train | {stats.get('valid', 0)} valid | {stats.get('ood_test', 0)} OOD")
            refusal_count = stats.get("distribution", {}).get("refusal", 0)
            if refusal_count < 80:
                fail(f"❌ Refusal samples = {refusal_count} < 80! Pipeline DỪNG.")
                fail("   Thêm refusal_rules vào domain YAML hoặc augment dataset thủ công.")
                if not self.dry_run:
                    raise RuntimeError("Insufficient refusal samples")

        self.results["E3"] = {"status": "done"}

    # ── E4: Training Engine ───────────────────────────────────────

    def _get_resume_flag(self, adapter_path: str) -> list[str]:
        """
        PERF-06 FIX: Tự detect checkpoint cuối cùng để resume khi train bị crash.

        mlx_lm.lora lưu checkpoints dạng: 0000100_adapters.safetensors
        Nếu có checkpoint → thêm --resume-adapter-file → tiếp tục từ iter đó.
        Nếu không có → train từ đầu (iter 0).

        Ví dụ: crash ở iter 450/600 → resume từ iter 400 → tiết kiệm 400 iters.
        """
        adapter_dir = Path(adapter_path)
        if not adapter_dir.exists():
            return []  # train từ đầu — thư mục chưa có

        # mlx_lm checkpoint format: 0000100_adapters.safetensors
        checkpoints = sorted(adapter_dir.glob("*_adapters.safetensors"))
        if not checkpoints:
            return []  # không có checkpoint → train từ đầu

        last_checkpoint = checkpoints[-1]
        try:
            last_iter = int(last_checkpoint.stem.split("_")[0])
            info(f"   ↻ Resume từ checkpoint: {last_checkpoint.name} (iter {last_iter})")
        except (ValueError, IndexError):
            return []

        return ["--resume-adapter-file", str(last_checkpoint)]

    def run_e4(self):
        head("E4: Training Engine (MLX LoRA)")

        if self.skip_train:
            warn("--skip-train: bỏ qua E4")
            return

        # Check mlx_lm
        try:
            result = subprocess.run(
                [sys.executable, "-c", "import mlx_lm; print('ok')"],
                capture_output=True, text=True, cwd=str(self.root)
            )
            if result.returncode != 0:
                fail("mlx_lm chưa cài! Chạy: pip install mlx-lm")
                return
        except Exception:
            fail("Không check được mlx_lm")
            return

        # Step 4a: Anchor train (4B)
        info(f"E4a: Anchor train {self.anchor_model} ({self.anchor_iters} iters)")
        adapter_anchor = f"adapters/{self.domain_slug}-anchor-4b"
        # PERF-06 FIX: auto-resume từ checkpoint nếu có (crash-safe)
        resume_flags_anchor = self._get_resume_flag(adapter_anchor)
        self.run_cmd([
            "mlx_lm.lora",
            "--model", self.anchor_model,
            "--train",
            "--data", "training_data/",
            "--iters", str(self.anchor_iters),
            "--learning-rate", str(self.lr),
            "--lora-rank", str(self.lora_rank),
            "--save-every", "100",
            "--adapter-path", adapter_anchor,
            *resume_flags_anchor,  # PERF-06 FIX: resume nếu crash
        ], label="anchor_train_4b")

        ok(f"Anchor adapter: {adapter_anchor}")

        # Step 4b: OOD eval anchor (CRITICAL)
        info("E4b: OOD eval anchor 4B — kiểm tra dataset chất lượng")
        warn("⚠️  Nếu fail → fix DATASET trước, không tăng iters!")
        self.run_e5(model_name=f"{self.domain_slug}-anchor-4b", is_anchor_check=True)

        # Step 4c: Fine-tune 1.7B (deploy model)
        info(f"E4c: Fine-tune deploy model {self.deploy_model} ({self.deploy_iters} iters)")
        adapter_deploy = f"adapters/{self.domain_slug}-1.7b"
        # PERF-06 FIX: auto-resume từ checkpoint nếu có
        resume_flags_deploy = self._get_resume_flag(adapter_deploy)
        self.run_cmd([
            "mlx_lm.lora",
            "--model", self.deploy_model,
            "--train",
            "--data", "training_data/",
            "--iters", str(self.deploy_iters),
            "--learning-rate", str(self.lr),
            "--lora-rank", str(self.lora_rank),
            "--save-every", "100",          # checkpoint mỗi 100 iters
            "--adapter-path", adapter_deploy,
            *resume_flags_deploy,           # PERF-06 FIX: resume nếu crash
        ], label="train_1.7b")

        # Step 4d: Fuse → GGUF → Ollama
        info(f"E4d: Fuse adapter + convert GGUF + Ollama deploy")
        fused_path = f"models/{self.domain_slug}-1.7b-fused"
        self.run_cmd([
            "mlx_lm.fuse",
            "--model", self.deploy_model,
            "--adapter-path", adapter_deploy,
            "--save-path", fused_path,
        ], label="fuse_1.7b", check=False)

        gguf_path = f"models/{self.domain_slug}-1.7b.gguf"
        self.run_cmd([
            sys.executable, "convert_to_gguf.py",
            fused_path, "--outtype", "q4_K_M",
        ], label="convert_gguf", check=False)

        # Verify chat template (CRITICAL!)
        info("E4e: Verify chat template (CRITICAL — không bỏ qua!)")
        self.run_cmd([
            sys.executable, "scripts/verify_chat_template.py",
            "--model", gguf_path,
        ], label="verify_template", check=False)

        # Ollama deploy
        modelfile_path = self._create_modelfile(gguf_path)
        self.run_cmd([
            "ollama", "create", self.ollama_name,
            "-f", modelfile_path,
        ], label="ollama_create", check=False)

        ok(f"Model deployed: ollama run {self.ollama_name}")
        self.results["E4"] = {"status": "done", "model": self.ollama_name}

    def _create_modelfile(self, gguf_path: str) -> str:
        """Tạo Ollama Modelfile."""
        dataset_cfg = self.cfg.get("dataset", {})
        persona = dataset_cfg.get("persona", "Bạn là trợ lý AI hữu ích.")
        modelfile_content = f"""FROM {gguf_path}
SYSTEM \"\"\"{persona}\"\"\"
PARAMETER temperature 0.3
PARAMETER top_p 0.9
PARAMETER num_predict 512
"""
        modelfile_path = f"models/Modelfile.{self.domain_slug}"
        Path(modelfile_path).parent.mkdir(parents=True, exist_ok=True)
        Path(modelfile_path).write_text(modelfile_content, encoding="utf-8")
        return modelfile_path

    # ── E5: Evaluation ────────────────────────────────────────────

    def run_e5(self, model_name: str = None, is_anchor_check: bool = False):
        if not is_anchor_check:
            head("E5: Evaluation")

        model = model_name or self.ollama_name
        rc = self.run_cmd([
            sys.executable, "scripts/evaluate_model.py",
            "--model", model,
            "--domain", str(self.domain_path),
            "--ood-test", "training_data/ood_test_set.jsonl",
        ], label="evaluate", check=False)

        if rc != 0 and is_anchor_check:
            fail("Anchor eval FAILED!")
            fail("━" * 50)
            fail("VẤN ĐỀ LÀ DATASET — không phải model hay số iters.")
            fail("Hành động cần làm:")
            fail("  1. Xem xét lại extraction prompt trong claude_extractor.py")
            fail("  2. Tăng số situations trong insights/validated/")
            fail("  3. Kiểm tra quality gate pass rate")
            fail("  4. Đảm bảo refusal samples >= 80")
            fail("  5. Retrain anchor sau khi fix dataset")
            fail("━" * 50)
            raise RuntimeError("Anchor evaluation failed — fix dataset first!")

        self.results["E5"] = {"status": "done", "passed": rc == 0}

    # ── E6: Deploy ────────────────────────────────────────────────

    def run_e6(self):
        head("E6: Deploy — RAG Server + Conversation Logger")

        info("E6: RAG Server test")
        self.run_cmd([
            sys.executable, "rag_engine/rag_server.py",
            "--domain", str(self.domain_path),
            "--query", "Tư vấn thế nào khi khách hỏi giá ngay câu đầu?",
        ], label="rag_test", check=False)

        print()
        ok("Pipeline E6 hoàn thành!")
        print()
        print(f"  🚀 Start RAG server:")
        print(f"     DOMAIN_CONFIG={self.domain_path} uvicorn rag_engine.rag_server:app --port 8000")
        print()
        print(f"  💬 Test chatbot:")
        print(f"     curl -X POST http://localhost:8000/chat \\")
        print(f"          -H 'Content-Type: application/json' \\")
        print(f"          -d '{{\"message\": \"Khách hỏi giá ngay, tôi xử lý thế nào?\"}}'")
        print()
        print(f"  📊 Train domain mới:")
        print(f"     python scripts/pipeline.py --domain domains/suc_khoe.yaml")

        self.results["E6"] = {"status": "done"}

    # ── Main run ──────────────────────────────────────────────────

    def run(self):
        """Run full pipeline."""
        self._print_header()

        steps = [
            ("E1",   self.run_e1),
            ("E2",   self.run_e2),
            ("E2.5", self.run_e25),
            ("E3",   self.run_e3),
            ("E4",   self.run_e4),
            ("E5",   self.run_e5),
            ("E6",   self.run_e6),
        ]

        for step_id, step_fn in steps:
            if not self.should_run(step_id):
                info(f"⏭  Skip {step_id} (--from-step {self.from_step})")
                continue
            if step_id in self.skip_steps:
                info(f"⏭  Skip {step_id} (--skip)")
                continue

            try:
                step_fn()
            except RuntimeError as e:
                fail(f"\n{'='*55}")
                fail(f"Pipeline DỪNG tại {step_id}: {e}")
                fail(f"{'='*55}")
                self._print_summary(failed_at=step_id)
                sys.exit(1)
            except KeyboardInterrupt:
                warn("\nPipeline bị interrupt bởi user")
                self._print_summary(failed_at=step_id)
                sys.exit(1)

        self._print_summary()

    def _print_header(self):
        print()
        print(_c("1;32", "╔══════════════════════════════════════════════════╗"))
        print(_c("1;32", "║") + _c("1;37", "       🕷️  Phidipus AI Training Factory          ") + _c("1;32", "║"))
        print(_c("1;32", "╚══════════════════════════════════════════════════╝"))
        print()
        info(f"Domain:    {self.domain_name}")
        info(f"Config:    {self.domain_path}")
        info(f"From step: {self.from_step}")
        info(f"Dry run:   {self.dry_run}")
        info(f"Whisper:   {self.whisper_model}")
        print()

    def _print_summary(self, failed_at: str = None):
        elapsed = int(time.time() - self.start_time)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        print()
        print("═" * 55)
        if failed_at:
            fail(f"Pipeline FAILED tại {failed_at}")
        else:
            ok("Pipeline HOÀN THÀNH!")
        ok(f"Domain: {self.domain_name}")
        ok(f"Thời gian: {h:02d}:{m:02d}:{s:02d}")
        if not failed_at:
            ok(f"Model: {self.ollama_name}")
            ok(f"RAG: uvicorn rag_engine.rag_server:app --port 8000")
        print("═" * 55)


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Phidipus AI Training Factory — Full Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline (bán hàng):
  python scripts/pipeline.py --domain domains/ban_hang.yaml

  # Từ E3 (đã có data, chỉ train):
  python scripts/pipeline.py --domain domains/ban_hang.yaml --from-step E3

  # Không train (chỉ prep data):
  python scripts/pipeline.py --domain domains/ban_hang.yaml --skip-train

  # Domain mới (y tế):
  python scripts/pipeline.py --domain domains/suc_khoe.yaml

  # Dry run (kiểm tra config):
  python scripts/pipeline.py --domain domains/ban_hang.yaml --dry-run

  # Dùng Whisper medium (nhanh hơn):
  python scripts/pipeline.py --domain domains/ban_hang.yaml --whisper medium
        """
    )
    parser.add_argument("--domain",     required=True, help="Domain YAML config path")
    parser.add_argument("--from-step",  default="E1",
                        choices=STEP_ORDER, help="Bắt đầu từ step nào")
    parser.add_argument("--skip-train", action="store_true", help="Skip E4 training")
    parser.add_argument("--skip",       nargs="*", default=[],
                        help="Skip specific steps (vd: --skip E1 E6)")
    parser.add_argument("--dry-run",    action="store_true", help="In commands nhưng không chạy")
    parser.add_argument("--whisper",    default="large-v3",
                        choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="Whisper model size")
    args = parser.parse_args()

    pipeline = Pipeline(
        domain_config_path=args.domain,
        from_step=args.from_step,
        skip_train=args.skip_train,
        dry_run=args.dry_run,
        whisper_model=args.whisper,
        skip_steps=args.skip,
    )
    pipeline.run()


if __name__ == "__main__":
    main()
