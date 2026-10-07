# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/chatbot_context.py — Phidipus AI Chatbot Context Builder v1.0
════════════════════════════════════════════════════════════════════

Thu thập live context từ toàn bộ hệ thống Phidipus đang chạy và
đóng gói thành prompt context để AI chatbot trả lời chính xác về:
  - Skills nào đang có, promoted, hay deactivated
  - Lỗi nào xảy ra gần đây và pattern
  - Workflow nào được dùng nhiều
  - Resource state hiện tại (RAM/CPU)
  - 5 task gần nhất kết quả ra sao
  - Cấu trúc hệ thống (file tree, modules)

3 loại context:
  STATIC   — kiến thức cố định về Phidipus (modules, workflow, cú pháp lệnh)
  LIVE     — trạng thái thực tế đang chạy (skills, errors, resources)
  LOG      — log gần nhất (khi hỏi về lỗi cụ thể)

Usage:
    ctx = ChatbotContext(agent_loop=loop, task_history=history)
    ctx.inject_components(
        skill_db=..., failure_memory=..., workflow_lib=...,
        resource_guard=..., runtime_monitor=...
    )
    prompt_context = await ctx.build(question, detail_level="full")
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ══════════════════════════════════════════════════════════════════
# Static Knowledge Base — kiến thức cố định về Phidipus
# ══════════════════════════════════════════════════════════════════

PHIDIPUS_STATIC_KNOWLEDGE = """
# Phidipus v1.22 — Kiến Trúc Hệ Thống

## Tổng Quan
Phidipus Agents là hệ thống AI agent tự động hóa macOS, chạy local.
Nhận lệnh qua Telegram Bot hoặc Admin Panel (localhost:8912).
Kiến trúc 2 tầng: L1 Orchestrator (agent_loop) + L2 Daemon (IPC + OS automation).

## Luồng Xử Lý (theo thứ tự ưu tiên)
1. SmartAction (P0-P3) — instant, no LLM: mở app, chrome profile, AppleScript
2. SkillIntelligence (P3.5) — kiểm tra registry có skill phù hợp không
3. Workflow shortcut (P4-pre) — nếu goal khớp workflow pattern → bypass SkillForge
4. Skill Forge (P4) — Gemini/LLM sinh Python code → chạy trong subprocess sandbox
5. TaskDecomposer (P4b) — tách goal thành subtasks → chạy qua Scheduler
6. LLM + VLM Loop (P5) — last resort

## Các Module Chính
- core/agent_loop.py      — orchestrator chính, điều phối toàn bộ flow
- core/skill_intelligence.py — AutoPromoter, SkillComposer, AntiLoopGuard
- utils/skill_forge.py    — tạo code Python từ LLM (Gemini → Mistral → Ollama)
- planner/task_decomposer.py — PatternLearner, PLAN_TEMPLATES, LLM decompose
- skills/workflow_spider_social_post.py — workflow ChatGPT→Gemini→Facebook
- core/error_diagnostics.py — chẩn đoán lỗi 2 tầng (Rule + AI)
- memory/semantic_memory.py — học file patterns, column names từ task history
- memory/failure_memory.py — lưu lỗi + fix strategies
- skills/skill_db.py       — SQLite registry với TF-IDF + embedding search

## Atomic Tools Có Sẵn (cho Skill Forge)
find_files, read_excel, create_excel, read_csv, read_text, write_text,
filter_rows, aggregate, get_screen_info, get_dir_tree,
web_fetch, read_pdf, get_clipboard, get_recent_files

## LLM FallbackStack (theo thứ tự ưu tiên)
gemini-2.5-flash → gemini-1.5-flash → mistral-small → cerebras-llama3.1-8b
→ openrouter-free → ollama-qwen2.5-coder:7b → ollama-qwen3:8b → ollama-qwen3-vl:8b

## Cú Pháp Lệnh Telegram — HƯỚNG DẪN ĐẦY ĐỦ

### Mở Chrome Profile
```
mở chrome profile 00Fujin
profile 00                     ← viết tắt, hệ thống tự tìm 00Fujin
mở profile 01Tam
mở 3 profile 00 01 02          ← mở nhiều profile cùng lúc
```

### Tạo Ảnh + Content + Đăng Facebook (Workflow Chính)
```
tạo ảnh "[mô tả ảnh]" chatgpt, content "[chủ đề]", đăng facebook
```
Ví dụ:
```
tạo ảnh "robot bee cyberpunk neon" chatgpt, content "điểm yếu openclaw", đăng facebook
profile 00, tạo ảnh "sunset beach" chatgpt, content "du lịch mùa hè", đăng facebook
```
Từ khoá bắt buộc (thứ tự tự do):
  - ChatGPT: chatgpt hoặc chat gpt
  - Ảnh: tạo ảnh, ảnh, image
  - Content: content, nội dung, viết, gemini
  - Đăng: facebook, fb, đăng, post, share

### Tìm File và Xử Lý Excel
```
tìm file tồn kho, lọc bóng đèn, tạo báo cáo, gửi cho tôi
tìm file excel trong Documents, đọc sheet "Doanh thu", tạo biểu đồ
```

### Thao Tác Hệ Thống
```
chụp màn hình
mở terminal
tìm file *.py trong Downloads
copy file abc.txt vào Desktop
```

### Tra Cứu Sản Phẩm (KnowledgeBee)
```
giá đèn LED 12W
tồn kho bóng đèn tiết kiệm
báo cáo tồn kho tháng này
so sánh giá đèn LED với thị trường
```

### Lệnh Telegram Commands
```
/chat    — bật chế độ hỏi đáp AI
/agent   — quay lại chế độ agent
/model   — chọn model AI cho chatbot
/status  — xem trạng thái hệ thống
/skills  — xem danh sách skills
/tasks   — xem lịch sử task
/ollama  — quản lý Ollama models
/memory  — xem bộ nhớ agent
```

## Error Codes Thường Gặp
OLLAMA_DOWN, MODEL_MISSING, NO_API_KEY, RATE_LIMIT, INVALID_KEY,
IPC_MISSING, PORT_BUSY, PROFILE_MISSING, CHROME_CLOSED,
CHATGPT_TIMEOUT, CHATGPT_NOT_LOGIN, IMAGE_DOWNLOAD_FAIL,
GEMINI_NOT_LOGIN, GEMINI_EMPTY, FACEBOOK_NOT_LOGIN, FB_POST_FAIL,
FILE_NOT_FOUND, PERMISSION_DENIED, WORKFLOW_TIMEOUT, SKILL_TIMEOUT,
LOW_RAM, SECURITY_BLOCK

## Lưu Ý Quan Trọng
- Silent failure: task báo ✅ nhưng không làm được gì thật → xảy ra khi
  SkillForge sinh code "thành công" nhưng code không có output
- Profile "00" → tự động resolve thành "00Fujin" qua AppScanner Tier 2
- Ollama tự unload model sau 5 phút không dùng (keep_alive configurable)
- Skill Forge dùng subprocess sandbox: không dùng os, subprocess, import
- workflow_spider_social_post chạy trực tiếp, KHÔNG qua Scheduler
"""


# ══════════════════════════════════════════════════════════════════
# Question Classifier — phân loại câu hỏi để build context phù hợp
# ══════════════════════════════════════════════════════════════════

# Keywords → cần live context
_LIVE_KEYWORDS = (
    "skill", "promoted", "deactivate", "registry",
    "lỗi", "error", "fail", "crash", "không chạy",
    "workflow", "chạy mấy lần", "bao nhiêu",
    "ram", "cpu", "resource", "tài nguyên",
    "task", "vừa rồi", "gần đây", "lần trước",
    "memory", "bộ nhớ", "học được",
    "provider", "gemini", "ollama",
    "pattern", "đã học",
)

# Keywords → cần log context
_LOG_KEYWORDS = (
    "tại sao", "why", "debug", "log",
    "lỗi cụ thể", "traceback", "exception",
    "hôm qua", "lúc nãy", "vừa xảy ra",
)

# Keywords → escalate to Gemini API
_COMPLEX_KEYWORDS = (
    "refactor", "thiết kế lại", "nên sửa",
    "so sánh kiến trúc", "best practice",
    "nâng cấp", "upgrade", "cải thiện",
    "đề xuất", "gợi ý cải tiến",
    "tại sao thiết kế", "design pattern",
    "performance", "tối ưu",
)


def classify_question(question: str) -> dict:
    """
    Phân loại câu hỏi để quyết định context cần thiết và routing.

    Returns:
        {
            "needs_live":  bool,   # cần live system state
            "needs_log":   bool,   # cần recent log
            "use_gemini":  bool,   # escalate to Gemini API
            "complexity":  str,    # "simple" | "medium" | "complex"
        }
    """
    q = question.lower()
    word_count = len(question.split())

    needs_live  = any(kw in q for kw in _LIVE_KEYWORDS)
    needs_log   = any(kw in q for kw in _LOG_KEYWORDS)
    is_complex  = any(kw in q for kw in _COMPLEX_KEYWORDS) or word_count > 50

    complexity = "simple"
    if needs_live or needs_log:
        complexity = "medium"
    if is_complex:
        complexity = "complex"

    return {
        "needs_live":  needs_live,
        "needs_log":   needs_log,
        "use_gemini":  is_complex,
        "complexity":  complexity,
    }


# ══════════════════════════════════════════════════════════════════
# ChatbotContext — thu thập và format context
# ══════════════════════════════════════════════════════════════════

class ChatbotContext:
    """
    Thu thập live context từ Phidipus components và build prompt context.

    Usage:
        ctx = ChatbotContext()
        ctx.inject_components(skill_db=db, failure_memory=fm, ...)
        context_str = await ctx.build(question)
    """

    LOG_PATH     = "logs/phidipus.log"
    LOG_LINES    = 80
    MAX_CTX_LEN  = 3000   # max chars để tránh prompt quá dài

    def __init__(self) -> None:
        # Components — injected from bot or agent_loop
        self._skill_db:       Any = None
        self._failure_memory: Any = None
        self._workflow_lib:   Any = None
        self._resource_guard: Any = None
        self._runtime_monitor: Any = None
        self._intelligence:   Any = None
        self._task_history:   list = []
        self._llm_fallback:   Any = None

    def inject_components(self, **components: Any) -> None:
        """Inject Phidipus components."""
        mapping = {
            "skill_db":        "_skill_db",
            "failure_memory":  "_failure_memory",
            "workflow_lib":    "_workflow_lib",
            "resource_guard":  "_resource_guard",
            "runtime_monitor": "_runtime_monitor",
            "intelligence":    "_intelligence",
            "task_history":    "_task_history",
            "llm_fallback":    "_llm_fallback",
        }
        for k, attr in mapping.items():
            if k in components:
                setattr(self, attr, components[k])

    async def build(
        self,
        question: str,
        detail_level: str = "auto",
    ) -> str:
        """
        Build context string để inject vào chatbot prompt.

        detail_level: "auto" | "minimal" | "full"
          auto    → classify_question() quyết định
          minimal → chỉ static knowledge
          full    → tất cả: static + live + log
        """
        cls = classify_question(question)

        if detail_level == "minimal":
            cls["needs_live"] = False
            cls["needs_log"]  = False
        elif detail_level == "full":
            cls["needs_live"] = True
            cls["needs_log"]  = True

        parts = []

        # 1. Static knowledge (luôn có)
        parts.append(PHIDIPUS_STATIC_KNOWLEDGE)

        # 2. Live context nếu cần
        if cls["needs_live"]:
            live = await self._build_live_context()
            if live:
                parts.append(live)

        # 3. Log context nếu hỏi về lỗi cụ thể
        if cls["needs_log"]:
            log_ctx = self._build_log_context()
            if log_ctx:
                parts.append(log_ctx)

        full_ctx = "\n\n".join(parts)

        # Trim nếu quá dài
        if len(full_ctx) > self.MAX_CTX_LEN * 3:
            full_ctx = full_ctx[:self.MAX_CTX_LEN * 3] + "\n...(truncated)"

        return full_ctx

    async def _build_live_context(self) -> str:
        """Thu thập trạng thái thực tế từ tất cả components."""
        sections = []

        # ── 1. Skills Registry ────────────────────────────────────
        if self._skill_db:
            try:
                skills_section = self._build_skills_section()
                if skills_section:
                    sections.append(skills_section)
            except Exception:
                pass

        # ── 2. Recent Errors ──────────────────────────────────────
        if self._failure_memory:
            try:
                err_section = self._build_errors_section()
                if err_section:
                    sections.append(err_section)
            except Exception:
                pass

        # ── 3. Workflow Library ───────────────────────────────────
        if self._workflow_lib:
            try:
                wf_section = self._build_workflow_section()
                if wf_section:
                    sections.append(wf_section)
            except Exception:
                pass

        # ── 4. Resource State ─────────────────────────────────────
        if self._resource_guard:
            try:
                res_section = self._build_resource_section()
                if res_section:
                    sections.append(res_section)
            except Exception:
                pass

        # ── 5. Recent Tasks ───────────────────────────────────────
        if self._task_history:
            try:
                task_section = self._build_task_section()
                if task_section:
                    sections.append(task_section)
            except Exception:
                pass

        # ── 6. Intelligence Stats ─────────────────────────────────
        if self._intelligence:
            try:
                intel_section = self._build_intelligence_section()
                if intel_section:
                    sections.append(intel_section)
            except Exception:
                pass

        if not sections:
            return ""

        return "# TRẠNG THÁI HỆ THỐNG HIỆN TẠI\n\n" + "\n\n".join(sections)

    def _build_skills_section(self) -> str:
        db = self._skill_db
        stats = db.stats()
        active_skills = db.list_active()

        # Sort: promoted (reliability=0.99) first, then by usage
        promoted = [s for s in active_skills if s.reliability_score >= 0.99]
        regular  = sorted(
            [s for s in active_skills if s.reliability_score < 0.99],
            key=lambda s: s.usage_count, reverse=True
        )[:10]

        lines = ["## Skills Registry"]
        lines.append(f"Tổng: {stats['active_skills']} skills | "
                     f"Reliability trung bình: {stats['avg_reliability']:.0%} | "
                     f"Tổng lần dùng: {stats['total_usage']}")

        if promoted:
            lines.append(f"\n🏆 PROMOTED ({len(promoted)} skills — reliability=99%):")
            for s in promoted[:5]:
                lines.append(
                    f"  - {s.name} | dùng {s.usage_count}x | "
                    f"rate={s.success_rate:.0%} | source={s.source}"
                )

        if regular:
            lines.append(f"\n📚 Active Skills (top {len(regular)} theo usage):")
            for s in regular[:8]:
                lines.append(
                    f"  - {s.name} | dùng {s.usage_count}x | "
                    f"rate={s.success_rate:.0%} | tags={','.join(s.capability_tags[:3])}"
                )

        by_source = stats.get("by_source", {})
        if by_source:
            lines.append(f"\nTheo nguồn: " + " | ".join(
                f"{k}={v}" for k, v in by_source.items()
            ))

        return "\n".join(lines)

    def _build_errors_section(self) -> str:
        fm = self._failure_memory
        stats = fm.stats()

        lines = ["## Lỗi Gần Đây (Failure Memory)"]
        lines.append(f"Tổng records: {stats['total_records']} | "
                     f"Error types: {stats['error_types']}")

        top_errors = stats.get("top_errors", [])
        if top_errors:
            lines.append("\nLỗi phổ biến nhất:")
            for etype, count in top_errors[:5]:
                # Get fix strategy if available
                fix = fm.find_fix(etype)
                fix_str = f" → fix: {fix.get('strategy','?')[:50]}" if fix else ""
                lines.append(f"  - {etype} ({count} lần){fix_str}")
        else:
            lines.append("  Chưa có lỗi được ghi nhận")

        return "\n".join(lines)

    def _build_workflow_section(self) -> str:
        wl = self._workflow_lib
        stats = wl.stats()
        templates = wl.list_templates()

        lines = ["## Workflow Library"]
        lines.append(f"Templates: {stats['workflow_templates']} | "
                     f"Plan cache: {stats['plan_cache_entries']} entries")

        top_wf = stats.get("top_workflows", [])
        if top_wf:
            lines.append("\nWorkflows được dùng nhiều nhất:")
            for wf in top_wf[:5]:
                lines.append(
                    f"  - {wf['name']} | score={wf['score']} | "
                    f"reliability={wf['reliability']:.0%} | uses={wf['uses']}"
                )

        top_cache = stats.get("top_cached_goals", [])
        if top_cache:
            lines.append("\nPlan cache hay được reuse:")
            for goal in top_cache[:3]:
                lines.append(f"  - \"{goal}\"")

        return "\n".join(lines)

    def _build_resource_section(self) -> str:
        rg = self._resource_guard
        try:
            snap = rg.current
            lines = ["## Resource State"]
            lines.append(
                f"RAM: {snap.ram_percent:.0f}% "
                f"({snap.ram_used_mb:.0f}MB / {snap.ram_total_mb:.0f}MB)"
            )
            lines.append(f"CPU: {snap.cpu_percent:.0f}%")
            if snap.is_warning:
                lines.append("⚠️ ĐANG Ở MỨC CẢNH BÁO")
            if snap.is_overloaded:
                lines.append("🔴 ĐANG QUÁ TẢI")
            return "\n".join(lines)
        except Exception:
            return ""

    def _build_task_section(self) -> str:
        recent = list(self._task_history)[-5:][::-1]  # 5 tasks mới nhất
        if not recent:
            return ""

        lines = ["## 5 Task Gần Nhất"]
        for t in recent:
            icon    = "✅" if t.get("success") else "❌"
            goal    = t.get("goal", "?")[:60]
            steps   = t.get("steps", 0)
            error   = t.get("error", "")[:60]
            started = t.get("started_at", "")[:16]
            status  = t.get("status", "?")

            line = f"  {icon} [{started}] {goal} ({steps} bước)"
            if error:
                line += f" — lỗi: {error}"
            lines.append(line)

        return "\n".join(lines)

    def _build_intelligence_section(self) -> str:
        intel = self._intelligence
        try:
            stats = intel.stats()
            lines = ["## Skill Intelligence"]
            lines.append(f"Enabled: {stats.get('enabled', False)}")
            promoter = stats.get("promoter", {})
            if promoter:
                lines.append(
                    f"AutoPromoter: {promoter.get('promoted_skills', 0)} promoted | "
                    f"tracking {promoter.get('tracked_skills', 0)} skills"
                )
                promoted_names = promoter.get("promoted_names", [])
                if promoted_names:
                    lines.append(f"  Promoted: {', '.join(promoted_names[:5])}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _build_log_context(self) -> str:
        """Đọc log file gần nhất."""
        log_paths = [
            self.LOG_PATH,
            "logs/agent.log",
            "/tmp/phidipus.log",
        ]
        for lp in log_paths:
            try:
                p = Path(lp)
                if not p.exists():
                    continue
                lines = p.read_text("utf-8", errors="replace").splitlines()
                recent = lines[-self.LOG_LINES:]
                if recent:
                    return (
                        f"## Log Gần Nhất ({lp}, {len(recent)} dòng cuối)\n"
                        + "\n".join(recent)
                    )
            except Exception:
                continue
        return ""


# ══════════════════════════════════════════════════════════════════
# Gemini Escalation
# ══════════════════════════════════════════════════════════════════

async def call_gemini_for_chat(
    question: str,
    context: str,
    llm_fallback: Any,
    history: list[dict],
) -> str:
    """
    Gọi Gemini API (qua LLMFallbackStack) cho câu hỏi phức tạp.
    Dùng Gemini 2.5 Flash — nhanh, hiểu tiếng Việt tốt, free tier ổn.
    """
    if not llm_fallback:
        return ""

    # Build conversation for Gemini
    history_str = ""
    if history:
        recent = history[-4:]  # 4 lượt gần nhất
        pairs = []
        for msg in recent:
            role = "Người dùng" if msg["role"] == "user" else "Trợ lý"
            pairs.append(f"{role}: {msg['content'][:200]}")
        history_str = "\n".join(pairs)

    prompt = f"""{context}

---
{f'Lịch sử hội thoại gần đây:{chr(10)}{history_str}{chr(10)}{chr(10)}---{chr(10)}' if history_str else ''}
Câu hỏi hiện tại: {question}

Hãy trả lời chi tiết, chính xác, bằng tiếng Việt.
Nếu đề xuất thay đổi code, hãy chỉ rõ file và dòng cần sửa.
Tối đa 400 từ trừ khi cần giải thích dài hơn."""

    try:
        result = await asyncio.wait_for(
            llm_fallback.call(prompt, preferred_provider="gemini-2.5-flash"),
            timeout=30,
        )
        if isinstance(result, str):
            return result.strip()
        if hasattr(result, "text"):
            return result.text.strip()
        return str(result).strip()
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════════════════════════

_instance: ChatbotContext | None = None

def get_chatbot_context() -> ChatbotContext:
    global _instance
    if _instance is None:
        _instance = ChatbotContext()
    return _instance
