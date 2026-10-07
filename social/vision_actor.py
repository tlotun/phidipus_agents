# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
social/vision_actor.py — Phidipus See-Think-Act Engine v2.1 (v9.23)
════════════════════════════════════════════════════════════════════

Nâng cấp v9.23:

1. get_chrome_window_bounds() — AppleScript cache 30s, offset logic
2. Window-scoped screenshot — chụp CHỈ vùng Chrome, không full screen
3. Coordinate offset — rx/ry tính trên Chrome bounds rồi cộng x_offset/y_offset
4. Largest-image heuristic — bỏ CDN pattern, lấy ảnh lớn nhất (area-based)
5. DOM-first strategy — JS selector trước, Vision chỉ khi DOM fail
6. Screenshot guard — activate Chrome trước mỗi screencapture -R
7. SemanticMemory UI selector cache — skip Vision sau lần đầu thành công

Kiến trúc 4 tầng:
  L0: SemanticMemory selector cache (0ms)
  L1: DOM / JS inject (5ms)
  L2: Gemini Vision window-scoped (1.5s)
  L3: Ollama VLM / AppleScript fallback (15s)

Invariants kế thừa từ v2.0:
  - Normalized ratio coordinates (rx/ry 0.0-1.0)
  - See-Do-Check loop với verify
  - SmartWait thay hardcode sleep
  - Gemini 2.0 Flash primary → qwen3-vl fallback
"""
from __future__ import annotations

import asyncio, base64, json, os, subprocess, time

# v9.28: Vision enhancement modules
try:
    # BUG #4 FIX: import từ social.click_verifier (C2 single-path) thay vì vision trực tiếp
    from social.click_verifier import ClickVerifier, SiteAssertions
    from vision.click_memory import ClickMemory, ActionKeys
    from vision.multimodal_consensus import MultiModalConsensus
    from vision.crop_locator import CropLocator
    from vision.zoom_action import ZoomActionEngine, ZoomPresets
    from vision.reflection_supervisor import ReflectionSupervisor, NextAction
    from vision.inner_monologue import critical_guard, GuardPresets, MonologueStore
    _VISION_ENHANCED = True
except ImportError:
    _VISION_ENHANCED = False

# v2.4 C2: ClickStrategySelector — extracted strategy cascade manager
_HAS_CSS = False
try:
    from social.click_strategy import ClickStrategySelector, get_strategy_selector, STRATEGIES
    _HAS_CSS = True
except ImportError:
    pass
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A2: CrossDomain Learning
try:
    from memory.cross_domain_memory import CrossDomainMemory, get_cross_domain_memory
    _HAS_CDM = True
except ImportError:
    _HAS_CDM = False

# B2: Dynamic Confidence Calibration
try:
    from memory.confidence_history import ConfidenceHistory, get_confidence_history
    _HAS_CONF_HIST = True
except ImportError:
    _HAS_CONF_HIST = False


def _vlog(icon, msg): print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ── A3 v9.24: cliclick availability check ────────────────────────────
import shutil as _shutil
_CLICLICK_AVAILABLE: bool | None = None   # None = not checked yet

def _check_cliclick() -> bool:
    """Kiểm tra cliclick có available không (lazy, cached)."""
    global _CLICLICK_AVAILABLE
    if _CLICLICK_AVAILABLE is None:
        _CLICLICK_AVAILABLE = _shutil.which("cliclick") is not None
        if not _CLICLICK_AVAILABLE:
            _vlog("⚠️ ", "cliclick chưa install — drag & drop sẽ dùng JS fallback. Fix: brew install cliclick")
    return _CLICLICK_AVAILABLE


# ══════════════════════════════════════════════════════════════════════
# Chrome Window Bounds — v9.23 Phase 1 core fix
# ══════════════════════════════════════════════════════════════════════

_chrome_bounds_cache: dict = {}   # {bounds: {x,y,w,h}, ts: float}

# v9.34 A2: Cache TTL giảm từ 30s → 5s
# 30s quá lâu — Chrome có thể bị resize/move giữa các bước workflow
# 5s đủ nhanh (không query mỗi click) nhưng catch kịp khi user resize
_CHROME_BOUNDS_CACHE_TTL = 5.0

async def get_chrome_window_bounds() -> dict:
    """
    Lấy bounds của Chrome front window qua AppleScript (logical coords).

    v9.34 A2:
      - Cache TTL: 30s → 5s (catch kịp khi user resize Chrome)
      - Bỏ activate trước khi đọc bounds (activate steal focus,
        làm mất cursor trong ô text đang type)
      - Guard: Chrome not running → return default ngay, không hang
      - Sanity check: w > 400 và h > 300 mới accept
      - Default {x:0, y:25, w:1920, h:1055} nếu fail

    Dùng cho:
      - screencapture -R x,y,w,h   (chụp đúng vùng Chrome)
      - offset tọa độ VLM: abs_x = rx * w + x, abs_y = ry * h + y

    Invalidate thủ công: invalidate_chrome_bounds_cache()
    """
    global _chrome_bounds_cache
    now = time.time()

    # Cache hit (còn trong 5s)
    if _chrome_bounds_cache and now - _chrome_bounds_cache.get("ts", 0) < _CHROME_BOUNDS_CACHE_TTL:
        return _chrome_bounds_cache["bounds"]

    default = {"x": 0, "y": 25, "w": 1920, "h": 1055}

    try:
        # Đọc bounds trực tiếp — KHÔNG activate trước
        # activate steal focus và làm mất cursor trong ô text
        script = (
            'tell application "Google Chrome"\n'
            '    if not running then return "not_running"\n'
            '    try\n'
            '        set b to bounds of window 1\n'
            '        set x1 to (item 1 of b) as integer\n'
            '        set y1 to (item 2 of b) as integer\n'
            '        set x2 to (item 3 of b) as integer\n'
            '        set y2 to (item 4 of b) as integer\n'
            '        return (x1 as string) & "," & (y1 as string) & '
            '"," & (x2 as string) & "," & (y2 as string)\n'
            '    on error\n'
            '        return "0,25,1920,1080"\n'
            '    end try\n'
            'end tell'
        )
        proc = await asyncio.to_thread(
            subprocess.run, ["osascript", "-e", script],
            capture_output=True, text=True, timeout=4,
        )
        raw = proc.stdout.strip()

        if raw == "not_running":
            return default

        parts = raw.split(",")
        if len(parts) == 4:
            x1, y1, x2, y2 = (int(p.strip()) for p in parts)
            w, h = x2 - x1, y2 - y1
            if w > 400 and h > 300:
                bounds = {"x": x1, "y": y1, "w": w, "h": h}
                _chrome_bounds_cache = {"bounds": bounds, "ts": now}
                _vlog("🪟", f"Chrome bounds: ({x1},{y1}) {w}×{h} [cached {_CHROME_BOUNDS_CACHE_TTL:.0f}s]")
                return bounds

    except Exception as e:
        _vlog("⚠️ ", f"Chrome bounds error: {e} — using defaults")

    return default


def invalidate_chrome_bounds_cache() -> None:
    """Gọi sau khi resize Chrome window để force re-detect bounds."""
    global _chrome_bounds_cache
    _chrome_bounds_cache = {}


# ══════════════════════════════════════════════════════════════════════
# Screen info — detect resolution & Retina scale
# ══════════════════════════════════════════════════════════════════════

_screen_cache: dict = {}

async def _get_screen_info() -> dict:
    """
    Lấy kích thước màn hình thực từ screencapture PNG.
    FIX: Không dùng Finder bounds (trả kết quả sai trên multi-monitor và 21:9).
    Chụp screenshot full → đọc PNG header → lấy width×height chính xác.
    
    2K 21:9 (2560×1080): trả {logical_w:2560, logical_h:1080, scale:1}
    Retina 1920×1200:     trả {logical_w:1920, logical_h:1200, scale:2}
    """
    global _screen_cache
    if _screen_cache:
        return _screen_cache

    info = {"logical_w": 1920, "logical_h": 1080, "scale": 1,
            "physical_w": 1920, "physical_h": 1080}
    tmp = f"/tmp/ph_screen_{int(time.time())}.png"
    try:
        # Chụp full screenshot — PNG header sẽ cho kích thước pixel thực
        await asyncio.to_thread(
            subprocess.run,
            ["screencapture", "-x", tmp],
            capture_output=True, timeout=6,
        )
        if os.path.exists(tmp):
            import struct
            with open(tmp, "rb") as f:
                f.read(8)   # PNG signature
                f.read(4)   # chunk length (IHDR = 13)
                f.read(4)   # "IHDR"
                phys_w = struct.unpack(">I", f.read(4))[0]
                phys_h = struct.unpack(">I", f.read(4))[0]

            # Lấy logical size qua NSScreen (AppleScript)
            r = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e",
                 'tell application "Finder"\n'
                 '  set {x1,y1,x2,y2} to bounds of window of desktop\n'
                 '  return (x2 as string) & "," & (y2 as string)\n'
                 'end tell'],
                capture_output=True, text=True, timeout=5,
            )
            parts = r.stdout.strip().split(",")
            if len(parts) == 2:
                logical_w = int(parts[0].strip())
                logical_h = int(parts[1].strip())
                # Sanity check: logical must be ≤ physical
                if 0 < logical_w <= phys_w and 0 < logical_h <= phys_h:
                    scale = max(1, phys_w // logical_w)
                else:
                    # Multi-monitor or bad Finder bounds → derive from physical
                    # Assume scale=1 for non-Retina, scale=2 for Retina
                    scale = 2 if phys_w > 2560 and phys_h > 1440 else 1
                    logical_w = phys_w // scale
                    logical_h = phys_h // scale
            else:
                scale = 2 if phys_w > 2560 else 1
                logical_w = phys_w // scale
                logical_h = phys_h // scale

            info = {
                "logical_w":  logical_w,
                "logical_h":  logical_h,
                "scale":      scale,
                "physical_w": phys_w,
                "physical_h": phys_h,
            }
            _vlog("🖥️ ", f"Screen: {logical_w}×{logical_h} logical, "
                         f"{phys_w}×{phys_h} physical, scale={scale}")

    except Exception as e:
        _vlog("⚠️ ", f"Screen info error: {e} — using defaults")
    finally:
        try: os.unlink(tmp)
        except: pass

    _screen_cache = info
    return info


# ══════════════════════════════════════════════════════════════════════
# Screenshot
# ══════════════════════════════════════════════════════════════════════

async def _take_screenshot(region=None) -> bytes | None:
    """
    Chụp screenshot.
    v9.23: Nếu có region (window-scoped), activate Chrome trước để tránh
    Finder hoặc dialog khác che lên — nếu không, vùng Chrome bị che sẽ
    cho ảnh sai và VLM tìm sai tọa độ.
    """
    tmp = f"/tmp/nb_vision_{int(time.time()*1000)}.png"
    try:
        # Screenshot guard: kéo Chrome lên frontmost trước khi chụp vùng
        if region:
            await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                capture_output=True, timeout=2,
            )
            await asyncio.sleep(0.15)  # chờ macOS raise window

        cmd = ["screencapture", "-x"]
        if region:
            x, y, w, h = region
            cmd += ["-R", f"{x},{y},{w},{h}"]
        cmd.append(tmp)
        await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=5)
        if os.path.exists(tmp):
            return Path(tmp).read_bytes()
    except Exception as e:
        _vlog("⚠️ ", f"Screenshot: {e}")
    finally:
        try: os.unlink(tmp)
        except: pass
    return None


# ══════════════════════════════════════════════════════════════════════
# VLM backends
# ══════════════════════════════════════════════════════════════════════

class QuotaExceededError(Exception): pass


def _resize_for_vlm(img_bytes: bytes, max_width: int = 1024) -> bytes:
    """
    PERF-05 FIX: Resize screenshot về max_width trước khi gửi VLM.

    1080p JPEG gốc ≈ 300–800KB mỗi call.
    Với S2 MultiModal (5 VLM calls) → 1.5–4MB network traffic per vision_click.
    Sau resize 1024px wide: ~80–150KB → giảm ~5× network + inference time.

    1024px wide là đủ cho element detection — VLM không cần full resolution
    để xác định vị trí button/input.
    """
    if len(img_bytes) <= 150_000:
        return img_bytes  # đã đủ nhỏ, không cần resize

    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(img_bytes)) as img:
            if img.width <= max_width:
                return img_bytes
            ratio = max_width / img.width
            new_h = int(img.height * ratio)
            resized = img.resize((max_width, new_h), Image.LANCZOS)
            if resized.mode in ("RGBA", "P", "LA"):
                resized = resized.convert("RGB")
            buf = _io.BytesIO()
            resized.save(buf, format="JPEG", quality=75, optimize=True)
            return buf.getvalue()
    except Exception:
        return img_bytes  # fallback: dùng ảnh gốc nếu resize fail

async def _call_gemini_vision(img_bytes, prompt, api_key,
                               model="gemini-2.5-flash", timeout=15.0) -> str:
    """
    Gọi Gemini Vision API.

    v9.35 B3: Hỗ trợ gemini-2.5-flash với thinking budget = 0
    (tắt thinking cho vision tasks — nhanh hơn, không cần chain-of-thought).
    Tự resize ảnh > 1MB trước khi gửi để giảm latency.
    """
    import urllib.request as _ur

    # Resize ảnh nếu quá lớn — Gemini 2.5 nhanh hơn với ảnh < 800KB
    _img = img_bytes
    if len(_img) > 800_000:
        try:
            from PIL import Image
            import io as _io
            with Image.open(_io.BytesIO(_img)) as im:
                w, h = im.size
                scale = min(1.0, (1024 / max(w, h)))
                if scale < 1.0:
                    im2 = im.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
                    buf = _io.BytesIO()
                    im2.save(buf, format="JPEG", quality=85, optimize=True)
                    _img = buf.getvalue()
        except Exception:
            pass  # dùng ảnh gốc nếu resize fail

    b64  = base64.b64encode(_img).decode()
    mime = "image/png" if _img[:4] == b"\x89PNG" else "image/jpeg"

    # generationConfig theo model
    gen_config: dict = {"maxOutputTokens": 512, "temperature": 0.1}

    # v4.3: retired ids (2.0 / dated previews) → current Flash
    try:
        from core.llm_fallback import normalize_gemini_model
        model = normalize_gemini_model(model)
    except Exception:
        pass

    # Gemini 2.5 Flash: tắt thinking cho vision tasks
    # thinking cần thêm 5–15s mà không cần thiết khi chỉ tìm tọa độ
    if "flash" in model:
        gen_config["thinkingConfig"] = {"thinkingBudget": 0}

    body = json.dumps({
        "contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": prompt},
        ]}],
        "generationConfig": gen_config,
    }).encode()

    # v4.3: key in header (URLs with ?key= leak into logs / exception text)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = _ur.Request(url, data=body,
                      headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                      method="POST")
    def _call():
        with _ur.urlopen(req, timeout=int(timeout)) as r:
            return json.loads(r.read().decode())
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout+2)
        return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        if "429" in str(e) or "quota" in str(e).lower():
            raise QuotaExceededError(str(e))
        raise

async def _call_ollama_vision(img_bytes, prompt,
                               model="qwen3-vl:8b-instruct", timeout=30.0) -> str:
    import urllib.request as _ur
    try:  # v4.3: use the installed vision model (registry) and configured host
        from core.model_registry import resolve_model, ollama_url
        model = resolve_model(model, "vision")
        _base = ollama_url()
    except Exception:
        _base = "http://127.0.0.1:11434"

    # PERF-05 FIX: resize trước khi gửi Ollama — giảm network traffic và inference time
    _img = _resize_for_vlm(img_bytes, max_width=1024)

    payload = json.dumps({
        "model": model, "stream": False,
        "messages": [{"role":"user","content":prompt,
                      "images":[base64.b64encode(_img).decode()]}],
        "options": {"temperature":0.1,"num_predict":300},
        "think": False,
    }).encode()
    req = _ur.Request(f"{_base}/api/chat", data=payload,
                      headers={"Content-Type":"application/json"}, method="POST")
    def _call():
        with _ur.urlopen(req, timeout=int(timeout)) as r:
            return json.loads(r.read().decode())
    result = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout+2)
    return result.get("message",{}).get("content","").strip()


# ══════════════════════════════════════════════════════════════════════
# VLM Router: Gemini primary → Ollama fallback
# ══════════════════════════════════════════════════════════════════════

class VLMRouter:
    """
    VLM Router 3 tiers — v9.35 B3.

    Tier 0: Gemini 2.5 Flash  (2–4s, best quality, cần API key + online)
    Tier 1: Gemini 2.0 Flash  (3–6s, fallback khi 2.5 quota/timeout)
    Tier 2: qwen3-vl local    (15–25s, offline fallback, không tốn $)

    Adaptive routing:
      - RAM > 80% → skip Tier 2 (Ollama tốn thêm RAM)
      - Quota exceeded → backoff 60s rồi retry
      - Timeout Tier 0 → ngay lập tức thử Tier 1 (không chờ)
      - Tier 0 + Tier 1 đều fail → Tier 2

    Stats tracking: mỗi call ghi nhận tier + latency → log cuối workflow.
    """

    # Gemini 2.5 Flash — model string production
    # v4.3: both former ids were retired (HTTP 404) — Flash + Flash-Lite
    MODEL_GEMINI_25 = "gemini-2.5-flash"
    MODEL_GEMINI_20 = "gemini-2.5-flash-lite"

    # Timeout per tier (giây) — 2.5 nhanh hơn cần timeout ngắn hơn
    TIMEOUT_T0 = 10.0   # Gemini 2.5
    TIMEOUT_T1 = 15.0   # Gemini 2.0
    TIMEOUT_T2 = 30.0   # Ollama

    def __init__(
        self,
        gemini_api_key: str = "",
        gemini_model:   str = MODEL_GEMINI_25,
        ollama_model:   str = "qwen3-vl:8b-instruct",
        resource_guard: Any = None,
    ):
        self._key           = gemini_api_key
        self._gmod          = gemini_model   # model override từ config
        self._omod          = ollama_model
        self._resource_guard = resource_guard

        # Quota backoff per tier: {tier: backoff_until_timestamp}
        self._quota_until: dict[str, float] = {"t0": 0.0, "t1": 0.0}

        # Stats
        self._calls:    dict[str, int]   = {"t0": 0, "t1": 0, "t2": 0, "error": 0}
        self._latency:  dict[str, float] = {"t0": 0.0, "t1": 0.0, "t2": 0.0}

        # PERF-01 FIX: circuit breaker for Ollama (Tier 2).
        # Trip after 1 consecutive timeout (was effectively 2, wasting 2×25s).
        # Backs off for 90s before retrying.
        self._t2_consec_timeouts: int = 0
        self._t2_breaker_until:   float = 0.0
        self._T2_TRIP_AFTER:      int   = 1      # trip on 1st timeout
        self._T2_BACKOFF_SEC:     float = 90.0   # cool-down before retry

    async def call(self, img_bytes: bytes, prompt: str) -> tuple[str, str]:
        """
        Gọi VLM tốt nhất có thể.
        Returns: (response_text, tier_name)
        tier_name: "gemini-2.5" | "gemini-2.0" | "ollama" | "error"

        PERF FIX (S4): Race T0+T1 song song thay vì sequential.
        Worst case: 55s (10+15+30) → 10s (race timeout) + 30s Ollama = 40s.
        Với Gemini hoạt động bình thường (2–4s), latency không đổi.
        """
        now = time.time()

        # ── Phase 1: Race T0 vs T1 song song ────────────────────────
        # Cả hai đều là Gemini online — lấy kết quả nhanh hơn, cancel cái còn lại.
        t0_available = bool(self._key) and now > self._quota_until.get("t0", 0)
        t1_available = bool(self._key) and now > self._quota_until.get("t1", 0)

        if t0_available or t1_available:
            tasks = {}
            if t0_available:
                tasks["t0"] = asyncio.create_task(
                    _call_gemini_vision(img_bytes, prompt, self._key,
                                        model=self.MODEL_GEMINI_25,
                                        timeout=self.TIMEOUT_T0)
                )
            if t1_available:
                tasks["t1"] = asyncio.create_task(
                    _call_gemini_vision(img_bytes, prompt, self._key,
                                        model=self.MODEL_GEMINI_20,
                                        timeout=self.TIMEOUT_T1)
                )

            # Chờ task nào về trước (trong 10s — timeout T0)
            race_timeout = self.TIMEOUT_T0
            pending_tasks = set(tasks.values())
            done_tasks: set = set()

            try:
                done_tasks, still_pending = await asyncio.wait(
                    pending_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=race_timeout,
                )
            except Exception:
                done_tasks, still_pending = set(), pending_tasks

            # Cancel các task chưa xong
            for task in still_pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            # Lấy kết quả từ task đầu tiên hoàn thành thành công
            for tier_name, task in tasks.items():
                if task in done_tasks and not task.cancelled():
                    try:
                        exc = task.exception()
                        if exc is None:
                            r = task.result()
                            elapsed_t = time.monotonic()
                            self._calls[tier_name] += 1
                            model_label = "gemini-2.5" if tier_name == "t0" else "gemini-2.0"
                            _vlog("✅", f"{model_label} race winner")
                            return r, model_label
                        # Handle quota errors
                        if isinstance(exc, QuotaExceededError):
                            self._quota_until[tier_name] = time.time() + 60
                            _vlog("⚠️ ", f"{tier_name} quota exceeded → backoff 60s")
                    except Exception as e:
                        _vlog("⚠️ ", f"{tier_name} race error: {str(e)[:60]}")

            _vlog("⚠️ ", f"Gemini race miss (both T0+T1 failed/timeout) → Tier 2 Ollama")

        # ── Phase 2: Tier 2 Ollama — genuine offline fallback ────────
        # Skip nếu RAM quá cao (Ollama cần thêm RAM cho VLM inference)
        if self._resource_guard:
            try:
                snap = self._resource_guard.get_snapshot()
                if snap.ram_used_percent > 88:
                    _vlog("⚠️ ", f"Tier 2 skip: RAM {snap.ram_used_percent:.0f}% > 88%")
                    self._calls["error"] += 1
                    return "", "error"
            except Exception:
                pass  # không có snapshot → proceed

        # PERF-01 FIX: circuit breaker — skip Tier 2 if it timed out recently
        _now_t = time.time()
        if _now_t < self._t2_breaker_until:
            remaining = int(self._t2_breaker_until - _now_t)
            _vlog("⚡", f"Tier 2 circuit OPEN (Ollama breaker active, {remaining}s left) → skip")
            self._calls["error"] += 1
            return "", "error"

        try:
            t0 = time.monotonic()
            r = await _call_ollama_vision(img_bytes, prompt, self._omod, self.TIMEOUT_T2)
            elapsed = time.monotonic() - t0
            self._calls["t2"] += 1
            self._latency["t2"] += elapsed
            # PERF-01 FIX: successful call resets consecutive-timeout counter
            self._t2_consec_timeouts = 0
            _vlog("✅", f"qwen3-vl ({elapsed:.1f}s)")
            return r, "ollama"
        except asyncio.TimeoutError:
            # PERF-01 FIX: trip breaker after _T2_TRIP_AFTER consecutive timeouts
            self._t2_consec_timeouts += 1
            if self._t2_consec_timeouts >= self._T2_TRIP_AFTER:
                self._t2_breaker_until = time.time() + self._T2_BACKOFF_SEC
                _vlog("⚡", f"Tier 2 circuit TRIPPED after {self._t2_consec_timeouts} timeout(s) — backoff {self._T2_BACKOFF_SEC:.0f}s")
            self._calls["error"] += 1
            return "", "error"
        except Exception as e:
            self._calls["error"] += 1
            _vlog("❌", f"All VLM tiers failed: {e}")
            return "", "error"

    def stats_summary(self) -> str:
        """Tóm tắt stats để log cuối workflow."""
        parts = []
        total = sum(self._calls.values())
        for tier, count in self._calls.items():
            if count > 0:
                avg_ms = (
                    int(self._latency.get(tier, 0) / count * 1000)
                    if tier != "error" else 0
                )
                parts.append(f"{tier}×{count}({avg_ms}ms avg)")
        return f"VLM: {total} calls [{', '.join(parts) or 'none'}]"

    def reset_stats(self) -> None:
        self._calls   = {"t0": 0, "t1": 0, "t2": 0, "error": 0}
        self._latency = {"t0": 0.0, "t1": 0.0, "t2": 0.0}


# ══════════════════════════════════════════════════════════════════════
# Prompts — dùng normalized 0-1000 coordinates (Gemini recommendation)
# ══════════════════════════════════════════════════════════════════════

_FIND_NORMALIZED = (
    "Look at this screenshot carefully.\n"
    "Find this UI element: {query}\n\n"
    "Respond ONLY with valid JSON (no markdown, no explanation):\n"
    "  If found:   {{\"found\": true, "
    "\"rx\": <x_ratio>, \"ry\": <y_ratio>, "
    "\"confidence\": <0.0-1.0>, \"label\": \"<short description>\"}}\n"
    "  If missing: {{\"found\": false}}\n"
    "rx = horizontal center as decimal 0.0(left) to 1.0(right)\n"
    "ry = vertical center as decimal 0.0(top) to 1.0(bottom)\n"
    "Example: element at screen center = rx=0.5, ry=0.5\n"
    "Example: top-right button = rx=0.85, ry=0.05"
)

_YESNO  = "Look at this screenshot. Answer YES or NO only.\nQuestion: {q}"
_DESC   = "Describe in 1-2 sentences what you see. Focus on: {focus}"
_VERIFY = (
    "Look at this screenshot. Did the action succeed?\n"
    "Expected result: {expected}\n"
    "Answer YES or NO, then one sentence explaining what you see."
)


# ══════════════════════════════════════════════════════════════════════
# Result type
# ══════════════════════════════════════════════════════════════════════

@dataclass
class VisionResult:
    found:      bool
    x:          int   = 0
    y:          int   = 0
    confidence: float = 0.0
    label:      str   = ""
    tier:       str   = ""
    raw:        str   = ""


def _parse(raw: str, scale: int = 1,
           screen_w: int = 0, screen_h: int = 0) -> VisionResult:
    """
    Parse JSON response từ VLM.
    Hỗ trợ cả ratio (rx/ry) và pixel (cx/cy/x/y).
    Ratio mode (rx/ry): multiply by screen_w/screen_h → resolution independent.
    Pixel mode (cx/cy): divide by scale → Retina compensation.
    screen_w=0/screen_h=0 → auto-detect via _detect_screen_size() from zoom_action.
    """
    if not raw:
        return VisionResult(found=False, raw="empty")
    # Resolve screen size if not explicitly provided
    if screen_w <= 0 or screen_h <= 0:
        try:
            from vision.zoom_action import _detect_screen_size
            _sw, _sh = _detect_screen_size()
            if screen_w <= 0:
                screen_w = _sw
            if screen_h <= 0:
                screen_h = _sh
        except Exception:
            if screen_w <= 0:
                screen_w = 1440
            if screen_h <= 0:
                screen_h = 900
    try:
        text = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        d = json.loads(text)
        if not d.get("found"):
            return VisionResult(found=False, raw=raw)

        # Ratio mode — preferred, works on any resolution
        if "rx" in d and "ry" in d:
            rx = float(d["rx"])
            ry = float(d["ry"])
            # Clamp to valid range
            rx = max(0.0, min(1.0, rx))
            ry = max(0.0, min(1.0, ry))
            cx = int(rx * screen_w)
            cy = int(ry * screen_h)
            _vlog("📐", f"Ratio ({rx:.3f},{ry:.3f}) → pixel ({cx},{cy}) on {screen_w}×{screen_h}")
        else:
            # Pixel mode fallback
            cx = int(d.get("cx", d.get("x", 0))) // max(1, scale)
            cy = int(d.get("cy", d.get("y", 0))) // max(1, scale)

        return VisionResult(found=True, x=cx, y=cy,
                            confidence=float(d.get("confidence", 0.5)),
                            label=str(d.get("label", "")), raw=raw)
    except Exception:
        import re as _re
        nums = _re.findall(r'[\d.]+', raw)
        if len(nums) >= 2 and any(w in raw.lower() for w in ("found","yes","visible")):
            # Try to interpret as ratios if values are 0-1
            try:
                v0, v1 = float(nums[0]), float(nums[1])
                if v0 <= 1.0 and v1 <= 1.0:
                    return VisionResult(found=True,
                                        x=int(v0 * screen_w), y=int(v1 * screen_h),
                                        confidence=0.5, raw=raw)
                return VisionResult(found=True,
                                    x=int(v0)//max(1,scale), y=int(v1)//max(1,scale),
                                    confidence=0.5, raw=raw)
            except Exception:
                pass
        return VisionResult(found=False, raw=raw)


# ══════════════════════════════════════════════════════════════════════
# VisionActor — core engine
# ══════════════════════════════════════════════════════════════════════

class VisionActor:
    """
    AI mắt của Phidipus — See-Think-Act với self-healing.

    Cách dùng:
        actor = VisionActor.from_config(ipc_client=ipc)
        ok  = await actor.find_and_click("Save button")
        yes = await actor.ask_vision("Is there a generated image?")
        ok  = await actor.smart_wait("Post button visible", timeout=30)
    """

    def __init__(self, ipc_client=None, gemini_api_key="",
                 gemini_model="gemini-2.5-flash",  # v4.3: preview id retired
                 ollama_model="qwen3-vl:8b-instruct",
                 screenshot_region=None,
                 semantic_mem=None,
                 memory_path=None,
                 vision_mode="standard",
                 resource_guard=None):
        self._ipc          = ipc_client
        self._region       = screenshot_region
        # v9.35 B3: VLMRouter 3 tiers — Gemini 2.5 → 2.0 → Ollama
        self._vlm          = VLMRouter(
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            ollama_model=ollama_model,
            resource_guard=resource_guard,
        )
        self._last_img: bytes | None = None
        self._screen: dict = {}
        self._semantic_mem = semantic_mem   # v9.23: SemanticMemory for selector cache

        # v9.28: Enhanced vision modules
        self._vision_mode  = vision_mode  # "standard" | "enhanced" | "ultra"
        self._click_mem:   "ClickMemory | None"  = None
        self._verifier:    "ClickVerifier | None" = None
        self._consensus:   "MultiModalConsensus | None" = None
        self._crop_loc:    "CropLocator | None"  = None
        self._zoom:        "ZoomActionEngine | None" = None       # v9.30
        self._reflection:  "ReflectionSupervisor | None" = None  # v9.31
        self._desktop_som: "UnifiedSOMEngine | None"     = None  # v9.38 P2B
        self._mem_path     = memory_path
        # A1 WorkflowMemory shortcuts — set từ workflow_executor trước mỗi run
        self._wm_shortcuts: "object | None" = None
        # A2: CrossDomain Learning — lazy init
        self._cdm: "CrossDomainMemory | None" = None
        # B2: Dynamic Confidence Calibration — lazy init
        self._conf_hist: "ConfidenceHistory | None" = None

        # v2.4 C2: ClickStrategySelector — strategy cascade manager
        self._strategy_selector: "object | None" = None
        if _HAS_CSS:
            self._strategy_selector = get_strategy_selector()
            # Register available components
            _available = ["click_mem", "vlm"]
            if self._zoom:      _available.append("zoom")
            if self._consensus: _available.append("consensus")
            if self._crop_loc:  _available.append("crop_loc")
            self._strategy_selector.set_available(_available)
        if _VISION_ENHANCED and vision_mode in ("enhanced", "ultra"):
            self._init_enhanced_vision()

    @classmethod
    def from_config(cls, ipc_client=None, screenshot_region=None, semantic_mem=None,
                    resource_guard=None):
        """v9.35: Thêm resource_guard để VLMRouter tự adaptive throttle."""
        key = ""
        try:
            from config.config_loader import PhidipusConfig
            key = PhidipusConfig().gemini_api_key
        except Exception:
            pass
        return cls(
            ipc_client=ipc_client,
            gemini_api_key=key,
            screenshot_region=screenshot_region,
            semantic_mem=semantic_mem,
            resource_guard=resource_guard,
        )

    async def _ensure_screen(self):
        if not self._screen:
            self._screen = await _get_screen_info()

    # ── Screenshot — v9.23: window-scoped ──────────────────────────────

    async def screenshot(self) -> bytes | None:
        """
        v9.23: Chụp CHỈ vùng Chrome window (window-scoped screenshot).
        VLM không nhìn thấy Desktop/Dock/app khác → tọa độ chính xác hơn,
        ít token rác hơn, Gemini call nhanh hơn.
        """
        await self._ensure_screen()
        if self._region:
            self._last_img = await _take_screenshot(self._region)
        else:
            bounds = await get_chrome_window_bounds()
            region = (bounds["x"], bounds["y"], bounds["w"], bounds["h"])
            self._last_img = await _take_screenshot(region)
        return self._last_img

    # ── Core vision ops ─────────────────────────────────────────────────

    def _init_enhanced_vision(self) -> None:
        """Lazy init v9.28 enhanced modules."""
        try:
            from social.click_verifier import ClickVerifier  # BUG #4 FIX: C2 path
            from vision.click_memory import ClickMemory
            from vision.multimodal_consensus import MultiModalConsensus
            from vision.crop_locator import CropLocator

            # Click memory (persistent cache)
            mem_path = self._mem_path or "data/plans/click_memory.json"
            self._click_mem = ClickMemory(memory_path=mem_path)

            # Verifier (uses our JS exec channel)
            self._verifier = ClickVerifier(
                js_exec_fn=self._js_exec_safe,
                ui_settle_s=0.5,
            )

            # Consensus (JS + VLM)
            self._consensus = MultiModalConsensus(
                js_exec_fn=self._js_exec_safe,
                vlm_fn=self._vlm.call,
                screenshot_fn=self.screenshot,
                pixel_threshold=40,
                parallel=(self._vision_mode == "ultra"),
            )

            # Crop locator
            self._crop_loc = CropLocator(
                vlm_fn=self._vlm.call,
                screenshot_fn=self.screenshot,
                crop_radius=220,
            )

            # v9.30: ZoomActionEngine — 3-pass adaptive zoom, zero API cost
            # screen_w=0/screen_h=0 → ZoomActionEngine calls _detect_screen_size()
            # (PIL.ImageGrab → AppKit NSScreen → xrandr → 1440×900 fallback).
            # Never hardcode 1920×1080 — wrong on every MacBook and 2K/4K display.
            self._zoom = ZoomPresets.small_button(
                vlm_fn=self._vlm.call,
                screenshot_fn=self.screenshot,
                # screen_w/h intentionally omitted → defaults to 0 → auto-detect
            )

            # v9.31: ReflectionSupervisor — tự phân tích lý do fail
            self._reflection = ReflectionSupervisor(
                vlm_fn=self._vlm.call,
                screenshot_fn=self.screenshot,
                use_vision=True,
            )

            # v9.38 P2B: UnifiedSOMEngine — Desktop SOM cho native apps
            try:
                from vision.desktop_som import UnifiedSOMEngine
                self._desktop_som = UnifiedSOMEngine(
                    vlm=self._vlm,
                    ipc_client=self._ipc,
                )
                _vlog("🖥️", "Desktop SOM (AX + Tkinter overlay) initialized")
            except Exception as som_exc:
                _vlog("⚠️ ", f"Desktop SOM init skip: {som_exc}")

            _vlog("🚀", f"v9.38 vision enhanced ({self._vision_mode} mode)")
        except Exception as exc:
            _vlog("⚠️ ", f"Enhanced vision init failed: {exc}")

    def enable_enhanced_vision(self, mode: str = "enhanced", memory_path: str | None = None) -> None:
        """
        Bật enhanced vision sau khi khởi tạo.
        mode: "enhanced" = JS+VLM consensus + memory cache
              "ultra"    = full 3-way consensus (chậm hơn nhưng chính xác nhất)
        """
        self._vision_mode = mode
        if self._mem_path is None and memory_path:
            self._mem_path = memory_path
        if _VISION_ENHANCED:
            self._init_enhanced_vision()
        else:
            _vlog("⚠️ ", "Enhanced vision modules not available (check import)")

    def apply_wm_shortcuts(self, shortcuts: "object | None") -> None:
        """
        A1 WorkflowMemory — áp dụng shortcuts đã học trước mỗi workflow run.

        Được gọi từ WorkflowExecutor sau khi load shortcuts từ TrajectoryDB.
        VisionActor dùng shortcuts để tự điều chỉnh strategy:

          preferred_upload_tier → upload_image_drag_drop() bắt đầu từ tier đúng
          reflection_helps      → ReflectionSupervisor luôn dùng vision=True
                                  (đã là default; nếu shortcuts xác nhận → giảm
                                  retries mù từ 3 → 1 để tiết kiệm thời gian)

        Shortcuts được reset về None bởi WorkflowExecutor sau khi run xong
        (qua self._run_shortcuts = None), nhưng VisionActor giữ ref này
        trong suốt workflow.
        """
        self._wm_shortcuts = shortcuts
        if shortcuts is None:
            return
        tier = getattr(shortcuts, "preferred_upload_tier", 1)
        if tier and tier != 1:
            _vlog("🧭", f"WorkflowMemory [preferred_upload_tier={tier}]: "
                        f"upload sẽ bắt đầu từ Tier {tier}")
        if getattr(shortcuts, "reflection_helps", False):
            _vlog("🧭", "WorkflowMemory [reflection_helps]: "
                        "ReflectionSupervisor vision=True confirmed beneficial")

    async def _js_exec_safe(self, js: str) -> str:
        """
        Execute JS in Chrome, trả về string result.

        v9.34 A1 — 2-tier:
          Tier 1: IPC browser_execute_js → L2 daemon → AppleScript
          Tier 2: Direct AppleScript fallback
        Không raise exception — safe cho click_verifier và consensus.
        """
        # Tier 1: IPC
        if self._ipc:
            try:
                resp = await self._ipc.send_action("browser_execute_js", {
                    "js_code": js,
                    "timeout_s": 6.0,
                })
                # FIX v4.3: resp is an IPCResponse object (never a dict), so the
                # old isinstance(resp, dict) check always fell through to Tier 2.
                if resp is not None:
                    if getattr(resp, "success", False):
                        val = getattr(resp, "result", "")
                        return "" if val is None else str(val).strip()
                    return ""
            except Exception:
                pass  # fall through to Tier 2

        # Tier 2: Direct AppleScript (khi không có IPC)
        import platform as _plat
        if _plat.system() != "Darwin":
            return ""
        try:
            import subprocess as _sp
            js_escaped = js.replace("\\", "\\\\").replace('"', '\\"')
            script = (
                'tell application "Google Chrome"\n'
                '    try\n'
                f'        set r to execute active tab of front window javascript "{js_escaped}"\n'
                '        if r is missing value then return ""\n'
                '        return r as string\n'
                '    on error\n'
                '        return ""\n'
                '    end try\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                _sp.run, ["osascript", "-e", script],
                capture_output=True, text=True, timeout=8,
            )
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except Exception:
            return ""

    async def find_and_click_v2(
        self,
        query: str,
        domain: str = "",
        action_key: str = "",
        js_selectors: list[str] | None = None,
        js_verify: list[str] | None = None,
        vision_verify: str = "",
        window_bounds: dict | None = None,
        anchor_coords: tuple[int, int] | None = None,
        retries: int = 2,
        strategy_hint: str = "",
    ) -> dict:
        """
        Enhanced find_and_click với 4 improvements (v9.28):
          #1 Post-Action Verification Loop (ClickVerifier)
          #7 Click Memory Cache (ClickMemory)
          #8 Multi-Modal Consensus (MultiModalConsensus)
          #4 Crop-based locator (CropLocator)

        Args:
            query:         Mô tả element (ví dụ: "Post button")
            domain:        Domain của trang (ví dụ: "facebook.com") — dùng cho cache
            action_key:    Cache key (ví dụ: "fb_post_button") — dùng cho cache
            js_selectors:  CSS selectors để thử DOM-first
            js_verify:     JS assertions để verify sau khi click
            vision_verify: Mô tả kết quả mong đợi để verify bằng VLM
            window_bounds: Chrome window bounds
            anchor_coords: Tọa độ anchor để dùng crop strategy
            retries:       Số lần retry khi verify fail
            strategy_hint: A1 WorkflowMemory hint — điều chỉnh thứ tự strategy:
                           "zoom_first" → skip S2 Consensus, ưu tiên S1.5 ZoomAction
                                          (khi TrajectoryDB thấy consensus tốn nhiều VLM)

        Returns:
            dict: {
                "success": bool,
                "x": int, "y": int,
                "confidence": float,
                "method": str,          # "memory" | "consensus" | "crop" | "vlm"
                "verified": bool,
                "verify_confidence": float,
                "memory_hit": bool,
                "latency_ms": int,
            }
        """
        import time as _time
        t0 = _time.time()

        bounds = window_bounds or {}
        if not bounds:
            try:
                b = await get_chrome_window_bounds()
                bounds = {"x": b.get("x",0), "y": b.get("y",0),
                         "w": b.get("w",1440), "h": b.get("h",900)}
            except Exception:
                bounds = {"x": 0, "y": 0, "w": 1440, "h": 900}

        # ── v2.4 C2: ClickStrategySelector — log available strategies for this call ──
        if _HAS_CSS and self._strategy_selector:
            _selected = self._strategy_selector.select_strategies(
                budget_ms=5000,
                strategy_hint=strategy_hint,
                has_anchor=bool(anchor_coords),
                has_js_selectors=bool(js_selectors),
            )
            _strat_names = [s.tier for s in _selected]
            _vlog("🎯", f"C2 Strategies: {' → '.join(_strat_names)} (budget=5000ms)")

        # ── B1: Budget-based timeout (v2.4) — FIX BUG#5: outside retry loop
        # FIX BUG#10: Use extracted BudgetTracker from click_strategy.py
        try:
            from social.click_strategy import BudgetTracker
            _budget = BudgetTracker(total_ms=5000)
            def _remaining_ms():
                return _budget.remaining_ms
        except ImportError:
            import time as _btime
            _budget_ms = 5000
            _budget_t0 = _btime.perf_counter()
            def _remaining_ms():
                return _budget_ms - (_btime.perf_counter() - _budget_t0) * 1000

        for attempt in range(retries + 1):
            if attempt > 0:
                await asyncio.sleep(1.5)
                _vlog("🔄", f"find_and_click_v2 retry {attempt}: '{query[:40]}'")

            x, y, conf, method, mem_hit = 0, 0, 0.0, "none", False

            # ── Strategy 1: ClickMemory cache (#7) ──────────────────
            if self._click_mem and domain and action_key:
                mem_result = self._click_mem.lookup(domain, action_key)
                if mem_result.found:
                    abs_x = int(mem_result.rx * bounds.get("w", 1440)) + bounds.get("x", 0)
                    abs_y = int(mem_result.ry * bounds.get("h", 900))  + bounds.get("y", 0)
                    x, y, conf, method, mem_hit = abs_x, abs_y, mem_result.confidence, "memory", True
                    _vlog("⚡", f"Memory hit: ({x},{y}) conf={conf:.0%}")

            # ── A2: CrossDomain hint — khi S1 miss ───────────────────
            # Lấy hint từ domain khác (vd: Instagram học từ Facebook).
            # Hint được dùng để: (a) merge selector_hints vào js_selectors,
            # (b) inject vlm_prompt_hint vào query cho S4 VLM.
            _cdm_prompt_hint = ""
            if (x == 0 and y == 0) and _HAS_CDM and domain:
                if self._cdm is None:
                    self._cdm = get_cross_domain_memory()
                cdm_hint = self._cdm.get_hint(
                    description=query,
                    target_domain=domain,
                    action_key=action_key,
                )
                if cdm_hint.found:
                    # Merge selector_hints từ domain khác vào js_selectors
                    if cdm_hint.selector_hints:
                        merged = list(js_selectors or [])
                        for sel in cdm_hint.selector_hints:
                            if sel not in merged:
                                merged.append(sel)
                        js_selectors = merged
                        _vlog("🌐", f"A2 CrossDomain: +{len(cdm_hint.selector_hints)} selectors "
                                    f"from {cdm_hint.source_domains}")
                    # Lưu prompt hint cho S4 VLM
                    _cdm_prompt_hint = cdm_hint.vlm_prompt_hint()

            # ── Strategy 1.5: ZoomAction 3-pass (#zoom) ──────────────
            # Chạy khi memory miss VÀ có ZoomEngine
            # B1: Skip if budget < 3000ms (ZoomAction takes 3-8s)
            if (x == 0 and y == 0) and self._zoom and _remaining_ms() > 3000:
                zoom_r = await self._zoom.locate(
                    query=query,
                    screen_w=bounds.get("w") or None,
                    screen_h=bounds.get("h") or None,
                )
                if zoom_r.ok and zoom_r.confidence >= 0.82:
                    # Offset theo chrome window position
                    abs_x = zoom_r.x + bounds.get("x", 0)
                    abs_y = zoom_r.y + bounds.get("y", 0)
                    x, y, conf = abs_x, abs_y, zoom_r.confidence
                    method = f"zoom_{zoom_r.final_pass.name.lower()}"
                    _vlog("🔍", f"ZoomAction hit: ({x},{y}) "
                          f"conf={conf:.0%} passes={zoom_r.passes_used}")
                    # C2: record strategy success
                    if self._strategy_selector:
                        self._strategy_selector.record_attempt("zoom_action", True, zoom_r.latency_ms if hasattr(zoom_r, "latency_ms") else 5000)

            # ── Strategy 2: Multi-Modal Consensus (#8) ───────────────
            # A1 WorkflowMemory: skip_consensus khi strategy_hint="zoom_first"
            # B1: Skip if budget < 2000ms
            _skip_consensus = (strategy_hint == "zoom_first") or _remaining_ms() < 2000
            if _skip_consensus and (x == 0 and y == 0) and _remaining_ms() < 2000:
                _vlog("⏱️", f"Budget {_remaining_ms():.0f}ms — skip S2 Consensus")
            elif _skip_consensus and (x == 0 and y == 0):
                _vlog("🧭", f"WorkflowMemory [prefer_zoom]: skip S2 Consensus → S3/S4")
            if (x == 0 and y == 0) and self._consensus and not _skip_consensus:
                c_result = await self._consensus.find(
                    query=query,
                    js_selectors=js_selectors,
                    window_bounds=bounds,
                )
                if c_result.consensus_ok:
                    x, y, conf, method = c_result.x, c_result.y, c_result.confidence, c_result.method

            # ── Strategy 3: Crop-based locator (#4) ──────────────────
            # B1: Skip if budget < 1500ms
            if (x == 0 and y == 0) and self._crop_loc and anchor_coords and _remaining_ms() > 1500:
                crop_result = await self._crop_loc.locate_near(
                    target_query=query,
                    anchor_x=anchor_coords[0],
                    anchor_y=anchor_coords[1],
                    screen_w=bounds.get("w") or None,
                    screen_h=bounds.get("h") or None,
                )
                if crop_result.found:
                    x, y, conf, method = crop_result.x, crop_result.y, crop_result.confidence, crop_result.method

            # ── Strategy 4: Fallback VLM (original find_element) ─────
            # A2: inject CrossDomain hint vào query để VLM biết vùng tìm
            # B1: VLM is expensive (15-25s) — only if budget allows or last resort
            if x == 0 and y == 0:
                if _remaining_ms() > 1000:
                    _vlm_query = f"{query} {_cdm_prompt_hint}".strip() if _cdm_prompt_hint else query
                    r = await self.find_element(_vlm_query)
                    if r.found:
                        x, y, conf, method = r.x, r.y, r.confidence, f"vlm_{r.tier}"
                        # C2: record strategy success
                        if self._strategy_selector:
                            self._strategy_selector.record_attempt("vlm_fullscreen", True, 0)
                else:
                    _vlog("⏱️", f"Budget exhausted ({_remaining_ms():.0f}ms) — skip VLM fallback")

            if x == 0 and y == 0:
                _vlog("❌", f"All strategies failed for '{query[:40]}' (attempt {attempt+1}, budget={_remaining_ms():.0f}ms)")
                continue

            # ── B2 Dynamic Confidence Calibration ─────────────────────
            # Điều chỉnh conf dựa trên lịch sử thực tế của element này.
            # Approach kết hợp: trust_bonus (per element history) +
            # force_verify (khi VLM tự tin nhưng thực tế hay fail).
            # KHÔNG thay đổi CONFIDENCE_THRESHOLD (R-21 invariant).
            _b2_force_verify = False
            if _HAS_CONF_HIST and domain and action_key and method != "memory":
                if self._conf_hist is None:
                    self._conf_hist = get_confidence_history()
                adj = self._conf_hist.adjust(domain, action_key, conf)
                if adj.trust_bonus != 0.0:
                    conf = adj.confidence   # adjusted confidence
                if adj.force_verify:
                    _b2_force_verify = True
                    _vlog("📊", f"B2 ConfCalib [force_verify]: {domain}:{action_key} "
                                f"VLM tự tin nhưng thường fail → bật verify bắt buộc")

            # ── Click ─────────────────────────────────────────────────
            clicked = await self._click(x, y, conf, query[:50])
            if not clicked:
                _vlog("⚠️ ", f"Click failed at ({x},{y})")
                continue

            # ── Verify (#1) ──────────────────────────────────────────
            verified = False
            verify_conf = 0.0
            # B2: force_verify khi ConfidenceHistory biết element hay fail dù conf cao
            _should_verify = bool(js_verify or vision_verify or _b2_force_verify)
            if self._verifier and _should_verify:
                v_result = await self._verifier.verify(
                    js_assertions=js_verify,
                    vision_fn=self.ask_vision if (vision_verify or _b2_force_verify) else None,
                    vision_desc=vision_verify or f"Action succeeded on {domain}",
                )
                verified = v_result.ok
                verify_conf = v_result.confidence

                if not verified and attempt < retries:
                    # v9.31: Reflection Supervisor — tự phân tích lý do fail
                    # Thay vì retry mù quáng → hỏi VLM "Tại sao fail? Làm gì tiếp?"
                    if self._reflection:
                        ref = await self._reflection.reflect(
                            action="click",
                            x=x, y=y,
                            query=query,
                            verify_result=v_result,
                        )

                        if ref.next.value == "already_done":
                            # VLM/Rule xác nhận action đã thành công thật sự
                            # Verify là false-positive (JS không reach Chrome)
                            _vlog("✅", f"Reflection: ALREADY_DONE — '{ref.reason[:60]}'")
                            verified = True
                            verify_conf = ref.confidence
                            # Không continue → thoát ra, save memory bình thường

                        elif ref.next.value == "retry_scroll":
                            # Cần scroll trước khi retry
                            _vlog("🔄", f"Reflection: SCROLL_{ref.scroll_dir.upper()} "
                                  f"{ref.scroll_px}px → '{ref.reason[:50]}'")
                            if self._ipc:
                                await self._ipc.send_action("mouse_scroll", {
                                    "direction": ref.scroll_dir,
                                    "amount": ref.scroll_px,
                                })
                            await asyncio.sleep(0.8)
                            # Reset coords để retry với strategy mới
                            x, y = 0, 0
                            if mem_hit and self._click_mem and domain and action_key:
                                self._click_mem.record_failure(domain, action_key, "scroll_needed")
                            continue

                        elif ref.next.value == "retry_wait":
                            # Cần chờ trước khi retry
                            wait = max(0.5, min(ref.wait_s, 10.0))
                            _vlog("⏳", f"Reflection: WAIT {wait:.1f}s → '{ref.reason[:50]}'")
                            await asyncio.sleep(wait)
                            if mem_hit and self._click_mem and domain and action_key:
                                self._click_mem.record_failure(domain, action_key, "wait_needed")
                            continue

                        elif ref.next.value == "retry_different":
                            # Thử strategy khác — reset coords → loop sẽ skip memory, dùng ZoomAction
                            _vlog("🔄", f"Reflection: DIFFERENT_STRATEGY → '{ref.reason[:50]}'")
                            x, y = 0, 0  # force re-locate với strategy khác
                            if mem_hit and self._click_mem and domain and action_key:
                                self._click_mem.record_failure(domain, action_key, "strategy_failed")
                            mem_hit = False  # không dùng memory nữa trong lần này
                            continue

                        elif ref.next.value == "escalate":
                            # Không tự fix được — dừng sớm, không tiếp tục retry
                            _vlog("🚨", f"Reflection: ESCALATE → '{ref.reason[:60]}'")
                            if self._click_mem and domain and action_key:
                                self._click_mem.record_failure(domain, action_key, "escalated")
                            break  # thoát loop, trả về failed

                        else:
                            # RETRY_SAME — click lại cùng tọa độ
                            _vlog("🔄", f"Reflection: RETRY_SAME → '{ref.reason[:50]}'")
                            if mem_hit and self._click_mem and domain and action_key:
                                self._click_mem.record_failure(domain, action_key, "retry_same")
                            continue

                    else:
                        # Không có reflection → fallback: invalidate cache và retry
                        if mem_hit and self._click_mem and domain and action_key:
                            self._click_mem.record_failure(domain, action_key, "verify_failed")
                        continue
            else:
                verified = True  # không có verify → assume ok
                verify_conf = conf

            # ── Save to memory (#7) ──────────────────────────────────
            # S1 FIX: Nâng threshold cache từ conf > 0.70 → conf >= 0.90 cho unverified.
            # Lý do: conf > 0.70 unverified có thể cache tọa độ sai khi element đã di chuyển.
            # Chỉ cache khi: (a) đã verify qua ClickVerifier, HOẶC (b) conf cực cao (≥ 0.90).
            # Tham chiếu: PDF audit S1 — "record_success() với OR condition nguy hiểm".
            if self._click_mem and domain and action_key and (verified or conf >= 0.90):
                rx = (x - bounds.get("x", 0)) / max(bounds.get("w", 1440), 1)
                ry = (y - bounds.get("y", 0)) / max(bounds.get("h", 900), 1)
                self._click_mem.record_success(
                    domain=domain,
                    action_key=action_key,
                    rx=rx, ry=ry,
                    selectors=js_selectors,
                    verified=verified,
                )
                # A2: CrossDomain Learning — học pattern từ click thành công
                if _HAS_CDM and domain:
                    if self._cdm is None:
                        self._cdm = get_cross_domain_memory()
                    self._cdm.record(
                        domain=domain,
                        action_key=action_key,
                        description=query,
                        rx=rx, ry=ry,
                        selectors=js_selectors or [],
                    )
                # B2: Confidence Calibration — ghi nhận conf + outcome thực tế
                if _HAS_CONF_HIST and domain and action_key:
                    if self._conf_hist is None:
                        self._conf_hist = get_confidence_history()
                    self._conf_hist.record(
                        domain=domain,
                        action_key=action_key,
                        conf=conf,
                        success=verified if (js_verify or vision_verify) else True,
                        strategy=method,
                    )

            total_ms = int((_time.time() - t0) * 1000)
            _vlog("✅", f"v2 click: ({x},{y}) method={method} verified={verified} "
                  f"conf={conf:.0%} {total_ms}ms")

            return {
                "success":          True,
                "x":                x, "y": y,
                "confidence":       round(conf, 3),
                "method":           method,
                "verified":         verified,
                "verify_confidence": round(verify_conf, 3),
                "memory_hit":       mem_hit,
                "latency_ms":       total_ms,
            }

        total_ms = int((_time.time() - t0) * 1000)
        return {
            "success": False, "x": 0, "y": 0,
            "confidence": 0.0, "method": "all_failed",
            "verified": False, "verify_confidence": 0.0,
            "memory_hit": False, "latency_ms": total_ms,
        }

    async def find_element(self, query: str, use_cached=False) -> VisionResult:
        """
        v9.23: 4-tier strategy
          L0: SemanticMemory selector cache (0ms)
          L1: VLM với window-scoped screenshot
          L2: parse ratio relative to Chrome window size
          L3: cộng Chrome offset → absolute coords
        """
        await self._ensure_screen()
        bounds = await get_chrome_window_bounds()

        # L0: SemanticMemory cache
        if self._semantic_mem:
            try:
                cache_key = f"vision_selector_{query[:60]}"
                cached = self._semantic_mem.query(cache_key)
                if cached and isinstance(cached, list) and cached:
                    entry = cached[0]
                    age = time.time() - entry.get("ts", 0)
                    if age < 3600:
                        abs_x = int(entry["rx"] * bounds["w"]) + bounds["x"]
                        abs_y = int(entry["ry"] * bounds["h"]) + bounds["y"]
                        _vlog("⚡", f"[cache] '{query[:40]}' → ({abs_x},{abs_y}) age={int(age)}s")
                        return VisionResult(found=True, x=abs_x, y=abs_y,
                                            confidence=0.92, label="cached",
                                            tier="semantic_cache")
            except Exception:
                pass

        # L1+L2: VLM
        img = self._last_img if (use_cached and self._last_img) else await self.screenshot()
        if not img:
            return VisionResult(found=False, tier="error", raw="no_screenshot")
        prompt = _FIND_NORMALIZED.format(query=query, W="", H="")
        raw, tier = await self._vlm.call(img, prompt)
        _vlog("👁️ ", f"[{tier}] find '{query[:40]}': {raw[:80]}")
        r = _parse(raw, screen_w=bounds["w"], screen_h=bounds["h"])
        r.tier = tier

        # L3: cộng offset
        if r.found:
            raw_rx = r.x / max(bounds["w"], 1)
            raw_ry = r.y / max(bounds["h"], 1)
            r.x += bounds["x"]
            r.y += bounds["y"]
            _vlog("📐", f"Chrome-relative → absolute: ({r.x},{r.y}) [offset +{bounds['x']},+{bounds['y']}]")
            if self._semantic_mem and r.confidence >= 0.75:
                try:
                    cache_key = f"vision_selector_{query[:60]}"
                    self._semantic_mem.learn("vision_selector_cache", {
                        "query": query, "rx": raw_rx, "ry": raw_ry,
                        "confidence": r.confidence, "ts": time.time(),
                    })
                except Exception:
                    pass
        return r

    async def ask_vision(self, question: str, use_cached=False) -> bool:
        """Yes/no question về màn hình."""
        img = self._last_img if (use_cached and self._last_img) else await self.screenshot()
        if not img: return False
        raw, tier = await self._vlm.call(img, _YESNO.format(q=question))
        _vlog("👁️ ", f"[{tier}] '{question[:50]}' → {raw[:30]}")
        return "yes" in raw.lower()

    async def verify_action(self, expected_result: str) -> bool:
        """
        See-Do-Check: Chụp màn hình mới và verify kết quả action vừa làm.
        Ví dụ: verify_action("Facebook post dialog is open")
        """
        await asyncio.sleep(0.8)  # Chờ UI cập nhật
        img = await self.screenshot()
        if not img: return False
        prompt = _VERIFY.format(expected=expected_result)
        raw, _ = await self._vlm.call(img, prompt)
        success = raw.strip().lower().startswith("yes")
        _vlog("🔍", f"Verify '{expected_result[:40]}': {'✅' if success else '❌'} — {raw[:60]}")
        return success

    async def describe(self, focus="the main content") -> str:
        img = await self.screenshot()
        if not img: return ""
        raw, _ = await self._vlm.call(img, _DESC.format(focus=focus))
        return raw

    # ── SmartWait — poll vision thay vì hardcode sleep ──────────────────

    async def smart_wait(
        self,
        condition: str,
        timeout: float = 30.0,
        poll_interval: float = 2.0,
        description: str = "",
    ) -> bool:
        """
        Poll cho đến khi vision xác nhận condition là true.
        Thay thế mọi asyncio.sleep(Ns) hardcode.

        Ví dụ:
            await actor.smart_wait("ChatGPT generated image is visible", timeout=60)
            await actor.smart_wait("Facebook post dialog is open", timeout=15)
            await actor.smart_wait("File picker / Finder dialog is visible", timeout=10)
        """
        label = description or condition[:50]
        deadline = time.time() + timeout
        attempt  = 0
        t0       = time.time()
        while time.time() < deadline:
            attempt += 1
            await asyncio.sleep(poll_interval)
            elapsed = int(time.time() - t0)
            _vlog("⏳", f"SmartWait #{attempt} ({elapsed}s/{int(timeout)}s): {label[:40]}")
            if await self.ask_vision(condition):
                _vlog("✅", f"SmartWait OK: '{label[:40]}' ({elapsed}s)")
                return True
        _vlog("⚠️ ", f"SmartWait timeout ({timeout}s): '{label[:40]}'")
        return False

    # ── Find & Click with See-Do-Check ──────────────────────────────────

    async def find_and_click(
        self,
        query: str,
        verify: str = "",
        retries: int = 2,
        retry_delay: float = 2.0,
        min_confidence: float = 0.5,
    ) -> bool:
        """
        Tìm element, click, rồi verify kết quả (See-Do-Check).
        verify: mô tả kết quả mong đợi sau khi click (optional).
        """
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(retry_delay)
                _vlog("🔄", f"Retry {attempt}: '{query[:40]}'")

            r = await self.find_element(query)
            if not r.found or r.confidence < min_confidence:
                _vlog("⚠️ ", f"Not found: '{query[:40]}' conf={r.confidence:.0%}")
                continue

            _vlog("🎯", f"Found at ({r.x},{r.y}) conf={r.confidence:.0%} [{r.tier}]")
            clicked = await self._click(r.x, r.y, r.confidence, query[:50])

            if not clicked:
                continue

            # See-Do-Check
            if verify:
                ok = await self.verify_action(verify)
                if ok:
                    return True
                _vlog("⚠️ ", f"Verify failed — retry click")
                continue

            return True

        return False

    # ── Click ──────────────────────────────────────────────────────────

    async def _click(self, x: int, y: int, confidence=1.0, label="") -> bool:
        """
        Click tọa độ (x, y).
        Giải pháp 2: Hover nhẹ trước khi click → UI có thể cần hover để hiện button.
        """
        # Hover trước (giúp hiện overlay buttons như Save trên ChatGPT)
        if self._ipc:
            try:
                await self._ipc.send_action("mouse_move", {"x": int(x), "y": int(y)})
                await asyncio.sleep(0.3)
            except Exception:
                pass

        if self._ipc:
            try:
                await self._ipc.send_action("mouse_click", {
                    "x": int(x), "y": int(y), "button": "left",
                    "confidence": round(float(confidence), 3),
                    "target_label": label,
                })
                _vlog("🖱️ ", f"IPC hover+click ({x},{y})")
                return True
            except Exception as e:
                _vlog("⚠️ ", f"IPC click: {e}")
        try:
            p = await asyncio.to_thread(subprocess.run, ["cliclick", f"m:{x},{y}", f"c:{x},{y}"],
                                        capture_output=True, timeout=5)
            if p.returncode == 0:
                _vlog("🖱️ ", f"cliclick hover+click ({x},{y})")
                return True
        except FileNotFoundError: pass
        except Exception: pass
        try:
            script = (
                f'tell application "System Events"\n'
                f'    set mouseLocation to {{{x}, {y}}}\n'
                f'    click at mouseLocation\n'
                f'end tell'
            )
            await asyncio.to_thread(subprocess.run, ["osascript","-e",script],
                                    capture_output=True, timeout=5)
            _vlog("🖱️ ", f"osascript click ({x},{y})")
            return True
        except Exception as e:
            _vlog("❌", f"All click failed: {e}")
            return False

    # ── Drag & Drop ─────────────────────────────────────────────────────

    async def drag_file_to_element(
        self,
        file_path: str,
        target_query: str,
    ) -> bool:
        """
        Drag & drop file vào element trên màn hình.
        Ổn định hơn Finder dialog cho upload ảnh Facebook.

        Flow:
          1. Vision tìm tọa độ target element (vùng post dialog)
          2. IPC mouse_drag từ file (cần file visible trên Desktop)
             hoặc dùng AppleScript set_clipboard + drop
        """
        # Tìm target
        r = await self.find_element(target_query)
        if not r.found:
            _vlog("⚠️ ", f"Drag target not found: '{target_query[:40]}'")
            return False

        target_x, target_y = r.x, r.y
        _vlog("🎯", f"Drag target: ({target_x},{target_y})")

        # IPC mouse_drag nếu có
        if self._ipc:
            try:
                # Cần source coords — nếu file trên Desktop, tìm icon
                file_name = Path(file_path).name
                src = await self.find_element(f"file icon named {file_name} on Desktop")
                if src.found:
                    await self._ipc.send_action("mouse_drag", {
                        "from_x": src.x, "from_y": src.y,
                        "to_x": target_x, "to_y": target_y,
                        "duration": 0.8,
                    })
                    _vlog("🖱️ ", f"IPC drag {file_name} → ({target_x},{target_y})")
                    return await self.verify_action(
                        f"image is attached to the post (thumbnail visible)"
                    )
            except Exception as e:
                _vlog("⚠️ ", f"Drag failed: {e}")

        # Fallback: AppleScript drag từ path
        try:
            posix = str(Path(file_path).resolve())
            script = (
                f'set theFile to POSIX file "{posix}"\n'
                f'tell application "System Events"\n'
                f'    drag theFile to {{{target_x},{target_y}}}\n'
                f'end tell'
            )
            await asyncio.to_thread(subprocess.run, ["osascript","-e",script],
                                    capture_output=True, timeout=10)
            _vlog("🖱️ ", f"AppleScript drag → ({target_x},{target_y})")
            return await self.verify_action("image thumbnail visible in post")
        except Exception as e:
            _vlog("❌", f"Drag fallback: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════
# Chrome tab helper — focus tab by title
# ══════════════════════════════════════════════════════════════════════

async def focus_chrome_tab(title_contains: str) -> bool:
    """
    AppleScript: focus Chrome tab có title chứa chuỗi cho trước.
    Tránh agent bị lạc khi có nhiều tab mở.
    """
    script = (
        f'tell application "Google Chrome"\n'
        f'    activate\n'
        f'    set tabFound to false\n'
        f'    repeat with w in windows\n'
        f'        repeat with t in tabs of w\n'
        f'            if title of t contains "{title_contains}" then\n'
        f'                set active tab index of w to index of t\n'
        f'                set index of w to 1\n'
        f'                set tabFound to true\n'
        f'                exit repeat\n'
        f'            end if\n'
        f'        end repeat\n'
        f'        if tabFound then exit repeat\n'
        f'    end repeat\n'
        f'    return tabFound as string\n'
        f'end tell'
    )
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["osascript", "-e", script],
            capture_output=True, text=True, timeout=8,
        )
        ok = "true" in r.stdout.lower()
        _vlog("🗂️ ", f"Tab focus '{title_contains}': {'✅' if ok else '❌'}")
        return ok
    except Exception as e:
        _vlog("⚠️ ", f"Tab focus error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
# JS helpers for Facebook — insertText bypass clipboard
# ══════════════════════════════════════════════════════════════════════

async def js_insert_text_facebook(text: str, chrome_tab_title="Facebook") -> bool:
    """
    Inject text vào Facebook post box bằng JS insertText.
    Không phụ thuộc clipboard — an toàn khi user đang dùng máy.

    Trả về True nếu inject thành công.
    """
    # Escape cho AppleScript string
    safe = text.replace("\\","\\\\").replace('"','\\"').replace("\n","\\n")
    js = (
        "(function(){"
        "  var focused = document.activeElement;"
        "  if(!focused || focused === document.body){"
        "    focused = document.querySelector('[role=textbox],[contenteditable=true]');"
        "    if(focused) focused.focus();"
        "  }"
        "  if(!focused) return 'no_focus';"
        f"  var txt = \"{safe[:3000]}\";"  # limit 3000 chars per inject
        "  document.execCommand('selectAll', false, null);"
        "  document.execCommand('delete', false, null);"
        "  document.execCommand('insertText', false, txt);"
        "  return 'ok:' + (focused.innerText||focused.textContent||'').length;"
        "})()"
    )
    script = (
        'tell application "Google Chrome"\n'
        f'  set r to execute active tab of front window javascript "{js}"\n'
        '  return r as string\n'
        'end tell'
    )
    try:
        # Ensure đúng tab
        await focus_chrome_tab(chrome_tab_title)
        await asyncio.sleep(0.3)
        r = await asyncio.to_thread(
            subprocess.run, ["osascript", "-e", script],
            capture_output=True, text=True, timeout=8,
        )
        result = r.stdout.strip()
        success = result.startswith("ok:")
        chars = result.split(":")[-1] if success else "0"
        _vlog("📝", f"JS insertText: {result} ({chars} chars)")
        return success
    except Exception as e:
        _vlog("⚠️ ", f"JS insertText error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
# ChatGPT Vision Helper
# ══════════════════════════════════════════════════════════════════════

class ChatGPTVisionHelper:
    """VisionActor chuyên dụng cho ChatGPT + Facebook workflow."""

    def __init__(self, ipc_client=None, gemini_api_key="",
                 vlm_model="qwen3-vl:8b-instruct"):  # vlm_model kept for compat
        key = gemini_api_key
        if not key:
            try:
                from config.config_loader import PhidipusConfig
                key = PhidipusConfig().gemini_api_key
            except Exception: pass
        self._actor = VisionActor(ipc_client=ipc_client, gemini_api_key=key)
        # v9.28: Bật Enhanced Vision ngay khi khởi tạo
        # → find_and_click_v2 dùng ClickMemory cache + MultiModal consensus
        self._actor.enable_enhanced_vision(mode="enhanced")

    # ── ChatGPT ────────────────────────────────────────────────────────

    async def wait_for_image_ready(self, timeout_s=60) -> bool:
        """SmartWait thay cho hardcode sleep — dừng ngay khi ảnh xuất hiện."""
        return await self._actor.smart_wait(
            condition=(
                "Is there a fully generated AI image (artwork/photo/illustration) "
                "visible on the ChatGPT page? Not a loading spinner or progress bar."
            ),
            timeout=timeout_s,
            poll_interval=6.0,
            description="ChatGPT image ready",
        )

    # Alias for backward compat with chatgpt_image_gen.py
    async def wait_for_image_with_vision(self, timeout_s=60, poll_interval=8.0) -> bool:
        return await self.wait_for_image_ready(timeout_s=timeout_s)

    async def describe_screen(self, focus='ChatGPT page') -> str:
        return await self.describe_state()

    async def click_image_to_expand(self) -> bool:
        """
        FIX: ChatGPT mới có download arrow button ngay trên thumbnail.
        Thử click download button trước — không cần expand.
        Verify message từ log: 'thumbnail view with only a download button' → button ĐÃ có.
        """
        # Priority 1: download/save button overlay on thumbnail
        ok = await self._actor.find_and_click(
            query="download arrow icon or save button overlaid on top of the generated image",
            retries=1, retry_delay=1.0,
        )
        if ok:
            return True
        # Priority 2: click ảnh để expand
        return await self._actor.find_and_click(
            query="AI generated image (the artwork itself, not the chat input box)",
            verify="Save or Share button visible at top, or image is full-size",
            retries=2,
        )

    @critical_guard(**GuardPresets.CHATGPT_DOWNLOAD)
    async def click_save_button(self) -> bool:
        """Tìm nút Save — thử nhiều query variant. critical_guard đảm bảo ảnh đã render xong."""
        queries = [
            "Save button with downward arrow icon (top of screen or above image)",
            "Download button with arrow pointing down",
            "button labeled Save near the generated image",
        ]
        for q in queries:
            ok = await self._actor.find_and_click(q, retries=1, retry_delay=1.0)
            if ok:
                return True
        return False

    async def describe_state(self) -> str:
        return await self._actor.describe(
            "ChatGPT page: images, buttons, loading indicators, text"
        )

    # ── Facebook ───────────────────────────────────────────────────────

    async def click_create_post_box(self) -> bool:
        """
        B6: v9.28 — find_and_click_v2 với ClickMemory + MultiModal Consensus.
        #7 cache hit → skip VLM hoàn toàn sau lần đầu.
        #8 JS+VLM+AX song song → consensus tọa độ chính xác hơn.
        #1 verify qua SiteAssertions.fb_composer_open() (JS, 50ms).
        """
        from vision.click_memory import ActionKeys
        from social.click_verifier import SiteAssertions  # BUG #4 FIX: C2 path
        await focus_chrome_tab("Facebook")
        result = await self._actor.find_and_click_v2(
            query="What's on your mind text box or Create post button at top of Facebook feed",
            domain="facebook.com",
            action_key=ActionKeys.FB_COMPOSER_OPEN,
            js_selectors=[
                "[aria-label*='on your mind']",
                "[aria-label*='nghĩ gì']",
                "[data-pagelet='FeedComposer'] [role='button']",
                "[role='button'][aria-label]",
            ],
            js_verify=SiteAssertions.fb_composer_open(),
            vision_verify="Facebook post creation dialog or composer box is open",
            retries=3,
        )
        ok = result.get("success", False)
        _vlog("✅" if ok else "❌", f"B6 create post box: method={result.get('method','?')} verified={result.get('verified','?')}")
        return ok

    async def click_photo_button(self) -> bool:
        """
        B7a: v9.28 — find_and_click_v2 với ClickMemory + SiteAssertions.
        #7 cache hit → no VLM sau lần đầu.
        #1 verify: file input đã trigger.
        """
        from vision.click_memory import ActionKeys
        from social.click_verifier import SiteAssertions  # BUG #4 FIX: C2 path
        result = await self._actor.find_and_click_v2(
            query="Photo/Video button or camera icon inside the Facebook post creation dialog",
            domain="facebook.com",
            action_key=ActionKeys.FB_PHOTO_BUTTON,
            js_selectors=[
                "[aria-label*='Photo']",
                "[aria-label*='Ảnh']",
                "[aria-label*='photo']",
                "[data-testid='photo-selector']",
            ],
            js_verify=SiteAssertions.fb_file_picker_open(),
            vision_verify="File picker, Finder, or photo upload area is visible",
            retries=2,
        )
        ok = result.get("success", False)
        _vlog("✅" if ok else "❌", f"B7a photo button: method={result.get('method','?')}")
        return ok

    async def _run_fb_js(self, js_code: str) -> str:
        """Run JS trong Facebook tab, trả về result string."""
        import platform as _platform
        if _platform.system() != "Darwin":
            return ""
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js",
                                             delete=False, encoding="utf-8") as f:
                f.write(js_code)
                tmp = f.name
            script = (
                'tell application "Google Chrome"\n'
                f'  set r to execute active tab of front window javascript'
                f' (read POSIX file "{tmp}" as «class utf8»)\n'
                '  return r as string\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run, ["osascript", "-e", script],
                capture_output=True, text=True, timeout=8,
            )
            os.unlink(tmp)
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except Exception:
            return ""

    async def wait_for_finder_dialog(self) -> bool:
        """SmartWait sau khi click Photo button."""
        return await self._actor.smart_wait(
            condition="Is a file picker, Finder, or file browser dialog visible?",
            timeout=10.0,
            poll_interval=1.0,
            description="Finder dialog open",
        )

    async def upload_image_drag_drop(self, image_path: str, start_tier: int = 0) -> bool:
        """
        B7a: Upload ảnh Facebook.
        A3 v9.24 — 3-tier strategy:
          Tầng 1: JS trigger input[type=file] (không cần cliclick, ổn định nhất)
          Tầng 2: Drag & Drop qua cliclick (nếu available)
          Tầng 3: AppleScript Cmd+Shift+G file dialog fallback

        Mỗi tầng thất bại → thử tầng tiếp theo.
        Sau mỗi tầng: verify thumbnail ảnh xuất hiện.

        Args:
            image_path: Đường dẫn file ảnh
            start_tier: A1 WorkflowMemory — bắt đầu từ tier nào (0=auto từ shortcuts,
                        1=Tầng 1, 2=Tầng 2, 3=Tầng 3). 0 = đọc từ _wm_shortcuts.
        """
        abs_path = str(__import__("pathlib").Path(image_path).resolve())

        # A1 WorkflowMemory: đọc preferred_upload_tier từ shortcuts
        # TrajectoryDB đã học tier nào thành công nhiều nhất → bắt đầu từ đó
        _effective_tier = start_tier
        if _effective_tier == 0 and self._wm_shortcuts is not None:
            _effective_tier = getattr(self._wm_shortcuts, "preferred_upload_tier", 1)
        if _effective_tier == 0:
            _effective_tier = 1  # default

        if _effective_tier != 1:
            _vlog("🧭", f"WorkflowMemory [preferred_upload_tier={_effective_tier}]: "
                        f"bắt đầu upload từ Tầng {_effective_tier} (skip Tầng 1..{_effective_tier-1})")

        # ── Tầng 1: JS trigger hidden input[type=file] ────────────────
        if _effective_tier <= 1:
            _vlog("📎", "B7a Tầng 1: JS input[type=file] trigger...")
            js_upload = (
                f"(function(){{"
                f"  var abs = {__import__('json').dumps(abs_path)};"
                f"  var inputs = document.querySelectorAll('input[type=file]');"
                f"  if(!inputs.length) return 'no_input';"
                f"  var inp = null;"
                f"  for(var i=0;i<inputs.length;i++){{"
                f"    var acc = inputs[i].accept||'';"
                f"    if(acc.includes('image') || acc === '' || acc.includes('*')) {{ inp=inputs[i]; break; }}"
                f"  }}"
                f"  if(!inp) inp = inputs[0];"
                f"  var dt = new DataTransfer();"
                f"  try {{"
                f"    inp.dispatchEvent(new MouseEvent('click',{{bubbles:true}}));"
                f"    return 'clicked:' + inp.accept;"
                f"  }} catch(e) {{ return 'error:'+e.message; }}"
                f"}})()"
            )
            js_result = await self._run_fb_js(js_upload)
            _vlog("📎", f"JS input trigger: {js_result}")
            if "clicked" in js_result:
                await asyncio.sleep(2.0)
                await self._fill_file_dialog(abs_path)
                await asyncio.sleep(3.0)
                if await self._verify_upload():
                    _vlog("✅", "B7a upload: Tầng 1 (JS + dialog) thành công")
                    return True

        # ── Tầng 2: Drag & Drop qua cliclick (nếu available) ─────────
        if _effective_tier <= 2:
            if _check_cliclick():
                _vlog("📎", "B7a Tầng 2: Drag & Drop via cliclick...")
                drag_ok = await self._actor.drag_file_to_element(
                    file_path=image_path,
                    target_query=(
                        "photo upload area or add photo/video dropzone "
                        "inside the Facebook post creation dialog"
                    ),
                )
                if drag_ok:
                    await asyncio.sleep(2.0)
                    if await self._verify_upload():
                        _vlog("✅", "B7a upload: Tầng 2 (drag & drop) thành công")
                        return True
            else:
                _vlog("⚠️ ", "B7a Tầng 2 skip: cliclick chưa install")

        # ── Tầng 3: AppleScript Cmd+Shift+G fallback ─────────────────
        _vlog("📎", "B7a Tầng 3: AppleScript file dialog fallback...")
        # Click Photo button để mở dialog nếu chưa mở
        photo_btn_ok = await self.click_photo_button()
        if photo_btn_ok:
            await asyncio.sleep(2.0)
        await self._fill_file_dialog(abs_path)
        await asyncio.sleep(3.5)
        if await self._verify_upload():
            _vlog("✅", "B7a upload: Tầng 3 (AppleScript) thành công")
            return True

        _vlog("❌", "B7a upload: Tất cả 3 tầng thất bại")
        return False

    async def _fill_file_dialog(self, abs_path: str) -> None:
        """Điền đường dẫn file vào Open dialog đang mở (AppleScript)."""
        try:
            import subprocess as _sp
            await asyncio.to_thread(
                _sp.run, ["pbcopy"],
                input=abs_path.encode("utf-8"),
                capture_output=True, timeout=3,
            )
            await asyncio.sleep(0.3)
            script = (
                'tell application "System Events"\n'
                '    keystroke "g" using {command down, shift down}\n'
                '    delay 1.2\n'
                '    keystroke "v" using command down\n'
                '    delay 0.5\n'
                '    keystroke return\n'
                '    delay 0.8\n'
                '    keystroke return\n'
                'end tell'
            )
            await asyncio.to_thread(
                _sp.run, ["osascript", "-e", script],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            _vlog("⚠️ ", f"_fill_file_dialog: {e}")

    async def _verify_upload(self) -> bool:
        """
        A3.3: Verify ảnh đã được upload thành công.
        Poll DOM 3 lần × 3s: tìm thumbnail hoặc image preview.
        """
        for attempt in range(3):
            await asyncio.sleep(3.0)
            result = await self._run_fb_js(
                "(function(){"
                "  var sels = ["
                "    'img[src*=\'blob:\']',"
                "    'img[src*=\'fbcdn\']',"
                "    '[data-testid*=\'photo\'] img',"
                "    '.x1n2onr6 img',"
                "    'div[role=\'img\']'"
                "  ];"
                "  for(var i=0;i<sels.length;i++){"
                "    if(document.querySelector(sels[i])) return 'found:'+sels[i];"
                "  }"
                "  return 'notfound';"
                "})()"
            )
            _vlog("🔍", f"Upload verify #{attempt+1}: {result[:50]}")
            if "found" in result:
                return True
        # Fallback: hỏi Vision
        if self._actor:
            return await self._actor.ask_vision(
                "Is there an image thumbnail or photo preview visible "
                "inside the Facebook post creation dialog?"
            )
        return False

    async def paste_content_js(self, content: str) -> bool:
        """
        B7b: v9.28 — find_and_click_v2 để click vào text area trước khi inject.
        #7 cache hit → no VLM cho việc tìm text area sau lần đầu.
        """
        from vision.click_memory import ActionKeys
        result = await self._actor.find_and_click_v2(
            query="text input area inside Facebook post creation dialog where you type the post content",
            domain="facebook.com",
            action_key="fb_composer_textarea",
            js_selectors=[
                "div[contenteditable='true'][role='textbox']",
                "[data-testid='react-composer-root'] div[contenteditable]",
                "div[aria-label*='post'][contenteditable]",
            ],
            retries=2,
        )
        await asyncio.sleep(0.5)
        # JS inject nội dung (không phụ thuộc clipboard)
        return await js_insert_text_facebook(content, chrome_tab_title="Facebook")

    @critical_guard(**GuardPresets.FB_POST)
    async def click_post_button(self) -> bool:
        """
        B7c: v9.33 — critical_guard kiểm tra màn hình trước khi click Đăng.
        Nếu VLM phát hiện popup/loading/error → escalate Telegram, KHÔNG đăng.
        find_and_click_v2 với ClickMemory + SiteAssertions.
        """
        from vision.click_memory import ActionKeys
        from social.click_verifier import SiteAssertions  # BUG #4 FIX: C2 path
        result = await self._actor.find_and_click_v2(
            query="Post button or Đăng button — blue submit button to publish the Facebook post",
            domain="facebook.com",
            action_key=ActionKeys.FB_POST_BUTTON,
            js_selectors=[
                "[aria-label='Post']",
                "[aria-label='Đăng']",
                "[data-testid='react-composer-post-button']",
                "div[aria-label*='Post']:not([aria-disabled])",
            ],
            js_verify=SiteAssertions.fb_post_submitted(),
            vision_verify="Facebook post was submitted — composer closed or success indicator visible",
            retries=3,
        )
        ok = result.get("success", False)
        _vlog("✅" if ok else "❌", f"B7c post button: method={result.get('method','?')} verified={result.get('verified','?')}")
        return ok

    async def verify_post_published(self) -> bool:
        """Verify cuối: bài đã được đăng thành công."""
        return await self._actor.ask_vision(
            "Is there a newly published post visible in the Facebook feed "
            "with an image and text content? Not the draft/composer."
        )
