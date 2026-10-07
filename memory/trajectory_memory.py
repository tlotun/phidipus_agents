# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/trajectory_memory.py — Phidipus v1.32
═══════════════════════════════════════════════════════════════════════

Trajectory-Informed Memory — Inspired by arxiv 2603.10600

Vấn đề:
  Phidipus chạy workflow_spider_social_post nhiều lần nhưng không học từ
  lịch sử. Mỗi lần chạy là fresh start — không biết:
    - B2 URL download thường 403 → nên dùng vision_click_save ngay
    - B6 JS verify thường fail (JS không reach Chrome) → skip JS verify
    - Workflow lần trước mất 134s → lần này có thể làm nhanh hơn không?

Giải pháp — TrajectoryDB:
  Sau mỗi workflow success/fail:
    1. Record trajectory (danh sách bước, thời gian, số VLM calls, lỗi)
    2. generate_insights() → LLM phân tích trajectory history → sinh shortcuts
    3. Shortcuts được đọc trước khi chạy → adjust strategy tự động

Schema (data/plans/trajectory_memory.json):
{
  "workflow_spider_social_post": {
    "success_count": 12,
    "fail_count": 3,
    "avg_duration_s": 98.5,
    "trajectory_history": [
      {
        "ts": "2026-03-21T20:06:18",
        "success": true,
        "steps": [
          {"name": "B2_chatgpt_image", "duration_s": 134.6,
           "vlm_calls": 4, "note": "url_403_fallback_vision"},
          {"name": "B6_fb_composer",   "duration_s": 62.5,
           "vlm_calls": 8, "note": "js_verify_unavailable_vision_ok"},
          {"name": "B7c_post_button",  "duration_s": 17.0,
           "vlm_calls": 2, "note": ""}
        ],
        "failed_at": null,
        "duration_s": 134.6,
        "vlm_calls": 14,
        "memory_hits": 0,
        "reflection_used": true
      }
    ],
    "learned_insights": [
      {
        "id": "ins_001",
        "insight": "chatgpt_url_download thường fail HTTP 403",
        "shortcut": "skip_url_download",
        "action": "B2: dùng vision_click_save_button ngay, không thử URL download",
        "confidence": 0.9,
        "applied_count": 5,
        "success_count": 5,
        "created_ts": "2026-03-21T20:30:00"
      }
    ]
  }
}

Tích hợp:
  1. Cuối workflow.run() → trajectory_db.record(wf_result, steps_log)
  2. Đầu workflow.run() → shortcuts = trajectory_db.get_shortcuts(wf_name)
  3. Sau 3+ trajectories → generate_insights() tự động
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class StepTrace:
    """Trace của 1 bước trong workflow."""
    name:        str
    duration_s:  float = 0.0
    vlm_calls:   int   = 0
    memory_hits: int   = 0
    retries:     int   = 0
    success:     bool  = True
    note:        str   = ""   # ghi chú tự do: "url_403", "js_unavailable", v.v.

    def to_dict(self) -> dict:
        return {
            "name":        self.name,
            "duration_s":  round(self.duration_s, 1),
            "vlm_calls":   self.vlm_calls,
            "memory_hits": self.memory_hits,
            "retries":     self.retries,
            "success":     self.success,
            "note":        self.note,
        }

    @staticmethod
    def from_dict(d: dict) -> "StepTrace":
        return StepTrace(
            name=d.get("name", ""),
            duration_s=float(d.get("duration_s", 0.0)),
            vlm_calls=int(d.get("vlm_calls", 0)),
            memory_hits=int(d.get("memory_hits", 0)),
            retries=int(d.get("retries", 0)),
            success=bool(d.get("success", True)),
            note=str(d.get("note", "")),
        )


@dataclass
class TrajectoryEntry:
    """1 lần chạy workflow."""
    ts:               str            # ISO timestamp
    success:          bool
    steps:            list[StepTrace]
    failed_at:        str | None     # tên bước fail (nếu có)
    duration_s:       float
    vlm_calls:        int
    memory_hits:      int
    reflection_used:  bool = False
    goal_hash:        str  = ""      # hash của goal để cluster similar goals

    def to_dict(self) -> dict:
        return {
            "ts":              self.ts,
            "success":         self.success,
            "steps":           [s.to_dict() for s in self.steps],
            "failed_at":       self.failed_at,
            "duration_s":      round(self.duration_s, 1),
            "vlm_calls":       self.vlm_calls,
            "memory_hits":     self.memory_hits,
            "reflection_used": self.reflection_used,
            "goal_hash":       self.goal_hash,
        }

    @staticmethod
    def from_dict(d: dict) -> "TrajectoryEntry":
        return TrajectoryEntry(
            ts=d.get("ts", ""),
            success=bool(d.get("success", False)),
            steps=[StepTrace.from_dict(s) for s in d.get("steps", [])],
            failed_at=d.get("failed_at"),
            duration_s=float(d.get("duration_s", 0.0)),
            vlm_calls=int(d.get("vlm_calls", 0)),
            memory_hits=int(d.get("memory_hits", 0)),
            reflection_used=bool(d.get("reflection_used", False)),
            goal_hash=d.get("goal_hash", ""),
        )


@dataclass
class Insight:
    """1 shortcut/insight được học từ trajectory history."""
    id:            str
    insight:       str    # mô tả pattern đã phát hiện
    shortcut:      str    # key ngắn gọn để lookup
    action:        str    # hành động cụ thể nên làm
    confidence:    float  # 0.0–1.0
    applied_count: int    = 0
    success_count: int    = 0
    created_ts:    str    = ""
    last_applied:  str    = ""

    @property
    def success_rate(self) -> float:
        if self.applied_count == 0:
            return self.confidence  # dùng initial confidence
        return self.success_count / self.applied_count

    @property
    def is_reliable(self) -> bool:
        """Đủ reliable khi đã apply 3+ lần và success_rate >= 0.75."""
        return self.applied_count >= 3 and self.success_rate >= 0.75

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "insight":       self.insight,
            "shortcut":      self.shortcut,
            "action":        self.action,
            "confidence":    round(self.confidence, 3),
            "applied_count": self.applied_count,
            "success_count": self.success_count,
            "created_ts":    self.created_ts,
            "last_applied":  self.last_applied,
        }

    @staticmethod
    def from_dict(d: dict) -> "Insight":
        return Insight(
            id=d.get("id", ""),
            insight=d.get("insight", ""),
            shortcut=d.get("shortcut", ""),
            action=d.get("action", ""),
            confidence=float(d.get("confidence", 0.5)),
            applied_count=int(d.get("applied_count", 0)),
            success_count=int(d.get("success_count", 0)),
            created_ts=d.get("created_ts", ""),
            last_applied=d.get("last_applied", ""),
        )


@dataclass
class Shortcuts:
    """
    Shortcuts được đọc từ TrajectoryDB trước khi chạy workflow.
    Được truyền vào workflow để adjust strategy.
    """
    skip_url_download:           bool  = False   # B2: dùng vision click save ngay
    skip_js_verify:              bool  = False   # Verify: skip JS, chỉ dùng vision
    skip_new_tab:                bool  = False   # B3: không cần mở tab mới
    preferred_upload_tier:       int   = 1       # B7a: bắt đầu từ tier nào (1/2/3)
    prefer_zoom_b6:              bool  = False   # B6: skip S2 Consensus → ZoomAction trước
    reflection_helps:            bool  = False   # Reflection nhanh hơn blind retry >=15%
    expected_duration_s:         float = 0.0     # ước lượng thời gian dựa trên history
    avg_vlm_calls:               int   = 0       # VLM calls trung bình từ history
    insights:                    list[Insight] = field(default_factory=list)

    def log(self, workflow_name: str) -> None:
        active = [k for k, v in [
            ("skip_url_download", self.skip_url_download),
            ("skip_js_verify",    self.skip_js_verify),
            ("skip_new_tab",      self.skip_new_tab),
            ("prefer_zoom_b6",    self.prefer_zoom_b6),
            ("reflection_helps",  self.reflection_helps),
        ] if v]
        _vlog("🧭", f"TrajectoryDB shortcuts for {workflow_name}: "
              f"active={active or ['none']} "
              f"upload_tier={self.preferred_upload_tier} "
              f"est={self.expected_duration_s:.0f}s "
              f"insights={len(self.insights)}")


# ══════════════════════════════════════════════════════════════════
# Rule-based insight extractor (Tier 1 — no LLM needed)
# ══════════════════════════════════════════════════════════════════

def _extract_rule_insights(
    history: list[TrajectoryEntry],
    existing_shortcuts: set[str],
) -> list[Insight]:
    """
    Phân tích trajectory history bằng rules đơn giản.
    Không cần LLM — nhanh, deterministic, không tốn API.

    Chạy khi có >= 2 trajectories.
    """
    insights: list[Insight] = []
    ts_now = datetime.now(timezone.utc).isoformat()

    if len(history) < 2:
        return insights

    # ── Rule 1: URL download thường fail ─────────────────────────
    if "skip_url_download" not in existing_shortcuts:
        b2_url_fails = sum(
            1 for t in history
            if any("url_403" in s.note or "url_download" in s.note
                   for s in t.steps if "B2" in s.name)
        )
        total_b2 = sum(1 for t in history if any("B2" in s.name for s in t.steps))
        if total_b2 >= 2 and b2_url_fails / max(total_b2, 1) >= 0.6:
            insights.append(Insight(
                id=f"ins_{int(time.time())}_url",
                insight=f"ChatGPT URL download fail {b2_url_fails}/{total_b2} lần ({b2_url_fails/max(total_b2,1):.0%})",
                shortcut="skip_url_download",
                action="B2: bỏ qua thử URL download — dùng vision_click_save_button ngay từ đầu",
                confidence=min(0.95, b2_url_fails / max(total_b2, 1)),
                created_ts=ts_now,
            ))

    # ── Rule 2: JS verify không reach Chrome ─────────────────────
    if "skip_js_verify" not in existing_shortcuts:
        js_skip_count = sum(
            1 for t in history
            if any("js_unavailable" in s.note or "js_exec_skipped" in s.note
                   or "js_verify" in s.note
                   for s in t.steps)
        )
        if js_skip_count >= 2 and js_skip_count / max(len(history), 1) >= 0.5:
            insights.append(Insight(
                id=f"ins_{int(time.time())}_jsv",
                insight=f"JS verify không reach Chrome {js_skip_count}/{len(history)} lần",
                shortcut="skip_js_verify",
                action="Verify: ưu tiên Vision verify, treat JS skip là ALREADY_DONE",
                confidence=min(0.90, js_skip_count / max(len(history), 1)),
                created_ts=ts_now,
            ))

    # ── Rule 3: Slow B6 → prefer ZoomAction ──────────────────────
    if "prefer_zoom_b6" not in existing_shortcuts:
        b6_steps = [
            s for t in history
            for s in t.steps if "B6" in s.name and s.vlm_calls >= 4
        ]
        if len(b6_steps) >= 2:
            avg_b6_vlm = sum(s.vlm_calls for s in b6_steps) / len(b6_steps)
            if avg_b6_vlm >= 5:
                insights.append(Insight(
                    id=f"ins_{int(time.time())}_b6z",
                    insight=f"B6 click composer tốn trung bình {avg_b6_vlm:.1f} VLM calls",
                    shortcut="prefer_zoom_b6",
                    action="B6: dùng ZoomAction (3-pass) thay vì MultiModal trước",
                    confidence=0.75,
                    created_ts=ts_now,
                ))

    # ── Rule 4: Upload tier pattern ───────────────────────────────
    if "preferred_upload_tier" not in existing_shortcuts:
        tier_counts = {1: 0, 2: 0, 3: 0}
        for t in history:
            for s in t.steps:
                if "B7a" in s.name and s.success:
                    for tier_n, tier_key in [(1, "tier1"), (2, "tier2"), (3, "tier3")]:
                        if tier_key in s.note or f"tầng {tier_n}" in s.note.lower():
                            tier_counts[tier_n] += 1
                            break
        best_tier = max(tier_counts, key=lambda k: tier_counts[k])
        if tier_counts[best_tier] >= 2:
            insights.append(Insight(
                id=f"ins_{int(time.time())}_tier",
                insight=f"Upload tier {best_tier} thành công {tier_counts[best_tier]} lần",
                shortcut="preferred_upload_tier",
                action=f"B7a: bắt đầu thử từ Tier {best_tier} thay vì Tier 1",
                confidence=0.70,
                created_ts=ts_now,
            ))

    # ── Rule 5: Reflection thực sự giúp ích ─────────────────────
    if "reflection_helps" not in existing_shortcuts:
        with_ref = [t for t in history if t.reflection_used and t.success]
        without_ref = [t for t in history if not t.reflection_used and t.success]
        if len(with_ref) >= 2 and len(without_ref) >= 2:
            avg_with = sum(t.duration_s for t in with_ref) / len(with_ref)
            avg_without = sum(t.duration_s for t in without_ref) / len(without_ref)
            if avg_with < avg_without * 0.85:  # reflection nhanh hơn >=15%
                insights.append(Insight(
                    id=f"ins_{int(time.time())}_ref",
                    insight=f"Reflection giúp nhanh hơn {(1-avg_with/avg_without):.0%} "
                            f"({avg_without:.0f}s → {avg_with:.0f}s)",
                    shortcut="reflection_helps",
                    action="Reflection Supervisor: bật use_vision=True luôn",
                    confidence=0.80,
                    created_ts=ts_now,
                ))

    return insights


# ══════════════════════════════════════════════════════════════════
# TrajectoryDB — Main class
# ══════════════════════════════════════════════════════════════════

class TrajectoryDB:
    """
    Persistent Trajectory Memory cho Phidipus workflows.

    Workflow:
      1. Before run: shortcuts = db.get_shortcuts("workflow_spider_social_post")
      2. After run:  db.record("workflow_spider_social_post", wf_result, steps_log)
      3. Sau mỗi 3 trajectories mới: tự động generate_insights()

    Thread-safe, atomic writes, tự evict trajectories cũ (max 50/workflow).

    Args:
        db_path:          Đường dẫn file JSON
        max_history:      Số trajectory tối đa lưu mỗi workflow (default 50)
        auto_insight_every: Generate insights mỗi N trajectory mới (default 3)
        llm_fn:           Optional LLM để generate insights phức tạp hơn rules
    """

    DEFAULT_PATH = Path("data/plans/trajectory_memory.json")

    def __init__(
        self,
        db_path: str | Path | None = None,
        max_history: int = 50,
        auto_insight_every: int = 3,
        llm_fn: Any = None,   # callable(prompt) → str, optional
    ) -> None:
        self._path        = Path(db_path or self.DEFAULT_PATH)
        self._max_history = max_history
        self._auto_every  = auto_insight_every
        self._llm         = llm_fn
        self._data: dict  = {}
        self._lock        = threading.Lock()
        self._loaded      = False
        self._new_traj_counts: dict[str, int] = {}  # track khi nào generate insights

    # ── Public API ────────────────────────────────────────────────

    def record(
        self,
        workflow_name: str,
        *,
        success: bool,
        duration_s: float,
        steps: list[StepTrace] | None = None,
        failed_at: str | None = None,
        vlm_calls: int = 0,
        memory_hits: int = 0,
        reflection_used: bool = False,
        goal: str = "",
    ) -> None:
        """
        Ghi nhận 1 lần chạy workflow vào trajectory history.

        Gọi sau khi workflow.run() kết thúc (success hoặc fail).

        Args:
            workflow_name:    "workflow_spider_social_post" (hoặc tên khác)
            success:          workflow có thành công không
            duration_s:       tổng thời gian (giây)
            steps:            danh sách StepTrace, None nếu không track bước
            failed_at:        tên bước fail ("B6_fb_composer"), None nếu success
            vlm_calls:        tổng VLM calls trong workflow
            memory_hits:      số lần ClickMemory cache hit
            reflection_used:  có dùng ReflectionSupervisor không
            goal:             goal string (để hash và cluster)
        """
        self._ensure_loaded()

        import hashlib
        goal_hash = hashlib.md5(goal[:100].encode()).hexdigest()[:8] if goal else ""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        entry = TrajectoryEntry(
            ts=ts,
            success=success,
            steps=steps or [],
            failed_at=failed_at,
            duration_s=duration_s,
            vlm_calls=vlm_calls,
            memory_hits=memory_hits,
            reflection_used=reflection_used,
            goal_hash=goal_hash,
        )

        with self._lock:
            wf_data = self._data.setdefault(workflow_name, {
                "success_count": 0,
                "fail_count": 0,
                "avg_duration_s": 0.0,
                "trajectory_history": [],
                "learned_insights": [],
            })

            # Cập nhật counts
            if success:
                wf_data["success_count"] = wf_data.get("success_count", 0) + 1
            else:
                wf_data["fail_count"] = wf_data.get("fail_count", 0) + 1

            # Cập nhật avg duration (exponential moving average)
            old_avg = wf_data.get("avg_duration_s", 0.0)
            total = wf_data["success_count"] + wf_data["fail_count"]
            alpha = min(0.3, 2.0 / (total + 1))
            wf_data["avg_duration_s"] = round(old_avg * (1 - alpha) + duration_s * alpha, 1)

            # Thêm trajectory
            history: list = wf_data.setdefault("trajectory_history", [])
            history.append(entry.to_dict())

            # Evict cũ nếu quá max
            if len(history) > self._max_history:
                history[:] = history[-self._max_history:]

            # Track để auto-generate insights
            self._new_traj_counts[workflow_name] = (
                self._new_traj_counts.get(workflow_name, 0) + 1
            )

        emoji = "✅" if success else "❌"
        _vlog("📈", f"Trajectory recorded: {workflow_name} {emoji} "
              f"{duration_s:.0f}s vlm={vlm_calls} mem_hits={memory_hits} "
              f"total={wf_data['success_count']+wf_data['fail_count']}")

        # Auto-generate insights mỗi N trajectories
        if self._new_traj_counts.get(workflow_name, 0) >= self._auto_every:
            self._new_traj_counts[workflow_name] = 0
            # Chạy trong background thread để không block workflow
            t = threading.Thread(
                target=lambda: asyncio.run(self._generate_insights_bg(workflow_name)),
                daemon=True,
            )
            t.start()

        self._save_async()

    def get_shortcuts(self, workflow_name: str) -> Shortcuts:
        """
        Đọc shortcuts từ trajectory history trước khi chạy workflow.

        Trả về Shortcuts object với các flag đã được học.
        Nếu không có data → Shortcuts() mặc định (không skip gì).
        """
        self._ensure_loaded()

        with self._lock:
            wf_data = self._data.get(workflow_name, {})

        if not wf_data:
            return Shortcuts()

        insights = [
            Insight.from_dict(d)
            for d in wf_data.get("learned_insights", [])
        ]

        # Chỉ dùng insights đã reliable (hoặc confidence cao từ đầu)
        active = [i for i in insights if i.is_reliable or i.confidence >= 0.85]

        shortcuts = Shortcuts(
            expected_duration_s=wf_data.get("avg_duration_s", 0.0),
            insights=active,
        )

        # Map shortcut keys → Shortcuts fields
        active_keys = {i.shortcut for i in active}
        if "skip_url_download" in active_keys:
            shortcuts.skip_url_download = True
        if "skip_js_verify" in active_keys:
            shortcuts.skip_js_verify = True
        if "skip_new_tab" in active_keys:
            shortcuts.skip_new_tab = True
        if "prefer_zoom_b6" in active_keys:
            shortcuts.prefer_zoom_b6 = True
        if "reflection_helps" in active_keys:
            shortcuts.reflection_helps = True

        # Upload tier
        tier_insights = [i for i in active if i.shortcut == "preferred_upload_tier"]
        if tier_insights:
            action = tier_insights[-1].action
            for tier_n in [1, 2, 3]:
                if f"Tier {tier_n}" in action:
                    shortcuts.preferred_upload_tier = tier_n
                    break

        # Average VLM calls từ recent successful trajectories
        history = wf_data.get("trajectory_history", [])
        recent_success = [
            t for t in history[-10:] if t.get("success", False)
        ]
        if recent_success:
            shortcuts.avg_vlm_calls = int(
                sum(t.get("vlm_calls", 0) for t in recent_success) / len(recent_success)
            )

        return shortcuts

    def get_stats(self, workflow_name: str | None = None) -> dict:
        """Thống kê trajectory memory."""
        self._ensure_loaded()
        with self._lock:
            if workflow_name:
                wf = self._data.get(workflow_name, {})
                return {
                    "workflow": workflow_name,
                    "success_count": wf.get("success_count", 0),
                    "fail_count": wf.get("fail_count", 0),
                    "avg_duration_s": wf.get("avg_duration_s", 0.0),
                    "trajectory_count": len(wf.get("trajectory_history", [])),
                    "insights_count": len(wf.get("learned_insights", [])),
                    "active_insights": sum(
                        1 for i in wf.get("learned_insights", [])
                        if Insight.from_dict(i).is_reliable
                    ),
                }
            return {
                "workflows": list(self._data.keys()),
                "total_trajectories": sum(
                    len(v.get("trajectory_history", []))
                    for v in self._data.values()
                ),
                "total_insights": sum(
                    len(v.get("learned_insights", []))
                    for v in self._data.values()
                ),
            }

    def record_insight_outcome(
        self,
        workflow_name: str,
        shortcut_key: str,
        success: bool,
    ) -> None:
        """
        Gọi sau khi workflow dùng shortcut → update success_rate.
        Insight xấu (success_rate < 40%) sẽ tự động bị evict.
        """
        self._ensure_loaded()
        with self._lock:
            wf_data = self._data.get(workflow_name, {})
            insights = wf_data.get("learned_insights", [])
            for d in insights:
                if d.get("shortcut") == shortcut_key:
                    d["applied_count"] = d.get("applied_count", 0) + 1
                    if success:
                        d["success_count"] = d.get("success_count", 0) + 1
                    d["last_applied"] = datetime.now(timezone.utc).isoformat()

                    # Evict nếu quá tệ
                    applied = d["applied_count"]
                    sr = d["success_count"] / max(applied, 1)
                    if applied >= 5 and sr < 0.40:
                        _vlog("🗑️", f"Evicting bad insight '{shortcut_key}' "
                              f"sr={sr:.0%} ({applied} trials)")
                        insights.remove(d)
                    break
        self._save_async()

    # ── Insight generation ────────────────────────────────────────

    async def generate_insights(self, workflow_name: str) -> list[Insight]:
        """
        Phân tích trajectory history và generate insights.

        Tier 1: Rule-based (luôn chạy, <5ms)
        Tier 2: LLM analysis (chỉ khi có llm_fn và > 5 trajectories)

        Returns: list[Insight] mới được thêm vào DB
        """
        self._ensure_loaded()

        with self._lock:
            wf_data = self._data.get(workflow_name, {})
            if not wf_data:
                return []
            history_dicts = wf_data.get("trajectory_history", [])
            existing_insights = wf_data.get("learned_insights", [])

        if len(history_dicts) < 2:
            return []

        history = [TrajectoryEntry.from_dict(d) for d in history_dicts]
        existing_shortcuts = {d.get("shortcut", "") for d in existing_insights}

        # Tier 1: Rule-based
        new_insights = _extract_rule_insights(history, existing_shortcuts)

        # Tier 2: LLM (optional, chỉ khi có >= 5 trajectories)
        if self._llm and len(history) >= 5:
            llm_insights = await self._llm_generate_insights(
                workflow_name, history, existing_shortcuts
            )
            # Merge — tránh duplicate shortcut keys
            llm_shortcuts = {i.shortcut for i in new_insights}
            for ins in llm_insights:
                if ins.shortcut not in llm_shortcuts and ins.shortcut not in existing_shortcuts:
                    new_insights.append(ins)

        if not new_insights:
            _vlog("📊", f"No new insights for {workflow_name} "
                  f"(analyzed {len(history)} trajectories)")
            return []

        # Save new insights
        with self._lock:
            wf_data = self._data.setdefault(workflow_name, {})
            insights_list: list = wf_data.setdefault("learned_insights", [])
            for ins in new_insights:
                insights_list.append(ins.to_dict())

        _vlog("💡", f"Generated {len(new_insights)} new insights for {workflow_name}: "
              f"{[i.shortcut for i in new_insights]}")
        self._save_async()
        return new_insights

    async def _generate_insights_bg(self, workflow_name: str) -> None:
        """Background wrapper cho generate_insights."""
        try:
            await self.generate_insights(workflow_name)
        except Exception as exc:
            _log.debug("Insight generation error: %s", exc)

    async def _llm_generate_insights(
        self,
        workflow_name: str,
        history: list[TrajectoryEntry],
        existing_shortcuts: set[str],
    ) -> list[Insight]:
        """
        Dùng LLM để phân tích các pattern phức tạp hơn rules.
        Chỉ gọi khi có >= 5 trajectories và llm_fn được cấu hình.
        """
        # Tóm tắt history thành text ngắn gọn
        summary_lines = []
        for i, t in enumerate(history[-10:], 1):  # chỉ dùng 10 gần nhất
            steps_str = ", ".join(
                f"{s.name}({s.duration_s:.0f}s,vlm={s.vlm_calls}"
                + (f",note={s.note}" if s.note else "") + ")"
                for s in t.steps
            )
            summary_lines.append(
                f"  Run {i}: {'✅' if t.success else '❌'} {t.duration_s:.0f}s "
                f"vlm={t.vlm_calls} mem={t.memory_hits} | {steps_str}"
            )

        existing_str = ", ".join(existing_shortcuts) if existing_shortcuts else "none"

        prompt = f"""Analyze these {workflow_name} execution trajectories and find optimization patterns.

Trajectories (recent {len(summary_lines)} runs):
{chr(10).join(summary_lines)}

Already known shortcuts: {existing_str}

Find NEW patterns not already captured. Look for:
1. Steps that consistently fail first then succeed with fallback (→ skip initial attempt)
2. Steps consuming many VLM calls (→ use faster strategy)
3. Steps that are sometimes unnecessary (→ conditional skip)
4. Timing patterns (→ add wait or reduce wait)

Respond with JSON array (max 3 insights, only if confidence >= 0.70):
[
  {{
    "insight": "brief description of pattern found",
    "shortcut": "snake_case_key",
    "action": "specific action to take (1 sentence)",
    "confidence": 0.0-1.0
  }}
]
If no new patterns found, respond: []
Do NOT reproduce existing shortcuts. JSON only, no markdown."""

        try:
            import json as _json
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._llm, prompt),
                timeout=20.0,
            )
            if asyncio.iscoroutine(raw):
                raw = await raw

            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = "\n".join(cleaned.split("\n")[1:-1])

            items = _json.loads(cleaned)
            if not isinstance(items, list):
                return []

            ts_now = datetime.now(timezone.utc).isoformat()
            results = []
            for item in items[:3]:
                if not isinstance(item, dict):
                    continue
                conf = float(item.get("confidence", 0.5))
                if conf < 0.70:
                    continue
                results.append(Insight(
                    id=f"ins_{int(time.time())}_{item.get('shortcut','x')[:8]}",
                    insight=str(item.get("insight", ""))[:200],
                    shortcut=str(item.get("shortcut", ""))[:50],
                    action=str(item.get("action", ""))[:200],
                    confidence=conf,
                    created_ts=ts_now,
                ))
            return results

        except Exception as exc:
            _log.debug("LLM insight generation failed: %s", exc)
            return []

    # ── Storage ───────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    self._data = raw
                total = sum(
                    len(v.get("trajectory_history", []))
                    for v in self._data.values()
                )
                insights_total = sum(
                    len(v.get("learned_insights", []))
                    for v in self._data.values()
                )
                _vlog("📚", f"TrajectoryDB loaded: {len(self._data)} workflows, "
                      f"{total} trajectories, {insights_total} insights")
            except Exception as exc:
                _log.warning("TrajectoryDB load failed: %s", exc)
                self._data = {}
        else:
            self._data = {}
            _vlog("📚", f"TrajectoryDB: new file at {self._path}")
        self._loaded = True

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            _log.warning("TrajectoryDB save failed: %s", exc)

    def _save_async(self) -> None:
        data_copy = json.loads(json.dumps(self._data))
        t = threading.Thread(
            target=lambda: self._save_with_data(data_copy),
            daemon=True,
        )
        t.start()

    def _save_with_data(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            _log.warning("TrajectoryDB background save failed: %s", exc)


# ══════════════════════════════════════════════════════════════════
# StepTracker — helper dùng trong workflow để track steps dễ hơn
# ══════════════════════════════════════════════════════════════════

class StepTracker:
    """
    Context manager nhẹ để track từng bước trong workflow.

    Usage trong workflow_spider_social_post.py:
        tracker = StepTracker()

        with tracker.step("B2_chatgpt_image"):
            image_path = await self._step2_create_image()

        # Khi B2 dùng vision thay URL:
        tracker.add_note("B2_chatgpt_image", "url_403_fallback_vision")

        # Cuối workflow:
        trajectory_db.record(
            "workflow_spider_social_post",
            success=result.success,
            duration_s=result.duration_s,
            steps=tracker.get_steps(),
            ...
        )
    """

    def __init__(self) -> None:
        self._steps: list[StepTrace] = []
        self._current: StepTrace | None = None
        self._current_start: float = 0.0

    def start_step(self, name: str) -> None:
        """Bắt đầu track 1 bước."""
        if self._current:
            self._finish_current(success=True)
        self._current = StepTrace(name=name)
        self._current_start = time.monotonic()

    def finish_step(self, success: bool = True, note: str = "") -> None:
        """Kết thúc bước hiện tại."""
        if self._current:
            self._current.duration_s = time.monotonic() - self._current_start
            self._current.success = success
            if note:
                self._current.note = note
            self._steps.append(self._current)
            self._current = None

    def add_vlm_call(self, step_name: str | None = None) -> None:
        """Ghi nhận 1 VLM call vào bước hiện tại hoặc bước theo tên."""
        step = self._find_step(step_name)
        if step:
            step.vlm_calls += 1

    def add_memory_hit(self, step_name: str | None = None) -> None:
        """Ghi nhận 1 ClickMemory cache hit."""
        step = self._find_step(step_name)
        if step:
            step.memory_hits += 1

    def add_retry(self, step_name: str | None = None) -> None:
        """Ghi nhận 1 retry."""
        step = self._find_step(step_name)
        if step:
            step.retries += 1

    def add_note(self, step_name: str, note: str) -> None:
        """Thêm note vào bước theo tên (tìm trong steps đã xong)."""
        for s in reversed(self._steps):
            if s.name == step_name:
                s.note = note
                return
        if self._current and self._current.name == step_name:
            self._current.note = note

    def get_steps(self) -> list[StepTrace]:
        """Lấy tất cả steps đã complete."""
        if self._current:
            self._finish_current(success=True)
        return list(self._steps)

    def total_vlm_calls(self) -> int:
        return sum(s.vlm_calls for s in self._steps) + (
            self._current.vlm_calls if self._current else 0
        )

    def total_memory_hits(self) -> int:
        return sum(s.memory_hits for s in self._steps) + (
            self._current.memory_hits if self._current else 0
        )

    def _find_step(self, name: str | None) -> StepTrace | None:
        if name is None:
            return self._current
        if self._current and self._current.name == name:
            return self._current
        for s in reversed(self._steps):
            if s.name == name:
                return s
        return None

    def _finish_current(self, success: bool) -> None:
        if self._current:
            self._current.duration_s = time.monotonic() - self._current_start
            self._current.success = success
            self._steps.append(self._current)
            self._current = None
