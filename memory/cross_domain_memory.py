# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/cross_domain_memory.py — Phidipus A2 CrossDomain Learning v2.1.3
═══════════════════════════════════════════════════════════════════════════

A2: CrossDomain Learning — Học từ domain này, áp dụng sang domain khác.

Vấn đề:
  ClickMemory nhớ tọa độ chính xác cho từng (domain, action_key).
  Nhưng khi Phidipus gặp domain MỚI (vd: lần đầu mở Instagram sau khi
  đã dùng Facebook nhiều tuần), nó phải tốn 15-25s VLM để tìm Submit
  button từ đầu — dù Submit button ở Facebook đã được học kỹ.

Giải pháp — CrossDomain UIPattern Library:
  Sau mỗi click thành công → infer button_type từ action_key/description
  → lưu vào pattern library kèm (rx_avg, ry_avg, selectors[]).

  Khi gặp domain mới và ClickMemory miss:
    1. Infer button_type từ description của node
    2. Query pattern library: "Submit button thường ở đâu?"
    3. Trả về CrossDomainHint: {rx_hint, ry_hint, selector_hints, confidence}
    4. VisionActor dùng hint để:
       - Pre-bias VLM prompt: "The button is likely near (0.82, 0.75)"
       - Thử selector_hints từ domain khác trước
       - Narrow ZoomAction crop region

UI Pattern Types (button_type):
  "submit"   — Post, Đăng, Submit, Send, Publish, OK, Confirm
  "upload"   — Upload, Attach, Add photo, Chọn ảnh
  "close"    — Close, X, Cancel, Đóng, Dismiss
  "next"     — Next, Continue, Tiếp theo, →
  "search"   — Search, Tìm kiếm, 🔍
  "menu"     — Menu, ☰, More, ...
  "login"    — Login, Sign in, Đăng nhập
  "input"    — Text area, Composer, What's on your mind
  "download" — Download, Save, Tải về
  "reaction" — Like, ❤️, Reaction button
  "unknown"  — Không xác định được

Storage: data/memory/cross_domain_memory.json
Schema:
{
  "submit": {
    "facebook.com": {
      "rx_samples": [0.81, 0.82, 0.83],
      "ry_samples": [0.74, 0.75, 0.76],
      "rx_avg": 0.82, "ry_avg": 0.75,
      "selectors": ["[aria-label='Post']", "[aria-label='Đăng']", "[type='submit']"],
      "success_count": 47, "last_updated": "2026-03-24"
    },
    "instagram.com": { ... }
  },
  "upload": { ... }
}

Integration:
  1. vision_actor.find_and_click_v2() gọi cross_domain.get_hint() khi ClickMemory miss
  2. vision_actor.find_and_click_v2() gọi cross_domain.record() sau mỗi success
  3. workflow_executor._exec_vision_click() inject cross_domain vào actor
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Button type inference
# ══════════════════════════════════════════════════════════════════

# Từ khoá → button_type (theo thứ tự ưu tiên, lowercase)
_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("submit",   ["post", "đăng", "submit", "send", "publish", "xuất bản",
                  "confirm", "xác nhận", "ok", "apply", "save", "lưu",
                  "done", "finish", "complete", "gửi"]),
    ("upload",   ["upload", "attach", "add photo", "chọn ảnh", "tải lên",
                  "add image", "add file", "browse", "select file"]),
    ("download", ["download", "save", "tải về", "tải xuống", "export", "xuất"]),
    ("close",    ["close", "x button", "cancel", "đóng", "dismiss",
                  "hủy", "thoát", "exit", "no thanks"]),
    ("next",     ["next", "continue", "tiếp theo", "proceed", "forward", "→", ">"]),
    ("search",   ["search", "tìm kiếm", "find", "🔍", "magnify"]),
    ("login",    ["login", "sign in", "đăng nhập", "log in", "authenticate"]),
    ("input",    ["text area", "composer", "what's on your mind", "hộp nhập",
                  "text box", "input box", "write something", "nhập nội dung",
                  "create post", "tạo bài"]),
    ("menu",     ["menu", "☰", "more options", "...", "three dot",
                  "hamburger", "settings", "cài đặt"]),
    ("reaction", ["like", "love", "❤️", "👍", "reaction", "emoji"]),
]


def infer_button_type(action_key: str, description: str) -> str:
    """
    Infer button_type từ action_key và description.
    Returns one of: submit, upload, download, close, next, search,
                    login, input, menu, reaction, unknown
    """
    text = f"{action_key} {description}".lower()
    for btype, keywords in _TYPE_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return btype
    return "unknown"


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class DomainPattern:
    """Pattern của 1 button_type trên 1 domain cụ thể."""
    domain:        str
    button_type:   str
    rx_samples:    list[float] = field(default_factory=list)
    ry_samples:    list[float] = field(default_factory=list)
    rx_avg:        float = 0.5
    ry_avg:        float = 0.5
    selectors:     list[str] = field(default_factory=list)
    success_count: int = 0
    last_updated:  str = ""   # ISO date

    @property
    def is_reliable(self) -> bool:
        """Đáng tin cậy khi có >= 3 mẫu thành công."""
        return self.success_count >= 3

    def add_sample(self, rx: float, ry: float, selectors: list[str]) -> None:
        """Thêm 1 mẫu thành công, cập nhật avg và selector list."""
        self.rx_samples.append(round(rx, 4))
        self.ry_samples.append(round(ry, 4))
        # Giữ tối đa 20 mẫu gần nhất
        if len(self.rx_samples) > 20:
            self.rx_samples = self.rx_samples[-20:]
            self.ry_samples = self.ry_samples[-20:]
        self.rx_avg = round(sum(self.rx_samples) / len(self.rx_samples), 4)
        self.ry_avg = round(sum(self.ry_samples) / len(self.ry_samples), 4)
        # Merge selectors (không trùng lặp, giữ tối đa 8)
        for sel in selectors:
            if sel and sel not in self.selectors:
                self.selectors.append(sel)
        self.selectors = self.selectors[:8]
        self.success_count += 1
        self.last_updated = date.today().isoformat()

    def to_dict(self) -> dict:
        return {
            "rx_samples":    self.rx_samples,
            "ry_samples":    self.ry_samples,
            "rx_avg":        self.rx_avg,
            "ry_avg":        self.ry_avg,
            "selectors":     self.selectors,
            "success_count": self.success_count,
            "last_updated":  self.last_updated,
        }

    @staticmethod
    def from_dict(domain: str, button_type: str, d: dict) -> "DomainPattern":
        p = DomainPattern(domain=domain, button_type=button_type)
        p.rx_samples    = d.get("rx_samples", [])
        p.ry_samples    = d.get("ry_samples", [])
        p.rx_avg        = float(d.get("rx_avg", 0.5))
        p.ry_avg        = float(d.get("ry_avg", 0.5))
        p.selectors     = d.get("selectors", [])
        p.success_count = int(d.get("success_count", 0))
        p.last_updated  = d.get("last_updated", "")
        return p


@dataclass
class CrossDomainHint:
    """
    Gợi ý vị trí + selectors cho 1 button_type trên domain mới,
    dựa trên kiến thức tích lũy từ các domain khác.
    """
    found:           bool
    button_type:     str     = "unknown"
    rx_hint:         float   = 0.5        # tọa độ x dự đoán (0.0–1.0)
    ry_hint:         float   = 0.5        # tọa độ y dự đoán (0.0–1.0)
    rx_std:          float   = 0.1        # độ lệch chuẩn (vùng tìm kiếm)
    ry_std:          float   = 0.1
    selector_hints:  list[str] = field(default_factory=list)
    confidence:      float   = 0.0        # 0.0–1.0
    source_domains:  list[str] = field(default_factory=list)
    source_count:    int     = 0          # tổng số mẫu từ các domain

    def vlm_prompt_hint(self) -> str:
        """Tạo hint ngắn để inject vào VLM prompt."""
        if not self.found or self.confidence < 0.4:
            return ""
        loc = f"around ({self.rx_hint:.2f}, {self.ry_hint:.2f}) relative to window"
        domains = ", ".join(self.source_domains[:3])
        return (
            f"[CrossDomain hint: Based on {self.source_count} samples from {domains}, "
            f"'{self.button_type}' buttons are typically located {loc}. "
            f"Confidence: {self.confidence:.0%}]"
        )

    def log(self) -> None:
        if self.found:
            _vlog("🌐", f"CrossDomain [{self.button_type}] hint: "
                        f"({self.rx_hint:.2f},{self.ry_hint:.2f}) "
                        f"conf={self.confidence:.0%} "
                        f"from {self.source_domains} ({self.source_count} samples)")
        else:
            _vlog("🌐", f"CrossDomain [{self.button_type}]: no hint available")


# ══════════════════════════════════════════════════════════════════
# CrossDomainMemory
# ══════════════════════════════════════════════════════════════════

class CrossDomainMemory:
    """
    A2 CrossDomain Learning — UIPattern Library.

    Tích lũy kiến thức về vị trí các loại button trên nhiều domain
    để giúp VisionActor tìm nhanh hơn trên domain mới.

    Usage:
        cdm = CrossDomainMemory()

        # Sau mỗi click thành công
        cdm.record(
            domain="facebook.com",
            action_key="fb_post_button",
            description="Post blue submit button",
            rx=0.82, ry=0.75,
            selectors=["[aria-label='Post']"]
        )

        # Khi gặp domain mới, ClickMemory miss
        hint = cdm.get_hint(
            description="Submit post button",
            target_domain="instagram.com"
        )
        if hint.found:
            # Pre-bias VLM prompt
            extra_prompt = hint.vlm_prompt_hint()
            # Thử selectors từ domain khác
            selectors = hint.selector_hints + existing_selectors
    """

    def __init__(self, storage_path: str = "data/memory/cross_domain_memory.json") -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # {button_type → {domain → DomainPattern}}
        self._patterns: dict[str, dict[str, DomainPattern]] = {}
        self._load()

    # ── Public API ────────────────────────────────────────────────

    def record(
        self,
        domain: str,
        action_key: str,
        description: str,
        rx: float,
        ry: float,
        selectors: list[str] | None = None,
    ) -> str:
        """
        Ghi nhận 1 click thành công → học pattern.

        Args:
            domain:      Domain trang (vd: "facebook.com")
            action_key:  Cache key (vd: "fb_post_button")
            description: Mô tả element (vd: "Post blue submit button")
            rx, ry:      Tọa độ tương đối (0.0–1.0)
            selectors:   CSS selectors đã dùng thành công

        Returns:
            button_type đã infer được
        """
        if not domain or not (0.0 <= rx <= 1.0) or not (0.0 <= ry <= 1.0):
            return "unknown"

        btype = infer_button_type(action_key, description)

        if btype not in self._patterns:
            self._patterns[btype] = {}

        if domain not in self._patterns[btype]:
            self._patterns[btype][domain] = DomainPattern(
                domain=domain, button_type=btype
            )

        pattern = self._patterns[btype][domain]
        pattern.add_sample(rx, ry, selectors or [])

        _vlog("🌐", f"CrossDomain.record [{btype}] {domain}: "
                    f"({rx:.2f},{ry:.2f}) count={pattern.success_count}")

        self._save_async()
        return btype

    def get_hint(
        self,
        description: str,
        target_domain: str,
        action_key: str = "",
        min_confidence: float = 0.3,
    ) -> CrossDomainHint:
        """
        Lấy gợi ý vị trí cho button trên target_domain dựa trên
        kiến thức từ các domain khác.

        Logic:
          1. Infer button_type từ description/action_key
          2. Lấy tất cả DomainPattern cho button_type này (trừ target_domain)
          3. Tính weighted average của rx/ry (weight = success_count)
          4. Merge selectors từ tất cả domain
          5. Tính confidence dựa trên số mẫu và số domain

        Args:
            description:   Mô tả element cần tìm
            target_domain: Domain đang tìm (để exclude khỏi training data)
            action_key:    Optional, để infer button_type chính xác hơn
            min_confidence: Ngưỡng confidence tối thiểu để trả về hint

        Returns:
            CrossDomainHint
        """
        btype = infer_button_type(action_key, description)

        # Nếu không infer được hoặc chưa có data
        if btype == "unknown" or btype not in self._patterns:
            return CrossDomainHint(found=False, button_type=btype)

        # Lấy patterns từ domain khác (không phải target_domain)
        source_patterns = [
            p for d, p in self._patterns[btype].items()
            if d != target_domain and p.is_reliable
        ]

        if not source_patterns:
            return CrossDomainHint(found=False, button_type=btype)

        # Weighted average (weight = success_count)
        total_weight = sum(p.success_count for p in source_patterns)
        rx_avg = sum(p.rx_avg * p.success_count for p in source_patterns) / total_weight
        ry_avg = sum(p.ry_avg * p.success_count for p in source_patterns) / total_weight

        # Độ lệch chuẩn (spread của các sample)
        all_rx = [rx for p in source_patterns for rx in p.rx_samples]
        all_ry = [ry for p in source_patterns for ry in p.ry_samples]
        rx_std = _std(all_rx) if len(all_rx) >= 2 else 0.15
        ry_std = _std(all_ry) if len(all_ry) >= 2 else 0.15

        # Merge selectors (unique, most common first)
        sel_freq: dict[str, int] = {}
        for p in source_patterns:
            for sel in p.selectors:
                sel_freq[sel] = sel_freq.get(sel, 0) + p.success_count
        selector_hints = sorted(sel_freq, key=sel_freq.get, reverse=True)[:6]

        # Confidence: log scale dựa trên total_weight và số domain
        n_domains = len(source_patterns)
        confidence = min(1.0, (total_weight / 20.0) * 0.6 + (n_domains / 5.0) * 0.4)

        if confidence < min_confidence:
            return CrossDomainHint(found=False, button_type=btype)

        hint = CrossDomainHint(
            found=True,
            button_type=btype,
            rx_hint=round(rx_avg, 4),
            ry_hint=round(ry_avg, 4),
            rx_std=round(rx_std, 4),
            ry_std=round(ry_std, 4),
            selector_hints=selector_hints,
            confidence=round(confidence, 4),
            source_domains=[p.domain for p in source_patterns],
            source_count=total_weight,
        )
        hint.log()
        return hint

    def get_all_hints_for_domain(self, target_domain: str) -> dict[str, CrossDomainHint]:
        """
        Lấy tất cả hints có sẵn cho 1 domain (B4 Proactive Screen Understanding).
        Returns: {button_type → CrossDomainHint}
        """
        result = {}
        for btype in self._patterns:
            hint = self.get_hint(
                description=btype,  # dùng btype làm description
                target_domain=target_domain,
                action_key=btype,
            )
            if hint.found:
                result[btype] = hint
        return result

    def stats(self) -> dict:
        """Stats cho Admin Panel."""
        total_patterns = sum(
            len(domains) for domains in self._patterns.values()
        )
        total_samples = sum(
            p.success_count
            for domains in self._patterns.values()
            for p in domains.values()
        )
        return {
            "button_types":    len(self._patterns),
            "total_patterns":  total_patterns,
            "total_samples":   total_samples,
            "by_type": {
                btype: {
                    "domains": list(domains.keys()),
                    "total_samples": sum(p.success_count for p in domains.values()),
                }
                for btype, domains in self._patterns.items()
            },
        }

    # ── Persistence ───────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            for btype, domains in raw.items():
                self._patterns[btype] = {}
                for domain, d in domains.items():
                    self._patterns[btype][domain] = DomainPattern.from_dict(
                        domain, btype, d
                    )
            n = sum(len(v) for v in self._patterns.values())
            _vlog("🌐", f"CrossDomainMemory loaded: {len(self._patterns)} types, {n} patterns")
        except Exception as exc:
            _vlog("⚠️", f"CrossDomainMemory load error: {exc}")

    def _save(self) -> None:
        try:
            raw = {
                btype: {
                    domain: p.to_dict()
                    for domain, p in domains.items()
                }
                for btype, domains in self._patterns.items()
            }
            self._path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception as exc:
            _vlog("⚠️", f"CrossDomainMemory save error: {exc}")

    def _save_async(self) -> None:
        """Save không block — gọi từ async context."""
        import threading
        threading.Thread(target=self._save, daemon=True).start()


# ══════════════════════════════════════════════════════════════════
# Helper
# ══════════════════════════════════════════════════════════════════

def _std(values: list[float]) -> float:
    """Tính độ lệch chuẩn đơn giản."""
    if len(values) < 2:
        return 0.1
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance ** 0.5


# ══════════════════════════════════════════════════════════════════
# Singleton helper
# ══════════════════════════════════════════════════════════════════

_instance: CrossDomainMemory | None = None


def get_cross_domain_memory() -> CrossDomainMemory:
    """Singleton instance — dùng trong workflow_executor và vision_actor."""
    global _instance
    if _instance is None:
        _instance = CrossDomainMemory()
    return _instance
