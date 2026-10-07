#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
elite_pipeline/run_pipeline.py — QwenElite PsySales Pipeline v1.0
═══════════════════════════════════════════════════════════════════

Pipeline 3-pass GCR (Generate → Critique → Revise) + Rule Engine
chạy 100% local trên Mac qua Ollama.

Cách dùng:
  # Tạo 1 tình huống (test nhanh)
  python elite_pipeline/run_pipeline.py --topic "Khách hỏi giá ngay câu đầu"

  # Tạo batch từ file topics
  python elite_pipeline/run_pipeline.py --topics-file elite_pipeline/topics.txt --output training_data/elite.jsonl

  # Dùng model khác
  python elite_pipeline/run_pipeline.py --topic "Khách so sánh đối thủ" --model qwen3:32b

  # Chỉ generate (bỏ GCR loop)
  python elite_pipeline/run_pipeline.py --topic "Khách chần chừ" --no-gcr

RAM cần thiết:
  qwen3:72b  → ~42GB (Mac Mini M4 Pro 64GB)
  qwen3:32b  → ~20GB (Mac Mini M4 Pro 36GB+)
  qwen3:8b   → ~5GB  (Mac Mini M1 16GB)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import urllib.request
import urllib.error

# ══════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════

GENERATOR_SYSTEM = """Bạn là Chuyên gia Tâm lý Khách hàng & Chiến lược Bán hàng cấp cao — 20+ năm kinh nghiệm trong Customer Psychology, Behavioral Economics và Strategic Sales Response.

Quy trình suy nghĩ BẮT BUỘC:
1. Hiểu trigger + bối cảnh sâu
2. Phân tích tâm lý 3 tầng: bề mặt → tầng sâu → động cơ ẩn
3. Xác định 2-4 nguyên tắc tâm lý học (Cialdini, Kahneman, etc.)
4. Chiến lược multi-step: bước + lý do tâm lý + dự phòng
5. Ví dụ tốt (empathetic, thuyết phục) và xấu (sai điển hình + giải thích)

QUY TẮC: Không sáo rỗng. Không generic. Tiếng Việt tự nhiên.
Chỉ output JSON đúng schema, không text khác."""

GENERATOR_SCHEMA = """{
  "id": "string",
  "topic": "string",
  "trigger": "string",
  "customer_psychology": {
    "surface": "cảm xúc bề mặt",
    "deep": "nỗi sợ/mong muốn tầng sâu",
    "hidden_motive": "động cơ ẩn thực sự"
  },
  "psychology_principles": ["list 2-4 nguyên tắc"],
  "strategy": {
    "objective": "mục tiêu",
    "tactics": ["bước 1", "bước 2", "bước 3"],
    "psychological_levers": ["lever 1", "lever 2"]
  },
  "good_example": "script tốt",
  "bad_example": "script xấu",
  "bad_reason": "giải thích lỗi",
  "effectiveness_score": 8.5,
  "meta": {"scenario_type": "string", "difficulty": "string"}
}"""

CRITIC_SYSTEM = """Bạn là Giám khảo chấm chất lượng tình huống CSKH/Sales.
Đánh giá NGHIÊM KHẮC theo rubric, không nể nang.
Chỉ output JSON, không text khác."""

CRITIC_PROMPT = """Đánh giá tình huống sau theo 5 tiêu chí (0-10):

{scenario_json}

Rubric:
1. psychology_depth: Tâm lý 3 tầng có sâu không? Có insight thực sự?
2. strategy_quality: Chiến lược có multi-step? Có gắn nguyên tắc tâm lý cụ thể?
3. example_realism: Ví dụ tốt/xấu có giống đời thật? Không sáo rỗng?
4. consistency: Trigger ↔ Psychology ↔ Strategy ↔ Examples nhất quán?
5. actionability: Nhân viên đọc xong có làm theo được ngay không?

Kiểm tra thêm:
- Có cụm sáo rỗng ("xin lỗi vì sự bất tiện", "cố gắng hết sức")?
- Psychology chỉ bề mặt (tức giận, buồn) mà thiếu phân tích sâu?
- Strategy chung chung ("lắng nghe, đồng cảm, giải quyết")?

Output JSON:
{
  "scores": {
    "psychology_depth": 0-10,
    "strategy_quality": 0-10,
    "example_realism": 0-10,
    "consistency": 0-10,
    "actionability": 0-10
  },
  "avg_score": number,
  "weaknesses": ["điểm yếu 1", "điểm yếu 2"],
  "improvements": ["gợi ý cải thiện 1", "gợi ý 2"],
  "has_cliche": true/false,
  "verdict": "PASS" hoặc "NEEDS_REVISION"
}"""

REVISER_SYSTEM = """Bạn là Biên tập viên cấp cao, chuyên nâng cấp chất lượng tình huống CSKH/Sales.
Dựa trên critique, cải thiện tình huống để đạt >= 8.5/10 ở MỌI tiêu chí.
Chỉ output JSON cải thiện theo đúng schema gốc, không text khác."""

REVISER_PROMPT = """Cải thiện tình huống này dựa trên critique:

TÌNH HUỐNG GỐC:
{scenario_json}

CRITIQUE:
{critique_json}

Yêu cầu:
- Sửa TẤT CẢ điểm yếu được chỉ ra
- Nâng cao psychology depth nếu yếu
- Thay thế mọi cụm sáo rỗng
- Chiến lược phải gắn nguyên tắc tâm lý cụ thể
- Ví dụ phải tự nhiên, đời thật

Output JSON đã cải thiện (cùng schema, tăng version lên v1.1+)."""


# ══════════════════════════════════════════════════════════
# OLLAMA CLIENT
# ══════════════════════════════════════════════════════════

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

def ollama_generate(
    prompt: str,
    system: str = "",
    model: str = "qwen3:72b",
    temperature: float = 0.65,
    max_tokens: int = 4096,
    timeout: int = 300,
) -> str:
    """Call Ollama API and return response text."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "num_predict": max_tokens,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data.get("response", "")
            # Strip <think>...</think> blocks (Qwen3 thinking mode)
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            return raw
    except urllib.error.URLError as e:
        print(f"  [ERROR] Ollama connection failed: {e}")
        print(f"  Kiểm tra: ollama serve đang chạy? Model {model} đã pull?")
        return ""
    except Exception as e:
        print(f"  [ERROR] {e}")
        return ""


def extract_json(text: str) -> dict | None:
    """Extract JSON from LLM output (handles markdown fences, extra text)."""
    if not text:
        return None

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE)
    try:
        return json.loads(cleaned.strip())
    except json.JSONDecodeError:
        pass

    # Find first { ... } block
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


# ══════════════════════════════════════════════════════════
# RULE ENGINE
# ══════════════════════════════════════════════════════════

BANNED_PHRASES = [
    "xin lỗi vì sự bất tiện",
    "mong anh thông cảm",
    "mong chị thông cảm",
    "mong quý khách thông cảm",
    "sẽ cố gắng hết sức",
    "cố gắng hết khả năng",
    "rất lấy làm tiếc",
    "chân thành xin lỗi",
    "bên em sẽ cố gắng",
    "xin lỗi vì sự chậm trễ",
]

SHALLOW_EMOTIONS = {"tức giận", "buồn", "thất vọng", "bực bội", "khó chịu"}
DEEP_KEYWORDS = {
    "loss aversion", "mất mát", "ego", "tự trọng", "social proof",
    "bằng chứng xã hội", "reciprocity", "có qua có lại", "anchoring",
    "neo giá", "scarcity", "khan hiếm", "fear of missing",
    "sợ bỏ lỡ", "cognitive dissonance", "bất hòa nhận thức",
    "sunk cost", "chi phí chìm", "decision fatigue", "mệt mỏi quyết định",
    "risk aversion", "ngại rủi ro", "confirmation bias", "thiên kiến xác nhận",
}

GENERIC_STRATEGY = [
    "bước 1: xin lỗi",
    "bước 1: lắng nghe",
    "bước 2: đồng cảm",
    "bước 3: giải quyết",
    "bước 3: đưa giải pháp",
]


class RuleEngine:
    """Validate and score situation quality."""

    def validate(self, record: dict) -> tuple[bool, list[str]]:
        """Validate a situation record. Returns (passed, issues)."""
        issues = []

        # 1. Required fields
        required = ["trigger", "customer_psychology", "strategy", "good_example", "bad_example"]
        for field in required:
            if field not in record or not record[field]:
                issues.append(f"Thiếu trường: {field}")

        # 2. Psychology depth
        psych = record.get("customer_psychology", {})
        if isinstance(psych, dict):
            for layer in ["surface", "deep", "hidden_motive"]:
                val = psych.get(layer, "")
                if len(str(val)) < 20:
                    issues.append(f"Psychology '{layer}' quá ngắn ({len(str(val))} chars)")

            # Check shallow-only psychology
            deep_text = str(psych.get("deep", "")) + str(psych.get("hidden_motive", ""))
            has_deep = any(kw in deep_text.lower() for kw in DEEP_KEYWORDS)
            only_shallow = all(
                em in str(psych.get("surface", "")).lower()
                for em in SHALLOW_EMOTIONS
                if em in str(psych).lower()
            )
            if not has_deep and only_shallow:
                issues.append("Psychology chỉ bề mặt — thiếu phân tích tâm lý học sâu")
        elif isinstance(psych, str) and len(psych) < 80:
            issues.append("Psychology quá ngắn — cần phân tích 3 tầng")

        # 3. Strategy depth
        strategy = record.get("strategy", {})
        if isinstance(strategy, dict):
            tactics = strategy.get("tactics", [])
            if len(tactics) < 2:
                issues.append("Strategy cần ít nhất 2 bước chiến thuật")
            # Check generic
            tactics_text = " ".join(str(t).lower() for t in tactics)
            generic_count = sum(1 for g in GENERIC_STRATEGY if g in tactics_text)
            if generic_count >= 2:
                issues.append("Strategy quá generic — cần kỹ thuật cụ thể")
        elif isinstance(strategy, str) and len(strategy) < 120:
            issues.append("Strategy quá ngắn")

        # 4. Banned phrases
        full_text = json.dumps(record, ensure_ascii=False).lower()
        for phrase in BANNED_PHRASES:
            if phrase in full_text:
                issues.append(f"Cụm sáo rỗng: \"{phrase}\"")

        # 5. Good/bad example length
        good = str(record.get("good_example", ""))
        bad = str(record.get("bad_example", ""))
        if len(good) < 80:
            issues.append(f"Ví dụ tốt quá ngắn ({len(good)} chars)")
        if len(bad) < 40:
            issues.append(f"Ví dụ xấu quá ngắn ({len(bad)} chars)")
        if good.strip() == bad.strip():
            issues.append("Ví dụ tốt và xấu giống nhau!")

        passed = len(issues) == 0
        return passed, issues

    def score(self, record: dict) -> float:
        """Score a record 0-10 based on rules."""
        score = 10.0
        _, issues = self.validate(record)
        score -= len(issues) * 1.5
        return max(0.0, min(10.0, round(score, 1)))


# ══════════════════════════════════════════════════════════
# PIPELINE
# ══════════════════════════════════════════════════════════

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def log_step(step: str, msg: str):
    print(f"  {_c('0;36', f'[{step}]')}  {msg}")

def log_ok(msg: str):
    print(f"  {_c('0;32', '[  OK]')}  {msg}")

def log_warn(msg: str):
    print(f"  {_c('1;33', '[WARN]')}  {msg}")


def run_single(
    topic: str,
    model: str = "qwen3:72b",
    gcr_loops: int = 2,
    min_score: float = 8.0,
    verbose: bool = True,
) -> dict | None:
    """Run elite pipeline for a single topic. Returns validated record or None."""

    rule_engine = RuleEngine()

    # ── PASS 1: Generate ──
    if verbose:
        log_step("P1", f"Generating scenario for: {topic}")
        log_step("P1", f"Model: {model}")

    t0 = time.time()
    gen_prompt = f"""Tạo tình huống CSKH/Sales có cấu trúc cho topic sau.
Topic: {topic}

Output JSON theo schema:
{GENERATOR_SCHEMA}

Ghi chú: id phải unique, effectiveness_score tự đánh giá 1-10."""

    raw = ollama_generate(gen_prompt, system=GENERATOR_SYSTEM, model=model)
    record = extract_json(raw)

    if not record:
        if verbose:
            log_warn(f"P1 failed — không parse được JSON ({len(raw)} chars)")
            if raw:
                print(f"    Raw output (first 200): {raw[:200]}...")
        return None

    record.setdefault("id", str(uuid.uuid4())[:8])
    record.setdefault("topic", topic)
    record["version"] = "v1.0"
    record["generated_at"] = datetime.now().isoformat()
    record["model"] = model

    gen_time = time.time() - t0
    if verbose:
        log_ok(f"P1 done ({gen_time:.1f}s)")

    if gcr_loops <= 0:
        # No GCR — just validate and return
        passed, issues = rule_engine.validate(record)
        if verbose:
            if passed:
                log_ok("Rule Engine: PASS")
            else:
                log_warn(f"Rule Engine: {len(issues)} issues")
                for iss in issues:
                    print(f"    ⚠️ {iss}")
        return record

    # ── GCR LOOPS ──
    for loop_idx in range(gcr_loops):
        loop_num = loop_idx + 1
        if verbose:
            print(f"\n  {'─' * 40}")
            log_step(f"GCR {loop_num}/{gcr_loops}", "Critique phase")

        # ── CRITIQUE ──
        t1 = time.time()
        critique_prompt = CRITIC_PROMPT.format(
            scenario_json=json.dumps(record, ensure_ascii=False, indent=2)
        )
        raw_critique = ollama_generate(
            critique_prompt, system=CRITIC_SYSTEM, model=model,
            temperature=0.3,  # Lower temp for more consistent evaluation
        )
        critique = extract_json(raw_critique)

        if not critique:
            if verbose:
                log_warn(f"Critique failed — skipping GCR loop {loop_num}")
            continue

        avg_score = critique.get("avg_score", 0)
        verdict = critique.get("verdict", "UNKNOWN")
        crit_time = time.time() - t1

        if verbose:
            scores = critique.get("scores", {})
            log_ok(f"Critique done ({crit_time:.1f}s) — avg: {avg_score:.1f}/10 — {verdict}")
            for k, v in scores.items():
                color = "0;32" if v >= 8 else "1;33" if v >= 6 else "0;31"
                print(f"    {_c(color, f'{v:4.1f}')}  {k}")
            for w in critique.get("weaknesses", []):
                print(f"    ⚠️ {w}")

        # Check if already good enough
        if avg_score >= min_score and verdict == "PASS":
            if verbose:
                log_ok(f"Score >= {min_score} — skipping revision")
            break

        # ── REVISE ──
        if verbose:
            log_step(f"GCR {loop_num}/{gcr_loops}", "Revise phase")

        t2 = time.time()
        revise_prompt = REVISER_PROMPT.format(
            scenario_json=json.dumps(record, ensure_ascii=False, indent=2),
            critique_json=json.dumps(critique, ensure_ascii=False, indent=2),
        )
        raw_revised = ollama_generate(
            revise_prompt, system=REVISER_SYSTEM, model=model,
        )
        revised = extract_json(raw_revised)

        if revised:
            revised["version"] = f"v1.{loop_num}"
            revised["generated_at"] = datetime.now().isoformat()
            revised["model"] = model
            revised.setdefault("id", record.get("id", str(uuid.uuid4())[:8]))
            revised.setdefault("topic", topic)
            record = revised
            rev_time = time.time() - t2
            if verbose:
                log_ok(f"Revised to v1.{loop_num} ({rev_time:.1f}s)")
        else:
            if verbose:
                log_warn("Revise failed — keeping previous version")

    # ── FINAL RULE ENGINE ──
    passed, issues = rule_engine.validate(record)
    rule_score = rule_engine.score(record)
    record["rule_engine_score"] = rule_score
    record["rule_engine_passed"] = passed

    if verbose:
        print(f"\n  {'═' * 40}")
        if passed:
            log_ok(f"FINAL: PASS — Rule score: {rule_score}/10")
        else:
            log_warn(f"FINAL: {len(issues)} issues — Rule score: {rule_score}/10")
            for iss in issues:
                print(f"    ⚠️ {iss}")

    return record


def run_batch(
    topics: list[str],
    model: str = "qwen3:72b",
    gcr_loops: int = 2,
    min_score: float = 8.0,
    output_path: str = "training_data/elite.jsonl",
) -> dict:
    """Run pipeline for multiple topics. Returns stats."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    stats = {
        "total": len(topics), "generated": 0, "passed": 0,
        "failed": 0, "avg_score": 0.0, "total_time": 0.0,
    }
    scores = []

    print(f"\n{'═' * 60}")
    print(f"  QwenElite PsySales Pipeline v1.0")
    print(f"  Model: {model} | GCR loops: {gcr_loops} | Min score: {min_score}")
    print(f"  Topics: {len(topics)} | Output: {output_path}")
    print(f"{'═' * 60}\n")

    t_start = time.time()

    with open(output_path, "a", encoding="utf-8") as f:
        for i, topic in enumerate(topics):
            print(f"\n{'━' * 60}")
            print(f"  [{i+1}/{len(topics)}] {topic}")
            print(f"{'━' * 60}")

            record = run_single(
                topic, model=model, gcr_loops=gcr_loops,
                min_score=min_score, verbose=True,
            )

            if record:
                stats["generated"] += 1
                score = record.get("rule_engine_score", 0)
                scores.append(score)

                if record.get("rule_engine_passed"):
                    stats["passed"] += 1
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
                    log_ok(f"Saved to {output_path}")
                else:
                    # Save anyway but mark as needs_review
                    record["needs_review"] = True
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
                    log_warn(f"Saved with needs_review flag")
            else:
                stats["failed"] += 1
                log_warn("Skipped — generation failed")

    stats["total_time"] = time.time() - t_start
    stats["avg_score"] = sum(scores) / len(scores) if scores else 0.0

    # Print summary
    print(f"\n{'═' * 60}")
    print(f"  HOÀN TẤT")
    print(f"  Tạo: {stats['generated']}/{stats['total']} | Pass: {stats['passed']} | Fail: {stats['failed']}")
    print(f"  Điểm TB: {stats['avg_score']:.1f}/10 | Thời gian: {stats['total_time']:.0f}s")
    print(f"  Output: {output_path}")
    print(f"{'═' * 60}\n")

    return stats


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="QwenElite PsySales Pipeline — Tạo tình huống CSKH/Sales chất lượng cao",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--topic", help="Một topic (VD: 'Khách hỏi giá ngay câu đầu')")
    parser.add_argument("--topics-file", help="File chứa topics (mỗi dòng 1 topic)")
    parser.add_argument("--model", default="qwen3:72b",
                        help="Ollama model (default: qwen3:72b)\n"
                             "  72b → ~42GB RAM, chậm nhưng tốt nhất\n"
                             "  32b → ~20GB RAM, cân bằng\n"
                             "  8b  → ~5GB RAM, nhanh nhưng kém hơn")
    parser.add_argument("--output", "-o", default="training_data/elite.jsonl",
                        help="Output JSONL file")
    parser.add_argument("--gcr-loops", type=int, default=2,
                        help="Số vòng GCR (default: 2, 0=bỏ critique)")
    parser.add_argument("--min-score", type=float, default=8.0,
                        help="Điểm tối thiểu để pass (default: 8.0)")
    parser.add_argument("--no-gcr", action="store_true",
                        help="Bỏ GCR loop — chỉ generate 1 pass")
    args = parser.parse_args()

    topics = []
    if args.topic:
        topics.append(args.topic)
    if args.topics_file:
        p = Path(args.topics_file)
        if p.exists():
            topics.extend([
                line.strip() for line in p.read_text(encoding="utf-8").split("\n")
                if line.strip() and not line.startswith("#")
            ])

    if not topics:
        print("Cần ít nhất 1 topic. Dùng --topic hoặc --topics-file")
        print("\nVí dụ:")
        print('  python elite_pipeline/run_pipeline.py --topic "Khách hỏi giá ngay câu đầu"')
        print('  python elite_pipeline/run_pipeline.py --topics-file elite_pipeline/topics.txt')
        sys.exit(1)

    gcr = 0 if args.no_gcr else args.gcr_loops

    if len(topics) == 1:
        record = run_single(topics[0], model=args.model, gcr_loops=gcr, min_score=args.min_score)
        if record:
            # Save single result
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            with open(args.output, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"\n  Saved → {args.output}")
            print(f"\n  JSON preview:")
            print(json.dumps(record, ensure_ascii=False, indent=2)[:1000])
    else:
        run_batch(topics, model=args.model, gcr_loops=gcr,
                  min_score=args.min_score, output_path=args.output)


if __name__ == "__main__":
    main()
