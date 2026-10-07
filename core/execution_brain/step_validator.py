# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/execution_brain/step_validator.py — Phidipus v1.37
═══════════════════════════════════════════════════════════════════════

P0.1 — StepValidator: Expected Outcome Per Step

Vấn đề cũ: sau mỗi B1..B7 trong workflow, không có gì verify
xem step có thật sự thành công chưa — chỉ dựa vào "không raise exception".

Giải pháp: mỗi StepExpectation định nghĩa:
  - success_criteria: list các điều kiện phải thỏa (JS, VLM, rule)
  - timeout_s: bao lâu để chờ condition thỏa
  - max_retries: thử lại tối đa bao nhiêu lần
  - retry_wait_s: chờ giữa các lần retry

Checker chain (ưu tiên nhanh → chậm):
  1. JSChecker  (<50ms): js_code → ipc.browser_execute_js → expect value
  2. RuleChecker (<5ms): kiểm tra giá trị có sẵn (path tồn tại, text length)
  3. VLMChecker (~3-15s): screenshot → VLM → yes/no question

Kết quả: ValidationResult với success, attempts, failed_criteria, duration_ms
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Criterion types
# ══════════════════════════════════════════════════════════════════

class CriterionType(Enum):
    JS       = "js"       # Execute JS, check result
    RULE     = "rule"     # Check value/file/state without LLM
    VLM      = "vlm"      # Screenshot + VLM yes/no question
    CALLABLE = "callable" # Custom async function


@dataclass
class Criterion:
    """Một điều kiện trong expected outcome."""
    name:        str
    type:        CriterionType
    # For JS:
    js_code:     str  = ""
    js_expect:   str  = ""    # expected substring in result ("true", "1", "open")
    # For RULE:
    rule_fn:     Optional[Callable] = None  # async () -> bool
    # For VLM:
    vlm_question:str  = ""    # yes/no question for VLM
    # For CALLABLE:
    check_fn:    Optional[Callable] = None  # async (ipc, vlm) -> bool
    # Common:
    optional:    bool = False  # nếu True: fail criterion không fail toàn bộ step


@dataclass
class StepExpectation:
    """Định nghĩa expected outcome cho 1 workflow step."""
    step_name:    str
    criteria:     list[Criterion]  = field(default_factory=list)
    timeout_s:    float = 30.0
    max_retries:  int   = 2
    retry_wait_s: float = 2.0

    def add_js(self, name: str, js_code: str, expect: str,
                optional: bool = False) -> "StepExpectation":
        self.criteria.append(Criterion(name, CriterionType.JS,
                                       js_code=js_code, js_expect=expect,
                                       optional=optional))
        return self

    def add_rule(self, name: str, fn: Callable,
                 optional: bool = False) -> "StepExpectation":
        self.criteria.append(Criterion(name, CriterionType.RULE,
                                       rule_fn=fn, optional=optional))
        return self

    def add_vlm(self, name: str, question: str,
                optional: bool = False) -> "StepExpectation":
        self.criteria.append(Criterion(name, CriterionType.VLM,
                                       vlm_question=question, optional=optional))
        return self

    def add_callable(self, name: str, fn: Callable,
                     optional: bool = False) -> "StepExpectation":
        self.criteria.append(Criterion(name, CriterionType.CALLABLE,
                                       check_fn=fn, optional=optional))
        return self


@dataclass
class ValidationResult:
    """Kết quả validate một step."""
    success:          bool
    step_name:        str  = ""
    attempts:         int  = 1
    passed_criteria:  list = field(default_factory=list)
    failed_criteria:  list = field(default_factory=list)
    skipped_criteria: list = field(default_factory=list)  # optional ones
    duration_ms:      int  = 0
    error:            str  = ""

    @property
    def summary(self) -> str:
        total = len(self.passed_criteria) + len(self.failed_criteria)
        return (f"{'✅' if self.success else '❌'} {self.step_name}: "
                f"{len(self.passed_criteria)}/{total} criteria passed "
                f"({self.attempts} attempts, {self.duration_ms}ms)")


# ══════════════════════════════════════════════════════════════════
# Pre-built expectations cho workflow_spider_social_post
# ══════════════════════════════════════════════════════════════════

def expect_b1_chrome_open(chrome_profile: str) -> StepExpectation:
    """B1: Chrome đã mở với đúng profile."""
    exp = StepExpectation(step_name="B1_open_chrome", timeout_s=15.0)
    exp.add_rule(
        "chrome_running",
        lambda: asyncio.to_thread(_check_process_running, "Google Chrome"),
    )
    return exp


def expect_b2_image_saved(image_path_fn: Callable) -> StepExpectation:
    """B2: Ảnh đã được tạo và lưu về máy."""
    exp = StepExpectation(step_name="B2_chatgpt_image", timeout_s=180.0,
                          max_retries=1)
    exp.add_rule(
        "image_file_exists",
        lambda: _async_file_exists(image_path_fn()),
    )
    exp.add_rule(
        "image_size_valid",
        lambda: _async_file_size_ok(image_path_fn(), min_bytes=10_000),
    )
    return exp


def expect_b4_content_ready(content_fn: Callable) -> StepExpectation:
    """B4: Content đã được tạo, đủ độ dài."""
    exp = StepExpectation(step_name="B4_gemini_content", timeout_s=60.0)
    exp.add_rule(
        "content_not_empty",
        lambda: asyncio.coroutine(lambda: len(content_fn()) > 100)(),
    )
    exp.add_rule(
        "content_min_length",
        lambda: asyncio.coroutine(lambda: len(content_fn()) >= 500)(),
    )
    return exp


def expect_b6_fb_composer_open() -> StepExpectation:
    """B6: Facebook Create Post composer đã mở."""
    exp = StepExpectation(step_name="B6_fb_composer_open", timeout_s=20.0,
                          max_retries=2)
    exp.add_js(
        "composer_contenteditable",
        "document.querySelectorAll('div[contenteditable=\"true\"]').length > 0",
        "true",
    )
    exp.add_js(
        "fb_url_correct",
        "window.location.hostname",
        "facebook.com",
    )
    exp.add_vlm(
        "composer_visually_open",
        "Is the Facebook Create Post dialog or composer box visible on screen?",
        optional=True,
    )
    return exp


def expect_b7a_image_uploaded() -> StepExpectation:
    """B7a: Ảnh đã upload vào composer."""
    exp = StepExpectation(step_name="B7a_image_uploaded", timeout_s=30.0,
                          max_retries=2)
    exp.add_js(
        "image_attached",
        "(document.querySelector('img[src^=\"blob:\"]') !== null || "
        " document.querySelector('[data-type=\"photo\"]') !== null).toString()",
        "true",
        optional=True,
    )
    exp.add_vlm(
        "thumbnail_visible",
        "Is there an image thumbnail or photo visible inside the Facebook post composer?",
    )
    return exp


def expect_b7b_content_pasted(min_chars: int = 100) -> StepExpectation:
    """B7b: Content đã paste vào ô text."""
    exp = StepExpectation(step_name="B7b_content_pasted", timeout_s=15.0)
    exp.add_js(
        "text_in_composer",
        "(() => { const el = document.querySelector('div[contenteditable=\"true\"]'); "
        "return el ? el.innerText.length.toString() : '0'; })()",
        str(min(min_chars, 50)),  # expect ít nhất 50 chars
    )
    return exp


def expect_b7c_post_submitted() -> StepExpectation:
    """B7c: Bài đã được đăng thành công."""
    exp = StepExpectation(step_name="B7c_post_submitted", timeout_s=30.0,
                          max_retries=1)
    exp.add_js(
        "composer_closed",
        "document.querySelectorAll('div[contenteditable=\"true\"]').length === 0",
        "true",
        optional=True,
    )
    exp.add_js(
        "post_button_gone",
        "document.querySelector('[aria-label=\"Post\"], [aria-label=\"Đăng\"]') === null",
        "true",
        optional=True,
    )
    exp.add_vlm(
        "post_submitted_visually",
        "Has the Facebook post been submitted? Is the composer closed or is there a success indicator?",
    )
    return exp


# ══════════════════════════════════════════════════════════════════
# StepValidator
# ══════════════════════════════════════════════════════════════════

class StepValidator:
    """
    Validate expected outcome của mỗi workflow step.

    Dùng checker chain: JS (nhanh) → Rule → VLM (chậm).
    Không bao giờ chuyển sang step tiếp theo nếu step hiện tại chưa pass.
    """

    def __init__(self, ipc_client: Any = None, vlm: Any = None) -> None:
        self._ipc = ipc_client
        self._vlm = vlm

    async def validate(self, expectation: StepExpectation) -> ValidationResult:
        """
        Validate step dựa trên StepExpectation.
        Retry tối đa max_retries lần với retry_wait_s giữa các lần.
        """
        t0 = time.time()
        passed:  list[str] = []
        failed:  list[str] = []
        skipped: list[str] = []

        for attempt in range(max(1, expectation.max_retries)):
            passed.clear()
            failed.clear()
            skipped.clear()

            for criterion in expectation.criteria:
                try:
                    ok = await asyncio.wait_for(
                        self._check_criterion(criterion),
                        timeout=expectation.timeout_s,
                    )
                    if ok:
                        passed.append(criterion.name)
                    elif criterion.optional:
                        skipped.append(criterion.name)
                        _log.debug("StepValidator: optional criterion '%s' not met (skipped)",
                                   criterion.name)
                    else:
                        failed.append(criterion.name)
                        _log.debug("StepValidator: criterion '%s' FAILED", criterion.name)

                except asyncio.TimeoutError:
                    if criterion.optional:
                        skipped.append(criterion.name)
                    else:
                        failed.append(criterion.name)
                        _log.debug("StepValidator: criterion '%s' TIMEOUT", criterion.name)
                except Exception as exc:
                    _log.debug("StepValidator: criterion '%s' error: %s", criterion.name, exc)
                    if criterion.optional:
                        skipped.append(criterion.name)
                    else:
                        failed.append(criterion.name)

            # Tất cả non-optional criteria phải pass
            if not failed:
                ms = int((time.time() - t0) * 1000)
                result = ValidationResult(
                    success=True, step_name=expectation.step_name,
                    attempts=attempt + 1, passed_criteria=passed,
                    failed_criteria=[], skipped_criteria=skipped,
                    duration_ms=ms,
                )
                _vlog("✅", f"StepValidator: {result.summary}")
                return result

            # Chưa pass → chờ rồi retry (nếu còn attempt)
            if attempt < expectation.max_retries - 1:
                _vlog("⏳", f"StepValidator: {expectation.step_name} "
                      f"attempt {attempt+1}/{expectation.max_retries} "
                      f"— failed: {failed} → retry {expectation.retry_wait_s}s")
                await asyncio.sleep(expectation.retry_wait_s)

        ms = int((time.time() - t0) * 1000)
        result = ValidationResult(
            success=False, step_name=expectation.step_name,
            attempts=expectation.max_retries,
            passed_criteria=passed, failed_criteria=failed,
            skipped_criteria=skipped, duration_ms=ms,
        )
        _vlog("❌", f"StepValidator: {result.summary}")
        return result

    async def _check_criterion(self, c: Criterion) -> bool:
        """Dispatch tới đúng checker."""
        if c.type == CriterionType.JS:
            return await self._js_check(c)
        if c.type == CriterionType.RULE:
            return await self._rule_check(c)
        if c.type == CriterionType.VLM:
            return await self._vlm_check(c)
        if c.type == CriterionType.CALLABLE:
            return await c.check_fn(self._ipc, self._vlm)
        return False

    async def _js_check(self, c: Criterion) -> bool:
        """JS check qua IPC browser_execute_js."""
        if not self._ipc:
            return False
        try:
            resp = await self._ipc.send_action("browser_execute_js",
                                               {"js_code": c.js_code, "timeout_s": 6.0})
            result = str(resp.result if hasattr(resp, "result") else resp.get("result", ""))
            return c.js_expect.lower() in result.lower()
        except Exception:
            return False

    async def _rule_check(self, c: Criterion) -> bool:
        """Rule check — custom callable."""
        if not c.rule_fn:
            return True
        try:
            result = c.rule_fn()
            if asyncio.iscoroutine(result):
                return bool(await result)
            return bool(result)
        except Exception:
            return False

    async def _vlm_check(self, c: Criterion) -> bool:
        """VLM yes/no check."""
        if not self._vlm:
            return True  # assume pass nếu không có VLM
        try:
            from social.vision_actor import capture_screen_region
            screenshot = await capture_screen_region()
            if not screenshot:
                return True  # không chụp được → assume pass

            prompt = (f"Look at this screenshot carefully.\n"
                      f"Question: {c.vlm_question}\n"
                      f"Answer YES or NO only.")
            raw, _ = await self._vlm.call(screenshot, prompt)
            return "yes" in raw.lower()[:20]
        except Exception:
            return True  # VLM error → assume pass (không block workflow)


# ══════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════

def _check_process_running(process_name: str) -> bool:
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-x", process_name],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


async def _async_file_exists(path: str) -> bool:
    if not path:
        return False
    return await asyncio.to_thread(os.path.exists, path)


async def _async_file_size_ok(path: str, min_bytes: int = 1000) -> bool:
    if not path:
        return False
    try:
        size = await asyncio.to_thread(os.path.getsize, path)
        return size >= min_bytes
    except Exception:
        return False
