# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/llm_fallback.py — Phidipus LLM Provider Fallback Stack v9.20
══════════════════════════════════════════════════════════════════

Provider stack cho Skill Forge với smart fallback routing.

Stack thứ tự mặc định:
  P0 (primary)  : Gemini 2.0 Flash        — temperature 0.7
  P1 (fallback) : Gemini 1.5 Flash        — temperature 0.2
  P2 (fallback) : OpenRouter / Qwen3-Coder 480B Free — temperature 0.2
  P3 (fallback) : Ollama local (qwen2.5-coder:7b)    — temperature 0.2

Status-code routing rules (user spec):
  SAFETY block (finishReason=SAFETY / blocked)
      → SKIP tất cả Gemini, nhảy thẳng sang OpenRouter Qwen3-Coder-480B
  429 / timeout / 500
      → fallback theo thứ tự bình thường P0→P1→P2→P3

Failure Memory integration:
  Mỗi lần switch provider → record_switch(goal_hash, from, to, reason)
  get_recommended_provider(goal_hash) → provider đã thành công trước đó

Design (PRAGMATIC):
  - stdlib urllib.request — không cần httpx/aiohttp
  - asyncio.to_thread() cho blocking HTTP
  - Timeout per provider: 60s Gemini, 90s OpenRouter, 120s Ollama
  - Không retry infinite — mỗi provider thử đúng 1 lần trong stack
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Error classification
# ══════════════════════════════════════════════════════════════════

class SafetyBlockError(Exception):
    """Raised when provider blocks request due to safety policy."""

class RateLimitError(Exception):
    """Raised on HTTP 429 Too Many Requests."""

class ProviderTimeoutError(Exception):
    """Raised when provider times out."""

class ProviderServerError(Exception):
    """Raised on HTTP 5xx server errors."""


# ── v4.3: Gemini model ids ─────────────────────────────────────────────
# gemini-1.5-*, gemini-2.0-* and dated "-preview-MM-DD" ids have been retired
# (HTTP 404).  Retired/preview ids in config.yaml are mapped to a current one
# and a 404 at call time walks this list instead of failing the provider.
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_LITE_MODEL = "gemini-2.5-flash-lite"
GEMINI_FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-flash-latest",
                          "gemini-2.5-flash-lite", "gemini-flash-lite-latest")


def normalize_gemini_model(name: str, default: str = GEMINI_DEFAULT_MODEL) -> str:
    n = (name or "").strip()
    if (not n or "preview" in n or n.startswith(("gemini-1.", "gemini-2.0", "gemini-pro"))
            or not n.startswith("gemini")):
        return default
    return n


class ModelNotFoundError(Exception):
    """HTTP 404 — the model id does not exist (retired) for this API key."""


def _classify_http_error(exc: urllib.error.HTTPError) -> Exception:
    """Map HTTP error code to typed exception."""
    if exc.code == 429:
        return RateLimitError(f"429 Too Many Requests from provider")
    if exc.code >= 500:
        return ProviderServerError(f"HTTP {exc.code} Server Error")
    if exc.code == 404:
        return ModelNotFoundError("HTTP 404 model not found")
    return exc


# ══════════════════════════════════════════════════════════════════
# Provider definitions
# ══════════════════════════════════════════════════════════════════

@dataclass
class Provider:
    """Single LLM provider configuration."""
    name: str
    kind: str           # "gemini" | "mistral" | "cerebras" | "openrouter" | "ollama"
    model: str
    temperature: float
    timeout_s: int
    api_key: str = ""   # empty = use env or skip
    base_url: str = ""
    skip_on_safety: bool = False  # If True, skip this provider on SAFETY errors
    enabled: bool = True          # v9.21: Admin can disable per provider

    # v9.21: Health tracking (updated at runtime)
    last_check_ts: float = 0.0
    last_check_ok: bool = True
    consecutive_fails: int = 0
    total_calls: int = 0
    total_success: int = 0
    last_error: str = ""
    avg_latency_ms: float = 0.0


# Primary provider temperature: 0.7 (per user spec)
# All fallback providers: 0.2 (per user spec)

def build_provider_stack(
    gemini_api_key: str,
    gemini_model: str = GEMINI_DEFAULT_MODEL,   # v4.3: 2.0-flash was retired
    mistral_api_key: str = "",
    cerebras_api_key: str = "",
    openrouter_api_key: str = "",
    ollama_base_url: str = "http://127.0.0.1:11434",
    ollama_coder_model: str = "qwen2.5-coder:7b",
) -> list[Provider]:
    """
    Build the ordered Skill Forge provider stack.

    v9.21 Stack (tối ưu cho Mac Mini M4 16GB RAM):
    ═══════════════════════════════════════════════════════
    ALL cloud API — Mac Mini chỉ chạy Ollama local models nhỏ.

      P0: Gemini 2.0 Flash         — Primary (nhanh nhất, miễn phí)
      P1: Gemini 1.5 Flash         — Fallback Gemini (khi P0 rate-limited)
      P2: Mistral Codestral        — Fallback #1 (code chuyên dụng, free tier)
      P3: Cerebras Qwen3-Coder-32B — Fallback #2 (cực nhanh, ~2000 tok/s)
      P4: OpenRouter :free models  — Fallback #3 (Qwen3-Coder-480B free)
      P5: Ollama local coder       — Last resort (qwen2.5-coder:7b, 4.5GB)
    ═══════════════════════════════════════════════════════

    SAFETY routing:
      Gemini SAFETY block → skip ALL Gemini → jump to P2 Mistral
      Any provider 429/timeout/500 → next in stack

    Mac Mini 16GB RAM budget:
      - M1 qwen3:8b (reasoning)  ~5GB VRAM
      - M2 qwen2.5-coder:7b     ~4.5GB (shared with M1, Ollama hot-swaps)
      - M3 qwen3-vl:8b (VLM)    ~5GB (shared, loaded on demand)
      - M7 nomic-embed-text      ~137MB (always loaded)
      - Total active: ~5.5GB (1 main model + embedding)
      - P2-P4 = CLOUD API → 0 local VRAM
    """
    stack: list[Provider] = []
    gemini_model = normalize_gemini_model(gemini_model)
    try:  # v4.3: use an installed local coder model (registry) — was hard-coded
        from core.model_registry import resolve_model
        ollama_coder_model = resolve_model(ollama_coder_model, "coder")
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════
    # v9.21 FIX: LUÔN tạo TẤT CẢ providers, kể cả khi chưa có key.
    # Provider không có key → enabled=False → hiện trên Admin Panel
    # → user paste key → enabled=True → hoạt động ngay.
    #
    # TRƯỚC (bug): if gemini_api_key → tạo provider. Không có key
    #   → provider không tồn tại → Admin Panel không thể update key.
    # SAU (fix): luôn tạo, enabled=bool(key).
    # ═══════════════════════════════════════════════════════════

    # P0: Primary Gemini Flash — temperature 0.7
    stack.append(Provider(
        name="gemini-flash",
        kind="gemini",
        model=gemini_model,
        temperature=0.7,
        timeout_s=60,
        api_key=gemini_api_key,
        skip_on_safety=False,
        enabled=bool(gemini_api_key),
    ))
    # P1: Gemini Flash-Lite — temperature 0.2 (separate quota → rate-limit fallback)
    stack.append(Provider(
        name="gemini-flash-lite",
        kind="gemini",
        model=GEMINI_LITE_MODEL,
        temperature=0.2,
        timeout_s=60,
        api_key=gemini_api_key,
        skip_on_safety=True,
        enabled=bool(gemini_api_key),
    ))

    # P2: Mistral — fast, free tier available
    stack.append(Provider(
        name="mistral-small",
        kind="mistral",
        model="mistral-small-latest",
        temperature=0.2,
        timeout_s=60,
        api_key=mistral_api_key,
        base_url="https://api.mistral.ai/v1/chat/completions",
        skip_on_safety=False,
        enabled=bool(mistral_api_key),
    ))

    # P3: Cerebras — ultra-fast inference (~2000 tok/s), free tier
    stack.append(Provider(
        name="cerebras-llama3.1-8b",
        kind="cerebras",
        model="llama3.1-8b",
        temperature=0.2,
        timeout_s=45,
        api_key=cerebras_api_key,
        base_url="https://api.cerebras.ai/v1/chat/completions",
        skip_on_safety=False,
        enabled=bool(cerebras_api_key),
    ))

    # P4: OpenRouter :free — Qwen 3.6 Plus (free, 1M context, reasoning)
    # [FIX v4.2] Upgraded from qwen3-coder:free → qwen3.6-plus:free
    # No API key required for :free models (but recommended for higher limits)
    stack.append(Provider(
        name="openrouter-free",
        kind="openrouter",
        model="qwen/qwen3.6-plus:free",
        temperature=0.2,
        timeout_s=90,
        api_key=openrouter_api_key,
        base_url="https://openrouter.ai/api/v1/chat/completions",
        skip_on_safety=False,
    ))

    # P5: Ollama local — always available, no internet needed
    stack.append(Provider(
        name=f"ollama-{ollama_coder_model}",
        kind="ollama",
        model=ollama_coder_model,
        temperature=0.2,
        timeout_s=120,
        base_url=f"{ollama_base_url}/api/generate",
        skip_on_safety=False,
    ))

    return stack


# ══════════════════════════════════════════════════════════════════
# Per-provider HTTP callers
# ══════════════════════════════════════════════════════════════════

def _gemini_request(model: str, api_key: str, prompt: str, max_tokens: int,
                    temperature: float, timeout_s: int) -> dict:
    # v4.3: API key travels in the x-goog-api-key header, not the URL query
    # (URLs end up in logs, proxies and exception messages).
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gen_cfg: dict = {"maxOutputTokens": max_tokens, "temperature": temperature}
    if max_tokens <= 1024 and "flash" in model:
        # 2.5 Flash models "think" by default; with a small output budget the
        # thoughts can consume every token and the reply comes back empty.
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode())


def _call_gemini_sync(provider: Provider, prompt: str, max_tokens: int) -> str:
    """Synchronous Gemini API call. Raises SafetyBlockError on SAFETY.

    v4.3: a 404 (retired model id) tries the next id in GEMINI_FALLBACK_MODELS
    and remembers the one that works on the provider.
    """
    candidates = [provider.model] + [m for m in GEMINI_FALLBACK_MODELS if m != provider.model]
    result: dict = {}
    for model in candidates:
        try:
            result = _gemini_request(model, provider.api_key, prompt, max_tokens,
                                     provider.temperature, provider.timeout_s)
        except urllib.error.HTTPError as e:
            err = _classify_http_error(e)
            if isinstance(err, ModelNotFoundError):
                _vlog("🔁", f"Gemini model '{model}' not found (404) → thử id khác")
                continue
            raise err from e
        except TimeoutError:
            raise ProviderTimeoutError(f"{provider.name} timeout {provider.timeout_s}s")
        if model != provider.model:
            _vlog("✅", f"Gemini: dùng '{model}' thay cho '{provider.model}'")
            provider.model = model
        break
    else:
        raise ModelNotFoundError(f"No Gemini model available (tried {', '.join(candidates)})")

    # Check for SAFETY block
    candidates = result.get("candidates", [])
    if candidates:
        finish = candidates[0].get("finishReason", "")
        if finish == "SAFETY":
            raise SafetyBlockError(f"Gemini SAFETY block: {provider.model}")

    # Check promptFeedback block
    feedback = result.get("promptFeedback", {})
    if feedback.get("blockReason"):
        raise SafetyBlockError(
            f"Gemini blocked: {feedback.get('blockReason')}"
        )

    try:
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Gemini response parse error: {str(result)[:200]}") from e


def _call_openrouter_sync(provider: Provider, prompt: str, max_tokens: int) -> str:
    """Synchronous OpenRouter (OpenAI-compatible) API call."""
    if not provider.api_key:
        headers = {"Content-Type": "application/json",
                   "HTTP-Referer": "https://phidipus.local",
                   "X-Title": "Phidipus"}
    else:
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {provider.api_key}",
                   "HTTP-Referer": "https://phidipus.local",
                   "X-Title": "Phidipus"}

    body = json.dumps({
        "model": provider.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": provider.temperature,
    }).encode()

    req = urllib.request.Request(
        provider.base_url, data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=provider.timeout_s) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise _classify_http_error(e) from e
    except TimeoutError:
        raise ProviderTimeoutError(f"{provider.name} timeout {provider.timeout_s}s")

    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"OpenRouter response parse error: {str(result)[:200]}") from e


def _call_mistral_sync(provider: Provider, prompt: str, max_tokens: int) -> str:
    """
    Synchronous Mistral AI API call (OpenAI-compatible).
    Endpoint: https://api.mistral.ai/v1/chat/completions
    Free tier: Codestral, Mistral Small
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    body = json.dumps({
        "model": provider.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": provider.temperature,
    }).encode()

    req = urllib.request.Request(
        provider.base_url, data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=provider.timeout_s) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise _classify_http_error(e) from e
    except TimeoutError:
        raise ProviderTimeoutError(f"{provider.name} timeout {provider.timeout_s}s")

    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Mistral response parse error: {str(result)[:200]}") from e


def _call_cerebras_sync(provider: Provider, prompt: str, max_tokens: int) -> str:
    """
    Synchronous Cerebras API call (OpenAI-compatible).
    Endpoint: https://api.cerebras.ai/v1/chat/completions
    ~2000 tokens/sec inference. Free tier available.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    body = json.dumps({
        "model": provider.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": provider.temperature,
    }).encode()

    req = urllib.request.Request(
        provider.base_url, data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=provider.timeout_s) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise _classify_http_error(e) from e
    except TimeoutError:
        raise ProviderTimeoutError(f"{provider.name} timeout {provider.timeout_s}s")

    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Cerebras response parse error: {str(result)[:200]}") from e


def _call_ollama_sync(provider: Provider, prompt: str, max_tokens: int) -> str:
    """Synchronous Ollama API call (local model)."""
    body = json.dumps({
        "model": provider.model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": provider.temperature,
            "num_predict": max_tokens,
        },
    }).encode()

    req = urllib.request.Request(
        provider.base_url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=provider.timeout_s) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise _classify_http_error(e) from e
    except TimeoutError:
        raise ProviderTimeoutError(f"{provider.name} timeout {provider.timeout_s}s")

    try:
        return result["response"]
    except KeyError as e:
        raise RuntimeError(f"Ollama response parse error: {str(result)[:200]}") from e


def _call_provider_sync(provider: Provider, prompt: str, max_tokens: int) -> str:
    """Dispatch to correct provider caller."""
    if provider.kind == "gemini":
        return _call_gemini_sync(provider, prompt, max_tokens)
    elif provider.kind == "mistral":
        return _call_mistral_sync(provider, prompt, max_tokens)
    elif provider.kind == "cerebras":
        return _call_cerebras_sync(provider, prompt, max_tokens)
    elif provider.kind == "openrouter":
        return _call_openrouter_sync(provider, prompt, max_tokens)
    elif provider.kind == "ollama":
        return _call_ollama_sync(provider, prompt, max_tokens)
    elif provider.kind == "custom":
        return _call_custom_sync(provider, prompt, max_tokens)
    else:
        raise ValueError(f"Unknown provider kind: {provider.kind}")


def _call_custom_sync(provider: Provider, prompt: str, max_tokens: int) -> str:
    """
    Generic OpenAI-compatible API call.
    Works with: Groq, Together, Fireworks, DeepSeek, LMStudio, vLLM, etc.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    body = json.dumps({
        "model": provider.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": provider.temperature,
    }).encode()

    url = provider.base_url
    if not url.endswith("/chat/completions"):
        url = url.rstrip("/") + "/chat/completions"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=provider.timeout_s) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise _classify_http_error(e) from e
    except TimeoutError:
        raise ProviderTimeoutError(f"{provider.name} timeout {provider.timeout_s}s")

    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Custom provider response parse error: {str(result)[:200]}") from e


# ══════════════════════════════════════════════════════════════════
# v9.21: Provider Health Check
# ══════════════════════════════════════════════════════════════════

def check_provider_health_sync(provider: Provider) -> dict[str, Any]:
    """
    Quick health check for a single provider.
    Sends a tiny prompt to verify API key + connectivity.

    Returns:
        {"ok": bool, "latency_ms": int, "error": str, "model": str}
    """
    t0 = time.time()
    test_prompt = "Say 'ok' in one word."

    try:
        result = _call_provider_sync(provider, test_prompt, max_tokens=5)
        latency = int((time.time() - t0) * 1000)
        provider.last_check_ts = time.time()
        provider.last_check_ok = True
        provider.consecutive_fails = 0
        provider.last_error = ""
        return {
            "ok": True,
            "latency_ms": latency,
            "error": "",
            "model": provider.model,
            "provider": provider.name,
        }
    except Exception as exc:
        latency = int((time.time() - t0) * 1000)
        provider.last_check_ts = time.time()
        provider.last_check_ok = False
        provider.consecutive_fails += 1
        provider.last_error = str(exc)[:200]
        return {
            "ok": False,
            "latency_ms": latency,
            "error": str(exc)[:200],
            "model": provider.model,
            "provider": provider.name,
        }


async def check_provider_health(provider: Provider) -> dict[str, Any]:
    """Async wrapper for health check."""
    import asyncio
    return await asyncio.to_thread(check_provider_health_sync, provider)


async def check_all_providers_health(providers: list[Provider]) -> list[dict]:
    """Check health of all providers concurrently."""
    import asyncio
    tasks = [check_provider_health(p) for p in providers if p.enabled]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"ok": False, "error": str(r)[:200]})
        else:
            out.append(r)
    return out


# ══════════════════════════════════════════════════════════════════
# FallbackStack — main orchestrator
# ══════════════════════════════════════════════════════════════════

@dataclass
class CallResult:
    """Result from FallbackStack.call()."""
    text: str
    provider_used: str
    api_calls: int
    total_duration_ms: int
    was_fallback: bool
    safety_skipped: list[str] = field(default_factory=list)  # Providers skipped due to SAFETY


class FallbackStack:
    """
    Orchestrates LLM calls through the provider stack with smart routing.

    SAFETY handling (user spec):
      - Gemini returns SAFETY → skip ALL remaining Gemini providers
      - Jump directly to OpenRouter Qwen3-Coder-480B
      - Record in Failure Memory with key "next_provider"

    Normal error handling:
      - 429 / timeout / 500 → try next provider in order

    Usage::
        stack = FallbackStack(providers, failure_memory=fm)
        result = await stack.call(prompt, goal_hash="abc123")
        print(result.text, result.provider_used)
    """

    def __init__(
        self,
        providers: list[Provider],
        failure_memory: Any | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self._providers = providers
        self._failure_memory = failure_memory
        self._max_tokens = max_tokens
        self._call_log: list[dict] = []  # Recent calls (max 20)

    def _log_call(self, provider_name: str, success: bool, latency_ms: float, error: str = "") -> None:
        """Record a call in the log ring buffer."""
        import time
        self._call_log.append({
            "ts": time.time(),
            "provider": provider_name,
            "success": success,
            "latency_ms": round(latency_ms),
            "error": error[:100] if error else "",
        })
        if len(self._call_log) > 20:
            self._call_log = self._call_log[-20:]

    def get_logs(self) -> list[dict]:
        """Return recent call logs (newest first)."""
        return list(reversed(self._call_log))

    async def call(
        self,
        prompt: str,
        goal_hash: str = "",
        force_provider: str | None = None,
        preferred_provider: str | None = None,
    ) -> CallResult:
        """
        Call providers in stack order, with smart SAFETY routing.

        Args:
            prompt:         Full prompt string.
            goal_hash:      Goal hash for Failure Memory lookup.
            force_provider: If set, try this provider name first.

        Returns:
            CallResult with text and metadata.

        Raises:
            RuntimeError: If all providers fail.
        """
        t0 = time.monotonic()
        api_calls = 0
        safety_skipped: list[str] = []
        safety_triggered = False

        # Check Failure Memory for recommended provider from past experience
        recommended = self._get_recommended(goal_hash)

        # Build ordered list: force/recommended first, then normal stack
        # v4.3: chatbot_context / social workflow pass preferred_provider=…,
        # which raised TypeError before (content generation always failed).
        ordered = self._build_ordered(force_provider or preferred_provider or recommended)

        for provider in ordered:
            # v9.21: Skip disabled providers
            if not provider.enabled:
                _vlog("⏭", f"Skip {provider.name} (disabled)")
                continue

            # Skip Gemini providers if SAFETY was already triggered
            if safety_triggered and provider.skip_on_safety:
                safety_skipped.append(provider.name)
                _vlog("🛡️", f"Skip {provider.name} (SAFETY block nhảy qua Gemini)")
                continue

            # v9.21: Skip providers with 5+ consecutive failures (auto-disable)
            if provider.consecutive_fails >= 5:
                _vlog("🔴", f"Skip {provider.name} (5+ consecutive failures)")
                continue

            _vlog("🤖", f"Thử provider: {provider.name} "
                  f"(temp={provider.temperature}, timeout={provider.timeout_s}s)")
            api_calls += 1
            provider.total_calls += 1

            try:
                text = await asyncio.to_thread(
                    _call_provider_sync, provider, prompt, self._max_tokens
                )
                duration_ms = int((time.monotonic() - t0) * 1000)
                was_fallback = (provider.name != self._providers[0].name
                                if self._providers else False)

                # v9.21: Update health stats
                provider.total_success += 1
                provider.consecutive_fails = 0
                provider.last_check_ok = True
                provider.last_check_ts = time.time()
                provider.last_error = ""
                # Running average latency
                if provider.avg_latency_ms:
                    provider.avg_latency_ms = provider.avg_latency_ms * 0.8 + duration_ms * 0.2
                else:
                    provider.avg_latency_ms = float(duration_ms)

                _vlog("✅", f"Provider {provider.name} thành công "
                      f"({duration_ms}ms, {api_calls} calls)")
                self._log_call(provider.name, True, duration_ms)

                # Record success in Failure Memory for future routing
                if goal_hash and self._failure_memory and was_fallback:
                    self._record_switch(
                        goal_hash,
                        from_provider="primary",
                        to_provider=provider.name,
                        reason="success_after_fallback",
                    )

                return CallResult(
                    text=text,
                    provider_used=provider.name,
                    api_calls=api_calls,
                    total_duration_ms=duration_ms,
                    was_fallback=was_fallback,
                    safety_skipped=safety_skipped,
                )

            except SafetyBlockError as exc:
                _vlog("🛡️", f"SAFETY block từ {provider.name} — nhảy qua Gemini")
                safety_triggered = True
                safety_skipped.append(provider.name)
                provider.consecutive_fails += 1
                provider.last_error = f"SAFETY: {str(exc)[:100]}"
                self._log_call(provider.name, False, 0, f"SAFETY: {str(exc)[:60]}")
                provider.last_check_ts = time.time()

                if goal_hash and self._failure_memory:
                    self._record_switch(
                        goal_hash,
                        from_provider=provider.name,
                        to_provider="mistral-small",  # v9.21: jump to Mistral first
                        reason=f"safety_block: {str(exc)[:100]}",
                    )
                continue

            except RateLimitError as exc:
                _vlog("⏳", f"429 Rate limit từ {provider.name} → thử tiếp")
                provider.consecutive_fails += 1
                provider.last_error = f"429: {str(exc)[:100]}"
                self._log_call(provider.name, False, 0, f"429: {str(exc)[:60]}")
                provider.last_check_ts = time.time()
                if goal_hash and self._failure_memory:
                    self._record_switch(
                        goal_hash,
                        from_provider=provider.name,
                        to_provider="next",
                        reason=f"rate_limit: {str(exc)[:80]}",
                    )
                continue

            except ProviderTimeoutError as exc:
                _vlog("⏰", f"Timeout từ {provider.name} → thử tiếp ({exc})")
                provider.consecutive_fails += 1
                provider.last_error = f"TIMEOUT"
                self._log_call(provider.name, False, 0, "TIMEOUT")
                provider.last_check_ts = time.time()
                continue

            except ProviderServerError as exc:
                _vlog("❌", f"Server error từ {provider.name} → thử tiếp ({exc})")
                provider.consecutive_fails += 1
                provider.last_error = f"5xx: {str(exc)[:80]}"
                self._log_call(provider.name, False, 0, f"5xx: {str(exc)[:60]}")
                provider.last_check_ts = time.time()
                continue

            except Exception as exc:
                _vlog("❌", f"Lỗi từ {provider.name}: {str(exc)[:80]} → thử tiếp")
                provider.consecutive_fails += 1
                provider.last_error = str(exc)[:100]
                provider.last_check_ts = time.time()
                continue

        # All providers failed
        duration_ms = int((time.monotonic() - t0) * 1000)
        raise RuntimeError(
            f"Tất cả {len(self._providers)} providers thất bại "
            f"sau {api_calls} calls, {duration_ms}ms. "
            f"SAFETY skipped: {safety_skipped}"
        )

    # ── Internal helpers ──────────────────────────────────────────

    def _build_ordered(self, preferred_name: str | None) -> list[Provider]:
        """
        Return providers in call order.
        If preferred_name is set and found → move it to front.
        """
        if not preferred_name:
            return list(self._providers)

        # match by name, then exact model id, then provider family ("gemini-…")
        family = preferred_name.split("-")[0].split(":")[0].lower()
        preferred = (
            next((p for p in self._providers if p.name == preferred_name), None)
            or next((p for p in self._providers if p.model == preferred_name and p.enabled), None)
            or next((p for p in self._providers if p.kind == family and p.enabled), None)
        )
        if not preferred:
            return list(self._providers)

        # Move preferred to front, keep rest in order
        rest = [p for p in self._providers if p is not preferred]
        return [preferred] + rest

    def _get_recommended(self, goal_hash: str) -> str | None:
        """
        Ask Failure Memory which provider succeeded before for this goal.
        Returns provider name or None.
        """
        if not goal_hash or not self._failure_memory:
            return None
        try:
            if hasattr(self._failure_memory, "get_recommended_provider"):
                return self._failure_memory.get_recommended_provider(goal_hash)
        except Exception:
            pass
        return None

    def _record_switch(
        self,
        goal_hash: str,
        from_provider: str,
        to_provider: str,
        reason: str,
    ) -> None:
        """Record provider switch in Failure Memory."""
        if not self._failure_memory:
            return
        try:
            if hasattr(self._failure_memory, "record_switch"):
                self._failure_memory.record_switch(
                    goal_hash=goal_hash,
                    from_provider=from_provider,
                    to_provider=to_provider,
                    reason=reason,
                )
        except Exception:
            pass

    # ── Stats + Provider Management ─────────────────────────────

    def stats(self) -> dict:
        return {
            "providers": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "model": p.model,
                    "temperature": p.temperature,
                    "timeout_s": p.timeout_s,
                    "has_key": bool(p.api_key),
                    "enabled": p.enabled,
                    # v9.21: Health data
                    "health_ok": p.last_check_ok,
                    "consecutive_fails": p.consecutive_fails,
                    "total_calls": p.total_calls,
                    "total_success": p.total_success,
                    "success_rate": round(
                        p.total_success / p.total_calls, 2
                    ) if p.total_calls > 0 else 1.0,
                    "avg_latency_ms": round(p.avg_latency_ms),
                    "last_error": p.last_error,
                    "last_check_ts": p.last_check_ts,
                }
                for p in self._providers
            ],
            "stack_size": len(self._providers),
            "enabled_count": sum(1 for p in self._providers if p.enabled),
            "healthy_count": sum(1 for p in self._providers if p.last_check_ok and p.enabled),
        }

    def get_provider(self, name: str) -> Provider | None:
        """Get provider by name."""
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def set_provider_enabled(self, name: str, enabled: bool) -> bool:
        """Enable/disable a provider from admin panel."""
        p = self.get_provider(name)
        if p:
            p.enabled = enabled
            _vlog("⚙", f"Provider '{name}' → {'enabled' if enabled else 'disabled'}")
            return True
        return False

    def update_api_key(self, name: str, new_key: str) -> bool:
        """Update API key for a provider (e.g. when old key expired).
        Auto-enables provider if it was disabled due to missing key."""
        p = self.get_provider(name)
        if p:
            p.api_key = new_key
            p.consecutive_fails = 0  # Reset fails on key change
            p.last_check_ok = True
            p.last_error = ""
            # Auto-enable if key is provided
            if new_key.strip():
                p.enabled = True
            _vlog("🔑", f"Provider '{name}' API key updated → {'enabled' if p.enabled else 'disabled'}")
            # For Gemini: update BOTH providers with same key
            if "gemini" in name:
                for other in self._providers:
                    if other.kind == "gemini" and other.name != name:
                        other.api_key = new_key
                        other.consecutive_fails = 0
                        other.last_error = ""
                        if new_key.strip():
                            other.enabled = True
                        _vlog("🔑", f"Provider '{other.name}' key synced from '{name}'")
            return True
        return False

    def reset_provider_health(self, name: str) -> bool:
        """Reset health counters for a provider."""
        p = self.get_provider(name)
        if p:
            p.consecutive_fails = 0
            p.last_check_ok = True
            p.last_error = ""
            p.total_calls = 0
            p.total_success = 0
            _vlog("🔄", f"Provider '{name}' health reset")
            return True
        return False

    def reorder(self, ordered_names: list[str]) -> bool:
        """Reorder providers by name list from admin panel."""
        name_map = {p.name: p for p in self._providers}
        new_order = []
        for n in ordered_names:
            if n in name_map:
                new_order.append(name_map.pop(n))
        # Append any remaining providers not in the list
        for p in name_map.values():
            new_order.append(p)
        self._providers = new_order
        names = [p.name for p in self._providers]
        _vlog("🔀", f"Provider stack reordered → {' → '.join(names)}")
        return True

    def move_provider(self, name: str, direction: str) -> bool:
        """Move a provider up or down in the stack. direction='up' or 'down'."""
        idx = next((i for i, p in enumerate(self._providers) if p.name == name), -1)
        if idx < 0:
            return False
        if direction == "up" and idx > 0:
            self._providers[idx], self._providers[idx - 1] = self._providers[idx - 1], self._providers[idx]
        elif direction == "down" and idx < len(self._providers) - 1:
            self._providers[idx], self._providers[idx + 1] = self._providers[idx + 1], self._providers[idx]
        else:
            return False
        names = [p.name for p in self._providers]
        _vlog("🔀", f"Provider '{name}' moved {direction} → {' → '.join(names)}")
        return True

    def add_provider(self, provider: Provider) -> bool:
        """Add a new provider to the stack."""
        if any(p.name == provider.name for p in self._providers):
            return False  # Already exists
        self._providers.append(provider)
        _vlog("➕", f"Provider added: {provider.name} ({provider.kind}/{provider.model})")
        return True

    def remove_provider(self, name: str) -> bool:
        """Remove a provider from the stack."""
        idx = next((i for i, p in enumerate(self._providers) if p.name == name), -1)
        if idx < 0:
            return False
        removed = self._providers.pop(idx)
        _vlog("➖", f"Provider removed: {removed.name}")
        return True
