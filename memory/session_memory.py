# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/session_memory.py — Phidipus A3 Session Memory v2.1.3
═══════════════════════════════════════════════════════════════

A3: Session Memory — Nhớ trạng thái trong 1 phiên làm việc.

Vấn đề:
  Mỗi workflow bắt đầu lại từ đầu — Phidipus không biết:
    - Chrome đang mở tab nào → mở lại trang đã mở (lãng phí 3-5s)
    - Đã nhập nội dung vào ô text nào → nhập lại nội dung đã có
    - Dialog/modal nào đang mở → click nhầm vào nền khi modal mở
    - Action nào đã làm → retry action đã thành công (ALREADY_DONE nhưng
      phát hiện muộn, sau khi đã tốn VLM call)

Giải pháp — SessionMemory:
  Singleton in-memory (+ optional persist) lưu trạng thái phiên hiện tại.
  TTL: 2 giờ (session tự expire khi không hoạt động).

  Workflow executor check SessionMemory TRƯỚC khi thực hiện actions:
    - _exec_chrome navigate: is_tab_open(url) → skip nếu đã mở
    - _exec_vision_click:   was_action_done(domain, key) → skip nếu đã làm
    - _exec_type_text:      is_text_present(domain, selector) → skip nếu đã có

  Workflow executor UPDATE SessionMemory SAU mỗi action thành công:
    - record_navigation(url, title)
    - record_action(domain, action_key, success)
    - record_text_typed(domain, selector, text)

Schema (in-memory, không persist mặc định):
{
  "session_id":    "uuid",
  "started_at":   float (epoch),
  "last_active":  float (epoch),
  "open_tabs": [
    {"url": "https://facebook.com/...", "title": "Facebook", "visited_at": float}
  ],
  "last_actions": {
    "facebook.com": {
      "fb_post_button": {"ts": float, "success": True, "result": "posted"},
      "fb_composer_open": {"ts": float, "success": True}
    }
  },
  "typed_texts": {
    "facebook.com:div[contenteditable]": "Nội dung bài post..."
  },
  "active_dialog": "",
  "clipboard":     "",
  "workflow_vars": {}   # cross-node shared state
}

Usage:
    sess = get_session_memory()

    # Trước _exec_chrome navigate
    if sess.is_tab_open("facebook.com"):
        skip_navigate = True

    # Sau navigate thành công
    sess.record_navigation("https://facebook.com", "Facebook")

    # Trước _exec_vision_click
    if sess.was_action_done("facebook.com", "fb_post_button", within_seconds=300):
        return {"success": True, "skipped": True, "result": "session_cache_hit"}

    # Sau vision_click thành công
    sess.record_action("facebook.com", "fb_post_button", success=True, result="posted")

    # Cross-node shared vars
    sess.set_var("generated_image_path", "/tmp/chatgpt_img.png")
    path = sess.get_var("generated_image_path")
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# TTL mặc định: 2 giờ không hoạt động → session expire
_SESSION_TTL_SECONDS = 7200


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class TabEntry:
    """1 Chrome tab đang mở."""
    url:        str
    title:      str = ""
    visited_at: float = field(default_factory=time.time)

    def matches(self, url_fragment: str) -> bool:
        """Check xem tab này có match với URL fragment không."""
        fragment = url_fragment.lower().rstrip("/")
        return (
            fragment in self.url.lower()
            or fragment in self.title.lower()
        )


@dataclass
class ActionRecord:
    """Kết quả của 1 action đã thực hiện trong session."""
    ts:      float = field(default_factory=time.time)
    success: bool  = True
    result:  str   = ""


# ══════════════════════════════════════════════════════════════════
# SessionMemory
# ══════════════════════════════════════════════════════════════════

class SessionMemory:
    """
    A3 Session Memory — Trạng thái phiên làm việc hiện tại.

    Singleton in-memory. Tự expire sau TTL giây không hoạt động.
    Thread-safe cho asyncio (single event loop).
    """

    def __init__(self, ttl_seconds: int = _SESSION_TTL_SECONDS) -> None:
        self._ttl          = ttl_seconds
        self._session_id   = str(uuid.uuid4())[:8]
        self._started_at   = time.time()
        self._last_active  = time.time()

        # Chrome tabs đang mở (ordered by visited_at, newest first)
        self._open_tabs: list[TabEntry] = []

        # {domain → {action_key → ActionRecord}}
        self._last_actions: dict[str, dict[str, ActionRecord]] = {}

        # {domain:selector → text} — text đã được type
        self._typed_texts: dict[str, str] = {}

        # Dialog/modal đang active
        self._active_dialog: str = ""

        # Clipboard content hiện tại
        self._clipboard: str = ""

        # Cross-node shared variables
        self._workflow_vars: dict[str, Any] = {}

        _vlog("📋", f"SessionMemory started [id={self._session_id}] TTL={ttl_seconds//60}min")

    # ── Lifecycle ─────────────────────────────────────────────────

    def is_expired(self) -> bool:
        """Check xem session đã expire chưa."""
        return time.time() - self._last_active > self._ttl

    def _touch(self) -> None:
        """Cập nhật last_active timestamp."""
        self._last_active = time.time()

    def clear(self) -> None:
        """Reset toàn bộ session state (workflow mới)."""
        self._session_id  = str(uuid.uuid4())[:8]
        self._started_at  = time.time()
        self._last_active = time.time()
        self._open_tabs.clear()
        self._last_actions.clear()
        self._typed_texts.clear()
        self._active_dialog = ""
        self._clipboard = ""
        self._workflow_vars.clear()
        _vlog("📋", f"SessionMemory cleared [new id={self._session_id}]")

    # ── Tab Management ────────────────────────────────────────────

    def record_navigation(self, url: str, title: str = "") -> None:
        """
        Ghi nhận Chrome đã navigate tới URL.
        Nếu URL đã có trong open_tabs → update visited_at.
        """
        if not url:
            return
        self._touch()
        # Check xem tab này đã có chưa
        for tab in self._open_tabs:
            if tab.url == url:
                tab.visited_at = time.time()
                tab.title = title or tab.title
                _vlog("📋", f"Session: tab updated [{title or url[:50]}]")
                return
        # Tab mới
        self._open_tabs.append(TabEntry(url=url, title=title))
        # Giữ tối đa 20 tabs (xóa cũ nhất)
        if len(self._open_tabs) > 20:
            self._open_tabs.sort(key=lambda t: t.visited_at, reverse=True)
            self._open_tabs = self._open_tabs[:20]
        _vlog("📋", f"Session: tab recorded [{title or url[:50]}]")

    def is_tab_open(self, url_fragment: str) -> bool:
        """
        Check Chrome có đang mở tab với URL/title chứa url_fragment không.
        Ví dụ: is_tab_open("facebook.com") → True nếu có tab Facebook mở.
        """
        if not url_fragment or self.is_expired():
            return False
        return any(tab.matches(url_fragment) for tab in self._open_tabs)

    def get_open_tabs(self) -> list[dict]:
        """Danh sách tabs đang mở (cho Admin Panel)."""
        return [
            {"url": t.url, "title": t.title, "visited_at": t.visited_at}
            for t in sorted(self._open_tabs, key=lambda x: x.visited_at, reverse=True)
        ]

    def record_tab_closed(self, url_fragment: str) -> None:
        """Ghi nhận tab đã đóng."""
        self._open_tabs = [t for t in self._open_tabs if not t.matches(url_fragment)]

    # ── Action Tracking ───────────────────────────────────────────

    def record_action(
        self,
        domain: str,
        action_key: str,
        success: bool = True,
        result: str = "",
    ) -> None:
        """
        Ghi nhận action đã thực hiện (sau vision_click thành công).

        Args:
            domain:     Domain (vd: "facebook.com")
            action_key: Action key (vd: "fb_post_button")
            success:    Thành công hay thất bại
            result:     Mô tả kết quả ngắn (optional)
        """
        if not domain or not action_key:
            return
        self._touch()
        if domain not in self._last_actions:
            self._last_actions[domain] = {}
        self._last_actions[domain][action_key] = ActionRecord(
            success=success, result=result
        )
        status = "✅" if success else "❌"
        _vlog("📋", f"Session: action {status} [{domain}:{action_key}]")

    def was_action_done(
        self,
        domain: str,
        action_key: str,
        within_seconds: float | None = None,
        require_success: bool = True,
    ) -> bool:
        """
        Check xem action này đã được thực hiện trong session chưa.

        Args:
            domain:          Domain (vd: "facebook.com")
            action_key:      Action key
            within_seconds:  Nếu không None, chỉ tính nếu action < N giây trước
            require_success: Chỉ tính nếu action thành công (default True)

        Returns:
            True nếu action đã được thực hiện (và thành công nếu require_success)
        """
        if self.is_expired():
            return False
        record = self._last_actions.get(domain, {}).get(action_key)
        if record is None:
            return False
        if require_success and not record.success:
            return False
        if within_seconds is not None:
            if time.time() - record.ts > within_seconds:
                return False
        return True

    def get_last_action_result(self, domain: str, action_key: str) -> str:
        """Lấy result string của action đã làm."""
        record = self._last_actions.get(domain, {}).get(action_key)
        return record.result if record else ""

    # ── Text Tracking ─────────────────────────────────────────────

    def record_text_typed(
        self,
        domain: str,
        text: str,
        selector: str = "default",
    ) -> None:
        """
        Ghi nhận text đã được type vào 1 input trên domain.

        Args:
            domain:   Domain (vd: "facebook.com")
            text:     Nội dung đã type
            selector: CSS selector hoặc "default" (cho ô text chính)
        """
        if not domain:
            return
        self._touch()
        key = f"{domain}:{selector}"
        self._typed_texts[key] = text
        preview = text[:40] + ("..." if len(text) > 40 else "")
        _vlog("📋", f"Session: text typed [{domain}:{selector}] → '{preview}'")

    def get_typed_text(self, domain: str, selector: str = "default") -> str:
        """Lấy text đã type trên domain:selector."""
        if self.is_expired():
            return ""
        return self._typed_texts.get(f"{domain}:{selector}", "")

    def is_text_present(self, domain: str, expected_text: str, selector: str = "default") -> bool:
        """Check text đã được type chưa (để skip type_text redundant)."""
        typed = self.get_typed_text(domain, selector)
        return bool(typed) and expected_text[:50] in typed

    # ── Dialog Tracking ───────────────────────────────────────────

    def set_active_dialog(self, dialog_name: str) -> None:
        """Ghi nhận dialog/modal đang mở."""
        self._touch()
        self._active_dialog = dialog_name
        if dialog_name:
            _vlog("📋", f"Session: dialog opened [{dialog_name}]")

    def clear_active_dialog(self) -> None:
        """Ghi nhận dialog đã đóng."""
        if self._active_dialog:
            _vlog("📋", f"Session: dialog closed [{self._active_dialog}]")
        self._active_dialog = ""

    def get_active_dialog(self) -> str:
        """Dialog/modal đang mở hiện tại."""
        return self._active_dialog if not self.is_expired() else ""

    # ── Clipboard ─────────────────────────────────────────────────

    def set_clipboard(self, content: str) -> None:
        """Ghi nhận clipboard content hiện tại."""
        self._touch()
        self._clipboard = content

    def get_clipboard(self) -> str:
        """Clipboard content hiện tại."""
        return self._clipboard if not self.is_expired() else ""

    # ── Workflow Variables (cross-node shared state) ───────────────

    def set_var(self, key: str, value: Any) -> None:
        """
        Lưu biến shared giữa các nodes trong cùng workflow.
        Ví dụ: set_var("generated_image_path", "/tmp/img.png")
        """
        self._touch()
        self._workflow_vars[key] = value
        _vlog("📋", f"Session: var set [{key}] = {str(value)[:60]}")

    def get_var(self, key: str, default: Any = None) -> Any:
        """Lấy biến đã lưu. Returns default nếu không có hoặc session expired."""
        if self.is_expired():
            return default
        return self._workflow_vars.get(key, default)

    def has_var(self, key: str) -> bool:
        """Check biến đã được set chưa."""
        return key in self._workflow_vars and not self.is_expired()

    def clear_var(self, key: str) -> None:
        """Xóa 1 biến."""
        self._workflow_vars.pop(key, None)

    # ── Stats ─────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Session stats cho Admin Panel và logging."""
        age_s = int(time.time() - self._started_at)
        idle_s = int(time.time() - self._last_active)
        total_actions = sum(
            len(actions) for actions in self._last_actions.values()
        )
        return {
            "session_id":     self._session_id,
            "age_seconds":    age_s,
            "idle_seconds":   idle_s,
            "expired":        self.is_expired(),
            "open_tabs":      len(self._open_tabs),
            "domains_active": len(self._last_actions),
            "total_actions":  total_actions,
            "typed_inputs":   len(self._typed_texts),
            "workflow_vars":  len(self._workflow_vars),
            "active_dialog":  self._active_dialog,
        }

    def log_summary(self) -> None:
        """Log tóm tắt session state hiện tại."""
        s = self.stats()
        age_min = s["age_seconds"] // 60
        _vlog("📋", f"Session [{self._session_id}] age={age_min}min "
                    f"tabs={s['open_tabs']} actions={s['total_actions']} "
                    f"vars={s['workflow_vars']} expired={s['expired']}")


# ══════════════════════════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════════════════════════

_instance: SessionMemory | None = None


def get_session_memory() -> SessionMemory:
    """
    Singleton SessionMemory — dùng xuyên suốt workflow_executor.
    Tự tạo mới nếu session đã expired.
    """
    global _instance
    if _instance is None or _instance.is_expired():
        if _instance is not None and _instance.is_expired():
            _vlog("📋", "Session expired — starting new session")
        _instance = SessionMemory()
    return _instance


def reset_session() -> SessionMemory:
    """
    Force reset session (gọi khi bắt đầu workflow mới).
    Returns new SessionMemory instance.
    """
    global _instance
    _instance = SessionMemory()
    return _instance
