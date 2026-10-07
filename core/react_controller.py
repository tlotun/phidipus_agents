# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/react_controller.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

ReAct Controller — Verify → Think → Correct cycle.

Vấn đề: Agent chạy xong task nhưng KHÔNG kiểm tra kết quả.
  → get_page_text trả về HTML rác → vẫn báo "✅ success"
  → navigate sai URL → không ai biết
  → click nút nhưng dialog đã đóng → fail silent

Giải pháp: ReAct Controller wraps mọi task result:
  1. VERIFY  — Kiểm tra output có match goal không
  2. THINK   — Nếu fail, RAG recall cách khác đã thành công
  3. CORRECT — Thử approach mới (tối đa 2 retry)

Vị trí trong pipeline:
  SmartRouter/SmartAction execute task
    → ReAct Controller verify result
    → Nếu OK → return
    → Nếu fail → recall RAG → re-route → verify lại
    → Max 2 retries → return best result

Integration:
  core/agent_loop.py — wraps SmartRouter result
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from typing import Any, Optional

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ── v4.3 safe retry policy ──────────────────────────────────────────────
# The old controller re-ran the WHOLE goal whenever a result had no "output"
# field — which is the case for every successful taught workflow — so emails,
# posts or deletions could be executed twice.  Now:
#   * a successful result without output is trusted;
#   * goals with side effects are never re-run automatically;
#   * failed read-only goals are retried once, and only for transient errors.
_SIDE_EFFECT_WORDS = (
    "gui", "send", "post", "dang", "dang bai", "xoa", "delete", "remove", "thanh toan",
    "pay", "payment", "chuyen tien", "chuyen khoan", "transfer", "mua", "buy", "order",
    "dat hang", "dat lich", "book", "submit", "nop", "comment", "binh luan", "like",
    "follow", "share", "chia se", "reply", "tra loi", "upload", "tai len", "publish",
    "email", "mail", "nhan tin", "message", "tweet", "huy", "cancel", "ghi de", "overwrite",
)
_TRANSIENT_HINTS = (
    "timeout", "timed out", "connection", "not found", "khong tim thay", "element",
    "stale", "rate limit", "429", "503", "temporar", "tam thoi", "not loaded",
    "chua tai", "busy", "unavailable",
)


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def is_side_effectful(goal: str) -> bool:
    """True when re-running *goal* could duplicate an external action."""
    g = _fold(goal)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", g)
               for w in _SIDE_EFFECT_WORDS)


def _looks_transient(error: str) -> bool:
    e = _fold(error)
    return any(h in e for h in _TRANSIENT_HINTS)


def _result_fields(result: Any) -> tuple[bool, str, str]:
    """(success, error, output) from TaskResult-like objects or dicts."""
    if hasattr(result, "success"):
        return (bool(result.success), str(getattr(result, "error", "") or ""),
                str(getattr(result, "output", getattr(result, "result", "")) or ""))
    if isinstance(result, dict):
        return (bool(result.get("success", True)), str(result.get("error", "") or ""),
                str(result.get("output", result.get("result", "")) or ""))
    return True, "", ""


class VerifyResult:
    """Result of verification step."""
    __slots__ = ("valid", "confidence", "issue", "suggestion")

    def __init__(self, valid: bool = True, confidence: float = 1.0,
                 issue: str = "", suggestion: str = ""):
        self.valid = valid
        self.confidence = confidence
        self.issue = issue
        self.suggestion = suggestion


class ReActController:
    """
    Lightweight ReAct wrapper — verify results and self-correct.

    NOT the full VLM ReAct loop (that's planner/react_reasoner.py P5).
    This is a fast verification layer (~200ms) that catches obvious failures.
    """

    def __init__(self, llm_client=None, notify_fn=None):
        self._llm = llm_client
        self._notify = notify_fn
        self._verify_count = 0
        self._correct_count = 0
        self._catch_count = 0  # times verification caught a false-positive

    # ══════════════════════════════════════════════════════════
    # VERIFY — Check if task result actually matches goal
    # ══════════════════════════════════════════════════════════

    async def verify_result(self, goal: str, result: Any, method: str = "") -> VerifyResult:
        """
        Quick verification: did the result actually achieve the goal?

        Checks:
          1. Result not empty/null
          2. Error field not set
          3. Output makes sense for goal type
          4. LLM quick check (if available, ~200ms)
        """
        self._verify_count += 1
        t0 = time.time()

        # ── Basic checks (instant) ────────────────────────────
        if result is None:
            return VerifyResult(False, 0.0, "Result is None", "Retry with different method")

        success, error, output = _result_fields(result)

        if not success:
            return VerifyResult(False, 0.1, f"Task reported failure: {error[:100] or 'unknown'}",
                              "Try alternative approach from RAG memory")

        if not output:
            # v4.3: workflow / action results usually carry no text output —
            # a reported success is trusted instead of triggering a re-run.
            return VerifyResult(True, 0.8, "", "")

        # ── Content quality checks ────────────────────────────
        goal_lower = goal.lower()

        # Web content tasks — check for HTML garbage
        if any(w in goal_lower for w in ("nội dung", "content", "text", "đọc", "lấy")):
            if output and len(output) < 20:
                return VerifyResult(False, 0.3, "Output too short for content task",
                                  "Page may not have loaded — add Wait before get_text")
            if output and output.count("<") > output.count(" "):
                return VerifyResult(False, 0.2, "Output looks like raw HTML, not text",
                                  "Use get_page_text instead of get_page_html")

        # File tasks — check for results
        if any(w in goal_lower for w in ("file", "pdf", "tìm", "find", "list")):
            if "0 file" in output or "không tìm" in output:
                return VerifyResult(True, 0.6, "No files found — may be correct",
                                  "Check if path/pattern is correct")

        # Email tasks
        if any(w in goal_lower for w in ("email", "gmail", "gửi")):
            if "timeout" in error.lower() or "not found" in error.lower():
                return VerifyResult(False, 0.2, "Email operation timeout/not found",
                                  "Check Gmail is logged in, try navigate first")

        # ── LLM quick verify (optional, ~200ms) ──────────────
        if self._llm and output and len(output) > 50:
            try:
                llm_verify = await self._llm_verify(goal, output, method)
                if llm_verify is not None:
                    return llm_verify
            except Exception:
                pass  # LLM verify failed — trust basic checks

        ms = int((time.time() - t0) * 1000)
        _vlog("✓", f"Verify: valid=True confidence=0.85 ({ms}ms)")
        return VerifyResult(True, 0.85, "", "")

    async def _llm_verify(self, goal: str, output: str, method: str) -> VerifyResult | None:
        """Use LLM for quick output verification (~200ms with qwen3:4b)."""
        try:
            import urllib.request

            prompt = (
                f"Verify: Goal was \"{goal[:100]}\". "
                f"Method: {method}. "
                f"Output (first 200 chars): \"{output[:200]}\". "
                f"Is this output CORRECT for the goal? "
                f"Reply ONLY: YES or NO + 1 sentence reason."
            )

            from core.model_registry import get_model, ollama_url
            payload = json.dumps({
                "model": get_model("fast", "qwen3:4b-instruct"),
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.0, "num_predict": 50},
            }).encode()

            req = urllib.request.Request(
                f"{ollama_url()}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )

            def _call():
                with urllib.request.urlopen(req, timeout=3) as resp:
                    return json.loads(resp.read()).get("response", "")

            response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=3.5)
            response = re.sub(r"<think>.*?</think>", "", response, flags=re.S)

            response_lower = response.lower().strip()
            if response_lower.startswith("no"):
                reason = response[2:].strip().lstrip(":.-").strip()
                self._catch_count += 1
                _vlog("🔍", f"LLM verify caught issue: {reason[:60]}")
                return VerifyResult(False, 0.3, f"LLM: {reason[:100]}",
                                  "Re-route with different approach")
            elif response_lower.startswith("yes"):
                return VerifyResult(True, 0.9, "", "")
            return None  # Ambiguous — trust basic checks

        except Exception:
            return None

    # ══════════════════════════════════════════════════════════
    # THINK — Use RAG to find better approach
    # ══════════════════════════════════════════════════════════

    async def think_alternative(self, goal: str, failed_method: str,
                                 error: str = "") -> dict:
        """
        When verification fails, consult RAG memory for alternative approach.
        Returns: {should_retry: bool, suggestion: str, alternative_goal: str}
        """
        try:
            from memory.rag_engine import get_rag_engine_sync
            rag = get_rag_engine_sync()
            if not rag or not rag._ready:
                return {"should_retry": False, "suggestion": "RAG not available"}

            # Search for successful tasks with similar goal
            similar = await rag.recall_similar_tasks(goal, k=5, success_only=True)

            if similar:
                # Find one that used a DIFFERENT method
                for task in similar:
                    task_skill = task.get("skill", "")
                    task_lane = task.get("lane", "")
                    if task_skill != failed_method and task_lane != failed_method:
                        suggestion = (
                            f"Lần trước '{task['goal'][:50]}' thành công "
                            f"bằng {task_skill or task_lane} ({task['duration_s']}s)"
                        )
                        _vlog("💡", f"RAG alternative: {suggestion}")
                        return {
                            "should_retry": True,
                            "suggestion": suggestion,
                            "alternative_skill": task_skill,
                            "alternative_lane": task_lane,
                        }

            # No alternative found in RAG
            # Check failure patterns
            knowledge = await rag.recall_knowledge(f"failure_pattern: {goal[:50]}", k=2)
            if knowledge:
                _vlog("💡", f"RAG found failure pattern: {knowledge[0].get('text', '')[:60]}")
                return {
                    "should_retry": False,
                    "suggestion": knowledge[0].get("text", "")[:200],
                }

            return {"should_retry": True, "suggestion": "No RAG history — retry with explorer lane"}

        except Exception as exc:
            return {"should_retry": False, "suggestion": f"RAG error: {str(exc)[:50]}"}

    # ══════════════════════════════════════════════════════════
    # CORRECT — Execute correction cycle
    # ══════════════════════════════════════════════════════════

    async def maybe_correct(self, goal: str, result: Any, method: str = "",
                             router=None, task_id: str = "") -> Any:
        """
        Full verify → think → correct cycle.
        Returns original result if OK, or corrected result if retry succeeded.
        Max 1 retry to keep latency low.
        """
        # Step 1: Verify
        verify = await self.verify_result(goal, result, method)

        if verify.valid and verify.confidence >= 0.7:
            return result  # ✅ All good

        _vlog("🔄", f"Verification failed: {verify.issue[:60]} (confidence={verify.confidence})")
        success, error, _ = _result_fields(result)

        # v4.3 safety gates — never duplicate external actions
        if is_side_effectful(goal):
            _vlog("🛡️", "No auto-retry: goal has side effects (send/post/delete/pay…)")
            if success and self._notify:
                try:
                    await self._notify(f"⚠️ Nên kiểm tra lại kết quả: {verify.issue[:150]}")
                except Exception:
                    pass
            return result
        if not success and not _looks_transient(error):
            _vlog("🔄", f"No auto-retry: non-transient failure ({error[:60]})")
            return result

        # Step 2: Think — find alternative
        alt = await self.think_alternative(goal, method, verify.issue)

        if not alt.get("should_retry"):
            _vlog("🔄", f"No retry: {alt.get('suggestion', '')[:60]}")
            return result  # Can't fix — return original

        # Step 3: Correct — retry with alternative
        _vlog("🔄", f"Retrying with alternative: {alt.get('suggestion', '')[:60]}")
        self._correct_count += 1

        if router:
            try:
                # Re-route with hint to use different lane
                retry_result = await asyncio.wait_for(
                    router.route(goal, task_id=task_id),
                    timeout=30,
                )
                if retry_result.handled and retry_result.task_result:
                    retry_tr = retry_result.task_result
                    if getattr(retry_tr, "success", False):
                        _vlog("✅", f"Self-correction succeeded! (method: {retry_result.lane_used})")
                        # Notify about self-correction
                        if self._notify:
                            try:
                                await self._notify(
                                    f"🔄 *Self-correction:* Agent tự sửa lỗi\n"
                                    f"❌ Lần 1: {method} → {verify.issue[:50]}\n"
                                    f"✅ Lần 2: {retry_result.lane_used} → thành công"
                                )
                            except Exception:
                                pass
                        return retry_tr
            except Exception as exc:
                _vlog("⚠️", f"Retry failed: {str(exc)[:50]}")

        return result  # Retry failed — return original

    # ══════════════════════════════════════════════════════════
    # Stats
    # ══════════════════════════════════════════════════════════

    def stats(self) -> dict:
        return {
            "verify_count": self._verify_count,
            "correct_count": self._correct_count,
            "catch_count": self._catch_count,
            "catch_rate": round(self._catch_count / max(self._verify_count, 1), 3),
        }


# Singleton
_controller: Optional[ReActController] = None

def get_react_controller(llm_client=None, notify_fn=None) -> ReActController:
    global _controller
    if _controller is None:
        _controller = ReActController(llm_client=llm_client, notify_fn=notify_fn)
    return _controller
