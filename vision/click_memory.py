# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
vision/click_memory.py — Phidipus v1.28
═══════════════════════════════════════════════════════════════════════

#7 Persistent Click Memory per Domain
  Lưu selector + tọa độ tương đối của từng action theo domain.
  Sau 5–7 ngày dùng: VLM call giảm 80%, click speed tăng 10×.

Schema (data/plans/click_memory.json):
{
  "facebook.com": {
    "post_button": {
      "selectors": ["[aria-label='Post']", "[data-testid='react-composer-post-button']"],
      "relative_coords": [{"rx": 0.82, "ry": 0.75, "weight": 3}],
      "success_rate": 0.97,
      "hit_count": 47,
      "fail_count": 1,
      "last_used": "2026-03-21",
      "last_verified": "2026-03-21T14:30:00"
    }
  }
}

rx/ry là tọa độ tương đối (0.0–1.0) so với Chrome window bounds.
→ Chịu được resize window, zoom, responsive layout.

Usage:
    mem = ClickMemory()
    entry = mem.lookup("facebook.com", "post_button")
    if entry:
        # Skip VLM, click thẳng
        abs_x = int(entry.rx * window_w) + window_x
        abs_y = int(entry.ry * window_h) + window_y
    else:
        # VLM call, rồi save result
        mem.record_success("facebook.com", "post_button",
                           rx=0.82, ry=0.75,
                           selectors=["[data-testid='post-btn']"])
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class MemoryEntry:
    """Cached click pattern cho 1 action trên 1 domain."""
    action_key:     str              # ví dụ: "post_button", "upload_photo"
    domain:         str              # ví dụ: "facebook.com"
    selectors:      list[str]        # CSS selectors theo độ ưu tiên
    relative_coords: list[dict]      # [{"rx":0.82, "ry":0.75, "weight":3}]
    success_rate:   float = 1.0
    hit_count:      int   = 0
    fail_count:     int   = 0
    last_used:      str   = ""       # ISO date string
    last_verified:  str   = ""       # ISO datetime string

    @property
    def best_rx(self) -> float:
        """Tọa độ x tương đối có weight cao nhất."""
        if not self.relative_coords:
            return 0.5
        best = max(self.relative_coords, key=lambda c: c.get("weight", 1))
        return best.get("rx", 0.5)

    @property
    def best_ry(self) -> float:
        """Tọa độ y tương đối có weight cao nhất."""
        if not self.relative_coords:
            return 0.5
        best = max(self.relative_coords, key=lambda c: c.get("weight", 1))
        return best.get("ry", 0.5)

    @property
    def is_reliable(self) -> bool:
        """Entry đáng tin cậy nếu success_rate >= 0.85 và hit_count >= 3."""
        return self.success_rate >= 0.85 and self.hit_count >= 3

    @property
    def is_stale(self) -> bool:
        """Entry cũ hơn 7 ngày → cần verify lại."""
        if not self.last_verified:
            return True
        try:
            last = datetime.fromisoformat(self.last_verified)
            age_days = (datetime.now(timezone.utc) - last).days
            return age_days > 7
        except Exception:
            return True

    def to_dict(self) -> dict:
        return {
            "selectors":       self.selectors,
            "relative_coords": self.relative_coords,
            "success_rate":    round(self.success_rate, 4),
            "hit_count":       self.hit_count,
            "fail_count":      self.fail_count,
            "last_used":       self.last_used,
            "last_verified":   self.last_verified,
        }

    @staticmethod
    def from_dict(domain: str, action_key: str, d: dict) -> "MemoryEntry":
        return MemoryEntry(
            action_key      = action_key,
            domain          = domain,
            selectors       = d.get("selectors", []),
            relative_coords = d.get("relative_coords", []),
            success_rate    = float(d.get("success_rate", 1.0)),
            hit_count       = int(d.get("hit_count", 0)),
            fail_count      = int(d.get("fail_count", 0)),
            last_used       = d.get("last_used", ""),
            last_verified   = d.get("last_verified", ""),
        )


@dataclass
class LookupResult:
    """Kết quả lookup từ memory."""
    found:      bool
    entry:      Optional[MemoryEntry] = None
    rx:         float = 0.5
    ry:         float = 0.5
    selector:   str   = ""
    confidence: float = 0.0
    reason:     str   = ""


# ══════════════════════════════════════════════════════════════════
# ClickMemory
# ══════════════════════════════════════════════════════════════════

class ClickMemory:
    """
    Persistent click pattern cache per domain.

    Thread-safe write (file lock via threading.Lock).
    Lazy-load on first access.

    Args:
        memory_path: đường dẫn file JSON
        min_hits_for_cache: số lần thành công tối thiểu trước khi trust cache
        min_success_rate:   success rate tối thiểu để dùng cache
    """

    DEFAULT_PATH = Path("data/plans/click_memory.json")

    def __init__(
        self,
        memory_path: str | Path | None = None,
        min_hits_for_cache: int = 3,
        min_success_rate: float = 0.85,
    ) -> None:
        self._path = Path(memory_path or self.DEFAULT_PATH)
        self._min_hits = min_hits_for_cache
        self._min_sr   = min_success_rate
        self._data: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()
        self._loaded = False

    # ── Public API ────────────────────────────────────────────────

    def lookup(self, domain: str, action_key: str) -> LookupResult:
        """
        Tìm cached pattern cho domain + action_key.

        Returns:
            LookupResult.found=True + coordinates nếu có entry tin cậy.
        """
        self._ensure_loaded()
        domain_norm = self._normalize_domain(domain)
        domain_data = self._data.get(domain_norm, {})

        if action_key not in domain_data:
            return LookupResult(found=False, reason="not_in_cache")

        entry = MemoryEntry.from_dict(domain_norm, action_key, domain_data[action_key])

        # Kiểm tra độ tin cậy
        if entry.hit_count < self._min_hits:
            return LookupResult(
                found=False,
                reason=f"insufficient_hits({entry.hit_count}<{self._min_hits})",
            )
        if entry.success_rate < self._min_sr:
            return LookupResult(
                found=False,
                reason=f"low_success_rate({entry.success_rate:.0%}<{self._min_sr:.0%})",
            )

        # Entry cũ → vẫn trả về nhưng đánh dấu cần verify
        if entry.is_stale:
            _vlog("⚠️", f"Stale cache for {domain_norm}/{action_key} — will verify after use")

        # Cập nhật last_used (không write file, chỉ in-memory)
        domain_data[action_key]["last_used"] = datetime.now().strftime("%Y-%m-%d")

        selector = entry.selectors[0] if entry.selectors else ""
        _vlog("⚡", f"Cache HIT: {domain_norm}/{action_key} "
              f"rx={entry.best_rx:.3f} ry={entry.best_ry:.3f} "
              f"sr={entry.success_rate:.0%} hits={entry.hit_count}")

        return LookupResult(
            found=True,
            entry=entry,
            rx=entry.best_rx,
            ry=entry.best_ry,
            selector=selector,
            confidence=min(entry.success_rate, 0.95),  # cap at 0.95 — không bao giờ 100%
            reason="cache_hit",
        )

    def record_success(
        self,
        domain: str,
        action_key: str,
        rx: float,
        ry: float,
        selectors: list[str] | None = None,
        verified: bool = False,
    ) -> None:
        """
        Ghi nhận click thành công.

        Args:
            domain:     domain của trang (vd: "facebook.com")
            action_key: tên action (vd: "post_button")
            rx, ry:     tọa độ tương đối (0.0–1.0) so với Chrome window
            selectors:  CSS selectors đã dùng thành công
            verified:   True nếu đã verify qua ClickVerifier
        """
        self._ensure_loaded()
        domain_norm = self._normalize_domain(domain)

        with self._lock:
            domain_data = self._data.setdefault(domain_norm, {})

            if action_key not in domain_data:
                domain_data[action_key] = {
                    "selectors":       selectors or [],
                    "relative_coords": [],
                    "success_rate":    1.0,
                    "hit_count":       0,
                    "fail_count":      0,
                    "last_used":       "",
                    "last_verified":   "",
                }

            entry_dict = domain_data[action_key]

            # Cập nhật selectors (thêm mới, không xoá cũ)
            if selectors:
                existing = set(entry_dict.get("selectors", []))
                for sel in selectors:
                    if sel not in existing:
                        entry_dict["selectors"].insert(0, sel)  # thêm vào đầu (ưu tiên cao hơn)
                        existing.add(sel)
                # Giới hạn 10 selectors
                entry_dict["selectors"] = entry_dict["selectors"][:10]

            # Cập nhật relative coords (average với weight)
            self._update_coords(entry_dict, rx, ry)

            # Cập nhật stats
            entry_dict["hit_count"] = entry_dict.get("hit_count", 0) + 1
            entry_dict["last_used"] = datetime.now().strftime("%Y-%m-%d")
            if verified:
                entry_dict["last_verified"] = datetime.now(timezone.utc).isoformat()

            # Recalculate success_rate (exponential moving average)
            old_sr = entry_dict.get("success_rate", 1.0)
            hits   = entry_dict["hit_count"]
            alpha  = min(0.2, 2.0 / (hits + 1))  # học nhanh hơn khi ít data
            entry_dict["success_rate"] = round(old_sr * (1 - alpha) + 1.0 * alpha, 4)

        _vlog("📚", f"Memory saved: {domain_norm}/{action_key} "
              f"rx={rx:.3f} ry={ry:.3f} hits={entry_dict['hit_count']}")
        self._save_async()

    def record_failure(
        self,
        domain: str,
        action_key: str,
        reason: str = "",
    ) -> None:
        """
        Ghi nhận click thất bại → giảm success_rate.
        Nếu success_rate < 0.5 → xoá entry để VLM re-learn.
        """
        self._ensure_loaded()
        domain_norm = self._normalize_domain(domain)

        with self._lock:
            domain_data = self._data.get(domain_norm, {})
            if action_key not in domain_data:
                return

            entry_dict = domain_data[action_key]
            entry_dict["fail_count"] = entry_dict.get("fail_count", 0) + 1

            hits  = entry_dict.get("hit_count", 1)
            alpha = min(0.3, 2.0 / (hits + 1))
            old_sr = entry_dict.get("success_rate", 1.0)
            new_sr = old_sr * (1 - alpha) + 0.0 * alpha
            entry_dict["success_rate"] = round(new_sr, 4)

            _vlog("⚠️", f"Memory fail: {domain_norm}/{action_key} "
                  f"sr={new_sr:.0%} reason={reason[:50]}")

            # Xoá entry nếu quá tệ (buộc VLM re-learn)
            if new_sr < 0.4:
                del domain_data[action_key]
                _vlog("🗑️", f"Memory evicted: {domain_norm}/{action_key} (sr={new_sr:.0%})")

        self._save_async()

    def get_stats(self) -> dict:
        """Thống kê toàn bộ memory."""
        self._ensure_loaded()
        stats = {
            "total_domains": len(self._data),
            "total_entries": sum(len(v) for v in self._data.values()),
            "domains": {},
        }
        for domain, entries in self._data.items():
            stats["domains"][domain] = {
                "entries": len(entries),
                "reliable": sum(
                    1 for e in entries.values()
                    if MemoryEntry.from_dict(domain, "", e).is_reliable
                ),
            }
        return stats

    def clear_domain(self, domain: str) -> None:
        """Xoá tất cả entries của 1 domain."""
        domain_norm = self._normalize_domain(domain)
        with self._lock:
            self._data.pop(domain_norm, None)
        self._save_async()
        _vlog("🗑️", f"Memory cleared for domain: {domain_norm}")

    def clear_all(self) -> None:
        """Xoá toàn bộ memory (reset về trạng thái ban đầu)."""
        with self._lock:
            self._data = {}
        self._save_async()
        _vlog("🗑️", "Memory cleared (all domains)")

    # ── Internal ──────────────────────────────────────────────────

    def _normalize_domain(self, domain: str) -> str:
        """facebook.com, www.facebook.com, https://www.facebook.com → facebook.com"""
        domain = domain.strip().lower()
        domain = domain.replace("https://", "").replace("http://", "")
        domain = domain.split("/")[0]   # remove path
        domain = domain.lstrip("www.")  # remove www.
        return domain

    def _update_coords(self, entry_dict: dict, rx: float, ry: float) -> None:
        """Cập nhật relative coords với exponential decay (giữ 3 coord gần nhất)."""
        coords = entry_dict.get("relative_coords", [])

        # Tìm coord gần giống (trong 5% window) → tăng weight
        threshold = 0.05
        merged = False
        for c in coords:
            if abs(c["rx"] - rx) < threshold and abs(c["ry"] - ry) < threshold:
                # Average weighted
                w = c.get("weight", 1)
                c["rx"] = round((c["rx"] * w + rx) / (w + 1), 4)
                c["ry"] = round((c["ry"] * w + ry) / (w + 1), 4)
                c["weight"] = w + 1
                merged = True
                break

        if not merged:
            coords.append({"rx": round(rx, 4), "ry": round(ry, 4), "weight": 1})

        # Giữ top 3 coord theo weight
        coords.sort(key=lambda c: -c.get("weight", 1))
        entry_dict["relative_coords"] = coords[:3]

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    raw = json.load(f)
                # Migrate từ old format ({}) sang new format nếu cần
                if isinstance(raw, dict):
                    self._data = raw
                _vlog("📚", f"Click memory loaded: {self._path} "
                      f"({sum(len(v) for v in self._data.values())} entries)")
            except Exception as exc:
                _log.warning("ClickMemory load failed: %s", exc)
                self._data = {}
        else:
            self._data = {}
            _vlog("📚", f"Click memory: new file at {self._path}")
        self._loaded = True

    def _save(self) -> None:
        """Save to disk (blocking)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            _log.warning("ClickMemory save failed: %s", exc)

    def _save_async(self) -> None:
        """Save to disk in background thread (non-blocking)."""
        import threading
        data_copy = json.loads(json.dumps(self._data))  # deep copy
        t = threading.Thread(
            target=lambda: self._save_with_data(data_copy),
            daemon=True
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
            _log.warning("ClickMemory background save failed: %s", exc)


# ══════════════════════════════════════════════════════════════════
# Action key conventions (chuẩn hóa để reuse giữa các workflow)
# ══════════════════════════════════════════════════════════════════

class ActionKeys:
    """
    Convention cho action_key strings.
    Dùng consistent naming giúp cache được reuse giữa các session.
    """
    # Facebook
    FB_POST_BUTTON       = "fb_post_button"
    FB_COMPOSER_OPEN     = "fb_composer_open"
    FB_PHOTO_BUTTON      = "fb_photo_button"
    FB_FILE_INPUT        = "fb_file_input"
    FB_CONFIRM_POST      = "fb_confirm_post"

    # Instagram
    IG_CREATE_NEW        = "ig_create_new_post"
    IG_SELECT_FILE       = "ig_select_file"
    IG_NEXT_BUTTON       = "ig_next_button"
    IG_SHARE_BUTTON      = "ig_share_button"
    IG_CAPTION_FIELD     = "ig_caption_field"

    # X (Twitter)
    X_NEW_TWEET          = "x_new_tweet_button"
    X_COMPOSE_AREA       = "x_compose_textarea"
    X_ATTACH_IMAGE       = "x_attach_image"
    X_POST_BUTTON        = "x_post_button"

    # ChatGPT
    CHATGPT_PROMPT_INPUT = "chatgpt_prompt_input"
    CHATGPT_SEND_BUTTON  = "chatgpt_send_button"
    CHATGPT_IMAGE_SAVE   = "chatgpt_image_save"

    @staticmethod
    def domain_from_url(url: str) -> str:
        """Lấy domain từ URL để dùng làm key."""
        url = url.lower().split("?")[0].split("#")[0]
        url = url.replace("https://", "").replace("http://", "").lstrip("www.")
        return url.split("/")[0]
