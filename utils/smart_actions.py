# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/smart_actions.py — Phidipus Smart Action Engine v9.19
═══════════════════════════════════════════════════════════

5-priority action system — fastest method first, VLM last resort.

Priority 1: Fast-Action     (< 1s)  — regex + AppScanner
Priority 2: Keyboard        (< 0.5s)— shortcuts đã biết
Priority 3: AppleScript     (1-3s)  — macOS native automation
Priority 4: LLM-only        (5-15s) — suy luận không cần nhìn
Priority 5: VLM + LLM       (30-90s)— nhìn + suy luận (last resort)

Usage:
    engine = SmartActionEngine(ipc_client, app_scanner)
    result = await engine.execute("mở chrome profile 00Fujin")
    # → Priority 1: app_launch with --profile-directory (< 2s)

    result = await engine.execute("mở tab mới trong chrome")
    # → Priority 2: Cmd+T (< 0.5s)

    result = await engine.execute("vào trang gemini.com")
    # → Priority 2+3: Cmd+L → type URL → Enter (< 2s)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── [C-03 FIX] AppleScript URL sanitizer ─────────────────────────────────────
import re as _re_safe

def _escape_applescript_url(url: str) -> str:
    """
    [C-03] Validate and sanitize URL before AppleScript string interpolation.
    Prevents injection of AppleScript commands via malicious URLs.
    """
    # Only allow safe URL chars — reject anything suspicious
    if not _re_safe.match(
        r"^https?://[a-zA-Z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$", url
    ):
        # Allow localhost and file-less schemes for internal use
        if not _re_safe.match(r'^https?://', url):
            raise ValueError(f"[C-03] Unsafe URL scheme rejected: {url[:80]}")
        # Strip/escape dangerous chars
        url = url.replace('"', "%22").replace("\\", "%5C").replace("'", "%27")
        url = url.replace(";", "%3B").replace("&", "%26").replace("|", "%7C")
    # Final hard check: no quotes or backslash remain
    if '"' in url or "\\" in url:
        raise ValueError(f"[C-03] URL contains unsafe characters after sanitization")
    return url


# ══════════════════════════════════════════════════════════════
# Vietnamese log helper
# ══════════════════════════════════════════════════════════════

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


import unicodedata as _ud


def _strip_accents(text: str) -> str:
    """'tạo thư mục' → 'tao thu muc' (accent-insensitive matching)."""
    t = _ud.normalize("NFD", text).replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in t if _ud.category(c) != "Mn")


# v4.3: words that mean the request is a *task*, not a bare app/shortcut.
_TASK_VERBS = re.compile(
    r"\b(?:tim|search|gui|send|dang|post|viet|write|tao|create|check|kiem tra|doc|read|"
    r"tong hop|bao cao|report|phan tich|analy[sz]e|so sanh|compare|lay|get|tai|download|"
    r"luu|save|xoa|delete|nhan tin|message|dat lich|schedule|chup|screenshot)\b"
)


# ══════════════════════════════════════════════════════════════
# Knowledge Base — shortcuts, patterns, workflows
# ══════════════════════════════════════════════════════════════

# macOS keyboard shortcuts (universal)
SHORTCUTS = {
    # ── Browser ──────────────────────────────────────
    "tab mới":       {"keys": ["command", "t"], "context": "browser"},
    "new tab":       {"keys": ["command", "t"], "context": "browser"},
    "đóng tab":      {"keys": ["command", "w"], "context": "browser"},
    "close tab":     {"keys": ["command", "w"], "context": "browser"},
    "thanh địa chỉ": {"keys": ["command", "l"], "context": "browser"},
    "address bar":   {"keys": ["command", "l"], "context": "browser"},
    "url bar":       {"keys": ["command", "l"], "context": "browser"},
    # FIX v1.0: "tìm kiếm" removed — Cmd+L chỉ focus address bar, không search
    "làm mới":       {"keys": ["command", "r"], "context": "browser"},
    "refresh":       {"keys": ["command", "r"], "context": "browser"},
    "quay lại":      {"keys": ["command", "["], "context": "browser"},
    "back":          {"keys": ["command", "["], "context": "browser"},
    "tiến tới":      {"keys": ["command", "]"], "context": "browser"},
    "forward":       {"keys": ["command", "]"], "context": "browser"},
    "tab trước":     {"keys": ["command", "shift", "["], "context": "browser"},
    "tab sau":       {"keys": ["command", "shift", "]"], "context": "browser"},
    "zoom in":       {"keys": ["command", "="], "context": "any"},
    "zoom out":      {"keys": ["command", "-"], "context": "any"},
    "toàn màn hình": {"keys": ["command", "control", "f"], "context": "any"},
    "fullscreen":    {"keys": ["command", "control", "f"], "context": "any"},
    "dev tools":     {"keys": ["command", "option", "i"], "context": "browser"},

    # ── System ───────────────────────────────────────
    "spotlight":     {"keys": ["command", "space"], "context": "system"},
    "copy":          {"keys": ["command", "c"], "context": "any"},
    "paste":         {"keys": ["command", "v"], "context": "any"},
    "cut":           {"keys": ["command", "x"], "context": "any"},
    "undo":          {"keys": ["command", "z"], "context": "any"},
    "redo":          {"keys": ["command", "shift", "z"], "context": "any"},
    "select all":    {"keys": ["command", "a"], "context": "any"},
    "chọn tất cả":  {"keys": ["command", "a"], "context": "any"},
    "lưu":           {"keys": ["command", "s"], "context": "any"},
    "save":          {"keys": ["command", "s"], "context": "any"},
    "in":            {"keys": ["command", "p"], "context": "any"},
    "print":         {"keys": ["command", "p"], "context": "any"},
    "chụp màn hình": {"keys": ["command", "shift", "3"], "context": "system"},
    "screenshot":    {"keys": ["command", "shift", "3"], "context": "system"},
    "chụp vùng":     {"keys": ["command", "shift", "4"], "context": "system"},
    "đóng cửa sổ":  {"keys": ["command", "w"], "context": "any"},
    "close window":  {"keys": ["command", "w"], "context": "any"},
    "thoát app":     {"keys": ["command", "q"], "context": "any"},
    "quit":          {"keys": ["command", "q"], "context": "any"},
    "ẩn app":        {"keys": ["command", "h"], "context": "any"},
    "hide":          {"keys": ["command", "h"], "context": "any"},
    "chuyển app":    {"keys": ["command", "tab"], "context": "system"},
    "switch app":    {"keys": ["command", "tab"], "context": "system"},
    "mission control": {"keys": ["control", "up"], "context": "system"},
    "show desktop":  {"keys": ["command", "f3"], "context": "system"},

    # ── Text editing ─────────────────────────────────
    "đầu dòng":     {"keys": ["command", "left"], "context": "any"},
    "cuối dòng":    {"keys": ["command", "right"], "context": "any"},
    "đầu trang":    {"keys": ["command", "up"], "context": "any"},
    "cuối trang":   {"keys": ["command", "down"], "context": "any"},
    "xoá dòng":     {"keys": ["command", "shift", "k"], "context": "editor"},
}

# URL patterns — direct navigate without search
URL_PATTERNS = {
    "google": "https://www.google.com",
    "gemini": "https://gemini.google.com",
    "facebook": "https://www.facebook.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "drive": "https://drive.google.com",
    "github": "https://github.com",
    "twitter": "https://twitter.com",
    "x.com": "https://x.com",
    "claude": "https://claude.ai",
    "chatgpt": "https://chatgpt.com",
    "tiktok": "https://www.tiktok.com",
    "instagram": "https://www.instagram.com",
    "linkedin": "https://www.linkedin.com",
    "reddit": "https://www.reddit.com",
    "shopee": "https://shopee.vn",
    "lazada": "https://www.lazada.vn",
    "tiki": "https://tiki.vn",
    "zalo": "https://chat.zalo.me",
    "binance": "https://www.binance.com",
    "tradingview": "https://www.tradingview.com",
    "coingecko": "https://www.coingecko.com",
    # [FIX v4.1] Thêm các AI platform còn thiếu
    "grok": "https://grok.com",
    "deepseek": "https://chat.deepseek.com",
    "perplexity": "https://www.perplexity.ai",
    "mistral": "https://chat.mistral.ai",
    "copilot": "https://copilot.microsoft.com",
}

# AppleScript templates
APPLESCRIPTS = {
    "chrome_url": '''
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then
        make new window
    end if
    set URL of active tab of front window to "{url}"
end tell
''',
    "chrome_new_tab_url": '''
tell application "Google Chrome"
    activate
    tell front window
        make new tab with properties {{URL:"{url}"}}
    end tell
end tell
''',
    "safari_url": '''
tell application "Safari"
    activate
    if (count of windows) = 0 then
        make new document
    end if
    set URL of current tab of front window to "{url}"
end tell
''',
    "get_frontmost_app": '''
tell application "System Events"
    set frontApp to name of first process whose frontmost is true
end tell
return frontApp
''',
    "chrome_current_url": '''
tell application "Google Chrome"
    return URL of active tab of front window
end tell
''',
    "scroll_down": '''
tell application "System Events"
    key code 125 using {{}}
end tell
''',
}


@dataclass
class ActionResult:
    """Result of a smart action execution."""
    success: bool
    priority: int           # 1-5 which priority handled it
    method: str             # "fast_action", "shortcut", "applescript", "llm", "vlm"
    result: str = ""
    error: str = ""
    duration_ms: int = 0


@dataclass
class LearnedWorkflow:
    """A workflow the agent has learned from successful task execution."""
    trigger: str            # regex pattern to match
    steps: list[dict]       # [{"action": "keyboard_hotkey", "payload": {...}}, ...]
    success_count: int = 0
    last_used: float = 0.0


class SmartActionEngine:
    """
    5-priority action engine for instant macOS automation.

    Processes goals through 5 priorities in order — stops at first match.
    Only falls through to VLM when all faster methods fail.
    """

    def __init__(
        self,
        ipc_client: Any,
        app_scanner: Any = None,
        knowledge_dir: str = "data/knowledge",
    ) -> None:
        self._ipc = ipc_client
        self._scanner = app_scanner
        self._knowledge_dir = Path(knowledge_dir)
        self._knowledge_dir.mkdir(parents=True, exist_ok=True)

        # Load learned workflows
        self._workflows: list[LearnedWorkflow] = []
        self._load_workflows()

        # v9.20 Phase 4: lazy EventBus reference (injected to avoid circular import)
        self._bus: Any = None
        # LLM client — injected for natural language profile parsing
        self._llm: Any = None

    # ══════════════════════════════════════════════════════════
    # Main entry point
    # ══════════════════════════════════════════════════════════

    def inject_event_bus(self, bus: Any) -> None:
        """Inject EventBus after construction (avoids circular imports)."""
        self._bus = bus

    def inject_llm(self, llm: Any) -> None:
        """Inject LLMClient for natural language parsing (profile commands etc)."""
        self._llm = llm

    async def execute(self, goal: str) -> ActionResult | None:
        """
        Try to execute goal via priorities 1-3.
        Returns ActionResult if handled, None if needs LLM/VLM.
        """
        t0 = time.monotonic()

        # v4.3 Priority 0: "chụp màn hình và gửi cho tôi" (the /start example)
        r = await self._try_screenshot_send(goal)
        if r:
            r.duration_ms = int((time.monotonic() - t0) * 1000)
            return r

        # [FIX v4.1] Priority 0a: Multi-Tab Chrome (mở N tab URL — không cần profile)
        # Phải check TRƯỚC _try_multi_chrome và _try_fast_action để tránh
        # _try_fast_action bắt nhầm app_name = "4 tab chrome bao gồm ..."
        r = await self._try_multi_tab_chrome(goal)
        if r:
            r.duration_ms = int((time.monotonic() - t0) * 1000)
            return r

        # Priority 0: Multi-Chrome profiles (special compound action)
        r = await self._try_multi_chrome(goal)
        if r:
            r.duration_ms = int((time.monotonic() - t0) * 1000)
            return r

        # Priority 1: Fast-Action (open apps, profiles)
        r = await self._try_fast_action(goal)
        if r:
            r.duration_ms = int((time.monotonic() - t0) * 1000)
            return r

        # Priority 2: Keyboard shortcuts
        r = await self._try_shortcut(goal)
        if r:
            r.duration_ms = int((time.monotonic() - t0) * 1000)
            return r

        # Priority 3: AppleScript workflows (URL navigation, etc.)
        r = await self._try_applescript(goal)
        if r:
            r.duration_ms = int((time.monotonic() - t0) * 1000)
            return r

        # Priority 3b: Learned workflows
        r = await self._try_learned_workflow(goal)
        if r:
            r.duration_ms = int((time.monotonic() - t0) * 1000)
            return r

        # Not handled → caller should use LLM (priority 4) or VLM (priority 5)
        return None

    # ══════════════════════════════════════════════════════════
    # Priority 0: Multi-Chrome Profiles + Window Tiling
    # ══════════════════════════════════════════════════════════

    async def _parse_profile_numbers_llm(self, goal: str) -> list | None:
        """Dùng LLM để extract số profile từ bất kỳ cách nói nào."""
        if not self._llm:
            return None
        try:
            prompt = (
                "Từ lệnh sau, trích xuất DANH SÁCH SỐ thứ tự profile Chrome muốn mở.\n"
                "Chỉ trả về JSON array số nguyên. Ví dụ: [1, 10, 15, 25, 30]\n"
                "Nếu không có danh sách số cụ thể thì trả về: null\n\n"
                f"Lệnh: {goal}\n\nJSON:"
            )
            resp = await self._llm.complete(prompt, max_tokens=60, temperature=0)
            import json as _json
            m = re.search(r'\[[\d,\s]+\]', resp.strip())
            if m:
                nums = _json.loads(m.group(0))
                if isinstance(nums, list) and all(isinstance(x, int) for x in nums):
                    return [n for n in nums if 0 <= n <= 999]
        except Exception as e:
            _vlog("⚠️", f"LLM profile parse lỗi: {str(e)[:50]}")
        return None

    async def _try_multi_chrome(self, goal: str) -> ActionResult | None:
        """
        Mở nhiều Chrome profile + tile windows.
        Fast path: regex (< 50ms). Smart fallback: LLM (< 2s).
        """
        gl = goal.strip().lower()
        if not any(k in gl for k in ("profile", "hồ sơ", "chrome", "trình duyệt")):
            return None
        if not self._scanner:
            return None
        all_profiles = self._scanner._chrome_profiles
        if not all_profiles:
            return None

        profiles_to_open: list = []

        # ── Fast: số liệt kê "01 10 15 25 30" (có hoặc không có dấu phẩy) ──
        if not profiles_to_open:
            m_nums = re.search(
                r'(?:profile|hồ sơ|chrome)[^0-9]*'
                r'((?:\d{1,3}[\s,;]+){1,}\d{1,3})',
                gl
            )
            if m_nums:
                nums = [int(n) for n in re.findall(r'\d{1,3}', m_nums.group(1))
                        if 0 <= int(n) <= 999]
                if len(nums) >= 2:
                    resolved = []
                    for n in nums:
                        p = (self._scanner.get_profile_by_number(n)
                             if hasattr(self._scanner, 'get_profile_by_number')
                             else self._scanner.find_chrome_profile(str(n)))
                        if p:
                            resolved.append(p)
                        else:
                            _vlog("⚠️", f"Profile số {n} không tìm thấy — bỏ qua")
                    if resolved:
                        profiles_to_open = resolved

        # ── Cuối danh sách "5 profile cuối" ────────────────────────
        if not profiles_to_open:
            m_last = re.search(
                r'(\d+)\s+(?:profile|hồ sơ)\s+(?:cuối|cuối cùng|last)'
                r'|(?:profile|hồ sơ)\s+(?:cuối|cuối cùng|last)\s+(\d+)', gl
            )
            if m_last:
                n = int(next(g for g in m_last.groups() if g))
                n = min(n, len(all_profiles), 10)
                profiles_to_open = all_profiles[-n:]
                _vlog("📋", f"{n} profile cuối: {', '.join(p.name for p in profiles_to_open)}")

        # ── Số lượng "mở 5 profile" ─────────────────────────────────
        if not profiles_to_open:
            m_count = re.search(
                r'(?:mở|open|bật|khởi động)\s+(\d+)\s+(?:profile|hồ sơ)'
                r'(?:\s+(?:chrome|bất kỳ|ngẫu nhiên|đầu tiên)?)?'
                r'(?:\s*$|\s+(?:và|xếp|tile|cạnh|vừa))', gl
            )
            if m_count and not re.search(r'(?:đến|tới|to|-)\s*\d+', gl):
                n = min(int(m_count.group(1)), len(all_profiles), 10)
                profiles_to_open = all_profiles[:n]

        # ── Khoảng số "profile 01 đến 05" ──────────────────────────
        if not profiles_to_open:
            m_range = re.search(
                r'(?:profile|hồ sơ)\s+(\d+)\s*(?:đến|tới|to|-)\s*(\d+)', gl
            )
            if m_range:
                start, end = int(m_range.group(1)), int(m_range.group(2))
                if hasattr(self._scanner, 'find_profiles_by_number_range'):
                    profiles_to_open = self._scanner.find_profiles_by_number_range(start, end)
                if not profiles_to_open:
                    s = max(0, min(start, len(all_profiles) - 1))
                    e = max(s, min(end, len(all_profiles) - 1))
                    profiles_to_open = all_profiles[s:e + 1]

        # ── Tiếp theo "5 profile tiếp theo sau 10" ──────────────────
        if not profiles_to_open:
            m_next = re.search(
                r'(\d+)\s+(?:profile|hồ sơ)\s+(?:tiếp theo|kế tiếp|next)'
                r'\s+(?:sau|after|từ)?\s+(.+?)(?:\s+(?:và|xếp|tile)|$)', gl
            )
            if m_next:
                n = int(m_next.group(1))
                pivot_p = self._scanner.find_chrome_profile(m_next.group(2).strip())
                if pivot_p:
                    try:
                        idx = next(i for i, p in enumerate(all_profiles)
                                   if p.directory == pivot_p.directory)
                        profiles_to_open = all_profiles[idx + 1:idx + 1 + n]
                    except StopIteration:
                        pass

        # ── Nhóm/tag "profile nhóm marketing" ──────────────────────
        if not profiles_to_open:
            m_group = re.search(
                r'(?:profile|hồ sơ)\s+(?:nhóm|group|tag|loại)\s+'
                r'(.+?)(?:\s+(?:và|xếp|tile)|$)', gl
            )
            if m_group:
                kw = m_group.group(1).strip().lower()
                profiles_to_open = [p for p in all_profiles if kw in p.name.lower()]

        # ── Tên cụ thể "profile 00Fujin, 01Tam" ────────────────────
        if not profiles_to_open:
            m = re.search(r'(?:profile|hồ sơ)\s*[:.]?\s*(.+)', goal.strip(), re.I)
            if m:
                raw = re.sub(
                    r'\s*(?:và|and|rồi)?\s*(?:xếp|sắp|tile|arrange|cạnh nhau'
                    r'|side by side|chia màn|split|vừa màn|canh màn).*$',
                    '', m.group(1).strip(), flags=re.I
                ).strip()
                parts = [p.strip() for p in re.split(r'\s*[,;]\s*|\s+và\s+|\s+and\s+', raw) if p.strip()]
                # v9.22 GUARD: nếu bất kỳ part nào chứa từ khoá workflow
                # (tạo ảnh, chatgpt, content, đăng) → đây là workflow prompt,
                # KHÔNG phải danh sách profile → bỏ qua, return None
                _workflow_keywords = ("tạo ảnh", "chatgpt", "content", "đăng", "post", "gemini", "facebook")
                _is_workflow = any(
                    any(kw in p.lower() for kw in _workflow_keywords)
                    for p in parts
                )
                if _is_workflow:
                    return None
                if len(parts) >= 2:
                    resolved = []
                    for name in parts:
                        p = self._scanner.find_chrome_profile(name)
                        if p:
                            resolved.append(p)
                        else:
                            _vlog("⚠️", f"Profile '{name}' không tìm thấy — bỏ qua")
                    profiles_to_open = resolved

        # ── LLM fallback: regex không match → nhờ AI phân tích ─────
        if not profiles_to_open and self._llm:
            _vlog("🤖", "Regex không parse được → nhờ LLM phân tích lệnh...")
            llm_nums = await self._parse_profile_numbers_llm(goal)
            if llm_nums:
                _vlog("🤖", f"LLM trích xuất: profile số {llm_nums}")
                resolved = []
                for n in llm_nums:
                    p = (self._scanner.get_profile_by_number(n)
                         if hasattr(self._scanner, 'get_profile_by_number')
                         else self._scanner.find_chrome_profile(str(n)))
                    if p:
                        resolved.append(p)
                    else:
                        _vlog("⚠️", f"Profile số {n} không tìm thấy — bỏ qua")
                profiles_to_open = resolved

        if len(profiles_to_open) < 2:
            return None

        profiles_to_open = profiles_to_open[:10]
        n = len(profiles_to_open)
        _vlog("🪟", f"Mở {n} Chrome profiles và xếp cạnh nhau...")
        for p in profiles_to_open:
            _vlog("  ▸", f"{p.name} ({p.directory})")

        try:
            _vlog("🔄", "Đang tắt Chrome...")
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", 'tell application "Google Chrome" to quit',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            await asyncio.sleep(2)

            import subprocess
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            for i, profile in enumerate(profiles_to_open):
                _vlog("🚀", f"Mở profile {i+1}/{n}: {profile.name}")
                # FIX v4.3: Chrome was relaunched without the DevTools port the
                # launcher enabled → CDP lost for the rest of the session.
                _args = [chrome_path, f"--profile-directory={profile.directory}"]
                if i == 0:
                    _args.append("--remote-debugging-port=9222")
                subprocess.Popen(
                    _args,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                await asyncio.sleep(1.5)
            await asyncio.sleep(2)

            _vlog("🪟", f"Đang xếp {n} cửa sổ vừa màn hình...")
            tile_result = await self._tile_chrome_windows(n)
            if tile_result:
                _vlog("✅", f"Đã mở {n} profile Chrome và xếp cạnh nhau!")
            else:
                _vlog("⚠️", "Đã mở Chrome nhưng không thể xếp cửa sổ tự động")

            return ActionResult(
                success=True, priority=0, method="multi_chrome",
                result=f"Opened {n} Chrome profiles: {', '.join(p.name for p in profiles_to_open)}",
            )
        except Exception as exc:
            return ActionResult(success=False, priority=0, method="multi_chrome",
                                error=str(exc))

    async def _tile_chrome_windows(self, count: int) -> bool:
        """
        Tile Chrome windows using smart grid layout.

        Layout tự động theo số lượng:
          1 window  → fullscreen
          2 windows → 2 cột ngang (50% | 50%)
          3 windows → 3 cột ngang (33% | 33% | 33%)
          4 windows → lưới 2×2
          5 windows → hàng trên 3, hàng dưới 2 (canh giữa)
          6 windows → lưới 2×3
          7-8       → lưới 2×4
          9         → lưới 3×3
          10        → lưới 2×5
        """
        # Build grid layout based on count
        def compute_grid(n: int) -> tuple[int, int]:
            """Return (cols, rows) for n windows."""
            if n <= 1:  return (1, 1)
            if n == 2:  return (2, 1)
            if n == 3:  return (3, 1)
            if n == 4:  return (2, 2)
            if n == 5:  return (3, 2)   # 3 top + 2 bottom
            if n == 6:  return (3, 2)
            if n <= 8:  return (4, 2)
            if n == 9:  return (3, 3)
            return (5, 2)               # 10

        cols, rows = compute_grid(count)

        script = f'''
tell application "System Events"
    try
        set screenWidth to (do shell script "system_profiler SPDisplaysDataType | grep Resolution | head -1 | awk '{{print $2}}'") as integer
        set screenHeight to (do shell script "system_profiler SPDisplaysDataType | grep Resolution | head -1 | awk '{{print $4}}'") as integer
    on error
        set screenWidth to 3440
        set screenHeight to 1440
    end try
end tell

set menuBar to 25
set dockH to 70
set usableW to screenWidth
set usableH to screenHeight - menuBar - dockH
set colCount to {cols}
set rowCount to {rows}
set winCount to {count}
set wW to (usableW / colCount) as integer
set wH to (usableH / rowCount) as integer

tell application "Google Chrome"
    activate
    set windowList to every window
    set i to 0
    repeat with w in windowList
        if i < winCount then
            set col to i mod colCount
            set row to (i div colCount) as integer
            -- For last row with fewer windows: center them
            set rowStart to row * colCount
            set windowsInThisRow to winCount - rowStart
            if windowsInThisRow < colCount then
                -- Center the last row
                set offset to ((colCount - windowsInThisRow) * wW / 2) as integer
                set xPos to offset + (col - (colCount - windowsInThisRow)) * wW
                if xPos < 0 then set xPos to 0
            else
                set xPos to col * wW
            end if
            set yPos to menuBar + row * wH
            set bounds of w to {{xPos, yPos, xPos + wW, yPos + wH}}
            set i to i + 1
        end if
    end repeat
end tell
'''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                _vlog("⚠️", f"AppleScript tile lỗi: {stderr.decode()[:80]} — thử fallback")
                return await self._tile_chrome_simple(count)
            return True
        except Exception:
            return await self._tile_chrome_simple(count)

    async def _tile_chrome_simple(self, count: int) -> bool:
        """Fallback tiling — hardcoded 3440×1440, single row."""
        screen_w, screen_h = 3440, 1440
        menu_bar = 25
        dock = 70
        usable_h = screen_h - menu_bar - dock

        # Same grid logic as above but hardcoded screen
        def compute_grid(n: int) -> tuple[int, int]:
            if n <= 3: return (n, 1)
            if n == 4: return (2, 2)
            if n <= 6: return (3, 2)
            if n <= 8: return (4, 2)
            if n == 9: return (3, 3)
            return (5, 2)

        cols, rows = compute_grid(count)
        w_each = screen_w // cols
        h_each = usable_h // rows

        lines = ['tell application "Google Chrome"', '    activate',
                 '    set windowList to every window']
        for i in range(count):
            col = i % cols
            row = i // cols
            x1 = col * w_each
            y1 = menu_bar + row * h_each
            x2 = x1 + w_each
            y2 = y1 + h_each
            lines.append(f'    if (count of windowList) > {i} then')
            lines.append(f'        set bounds of item {i+1} of windowList to {{{x1}, {y1}, {x2}, {y2}}}')
            lines.append(f'    end if')
        lines.append('end tell')
        script = '\n'.join(lines)

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode == 0
        except Exception:
            return False


    # ══════════════════════════════════════════════════════════════════
    # [FIX v4.1] Priority 0b: Multi-Tab Chrome (mở N tab với nhiều URL)
    # ══════════════════════════════════════════════════════════════════

    async def _try_multi_tab_chrome(self, goal: str) -> "ActionResult | None":
        """
        Xử lý lệnh mở nhiều tab Chrome với danh sách URL/tên site.

        Trigger patterns:
          "mở 4 tab chrome bao gồm gemini, grok, chatgpt, deepseek"
          "mở tab gemini grok chatgpt deepseek trên chrome"
          "mở chrome với gemini và chatgpt"

        [FIX v4.1] Trước đây lệnh này rơi vào _try_fast_action() với
        app_name = "4 tab chrome bao gồm gemini, grok, chatgpt, deepseek"
        → app_launch reject → false ✅. Nay được handle đúng ở đây.
        """
        gl = goal.strip().lower()

        # Chỉ xử lý nếu có dấu hiệu multi-site + chrome/tab
        _has_chrome = any(k in gl for k in ("chrome", "trình duyệt", "tab"))
        _has_open = any(gl.startswith(k) for k in ("mở", "open", "bật", "launch", "khởi động"))
        # [FIX v4.2] Thêm pattern "tham khảo N AI", "hỏi N AI", "so sánh N AI"
        # Người dùng nói "tham khảo ý kiến 4 AI" = mở 4 tab AI tools
        _has_multi_ai = bool(re.search(
            r'(?:tham\s*kh[aả]o|h[oỏ]i|so\s*s[aá]nh|dùng|sử\s*dụng)\s*(?:ý\s*ki[eế]n\s*)?'
            r'(\d+|nhiều|các|mấy)\s*(?:con\s*)?(?:AI|trợ\s*lý|chatbot|LLM)',
            gl, re.IGNORECASE
        ))
        if _has_multi_ai:
            # Override: xem như "mở N tab AI"
            _has_chrome = True
            _has_open = True
        if not (_has_open and _has_chrome):
            return None

        # Phải có ≥ 2 site names hoặc keyword số lượng tab hoặc multi AI
        _has_multi = bool(re.search(r'\d+\s*tab', gl)) or \
                     sum(1 for name in URL_PATTERNS if name in gl) >= 2 or \
                     _has_multi_ai
        if not _has_multi:
            return None

        # ── Extract danh sách site từ goal ────────────────────────
        # Loại bỏ các từ điều hướng để còn lại tên site
        _noise = re.compile(
            r'\b(?:mở|open|bật|launch|khởi động|chrome|trình duyệt|tab|'
            r'bao\s*gồm|gồm|gồm có|bao gồm|và|with|including|include|'
            r'\d+|các|những|some|several|a\s*few)\b',
            re.IGNORECASE | re.UNICODE,
        )
        cleaned = _noise.sub(' ', gl)
        # Tách bởi dấu phẩy, dấu chấm phẩy, khoảng trắng
        tokens = [t.strip().rstrip('.,;') for t in re.split(r'[,;\s]+', cleaned) if t.strip()]

        # Map token → URL
        urls_to_open: list[tuple[str, str]] = []  # (display_name, url)
        seen_urls: set[str] = set()
        for token in tokens:
            if not token or len(token) < 2:
                continue
            # Tìm trong URL_PATTERNS
            for name, url in URL_PATTERNS.items():
                if token in name or name in token:
                    if url not in seen_urls:
                        urls_to_open.append((name, url))
                        seen_urls.add(url)
                    break
            else:
                # Token có dạng domain (chứa dấu chấm) → dùng trực tiếp
                if '.' in token:
                    url = token if token.startswith('http') else f"https://{token}"
                    if url not in seen_urls:
                        urls_to_open.append((token, url))
                        seen_urls.add(url)

        # [FIX v4.2] Nếu user nói "4 AI" / "tham khảo AI" mà không nêu tên cụ thể
        # → dùng danh sách AI tools mặc định
        if _has_multi_ai and len(urls_to_open) < 2:
            _default_ai = [
                ("gemini", "https://gemini.google.com"),
                ("chatgpt", "https://chatgpt.com"),
                ("grok", "https://grok.com"),
                ("deepseek", "https://chat.deepseek.com"),
            ]
            # Lấy số lượng từ goal (mặc định 4)
            _n_match = re.search(r'(\d+)\s*(?:con\s*)?(?:AI|trợ\s*lý|chatbot)', gl)
            _n_ai = int(_n_match.group(1)) if _n_match else 4
            _n_ai = min(_n_ai, len(_default_ai))
            for name, url in _default_ai[:_n_ai]:
                if url not in seen_urls:
                    urls_to_open.append((name, url))
                    seen_urls.add(url)
            _vlog("🤖", f"Default AI tools: {', '.join(n for n, _ in urls_to_open)}")

        if len(urls_to_open) < 2:
            return None  # Không đủ URLs → nhường cho handler khác

        _vlog("🌐", f"Multi-tab Chrome: mở {len(urls_to_open)} tab — "
              f"{', '.join(n for n, _ in urls_to_open)}")

        # ── Mở Chrome trước ──────────────────────────────────────
        try:
            await self._ipc.send_action("app_launch", {"app_name": "google chrome"})
            await asyncio.sleep(1.5)
        except Exception:
            pass  # Chrome có thể đã mở → tiếp tục

        # ── Mở từng URL trong tab mới ────────────────────────────
        success_count = 0
        errors: list[str] = []
        for i, (name, url) in enumerate(urls_to_open):
            try:
                if i == 0:
                    # Tab đầu: navigate tab hiện tại
                    script = APPLESCRIPTS["chrome_url"].format(url=url)
                else:
                    # Tab tiếp theo: mở tab mới
                    script = APPLESCRIPTS["chrome_new_tab_url"].format(url=url)
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=10)
                if proc.returncode == 0:
                    success_count += 1
                    _vlog("✅", f"Tab {i+1}: {name} ({url})")
                else:
                    err = stderr_data.decode().strip()[:80] if stderr_data else "unknown"
                    errors.append(f"{name}: {err}")
                    _vlog("⚠️", f"Tab {i+1} lỗi: {name} — {err}")
                await asyncio.sleep(0.3)  # Throttle nhẹ
            except asyncio.TimeoutError:
                errors.append(f"{name}: timeout")
                _vlog("⚠️", f"Tab {i+1} timeout: {name}")
            except Exception as exc:
                errors.append(f"{name}: {str(exc)[:60]}")

        overall_success = success_count > 0
        result_msg = (
            f"Đã mở {success_count}/{len(urls_to_open)} tab: "
            f"{', '.join(n for n, _ in urls_to_open[:success_count])}"
        )
        if errors:
            result_msg += f" | Lỗi: {'; '.join(errors)}"

        return ActionResult(
            success=overall_success,
            priority=1,
            method="multi_tab_chrome",
            result=result_msg,
            error="; ".join(errors) if not overall_success else "",
        )

    # ══════════════════════════════════════════════════════════
    # Priority 1: Fast-Action (< 1s)
    # ══════════════════════════════════════════════════════════

    async def _try_fast_action(self, goal: str) -> ActionResult | None:
        """Open apps, Chrome profiles — instant via AppScanner."""
        g = goal.strip()
        gl = g.lower()

        open_keywords = ("mở", "open", "chạy", "launch", "khởi động", "bật")
        if not any(gl.startswith(kw) for kw in open_keywords):
            return None

        # Extract profile
        profile = ""
        pm = re.search(r'(?:profile|hồ sơ|pro)\s*[:.]?\s*(.+?)$', g, re.I)
        if pm:
            profile = pm.group(1).strip()

        # Extract app name
        app_text = re.sub(r'^(?:mở|open|chạy|launch|khởi động|bật)\s+', '', gl, flags=re.I)
        app_text = re.sub(r'(?:ứng dụng|app|phần mềm|application)\s*', '', app_text, flags=re.I)
        app_text = re.sub(r'\s*(?:profile|hồ sơ|pro)\s*[:.]?\s*.*$', '', app_text, flags=re.I)
        app_text = re.sub(r'\s*(?:vào|và|rồi|với|sau đó|then|and|trang|page|web)\s*', ' ', app_text, flags=re.I)
        app_name = re.sub(r"\s+", " ", app_text).strip()

        if not app_name:
            return None
        # "mở tab mới", "mở spotlight" … are keyboard shortcuts, not apps
        if app_name in SHORTCUTS:
            return None

        # FIX v4.3: "mở chrome và tìm giá vàng" used to open Chrome and report
        # the WHOLE task as done.  Only bare "open <app>" requests are handled.
        _app_ascii = _strip_accents(app_name.lower())
        if len(app_name.split()) > 3 or _TASK_VERBS.search(_app_ascii):
            if not re.search(r'(?:vào|trang|page|web|url|site|website)\s+\S+\s*$', gl):
                return None

        # Check if there's a URL embedded ("mở chrome vào trang gemini.com")
        # FIX v4.3: the old regex captured the filler word ("trang") instead of
        # the site, so "mở chrome vào trang gemini.com" never navigated.
        url = ""
        url_text = ""
        m_dom = re.search(r'(https?://\S+|(?<![\w@])[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)', gl)
        if m_dom:
            url_text = m_dom.group(1).rstrip('.,')
        else:
            m_site = re.search(r'(?:vào|tới|đến|trang|page|web|url|site|website)\s+(?:trang\s+|web\s+)?(\S+)', gl)
            if m_site:
                url_text = m_site.group(1).rstrip('.,')
        if url_text:
            for name, full_url in URL_PATTERNS.items():
                if url_text == name or name in url_text:
                    url = full_url
                    break
            if not url and ('.' in url_text or url_text.startswith('http')):
                url = url_text if url_text.startswith('http') else f"https://{url_text}"
            if url:
                app_name = re.sub(r"\s+", " ", app_name.replace(url_text, " ")).strip() or app_name

        is_chrome = any(k in app_name for k in ("chrome", "google chrome", "trình duyệt"))

        _vlog("⚡", f"Hành động nhanh: mở \033[1m{app_name}\033[0m" +
              (f" profile \033[1m{profile}\033[0m" if profile else "") +
              (f" → \033[1m{url}\033[0m" if url else ""))

        try:
            payload: dict[str, Any] = {"app_name": app_name}
            if is_chrome and profile:
                payload["chrome_profile"] = profile

            resp = await self._ipc.send_action("app_launch", payload)
            if not resp.success:
                return ActionResult(success=False, priority=1, method="fast_action",
                                    error=str(getattr(resp, "message", "") or getattr(resp, "result", "") or "app_launch failed"))

            # If URL specified → navigate after app opens
            if url and is_chrome:
                await asyncio.sleep(2)  # Wait for Chrome to open
                result = await self._applescript_navigate(url, "chrome")
                if result:
                    _vlog("✅", f"Chrome mở → chuyển đến {url}")
                    return ActionResult(success=True, priority=1, method="fast_action",
                                        result=f"Opened Chrome → navigated to {url}")

            _vlog("✅", f"Đã mở {app_name}" +
                  (f" profile {profile}" if profile else ""))
            return ActionResult(success=True, priority=1, method="fast_action",
                                result=f"Opened {app_name}")

        except Exception as exc:
            return ActionResult(success=False, priority=1, method="fast_action",
                                error=str(exc))

    # ══════════════════════════════════════════════════════════
    # Priority 2: Keyboard Shortcuts (< 0.5s)
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_command(gl: str) -> str:
        g = re.sub(r"[.!?…]+$", "", gl.strip()).strip()
        g = re.sub(r"^(?:hãy|làm ơn|vui lòng|please|bạn)\s+", "", g)
        g = re.sub(r"\s+(?:giúp tôi|giúp mình|giùm|cho tôi|cho mình|đi|nhé|nha|ngay|now|please)$", "", g)
        return g.strip()

    async def _try_shortcut(self, goal: str) -> ActionResult | None:
        """
        Keyboard shortcuts / typing / key presses / scrolling.

        FIX v4.3: triggers used to match ANYWHERE in the goal, so
        "log in to facebook" → Cmd+P ("in"), "quit smoking tips" → Cmd+Q,
        "viết bài LinkedIn …" typed the text into the frontmost app and
        "tạo báo cáo và lưu vào Desktop" pressed Cmd+S — all reported as
        success.  A shortcut now fires only when the request IS the shortcut
        (optionally prefixed by nhấn/bấm/press/mở).
        """
        # v4.3 keyboard-first catalog (data/shortcuts/macos.yaml): app-aware,
        # verified through ui_snapshot, menu fallback, confirmation for risky
        # commands.  The legacy table below stays as the fallback.
        try:
            from core.keyboard_first import get_keyboard_first
            kf = await get_keyboard_first(self._ipc).try_command(goal)
        except Exception as exc:
            _vlog("⚠️", f"keyboard-first lỗi: {str(exc)[:80]}")
            kf = None
        if kf is not None:
            method = f"kb_{kf.method}"
            if not kf.success and ("xác nhận" in kf.error or "rủi ro" in kf.error):
                method = "kb_declined"      # never retry a refused/blocked command elsewhere
            return ActionResult(success=kf.success, priority=2, method=method,
                                result=kf.output or f"{kf.method}: {kf.name}", error=kf.error)

        gl = goal.strip().lower()
        g = self._normalize_command(gl)

        for trigger, sc in SHORTCUTS.items():
            pat = (r"^(?:(?:nhấn|bấm|ấn|press|hit|dùng|mở|open|làm)\s+)?"
                   r"(?:phím\s+tắt\s+|phím\s+)?" + re.escape(trigger) + r"$")
            if re.match(pat, g, re.I):
                _vlog("⌨️ ", f"Phím tắt: {trigger} → {'+'.join(sc['keys'])}")
                try:
                    resp = await self._ipc.send_action("keyboard_hotkey", {"keys": sc["keys"]})
                    if not getattr(resp, "success", True):
                        return ActionResult(success=False, priority=2, method="shortcut",
                                            error=getattr(resp, "message", "") or "hotkey failed")
                    return ActionResult(success=True, priority=2, method="shortcut",
                                        result=f"Shortcut: {'+'.join(sc['keys'])}")
                except Exception as exc:
                    return ActionResult(success=False, priority=2, method="shortcut",
                                        error=str(exc))

        # Typing: "gõ <text>" | type "<text>" | nhập chữ/văn bản "<text>"
        orig = self._normalize_command(goal.strip())
        type_match = (
            re.match(r'^(?:gõ|gõ chữ|gõ văn bản)\s+["“\']?(.+?)["”\']?$', orig, re.I)
            or re.match(r'^(?:type|nhập|nhập chữ|nhập văn bản)\s+["“\'](.+?)["”\']$', orig, re.I)
            or re.match(r'^(?:nhập chữ|nhập văn bản|type text)\s+(.+)$', orig, re.I)
        )
        if type_match:
            text = type_match.group(1).strip()
            _vlog("⌨️ ", f"Gõ: {text[:50]}")
            try:
                resp = await self._ipc.send_action("keyboard_type", {"text": text[:8192]})
                if not getattr(resp, "success", True):
                    return ActionResult(success=False, priority=2, method="shortcut",
                                        error=getattr(resp, "message", "") or "keyboard_type failed")
                return ActionResult(success=True, priority=2, method="shortcut",
                                    result=f"Typed: {text[:50]}")
            except Exception as exc:
                return ActionResult(success=False, priority=2, method="shortcut",
                                    error=str(exc))

        # "nhấn enter/tab/escape/..."
        key_match = re.match(r'^(?:nhấn|press|bấm|ấn)\s+(?:phím\s+)?(enter|return|tab|escape|esc|space|backspace|delete)'
                             r'(?:\s+(\d+)\s*(?:lần|times)?)?$', g, re.I)
        if key_match:
            key = key_match.group(1).strip().lower()
            key = {"esc": "escape", "enter": "return"}.get(key, key)
            presses = max(1, min(10, int(key_match.group(2) or 1)))
            _vlog("⌨️ ", f"Nhấn phím: {key} ×{presses}")
            try:
                resp = await self._ipc.send_action("keyboard_press", {"key": key, "presses": presses})
                ok = getattr(resp, "success", True)
                return ActionResult(success=ok, priority=2, method="shortcut",
                                    result=f"Pressed: {key}", error="" if ok else getattr(resp, "message", ""))
            except Exception as exc:
                return ActionResult(success=False, priority=2, method="shortcut",
                                    error=str(exc))

        # "scroll xuống/lên [N]"  (dy > 0 = down, in lines)
        scroll_match = re.match(r'^(?:scroll|cuộn|kéo)\s+(xuống|down|lên|up)(?:\s+(\d+))?(?:\s*(?:lần|dòng|lines?))?$', g, re.I)
        if scroll_match:
            down = scroll_match.group(1) in ("xuống", "down")
            amount = max(1, min(100, int(scroll_match.group(2) or 5)))
            dy = amount if down else -amount
            _vlog("🖱️", f"Scroll {'xuống' if down else 'lên'} {amount}")
            try:
                resp = await self._ipc.send_action("mouse_scroll", {"dx": 0, "dy": dy})
                ok = getattr(resp, "success", True)
                return ActionResult(success=ok, priority=2, method="shortcut",
                                    result=f"Scrolled {dy}", error="" if ok else getattr(resp, "message", ""))
            except Exception as exc:
                return ActionResult(success=False, priority=2, method="shortcut",
                                    error=str(exc))

        return None

    async def _try_screenshot_send(self, goal: str) -> ActionResult | None:
        """'chụp màn hình (và) gửi (cho tôi)' → capture + deliver the image."""
        g = _strip_accents(goal.lower())
        if not re.search(r"\b(?:chup|screenshot|capture)\b", g):
            return None
        if not re.search(r"\b(?:gui|send|cho toi|cho minh|telegram)\b", g):
            return None
        try:
            from utils.platform_adapter import take_screenshot
            path = await take_screenshot()
        except Exception as exc:
            return ActionResult(success=False, priority=1, method="screenshot_send", error=str(exc))
        sent = False
        try:
            from core import notify_hub
            sent = await notify_hub.send_file(path, "📸 Ảnh chụp màn hình")
        except Exception:
            sent = False
        if not sent:
            return ActionResult(success=False, priority=1, method="screenshot_send",
                                result=path,
                                error=f"Đã chụp ({path}) nhưng chưa có kênh gửi file (Telegram chưa kết nối)")
        return ActionResult(success=True, priority=1, method="screenshot_send",
                            result=f"Đã chụp và gửi: {path}")

    # ══════════════════════════════════════════════════════════
    # Priority 3: AppleScript (1-3s)
    # ══════════════════════════════════════════════════════════

    async def _try_applescript(self, goal: str) -> ActionResult | None:
        """Use AppleScript for complex macOS interactions."""
        gl = goal.strip().lower()

        # FIX v1.0: File-op guard — tìm file/folder → FinderSkills, không search Google
        # v4.3: accent-insensitive ("thư mục" vs "thu muc")
        _FILE_OP2 = re.compile(
            r'(?:tim|find|search)\s+.{0,30}'
            r'(?:file|folder|thu\s+muc|pdf|png|jpg|jpeg|zip|docx?|xlsx?|\.\w+)',
            re.I
        )
        if _FILE_OP2.search(_strip_accents(gl)):
            return None  # nhường cho FinderSkills

        # ── Pattern: đóng N cửa sổ chrome ────────────────────────
        close_match = re.search(
            r'(?:đóng|close|tắt)\s+(?:(\d+)\s+)?(?:cửa sổ|window|tab|chrome|trình duyệt)',
            gl
        )
        if close_match and any(k in gl for k in ('chrome', 'cửa sổ', 'window', 'trình duyệt')):
            n = int(close_match.group(1)) if close_match.group(1) else None
            result = await self._close_chrome_windows(n)
            if result:
                msg = f"Đã đóng {'tất cả' if not n else n} cửa sổ Chrome"
                _vlog("✅", msg)
                return ActionResult(success=True, priority=3, method="applescript", result=msg)

        # ── Pattern: URL trên profile cụ thể ─────────────────────
        # "vào tinhte.vn trên profile 21" / "facebook.com trên 21"
        profile_url_match = re.search(
            r'(?:vào|navigate|truy cập|mở trang|go to)?\s*(\S+\.\w+)\s+(?:trên|on|trong|ở)\s+(?:profile|hồ sơ|pro)?\s*(\d+)',
            gl
        )
        if profile_url_match:
            url_text = profile_url_match.group(1).strip().rstrip('.,')
            profile_num = int(profile_url_match.group(2))
            url = ""
            for name, full_url in URL_PATTERNS.items():
                if name in url_text:
                    url = full_url
                    break
            if not url:
                url = url_text if url_text.startswith('http') else f"https://{url_text}"
            _vlog("🌐", f"Chuyển đến {url} trên profile {profile_num}")
            result = await self._navigate_url_in_profile(url, profile_num)
            if result:
                return ActionResult(success=True, priority=3, method="applescript",
                                    result=f"Navigated {url} in profile {profile_num}")

        # ── Pattern: URL trên tất cả / nhiều profile ─────────────
        # "vào facebook.com trên các profile" / "mở facebook trên tất cả"
        all_profiles_match = re.search(
            r'(?:vào|navigate|truy cập|mở trang)?\s*(\S+\.\w+|\S+)\s+(?:trên|on)\s+(?:các|tất cả|all|những|mọi|các profile|tất cả profile)',
            gl
        )
        if all_profiles_match:
            url_text = all_profiles_match.group(1).strip().rstrip('.,')
            url = ""
            for name, full_url in URL_PATTERNS.items():
                if name in url_text:
                    url = full_url
                    break
            if not url and '.' in url_text:
                url = url_text if url_text.startswith('http') else f"https://{url_text}"
            if url:
                _vlog("🌐", f"Mở {url} trên tất cả Chrome windows...")
                result = await self._navigate_all_chrome_windows(url)
                if result:
                    return ActionResult(success=True, priority=3, method="applescript",
                                        result=f"Navigated all Chrome windows to {url}")

        # ── Pattern: URL thường (1 tab) ───────────────────────────
        nav_match = re.match(
            r'(?:vào|navigate|go to|truy cập|chuyển đến|mở trang|open page)\s+(?:trang\s+)?(.+)',
            gl, re.I
        )
        if nav_match:
            target = nav_match.group(1).strip().rstrip('.')
            # Loại bỏ nếu có "trên profile" → đã handle bên trên
            if any(k in target for k in ('trên profile', 'trên hồ sơ', 'on profile')):
                return None
            # v4.3: "vào shopee tìm giá iPhone" is a task, not a navigation —
            # navigating and reporting success dropped the rest of the request.
            if len(target.split()) > 2 or _TASK_VERBS.search(_strip_accents(target)):
                return None
            url = ""
            for name, full_url in URL_PATTERNS.items():
                if name in target:
                    url = full_url
                    break
            if not url:
                url = target if target.startswith('http') else f"https://{target}"
                if '.' not in url.split('//')[1] if '//' in url else '.' not in url:
                    url = f"https://www.google.com/search?q={target}"

            _vlog("🌐", f"Chuyển đến: {url}")
            result = await self._applescript_navigate(url)
            if result:
                return ActionResult(success=True, priority=3, method="applescript",
                                    result=f"Navigated to {url}")

        # ── Pattern: tìm kiếm google ──────────────────────────────
        # v4.3: only EXPLICIT web searches ("tìm kiếm …", "search …", "google …").
        # Bare "tìm …" (find leads / find competitors …) belongs to workflows.
        search_match = re.match(
            r'^(?:tìm kiếm|search|google)\s+(?:(?:trên|on)\s+google\s+)?(.+?)(?:\s+(?:trên|on)\s+google)?$',
            self._normalize_command(gl), re.I
        )
        if search_match:
            query = search_match.group(1).strip()
            url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            _vlog("🔍", f"Tìm kiếm Google (Incognito): {query}")
            # FIX v1.0: mở Incognito window để search — sạch, không cần profile
            try:
                import subprocess as _sp
                _sp.Popen([
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "--incognito", url
                ])
                await asyncio.sleep(2)
                return ActionResult(success=True, priority=3, method="applescript",
                                    result=f"Searched (Incognito): {query}")
            except Exception:
                result = await self._applescript_navigate(url)
                if result:
                    return ActionResult(success=True, priority=3, method="applescript",
                                        result=f"Searched: {query}")

        return None

    async def _close_chrome_windows(self, n: int | None = None) -> bool:
        """Đóng N cửa sổ Chrome (n=None → đóng tất cả)."""
        if n is None:
            script = 'tell application "Google Chrome" to close every window'
        else:
            script = f'''
tell application "Google Chrome"
    set i to 0
    repeat while (count of windows) > 0 and i < {n}
        close window 1
        set i to i + 1
    end repeat
end tell
'''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0
        except Exception:
            return False

    async def _navigate_all_chrome_windows(self, url: str) -> bool:
        """Mở URL trong tất cả cửa sổ Chrome đang mở."""
        try:
            safe_url = _escape_applescript_url(url)
        except ValueError as e:
            _vlog("🛡️", f"[C-03] Unsafe URL: {str(e)[:60]}")
            return False

        script = f'''
tell application "Google Chrome"
    repeat with w in every window
        set URL of active tab of w to "{safe_url}"
    end repeat
end tell
'''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode == 0
        except Exception:
            return False

    async def _navigate_url_in_profile(self, url: str, profile_num: int) -> bool:
        """Mở URL trong cửa sổ Chrome thuộc profile có số prefix = profile_num."""
        if not self._scanner:
            return False
        # Tìm profile theo số
        profile = None
        if hasattr(self._scanner, 'get_profile_by_number'):
            profile = self._scanner.get_profile_by_number(profile_num)
        if not profile:
            profile = self._scanner.find_chrome_profile(str(profile_num))
        if not profile:
            _vlog("⚠️", f"Không tìm thấy profile số {profile_num}")
            return False

        try:
            safe_url = _escape_applescript_url(url)
        except ValueError as e:
            _vlog("🛡️", f"[C-03] Unsafe URL: {str(e)[:60]}")
            return False

        # Tìm cửa sổ Chrome thuộc profile đó (theo tên profile trong title hoặc index)
        # Cách đơn giản: activate Chrome → dùng window với title match profile name
        profile_name = profile.name
        script = f'''
tell application "Google Chrome"
    activate
    set profileName to "{profile_name}"
    set targetWindow to missing value
    repeat with w in every window
        set wTitle to name of w
        if wTitle contains profileName then
            set targetWindow to w
            exit repeat
        end if
    end repeat
    if targetWindow is missing value then
        -- fallback: try to match by profile directory via open new window
        -- just navigate front window
        set URL of active tab of front window to "{safe_url}"
    else
        set index of targetWindow to 1
        set URL of active tab of targetWindow to "{safe_url}"
    end if
end tell
'''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0
        except Exception:
            return False

    async def _applescript_navigate(self, url: str, browser: str = "") -> bool:
        """Navigate to URL using AppleScript."""
        if not browser:
            # Detect current browser
            browser = await self._get_frontmost_app()
            if "chrome" in browser.lower():
                browser = "chrome"
            elif "safari" in browser.lower():
                browser = "safari"
            else:
                browser = "chrome"  # default

        # [C-03 FIX] Sanitize URL before interpolating into AppleScript
        try:
            safe_url = _escape_applescript_url(url)
        except ValueError as _url_err:
            _vlog("🛡️", f"[C-03] Blocked unsafe URL for AppleScript: {str(_url_err)[:100]}")
            return False
        template = APPLESCRIPTS.get(f"{browser}_url", APPLESCRIPTS["chrome_url"])
        script = template.format(url=safe_url)

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            return proc.returncode == 0
        except Exception:
            # Fallback: use keyboard shortcut Cmd+L → type URL → Enter
            try:
                await self._ipc.send_action("keyboard_hotkey", {"keys": ["command", "l"]})
                await asyncio.sleep(0.3)
                await self._ipc.send_action("keyboard_type", {"text": url})
                await asyncio.sleep(0.2)
                await self._ipc.send_action("keyboard_press", {"key": "return"})
                return True
            except Exception:
                return False

    async def _get_frontmost_app(self) -> str:
        """Get the name of the frontmost application."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", APPLESCRIPTS["get_frontmost_app"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            return stdout.decode().strip()
        except Exception:
            return ""

    # ══════════════════════════════════════════════════════════
    # Priority 3b: Learned Workflows
    # ══════════════════════════════════════════════════════════

    async def _try_learned_workflow(self, goal: str) -> ActionResult | None:
        """Execute a previously learned workflow."""
        gl = goal.strip().lower()

        for wf in self._workflows:
            if re.search(wf.trigger, gl, re.I):
                _vlog("🧠", f"Workflow đã học: {wf.trigger}")
                try:
                    for step in wf.steps:
                        action = step.get("action", "")
                        payload = step.get("payload", {})
                        wait = step.get("wait", 0)
                        if wait:
                            await asyncio.sleep(wait)
                        await self._ipc.send_action(action, payload)
                    wf.success_count += 1
                    wf.last_used = time.time()
                    self._save_workflows()
                    return ActionResult(success=True, priority=3, method="learned_workflow",
                                        result=f"Workflow: {wf.trigger}")
                except Exception as exc:
                    return ActionResult(success=False, priority=3, method="learned_workflow",
                                        error=str(exc))

        return None

    # ══════════════════════════════════════════════════════════
    # Learning: save successful multi-step workflows
    # ══════════════════════════════════════════════════════════

    def learn_workflow(self, goal: str, steps: list[dict]) -> None:
        """
        Save a successful multi-step workflow for future reuse.

        Called by AgentLoop when a task completes successfully.
        """
        # Create a regex trigger from the goal
        trigger = re.escape(goal.strip().lower())
        # Don't save if too many steps (complex = unreliable)
        if len(steps) > 5:
            return
        # Don't duplicate
        for wf in self._workflows:
            if wf.trigger == trigger:
                wf.success_count += 1
                self._save_workflows()
                return

        self._workflows.append(LearnedWorkflow(
            trigger=trigger,
            steps=steps,
            success_count=1,
            last_used=time.time(),
        ))
        self._save_workflows()
        _vlog("💾", f"Workflow đã lưu: {goal[:50]}")

    def _load_workflows(self) -> None:
        """Load learned workflows from disk."""
        path = self._knowledge_dir / "workflows.json"
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                for item in data:
                    self._workflows.append(LearnedWorkflow(**item))
            except Exception:
                pass

    def _save_workflows(self) -> None:
        """Save learned workflows to disk."""
        path = self._knowledge_dir / "workflows.json"
        try:
            data = []
            for wf in self._workflows:
                data.append({
                    "trigger": wf.trigger,
                    "steps": wf.steps,
                    "success_count": wf.success_count,
                    "last_used": wf.last_used,
                })
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # Info / Stats
    # ══════════════════════════════════════════════════════════

    def stats(self) -> dict[str, Any]:
        """Return engine stats for Admin Panel."""
        return {
            "shortcuts_count": len(SHORTCUTS),
            "url_patterns_count": len(URL_PATTERNS),
            "learned_workflows": len(self._workflows),
            "has_scanner": self._scanner is not None,
            "apps_scanned": self._scanner.summary()["total_apps"] if self._scanner else 0,
        }
