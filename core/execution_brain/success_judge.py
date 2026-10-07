# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/execution_brain/success_judge.py — Phidipus v1.37
═══════════════════════════════════════════════════════════════════════

P0.3 — SuccessJudge: End-to-End Task Verification

Câu hỏi quan trọng nhất: "User muốn X. X có thực sự được thực hiện chưa?"

Khác với StepValidator (kiểm tra từng bước):
  SuccessJudge kiểm tra KẾT QUẢ CUỐI CÙNG của toàn bộ task.

Ví dụ:
  - StepValidator: "Nút Post đã được click thành công" (micro)
  - SuccessJudge: "Bài đăng có thật sự xuất hiện trên tường Facebook không?" (macro)

3 layers kiểm tra:
  Layer 1 — Rule check (0ms): kiểm tra files, URLs, values có sẵn
  Layer 2 — VLM check (~3-15s): screenshot + câu hỏi cụ thể về goal
  Layer 3 — Hybrid: kết hợp Layer 1 + 2, weighted verdict

JudgeVerdict:
  achieved:    bool   — goal có đạt không
  partial:     bool   — đạt một phần (vd upload ảnh ok nhưng text fail)
  confidence:  float  — độ tin cậy của verdict
  evidence:    str    — VLM quan sát gì để đưa ra verdict
  details:     dict   — chi tiết từng layer
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# JudgeVerdict
# ══════════════════════════════════════════════════════════════════

@dataclass
class JudgeVerdict:
    """Kết quả end-to-end verification."""
    achieved:    bool
    partial:     bool  = False
    confidence:  float = 0.0
    evidence:    str   = ""
    details:     dict  = field(default_factory=dict)
    judge_ms:    int   = 0

    @property
    def summary(self) -> str:
        status = "✅ ACHIEVED" if self.achieved else ("⚠️ PARTIAL" if self.partial else "❌ NOT ACHIEVED")
        return f"{status} (conf={self.confidence:.0%}) — {self.evidence[:100]}"


# ══════════════════════════════════════════════════════════════════
# Pre-built judge prompts cho từng workflow type
# ══════════════════════════════════════════════════════════════════

# Prompt template — thay {goal} và {context}
_JUDGE_PROMPT = """\
You are verifying if a task was successfully completed.

Original goal: {goal}

Context about what was done: {context}

Look at this final screenshot carefully.
Answer these questions:

1. Was the main goal ACTUALLY achieved? (yes/no)
2. Is there evidence of partial completion? (yes/no)
3. What specific evidence do you see that proves or disproves success?
4. How confident are you? (0.0-1.0)

Respond ONLY with valid JSON (no markdown):
{{
  "achieved": <true|false>,
  "partial": <true|false>,
  "evidence": "<what you see that proves/disproves success — be specific>",
  "confidence": <0.0-1.0>
}}"""

# Goal-specific success criteria cho rule layer
_RULE_CRITERIA = {
    "social_post": [
        ("image_exists", lambda r: bool(r.get("image_path")) and len(r.get("image_path","")) > 5),
        ("content_exists", lambda r: bool(r.get("content_text")) and len(r.get("content_text","")) > 100),
        ("steps_completed", lambda r: r.get("steps_done", 0) >= 7),
    ],
    "file_op": [
        ("no_error", lambda r: not r.get("error", "")),
        ("steps_completed", lambda r: r.get("steps_done", 0) > 0),
    ],
    "default": [
        ("no_error", lambda r: not r.get("error", "")),
        ("steps_completed", lambda r: r.get("steps_done", 0) > 0),
    ],
}


# ══════════════════════════════════════════════════════════════════
# SuccessJudge
# ══════════════════════════════════════════════════════════════════

class SuccessJudge:
    """
    End-to-end task verifier.

    Gọi judge() sau khi workflow kết thúc để xác nhận
    goal thực sự được thực hiện hay chỉ claimed success.

    Kết quả được log vào TrajectoryDB và dùng cho data curation (P5).
    """

    def __init__(self, vlm: Any = None, ipc_client: Any = None) -> None:
        self._vlm = vlm
        self._ipc = ipc_client

    async def judge(
        self,
        goal: str,
        task_result: Any,                    # WorkflowResult hoặc TaskResult
        intent_type: str = "default",
        screenshot: Optional[bytes] = None,  # None → tự chụp
    ) -> JudgeVerdict:
        """
        Verify end-to-end: goal có thực sự đạt không?

        Args:
            goal:        User goal string
            task_result: WorkflowResult / TaskResult object
            intent_type: 'social_post' | 'file_op' | 'default'
            screenshot:  Final screenshot (None → tự chụp)

        Returns:
            JudgeVerdict với đầy đủ evidence
        """
        t0 = time.time()

        # ── Layer 1: Rule check ────────────────────────────────
        result_dict = self._to_dict(task_result)
        rule_results = self._check_rules(intent_type, result_dict)

        # Nếu rule layer nói fail rõ ràng → không cần VLM
        rule_achieved = all(v for _, v in rule_results)
        rule_partial  = any(v for _, v in rule_results) and not rule_achieved

        _log.debug("SuccessJudge rule check: %s",
                   {name: v for name, v in rule_results})

        # ── Layer 2: VLM check ─────────────────────────────────
        vlm_verdict: Optional[dict] = None
        if self._vlm:
            try:
                # Tự chụp screenshot nếu không có
                if not screenshot:
                    screenshot = await self._capture_screenshot()

                if screenshot:
                    context = self._build_context(task_result, rule_results)
                    prompt  = _JUDGE_PROMPT.format(
                        goal=goal[:300],
                        context=context,
                    )
                    raw, tier = await asyncio.wait_for(
                        self._vlm.call(screenshot, prompt),
                        timeout=20.0,
                    )
                    vlm_verdict = self._parse_vlm(raw)
                    _vlog("🔍", f"SuccessJudge VLM [{tier}]: "
                          f"achieved={vlm_verdict.get('achieved')} "
                          f"conf={vlm_verdict.get('confidence', 0):.0%}")
            except asyncio.TimeoutError:
                _vlog("⏱️", "SuccessJudge: VLM timeout → dùng rule result")
            except Exception as exc:
                _log.debug("SuccessJudge VLM error: %s", exc)

        # ── Layer 3: Hybrid verdict ────────────────────────────
        verdict = self._combine(rule_achieved, rule_partial,
                                rule_results, vlm_verdict)
        verdict.judge_ms = int((time.time() - t0) * 1000)

        # Log
        icon = "✅" if verdict.achieved else ("⚠️" if verdict.partial else "❌")
        _vlog(icon, f"SuccessJudge: {verdict.summary} ({verdict.judge_ms}ms)")

        return verdict

    # ── Internal ──────────────────────────────────────────────────

    def _to_dict(self, task_result: Any) -> dict:
        """Convert task_result sang dict."""
        if isinstance(task_result, dict):
            return task_result
        if hasattr(task_result, "__dict__"):
            return task_result.__dict__
        return {}

    def _check_rules(self, intent_type: str, result: dict) -> list[tuple[str, bool]]:
        """Layer 1: check các rule-based criteria."""
        criteria = _RULE_CRITERIA.get(intent_type, _RULE_CRITERIA["default"])
        results = []
        for name, fn in criteria:
            try:
                results.append((name, bool(fn(result))))
            except Exception:
                results.append((name, False))
        return results

    def _build_context(self, task_result: Any, rule_results: list) -> str:
        """Build context string để inject vào VLM prompt."""
        d = self._to_dict(task_result)
        lines = []
        if d.get("steps_done"):
            lines.append(f"Steps completed: {d['steps_done']}")
        if d.get("image_path"):
            lines.append(f"Image saved: {d['image_path']}")
        if d.get("content_text"):
            lines.append(f"Content length: {len(d['content_text'])} chars")
        if d.get("error"):
            lines.append(f"Error recorded: {d['error'][:100]}")
        # Rule results
        passed = [n for n, v in rule_results if v]
        failed = [n for n, v in rule_results if not v]
        if passed:
            lines.append(f"Rule checks passed: {', '.join(passed)}")
        if failed:
            lines.append(f"Rule checks failed: {', '.join(failed)}")
        return "\n".join(lines) or "No additional context available."

    def _parse_vlm(self, raw: str) -> dict:
        """Parse VLM JSON response, robust với noise."""
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            m = re.search(r"\{[^{}]+\}", cleaned, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        # Fallback heuristic
        low = raw.lower()
        achieved = ("yes" in low[:50] or
                    ("achieved" in low and "not achieved" not in low))
        return {
            "achieved": achieved,
            "partial": False,
            "evidence": raw[:200],
            "confidence": 0.5,
        }

    def _combine(self, rule_achieved: bool, rule_partial: bool,
                 rule_results: list, vlm: Optional[dict]) -> JudgeVerdict:
        """Kết hợp rule và VLM verdict với weighted logic."""
        if vlm is None:
            # Chỉ có rule
            return JudgeVerdict(
                achieved=rule_achieved,
                partial=rule_partial and not rule_achieved,
                confidence=0.70 if rule_achieved else 0.60,
                evidence=("All rule checks passed" if rule_achieved
                          else f"Rule checks failed: {[n for n,v in rule_results if not v]}"),
                details={"rule": {n: v for n, v in rule_results}},
            )

        vlm_achieved = bool(vlm.get("achieved", False))
        vlm_conf     = float(vlm.get("confidence", 0.5))
        vlm_evidence = str(vlm.get("evidence", ""))
        vlm_partial  = bool(vlm.get("partial", False))

        # Weighted: rule 30%, VLM 70%
        # VLM is primary authority (can see screen), rule is sanity check
        if vlm_conf >= 0.80:
            # VLM confident → follow VLM
            achieved = vlm_achieved
            partial  = vlm_partial and not vlm_achieved
            conf     = vlm_conf
        elif vlm_conf >= 0.50:
            # VLM medium confidence → agree if rule agrees
            achieved = vlm_achieved and rule_achieved
            partial  = (vlm_partial or rule_partial) and not achieved
            conf     = (vlm_conf + (0.70 if rule_achieved else 0.40)) / 2
        else:
            # VLM không chắc → theo rule
            achieved = rule_achieved
            partial  = rule_partial and not rule_achieved
            conf     = 0.55 if rule_achieved else 0.45

        return JudgeVerdict(
            achieved=achieved,
            partial=partial,
            confidence=round(conf, 2),
            evidence=vlm_evidence or ("Rules passed" if rule_achieved else "Rules failed"),
            details={
                "rule": {n: v for n, v in rule_results},
                "vlm":  {"achieved": vlm_achieved, "confidence": vlm_conf},
            },
        )

    async def _capture_screenshot(self) -> Optional[bytes]:
        """Tự chụp screenshot nếu không được truyền vào."""
        try:
            from social.vision_actor import capture_screen_region
            return await capture_screen_region()
        except Exception:
            return None
