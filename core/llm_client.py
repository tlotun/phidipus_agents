# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/llm_client.py — Phidipus v1.0
Async Ollama LLM client with mandatory goal and traceback sanitization.

Architecture contract (R-24 / R-25 / F-6):
  "Add goal sanitization: strip format-string chars, enforce
   max_length=2048 before every LLM call (F-6)."

  "R-24: All user-supplied goals MUST be sanitized before LLM
   interpolation: strip { } format characters, enforce max 2048
   characters, reject goals containing Python keywords at the CLI
   entry point."

  "R-25: All tracebacks interpolated into LLM mutation prompts MUST
   be sanitized: truncated to max 1024 characters, { } stripped,
   non-ASCII escaped."

LLMClient wraps the Ollama HTTP API with two levels of protection:

  1. Input sanitization — every goal string passed to chat() is
     sanitized via _sanitize_goal() (R-24) before the HTTP request
     is constructed.  The raw goal never touches the prompt template.

  2. Output validation — the response JSON is parsed by json_utils
     (no eval, no exec) and returned as a plain Python dict.

Sanitization is not optional.  If sanitize_goal() raises
GoalSanitizationError or GoalInjectionError, the LLM call is aborted
and the exception propagates to the caller.

Ollama API
----------
LLMClient communicates with the Ollama /api/chat endpoint using
asyncio + aiohttp (if available) or urllib.request in async wrapper.
Both paths avoid subprocess.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation library import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-24  Goal sanitized before every LLM call — strip {}, max 2048
        chars, Python keyword rejection.
  R-25  Traceback strings sanitized before any prompt construction —
        truncate 1024, strip {}, escape non-ASCII.

Used by:
  core/agent_loop.py         — chat() for reasoning loop
  planner/react_reasoner.py  — chat() for ReAct thought generation

Dependencies:
  utils/json_utils.py             — loads(), dumps()
  utils/logger.py                 — get_logger()
  config/config_loader.py         — PhidipusConfig (cfg.llm.*)

Note on sanitization:
  _sanitize_goal() and _sanitize_traceback() are defined in THIS module
  (not imported from evolution/mutation_generator.py).  Phase 4's
  mutation_generator.py must import them from here, or they can be
  extracted to utils/sanitizer.py at that time.
"""

from __future__ import annotations

import asyncio
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from config.config_loader import PhidipusConfig
from utils.json_utils import JsonDecodeError, dumps, loads
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GOAL_MAX_LENGTH:      int = 2048   # R-24
_TRACEBACK_MAX_LENGTH: int = 1024   # R-25

# Python keywords forbidden in user-supplied goals (R-24)
_PYTHON_INJECTION_KEYWORDS: frozenset[str] = frozenset({
    "import", "exec", "eval", "__import__", "subprocess",
    "os.system", "open(", "compile(", "globals(", "locals(",
    "__builtins__",
})

# FIX v4.3: the keyword set above used to be matched as plain substrings, so
# ordinary goals such as "file important", "evaluate KPI" or "execute
# workflow" were rejected as "Python injection".  Detection now targets
# real code constructs only (call syntax, dunder access, import statements).
import re as _re_inj

_PYTHON_INJECTION_PATTERNS: tuple[tuple[str, "_re_inj.Pattern[str]"], ...] = (
    ("__import__",   _re_inj.compile(r"__import__\s*\(", _re_inj.I)),
    ("__builtins__", _re_inj.compile(r"__builtins__", _re_inj.I)),
    ("exec",         _re_inj.compile(r"\bexec\s*\(", _re_inj.I)),
    ("eval",         _re_inj.compile(r"\beval\s*\(", _re_inj.I)),
    ("compile(",     _re_inj.compile(r"\bcompile\s*\(", _re_inj.I)),
    ("globals(",     _re_inj.compile(r"\bglobals\s*\(\s*\)", _re_inj.I)),
    ("locals(",      _re_inj.compile(r"\blocals\s*\(\s*\)", _re_inj.I)),
    ("open(",        _re_inj.compile(r"\bopen\s*\(\s*['\"/~]", _re_inj.I)),
    ("os.system",    _re_inj.compile(r"\bos\s*\.\s*(?:system|popen|exec\w*|spawn\w*)\s*\(", _re_inj.I)),
    ("subprocess",   _re_inj.compile(r"\bsubprocess\s*\.\s*\w+", _re_inj.I)),
    # Python import statements: "import os", "from os import system"
    ("import",       _re_inj.compile(
        r"(?:^|[;\n])\s*(?:from\s+[A-Za-z_][\w.]*\s+import\s+[\w*]|import\s+"
        r"(?:os|sys|subprocess|socket|shutil|ctypes|pickle|marshal|builtins|importlib|pty)\b)",
        _re_inj.I)),
)


def _find_injection_keyword(text: str) -> str:
    """Return the matched injection construct name, or '' when clean."""
    for name, pattern in _PYTHON_INJECTION_PATTERNS:
        if pattern.search(text):
            return name
    return ""


_THINK_RE = _re_inj.compile(r"<think>.*?</think>", _re_inj.S | _re_inj.I)


def _strip_think(text: str) -> str:
    """Remove <think>…</think> reasoning blocks emitted by thinking models."""
    if not text or "<think" not in text.lower():
        return text
    cleaned = _THINK_RE.sub("", text)
    # Unclosed <think> (truncated output): drop everything up to the tag end
    if "<think>" in cleaned.lower():
        cleaned = cleaned.split("<think>", 1)[0]
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Sanitization helpers (R-24, R-25)
#
# NOTE: These functions are intentionally defined here (core/llm_client.py)
# rather than imported from evolution/mutation_generator.py.
# Reason: mutation_generator.py is a Phase-4 module.  Having a Phase-3
# module depend on a Phase-4 module breaks the build order.
# When mutation_generator.py is built in Phase 4, it should import these
# same helpers from here — or they can be extracted to utils/sanitizer.py
# at that time.
# ---------------------------------------------------------------------------

class GoalSanitizationError(ValueError):
    """
    Raised when a user-supplied goal fails sanitization (R-24).

    Attributes:
        reason: Short machine-readable reason code.
        field:  The field that failed ("goal").
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "SANITIZATION_ERROR",
        field:  str = "goal",
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.field  = field

    def __str__(self) -> str:
        return f"[{self.reason}] field={self.field!r} {super().__str__()}"


class GoalInjectionError(GoalSanitizationError):
    """
    Raised when a user-supplied goal contains Python injection keywords (R-24).

    Attributes:
        keyword: The offending keyword found in the goal.
    """

    def __init__(self, message: str, *, keyword: str = "") -> None:
        super().__init__(message, reason="INJECTION_DETECTED", field="goal")
        self.keyword = keyword

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} keyword={self.keyword!r}" if self.keyword else base


def _sanitize_goal(raw_goal: str, max_length: int) -> tuple[str, bool]:
    """
    Sanitize a user-supplied goal string (R-24).

    Operations (in order):
      1. Strip leading/trailing whitespace.
      2. Remove { and } format-string characters.
      3. Enforce max_length — truncate if exceeded (was_truncated=True).
      4. Reject empty goals.
      5. Scan for Python injection keywords → raise GoalInjectionError.

    Returns:
        (sanitized_goal, was_truncated)

    Raises:
        GoalSanitizationError: if goal is empty after sanitization.
        GoalInjectionError:    if goal contains a forbidden keyword.
    """
    cleaned = raw_goal.strip()
    # Strip format-string injection characters
    cleaned = cleaned.replace("{", "").replace("}", "")

    was_truncated = len(cleaned) > max_length
    if was_truncated:
        cleaned = cleaned[:max_length]

    if not cleaned:
        raise GoalSanitizationError(
            "Goal is empty after sanitization.",
            reason="EMPTY_GOAL",
        )

    kw = _find_injection_keyword(cleaned)
    if kw:
        raise GoalInjectionError(
            f"Goal contains forbidden code construct: {kw!r}",
            keyword=kw,
        )

    return cleaned, was_truncated


def _sanitize_traceback(raw_tb: str, max_length: int) -> tuple[str, bool]:
    """
    Sanitize a traceback string before LLM prompt injection (R-25).

    Operations:
      1. Truncate to max_length characters.
      2. Strip { and } characters.
      3. Escape non-ASCII bytes to \\xNN.

    Returns:
        (sanitized_tb, was_truncated)
    """
    was_truncated = len(raw_tb) > max_length
    cleaned = raw_tb[:max_length]
    cleaned = cleaned.replace("{", "").replace("}", "")
    # Escape non-ASCII
    cleaned = cleaned.encode("ascii", errors="backslashreplace").decode("ascii")
    return cleaned, was_truncated


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """
    Raised when an LLM API call fails.

    Attributes:
        model:  Model name that was called.
        reason: Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        model:  str = "",
        reason: str = "LLM_ERROR",
    ) -> None:
        super().__init__(message)
        self.model  = model
        self.reason = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.model:
            parts.append(f"model={self.model!r}")
        return " ".join(parts) + f" {base}"


class LLMTimeoutError(LLMError):
    """Raised when the Ollama API does not respond within the timeout."""

    def __init__(self, message: str, *, model: str = "") -> None:
        super().__init__(message, model=model, reason="LLM_TIMEOUT")


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMResponse:
    """
    Immutable record of a successful LLM API response.

    Attributes:
        content:     Text content from the model's reply.
        model:       Model name echoed from the response.
        input_tokens:  Approximate input token count (if reported).
        output_tokens: Approximate output token count (if reported).
    """

    content:       str
    model:         str
    input_tokens:  int = 0
    output_tokens: int = 0
    # v4.3: native function calling + separated reasoning (Ollama tools / think)
    tool_calls:    tuple = ()      # ({"name": str, "arguments": dict}, …)
    thinking:      str = ""


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class LLMClient:
    """
    Async Ollama LLM client with mandatory input sanitization.

    Every call to chat() sanitizes the goal (R-24) before constructing
    the HTTP request.  The sanitized goal is passed as a separate user
    message — never interpolated into a template string.

    Usage::

        client = LLMClient(cfg)

        # Basic reasoning call (goal is sanitized automatically):
        response = await client.chat(
            goal="Open the browser and navigate to example.com",
            system_prompt="You are a helpful task planner.",
        )
        print(response.content)

        # With context messages (ReAct history):
        response = await client.chat(
            goal="click the submit button",
            messages=[
                {"role": "assistant", "content": "Thought: I see a form."},
            ],
        )

    Args:
        cfg: Validated PhidipusConfig.  LLM parameters read from cfg.llm.*.
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._base_url  = cfg.llm.base_url.rstrip("/")
        self._model     = cfg.llm.reasoning_model
        self._coder     = cfg.llm.coder_model
        self._vlm       = cfg.llm.vlm_model
        self._max_tokens = cfg.llm.max_tokens
        self._temperature = cfg.llm.temperature
        self._timeout    = cfg.llm.request_timeout_seconds
        self._goal_max   = cfg.agent.goal_max_length   # R-24
        # Phase 1: retry with exponential backoff
        self._retry_max  = cfg.llm.retry_max_attempts
        self._retry_delay = cfg.llm.retry_base_delay

        # System prompt mặc định cho reasoning model.
        # DeepSeek-R1 có xu hướng "overthink" — prompt này giữ output
        # ngắn gọn và tập trung vào hành động cụ thể.
        self._default_system_prompt = cfg.llm.default_system_prompt

        _log.info(
            "LLMClient initialised",
            extra={
                "base_url":    self._base_url,
                "model":       self._model,
                "coder":       self._coder,
                "max_tokens":  self._max_tokens,
                "temperature": self._temperature,
                "timeout":     self._timeout,
                "retry_max":   self._retry_max,
                "retry_delay": self._retry_delay,
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def model_for(self, role: str = "reasoning") -> str:
        """Model that chat() would use for *role* (installed one when possible)."""
        return self._resolve(self._coder if role == "coder" else None, role=role)

    @staticmethod
    def _think_param(model: str, think: bool | None) -> Any:
        """Value for Ollama's "think" field, or None to omit it.

        v4.3: current models (Qwen3.5/3.6/3.8, Gemma 4, LFM2.5, gpt-oss) think by
        default — slow, and the hidden reasoning can eat the whole num_predict
        budget.  Thinking is OFF unless the caller (or config models.think) asks
        for it, and the field is only sent to models that support it.
        """
        try:
            from core.model_registry import get_registry
            reg = get_registry()
            if think is None:
                think = reg.think_default()
            if not reg.supports(model, "thinking"):
                return None
        except Exception:
            return None
        if model.startswith("gpt-oss"):
            return "medium" if think else "low"   # gpt-oss cannot disable reasoning
        return bool(think)

    def _resolve(self, model: str | None, role: str = "reasoning") -> str:
        """Pick an installed model: requested → configured → registry fallback."""
        try:
            from core.model_registry import resolve_model
            return resolve_model(model or self._model, role=role)
        except Exception:
            return model or self._model

    async def chat(
        self,
        goal: str,
        *,
        system_prompt: str = "",
        messages: list[dict[str, str]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool | None = None,
        fmt: Any = None,
    ) -> LLMResponse:
        """
        Send a sanitized goal to the LLM and return the response.

        Processing pipeline:
          1. Sanitize goal via _sanitize_goal() (R-24) — strips {}/},
             enforces max_length, rejects Python keywords.
          2. Build messages list: [system, ...history, user(sanitized_goal)].
          3. POST to Ollama /api/chat via asyncio.to_thread.
          4. Parse response JSON via json_utils.loads().
          5. Return LLMResponse.

        Args:
            goal:          Raw goal string (will be sanitized, R-24).
            system_prompt: Optional system message prepended to context.
            messages:      Optional prior conversation history.
            model:         Override model name (defaults to reasoning_model).

        Returns:
            LLMResponse with content and token counts.

        Raises:
            GoalSanitizationError: if goal is empty or invalid after sanitization.
            GoalInjectionError:    if goal contains Python injection keywords.
            LLMTimeoutError:       if the API does not respond in time.
            LLMError:              on any other API failure.
        """
        # ── R-24: sanitize goal ───────────────────────────────────────────
        sanitized_goal, was_truncated = _sanitize_goal(goal, self._goal_max)
        if was_truncated:
            _log.warning(
                "R-24: goal truncated before LLM call",
                extra={"original_len": len(goal), "max": self._goal_max},
            )

        # ── Build message list ────────────────────────────────────────────
        msg_list: list[dict[str, str]] = []
        # Dùng system_prompt truyền vào, hoặc fallback sang default
        # (quan trọng với DeepSeek-R1 để tránh overthink)
        _sys_prompt = system_prompt or self._default_system_prompt
        if _sys_prompt:
            msg_list.append({"role": "system", "content": _sys_prompt})
        if messages:
            msg_list.extend(messages)
        msg_list.append({"role": "user", "content": sanitized_goal})

        chosen_model = self._resolve(model, role="coder" if model and model == self._coder else "reasoning")
        return await self._call_with_retry(
            msg_list, model=chosen_model,
            temperature=temperature, max_tokens=max_tokens, think=think, fmt=fmt,
        )

    async def chat_tools(
        self,
        goal: str,
        *,
        tools: list[dict[str, Any]],
        system_prompt: str = "",
        messages: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool | None = None,
    ) -> LLMResponse:
        """v4.3: native function calling (Ollama ``tools``).

        Same R-24 goal sanitisation as chat(); the response carries
        ``tool_calls`` ({"name", "arguments"}) when the model chose a tool.
        """
        sanitized_goal, _ = _sanitize_goal(goal, self._goal_max)
        msg_list: list[dict[str, Any]] = []
        if system_prompt:
            msg_list.append({"role": "system", "content": system_prompt})
        msg_list.append({"role": "user", "content": f"Goal: {sanitized_goal}"})
        if messages:
            msg_list.extend(messages)
        chosen_model = self._resolve(model)
        return await self._call_with_retry(
            msg_list, model=chosen_model, temperature=temperature,
            max_tokens=max_tokens, think=think, tools=tools,
        )

    async def chat_coder(
        self,
        goal: str,
        *,
        system_prompt: str = "",
        messages: list[dict[str, str]] | None = None,
    ) -> LLMResponse:
        """Convenience wrapper: chat() using the coder model."""
        return await self.chat(
            goal,
            system_prompt=system_prompt,
            messages=messages,
            model=self._coder,
        )

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 200,
        temperature: float = 0.0,
    ) -> str:
        """
        Simple text completion — trả về string thay vì LLMResponse.
        Dùng cho các task parse nhanh (profile numbers, intent classify...).

        FIX v4.3: max_tokens / temperature were silently ignored before.
        """
        resp = await self.chat(
            prompt,
            system_prompt="You are a precise JSON extractor. Return only valid JSON.",
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.content

    def sanitize_goal(self, raw_goal: str) -> str:
        """
        Sanitize *raw_goal* and return the cleaned string (R-24).

        Raises GoalSanitizationError or GoalInjectionError on bad input.
        """
        cleaned, _ = _sanitize_goal(raw_goal, self._goal_max)
        return cleaned

    def sanitize_traceback(self, raw_tb: str) -> str:
        """
        Sanitize *raw_tb* and return the cleaned string (R-25).

        Truncates to 1024 chars, strips {}, escapes non-ASCII.
        """
        cleaned, _ = _sanitize_traceback(raw_tb, _TRACEBACK_MAX_LENGTH)
        return cleaned

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _call_api(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool | None = None,
        fmt: Any = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """
        POST to Ollama /api/chat and return a parsed LLMResponse.

        Offloads the blocking urllib call to asyncio.to_thread so the
        event loop is not blocked during network I/O.
        """
        body: dict[str, Any] = {
            "model":    model,
            "messages": messages,
            "stream":   False,
            "options": {
                "num_predict": int(max_tokens or self._max_tokens),
                "temperature": float(self._temperature if temperature is None else temperature),
            },
        }
        think_value = await asyncio.to_thread(self._think_param, model, think)
        if think_value is not None:
            body["think"] = think_value
        if fmt is not None:
            body["format"] = fmt          # "json" or a JSON schema (structured output)
        if tools:
            body["tools"] = tools
        payload = dumps(body).encode("utf-8")

        url = f"{self._base_url}/api/chat"

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._http_post, url, payload, self._timeout),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(
                f"Ollama API did not respond within {self._timeout:.1f}s.",
                model=model,
            ) from exc

        # Parse response
        try:
            data = loads(raw)
        except JsonDecodeError as exc:
            raise LLMError(
                f"Ollama API returned non-JSON response: {exc}",
                model=model,
                reason="INVALID_RESPONSE",
            ) from exc

        if not isinstance(data, dict):
            raise LLMError(
                "Ollama API response is not a JSON object.",
                model=model,
                reason="INVALID_RESPONSE",
            )

        # Extract content
        try:
            content = data["message"].get("content") or ""
        except (KeyError, TypeError, AttributeError) as exc:
            raise LLMError(
                f"Unexpected Ollama response structure: {exc}. "
                f"Keys: {list(data.keys()) if isinstance(data, dict) else '?'}",
                model=model,
                reason="UNEXPECTED_STRUCTURE",
            ) from exc

        usage = data.get("usage", {}) or {}
        calls = []
        for tc in data["message"].get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = loads(args)
                except JsonDecodeError:
                    args = {}
            if fn.get("name"):
                calls.append({"name": str(fn["name"]), "arguments": args if isinstance(args, dict) else {}})
        return LLMResponse(
            content=_strip_think(str(content)),
            model=data.get("model", model),
            input_tokens=int(usage.get("prompt_tokens", data.get("prompt_eval_count", 0)) or 0),
            output_tokens=int(usage.get("completion_tokens", data.get("eval_count", 0)) or 0),
            tool_calls=tuple(calls),
            thinking=str(data["message"].get("thinking") or ""),
        )

    @staticmethod
    def _http_post(url: str, payload: bytes, timeout: float = 120.0) -> str:
        """
        Synchronous HTTP POST to *url* with JSON *payload*.

        Called via asyncio.to_thread — must not use await.
        FIX S6: timeout parameter prevents indefinite blocking at TCP level.

        Returns:
            Response body as a str.

        Raises:
            LLMError: on HTTP errors or connection failures.
        """
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise LLMError(
                f"Ollama HTTP {exc.code}: {exc.reason}",
                reason="HTTP_ERROR",
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                f"Ollama connection error: {exc.reason}",
                reason="CONNECTION_ERROR",
            ) from exc
        except OSError as exc:
            # FIX S6: socket.timeout is a subclass of OSError, NOT URLError.
            # Without this, socket timeouts from urlopen(timeout=N) propagate
            # as unhandled OSError instead of LLMError.
            raise LLMError(
                f"Ollama socket error: {exc}",
                reason="CONNECTION_ERROR",
            ) from exc

    # ------------------------------------------------------------------
    # Phase 1: Retry with exponential backoff
    # ------------------------------------------------------------------

    async def _call_with_retry(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **extra: Any,
    ) -> LLMResponse:
        """
        Call ``_call_api`` with exponential backoff retry on transient errors.

        Retries on:
          - ``LLMTimeoutError`` — Ollama did not respond in time.
          - ``LLMError`` with reason ``CONNECTION_ERROR`` — Ollama unreachable.

        Does NOT retry on:
          - ``GoalInjectionError`` / ``GoalSanitizationError`` — input errors.
          - ``LLMError`` with reason ``INVALID_RESPONSE`` — malformed response.
          - ``LLMError`` with reason ``HTTP_ERROR`` — 4xx/5xx from Ollama.

        Delays: base_delay × 2^attempt (e.g. 0.5s → 1.0s → 2.0s).

        Args:
            messages: Chat message list.
            model:    Ollama model name.

        Returns:
            LLMResponse on success.

        Raises:
            LLMTimeoutError: if all attempts timeout.
            LLMError:        if all attempts fail with connection errors,
                             or on non-retryable errors (immediate raise).
        """
        last_exc: LLMError | None = None

        for attempt in range(self._retry_max):
            try:
                return await self._call_api(
                    messages, model=model,
                    temperature=temperature, max_tokens=max_tokens, **extra,
                )
            except LLMTimeoutError as exc:
                last_exc = exc
            except LLMError as exc:
                if exc.reason == "CONNECTION_ERROR":
                    last_exc = exc
                else:
                    raise  # non-retryable (HTTP_ERROR, INVALID_RESPONSE, etc.)

            # Backoff before retry
            if attempt < self._retry_max - 1:
                delay = self._retry_delay * (2 ** attempt)
                _log.warning(
                    "LLMClient: retrying after transient error",
                    extra={
                        "attempt":      attempt + 1,
                        "max_attempts": self._retry_max,
                        "delay_s":      round(delay, 2),
                        "error":        str(last_exc),
                        "model":        model,
                    },
                )
                await asyncio.sleep(delay)

        # All attempts exhausted
        assert last_exc is not None
        _log.error(
            "LLMClient: all retry attempts exhausted",
            extra={
                "attempts": self._retry_max,
                "model":    model,
                "error":    str(last_exc),
            },
        )
        raise last_exc

    # ------------------------------------------------------------------
    # Phase 3: Streaming API
    # ------------------------------------------------------------------

    async def chat_streaming(
        self,
        goal: str,
        *,
        system_prompt: str = "",
        messages: list[dict[str, str]] | None = None,
        model: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Phase 3: stream tokens from the LLM one chunk at a time.

        Same sanitization as chat() (R-24), but yields partial text
        as it arrives instead of waiting for the full response.

        The caller can accumulate chunks and stop early once enough
        information has been received (e.g. early action detection).

        Yields:
            str: text chunk from the model.

        Raises:
            Same as chat().
        """
        sanitized_goal, was_truncated = _sanitize_goal(goal, self._goal_max)
        if was_truncated:
            _log.warning(
                "R-24: goal truncated before streaming LLM call",
                extra={"original_len": len(goal), "max": self._goal_max},
            )

        msg_list: list[dict[str, str]] = []
        if system_prompt:
            msg_list.append({"role": "system", "content": system_prompt})
        if messages:
            msg_list.extend(messages)
        msg_list.append({"role": "user", "content": sanitized_goal})

        chosen_model = self._resolve(model)

        async for chunk in self._call_api_streaming(msg_list, model=chosen_model):
            yield chunk

    async def _call_api_streaming(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
    ) -> AsyncGenerator[str, None]:
        """
        POST to Ollama /api/chat with stream=true.

        FIX H-03: Uses a threading.Event to signal the background reader
        to stop when the consumer breaks early (early action detection).
        FIX M-02: Queue has maxsize=100 to prevent unbounded growth.
        """
        body: dict[str, Any] = {
            "model":    model,
            "messages": messages,
            "stream":   True,
            "options": {
                "num_predict": self._max_tokens,
                "temperature": self._temperature,
            },
        }
        think_value = await asyncio.to_thread(self._think_param, model, None)
        if think_value is not None:
            body["think"] = think_value
        payload = dumps(body).encode("utf-8")

        url = f"{self._base_url}/api/chat"
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        import queue
        import threading

        # FIX M-02: bounded queue prevents unbounded memory growth on early break
        chunk_q: queue.Queue[str | None | Exception] = queue.Queue(maxsize=100)
        # FIX H-03: stop event signals reader thread to close connection
        stop_event = threading.Event()
        # FIX S6: capture timeout for closure — nested function cannot access self
        _stream_timeout = self._timeout

        def _stream_reader() -> None:
            """Run in a background thread — reads NDJSON lines."""
            try:
                # FIX S6: timeout prevents thread from blocking forever if Ollama hangs
                with urllib.request.urlopen(req, timeout=_stream_timeout) as resp:
                    for raw_line in resp:
                        # FIX H-03: check stop signal each iteration
                        if stop_event.is_set():
                            break
                        line = raw_line.decode("utf-8").strip()
                        if not line:
                            continue
                        try:
                            data = loads(line)
                        except JsonDecodeError:
                            continue
                        content = ""
                        if isinstance(data, dict):
                            msg = data.get("message")
                            if isinstance(msg, dict):
                                content = msg.get("content", "")
                        if content:
                            try:
                                chunk_q.put(content, timeout=1.0)
                            except queue.Full:
                                if stop_event.is_set():
                                    break
                        # Check if stream is done
                        if isinstance(data, dict) and data.get("done"):
                            break
            except Exception as exc:
                if not stop_event.is_set():
                    try:
                        chunk_q.put(exc, timeout=1.0)
                    except queue.Full:
                        pass
            finally:
                try:
                    chunk_q.put(None, timeout=1.0)  # sentinel
                except queue.Full:
                    pass

        reader_thread = threading.Thread(target=_stream_reader, daemon=True)
        reader_thread.start()

        # Yield chunks from the queue on the async event loop
        try:
            while True:
                try:
                    item = chunk_q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue

                if item is None:
                    break  # stream finished
                if isinstance(item, Exception):
                    raise LLMError(
                        f"Streaming error: {item}",
                        model=model,
                        reason="STREAM_ERROR",
                    )
                yield item
        finally:
            # FIX H-03: signal reader thread to stop on early break / exception
            stop_event.set()


# ══════════════════════════════════════════════════════════════
# Singleton accessor — for use by workflow_executor, openclaw, etc.
# ══════════════════════════════════════════════════════════════

_llm_singleton: "LLMClient | None" = None


def set_llm_client(client: "LLMClient") -> None:
    """Set the global LLM client instance (called by AgentLoop at startup)."""
    global _llm_singleton
    _llm_singleton = client


def get_llm_client() -> "LLMClient":
    """
    Get the global LLM client singleton.

    Must be called AFTER set_llm_client() is invoked by AgentLoop.
    Falls back to creating a minimal client if not set.
    """
    global _llm_singleton
    if _llm_singleton is not None:
        return _llm_singleton

    # Fallback: create minimal client from config.yaml
    # FIX v4.3: load_config() did not exist before → fallback always failed.
    try:
        from config.config_loader import load_config
        cfg = load_config()
        _llm_singleton = LLMClient(cfg)
        return _llm_singleton
    except Exception:
        pass

    raise RuntimeError(
        "LLM client not initialized. "
        "Ensure AgentLoop has started or call set_llm_client() first."
    )
