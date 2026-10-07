# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/llm_intent_parser.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

Tier 1.5: qwen3:4b Local LLM Intent Parser (~300-800ms)

Vị trí trong pipeline:
  Tier 0: Cache         (0ms)     — goal đã parse trước đó
  Tier 1: Rules         (<5ms)    — 20+ regex patterns
  ★ Tier 1.5: qwen3:4b (~500ms)  — LLM nhẹ, hiểu typo, đảo từ, tự nhiên
  Tier 2: Semantic      (<50ms)   — nomic-embed-text cosine sim
  Tier 3: qwen3:8b      (~2s)     — LLM nặng fallback
  Tier 4: Fallback      (0ms)     → explorer lane

Tại sao qwen3:4b?
  - 4B params → ~2.5GB RAM, fit Mac 16GB cùng qwen3:8b
  - Ollama serve sẵn, không cần setup thêm
  - Hiểu tiếng Việt tốt (Google multilingual)
  - Xử lý typo, đảo từ, informal speech
  - ~300-500ms trên Apple Silicon — đủ nhanh cho UX tốt

Prompt Design:
  System: JSON-only task classifier, Vietnamese + English
  User: goal string
  Output: {"action":"find","target":"pdf","location":"downloads",...}

Cache:
  LRU in-memory 200 entries — goal hash → parsed result
  Same goal → 0ms (no LLM call)

Usage:
  parser = IntentParser()
  intent = await parser.parse("trong download có bao nhiêu file pdf")
  # → LLMIntent(action="find", target="pdf", location="downloads", ...)

Integration:
  - core/universal_parser.py → Tier 1.5
  - skills/apps/finder_skills.py → fallback khi decomposer return unknown
  - skills/apps/chrome_skills.py → disambiguate web tasks
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.request
import urllib.error
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# LLMIntent — structured result from LLM parse
# ══════════════════════════════════════════════════════════════════

@dataclass
class LLMIntent:
    """Structured intent parsed by qwen3:4b."""
    action:       str = "unknown"    # find|list|copy|move|delete|create|open|search|navigate|...
    target:       str = ""           # "pdf", "ảnh", "email", "trang web", ...
    file_type:    str = ""           # "pdf", "jpg", "xlsx", ... (normalized extension)
    location:     str = ""           # "downloads", "desktop", ... (folder name, not full path)
    destination:  str = ""           # for copy/move
    url:          str = ""           # for web tasks
    query:        str = ""           # search query
    recipient:    str = ""           # for email/messenger
    platform:     str = ""           # "gmail", "messenger", "zalo", ...
    subject:      str = ""           # email subject
    body:         str = ""           # email/message body
    raw_goal:     str = ""
    confidence:   float = 0.0
    parse_ms:     int = 0
    from_cache:   bool = False

    # ── Mapping sang Phidipus intent types ────────────────────────
    @property
    def intent_type(self) -> str:
        """Map action → Phidipus intent_type."""
        _MAP = {
            "find": "file_op", "list": "file_op", "copy": "file_op",
            "move": "file_op", "delete": "file_op", "create_folder": "file_op",
            "compress": "file_op", "rename": "file_op", "open_file": "file_op",
            "navigate": "web_task", "search_web": "web_task",
            "get_content": "web_task", "screenshot_web": "web_task",
            "send_email": "email", "read_email": "email", "search_email": "email",
            "send_message": "messenger",
            "git": "terminal", "npm": "terminal", "run_command": "terminal",
            "terminal": "terminal",
            "social_post": "social_post",
        }
        return _MAP.get(self.action, "general")


# ══════════════════════════════════════════════════════════════════
# LRU Cache
# ══════════════════════════════════════════════════════════════════

class _LRUCache:
    """Simple LRU cache with max size."""
    def __init__(self, max_size: int = 200):
        self._cache: OrderedDict[str, LLMIntent] = OrderedDict()
        self._max = max_size

    def get(self, key: str) -> LLMIntent | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, value: LLMIntent) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    @property
    def size(self) -> int:
        return len(self._cache)


# ══════════════════════════════════════════════════════════════════
# Prompt Template
# ══════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """Bạn là Agent PC chuyên nghiệp trên macOS.
Phân tích mọi cách nói tiếng Việt (slang, viết tắt, gián tiếp, đảo từ, sai chính tả, cảm thán) và tiếng Anh thành JSON actions sạch 100%.
Không bao giờ giải thích, không thêm chữ nào ngoài JSON.

Classify into JSON with these fields:
- "action": one of: find, list, copy, move, delete, create_folder, compress, rename, open_file, navigate, search_web, get_content, screenshot_web, send_email, read_email, search_email, send_message, git, npm, run_command, terminal, social_post, unknown
- "target": what the user wants (e.g. "pdf files", "images", "email")
- "file_type": file extension if mentioned (e.g. "pdf", "jpg", "xlsx"), empty if none
- "location": folder name (e.g. "downloads", "desktop", "documents"), empty if none
- "destination": target folder for copy/move, empty if none
- "url": URL if mentioned, empty if none
- "query": search query if searching, empty if none
- "recipient": person name or email for messaging/email, empty if none
- "platform": messaging platform (gmail, messenger, zalo, wechat, telegram, instagram), empty if none
- "subject": email subject if mentioned, empty if none
- "body": message body if mentioned, empty if none
- "confidence": 0.0-1.0

Luôn trả đúng format JSON. Không markdown, không giải thích."""

_USER_TEMPLATE = """Classify this command: "{goal}"
JSON:"""

# ══════════════════════════════════════════════════════════════════
# Custom Ollama Modelfile — bake system prompt vào model
# ══════════════════════════════════════════════════════════════════
# Khi dùng custom model: không cần gửi system prompt mỗi call
# → nhanh hơn ~30%, JSON output ổn định hơn
# ══════════════════════════════════════════════════════════════════

_CUSTOM_MODEL_NAME = "phidipus-intent"
_BASE_MODEL = "qwen3:4b-instruct"

_MODELFILE_CONTENT = '''FROM qwen3:4b-instruct

SYSTEM """Bạn là Agent PC chuyên nghiệp trên macOS.
Phân tích mọi cách nói tiếng Việt (slang, viết tắt, gián tiếp, đảo từ, sai chính tả, cảm thán) và tiếng Anh thành JSON actions sạch 100%.
Không bao giờ giải thích, không thêm chữ nào ngoài JSON.
Luôn trả đúng format JSON và confidence.

Actions: find, list, copy, move, delete, create_folder, compress, rename, open_file, navigate, search_web, get_content, screenshot_web, send_email, read_email, search_email, send_message, git, npm, run_command, terminal, social_post, unknown

Output format:
{"action":"...","target":"...","file_type":"...","location":"...","destination":"...","url":"...","query":"...","recipient":"...","platform":"...","subject":"...","body":"...","confidence":0.9}"""

PARAMETER num_ctx 4096
PARAMETER temperature 0.0
PARAMETER top_p 0.95
PARAMETER num_predict 300
PARAMETER stop <|im_end|>
PARAMETER stop </s>
'''


def _resolve_base_model(requested: str = "") -> str:
    """v4.3: the configured/installed 'fast' model (was hard-coded qwen3:4b-instruct)."""
    try:
        from core.model_registry import get_model
        return get_model("fast", requested or _BASE_MODEL)
    except Exception:
        return requested or _BASE_MODEL


_CUSTOM_PARAMS = {"num_ctx": 4096, "temperature": 0.0, "top_p": 0.95,
                  "num_predict": 300, "stop": ["<|im_end|>", "</s>"]}


async def _ensure_custom_model(base_url: str, timeout: float = 60.0, base_model: str = "") -> bool:
    """
    Tạo model phidipus-intent từ qwen3:4b-instruct nếu chưa có.
    System prompt baked in → mỗi call nhanh hơn 30%.
    Chỉ chạy 1 lần (Ollama lưu model vĩnh viễn).

    Returns True nếu custom model sẵn sàng.
    """
    base = base_url.rstrip("/")

    # Check if custom model already exists
    try:
        req = urllib.request.Request(f"{base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            models = [m.get("name", "") for m in data.get("models", [])]
            if any(_CUSTOM_MODEL_NAME in m for m in models):
                _vlog("🕷️", f"Model {_CUSTOM_MODEL_NAME} đã có sẵn ✓")
                return True
    except Exception:
        return False

    # Check base model exists (exact tag; "qwen3" prefix matching picked
    # unrelated models before)
    base_model = base_model or _resolve_base_model()
    norm = base_model if ":" in base_model else f"{base_model}:latest"
    if norm not in models and base_model not in models:
        _vlog("⚠️", f"Base model {base_model} chưa có — dùng trực tiếp (không tạo custom)")
        return False

    # Create custom model via Ollama API.  v4.3: Ollama ≥ 0.5 dropped the
    # "modelfile" field — use {model, from, system, parameters}; fall back to
    # the legacy body for old servers.
    _vlog("🔨", f"Đang tạo model {_CUSTOM_MODEL_NAME} từ {base_model} (lần đầu, ~5-10s)...")
    try:
        bodies = [
            {"model": _CUSTOM_MODEL_NAME, "from": base_model, "system": _SYSTEM_PROMPT,
             "parameters": _CUSTOM_PARAMS, "stream": False},
            {"name": _CUSTOM_MODEL_NAME, "stream": False,
             "modelfile": _MODELFILE_CONTENT.replace("FROM qwen3:4b-instruct", f"FROM {base_model}", 1)},
        ]

        def _call():
            last_exc: Exception | None = None
            for body in bodies:
                req = urllib.request.Request(
                    f"{base}/api/create",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return resp.read()
                except urllib.error.HTTPError as exc:
                    last_exc = exc
                    continue
            raise last_exc or RuntimeError("create failed")

        await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout + 5)

        _vlog("✅", f"Model {_CUSTOM_MODEL_NAME} đã tạo thành công! "
              f"(baked system prompt + temp=0.0 + Vietnamese NLP)")
        return True

    except Exception as exc:
        _vlog("⚠️", f"Tạo model {_CUSTOM_MODEL_NAME} thất bại: {str(exc)[:60]}")
        _vlog("💡", f"Dùng {base_model} trực tiếp (chậm hơn ~30%)")
        return False


# ══════════════════════════════════════════════════════════════════
# IntentParser
# ══════════════════════════════════════════════════════════════════

class IntentParser:
    """
    Parse intent dùng qwen3:4b-instruct qua Ollama API.
    FIX v1.0: Tự tạo model phidipus-intent (baked system prompt) nếu chưa có.

    Ưu tiên: phidipus-intent (custom, nhanh 30%) → qwen3:4b-instruct (fallback)
    """

    def __init__(
        self,
        model: str = "qwen3:4b-instruct",
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 5.0,
        enabled: bool = True,
    ) -> None:
        self._base_model = model
        self._model = model  # may switch to phidipus-intent
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._enabled = enabled
        self._loaded = False
        self._custom_ready = False
        self._setup_done = False
        self._cache = _LRUCache(200)
        self._call_count = 0
        self._total_ms = 0
        self._errors = 0

    @property
    def available(self) -> bool:
        """phidipus-intent or the configured 'fast' model is installed."""
        if not self._enabled:
            return False
        try:
            req = urllib.request.Request(
                f"{self._base_url}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                models = {m.get("name", "") for m in data.get("models", [])}
            if any(m.split(":")[0] == _CUSTOM_MODEL_NAME for m in models):
                return True
            self._base_model = _resolve_base_model(self._base_model)
            if not self._custom_ready:
                self._model = self._base_model
            norm = self._base_model if ":" in self._base_model else f"{self._base_model}:latest"
            return norm in models or self._base_model in models
        except Exception:
            return False

    @property
    def model_name(self) -> str:
        return self._model

    async def _setup_custom_model(self) -> None:
        """One-time setup: tạo phidipus-intent nếu chưa có."""
        if self._setup_done:
            return
        self._setup_done = True
        try:
            self._base_model = _resolve_base_model(self._base_model)
            ok = await _ensure_custom_model(self._base_url, base_model=self._base_model)
            if ok:
                self._model = _CUSTOM_MODEL_NAME
                self._custom_ready = True
            else:
                self._model = self._base_model
        except Exception:
            self._model = self._base_model

    async def parse(self, goal: str) -> LLMIntent | None:
        """
        Parse goal → LLMIntent.

        Returns None if:
          - Parser disabled
          - Ollama unavailable
          - LLM timeout
          - JSON parse error
        """
        if not self._enabled or not goal or not goal.strip():
            return None

        t0 = time.time()

        # ── Cache check ───────────────────────────────────────────
        cache_key = hashlib.md5(goal.strip().lower().encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached.from_cache = True
            cached.parse_ms = 0
            return cached

        # ── Auto-setup custom model (one-time) ───────────────────
        if not self._setup_done:
            await self._setup_custom_model()

        # ── Auto-load model nếu chưa loaded ──────────────────────
        if not self._loaded:
            loaded = await self.load_model()
            if not loaded:
                return None

        # ── Call Ollama ──────────────────────────────────────────
        # Nếu dùng phidipus-intent: KHÔNG gửi system prompt (đã bake in)
        # Nếu dùng qwen3:4b-instruct: gửi system prompt bình thường
        try:
            api_payload: dict = {
                "model": self._model,
                "prompt": _USER_TEMPLATE.format(goal=goal[:300]),
                "stream": False,
                "keep_alive": "5m",
                "think": False,   # v4.3: hybrid models (qwen3.5) would spend the budget thinking
            }
            # Custom model đã bake system prompt + params → không gửi lại
            if self._custom_ready and self._model == _CUSTOM_MODEL_NAME:
                pass  # system, temperature, top_p đã trong Modelfile
            else:
                api_payload["system"] = _SYSTEM_PROMPT
                api_payload["options"] = {
                    "temperature": 0.0,
                    "num_predict": 300,
                    "top_p": 0.95,
                }

            payload = json.dumps(api_payload).encode("utf-8")

            req = urllib.request.Request(
                f"{self._base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            # Run in thread to not block event loop
            def _call():
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode())

            response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=self._timeout + 1)

            raw_text = re.sub(r"<think>.*?</think>", "", response.get("response", ""), flags=re.S)

        except (asyncio.TimeoutError, urllib.error.URLError, Exception) as exc:
            self._errors += 1
            _vlog("⚠️", f"{self._model} error: {str(exc)[:60]}")
            return None

        # ── Parse JSON from response ──────────────────────────────
        intent = self._parse_json(raw_text, goal)
        if intent is None:
            self._errors += 1
            return None

        ms = int((time.time() - t0) * 1000)
        intent.parse_ms = ms
        intent.raw_goal = goal

        # ── Cache + stats ─────────────────────────────────────────
        self._cache.put(cache_key, intent)
        self._call_count += 1
        self._total_ms += ms
        _vlog("🧠", f"qwen3:4b: action={intent.action} target={intent.target} "
              f"type={intent.file_type} loc={intent.location} ({ms}ms)")

        return intent

    def _parse_json(self, text: str, goal: str) -> LLMIntent | None:
        """Extract JSON from LLM response text."""
        # Strip markdown code blocks
        text = re.sub(r"```(?:json)?|```", "", text).strip()

        # Try direct parse
        try:
            d = json.loads(text)
        except json.JSONDecodeError:
            # Try finding JSON object in text
            m = re.search(r"\{[^{}]+\}", text, re.DOTALL)
            if m:
                try:
                    d = json.loads(m.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        if not isinstance(d, dict):
            return None

        return LLMIntent(
            action=str(d.get("action", "unknown")).lower().strip(),
            target=str(d.get("target", "")),
            file_type=str(d.get("file_type", "")).lower().strip().lstrip("."),
            location=str(d.get("location", "")).lower().strip(),
            destination=str(d.get("destination", "")).lower().strip(),
            url=str(d.get("url", "")),
            query=str(d.get("query", "")),
            recipient=str(d.get("recipient", "")),
            platform=str(d.get("platform", "")).lower().strip(),
            subject=str(d.get("subject", "")),
            body=str(d.get("body", "")),
            confidence=float(d.get("confidence", 0.5)),
        )

    def stats(self) -> dict:
        """Parser statistics."""
        avg = self._total_ms / max(self._call_count, 1)
        return {
            "model": self._model,
            "base_model": self._base_model,
            "custom_model": self._custom_ready,
            "enabled": self._enabled,
            "loaded": self._loaded,
            "calls": self._call_count,
            "errors": self._errors,
            "cache_size": self._cache.size,
            "avg_ms": round(avg),
            "total_ms": self._total_ms,
        }

    # ══════════════════════════════════════════════════════════════
    # VRAM Management — load/unload on demand
    # ══════════════════════════════════════════════════════════════

    async def load_model(self) -> bool:
        """
        Load qwen3:4b vào VRAM trước khi parse.
        Gọi Ollama /api/generate với prompt ngắn + keep_alive=5m.
        Trả True nếu load thành công.
        """
        if not self._enabled:
            return False
        if self._loaded:
            return True

        t0 = time.time()
        try:
            payload = json.dumps({
                "model": self._model,
                "prompt": "hi",
                "stream": False,
                "keep_alive": "5m",
                "options": {"num_predict": 1},
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self._base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            def _call():
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()

            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _call),
                timeout=30,
            )

            self._loaded = True
            ms = int((time.time() - t0) * 1000)
            _vlog("🕷️", f"qwen3:4b loaded vào VRAM ({ms}ms)")
            return True

        except Exception as exc:
            _vlog("⚠️", f"qwen3:4b load failed: {str(exc)[:60]}")
            return False

    async def unload_model(self) -> bool:
        """
        Unload qwen3:4b khỏi VRAM sau khi task xong.
        Gọi Ollama /api/generate với keep_alive=0 → giải phóng VRAM ngay.
        """
        if not self._loaded:
            return True

        try:
            payload = json.dumps({
                "model": self._model,
                "prompt": "",
                "keep_alive": 0,
                "stream": False,
                "options": {"num_predict": 1},
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self._base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            def _call():
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.read()

            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _call),
                timeout=6,
            )

            self._loaded = False
            _vlog("🕷️", "qwen3:4b unloaded khỏi VRAM ✓")
            return True

        except Exception as exc:
            _vlog("⚠️", f"qwen3:4b unload error: {str(exc)[:60]}")
            self._loaded = False  # mark unloaded anyway
            return False


# ══════════════════════════════════════════════════════════════════
# Singleton — dùng chung trong toàn bộ Phidipus
# ══════════════════════════════════════════════════════════════════

_PARSER: IntentParser | None = None


def get_intent_parser(
    model: str = "qwen3:4b-instruct",
    base_url: str = "http://127.0.0.1:11434",
    enabled: bool = True,
) -> IntentParser:
    """Get or create singleton IntentParser."""
    global _PARSER
    if _PARSER is None:
        _PARSER = IntentParser(model=model, base_url=base_url, enabled=enabled)
    return _PARSER


# ══════════════════════════════════════════════════════════════════
# Helper: Convert LLMIntent → FinderSkills-compatible fields
# ══════════════════════════════════════════════════════════════════

_LOCATION_MAP = {
    "downloads": "~/Downloads",
    "download":  "~/Downloads",
    "desktop":   "~/Desktop",
    "documents": "~/Documents",
    "document":  "~/Documents",
    "pictures":  "~/Pictures",
    "picture":   "~/Pictures",
    "photos":    "~/Pictures",
    "movies":    "~/Movies",
    "music":     "~/Music",
    "home":      "~",
}


def intent_to_finder_path(location: str) -> str:
    """Convert LLM location string to macOS path."""
    loc = location.lower().strip().rstrip("/")
    return _LOCATION_MAP.get(loc, f"~/{location}" if location else "~/Desktop")


def intent_to_file_filter(file_type: str) -> str:
    """Convert LLM file_type to glob pattern."""
    if not file_type:
        return "*"
    ft = file_type.lower().strip().lstrip(".")
    # Handle app names
    _APP_MAP = {
        "excel": "xlsx", "word": "docx", "powerpoint": "pptx",
        "ppt": "pptx", "photo": "jpg", "image": "jpg",
    }
    ft = _APP_MAP.get(ft, ft)
    return f"*.{ft}"
