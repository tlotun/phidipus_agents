# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/prefetch_engine.py — Phidipus B1 Predictive Pre-Click v2.2
════════════════════════════════════════════════════════════════

B1: Predictive Pre-Click — Dự đoán và pre-load tọa độ nút tiếp theo.

Vấn đề:
  Workflow tuần tự: type_text(2s) → vision_click(3-25s) → ...
  Trong 2 giây type_text, VisionActor idle hoàn toàn.
  → Lãng phí 2s có thể dùng để pre-fetch tọa độ nút Submit.

Giải pháp — PrefetchEngine:
  Khi workflow executor bắt đầu execute node N (type_text/wait/scroll):
    1. Peek node N+1 từ queue
    2. Nếu N+1 là vision_click và có description + domain + action_key:
       → Kiểm tra ClickMemory: nếu cache HIT → skip (không cần pre-fetch)
       → Nếu cache MISS → launch asyncio.Task pre-fetch ngay lập tức
    3. Task chạy background: VisionActor.find_element(description)
    4. Khi node N xong → node N+1 (vision_click) bắt đầu:
       → await prefetch_task với timeout ngắn (0.1s)
       → Nếu có kết quả → dùng ngay (S0, 0ms click)
       → Nếu chưa xong → fallback pipeline bình thường

Kết quả kỳ vọng:
  - Workflow type_text → vision_click: giảm latency 30-50%
  - Đặc biệt hiệu quả khi cache cold (lần đầu) và cần VLM (3-25s)
  - Zero risk: nếu prefetch fail → fallback bình thường, không crash

Architecture:
  workflow_executor:
    execute(type_text_node)
    ↓ launch PrefetchEngine.start_prefetch(next_node)   ← non-blocking
    [type_text đang chạy, prefetch chạy background]
    ↓
    execute(vision_click_node)
    → PrefetchEngine.get_result(node)    ← await 0.1s
    → nếu có kết quả → dùng ngay (method="prefetch_s0")
    → nếu không → pipeline S1→S4 bình thường

Nodes hưởng lợi từ prefetch (node hiện tại):
  - type_text    (thường kéo dài 0.5-3s)
  - wait         (thường kéo dài 1-60s)
  - scroll       (thường kéo dài 0.5-2s)
  - vision_wait  (thường kéo dài 2-30s)
  - ai_process   (thường kéo dài 5-30s)

Nodes được pre-fetch (node tiếp theo):
  - vision_click (cần description, tốt hơn nếu có domain+action_key)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# Nodes mà khi đang execute → nên prefetch next vision_click
PREFETCH_TRIGGER_NODES = frozenset({
    "type_text",
    "wait",
    "scroll",
    "vision_wait",
    "ai_process",
    "download_wait",
})

# Nodes có thể được pre-fetch
PREFETCHABLE_NODES = frozenset({"vision_click"})

# Timeout khi lấy kết quả prefetch từ vision_click node
_PREFETCH_CONSUME_TIMEOUT = 0.15   # 150ms — đủ để lấy kết quả nếu đã xong


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class PrefetchResult:
    """Kết quả pre-fetch cho 1 vision_click node."""
    node_id:     str
    found:       bool
    x:           int   = 0
    y:           int   = 0
    confidence:  float = 0.0
    method:      str   = "prefetch_s0"
    latency_ms:  int   = 0
    error:       str   = ""

    def to_click_result(self) -> dict:
        """Convert sang format của find_and_click_v2 result."""
        return {
            "success":    self.found,
            "x":          self.x,
            "y":          self.y,
            "confidence": self.confidence,
            "method":     self.method,
            "verified":   False,   # chưa verify — vision_click sẽ verify sau
            "memory_hit": False,
            "latency_ms": self.latency_ms,
            "prefetched": True,
        }


# ══════════════════════════════════════════════════════════════════
# PrefetchEngine
# ══════════════════════════════════════════════════════════════════

class PrefetchEngine:
    """
    B1 Predictive Pre-Click Engine.

    Một instance per WorkflowExecutor — lifecycle gắn với executor.
    Giữ tối đa 1 prefetch task active tại một thời điểm (tránh spam VLM).

    Usage (trong WorkflowExecutor):
        prefetch = PrefetchEngine()

        # Trước khi execute type_text
        prefetch.maybe_start(
            current_node_type="type_text",
            next_node=queue[0] if queue else None,
            node_map=node_map,
            actor=self._actor,
            click_mem=self._actor._click_mem if self._actor else None,
        )

        # Execute type_text (2s)
        result = await self._exec_type_text(config)

        # Lấy prefetch result khi bắt đầu vision_click
        pre = await prefetch.consume(node_id)
        if pre and pre.found:
            # Dùng kết quả prefetch → skip S1-S4
            x, y, conf = pre.x, pre.y, pre.confidence
            method = "prefetch_s0"
    """

    def __init__(self) -> None:
        self._task:      Optional[asyncio.Task] = None
        self._node_id:   str = ""
        self._started_at: float = 0.0
        self._result:    Optional[PrefetchResult] = None

    def maybe_start(
        self,
        current_node_type: str,
        queue: list,
        node_map: dict,
        actor: Any,
        click_mem: Any = None,
    ) -> bool:
        """
        Nếu điều kiện phù hợp → launch prefetch task.

        Args:
            current_node_type: Node đang execute ("type_text", "wait", ...)
            queue:             Queue còn lại (peek queue[0])
            node_map:          {node_id → node_dict}
            actor:             VisionActor instance (hoặc None)
            click_mem:         ClickMemory instance (để check cache hit)

        Returns:
            True nếu đã launch prefetch task
        """
        # Chỉ prefetch khi current node là trigger node
        if current_node_type not in PREFETCH_TRIGGER_NODES:
            return False

        # Phải có actor để gọi find_element
        if actor is None:
            return False

        # Peek next node
        if not queue:
            return False
        next_id = queue[0]
        next_node = node_map.get(next_id)
        if not next_node:
            return False

        next_type = next_node.get("type", "")
        if next_type not in PREFETCHABLE_NODES:
            return False

        next_cfg = next_node.get("config", {})
        description = next_cfg.get("description", "")
        if not description:
            return False

        # Check ClickMemory: nếu cache HIT → skip prefetch (sẽ 0ms anyway)
        domain     = next_cfg.get("domain", "")
        action_key = next_cfg.get("action_key", "")
        if click_mem and domain and action_key:
            try:
                mem_result = click_mem.lookup(domain, action_key)
                if mem_result.found:
                    _vlog("⚡", f"B1 Prefetch: cache HIT for '{description[:40]}' → skip prefetch")
                    return False
            except Exception:
                pass

        # Cancel previous task nếu còn
        self._cancel()

        # Launch background task
        self._node_id    = next_id
        self._started_at = time.time()
        self._result     = None

        self._task = asyncio.create_task(
            self._prefetch_task(
                node_id=next_id,
                description=description,
                actor=actor,
            )
        )
        _vlog("🔮", f"B1 Prefetch: launched for '{description[:50]}' "
                    f"(next node={next_id[:8]})")
        return True

    async def consume(self, node_id: str) -> Optional[PrefetchResult]:
        """
        Lấy kết quả prefetch cho node_id.
        Await tối đa _PREFETCH_CONSUME_TIMEOUT giây.

        Returns:
            PrefetchResult nếu có kết quả tốt, None nếu không có/fail/timeout
        """
        if self._task is None or self._node_id != node_id:
            return None

        # Nếu task đã xong → lấy ngay
        if self._task.done():
            return self._get_result()

        # Await timeout ngắn
        try:
            await asyncio.wait_for(
                asyncio.shield(self._task),
                timeout=_PREFETCH_CONSUME_TIMEOUT,
            )
        except asyncio.TimeoutError:
            elapsed = round((time.time() - self._started_at) * 1000)
            _vlog("🔮", f"B1 Prefetch: still running ({elapsed}ms) → fallback pipeline")
            return None
        except Exception:
            return None

        return self._get_result()

    def cancel(self) -> None:
        """Cancel prefetch task (gọi khi workflow kết thúc)."""
        self._cancel()

    # ── Internal ─────────────────────────────────────────────────

    async def _prefetch_task(
        self,
        node_id: str,
        description: str,
        actor: Any,
    ) -> None:
        """Background task: gọi find_element và lưu kết quả."""
        t0 = time.time()
        try:
            r = await actor.find_element(description)
            elapsed = int((time.time() - t0) * 1000)
            if r.found and r.confidence >= 0.65:
                self._result = PrefetchResult(
                    node_id=node_id,
                    found=True,
                    x=r.x, y=r.y,
                    confidence=r.confidence,
                    method=f"prefetch_s0({r.tier})",
                    latency_ms=elapsed,
                )
                _vlog("🔮", f"B1 Prefetch done: ({r.x},{r.y}) "
                            f"conf={r.confidence:.0%} {elapsed}ms ✅")
            else:
                self._result = PrefetchResult(
                    node_id=node_id, found=False,
                    error=f"conf too low ({r.confidence:.2f})" if r.found else "not found",
                    latency_ms=elapsed,
                )
                _vlog("🔮", f"B1 Prefetch: {'low conf' if r.found else 'not found'} "
                            f"conf={r.confidence:.2f} {elapsed}ms → will use fallback")
        except Exception as exc:
            elapsed = int((time.time() - t0) * 1000)
            self._result = PrefetchResult(
                node_id=node_id, found=False,
                error=str(exc)[:80], latency_ms=elapsed,
            )
            _vlog("⚠️", f"B1 Prefetch error: {exc}")

    def _get_result(self) -> Optional[PrefetchResult]:
        """Lấy result, clear state."""
        result = self._result
        if result and result.found:
            elapsed = round((time.time() - self._started_at) * 1000)
            _vlog("🔮", f"B1 Prefetch consumed: ({result.x},{result.y}) "
                        f"total_elapsed={elapsed}ms")
        self._task    = None
        self._node_id = ""
        self._result  = None
        return result if (result and result.found) else None

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            _vlog("🔮", "B1 Prefetch: cancelled previous task")
        self._task    = None
        self._node_id = ""
        self._result  = None
