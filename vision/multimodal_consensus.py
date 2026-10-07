# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/multimodal_consensus.py — Phidipus v1.28
═══════════════════════════════════════════════════════════════════════

#8 Multi-Modal Grounding
  Kết hợp 3 nguồn tín hiệu để xác định tọa độ click:
    - Source A: JS querySelector (DOM-first, không cần screenshot)
    - Source B: Screenshot VLM (Gemini/Qwen, visual understanding)
    - Source C: AX Accessibility Tree (native macOS, không cần Chrome)

  Consensus voting:
    - 3/3 đồng ý → confidence=1.0, click ngay
    - 2/3 đồng ý (trong threshold) → confidence=0.85, click
    - 1/3 → confidence=0.5, cần verify hoặc human-in-loop
    - 0/3 → không click, báo lỗi

  Robust với: dark/light theme, popup overlay, responsive layout,
  ngôn ngữ khác nhau (vi/en/ja), lazy-loaded elements.

Usage:
    consensus = MultiModalConsensus(
        js_exec_fn=actor._js_exec,
        vlm_fn=actor._vlm.call,
        screenshot_fn=actor.screenshot,
    )
    result = await consensus.find(
        query="Facebook Post button",
        js_selectors=["[data-testid='react-composer-post-button']",
                       "[aria-label='Post']"],
        window_bounds={"x": 0, "y": 80, "w": 1440, "h": 900},
    )
    if result.consensus_ok:
        await actor._click(result.x, result.y)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;32m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class SourceResult:
    """Tọa độ + confidence từ 1 nguồn."""
    source:     str         # "js" | "vlm" | "ax"
    found:      bool
    x:          int = 0
    y:          int = 0
    confidence: float = 0.0
    detail:     str = ""
    latency_ms: int = 0


@dataclass
class ConsensusResult:
    """Kết quả tổng hợp từ 3 nguồn."""
    consensus_ok:  bool
    x:             int   = 0
    y:             int   = 0
    confidence:    float = 0.0
    method:        str   = ""       # "3way" | "2way_js_vlm" | "2way_js_ax" | "2way_vlm_ax" | "single_*" | "failed"
    sources:       list[SourceResult] = field(default_factory=list)
    latency_ms:    int   = 0

    def log_summary(self) -> None:
        src_str = " | ".join(
            f"{s.source}={'✅' if s.found else '❌'}({s.x},{s.y}) conf={s.confidence:.0%}"
            for s in self.sources
        )
        status = f"{'✅ CONSENSUS' if self.consensus_ok else '❌ NO CONSENSUS'} ({self.method})"
        _vlog("🎯", f"{status} → ({self.x},{self.y}) conf={self.confidence:.0%} [{src_str}] {self.latency_ms}ms")


# ══════════════════════════════════════════════════════════════════
# MultiModalConsensus
# ══════════════════════════════════════════════════════════════════

class MultiModalConsensus:
    """
    3-source consensus locator (#8).

    Chạy JS + VLM + AX song song (asyncio.gather) → vote tọa độ.
    Nếu 2+ nguồn đồng ý trong threshold → consensus OK.

    Args:
        js_exec_fn:    async/sync callable(js_code) → str
        vlm_fn:        async callable(image_bytes, prompt) → (raw_str, tier_str)
        screenshot_fn: async callable() → bytes
        ax_fn:         async callable(query) → SourceResult (optional)
        pixel_threshold: khoảng cách tối đa (px) giữa 2 tọa độ để coi là "đồng ý"
        parallel:      True = chạy song song (nhanh hơn), False = sequential (ít timeout hơn)
    """

    def __init__(
        self,
        js_exec_fn: Optional[Callable] = None,
        vlm_fn: Optional[Callable] = None,
        screenshot_fn: Optional[Callable] = None,
        ax_fn: Optional[Callable] = None,
        pixel_threshold: int = 30,
        parallel: bool = True,
    ) -> None:
        self._js        = js_exec_fn
        self._vlm       = vlm_fn
        self._screen    = screenshot_fn
        self._ax        = ax_fn
        self._threshold = pixel_threshold
        self._parallel  = parallel

    # ── Public API ────────────────────────────────────────────────

    async def find(
        self,
        query: str,
        js_selectors: list[str] | None = None,
        window_bounds: dict | None = None,
        vlm_prompt_override: str | None = None,
    ) -> ConsensusResult:
        """
        Tìm element qua 3 nguồn và vote consensus.

        Args:
            query:           Mô tả element (dùng cho VLM và AX)
            js_selectors:    CSS selectors để thử DOM-first
            window_bounds:   {"x": int, "y": int, "w": int, "h": int}
                             (Chrome window bounds để convert relative → absolute)
            vlm_prompt_override: Custom VLM prompt nếu cần
        """
        t0     = time.time()
        bounds = window_bounds or {"x": 0, "y": 0, "w": 1440, "h": 900}

        # ── PERF FIX: JS fast-path — nếu JS hit với confidence cao, skip VLM hoàn toàn ──
        # JS querySelector là 0-100ms và deterministic → không cần chờ VLM 25s.
        if self._js and js_selectors:
            js_fast = await self._run_js(js_selectors, bounds)
            if js_fast.found and js_fast.confidence >= 0.95:
                total_ms = int((time.time() - t0) * 1000)
                _vlog("⚡", f"JS fast-path hit (conf={js_fast.confidence:.0%}) "
                      f"→ skip VLM+AX [{total_ms}ms]")
                result = ConsensusResult(
                    consensus_ok=True,
                    x=js_fast.x, y=js_fast.y,
                    confidence=js_fast.confidence,
                    method="js_exact",
                    sources=[js_fast],
                    latency_ms=total_ms,
                )
                result.log_summary()
                return result

        # ── Chạy các nguồn song song ──────────────────────────────
        tasks = []
        if self._js and js_selectors:
            tasks.append(self._run_js(js_selectors, bounds))
        else:
            # FIX v9.29: asyncio.coroutine bị xóa khỏi Python 3.11+
            # Dùng _null_source thay vì asyncio.coroutine(lambda)()
            tasks.append(self._null_source("js"))

        if self._vlm and self._screen:
            tasks.append(self._run_vlm(query, bounds, vlm_prompt_override))
        else:
            tasks.append(self._null_source("vlm"))

        if self._ax:
            tasks.append(self._run_ax(query, bounds))
        else:
            tasks.append(self._null_source("ax"))

        if self._parallel:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            sources = []
            for r in results:
                if isinstance(r, Exception):
                    _log.debug("Consensus source error: %s", r)
                    sources.append(SourceResult(source="error", found=False, detail=str(r)[:60]))
                else:
                    sources.append(r)
        else:
            sources = []
            for t in tasks:
                try:
                    sources.append(await t)
                except Exception as exc:
                    sources.append(SourceResult(source="error", found=False, detail=str(exc)[:60]))

        total_ms = int((time.time() - t0) * 1000)
        result   = self._vote(sources, total_ms)
        result.log_summary()
        return result

    # ── JS Source ─────────────────────────────────────────────────

    async def _run_js(
        self,
        selectors: list[str],
        bounds: dict,
    ) -> SourceResult:
        """
        Thử từng selector, lấy getBoundingClientRect() → absolute coords.
        """
        t0 = time.time()
        win_x = bounds.get("x", 0)
        win_y = bounds.get("y", 0)

        for sel in selectors:
            try:
                # Escape single quotes in selector for JS string
                sel_safe = sel.replace("'", "\'")
                js = (
                    "(function(){"
                    "  var el = document.querySelector('" + sel_safe + "');"
                    "  if (!el || !el.offsetParent) return null;"
                    "  var r = el.getBoundingClientRect();"
                    "  if (r.width === 0 || r.height === 0) return null;"
                    "  return JSON.stringify({"
                    "    cx: Math.round(r.left + r.width/2),"
                    "    cy: Math.round(r.top  + r.height/2),"
                    "    w:  Math.round(r.width),"
                    "    h:  Math.round(r.height)"
                    "  });"
                    "})()"
                )
                coro = self._js(js)
                raw = await coro if asyncio.iscoroutine(coro) else coro
                if raw and raw != "null" and raw.strip():
                    import json as _j
                    d = _j.loads(raw.strip())
                    cx = d.get("cx", 0) + win_x
                    cy = d.get("cy", 0) + win_y
                    ms = int((time.time() - t0) * 1000)
                    _vlog("🟢", f"JS found '{sel[:50]}' -> ({cx},{cy}) {ms}ms")
                    return SourceResult(
                        source="js", found=True,
                        x=cx, y=cy,
                        confidence=0.95,
                        detail=sel[:60],
                        latency_ms=ms,
                    )
            except Exception as exc:
                _log.debug("JS selector %r failed: %s", sel[:40], exc)

        ms = int((time.time() - t0) * 1000)
        _vlog("🔴", f"JS: no selector matched ({len(selectors)} tried, {ms}ms)")
        return SourceResult(source="js", found=False,
                            detail="no_match_" + str(len(selectors)) + "_selectors",
                            latency_ms=ms)


    async def _run_vlm(
        self,
        query: str,
        bounds: dict,
        prompt_override: str | None,
    ) -> SourceResult:
        """Chụp screenshot → gọi VLM → parse tọa độ."""
        t0 = time.time()
        try:
            img = await self._screen()
            if not img:
                return SourceResult(source="vlm", found=False, detail="no_screenshot")

            prompt = prompt_override or (
                f"Find the UI element: {query}\n"
                "Respond ONLY with JSON (no markdown):\n"
                "  If found: {\"found\": true, \"rx\": <ratio 0-1>, \"ry\": <ratio 0-1>, "
                "\"confidence\": <0.0-1.0>}\n"
                "  If not found: {\"found\": false}"
            )

            raw, tier = await asyncio.wait_for(
                self._vlm(img, prompt),
                timeout=25.0
            )

            # Parse response
            import json as _json
            import re as _re

            # Clean markdown
            cleaned = _re.sub(r"```(?:json)?|```", "", raw).strip()
            try:
                d = _json.loads(cleaned)
            except Exception:
                # Fallback: find JSON in text
                m = _re.search(r'\{[^}]+\}', cleaned)
                if m:
                    try:
                        d = _json.loads(m.group())
                    except Exception:
                        d = {}
                else:
                    d = {}

            if not d.get("found", False):
                ms = int((time.time() - t0) * 1000)
                return SourceResult(source="vlm", found=False, detail="vlm_not_found", latency_ms=ms)

            rx = float(d.get("rx", 0.5))
            ry = float(d.get("ry", 0.5))
            conf = float(d.get("confidence", 0.5))

            # Convert relative → absolute
            win_x = bounds.get("x", 0)
            win_y = bounds.get("y", 0)
            win_w = bounds.get("w", 1440)
            win_h = bounds.get("h", 900)
            abs_x = int(rx * win_w) + win_x
            abs_y = int(ry * win_h) + win_y

            ms = int((time.time() - t0) * 1000)
            _vlog("👁️", f"VLM [{tier}] found '{query[:40]}' → ({abs_x},{abs_y}) "
                  f"conf={conf:.0%} {ms}ms")
            return SourceResult(
                source="vlm", found=True,
                x=abs_x, y=abs_y,
                confidence=conf,
                detail=f"{tier}",
                latency_ms=ms,
            )
        except asyncio.TimeoutError:
            ms = int((time.time() - t0) * 1000)
            return SourceResult(source="vlm", found=False, detail="timeout", latency_ms=ms)
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            _log.debug("VLM source error: %s", exc)
            return SourceResult(source="vlm", found=False, detail=str(exc)[:60], latency_ms=ms)

    # ── AX Source ─────────────────────────────────────────────────

    async def _run_ax(self, query: str, bounds: dict) -> SourceResult:
        """Dùng AX Accessibility API để tìm element."""
        t0 = time.time()
        try:
            coro = self._ax(query)
            result = await coro if asyncio.iscoroutine(coro) else coro
            ms = int((time.time() - t0) * 1000)
            if isinstance(result, SourceResult):
                result.latency_ms = ms
                return result
            # Nếu ax_fn trả về tuple (x, y) hoặc dict
            if isinstance(result, tuple) and len(result) == 2:
                return SourceResult(source="ax", found=True,
                                    x=result[0], y=result[1],
                                    confidence=0.9, latency_ms=ms)
            if isinstance(result, dict) and result.get("found"):
                return SourceResult(source="ax", found=True,
                                    x=result.get("x", 0), y=result.get("y", 0),
                                    confidence=0.9, latency_ms=ms)
            return SourceResult(source="ax", found=False, detail="ax_not_found", latency_ms=ms)
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            return SourceResult(source="ax", found=False, detail=str(exc)[:60], latency_ms=ms)

    async def _null_source(self, name: str) -> SourceResult:
        # FIX v9.29: yield control để đảm bảo asyncio không warning coroutine leak
        await asyncio.sleep(0)
        return SourceResult(source=name, found=False, detail="not_configured")

    # ── Voting ────────────────────────────────────────────────────

    def _vote(self, sources: list[SourceResult], total_ms: int) -> ConsensusResult:
        """
        Vote consensus từ các nguồn đã có kết quả.

        Thuật toán:
          1. Lọc found=True sources
          2. Tìm cặp nào đồng ý (trong pixel_threshold)
          3. Nếu có ≥2 đồng ý → consensus OK, lấy weighted average
          4. Nếu chỉ 1 nguồn → single source (confidence thấp hơn)
        """
        found_sources = [s for s in sources if s.found]

        if not found_sources:
            return ConsensusResult(
                consensus_ok=False, method="failed",
                sources=sources, latency_ms=total_ms,
            )

        # Sắp xếp theo confidence (cao nhất trước)
        found_sources.sort(key=lambda s: -s.confidence)

        # Kiểm tra từng cặp có đồng ý không
        agreements: list[tuple[SourceResult, SourceResult]] = []
        for i in range(len(found_sources)):
            for j in range(i + 1, len(found_sources)):
                a, b = found_sources[i], found_sources[j]
                dist = ((a.x - b.x)**2 + (a.y - b.y)**2) ** 0.5
                if dist <= self._threshold:
                    agreements.append((a, b))

        # Kiểm tra 3-way agreement
        if len(found_sources) == 3 and len(agreements) == 3:
            # Tất cả 3 đồng ý
            x = round(sum(s.x for s in found_sources) / 3)
            y = round(sum(s.y for s in found_sources) / 3)
            conf = min(1.0, sum(s.confidence for s in found_sources) / 3 * 1.1)
            return ConsensusResult(
                consensus_ok=True, x=x, y=y,
                confidence=round(conf, 3), method="3way",
                sources=sources, latency_ms=total_ms,
            )

        # 2-way agreement
        if agreements:
            a, b = agreements[0]  # cặp confidence cao nhất
            x = round((a.x * a.confidence + b.x * b.confidence) / (a.confidence + b.confidence))
            y = round((a.y * a.confidence + b.y * b.confidence) / (a.confidence + b.confidence))
            conf = (a.confidence + b.confidence) / 2 * 0.95  # slight penalty vs 3-way
            method = f"2way_{a.source}_{b.source}"
            return ConsensusResult(
                consensus_ok=True, x=x, y=y,
                confidence=round(conf, 3), method=method,
                sources=sources, latency_ms=total_ms,
            )

        # Chỉ 1 nguồn tìm thấy, hoặc các nguồn không đồng ý
        best = found_sources[0]
        if best.confidence >= 0.85:
            # Chỉ trust nếu confidence cao (ví dụ: JS selector perfect match)
            return ConsensusResult(
                consensus_ok=True,
                x=best.x, y=best.y,
                confidence=best.confidence * 0.85,  # penalty vì no consensus
                method=f"single_{best.source}",
                sources=sources, latency_ms=total_ms,
            )

        # Không đủ tự tin
        return ConsensusResult(
            consensus_ok=False,
            x=best.x, y=best.y,  # best guess
            confidence=best.confidence * 0.5,
            method="no_consensus",
            sources=sources, latency_ms=total_ms,
        )
