# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/universal_parser.py — Phidipus v1.36
═══════════════════════════════════════════════════════════════════════

Universal Task Parser — Thay thế parse_goal() regex cứng.

Vấn đề cũ:
  parse_goal() dùng regex tìm 'tạo ảnh', 'đăng facebook'...
  → chỉ hiểu 1 loại task, task khác bị ignore hoặc gửi sang SkillForge

Giải pháp:
  LLM phân tích bất kỳ câu lệnh nào → IntentResult có đầy đủ thông tin
  để SmartRouter quyết định Fast Lane hay Explorer Lane.

Tier 1 — Rule-based (<5ms, no LLM):
  Pattern matching nhanh cho các task phổ biến đã biết.
  Nếu match → trả IntentResult luôn, không gọi LLM.

Tier 2 — LLM parse (~2s, Ollama qwen3:8b text):
  Gọi local model phân tích intent, complexity, app, steps.
  Trả JSON chuẩn để SmartRouter dùng.

Tier 3 — Fallback (0ms):
  Nếu LLM fail → trả IntentResult với lane='explorer'
  để AgentLoop tự xử lý bằng ReAct.

IntentResult fields:
  intent_type:    "social_post" | "file_op" | "web_task" | "email" |
                  "terminal" | "calendar" | "search" | "general" | "unknown"
  primary_app:    App chính cần dùng: "chrome", "finder", "terminal",
                  "mail", "calendar", "slack", "vscode", "notes", "system"
  apps_needed:    List tất cả apps cần (có thể multi-app)
  steps:          List bước ước tính (từ LLM)
  complexity:     "trivial" | "simple" | "medium" | "complex"
  lane:           "fast" | "explorer" | "clarify"
  workflow_skill: Nếu match workflow cứng (vd "workflow_spider_social_post")
  requires_confirm: True nếu task không thể undo (send email, delete, post)
  language:       "vi" | "en" | "mixed"
  raw_goal:       Goal gốc của user

Dùng bởi:
  core/smart_router.py → route() dùng IntentResult để quyết định lane
  planner/task_decomposer.py → thay thế _try_template() rule-based
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# IntentResult dataclass
# ══════════════════════════════════════════════════════════════════

@dataclass
class IntentResult:
    """Kết quả phân tích intent từ user goal."""

    # Core classification
    intent_type:      str  = "general"     # loại task
    primary_app:      str  = "chrome"      # app chính
    apps_needed:      list = field(default_factory=list)  # tất cả apps
    steps:            list = field(default_factory=list)  # bước ước tính
    complexity:       str  = "simple"      # trivial/simple/medium/complex

    # Routing
    lane:             str  = "explorer"    # fast/explorer/clarify
    workflow_skill:   str  = ""            # hardcoded workflow nếu có
    requires_confirm: bool = False         # task không thể undo

    # Meta
    language:         str  = "vi"          # vi/en/mixed
    raw_goal:         str  = ""
    parse_tier:       str  = "fallback"    # rule/llm/fallback
    parse_ms:         int  = 0

    @property
    def is_fast_lane(self) -> bool:
        return self.lane == "fast" and bool(self.workflow_skill)

    @property
    def needs_clarification(self) -> bool:
        return self.lane == "clarify"

    def to_dict(self) -> dict:
        return {
            "intent_type": self.intent_type,
            "primary_app": self.primary_app,
            "apps_needed": self.apps_needed,
            "steps":       self.steps,
            "complexity":  self.complexity,
            "lane":        self.lane,
            "workflow_skill": self.workflow_skill,
            "requires_confirm": self.requires_confirm,
            "language":    self.language,
            "parse_tier":  self.parse_tier,
            "parse_ms":    self.parse_ms,
        }


# ══════════════════════════════════════════════════════════════════
# Tier 1 — Rule-based fast patterns
# ══════════════════════════════════════════════════════════════════

@dataclass
class _RulePattern:
    pattern:          str
    intent_type:      str
    primary_app:      str
    apps_needed:      list
    complexity:       str
    lane:             str
    workflow_skill:   str  = ""
    requires_confirm: bool = False


_RULES: list[_RulePattern] = [
    # ── Social post (bee workflow) ────────────────────────────────
    _RulePattern(
        pattern=(
            r"(?=.*(?:chatgpt|chat gpt))"
            r"(?=.*(?:tạo ảnh|vẽ ảnh|ảnh|image|hình))"
            r"(?=.*(?:gemini|content|nội dung|viết))"
            r"(?=.*(?:facebook|fb|instagram|đăng|post))"
            r".*"
        ),
        intent_type="social_post",
        primary_app="chrome",
        apps_needed=["chrome", "chatgpt", "facebook"],
        complexity="complex",
        lane="fast",
        workflow_skill="workflow_spider_social_post",
        requires_confirm=True,
    ),
    # Generic social post (image + post)
    _RulePattern(
        pattern=r"(?=.*(?:tạo ảnh|tạo hình|generate image|ảnh ai))(?=.*(?:đăng|post|facebook|instagram|fb)).*",
        intent_type="social_post",
        primary_app="chrome",
        apps_needed=["chrome", "facebook"],
        complexity="complex",
        lane="fast",
        workflow_skill="workflow_spider_social_post",
        requires_confirm=True,
    ),
    # Just post to facebook/instagram (no image gen)
    _RulePattern(
        pattern=r"(?:đăng|post|share)\s+(?:lên|onto|to)?\s*(?:facebook|fb|instagram|insta|ig|x\.com|twitter)",
        intent_type="social_post",
        primary_app="chrome",
        apps_needed=["chrome"],
        complexity="simple",
        lane="explorer",
        requires_confirm=True,
    ),

    # ── File operations ───────────────────────────────────────────
    # FIX v9.39: lane=fast + workflow_skill
    # FIX v1.0: thêm tạo thư mục, nén, list, xem danh sách
    _RulePattern(
        pattern=r"(?:tạo|create|mkdir|make)\s+(?:thư mục|folder|directory|dir)\b",
        intent_type="file_op",
        primary_app="finder",
        apps_needed=["finder"],
        complexity="trivial",
        lane="fast",
        workflow_skill="workflow_finder_task",
    ),
    _RulePattern(
        pattern=r"(?:nén|compress|giải nén|unzip|extract)\s+(?:file|folder|thư mục|\S+\.zip|\S+\.tar)",
        intent_type="file_op",
        primary_app="finder",
        apps_needed=["finder"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_finder_task",
    ),
    _RulePattern(
        pattern=r"(?:xem|list|liệt kê|ls)\s+(?:file|folder|thư mục|danh sách file|danh sách thư mục)",
        intent_type="file_op",
        primary_app="finder",
        apps_needed=["finder"],
        complexity="trivial",
        lane="fast",
        workflow_skill="workflow_finder_task",
    ),
    _RulePattern(
        pattern=r"(?:copy|sao chép|chép|di chuyển|move|xóa|delete|đổi tên|rename)\s+(?:file|folder|thư mục|ảnh|image|tài liệu|document|pdf|jpg|jpeg|png|zip|doc|docx|xlsx|mp4|mp3|tất cả|\*\.\w+)",
        intent_type="file_op",
        primary_app="finder",
        apps_needed=["finder"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_finder_task",
    ),
    _RulePattern(
        pattern=r"(?:tìm|search|find)\s+(?:file|\*\.\w+|pdf|jpg|doc)",
        intent_type="file_op",
        primary_app="finder",
        apps_needed=["finder"],
        complexity="trivial",
        lane="fast",
        workflow_skill="workflow_finder_task",
    ),
    _RulePattern(
        pattern=r"(?:mở|open)\s+(?:file|folder|thư mục|ảnh|tài liệu)",
        intent_type="file_op",
        primary_app="finder",
        apps_needed=["finder"],
        complexity="trivial",
        lane="fast",
        workflow_skill="workflow_finder_task",
    ),

    # ── Terminal / code ───────────────────────────────────────────
    # FIX v9.39: lane=fast + workflow_skill
    _RulePattern(
        pattern=r"(?:chạy|run|thực thi|execute)\s+(?:lệnh|command|script|code|python|node|npm|git)",
        intent_type="terminal",
        primary_app="terminal",
        apps_needed=["terminal"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_terminal_task",
    ),
    _RulePattern(
        pattern=r"(?:git\s+(?:commit|push|pull|clone|status|log|diff|branch|merge|checkout)|npm\s+\w+|pip\s+install|pip3\s+install)",
        intent_type="terminal",
        primary_app="terminal",
        apps_needed=["terminal"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_terminal_task",
    ),

    # ── Web navigation ────────────────────────────────────────────
    # FIX v9.39: lane=fast + workflow_skill
    _RulePattern(
        pattern=r"(?:vào|mở|go to|navigate to|open)\s+(?:trang|website|web|url|https?://)",
        intent_type="web_task",
        primary_app="chrome",
        apps_needed=["chrome"],
        complexity="trivial",
        lane="fast",
        workflow_skill="workflow_chrome_task",
    ),
    _RulePattern(
        pattern=r"(?:search|tìm kiếm|google|bing)\s+(?:về|about|for|trên google|on google)?\s*\S",
        intent_type="web_task",
        primary_app="chrome",
        apps_needed=["chrome"],
        complexity="trivial",
        lane="fast",
        workflow_skill="workflow_chrome_task",
    ),
    # FIX v1.0: "lấy/đọc nội dung trang web" + URL patterns
    _RulePattern(
        pattern=r"(?:lấy|đọc|get|read|xem|extract)\s+(?:nội dung|content|text|bài viết|tin)\s+(?:trang web|trang|web|website|page|từ|trên|của)?",
        intent_type="web_task",
        primary_app="chrome",
        apps_needed=["chrome"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_chrome_task",
    ),
    # FIX v1.0: bất kỳ câu lệnh nào chứa URL trực tiếp → web_task fast
    _RulePattern(
        pattern=r"https?://\S+",
        intent_type="web_task",
        primary_app="chrome",
        apps_needed=["chrome"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_chrome_task",
    ),

    # ── Email ─────────────────────────────────────────────────────
    # FIX v1.0: lane=fast — GmailSkills đã có
    _RulePattern(
        pattern=r"(?:gửi|send|compose|soạn)\s+(?:email|mail|thư)",
        intent_type="email",
        primary_app="mail",
        apps_needed=["chrome", "gmail"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_gmail_task",
        requires_confirm=True,
    ),
    _RulePattern(
        pattern=r"(?:đọc|check|kiểm tra)\s+(?:email|mail|inbox|hộp thư)",
        intent_type="email",
        primary_app="mail",
        apps_needed=["chrome", "gmail"],
        complexity="trivial",
        lane="fast",
        workflow_skill="workflow_gmail_task",
    ),

    # ── Calendar ──────────────────────────────────────────────────
    _RulePattern(
        pattern=r"(?:đặt|tạo|add|create)\s+(?:lịch|reminder|meeting|sự kiện|event|nhắc nhở)",
        intent_type="calendar",
        primary_app="calendar",
        apps_needed=["calendar"],
        complexity="simple",
        lane="explorer",
        requires_confirm=False,
    ),
    _RulePattern(
        pattern=r"(?:xem|check)\s+(?:lịch|calendar|schedule|kế hoạch)",
        intent_type="calendar",
        primary_app="calendar",
        apps_needed=["calendar"],
        complexity="trivial",
        lane="explorer",
    ),

    # ── Messenger / Chat platforms — FIX v1.0 ────────────────────
    _RulePattern(
        pattern=(
            r"(?:gửi|send|nhắn|tin nhắn|message)\s+"
            r"(?:.*\s+)?(?:qua|trên|via|on)\s+"
            r"(?:messenger|facebook messenger|fb messenger|instagram|ig|zalo|wechat|telegram|tele)"
        ),
        intent_type="messenger",
        primary_app="chrome",
        apps_needed=["chrome"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_messenger_task",
        requires_confirm=True,
    ),
    _RulePattern(
        pattern=(
            r"(?:gửi|send|nhắn)\s+(?:messenger|zalo|instagram|wechat|telegram|tele)\s+"
            r"(?:cho|to)\s+\S+"
        ),
        intent_type="messenger",
        primary_app="chrome",
        apps_needed=["chrome"],
        complexity="simple",
        lane="fast",
        workflow_skill="workflow_messenger_task",
        requires_confirm=True,
    ),

    # ── Email — upgrade to fast lane ──────────────────────────────
    # (email rules already exist above but lane=explorer — keep for now)

    # ── Screenshot / screen ───────────────────────────────────────
    _RulePattern(
        pattern=r"(?:chụp|capture|screenshot|screen grab)\s+(?:màn hình|screen)?",
        intent_type="system",
        primary_app="system",
        apps_needed=["system"],
        complexity="trivial",
        lane="explorer",
    ),

    # ── Clipboard ─────────────────────────────────────────────────
    _RulePattern(
        pattern=r"(?:copy|sao chép)\s+(?:vào clipboard|to clipboard)\s+(.+)",
        intent_type="system",
        primary_app="system",
        apps_needed=["system"],
        complexity="trivial",
        lane="explorer",
    ),
]


def _intent_to_app(intent_type: str) -> str:
    """Map intent_type → primary app string."""
    return {
        "social_post": "chrome",
        "file_op":     "finder",
        "terminal":    "terminal",
        "web_task":    "chrome",
        "email":       "mail",
        "calendar":    "calendar",
        "messenger":   "chrome",
        "system":      "system",
    }.get(intent_type, "chrome")


def _rule_match(goal: str) -> IntentResult | None:
    """Tier 1: nhanh, không LLM."""
    t0 = time.time()
    gl = goal.lower().strip()

    for rule in _RULES:
        if re.search(rule.pattern, gl, re.IGNORECASE | re.DOTALL):
            lang = _detect_language(goal)
            return IntentResult(
                intent_type=rule.intent_type,
                primary_app=rule.primary_app,
                apps_needed=rule.apps_needed or [rule.primary_app],
                steps=[],
                complexity=rule.complexity,
                lane=rule.lane,
                workflow_skill=rule.workflow_skill,
                requires_confirm=rule.requires_confirm,
                language=lang,
                raw_goal=goal,
                parse_tier="rule",
                parse_ms=int((time.time() - t0) * 1000),
            )
    return None


def _detect_language(text: str) -> str:
    vi_chars = len(re.findall(
        r'[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]',
        text, re.IGNORECASE
    ))
    if vi_chars > 3:
        return "vi"
    if vi_chars > 0:
        return "mixed"
    return "en"


# ══════════════════════════════════════════════════════════════════
# LLM Parse Prompt
# ══════════════════════════════════════════════════════════════════

_LLM_PARSE_PROMPT = """\
You are an AI task parser. Analyze this user request and return ONLY a JSON object.

User request: "{goal}"

Classify the task and return JSON:
{{
  "intent_type": "<one of: social_post|file_op|web_task|email|terminal|calendar|search|screenshot|system|general|unknown>",
  "primary_app": "<main app: chrome|finder|terminal|mail|calendar|slack|vscode|notes|system|unknown>",
  "apps_needed": ["<list of apps needed>"],
  "steps": ["<step 1>", "<step 2>", "..."],
  "complexity": "<trivial|simple|medium|complex>",
  "requires_confirm": <true if irreversible: send email, post, delete, purchase>,
  "language": "<vi|en|mixed>",
  "clarification_needed": "<empty string OR what to ask user if task is ambiguous>"
}}

Rules:
- steps: max 6 steps, each under 10 words
- complexity trivial = 1 action, simple = 2-3 steps, medium = 4-6 steps, complex = 6+ or needs multiple apps
- requires_confirm = true for: send email, post social media, delete files, make purchases, send messages
- If the request is too ambiguous to execute, set clarification_needed
- Respond with JSON only, no markdown, no explanation"""


# ══════════════════════════════════════════════════════════════════
# UniversalParser
# ══════════════════════════════════════════════════════════════════

class UniversalParser:
    """
    Phân tích intent từ bất kỳ câu lệnh nào.

    Tier 1:   Rule matching (<5ms)          — 20+ regex patterns hardcoded
    Tier 1.5: qwen3:4b LLM (~500ms)       — hiểu typo, đảo từ, tự nhiên (v1.0)
    Tier 2:   Semantic Router (<50ms)       — nomic-embed-text cosine sim v9.39
    Tier 3:   LLM parse (~2s, qwen3:8b)    — Ollama text model heavy fallback
    Tier 4:   Fallback (0ms)               → lane=explorer
    """

    def __init__(self, llm_client=None):
        self._llm = llm_client
        self._semantic = None
        self._semantic_init_tried = False

    def _get_semantic_router(self):
        if self._semantic_init_tried:
            return self._semantic
        self._semantic_init_tried = True
        try:
            from core.semantic_router import SemanticRouter
            self._semantic = SemanticRouter()
        except Exception as exc:
            _log.debug("SemanticRouter unavailable: %s", exc)
        return self._semantic

    async def parse(self, goal: str) -> IntentResult:
        if not goal or not goal.strip():
            return IntentResult(lane="clarify", raw_goal=goal, parse_tier="fallback")
        t0 = time.time()

        # Tier 1: Rule-based (<5ms)
        rule_result = _rule_match(goal)
        if rule_result:
            rule_result.raw_goal = goal
            _vlog("⚡", f"Parse [rule] '{goal[:50]}' → "
                  f"intent={rule_result.intent_type} lane={rule_result.lane} ({rule_result.parse_ms}ms)")
            return rule_result

        # Tier 1.5: qwen3:4b LLM (~300-800ms) — FIX v1.0
        # Gọi TRƯỚC semantic router vì qwen3 hiểu typo/đảo từ tốt hơn cosine sim
        try:
            from core.llm_intent_parser import get_intent_parser, intent_to_finder_path
            parser = get_intent_parser()
            if parser._enabled:
                gi = await asyncio.wait_for(parser.parse(goal), timeout=3.0)
                if gi and gi.action != "unknown" and gi.confidence >= 0.5:
                    # Map LLMIntent → IntentResult
                    _WORKFLOW_MAP = {
                        "file_op":      "workflow_finder_task",
                        "terminal":     "workflow_terminal_task",
                        "web_task":     "workflow_chrome_task",
                        "email":        "workflow_gmail_task",
                        "messenger":    "workflow_messenger_task",
                        "social_post":  "workflow_spider_social_post",
                    }
                    itype = gi.intent_type
                    wf = _WORKFLOW_MAP.get(itype, "")
                    lane = "fast" if wf else "explorer"
                    ms = int((time.time() - t0) * 1000)

                    result = IntentResult(
                        intent_type=itype,
                        primary_app=_intent_to_app(itype),
                        apps_needed=[_intent_to_app(itype)],
                        complexity="simple",
                        lane=lane,
                        workflow_skill=wf,
                        requires_confirm=gi.action in ("delete", "send_email", "send_message", "social_post"),
                        language=_detect_language(goal),
                        raw_goal=goal,
                        parse_tier=f"qwen3({gi.confidence:.2f})",
                        parse_ms=ms,
                    )
                    # Inject LLMIntent vào result cho downstream use
                    result._llm_intent = gi  # type: ignore[attr-defined]

                    cache_note = " [cache]" if gi.from_cache else ""
                    _vlog("🧠", f"Parse [qwen3:4b{cache_note}] '{goal[:50]}' → "
                          f"intent={itype} action={gi.action} lane={lane} "
                          f"conf={gi.confidence:.2f} ({ms}ms)")
                    return result
        except asyncio.TimeoutError:
            _vlog("⏱️", "qwen3:4b timeout → Semantic fallback")
        except ImportError:
            pass  # qwen3 parser not installed
        except Exception as exc:
            _vlog("⚠️", f"qwen3:4b error: {str(exc)[:50]} → Semantic fallback")

        # Tier 2: Semantic Router (<50ms)
        sr = self._get_semantic_router()
        if sr is not None:
            try:
                decision = await asyncio.wait_for(sr.route(goal), timeout=2.0)
                if decision is not None:
                    lang = _detect_language(goal)
                    result = IntentResult(
                        intent_type=decision.intent_type,
                        primary_app=_intent_to_app(decision.intent_type),
                        apps_needed=[_intent_to_app(decision.intent_type)],
                        complexity="simple",
                        lane=decision.lane,
                        workflow_skill=decision.workflow_skill,
                        requires_confirm=decision.requires_confirm,
                        language=lang,
                        raw_goal=goal,
                        parse_tier=f"semantic({decision.similarity:.2f})",
                        parse_ms=int((time.time()-t0)*1000),
                    )
                    _vlog("🎯", f"Parse [semantic] '{goal[:50]}' → "
                          f"intent={result.intent_type} lane={result.lane} "
                          f"sim={decision.similarity:.2f} ({result.parse_ms}ms)")
                    return result
            except asyncio.TimeoutError:
                _vlog("⏱️", "SemanticRouter timeout → LLM fallback")
            except Exception as exc:
                _log.debug("SemanticRouter error: %s", exc)

        # Tier 3: LLM parse (~2s)
        if self._llm:
            try:
                llm_result = await asyncio.wait_for(self._llm_parse(goal, t0), timeout=15.0)
                if llm_result:
                    _vlog("🧠", f"Parse [llm] '{goal[:50]}' → "
                          f"intent={llm_result.intent_type} lane={llm_result.lane} ({llm_result.parse_ms}ms)")
                    return llm_result
            except asyncio.TimeoutError:
                _vlog("⏱️", "LLM timeout → fallback")
            except Exception as exc:
                _log.debug("LLM parse error: %s", exc)

        # Tier 4: Fallback
        lang = _detect_language(goal)
        result = IntentResult(
            intent_type="general", primary_app="chrome", apps_needed=["chrome"],
            complexity="simple", lane="explorer", language=lang, raw_goal=goal,
            parse_tier="fallback", parse_ms=int((time.time()-t0)*1000),
        )
        _vlog("⚪", f"Parse [fallback] '{goal[:50]}' → explorer")
        return result


    async def _llm_parse(self, goal: str, t0: float) -> IntentResult | None:
        """Gọi LLM để parse intent."""
        prompt = _LLM_PARSE_PROMPT.format(goal=goal[:300])

        try:
            # Thử Gemini trước (nhanh hơn), fallback Ollama
            raw = await self._llm.chat(
                prompt,
                system_prompt="You are a precise JSON-only task classifier.",
                temperature=0.1,
                max_tokens=400,
            )
        except Exception:
            return None

        # Parse JSON response
        text = raw.content if hasattr(raw, "content") else str(raw)
        text = re.sub(r"```(?:json)?|```", "", text).strip()

        try:
            d = json.loads(text)
        except Exception:
            m = re.search(r"\{[^{}]+\}", text, re.DOTALL)
            if m:
                try:
                    d = json.loads(m.group())
                except Exception:
                    return None
            else:
                return None

        # Map clarification_needed → lane='clarify'
        clarify = str(d.get("clarification_needed", "")).strip()
        lane = "clarify" if clarify else "explorer"

        # Check if matches a known workflow
        workflow_skill = ""
        intent = str(d.get("intent_type", "general"))
        if intent == "social_post":
            workflow_skill = "workflow_spider_social_post"
            lane = "fast"

        ms = int((time.time() - t0) * 1000)
        return IntentResult(
            intent_type=intent,
            primary_app=str(d.get("primary_app", "chrome")),
            apps_needed=list(d.get("apps_needed", ["chrome"])),
            steps=list(d.get("steps", [])),
            complexity=str(d.get("complexity", "simple")),
            lane=lane,
            workflow_skill=workflow_skill,
            requires_confirm=bool(d.get("requires_confirm", False)),
            language=str(d.get("language", "vi")),
            raw_goal=goal,
            parse_tier="llm",
            parse_ms=ms,
        )
