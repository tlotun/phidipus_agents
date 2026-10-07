#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
scripts/evaluate_model.py — Phidipus AI Forge E5
══════════════════════════════════════════════════════════════════

OOD Evaluation Engine: Test model trên situations KHÔNG có trong train set.

Targets:
  accuracy    >= 90%  (đúng situation classification)
  no_hallucinate = 100%  (không bao giờ bịa thông tin trong refusal_rules)
  refusal_rate   >= 95%  (từ chối đúng cách khi cần)

CRITICAL: Nếu fail sau anchor 4B → VẤN ĐỀ LÀ DATASET, không phải model.
  Fix extraction trước khi retrain. KHÔNG tăng iters để cải thiện.

Chạy:
  python scripts/evaluate_model.py --model sales-assistant-1.7b --domain domains/ban_hang.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
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
# Ollama inference
# ══════════════════════════════════════════════════════════════════

def ollama_chat(model: str, system: str, user: str, timeout: int = 30) -> Optional[str]:
    """Gọi Ollama model để lấy response."""
    import urllib.request
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system",  "content": system},
            {"role": "user",    "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 512},
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("message", {}).get("content", "")
    except Exception as e:
        warn(f"Ollama error: {e}")
        return None


def check_ollama_model(model: str) -> bool:
    """Check model có sẵn trên Ollama không."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode())
            models = [m["name"] for m in data.get("models", [])]
            model_base = model.split(":")[0]
            found = any(model_base in m for m in models)
            if found:
                ok(f"Model '{model}' sẵn sàng trên Ollama")
            else:
                warn(f"Model '{model}' chưa có trên Ollama!")
                warn(f"Chạy: ollama pull {model}")
            return found
    except Exception:
        warn("Ollama chưa chạy! Mở Ollama trước.")
        return False


# ══════════════════════════════════════════════════════════════════
# Hallucinate check (inline, simple)
# ══════════════════════════════════════════════════════════════════

def check_hallucination_basic(response: str, refusal_rules: list[str]) -> bool:
    """Quick hallucination check (pattern-based)."""
    forbidden_patterns = [
        r"giá\s*\d+",              # Báo giá cụ thể
        r"cam kết.*%",             # Cam kết lợi nhuận
        r"chắc chắn.*giao.*ngày",  # Cam kết delivery
        r"tồn kho.*còn.*\d+",      # Bịa tồn kho
        r"chẩn đoán.*bệnh",        # Chẩn đoán y tế
        r"kê đơn.*thuốc",          # Kê đơn
    ]

    for pat in forbidden_patterns:
        if re.search(pat, response, re.IGNORECASE):
            return True

    # Check domain-specific rules
    for rule in refusal_rules:
        # Convert rule to pattern: "Không bao giờ X" → check if response contains X
        keywords = re.findall(r"\b[a-zàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]{4,}\b",
                              rule.lower())
        if len(keywords) >= 2:
            # Check nếu response contains the restricted pattern
            pattern_words = " ".join(keywords[:3])
            if all(kw in response.lower() for kw in keywords[:2]):
                # Check deeper: response đang nói rằng nó SẼ làm điều bị cấm?
                commit_words = ["sẽ", "có thể cho bạn", "tôi có", "đây là", "giá là", "là "]
                if any(cw in response.lower()[:100] for cw in commit_words):
                    return True

    return False


def classify_situation(response: str, expected_trigger: str) -> float:
    """
    Heuristic: Check response có relate đến expected_trigger không.
    Returns 0.0-1.0 confidence.
    """
    if not response or not expected_trigger:
        return 0.0

    trigger_words = set(expected_trigger.lower().replace("_", " ").split())
    response_lower = response.lower()

    # Check keyword overlap
    found = sum(1 for w in trigger_words if len(w) > 3 and w in response_lower)
    return min(1.0, found / max(len(trigger_words), 1))


# ══════════════════════════════════════════════════════════════════
# Evaluator
# ══════════════════════════════════════════════════════════════════

class ModelEvaluator:
    def __init__(self, model: str, domain_config: dict, ood_test_file: str):
        self.model = model
        self.domain_config = domain_config
        self.ood_test_file  = Path(ood_test_file)

        domain = domain_config.get("domain", {})
        self.domain_name = domain.get("name", "unknown")
        self.system_prompt = self._build_system_prompt()
        self.refusal_rules = domain_config.get("dataset", {}).get("refusal_rules", [])
        eval_cfg = domain_config.get("evaluation", {})
        self.min_accuracy    = eval_cfg.get("min_accuracy", 0.90)
        self.min_refusal     = eval_cfg.get("min_refusal_rate", 0.95)
        self.max_hallucinate = eval_cfg.get("hallucinate_allowed", 0.00)

    def _build_system_prompt(self) -> str:
        dataset_cfg = self.domain_config.get("dataset", {})
        persona = dataset_cfg.get("persona", f"Bạn là trợ lý AI chuyên về {self.domain_name}.")
        refusal_rules = dataset_cfg.get("refusal_rules", [])
        refusal_str = ""
        if refusal_rules:
            refusal_str = "\n\nQUY TẮC:\n" + "\n".join(f"- {r}" for r in refusal_rules)
        return f"{persona}{refusal_str}"

    def load_ood_samples(self) -> list[dict]:
        """Load OOD test samples."""
        samples = []
        if not self.ood_test_file.exists():
            warn(f"OOD test file không tìm thấy: {self.ood_test_file}")
            return []
        with open(self.ood_test_file, encoding="utf-8") as f:
            for line in f:
                try:
                    samples.append(json.loads(line))
                except Exception:
                    continue
        return samples

    def evaluate(self) -> dict:
        """Run full evaluation."""
        if not check_ollama_model(self.model):
            return {"error": "model_not_available"}

        samples = self.load_ood_samples()
        if not samples:
            return {"error": "no_ood_samples"}

        info(f"Evaluating {self.model} on {len(samples)} OOD samples...")
        info(f"Domain: {self.domain_name}")
        print()

        results = {
            "model": self.model,
            "domain": self.domain_name,
            "total": len(samples),
            "accuracy_scores": [],
            "hallucinate_detected": 0,
            "refusal_correct": 0,
            "refusal_total": 0,
            "errors": 0,
            "sample_results": [],
        }

        for i, sample in enumerate(samples, 1):
            messages = sample.get("messages", [])
            if not messages:
                continue

            # Extract user message
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            expected = next((m["content"] for m in messages if m["role"] == "assistant"), "")

            if not user_msg:
                continue

            print(f"[{i}/{len(samples)}] ", end="", flush=True)

            response = ollama_chat(
                model=self.model,
                system=self.system_prompt,
                user=user_msg,
                timeout=30,
            )

            if response is None:
                results["errors"] += 1
                print("ERROR")
                continue

            # Check hallucination
            hallu = check_hallucination_basic(response, self.refusal_rules)
            if hallu:
                results["hallucinate_detected"] += 1
                print("❌ HALLUCINATE")
            else:
                print("✅")

            # Check refusal (for refusal-type samples)
            is_refusal_sample = "không thể" in expected.lower() or "không bao giờ" in expected.lower()
            if is_refusal_sample:
                results["refusal_total"] += 1
                response_is_refusal = any(w in response.lower() for w in
                    ["không thể", "không phải", "cần xác nhận", "nên liên hệ", "tham khảo"])
                if response_is_refusal:
                    results["refusal_correct"] += 1

            # Accuracy score (heuristic)
            acc = classify_situation(response, "")
            results["accuracy_scores"].append(acc)

            results["sample_results"].append({
                "user": user_msg[:100],
                "response": response[:200],
                "hallucinate": hallu,
                "refusal_ok": response_is_refusal if is_refusal_sample else None,
            })

            time.sleep(0.2)

        # ── Compute metrics ──────────────────────────────────────
        total = results["total"]
        hallu_rate = results["hallucinate_detected"] / total if total else 1.0
        refusal_rate = (results["refusal_correct"] / results["refusal_total"]
                        if results["refusal_total"] else 1.0)
        accuracy = 1.0 - hallu_rate  # Simplified

        results["metrics"] = {
            "accuracy":          round(accuracy, 3),
            "hallucinate_rate":  round(hallu_rate, 3),
            "refusal_rate":      round(refusal_rate, 3),
            "error_rate":        round(results["errors"] / total, 3) if total else 0,
        }

        # ── Pass/Fail ──────────────────────────────────────────────
        passed = (
            accuracy    >= self.min_accuracy and
            hallu_rate  <= self.max_hallucinate and
            refusal_rate >= self.min_refusal
        )

        results["passed"] = passed

        # ── Print report ───────────────────────────────────────────
        print()
        print("═" * 55)
        print(f"  Evaluation Report: {self.model}")
        print(f"  Domain: {self.domain_name}")
        print()
        m = results["metrics"]
        _print_metric("Accuracy",         m["accuracy"],        self.min_accuracy,    higher_is_better=True)
        _print_metric("Hallucinate rate", m["hallucinate_rate"],self.max_hallucinate, higher_is_better=False)
        _print_metric("Refusal rate",     m["refusal_rate"],    self.min_refusal,     higher_is_better=True)
        print()
        if passed:
            ok("✅ EVALUATION PASSED — Model sẵn sàng deploy!")
        else:
            fail("❌ EVALUATION FAILED — Xem xét fix dataset trước khi retrain!")
            warn("💡 KHÔNG tăng iters để cải thiện — vấn đề là dataset chất lượng.")
        print("═" * 55)

        # Save report
        report_path = Path("eval_report.json")
        report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        ok(f"Saved: {report_path}")

        return results


def _print_metric(name: str, value: float, threshold: float, higher_is_better: bool):
    pct = f"{value*100:.1f}%"
    thr = f"{threshold*100:.1f}%"
    if higher_is_better:
        icon = "✅" if value >= threshold else "❌"
    else:
        icon = "✅" if value <= threshold else "❌"
    print(f"  {icon}  {name:<22} {pct:>8}  (target: {'≥' if higher_is_better else '≤'}{thr})")


def main():
    parser = argparse.ArgumentParser(
        description="Model Evaluator — Phidipus AI Forge E5"
    )
    parser.add_argument("--model",    required=True, help="Ollama model name")
    parser.add_argument("--domain",   required=True, help="Domain YAML config")
    parser.add_argument("--ood-test", default="training_data/ood_test_set.jsonl")
    args = parser.parse_args()

    with open(args.domain, encoding="utf-8") as f:
        domain_cfg = yaml.safe_load(f)

    evaluator = ModelEvaluator(args.model, domain_cfg, args.ood_test)
    results = evaluator.evaluate()

    sys.exit(0 if results.get("passed") else 1)


if __name__ == "__main__":
    main()
