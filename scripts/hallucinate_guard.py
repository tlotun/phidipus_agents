#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
scripts/hallucinate_guard.py — Phidipus AI Forge E5
═══════════════════════════════════════════════════════════════

Hallucinate Guard 2 tầng — Bảo vệ production chatbot.

Tầng 1: Pattern matching nhanh (0ms)
  - Regex check các forbidden patterns từ domain config
  - Block ngay nếu match

Tầng 2: LLM judge (chỉ khi Tier 1 pass, ~500ms)
  - Claude Haiku judge response có hallucinate không
  - Chỉ gọi khi Tier 1 pass (tiết kiệm chi phí)

Usage (as module):
  from scripts.hallucinate_guard import HallucinateGuard

  guard = HallucinateGuard(domain_config)
  result = await guard.check(response)
  if result.blocked:
      return FALLBACK_RESPONSE
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GuardResult:
    blocked:    bool
    tier:       int         # 0=clean, 1=blocked_tier1, 2=blocked_tier2
    reason:     str = ""
    latency_ms: int = 0
    pattern:    str = ""


# ══════════════════════════════════════════════════════════════════
# Base forbidden patterns (domain-agnostic)
# ══════════════════════════════════════════════════════════════════

BASE_FORBIDDEN_PATTERNS = [
    # Báo giá cụ thể — nhiều format khác nhau
    (r"giá\s+(?:chính\s+xác\s+)?(?:là\s+|:\s*)?\d[\d\.,]+\s*(?:đ|đồng|k|triệu|nghìn|ngàn|vnđ|vnd)?",
     "báo giá cụ thể"),
    (r"(?:giá|chi phí|phí)\s*(?:chỉ|chỉ còn|là|=)\s*\d[\d\.,]+",
     "báo giá cụ thể"),
    # Cam kết lợi nhuận/ROI
    (r"cam kết\s+\d+\s*%",                        "cam kết % lợi nhuận"),
    (r"đảm bảo.*\d+\s*%.*lợi nhuận",              "đảm bảo lợi nhuận"),
    # Cam kết delivery ngày cụ thể
    (r"(?:cam kết|chắc chắn|đảm bảo)\s+giao",     "cam kết delivery"),
    (r"giao\s+hàng.*ngày\s*\d+",                   "cam kết delivery"),
    (r"giao\s+(?:trong|sau)\s+(?:đúng\s+)?\d+\s*ngày",  "cam kết delivery"),
    # Bịa tồn kho
    (r"còn\s+(?:lại\s+)?\d+\s*(?:cái|chiếc|sp|sản phẩm)",
     "bịa tồn kho"),
    # Y tế - chẩn đoán
    (r"bạn(?:\s+bị|\s+mắc|\s+có bệnh)\s+\w+",     "chẩn đoán bệnh"),
    (r"chẩn đoán.*là.*bệnh",                        "chẩn đoán bệnh"),
    # Y tế - kê đơn
    (r"uống\s+thuốc\s+\w+\s+\d+",                  "kê đơn thuốc"),
    (r"kê đơn.*thuốc",                              "kê đơn thuốc"),
    # Pháp lý - phán quyết
    (r"bạn\s+(?:chắc chắn\s+)?(?:thắng|thua)\s+kiện", "phán quyết pháp lý"),
    (r"sẽ được bồi thường\s+\d+",                  "cam kết bồi thường"),
    # Tài chính
    (r"đầu tư.*(?:lời|lãi)\s+\d+\s*%",            "cam kết đầu tư"),
    (r"chắc chắn.*(?:sinh lời|có lợi nhuận)",      "cam kết đầu tư"),
]


def build_domain_patterns(refusal_rules: list[str]) -> list[tuple[str, str]]:
    """
    Convert domain refusal_rules → regex patterns.
    "Không bao giờ X" → pattern for X
    """
    patterns = []
    for rule in refusal_rules:
        # Extract key action being forbidden
        cleaned = rule.lower()
        cleaned = re.sub(r"^(không bao giờ|không|luôn|không được|tuyệt đối không)\s+", "", cleaned)
        cleaned = cleaned.strip().rstrip(".")

        if len(cleaned) > 10:
            # Build simple keyword pattern
            words = [w for w in cleaned.split() if len(w) >= 4][:4]
            if words:
                pattern = r"\b" + r"\b.{0,30}\b".join(re.escape(w) for w in words[:2]) + r"\b"
                patterns.append((pattern, rule[:50]))

    return patterns


class HallucinateGuard:
    """
    2-tier hallucination guard cho production chatbot.

    Tầng 1: Regex patterns (nhanh, miễn phí)
    Tầng 2: Claude Haiku judge (chậm hơn, chính xác hơn)
    """

    def __init__(
        self,
        domain_config: dict,
        use_tier2: bool = True,
        tier2_model: str = "claude-haiku-4-5",
    ):
        self.domain_config = domain_config
        self.use_tier2 = use_tier2
        self.tier2_model = tier2_model
        self.domain_name = domain_config.get("domain", {}).get("name", "general")
        self.refusal_rules = domain_config.get("dataset", {}).get("refusal_rules", [])

        # Compile patterns
        self._tier1_patterns = BASE_FORBIDDEN_PATTERNS.copy()
        self._tier1_patterns.extend(build_domain_patterns(self.refusal_rules))
        self._compiled = [(re.compile(pat, re.IGNORECASE), reason)
                          for pat, reason in self._tier1_patterns]

        # Claude client (lazy)
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except Exception:
                self._client = False  # Mark as unavailable
        return self._client if self._client is not False else None

    def check_tier1(self, response: str) -> GuardResult:
        """Tầng 1: Regex check (0ms)."""
        t0 = time.time()
        for compiled_re, reason in self._compiled:
            m = compiled_re.search(response)
            if m:
                return GuardResult(
                    blocked=True, tier=1, reason=reason,
                    pattern=m.group(0)[:50],
                    latency_ms=int((time.time()-t0)*1000),
                )
        return GuardResult(blocked=False, tier=0,
                           latency_ms=int((time.time()-t0)*1000))

    def check_tier2(self, response: str) -> GuardResult:
        """Tầng 2: LLM judge check (~500ms)."""
        client = self._get_client()
        if not client:
            return GuardResult(blocked=False, tier=0, reason="tier2_unavailable")

        t0 = time.time()
        rules_str = "\n".join(f"- {r}" for r in self.refusal_rules)

        prompt = f"""Domain: {self.domain_name}

Refusal rules (những điều KHÔNG được làm):
{rules_str}

Response cần kiểm tra:
\"\"\"{response[:500]}\"\"\"

Câu trả lời trên có vi phạm bất kỳ refusal rule nào không? 
Có thông tin bịa đặt, cam kết sai, hay không an toàn không?

Trả lời DUY NHẤT: YES hoặc NO"""

        try:
            resp = client.messages.create(
                model=self.tier2_model,
                max_tokens=5,
                messages=[{"role": "user", "content": prompt}]
            )
            answer = resp.content[0].text.strip().upper()
            is_hallucinate = answer.startswith("YES")
            return GuardResult(
                blocked=is_hallucinate,
                tier=2 if is_hallucinate else 0,
                reason="llm_judge_flagged" if is_hallucinate else "",
                latency_ms=int((time.time()-t0)*1000),
            )
        except Exception as e:
            return GuardResult(blocked=False, tier=0, reason=f"tier2_error:{e}")

    def check(self, response: str) -> GuardResult:
        """
        Full 2-tier check.
        Returns GuardResult — caller quyết định xử lý.
        """
        if not response or not response.strip():
            return GuardResult(blocked=False, tier=0)

        # Tier 1
        r1 = self.check_tier1(response)
        if r1.blocked:
            return r1

        # Tier 2 (chỉ khi cần và có sẵn)
        if self.use_tier2:
            return self.check_tier2(response)

        return r1

    def get_fallback_response(self, trigger: str = "") -> str:
        """Response mặc định khi hallucinate được detect."""
        domain_name = self.domain_name
        fallbacks = [
            f"Xin lỗi, tôi cần xác nhận thêm thông tin trước khi trả lời câu hỏi này. "
            f"Để đảm bảo độ chính xác, bạn có thể liên hệ trực tiếp để được hỗ trợ tốt nhất không?",

            f"Câu hỏi này cần thông tin chính xác từ chuyên gia trong lĩnh vực {domain_name}. "
            f"Tôi không muốn cung cấp thông tin không chính xác — hãy để tôi kết nối bạn với "
            f"người có thể giải đáp cụ thể hơn.",

            f"Để đảm bảo bạn nhận được thông tin đúng và đủ về {domain_name}, "
            f"tốt nhất nên xác nhận trực tiếp từ nguồn chính thức. "
            f"Tôi có thể giúp bạn chuẩn bị câu hỏi phù hợp không?",
        ]
        import random
        return random.choice(fallbacks)


# ══════════════════════════════════════════════════════════════════
# Standalone test CLI
# ══════════════════════════════════════════════════════════════════

def main():
    import argparse
    import yaml

    parser = argparse.ArgumentParser(
        description="Hallucinate Guard — Phidipus AI Forge E5"
    )
    parser.add_argument("--domain",   required=True, help="Domain YAML config")
    parser.add_argument("--response", required=True, help="Response text để test")
    parser.add_argument("--no-tier2", action="store_true")
    args = parser.parse_args()

    with open(args.domain, encoding="utf-8") as f:
        domain_cfg = yaml.safe_load(f)

    guard = HallucinateGuard(domain_cfg, use_tier2=not args.no_tier2)

    print(f"\nChecking: \"{args.response[:100]}...\"")
    result = guard.check(args.response)

    print(f"\nResult:")
    print(f"  blocked:  {result.blocked}")
    print(f"  tier:     {result.tier}")
    print(f"  reason:   {result.reason}")
    print(f"  latency:  {result.latency_ms}ms")
    if result.pattern:
        print(f"  pattern:  '{result.pattern}'")

    if result.blocked:
        print(f"\nFallback: {guard.get_fallback_response()}")


if __name__ == "__main__":
    main()
