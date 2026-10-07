#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
scripts/generate_domain_dataset.py — Phidipus AI Forge E3
════════════════════════════════════════════════════════════════

Dataset Generator: Situations JSON → ChatML JSONL (train/valid/ood_test)

Phân bố chuẩn:
  60% positive  — tình huống + xử lý tốt
  12% refusal   — từ chối bịa đặt (≥80 samples bắt buộc)
  15% contrastive — so sánh đúng vs sai side-by-side
   8% recovery  — xử lý khi mắc lỗi
   5% edge_case — OOD / edge cases

Format output: ChatML JSONL cho MLX LoRA training
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}

Chạy:
  python scripts/generate_domain_dataset.py --domain domains/ban_hang.yaml
  python scripts/generate_domain_dataset.py --domain domains/suc_khoe.yaml --target 800
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Optional

import yaml


INSIGHTS_DIR     = "insights/validated"
TRAINING_DIR     = "training_data"
TRAIN_RATIO      = 0.90
VALID_RATIO      = 0.05
OOD_RATIO        = 0.05


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")


# ══════════════════════════════════════════════════════════════════
# Template builders
# ══════════════════════════════════════════════════════════════════

def build_system_prompt(domain_config: dict) -> str:
    """Build system prompt từ domain config."""
    domain = domain_config.get("domain", {})
    dataset_cfg = domain_config.get("dataset", {})
    domain_name = domain.get("name", "")
    persona = dataset_cfg.get("persona", f"Bạn là trợ lý AI chuyên về {domain_name}.")
    refusal_rules = dataset_cfg.get("refusal_rules", [])

    refusal_str = ""
    if refusal_rules:
        refusal_str = "\n\nQUY TẮC BẮT BUỘC:\n" + "\n".join(f"- {r}" for r in refusal_rules)

    return f"{persona}{refusal_str}"


# User message variants cho diversity
USER_TEMPLATES_POSITIVE = [
    "Khách hàng {context}. Tôi nên xử lý như thế nào?",
    "{context}. Cho tôi biết cách phản hồi tốt nhất?",
    "Tình huống: {context}. Chiến lược xử lý?",
    "Tôi đang gặp tình huống: {context}. Giúp tôi với?",
    "{context}. Theo bạn, tôi nên nói gì?",
    "Xử lý thế nào khi {context}?",
    "Có cách nào tốt để {context} không?",
]

USER_TEMPLATES_REFUSAL = [
    "Khách hỏi: {question}",
    "Cho tôi biết {question}",
    "{question} — bạn có thể trả lời không?",
    "Tôi cần biết {question}",
    "{question}?",
]

REFUSAL_QUESTIONS_GENERIC = [
    "giá chính xác của sản phẩm là bao nhiêu",
    "bao giờ hàng về kho",
    "đối thủ X có tệ hơn mình không",
    "cam kết bao nhiêu phần trăm lợi nhuận",
    "chắc chắn sẽ có discount không",
    "tồn kho còn bao nhiêu cái",
    "delivery trong vòng mấy ngày chắc chắn",
]

def build_refusal_question(domain_config: dict, idx: int) -> str:
    """Build refusal question từ domain refusal_rules."""
    dataset_cfg = domain_config.get("dataset", {})
    refusal_rules = dataset_cfg.get("refusal_rules", [])

    if refusal_rules and idx < len(refusal_rules):
        rule = refusal_rules[idx % len(refusal_rules)]
        # Convert rule to question: "Không bao giờ X" → "X?"
        question = rule.replace("Không bao giờ ", "").replace("Không ", "").strip()
        question = question.rstrip(".")
        return question
    return random.choice(REFUSAL_QUESTIONS_GENERIC)


def build_refusal_response(domain_config: dict, question: str) -> str:
    """Build refusal response chuẩn mực."""
    domain_name = domain_config.get("domain", {}).get("name", "lĩnh vực này")
    templates = [
        f"Tôi hiểu bạn muốn biết về {question}. Tuy nhiên, thông tin này cần được xác nhận trực tiếp từ chuyên gia có thẩm quyền. Tôi không muốn cung cấp thông tin không chính xác có thể gây hiểu lầm. Bạn có muốn tôi kết nối bạn với người có thể giải đáp chính xác hơn không?",
        f"Câu hỏi về {question} rất quan trọng và tôi muốn đảm bảo bạn nhận được thông tin chính xác nhất. Để tránh nhầm lẫn, tốt nhất bạn nên liên hệ trực tiếp để được xác nhận cụ thể. Tôi có thể giúp bạn chuẩn bị các câu hỏi cần hỏi không?",
        f"Tôi không có đủ thông tin để trả lời chính xác về {question} ngay lúc này. Thay vì đưa ra số liệu không chắc chắn, tôi nghĩ tốt hơn là chúng ta nên tập trung vào việc tìm hiểu nhu cầu thực sự của bạn trước. Bạn cần {question} vì mục đích gì?",
    ]
    return random.choice(templates)


def sit_to_messages(sit: dict, domain_config: dict, sample_type: str, system_prompt: str) -> Optional[dict]:
    """Convert 1 situation → 1 ChatML message dict."""
    context = sit.get("context", "")
    example = sit.get("example_response", "")
    bad = sit.get("bad_response", "")
    trigger = sit.get("trigger", "")

    if not context or not example:
        return None

    if sample_type == "positive":
        user_tmpl = random.choice(USER_TEMPLATES_POSITIVE)
        user_msg = user_tmpl.format(context=context)
        assistant_msg = example

    elif sample_type == "negative_contrastive":
        if not bad:
            return None
        user_tmpl = random.choice(USER_TEMPLATES_POSITIVE)
        user_msg = user_tmpl.format(context=context)
        why_bad = sit.get("why_bad", "thiếu empathy và không giải quyết được nhu cầu thực sự")
        assistant_msg = (
            f"❌ Cách xử lý SAI: \"{bad}\"\n"
            f"Vấn đề: {why_bad}\n\n"
            f"✅ Cách xử lý ĐÚNG:\n{example}"
        )

    elif sample_type == "recovery":
        bad_to_use = bad if bad else "Xin lỗi, tôi không biết."
        user_msg = (
            f"Tôi vừa trả lời: \"{bad_to_use[:80]}\" "
            f"khi khách {context}. Tôi đã sai ở đâu và nên sửa như thế nào?"
        )
        why_bad = sit.get("why_bad", "chưa đúng")
        assistant_msg = (
            f"Câu trả lời đó có vấn đề vì {why_bad}. "
            f"Bạn có thể sửa lại như sau:\n\n{example}"
        )

    elif sample_type == "edge_case":
        user_msg = f"Điều gì xảy ra nếu tình huống {context} nhưng phức tạp hơn thông thường?"
        assistant_msg = (
            f"Trong tình huống đặc biệt như vậy, cần thận trọng hơn. "
            f"Nguyên tắc cơ bản vẫn là: {example} "
            f"Tuy nhiên, hãy điều chỉnh theo hoàn cảnh cụ thể."
        )

    else:
        return None

    return {
        "messages": [
            {"role": "system",    "content": system_prompt},
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ]
    }


def refusal_to_messages(domain_config: dict, idx: int, system_prompt: str) -> dict:
    """Tạo refusal sample."""
    question = build_refusal_question(domain_config, idx)
    user_tmpl = random.choice(USER_TEMPLATES_REFUSAL)
    user_msg = user_tmpl.format(question=question)
    assistant_msg = build_refusal_response(domain_config, question)

    return {
        "messages": [
            {"role": "system",    "content": system_prompt},
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ]
    }


# ══════════════════════════════════════════════════════════════════
# Dataset Generator
# ══════════════════════════════════════════════════════════════════

def generate_dataset(
    domain_config: dict,
    insights_dir: str = INSIGHTS_DIR,
    output_dir: str = TRAINING_DIR,
    target_samples: Optional[int] = None,
    seed: int = 42,
):
    """Main dataset generation function."""
    random.seed(seed)

    domain = domain_config.get("domain", {})
    dataset_cfg = domain_config.get("dataset", {})
    domain_name = domain.get("name", "unknown")
    target = target_samples or dataset_cfg.get("target_samples", 700)
    dist = dataset_cfg.get("distribution", {})

    n_positive    = int(target * dist.get("positive", 0.60))
    n_refusal     = max(80, int(target * dist.get("refusal", 0.12)))
    n_contrastive = int(target * dist.get("contrastive", 0.15))
    n_recovery    = int(target * dist.get("recovery", 0.08))
    n_edge        = int(target * dist.get("edge_case", 0.05))

    info(f"Domain: {domain_name}")
    info(f"Target: {target} samples")
    info(f"Distribution: +{n_positive} refusal={n_refusal} contrastive={n_contrastive} recovery={n_recovery} edge={n_edge}")
    print()

    # Load validated situations
    in_path = Path(insights_dir)
    merged_file = in_path / "_all_situations_merged.json"

    situations = []
    if merged_file.exists():
        data = json.loads(merged_file.read_text(encoding="utf-8"))
        situations = data.get("situations", [])
    else:
        for f in in_path.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                situations.extend(data.get("situations", []))
            except Exception:
                continue

    if not situations:
        warn(f"Không tìm thấy situations trong {insights_dir}")
        warn("Chạy insight_engine/claude_extractor.py và quality_gate.py trước!")
        return

    ok(f"Loaded {len(situations)} validated situations")

    system_prompt = build_system_prompt(domain_config)

    # ── Generate samples ──────────────────────────────────────────
    all_samples = []

    # 1. Positive
    random.shuffle(situations)
    for sit in situations[:n_positive]:
        s = sit_to_messages(sit, domain_config, "positive", system_prompt)
        if s:
            all_samples.append({"type": "positive", **s})

    # 2. Refusal (bắt buộc ≥80)
    for i in range(n_refusal):
        s = refusal_to_messages(domain_config, i, system_prompt)
        all_samples.append({"type": "refusal", **s})

    # 3. Contrastive
    contrastive_pool = [s for s in situations if s.get("bad_response")]
    random.shuffle(contrastive_pool)
    for sit in contrastive_pool[:n_contrastive]:
        s = sit_to_messages(sit, domain_config, "negative_contrastive", system_prompt)
        if s:
            all_samples.append({"type": "contrastive", **s})

    # 4. Recovery
    recovery_pool = [s for s in situations if s.get("bad_response")]
    random.shuffle(recovery_pool)
    for sit in recovery_pool[:n_recovery]:
        s = sit_to_messages(sit, domain_config, "recovery", system_prompt)
        if s:
            all_samples.append({"type": "recovery", **s})

    # 5. Edge cases
    random.shuffle(situations)
    for sit in situations[:n_edge]:
        s = sit_to_messages(sit, domain_config, "edge_case", system_prompt)
        if s:
            all_samples.append({"type": "edge_case", **s})

    # ── Split train/valid/ood ──────────────────────────────────────
    random.shuffle(all_samples)
    total = len(all_samples)
    n_ood   = max(50, int(total * OOD_RATIO))
    n_valid = max(30, int(total * VALID_RATIO))
    n_train = total - n_ood - n_valid

    # OOD set = refusal samples (hardest, out-of-distribution)
    refusal_samples = [s for s in all_samples if s["type"] == "refusal"]
    ood_set = refusal_samples[:n_ood]
    rest = [s for s in all_samples if s not in ood_set]
    random.shuffle(rest)
    valid_set = rest[:n_valid]
    train_set = rest[n_valid:]

    # ── Save ──────────────────────────────────────────────────────
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    def save_jsonl(samples: list[dict], path: Path):
        with open(path, "w", encoding="utf-8") as f:
            for s in samples:
                # Remove "type" key (training only needs messages)
                clean = {"messages": s["messages"]}
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")

    domain_slug = domain_name.replace(" ", "_").replace("/", "_")[:20]

    train_path = out_path / "train.jsonl"
    valid_path = out_path / "valid.jsonl"
    ood_path   = out_path / "ood_test_set.jsonl"

    save_jsonl(train_set, train_path)
    save_jsonl(valid_set, valid_path)
    save_jsonl(ood_set,   ood_path)

    # Stats
    type_counts = {}
    for s in all_samples:
        t = s["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    stats = {
        "domain": domain_name,
        "total_samples": total,
        "train": len(train_set),
        "valid": len(valid_set),
        "ood_test": len(ood_set),
        "distribution": type_counts,
        "situations_used": len(situations),
    }
    (out_path / "_dataset_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print()
    print("═" * 55)
    ok(f"Domain: {domain_name}")
    ok(f"Total: {total} samples | Train: {len(train_set)} | Valid: {len(valid_set)} | OOD: {len(ood_set)}")
    for t, cnt in type_counts.items():
        print(f"  {t:<20} {cnt:4d} samples")
    print()
    ok(f"train.jsonl   → {train_path}")
    ok(f"valid.jsonl   → {valid_path}")
    ok(f"ood_test.jsonl→ {ood_path}")

    if type_counts.get("refusal", 0) < 80:
        warn(f"⚠️  Refusal samples = {type_counts.get('refusal', 0)} < 80! Cần thêm refusal samples!")
    else:
        ok(f"✅ Refusal samples = {type_counts.get('refusal', 0)} ≥ 80 (OK)")

    print("═" * 55)
    print()
    info("Tiếp theo:")
    info("  python scripts/augment_data.py (optional: thêm augmented samples)")
    info("  mlx_lm.lora --model Qwen/Qwen3-4B --train --data training_data/ --iters 600")


def main():
    parser = argparse.ArgumentParser(
        description="Dataset Generator — Phidipus AI Forge E3"
    )
    parser.add_argument("--domain",   required=True, help="Domain YAML config")
    parser.add_argument("--input",    default=INSIGHTS_DIR)
    parser.add_argument("--output",   default=TRAINING_DIR)
    parser.add_argument("--target",   type=int, default=None,
                        help="Override target sample count")
    parser.add_argument("--seed",     type=int, default=42)
    args = parser.parse_args()

    with open(args.domain, encoding="utf-8") as f:
        domain_cfg = yaml.safe_load(f)

    generate_dataset(
        domain_config=domain_cfg,
        insights_dir=args.input,
        output_dir=args.output,
        target_samples=args.target,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
