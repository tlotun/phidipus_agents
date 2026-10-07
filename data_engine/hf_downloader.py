#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
data_engine/hf_downloader.py — Phidipus AI Forge E1
════════════════════════════════════════════════════════

HuggingFace Dataset Downloader: Tải datasets NLP tiếng Việt/đa ngôn ngữ.

Cài đặt:
  pip install datasets huggingface_hub

Chạy:
  python data_engine/hf_downloader.py --domain domains/ban_hang.yaml
  python data_engine/hf_downloader.py --dataset thu-coai/DialogStitch --split train --max-samples 5000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import yaml


HF_OUTPUT_DIR = "raw_corpus/huggingface"

RECOMMENDED_DATASETS = {
    "thu-coai/DialogStitch": {
        "desc": "Hội thoại đa lượt tiếng Việt",
        "text_columns": ["source", "target"],
        "filter_lang": None,
        "max_samples": 5000,
    },
    "HuggingFaceH4/ultrachat_200k": {
        "desc": "Instruction tuning QA",
        "text_columns": ["prompt", "messages"],
        "filter_lang": "en",
        "max_samples": 3000,
    },
    "databricks/databricks-dolly-15k": {
        "desc": "QA instruction dataset",
        "text_columns": ["instruction", "response"],
        "filter_lang": None,
        "max_samples": 2000,
    },
    "wikimedia/wikipedia": {
        "desc": "Wikipedia tiếng Việt",
        "text_columns": ["text"],
        "filter_lang": "vi",
        "max_samples": 1000,
        "config_name": "20231101.vi",
    },
}


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")
def fail(msg: str) -> None: print(f"{_c('0;31', '[FAIL]')}  {msg}")


def download_dataset(
    dataset_id: str,
    split: str = "train",
    max_samples: int = 5000,
    output_dir: str = HF_OUTPUT_DIR,
    config_name: Optional[str] = None,
) -> Optional[Path]:
    """Download 1 HuggingFace dataset → save as JSONL."""
    try:
        from datasets import load_dataset
    except ImportError:
        fail("Cần cài: pip install datasets")
        sys.exit(1)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dataset_safe = dataset_id.replace("/", "_")
    out_file = out_dir / f"{dataset_safe}_{split}_{max_samples}.jsonl"

    if out_file.exists():
        info(f"⏭  Skip: {out_file.name} đã có")
        return out_file

    info(f"📥 Tải: {dataset_id} ({split}, max={max_samples:,})")

    try:
        kwargs = {"split": split, "streaming": True}
        if config_name:
            kwargs["name"] = config_name

        ds = load_dataset(dataset_id, **kwargs)

        samples = []
        for item in ds:
            samples.append(item)
            if len(samples) >= max_samples:
                break

        with open(out_file, "w", encoding="utf-8") as f:
            for sample in samples:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")

        ok(f"   Đã lưu {len(samples):,} samples → {out_file.name}")
        return out_file

    except Exception as e:
        fail(f"   Lỗi tải {dataset_id}: {e}")
        return None


def extract_text_from_hf(
    jsonl_path: Path,
    dataset_id: str,
    output_dir: str = "raw_corpus/cleaned",
) -> Optional[Path]:
    """Extract text từ HF JSONL → plain text."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = RECOMMENDED_DATASETS.get(dataset_id, {})
    text_columns = meta.get("text_columns", ["text"])

    out_file = out_dir / (jsonl_path.stem + "_extracted.txt")
    if out_file.exists():
        info(f"⏭  Skip extract: {out_file.name}")
        return out_file

    texts = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                for col in text_columns:
                    val = item.get(col, "")
                    if isinstance(val, list):
                        # messages format
                        for m in val:
                            if isinstance(m, dict) and m.get("content"):
                                texts.append(str(m["content"]))
                    elif isinstance(val, str) and len(val) > 20:
                        texts.append(val)
            except json.JSONDecodeError:
                continue

    if not texts:
        warn(f"Không extract được text từ {jsonl_path.name}")
        return None

    out_file.write_text("\n\n".join(texts), encoding="utf-8")
    ok(f"   Extracted {len(texts):,} texts → {out_file.name}")
    return out_file


def main():
    parser = argparse.ArgumentParser(
        description="HuggingFace Downloader — Phidipus AI Forge E1"
    )
    parser.add_argument("--domain",      help="Domain YAML để lấy dataset list")
    parser.add_argument("--dataset",     help="Dataset ID (vd: thu-coai/DialogStitch)")
    parser.add_argument("--split",       default="train")
    parser.add_argument("--max-samples", type=int, default=5000)
    parser.add_argument("--output",      default=HF_OUTPUT_DIR)
    parser.add_argument("--list",        action="store_true", help="Hiện recommended datasets")
    args = parser.parse_args()

    if args.list:
        print("\nRecommended HuggingFace Datasets:")
        for ds_id, meta in RECOMMENDED_DATASETS.items():
            print(f"  {ds_id}")
            print(f"    {meta['desc']} | max_samples: {meta['max_samples']}")
        return

    datasets_to_download = []

    if args.domain:
        with open(args.domain, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        hf_datasets = cfg.get("data_sources", {}).get("huggingface_datasets", [])
        for ds in hf_datasets:
            meta = RECOMMENDED_DATASETS.get(ds, {})
            datasets_to_download.append((ds, meta.get("max_samples", args.max_samples)))
    elif args.dataset:
        datasets_to_download.append((args.dataset, args.max_samples))
    else:
        fail("Cần --domain hoặc --dataset")
        sys.exit(1)

    for ds_id, max_s in datasets_to_download:
        meta = RECOMMENDED_DATASETS.get(ds_id, {})
        out = download_dataset(
            dataset_id=ds_id,
            split=args.split,
            max_samples=max_s,
            output_dir=args.output,
            config_name=meta.get("config_name"),
        )
        if out:
            extract_text_from_hf(out, ds_id)


if __name__ == "__main__":
    main()
