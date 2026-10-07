# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/recovery_engine.py — Phidipus Recovery Engine v9.21
═══════════════════════════════════════════════════════

Three targeted fixes for "bất tử" resilience:

1. DOM State Capture — Khi Chrome task fail, capture DOM snapshot
   vào error context cho Self-Healing chẩn đoán chính xác hơn.

2. Per-Skill-Type Recovery Policy — UI skills → VLM re-analyze,
   Data skills → input validation, System skills → permission escalation.
   Auto-classify, không hardcode per skill.

3. Dynamic Re-planning — Khi step fail 2x liên tiếp, quay lại
   TaskDecomposer với "step X blocked, find alternative path".

Integration:
  - Wired vào agent_loop.py plan executor
  - Extends SelfHealer (không replace)
  - Uses existing IPC/Chrome infrastructure

Security:
  - DOM capture truncated (max 50KB) — no full page exfiltration
  - No new IPC actions required — uses existing infrastructure
  - Re-planning respects max_steps ceiling
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# 1. DOM STATE CAPTURE
# ══════════════════════════════════════════════════════════════════

# Max DOM text to capture (prevent memory explosion)
_MAX_DOM_BYTES = 50_000  # 50KB — enough for structure, not full content

# JavaScript to extract structured DOM snapshot
_DOM_SNAPSHOT_JS = """
(function() {
    var result = {
        url: location.href,
        title: document.title,
        bodyText: (document.body ? document.body.innerText : '').substring(0, 2000),
        forms: [],
        buttons: [],
        inputs: [],
        errors: [],
        modals: [],
    };

    // Forms
    document.querySelectorAll('form').forEach(function(f, i) {
        if (i < 5) result.forms.push({
            action: f.action || '',
            method: f.method || '',
            id: f.id || '',
            fields: Array.from(f.elements).slice(0, 10).map(function(e) {
                return {tag: e.tagName, name: e.name || '', type: e.type || '', id: e.id || ''};
            })
        });
    });

    // Buttons (visible only)
    document.querySelectorAll('button, [role=button], input[type=submit]').forEach(function(b, i) {
        if (i < 15 && b.offsetParent !== null) {
            var r = b.getBoundingClientRect();
            result.buttons.push({
                text: (b.innerText || b.value || '').substring(0, 50),
                id: b.id || '',
                class: b.className ? b.className.substring(0, 80) : '',
                ariaLabel: b.getAttribute('aria-label') || '',
                x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height),
            });
        }
    });

    // Text inputs
    document.querySelectorAll('input[type=text], input[type=search], textarea, [contenteditable=true]').forEach(function(e, i) {
        if (i < 10 && e.offsetParent !== null) {
            var r = e.getBoundingClientRect();
            result.inputs.push({
                tag: e.tagName, name: e.name || '', id: e.id || '',
                placeholder: e.placeholder || '',
                ariaLabel: e.getAttribute('aria-label') || '',
                x: Math.round(r.x), y: Math.round(r.y),
            });
        }
    });

    // Error messages (common patterns)
    document.querySelectorAll('[role=alert], .error, .alert-danger, [class*=error], [class*=Error]').forEach(function(e, i) {
        if (i < 5) result.errors.push((e.innerText || '').substring(0, 200));
    });

    // Modals/dialogs
    document.querySelectorAll('[role=dialog], [aria-modal=true], .modal.show, dialog[open]').forEach(function(d, i) {
        if (i < 3) result.modals.push({
            title: (d.querySelector('h1,h2,h3,[class*=title]') || {}).innerText || '',
            text: (d.innerText || '').substring(0, 300),
        });
    });

    return JSON.stringify(result);
})()
"""


class DOMCapturer:
    """
    Captures Chrome DOM state when automation tasks fail.

    Uses AppleScript (macOS) to execute JavaScript in Chrome.
    Fallback: returns empty dict if Chrome not accessible.

    Output is structured JSON:
      {url, title, bodyText, forms[], buttons[], inputs[], errors[], modals[]}

    This gives Self-Healing MUCH better context than screenshot-only:
      - Button text + coordinates → know exactly what's on screen
      - Error messages → diagnose without VLM
      - Form fields → understand current page state
      - Modals/dialogs → detect popups blocking interaction
    """

    CAPTURE_TIMEOUT = 5  # seconds

    async def capture(self) -> dict[str, Any]:
        """
        Capture current Chrome DOM state.

        Returns structured dict or empty dict on failure.
        Never raises — failure is non-fatal.
        """
        if platform.system() != "Darwin":
            # Linux/Windows: would need different approach
            return {}

        try:
            return await asyncio.wait_for(
                self._capture_macos(), timeout=self.CAPTURE_TIMEOUT
            )
        except (asyncio.TimeoutError, Exception) as exc:
            _vlog("⚠", f"DOM capture failed: {str(exc)[:60]}")
            return {}

    async def _capture_macos(self) -> dict[str, Any]:
        """Execute JS in Chrome via AppleScript."""
        # Escape JS for AppleScript string
        js_escaped = _DOM_SNAPSHOT_JS.replace('"', '\\"').replace('\n', ' ')

        script = (
            'tell application "Google Chrome"\n'
            f'  set result to execute active tab of front window javascript "{js_escaped}"\n'
            '  return result\n'
            'end tell'
        )

        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            timeout=self.CAPTURE_TIMEOUT,
            capture_output=True, text=True,
        )

        output = proc.stdout.strip()
        if not output or output.startswith("missing value"):
            return {}

        # Parse JSON result
        try:
            data = json.loads(output)
            # Truncate total size
            raw = json.dumps(data, ensure_ascii=False)
            if len(raw) > _MAX_DOM_BYTES:
                # Truncate bodyText first
                data["bodyText"] = data.get("bodyText", "")[:500]
                data["_truncated"] = True
            return data

        except json.JSONDecodeError:
            # AppleScript may return non-JSON
            return {"raw_output": output[:2000]}

    def extract_diagnostics(self, dom: dict[str, Any]) -> dict[str, Any]:
        """
        Extract actionable diagnostics from DOM snapshot.

        Returns:
            {
                "page_type": "login|error|form|content|unknown",
                "has_errors": bool,
                "error_messages": list[str],
                "has_modal": bool,
                "modal_text": str,
                "available_buttons": list[str],
                "available_inputs": list[str],
                "url": str,
            }
        """
        if not dom:
            return {"page_type": "unknown"}

        errors = dom.get("errors", [])
        modals = dom.get("modals", [])
        buttons = dom.get("buttons", [])
        inputs = dom.get("inputs", [])
        body = dom.get("bodyText", "").lower()
        url = dom.get("url", "")

        # Classify page type
        page_type = "unknown"
        if any(kw in body for kw in ["sign in", "log in", "đăng nhập", "password"]):
            page_type = "login"
        elif errors or any(kw in body for kw in ["error", "lỗi", "something went wrong"]):
            page_type = "error"
        elif inputs:
            page_type = "form"
        elif dom.get("title"):
            page_type = "content"

        return {
            "page_type": page_type,
            "has_errors": bool(errors),
            "error_messages": errors[:3],
            "has_modal": bool(modals),
            "modal_text": modals[0].get("text", "")[:200] if modals else "",
            "available_buttons": [b.get("text", "")[:30] for b in buttons[:8]],
            "available_inputs": [
                i.get("placeholder") or i.get("ariaLabel") or i.get("name") or "unnamed"
                for i in inputs[:5]
            ],
            "url": url,
        }


# ══════════════════════════════════════════════════════════════════
# 2. PER-SKILL-TYPE RECOVERY POLICY
# ══════════════════════════════════════════════════════════════════

class SkillType:
    """Skill type constants."""
    UI = "ui"           # Browser/app interaction (click, type, navigate)
    DATA = "data"       # File/data processing (read, filter, transform)
    SYSTEM = "system"   # OS operations (launch app, file management)
    NETWORK = "network" # Web requests, API calls
    CONTENT = "content" # Content generation (text, images)
    UNKNOWN = "unknown"


# Keyword-based classification
_SKILL_TYPE_KEYWORDS: dict[str, list[str]] = {
    SkillType.UI: [
        "click", "nhấn", "button", "nút", "type", "gõ", "navigate",
        "vào trang", "mở tab", "scroll", "select", "dropdown",
        "form", "input", "upload", "download", "checkbox",
        "facebook", "instagram", "chrome", "browser",
    ],
    SkillType.DATA: [
        "excel", "csv", "json", "file", "tệp", "đọc", "read",
        "filter", "lọc", "sort", "sắp xếp", "transform",
        "column", "cột", "row", "hàng", "data", "dữ liệu",
        "báo cáo", "report", "tổng hợp", "aggregate",
        "parse", "extract", "import", "export",
    ],
    SkillType.SYSTEM: [
        "mở ứng dụng", "open app", "launch", "khởi động",
        "permission", "quyền", "directory", "thư mục",
        "process", "kill", "restart", "install",
    ],
    SkillType.NETWORK: [
        "api", "http", "url", "fetch", "request",
        "download", "scrape", "crawl", "web",
        "giá", "price", "search web", "tìm trên mạng",
    ],
    SkillType.CONTENT: [
        "tạo content", "viết", "write", "generate",
        "tạo ảnh", "image", "post", "đăng bài",
        "caption", "hashtag",
    ],
}


def classify_skill_type(goal: str, step_action: str = "") -> str:
    """
    Auto-classify a goal/step into skill type.

    Uses keyword matching on both goal text and step action name.
    Returns the type with highest keyword match count.
    """
    text = f"{goal} {step_action}".lower()
    scores: dict[str, int] = {}

    for stype, keywords in _SKILL_TYPE_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in text)
        if count > 0:
            scores[stype] = count

    if not scores:
        return SkillType.UNKNOWN

    return max(scores, key=scores.get)


# Recovery policies per skill type
RECOVERY_POLICIES: dict[str, dict[str, Any]] = {
    SkillType.UI: {
        "max_retries": 3,
        "strategies": [
            "vlm_re_analyze",    # Re-capture screen, find element at new position
            "dom_diagnose",      # Use DOM snapshot to find alternative selector
            "wait_and_retry",    # Page may still be loading
            "close_modal_first", # Modal/popup blocking interaction
        ],
        "fallback": "human_intervention",
        "confidence_threshold": 0.7,  # Need high confidence for UI actions
        "capture_dom": True,
        "capture_screenshot": True,
    },
    SkillType.DATA: {
        "max_retries": 2,
        "strategies": [
            "auto_detect_schema",  # Re-read headers, find actual column names
            "try_encodings",       # utf-8 → utf-8-sig → latin-1
            "validate_input",      # Check for None/NaN before processing
            "reduce_batch",        # Process smaller chunks
        ],
        "fallback": "skip_step",
        "confidence_threshold": 0.5,
        "capture_dom": False,
        "capture_screenshot": False,
    },
    SkillType.SYSTEM: {
        "max_retries": 2,
        "strategies": [
            "check_permissions",   # Verify file/app permissions
            "use_alternative_path", # Try /tmp or ~/Downloads
            "wait_for_app",        # App may still be launching
        ],
        "fallback": "human_intervention",
        "confidence_threshold": 0.6,
        "capture_dom": False,
        "capture_screenshot": True,
    },
    SkillType.NETWORK: {
        "max_retries": 3,
        "strategies": [
            "retry_with_backoff",  # Exponential backoff
            "switch_provider",     # Try different API/source
            "check_connectivity",  # Verify internet connection
        ],
        "fallback": "skip_step",
        "confidence_threshold": 0.4,
        "capture_dom": True,   # Helpful for web scraping errors
        "capture_screenshot": False,
    },
    SkillType.CONTENT: {
        "max_retries": 2,
        "strategies": [
            "retry_with_different_prompt",  # Rephrase the generation prompt
            "switch_provider",              # Try different LLM
        ],
        "fallback": "skip_step",
        "confidence_threshold": 0.5,
        "capture_dom": False,
        "capture_screenshot": False,
    },
    SkillType.UNKNOWN: {
        "max_retries": 2,
        "strategies": ["retry_simple", "validate_input"],
        "fallback": "human_intervention",
        "confidence_threshold": 0.5,
        "capture_dom": False,
        "capture_screenshot": True,
    },
}


def get_recovery_policy(goal: str, step_action: str = "") -> dict[str, Any]:
    """Get recovery policy for a goal/step based on auto-classified type."""
    stype = classify_skill_type(goal, step_action)
    policy = RECOVERY_POLICIES.get(stype, RECOVERY_POLICIES[SkillType.UNKNOWN])
    return {**policy, "skill_type": stype}


# ══════════════════════════════════════════════════════════════════
# 3. DYNAMIC RE-PLANNING
# ══════════════════════════════════════════════════════════════════

@dataclass
class ReplanRequest:
    """Request to re-plan after step failure."""
    task_id: str
    original_goal: str
    failed_step_id: str
    failed_step_description: str
    failed_step_action: str
    failure_reason: str
    completed_steps: list[str]     # IDs of completed steps
    step_outputs: dict[str, Any]   # Outputs from completed steps
    attempt: int = 1               # Re-plan attempt (max 2)
    dom_diagnostics: dict = field(default_factory=dict)


@dataclass
class ReplanResult:
    """Result of re-planning attempt."""
    success: bool
    new_plan: Any = None           # TaskPlan or None
    skipped_actions: list[str] = field(default_factory=list)  # Actions to avoid
    explanation: str = ""


class DynamicReplanner:
    """
    Re-plans task execution when steps fail repeatedly.

    Trigger: step fails 2x consecutively (including self-healing attempts).
    Action: Call TaskDecomposer with enriched context about what failed.

    Key insight: don't just retry the same plan. Tell the planner
    "step X using method Y doesn't work, find alternative approach."

    Examples:
      - "Web scraping blocked" → re-plan: "use API instead"
      - "Chrome element not found" → re-plan: "use keyboard shortcut"
      - "Excel file locked" → re-plan: "copy file first, then read copy"
    """

    MAX_REPLAN_ATTEMPTS = 2  # Max re-plans per task

    def __init__(self, decomposer: Any = None) -> None:
        self._decomposer = decomposer
        self._replan_count: dict[str, int] = {}  # task_id → count

    async def replan(self, request: ReplanRequest) -> ReplanResult:
        """
        Attempt to re-plan a task after step failure.

        Returns new TaskPlan with alternative steps, or failure.
        """
        if not self._decomposer:
            return ReplanResult(success=False, explanation="TaskDecomposer not available")

        task_key = request.task_id
        current_count = self._replan_count.get(task_key, 0)

        if current_count >= self.MAX_REPLAN_ATTEMPTS:
            _vlog("🔁", f"Max re-plans reached ({self.MAX_REPLAN_ATTEMPTS}) for {task_key}")
            return ReplanResult(
                success=False,
                explanation=f"Đã re-plan {current_count} lần, không thể tiếp tục",
            )

        self._replan_count[task_key] = current_count + 1

        # Build enriched goal with failure context
        enriched_goal = self._build_replan_goal(request)

        _vlog("🔄", f"Dynamic re-plan #{current_count + 1}: {enriched_goal[:100]}")

        try:
            new_plan = await self._decomposer.decompose(enriched_goal)

            if new_plan and new_plan.total >= 1:
                _vlog("📋", f"Re-plan thành công: {new_plan.total} bước mới")
                return ReplanResult(
                    success=True,
                    new_plan=new_plan,
                    skipped_actions=[request.failed_step_action],
                    explanation=f"Re-planned: avoiding '{request.failed_step_action}'",
                )
            else:
                return ReplanResult(
                    success=False,
                    explanation="TaskDecomposer không tạo được plan mới",
                )

        except Exception as exc:
            _vlog("❌", f"Re-plan failed: {exc}")
            return ReplanResult(success=False, explanation=str(exc)[:200])

    def _build_replan_goal(self, request: ReplanRequest) -> str:
        """
        Build enriched goal string for re-planning.

        Includes: original goal + what failed + what's already done + constraints.
        """
        parts = [request.original_goal]

        # Add failure context
        parts.append(
            f"\n\nLƯU Ý QUAN TRỌNG: Bước '{request.failed_step_description}' "
            f"({request.failed_step_action}) đã thất bại: {request.failure_reason[:100]}."
        )

        # Add constraint: avoid the failed approach
        parts.append(
            f"KHÔNG dùng '{request.failed_step_action}' cho bước này. "
            f"Tìm cách tiếp cận KHÁC."
        )

        # Add DOM diagnostics if available
        diag = request.dom_diagnostics
        if diag:
            if diag.get("page_type") == "login":
                parts.append("Trang hiện tại là trang đăng nhập — cần login trước.")
            elif diag.get("page_type") == "error":
                errors = diag.get("error_messages", [])
                if errors:
                    parts.append(f"Trang hiện tại hiển thị lỗi: {errors[0][:100]}")
            elif diag.get("has_modal"):
                parts.append(
                    f"Có popup/dialog đang mở: {diag.get('modal_text', '')[:80]}. "
                    "Cần đóng popup trước."
                )
            available = diag.get("available_buttons", [])
            if available:
                parts.append(f"Các nút có trên trang: {', '.join(available[:5])}")

        # Add completed steps context
        if request.completed_steps:
            parts.append(
                f"\nCác bước ĐÃ HOÀN THÀNH (không cần làm lại): "
                f"{', '.join(request.completed_steps)}"
            )

        # Add step outputs for context
        if request.step_outputs:
            for step_id, output in list(request.step_outputs.items())[:3]:
                parts.append(f"  Kết quả {step_id}: {str(output)[:100]}")

        return "\n".join(parts)

    def reset(self, task_id: str) -> None:
        """Reset re-plan counter for a task."""
        self._replan_count.pop(task_id, None)

    def stats(self) -> dict:
        return {
            "active_replans": dict(self._replan_count),
            "max_attempts": self.MAX_REPLAN_ATTEMPTS,
        }


# ══════════════════════════════════════════════════════════════════
# RECOVERY ENGINE — Orchestrates all 3 features
# ══════════════════════════════════════════════════════════════════

class RecoveryEngine:
    """
    Coordinates DOM capture + skill-type policy + dynamic re-planning.

    Usage in agent_loop.py plan executor:

        recovery = RecoveryEngine(decomposer=self._decomposer)

        # When step fails:
        policy = recovery.get_policy(step_goal, step_action)
        if policy["capture_dom"]:
            dom = await recovery.capture_dom()
            error_context["dom"] = dom

        # When step fails 2x:
        replan_result = await recovery.try_replan(...)
        if replan_result.success:
            # Execute new plan instead
    """

    def __init__(self, decomposer: Any = None) -> None:
        self._dom = DOMCapturer()
        self._replanner = DynamicReplanner(decomposer)

    # ── DOM Capture ──────────────────────────────────────────────

    async def capture_dom(self) -> dict[str, Any]:
        """Capture DOM state from Chrome."""
        return await self._dom.capture()

    def diagnose_dom(self, dom: dict[str, Any]) -> dict[str, Any]:
        """Extract actionable diagnostics from DOM."""
        return self._dom.extract_diagnostics(dom)

    # ── Skill-Type Policy ────────────────────────────────────────

    def get_policy(self, goal: str, step_action: str = "") -> dict[str, Any]:
        """Get recovery policy based on auto-classified skill type."""
        return get_recovery_policy(goal, step_action)

    def classify(self, goal: str, step_action: str = "") -> str:
        """Classify skill type."""
        return classify_skill_type(goal, step_action)

    # ── Dynamic Re-planning ──────────────────────────────────────

    async def try_replan(
        self,
        task_id: str,
        goal: str,
        failed_step_id: str,
        failed_step_desc: str,
        failed_step_action: str,
        failure_reason: str,
        completed_steps: list[str],
        step_outputs: dict[str, Any],
        dom_diagnostics: dict | None = None,
    ) -> ReplanResult:
        """Try to re-plan after step failure."""
        request = ReplanRequest(
            task_id=task_id,
            original_goal=goal,
            failed_step_id=failed_step_id,
            failed_step_description=failed_step_desc,
            failed_step_action=failed_step_action,
            failure_reason=failure_reason,
            completed_steps=completed_steps,
            step_outputs=step_outputs,
            dom_diagnostics=dom_diagnostics or {},
        )
        return await self._replanner.replan(request)

    def reset_task(self, task_id: str) -> None:
        """Reset re-plan counter for a task."""
        self._replanner.reset(task_id)

    # ── Stats ────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "replanner": self._replanner.stats(),
            "skill_types": list(RECOVERY_POLICIES.keys()),
        }
