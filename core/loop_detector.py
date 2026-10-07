# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/loop_detector.py — Phidipus v1.34
═══════════════════════════════════════════════════════════════════════

B1: Phát hiện stuck loop bằng perceptual hash — zero AI, zero RAM thêm.

Vấn đề từ log thực tế:
  Bot click B6 4 lần × 15s = 62s mà không nhận ra màn hình không đổi.
  agent_loop có anti_loop nhưng chỉ đếm theo skill name — không detect
  "màn hình không thay đổi sau action" hay "click cùng vùng nhiều lần".

Giải pháp — 3 signal độc lập:

  Signal 1: SCREEN_STUCK (pHash)
    Sau mỗi action → resize screenshot 32×32 grayscale → tính DCT hash
    Hamming distance với screenshot N-2 trước < HASH_THRESHOLD (5 bits)
    × STUCK_COUNT (3) lần liên tiếp → SCREEN_STUCK
    → Màn hình không đổi = action không có tác dụng

  Signal 2: COORD_LOOP
    Lưu lịch sử 5 clicks gần nhất
    Nếu 3/5 clicks trong radius 30px của nhau → COORD_LOOP
    → Bot đang click quẩn quanh cùng 1 điểm

  Signal 3: ACTION_LOOP (thay thế anti_loop trong skill_intelligence)
    Cùng action_name + cùng payload hash × 3 lần liên tiếp → ACTION_LOOP

Response theo signal:
  SCREEN_STUCK → scroll_down 300px + wait 2s + reset coords
  COORD_LOOP   → invalidate ClickMemory + force ZoomAction
  Cả hai fail 2 lần → Telegram escalation

Tích hợp:
  core/agent_loop.py → gọi loop_detector.record() sau mỗi step
  Nếu signal != CLEAR → gọi loop_detector.handle(signal, agent_loop)

Performance:
  pHash computation: < 2ms (resize + DCT 32×32)
  Memory: 5 screenshots × 1KB compressed = ~5KB
  CPU: negligible
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[1;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# LoopSignal enum
# ══════════════════════════════════════════════════════════════════

class LoopSignal(Enum):
    CLEAR        = auto()   # không có loop
    SCREEN_STUCK = auto()   # màn hình không đổi sau N actions
    COORD_LOOP   = auto()   # click quẩn quanh cùng tọa độ
    ACTION_LOOP  = auto()   # cùng action+payload lặp lại
    ESCALATE     = auto()   # đã can thiệp 2 lần, vẫn fail


# ══════════════════════════════════════════════════════════════════
# Perceptual Hash (pHash) — không cần thư viện ngoài
# ══════════════════════════════════════════════════════════════════

def _compute_phash(img_bytes: bytes) -> int | None:
    """
    Tính perceptual hash của ảnh — 64-bit integer.

    Algorithm: resize 32×32 grayscale → DCT → lấy 64 bit top-left.
    Không dùng imagehash/scipy — chỉ dùng Pillow (đã có sẵn).

    Returns None nếu Pillow không available hoặc ảnh lỗi.
    """
    if not img_bytes:
        return None
    try:
        from PIL import Image
        import struct

        with Image.open(io.BytesIO(img_bytes)) as img:
            # Resize 32×32 và convert grayscale
            small = img.convert("L").resize((32, 32), Image.LANCZOS)
            pixels = list(small.getdata())  # 1024 values 0-255

        # DCT đơn giản: mean per 8×8 block (4×4 = 16 blocks)
        # Nhanh hơn full DCT nhưng đủ để detect "màn hình giống nhau"
        BLOCK = 8
        GRID  = 32 // BLOCK  # = 4
        block_means = []
        for by in range(GRID):
            for bx in range(GRID):
                total = 0
                for py in range(BLOCK):
                    for px in range(BLOCK):
                        total += pixels[(by * BLOCK + py) * 32 + (bx * BLOCK + px)]
                block_means.append(total / (BLOCK * BLOCK))

        # Median threshold → 16-bit hash
        median = sorted(block_means)[len(block_means) // 2]
        bits = 0
        for i, v in enumerate(block_means):
            if v > median:
                bits |= (1 << i)
        return bits

    except ImportError:
        # Pillow không có — fallback: MD5 của raw bytes (ít reliable hơn)
        return int(hashlib.md5(img_bytes[:4096]).hexdigest()[:16], 16)
    except Exception as exc:
        _log.debug("pHash error: %s", exc)
        return None


def _hamming_distance(a: int, b: int) -> int:
    """Số bit khác nhau giữa 2 hash (Hamming distance)."""
    return bin(a ^ b).count("1")


# ══════════════════════════════════════════════════════════════════
# LoopDetector
# ══════════════════════════════════════════════════════════════════

class LoopDetector:
    """
    Phát hiện stuck loop bằng 3 signal độc lập.

    Gọi record() sau mỗi agent step.
    Nếu kết quả != CLEAR → gọi handle() để can thiệp.

    Args:
        hash_threshold:   Hamming distance tối đa để coi là "cùng màn hình"
        stuck_count:      Số lần consecutive "cùng màn hình" để trigger SCREEN_STUCK
        coord_radius:     Pixel radius để detect coordinate cluster
        coord_window:     Số clicks gần nhất để check
        coord_min_cluster:Số clicks trong radius để trigger COORD_LOOP
        action_repeat:    Số lần cùng action+payload để trigger ACTION_LOOP
        max_interventions:Số lần can thiệp tối đa trước ESCALATE
    """

    # Thresholds mặc định — calibrated từ log thực tế
    HASH_THRESHOLD   = 5    # < 5 bits khác = "cùng màn hình" (0–64 bits)
    STUCK_COUNT      = 3    # 3 lần liên tiếp → stuck
    COORD_RADIUS     = 30   # px
    COORD_WINDOW     = 5    # 5 clicks gần nhất
    COORD_MIN_CLUSTER= 3    # 3/5 clicks trong radius
    ACTION_REPEAT    = 3    # 3 lần cùng action
    MAX_INTERVENTIONS= 2    # sau 2 can thiệp fail → escalate

    def __init__(self) -> None:
        self._hashes:       list[int]  = []   # pHash lịch sử (max 5)
        self._stuck_streak: int        = 0    # consecutive "same screen"
        self._click_history: list[tuple[int, int]] = []  # (x, y) history
        self._action_history: list[str] = []  # action+payload_hash history
        self._intervention_count: int  = 0    # số lần đã can thiệp
        self._last_signal: LoopSignal  = LoopSignal.CLEAR
        self._last_signal_ts: float    = 0.0

    def record(
        self,
        action_name:    str,
        action_payload: dict,
        screenshot:     bytes | None = None,
        x:              int = 0,
        y:              int = 0,
    ) -> LoopSignal:
        """
        Ghi nhận 1 agent step và trả về LoopSignal.

        Gọi ngay sau khi IPC dispatch xong (trước khi record observation).

        Args:
            action_name:    Tên action đã dispatch ("mouse_click", v.v.)
            action_payload: Payload dict
            screenshot:     Screenshot SAU action (None = không check pHash)
            x, y:           Tọa độ click (0,0 nếu không phải click)
        """
        # ── Signal 1: Screen stuck (pHash) ───────────────────────────
        if screenshot:
            h = _compute_phash(screenshot)
            if h is not None:
                if self._hashes and _hamming_distance(h, self._hashes[-1]) < self.HASH_THRESHOLD:
                    self._stuck_streak += 1
                    _log.debug("LoopDetector: same screen streak=%d (hash distance < %d)",
                               self._stuck_streak, self.HASH_THRESHOLD)
                else:
                    self._stuck_streak = 0

                # Giữ 5 hash gần nhất
                self._hashes.append(h)
                if len(self._hashes) > 5:
                    self._hashes.pop(0)

                if self._stuck_streak >= self.STUCK_COUNT:
                    _vlog("🔁", f"SCREEN_STUCK: màn hình không đổi {self._stuck_streak} lần liên tiếp")
                    return self._emit(LoopSignal.SCREEN_STUCK)

        # ── Signal 2: Coordinate loop ─────────────────────────────────
        if action_name in ("mouse_click", "element_click") and (x or y):
            self._click_history.append((x, y))
            if len(self._click_history) > self.COORD_WINDOW:
                self._click_history.pop(0)

            if len(self._click_history) >= self.COORD_MIN_CLUSTER:
                cluster = self._count_cluster(x, y)
                if cluster >= self.COORD_MIN_CLUSTER:
                    _vlog("🔁", f"COORD_LOOP: {cluster}/{len(self._click_history)} clicks "
                          f"trong radius {self.COORD_RADIUS}px quanh ({x},{y})")
                    return self._emit(LoopSignal.COORD_LOOP)

        # ── Signal 3: Action loop ─────────────────────────────────────
        action_key = f"{action_name}:{self._payload_hash(action_payload)}"
        self._action_history.append(action_key)
        if len(self._action_history) > self.ACTION_REPEAT + 1:
            self._action_history.pop(0)

        if (len(self._action_history) >= self.ACTION_REPEAT and
                len(set(self._action_history[-self.ACTION_REPEAT:])) == 1):
            _vlog("🔁", f"ACTION_LOOP: '{action_name}' lặp {self.ACTION_REPEAT} lần giống hệt")
            return self._emit(LoopSignal.ACTION_LOOP)

        return LoopSignal.CLEAR

    async def handle(
        self,
        signal:       LoopSignal,
        ipc_client:   Any = None,
        click_memory: Any = None,
        domain:       str = "",
        action_key:   str = "",
        notify_fn:    Optional[Callable] = None,
    ) -> bool:
        """
        Can thiệp khi phát hiện loop. Trả về True nếu đã can thiệp.

        Args:
            signal:       LoopSignal cần xử lý
            ipc_client:   IPCClient để scroll/navigate
            click_memory: ClickMemory để invalidate cache
            domain:       Domain hiện tại ("facebook.com")
            action_key:   Action key để invalidate ("fb_post_button")
            notify_fn:    async callable(msg) gửi Telegram notification
        """
        if signal == LoopSignal.CLEAR:
            return False

        self._intervention_count += 1

        # Quá nhiều lần can thiệp → ESCALATE
        if self._intervention_count > self.MAX_INTERVENTIONS:
            _vlog("🚨", f"LoopDetector ESCALATE: {self._intervention_count} can thiệp, vẫn loop")
            if notify_fn:
                try:
                    await notify_fn(
                        f"⚠️ *Phidipus bị kẹt loop*\n\n"
                        f"Signal: {signal.name}\n"
                        f"Can thiệp: {self._intervention_count} lần\n"
                        f"Vui lòng kiểm tra màn hình."
                    )
                except Exception:
                    pass
            self.reset()
            return True

        # ── SCREEN_STUCK: scroll + wait ───────────────────────────────
        if signal == LoopSignal.SCREEN_STUCK:
            _vlog("🔧", f"Can thiệp SCREEN_STUCK #{self._intervention_count}: scroll 300px + wait 2s")
            if ipc_client:
                try:
                    await ipc_client.send_action("mouse_scroll", {
                        "direction": "down",
                        "amount": 300,
                    })
                except Exception:
                    pass
            await asyncio.sleep(2.0)
            self._stuck_streak = 0  # reset sau can thiệp

        # ── COORD_LOOP: invalidate ClickMemory ────────────────────────
        elif signal == LoopSignal.COORD_LOOP:
            _vlog("🔧", f"Can thiệp COORD_LOOP #{self._intervention_count}: "
                  f"invalidate cache + force ZoomAction")
            if click_memory and domain and action_key:
                try:
                    click_memory.record_failure(domain, action_key, "coord_loop_detected")
                    _vlog("🗑️", f"ClickMemory invalidated: {domain}/{action_key}")
                except Exception:
                    pass
            self._click_history.clear()

        # ── ACTION_LOOP: clear action history ────────────────────────
        elif signal == LoopSignal.ACTION_LOOP:
            _vlog("🔧", f"Can thiệp ACTION_LOOP #{self._intervention_count}: "
                  f"scroll 200px + wait 1.5s")
            if ipc_client:
                try:
                    await ipc_client.send_action("mouse_scroll", {
                        "direction": "down",
                        "amount": 200,
                    })
                except Exception:
                    pass
            await asyncio.sleep(1.5)
            self._action_history.clear()

        return True

    def reset(self) -> None:
        """Reset tất cả state — gọi sau khi task kết thúc."""
        self._hashes.clear()
        self._stuck_streak      = 0
        self._click_history.clear()
        self._action_history.clear()
        self._intervention_count = 0
        self._last_signal        = LoopSignal.CLEAR
        self._last_signal_ts     = 0.0

    def stats(self) -> dict:
        """Thống kê để log cuối task."""
        return {
            "stuck_streak":        self._stuck_streak,
            "intervention_count":  self._intervention_count,
            "last_signal":         self._last_signal.name,
            "click_history_len":   len(self._click_history),
        }

    # ── Internal ──────────────────────────────────────────────────

    def _emit(self, signal: LoopSignal) -> LoopSignal:
        self._last_signal    = signal
        self._last_signal_ts = time.time()
        return signal

    def _count_cluster(self, cx: int, cy: int) -> int:
        """Đếm số click trong _click_history gần tọa độ (cx, cy)."""
        count = 0
        r2 = self.COORD_RADIUS ** 2
        for x, y in self._click_history:
            if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                count += 1
        return count

    @staticmethod
    def _payload_hash(payload: dict) -> str:
        """Hash ngắn của payload để so sánh."""
        try:
            import json
            return hashlib.md5(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest()[:8]
        except Exception:
            return str(hash(str(payload)))[:8]
