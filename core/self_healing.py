# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/self_healing.py — Phidipus Self-Healing Engine v2 (v9.20)
═══════════════════════════════════════════════════════════════

Detect → Diagnose → Heal → Retry với adaptive pattern learning.

v2 so với v1:
  ❌ v1: hardcoded strategies, lookup đơn giản, không học từ outcome
  ✅ v2: pattern scoring từ Failure Memory, strategy ranking theo
          success_rate × recency, multi-pass healing, provider-aware retry

Luồng v2:
  1. Classify error → error_type
  2. Query Failure Memory → lấy TẤT CẢ known fixes, sắp xếp theo score
  3. Score formula: score = 0.6×success_rate + 0.3×recency + 0.1×context_match
  4. Nếu top fix score ≥ threshold (0.4) → apply ngay
  5. Nếu không có fix tốt → built-in strategies (v1 fallback)
  6. Sau retry → record_outcome → Failure Memory cập nhật score
  7. Provider-aware: nếu error liên quan LLM → đề xuất switch provider

Adaptive learning:
  - Mỗi fix thành công → success_rate tăng → được ưu tiên cao hơn lần sau
  - Mỗi fix thất bại → score giảm → tụt xuống trong ranking
  - Fix thất bại liên tục (< 0.2 rate) → bị loại khỏi candidates
  - Mỗi lỗi lưu context_fingerprint → match context tương tự nhanh hơn

Context fingerprinting:
  Trích xuất keywords từ context (file extension, app name, goal keywords)
  → fingerprint string → so sánh với recorded context_patterns
  → context_match score boost cho fix đã thành công trong context tương tự
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from memory.failure_memory import FailureMemory


# ── [M-10 FIX] Strategy hint sanitizer ───────────────────────────────────────
def _sanitize_strategy_hint(hint: str) -> str:
    """
    Sanitize strategy_hint loaded from failure_memory.json before
    injecting into LLM prompt. Prevents prompt injection via poisoned JSON.
    """
    import re
    if not hint or not isinstance(hint, str):
        return ""
    # Truncate to 200 chars
    hint = hint[:200]
    # Remove common prompt injection patterns
    _injection_patterns = [
        r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|rules?|context)",
        r"forget\s+(everything|all|prior)",
        r"you\s+are\s+now\s+",
        r"new\s+(system\s+)?prompt",
        r"\\x[0-9a-fA-F]{2}",   # hex escapes
        r"<\|[^|>]+\|>",           # instruction tokens
        r"\[INST\]",
        r"###\s*System",
    ]
    for pattern in _injection_patterns:
        if re.search(pattern, hint, re.IGNORECASE):
            return "[strategy hint redacted due to suspicious content]"
    return hint



def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Error classification (expanded v2)
# ══════════════════════════════════════════════════════════════════

ERROR_TYPES: dict[str, str] = {
    "FileNotFoundError": "FILE_NOT_FOUND",
    "PermissionError": "PERMISSION_ERROR",
    "TimeoutError": "TIMEOUT",
    "asyncio.TimeoutError": "TIMEOUT",
    "ConnectionError": "CONNECTION_ERROR",
    "ConnectionResetError": "CONNECTION_ERROR",
    "ConnectionRefusedError": "CONNECTION_ERROR",
    "KeyError": "DATA_SCHEMA_CHANGE",
    "IndexError": "DATA_SCHEMA_CHANGE",
    "ValueError": "DATA_FORMAT_ERROR",
    "TypeError": "DATA_FORMAT_ERROR",
    "ModuleNotFoundError": "MISSING_MODULE",
    "ImportError": "MISSING_MODULE",
    "json.JSONDecodeError": "DATA_FORMAT_ERROR",
    "UnicodeDecodeError": "ENCODING_ERROR",
    "UnicodeEncodeError": "ENCODING_ERROR",
    "OSError": "OS_ERROR",
    "IsADirectoryError": "OS_ERROR",
    "NotADirectoryError": "OS_ERROR",
    "RecursionError": "CODE_ERROR",
    "MemoryError": "RESOURCE_EXHAUSTED",
    "RuntimeError": "RUNTIME_ERROR",
    "AttributeError": "CODE_ERROR",
    "StopIteration": "DATA_EXHAUSTED",
    "ZeroDivisionError": "DATA_FORMAT_ERROR",
}

# Extra string patterns for error classification
_STR_PATTERNS: list[tuple[str, str]] = [
    (r"no such file|not found|cannot find", "FILE_NOT_FOUND"),
    (r"timeout|timed out", "TIMEOUT"),
    (r"permission denied|access denied|operation not permitted", "PERMISSION_ERROR"),
    (r"connection refused|connection reset|network", "CONNECTION_ERROR"),
    (r"encoding|codec|decode|encode", "ENCODING_ERROR"),
    (r"safety|blocked|content.?filter", "LLM_SAFETY_BLOCK"),
    (r"rate.?limit|429|too many requests", "LLM_RATE_LIMIT"),
    (r"api.?key|unauthorized|401|403", "LLM_AUTH_ERROR"),
    (r"quota|limit exceeded", "LLM_QUOTA"),
    (r"column|header|sheet|worksheet", "DATA_SCHEMA_CHANGE"),
    (r"memory|out of memory|oom", "RESOURCE_EXHAUSTED"),
    (r"disk|no space", "DISK_FULL"),
    # v9.21: DOM-informed patterns
    (r"captcha|recaptcha|hcaptcha", "CAPTCHA_DETECTED"),
    (r"maintenance|bảo trì|under construction", "SERVICE_UNAVAILABLE"),
    (r"login.?required|sign.?in.?required|unauthorized", "AUTH_REQUIRED"),
    (r"element.?not.?found|selector.?not.?found|no.?such.?element", "UI_ELEMENT_MISSING"),
    (r"stale.?element|detached|not.?attached", "UI_STALE_ELEMENT"),
]


# ══════════════════════════════════════════════════════════════════
# Built-in strategy bank (v2: expanded + parameterized)
# ══════════════════════════════════════════════════════════════════

# Each strategy: {name, description, action, priority, transform(ctx)->dict}
# priority: lower = try earlier in list

HEALING_STRATEGIES: dict[str, list[dict]] = {
    "FILE_NOT_FOUND": [
        {
            "name": "search_broader",
            "description": "Tìm file với pattern rộng hơn trong Documents/Desktop/Downloads",
            "priority": 1,
            "action": "search_files",
            "hint": "Dùng find_files() với pattern chứa từ khoá từ tên file gốc",
        },
        {
            "name": "search_home",
            "description": "Tìm trong toàn bộ home directory với tên file",
            "priority": 2,
            "action": "search_files",
            "hint": "Dùng find_files(pattern='*<tên_file>*', search_dirs=['~'])",
        },
        {
            "name": "check_recent",
            "description": "Tìm file được sửa gần đây nhất có extension phù hợp",
            "priority": 3,
            "action": "search_files",
            "hint": "Dùng find_files() sắp xếp theo modified_time, lấy file mới nhất",
        },
    ],
    "TIMEOUT": [
        {
            "name": "retry_simple",
            "description": "Thử lại với input nhỏ hơn hoặc timeout dài hơn",
            "priority": 1,
            "action": "retry",
            "hint": "Giảm kích thước dữ liệu xử lý, tăng timeout",
        },
        {
            "name": "split_task",
            "description": "Chia nhỏ tác vụ thành nhiều phần",
            "priority": 2,
            "action": "retry",
            "hint": "Xử lý từng batch nhỏ thay vì toàn bộ file cùng lúc",
        },
    ],
    "DATA_SCHEMA_CHANGE": [
        {
            "name": "auto_detect_columns",
            "description": "Tự phát hiện tên cột thực tế trong file",
            "priority": 1,
            "action": "detect_schema",
            "hint": "Đọc header row trước, in ra tên cột thực tế, rồi dùng đúng tên đó",
        },
        {
            "name": "fuzzy_column_match",
            "description": "Tìm cột tương tự theo keyword",
            "priority": 2,
            "action": "detect_schema",
            "hint": "Dùng str.lower() và contains() để tìm cột chứa keyword",
        },
    ],
    "ENCODING_ERROR": [
        {
            "name": "try_utf8_sig",
            "description": "Thử encoding utf-8-sig (Excel export)",
            "priority": 1,
            "action": "retry",
            "hint": "Thêm encoding='utf-8-sig' vào open()/read_csv()",
        },
        {
            "name": "try_latin1",
            "description": "Thử encoding latin-1 (Windows locale)",
            "priority": 2,
            "action": "retry",
            "hint": "Thêm encoding='latin-1' — phổ biến cho file Excel cũ trên Windows",
        },
        {
            "name": "try_cp1252",
            "description": "Thử encoding cp1252 (Windows-1252)",
            "priority": 3,
            "action": "retry",
            "hint": "Thêm encoding='cp1252' hoặc errors='ignore'",
        },
    ],
    "MISSING_MODULE": [
        {
            "name": "use_stdlib_alt",
            "description": "Thay thế bằng stdlib Python",
            "priority": 1,
            "action": "refactor",
            "hint": "Dùng csv.reader thay pandas, json thay ujson, urllib thay requests",
        },
        {
            "name": "use_openpyxl",
            "description": "Dùng openpyxl thay thế cho Excel",
            "priority": 2,
            "action": "refactor",
            "hint": "openpyxl đã có trong venv — dùng thay vì xlrd/xlwt",
        },
    ],
    "PERMISSION_ERROR": [
        {
            "name": "copy_to_tmp",
            "description": "Copy file sang /tmp để đọc",
            "priority": 1,
            "action": "copy_file",
            "hint": "import shutil; shutil.copy(src, '/tmp/phidipus_output/') rồi đọc từ đó",
        },
        {
            "name": "read_only_mode",
            "description": "Mở file ở chế độ read-only",
            "priority": 2,
            "action": "retry",
            "hint": "Mở file với mode='r' và không ghi vào file gốc",
        },
    ],
    "LLM_SAFETY_BLOCK": [
        {
            "name": "rephrase_neutral",
            "description": "Diễn đạt lại yêu cầu trung lập hơn",
            "priority": 1,
            "action": "rephrase",
            "hint": "Loại bỏ từ nhạy cảm, mô tả kỹ thuật hơn: 'tạo script Python để...'",
        },
        {
            "name": "switch_provider",
            "description": "Chuyển sang provider không bị SAFETY block",
            "priority": 2,
            "action": "switch_provider",
            "hint": "Dùng openrouter/qwen3-coder-480b-free thay Gemini",
            "next_provider": "openrouter/qwen3-coder-480b-free",
        },
    ],
    "LLM_RATE_LIMIT": [
        {
            "name": "wait_and_retry",
            "description": "Đợi 30s rồi thử lại",
            "priority": 1,
            "action": "wait_retry",
            "hint": "await asyncio.sleep(30) rồi thử lại cùng provider",
        },
        {
            "name": "switch_provider",
            "description": "Dùng provider khác ngay để không mất thời gian chờ",
            "priority": 2,
            "action": "switch_provider",
            "hint": "Dùng ollama local — không có rate limit",
            "next_provider": "ollama/qwen2.5-coder:7b",
        },
    ],
    "LLM_AUTH_ERROR": [
        {
            "name": "switch_free_provider",
            "description": "Chuyển sang provider không cần API key",
            "priority": 1,
            "action": "switch_provider",
            "hint": "OpenRouter free tier hoặc Ollama local",
            "next_provider": "openrouter/qwen3-coder-480b-free",
        },
    ],
    "DATA_FORMAT_ERROR": [
        {
            "name": "validate_input",
            "description": "Kiểm tra và làm sạch dữ liệu đầu vào",
            "priority": 1,
            "action": "retry",
            "hint": "Thêm bước validate: kiểm tra type, xử lý None/NaN trước khi tính toán",
        },
        {
            "name": "convert_types",
            "description": "Chuyển đổi kiểu dữ liệu trước khi xử lý",
            "priority": 2,
            "action": "retry",
            "hint": "Dùng pd.to_numeric(errors='coerce') hoặc str() để ép kiểu",
        },
    ],
    "RESOURCE_EXHAUSTED": [
        {
            "name": "reduce_batch",
            "description": "Xử lý từng batch nhỏ hơn",
            "priority": 1,
            "action": "retry",
            "hint": "Chia list/dataframe thành chunks nhỏ, xử lý từng chunk",
        },
    ],
    "OS_ERROR": [
        {
            "name": "use_tmp",
            "description": "Dùng thư mục /tmp thay vì thư mục hiện tại",
            "priority": 1,
            "action": "retry",
            "hint": "Đổi output_dir sang /tmp/phidipus_output/",
        },
    ],
    "CODE_ERROR": [
        {
            "name": "add_null_checks",
            "description": "Thêm kiểm tra None/empty trước khi truy cập thuộc tính",
            "priority": 1,
            "action": "retry",
            "hint": "Thêm: if obj is None: ... hoặc getattr(obj, 'attr', default)",
        },
    ],
    # ── v9.21: DOM-informed error types ──────────────────────────
    "CAPTCHA_DETECTED": [
        {
            "name": "wait_and_notify",
            "description": "Chờ 30s rồi thử lại — captcha có thể hết hạn",
            "priority": 1,
            "action": "wait_retry",
            "hint": "Chờ 30 giây rồi refresh trang, captcha challenge có thể reset",
        },
        {
            "name": "human_intervention",
            "description": "Yêu cầu người dùng giải captcha",
            "priority": 2,
            "action": "escalate",
            "hint": "Gửi screenshot cho admin qua Telegram, chờ xác nhận đã giải captcha",
        },
    ],
    "SERVICE_UNAVAILABLE": [
        {
            "name": "exponential_backoff",
            "description": "Chờ 30s-60s rồi thử lại — website có thể đang bảo trì ngắn",
            "priority": 1,
            "action": "wait_retry",
            "hint": "Dùng asyncio.sleep(30) rồi thử lại. Nếu vẫn lỗi, chờ 60s",
        },
        {
            "name": "alternative_source",
            "description": "Dùng nguồn khác — API thay vì web, hoặc site backup",
            "priority": 2,
            "action": "reroute",
            "hint": "Nếu website không truy cập được, thử dùng API hoặc nguồn dữ liệu khác",
        },
    ],
    "AUTH_REQUIRED": [
        {
            "name": "navigate_login",
            "description": "Mở trang đăng nhập — session có thể đã hết hạn",
            "priority": 1,
            "action": "reroute",
            "hint": "Navigate đến trang login của website, chờ user login, rồi quay lại task",
        },
        {
            "name": "refresh_cookies",
            "description": "Refresh trang để lấy cookie mới",
            "priority": 2,
            "action": "retry",
            "hint": "Cmd+R refresh trang, chờ 3s, rồi thử lại thao tác",
        },
    ],
    "UI_ELEMENT_MISSING": [
        {
            "name": "vlm_re_analyze",
            "description": "Dùng VLM nhìn lại màn hình, tìm element ở vị trí mới",
            "priority": 1,
            "action": "re_perceive",
            "hint": "Chụp screenshot mới, dùng VLM tìm element bằng mô tả thay vì tọa độ cũ",
        },
        {
            "name": "dom_find_alternative",
            "description": "Tìm element bằng text/aria-label thay vì selector cũ",
            "priority": 2,
            "action": "retry",
            "hint": "Dùng DOM buttons/inputs list để tìm element có text phù hợp",
        },
        {
            "name": "wait_page_load",
            "description": "Trang chưa load xong — chờ 5s rồi thử lại",
            "priority": 3,
            "action": "wait_retry",
            "hint": "Dùng wait_until(element_exists, timeout=10) trước khi click",
        },
    ],
    "UI_STALE_ELEMENT": [
        {
            "name": "refresh_and_retry",
            "description": "Element đã bị DOM update — refresh rồi tìm lại",
            "priority": 1,
            "action": "retry",
            "hint": "Chờ 2s cho DOM ổn định, rồi tìm lại element bằng selector mới",
        },
    ],
}


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class HealingResult:
    """Result of a healing attempt."""
    healed: bool
    strategy_name: str = ""
    strategy_description: str = ""
    strategy_hint: str = ""         # Actionable instruction for LLM fix prompt
    new_context: dict[str, Any] | None = None
    error_type: str = ""
    original_error: str = ""
    source: str = ""                # "learned" | "builtin" | "pattern"
    next_provider: str = ""         # Set if strategy is switch_provider
    confidence: float = 0.0         # 0.0-1.0 confidence in this fix


@dataclass
class ScoredFix:
    """A fix candidate with computed relevance score."""
    strategy: str
    score: float
    success_rate: float
    source: str
    context_match: float = 0.0
    recency_score: float = 0.0
    next_provider: str = ""
    hint: str = ""


# ══════════════════════════════════════════════════════════════════
# Context fingerprinting
# ══════════════════════════════════════════════════════════════════

def _fingerprint(context: dict[str, Any]) -> str:
    """
    Extract context fingerprint for similarity matching.

    Extracts: file extensions, app names, goal keywords, step names.
    Used to match current context with past recorded contexts.
    """
    parts: list[str] = []

    # File extension
    for key in ("file", "file_path", "input_file"):
        val = str(context.get(key, ""))
        m = re.search(r'\.\w+$', val)
        if m:
            parts.append(m.group().lower())

    # App name
    for key in ("app", "app_name"):
        val = str(context.get(key, ""))
        if val:
            parts.append(val.lower()[:20])

    # Step name
    step = str(context.get("step", ""))
    if step:
        parts.append(step.lower()[:20])

    # Goal keywords (first 4 significant words)
    goal = str(context.get("goal", ""))
    words = [w for w in re.findall(r'\w+', goal.lower()) if len(w) >= 4][:4]
    parts.extend(words)

    return "|".join(sorted(set(parts)))


def _context_similarity(fp_a: str, fp_b: str) -> float:
    """
    Jaccard similarity between two context fingerprints.
    Returns 0.0–1.0.
    """
    if not fp_a or not fp_b:
        return 0.0
    set_a = set(fp_a.split("|"))
    set_b = set(fp_b.split("|"))
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


# ══════════════════════════════════════════════════════════════════
# SelfHealer v2
# ══════════════════════════════════════════════════════════════════

class SelfHealer:
    """
    Self-Healing Engine v2 — adaptive pattern learning.

    Improvements over v1:
      - Score-ranked fix selection (success_rate × recency × context_match)
      - Multi-pass: tries ranked learned fixes before falling back to builtins
      - Provider-aware: detects LLM errors → suggests provider switch
      - Context fingerprinting for better fix matching
      - Confidence scoring on HealingResult

    Priority:
      1. Learned fixes từ Failure Memory (sorted by score)
      2. Built-in strategies (match by error_type)
      3. Generic retry hint
    """

    MAX_HEAL_ATTEMPTS = 3
    MIN_SCORE_THRESHOLD = 0.35       # Min score to use a learned fix
    MIN_SUCCESS_RATE_THRESHOLD = 0.2  # Ignore fixes with lower success rate

    def __init__(
        self,
        failure_memory: FailureMemory,
        ipc_client: Any = None,
    ) -> None:
        self._memory = failure_memory
        self._ipc = ipc_client

    # ── Main API ────────────────────────────────────────────────

    async def heal(
        self,
        error: Exception | str,
        context: dict[str, Any] | None = None,
        attempt: int = 1,
    ) -> HealingResult:
        """
        Try to heal from error using adaptive strategy selection.

        v9.21 changes:
          - Skill-type-aware: UI errors get VLM hints, data errors get schema hints
          - DOM diagnostics injected into context when available

        Args:
            error:   Exception or error message string.
            context: Execution context dict. May include dom_page_type, dom_errors.
            attempt: Current attempt number (max MAX_HEAL_ATTEMPTS).

        Returns:
            HealingResult with healed=True + strategy hint if fix found.
        """
        if attempt > self.MAX_HEAL_ATTEMPTS:
            return HealingResult(
                healed=False,
                original_error=str(error)[:200],
                error_type="MAX_ATTEMPTS_REACHED",
            )

        ctx = context or {}
        error_str = str(error)
        error_type = self._classify_error(error)
        ctx_fp = _fingerprint(ctx)

        # v9.21: Detect skill type for strategy boosting
        _skill_type = ctx.get("_skill_type", "")
        if not _skill_type:
            from core.recovery_engine import classify_skill_type
            _goal = str(ctx.get("goal", ""))
            _step = str(ctx.get("step", ""))
            _skill_type = classify_skill_type(_goal, _step)
            ctx["_skill_type"] = _skill_type

        _vlog("🔧", f"Self-Healing v2: {error_type} [{_skill_type}] "
              f"(attempt {attempt}/{self.MAX_HEAL_ATTEMPTS})")
        _vlog("  💬", f"Lỗi: {error_str[:100]}")

        # v9.21: DOM-informed error reclassification
        # If DOM says "login page" but error is TIMEOUT → real issue is AUTH
        dom_page = ctx.get("dom_page_type", "")
        if dom_page == "login" and error_type in ("TIMEOUT", "RUNTIME_ERROR"):
            error_type = "AUTH_REQUIRED"
            _vlog("🌐", "DOM says login page → reclassified as AUTH_REQUIRED")
        elif dom_page == "error" and ctx.get("dom_errors"):
            # Use DOM error message for better diagnosis
            dom_err = ctx["dom_errors"][0] if ctx["dom_errors"] else ""
            if "captcha" in dom_err.lower():
                error_type = "CAPTCHA_DETECTED"
                _vlog("🌐", "DOM captcha detected → CAPTCHA_DETECTED")
            elif "maintenance" in dom_err.lower() or "bảo trì" in dom_err.lower():
                error_type = "SERVICE_UNAVAILABLE"
                _vlog("🌐", "DOM maintenance detected → SERVICE_UNAVAILABLE")

        # ── Pass 1: Ranked learned fixes from Failure Memory ──
        scored = self._rank_learned_fixes(error_type, ctx_fp)

        if scored and scored[0].score >= self.MIN_SCORE_THRESHOLD:
            best = scored[0]
            _vlog("🧠", f"Learned fix (score={best.score:.2f}, "
                  f"rate={best.success_rate:.0%}): {best.strategy[:70]}")

            next_provider = best.next_provider or ""
            if next_provider:
                _vlog("🔌", f"Provider switch → {next_provider}")

            return HealingResult(
                healed=True,
                strategy_name="learned_adaptive",
                strategy_description=best.strategy[:100],
                strategy_hint=best.hint or best.strategy[:200],
                new_context={**ctx, "_healing_fix": best.strategy,
                             "_healing_source": "learned"},
                error_type=error_type,
                original_error=error_str[:200],
                source="learned",
                next_provider=next_provider,
                confidence=min(1.0, best.score),
            )

        # ── Pass 2: Built-in strategies ──────────────────────
        builtin_result = self._apply_builtin(error_type, ctx, attempt)
        if builtin_result:
            return builtin_result

        # ── Pass 3: Generic Failure Memory builtin_fix ───────
        fallback = self._memory.builtin_fix(
            type(error).__name__ if isinstance(error, Exception) else error_type
        )
        if fallback:
            _vlog("📖", f"Generic fix: {fallback[:70]}")
            return HealingResult(
                healed=True,
                strategy_name="builtin_generic",
                strategy_description=fallback,
                strategy_hint=fallback,
                new_context={**ctx, "_healing_fix": fallback},
                error_type=error_type,
                original_error=error_str[:200],
                source="pattern",
                confidence=0.3,
            )

        _vlog("❌", f"Không tìm được fix cho {error_type}")
        return HealingResult(
            healed=False,
            error_type=error_type,
            original_error=error_str[:200],
        )

    def record_outcome(
        self,
        error_type: str,
        strategy: str,
        success: bool,
        context: dict[str, Any] | None = None,
        goal: str = "",
    ) -> None:
        """
        Record whether healing worked → updates Failure Memory scores.

        v2: also stores context_fingerprint for future matching.
        """
        ctx = context or {}
        ctx_fp = _fingerprint(ctx)
        augmented_ctx = {**ctx, "_context_fingerprint": ctx_fp}

        self._memory.record_failure(
            error_type=error_type,
            error_message="",
            context=augmented_ctx,
            fix_strategy=strategy,
            fix_success=success,
            goal=goal,
        )
        icon = "✅" if success else "❌"
        _vlog("📝", f"Healing outcome {icon}: {strategy[:60]}")

    def build_fix_prompt_suffix(self, result: HealingResult) -> str:
        """
        Build a suffix to append to the LLM fix prompt.

        Gives the LLM concrete, actionable instructions based on
        the chosen healing strategy.
        """
        if not result.healed:
            return ""

        parts = [f"\n[HEALING HINT — {result.error_type}]"]

        if result.strategy_hint:
            # [M-10 FIX] Sanitize strategy_hint to prevent prompt injection
            # from poisoned failure_memory.json
            safe_hint = _sanitize_strategy_hint(result.strategy_hint)
            parts.append(f"Áp dụng fix sau: {safe_hint}")
        elif result.strategy_description:
            parts.append(f"Chiến lược: {result.strategy_description}")

        if result.next_provider:
            parts.append(
                f"[PROVIDER] Chuyển sang: {result.next_provider} "
                f"do lỗi provider hiện tại"
            )

        if result.new_context and result.new_context.get("encoding"):
            parts.append(
                f"Dùng encoding='{result.new_context['encoding']}' "
                f"khi đọc file."
            )

        parts.append(f"(Confidence: {result.confidence:.0%})")
        return "\n".join(parts)

    # ── Ranking logic ────────────────────────────────────────────

    def _rank_learned_fixes(
        self,
        error_type: str,
        ctx_fingerprint: str,
    ) -> list[ScoredFix]:
        """
        Query Failure Memory → rank all known fixes by composite score.

        Score = 0.6 × success_rate
              + 0.3 × recency_score (decays over 7 days)
              + 0.1 × context_match (Jaccard similarity)

        Filters out fixes with success_rate < MIN_SUCCESS_RATE_THRESHOLD.
        """
        all_fixes = self._memory.find_all_fixes(error_type)
        if not all_fixes:
            return []

        now = time.time()
        scored: list[ScoredFix] = []

        for fix in all_fixes:
            success_rate = fix.get("success_rate", 0.0)
            if success_rate < self.MIN_SUCCESS_RATE_THRESHOLD:
                continue  # Skip consistently failing fixes

            # Recency: decays over 7 days (604800s)
            last_used = fix.get("last_used", 0.0)
            age_s = now - last_used if last_used else 7 * 86400
            recency = max(0.0, 1.0 - age_s / (7 * 86400))

            # Context match: compare fingerprints
            ctx_match = 0.0
            for recorded_fp in fix.get("context_patterns", []):
                sim = _context_similarity(ctx_fingerprint, recorded_fp)
                ctx_match = max(ctx_match, sim)

            # Composite score
            score = (
                0.6 * success_rate
                + 0.3 * recency
                + 0.1 * ctx_match
            )

            # Check if this fix suggests a provider switch
            strategy_str = fix.get("strategy", "")
            next_provider = ""
            if "openrouter" in strategy_str.lower():
                next_provider = "openrouter/qwen3-coder-480b-free"
            elif "ollama" in strategy_str.lower():
                next_provider = "ollama/qwen2.5-coder:7b"

            scored.append(ScoredFix(
                strategy=strategy_str,
                score=score,
                success_rate=success_rate,
                source="learned",
                context_match=ctx_match,
                recency_score=recency,
                next_provider=next_provider,
                hint=fix.get("hint", ""),
            ))

        # Sort: highest score first
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored

    # ── Built-in strategies ──────────────────────────────────────

    def _apply_builtin(
        self,
        error_type: str,
        ctx: dict[str, Any],
        attempt: int,
    ) -> HealingResult | None:
        """
        Apply built-in strategy for error_type.
        Uses attempt number to cycle through alternatives.
        """
        strategies = HEALING_STRATEGIES.get(error_type, [])
        if not strategies:
            return None

        # Pick strategy: attempt 1 → priority 1, attempt 2 → priority 2, etc.
        sorted_strats = sorted(strategies, key=lambda s: s.get("priority", 99))
        idx = min(attempt - 1, len(sorted_strats) - 1)
        strat = sorted_strats[idx]

        _vlog("🔧", f"Built-in [{error_type}] strategy {idx+1}/{len(sorted_strats)}: "
              f"{strat['description']}")

        next_provider = strat.get("next_provider", "")
        if next_provider:
            _vlog("🔌", f"Built-in → provider switch: {next_provider}")

        return HealingResult(
            healed=True,
            strategy_name=strat["name"],
            strategy_description=strat["description"],
            strategy_hint=strat.get("hint", strat["description"]),
            new_context={**ctx, "_healing_strategy": strat["name"],
                         "_healing_source": "builtin"},
            error_type=error_type,
            original_error="",
            source="builtin",
            next_provider=next_provider,
            confidence=0.55,    # Builtin = medium confidence
        )

    # ── Error classification ─────────────────────────────────────

    @staticmethod
    def _classify_error(error: Exception | str) -> str:
        """
        Classify error into a standardised type string.

        v2: extra string patterns + LLM-specific error types.
        """
        if isinstance(error, Exception):
            cls_name = type(error).__name__
            if cls_name in ERROR_TYPES:
                return ERROR_TYPES[cls_name]
            # Check MRO
            for parent in type(error).__mro__:
                pname = parent.__name__
                if pname in ERROR_TYPES:
                    return ERROR_TYPES[pname]

        error_str = str(error).lower()
        for pattern, etype in _STR_PATTERNS:
            if re.search(pattern, error_str):
                return etype

        return "UNKNOWN_ERROR"

    # ── Stats ────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Stats for Admin Panel and main.py boot log."""
        fm_stats = self._memory.stats()
        return {
            "version": "v2",
            "failure_memory": fm_stats,
            "builtin_strategies": sum(len(v) for v in HEALING_STRATEGIES.values()),
            "error_types_covered": len(HEALING_STRATEGIES),
            "error_types_known": len(ERROR_TYPES),
            "total_learned_fixes": fm_stats.get("total_records", 0),
            "min_score_threshold": SelfHealer.MIN_SCORE_THRESHOLD,
            "min_success_rate": SelfHealer.MIN_SUCCESS_RATE_THRESHOLD,
        }
