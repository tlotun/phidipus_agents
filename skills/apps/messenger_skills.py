# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/apps/messenger_skills.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

Messenger Skill — Gửi tin nhắn + file trên nhiều nền tảng chat.

Nền tảng hỗ trợ:
  ├─ Facebook Messenger  (Chrome + CDP)
  ├─ Instagram DM        (Chrome + CDP)
  ├─ Telegram            (python-telegram-bot — đã có sẵn)
  ├─ Zalo                (Desktop app — AX accessibility)
  └─ WeChat              (Desktop app — AX accessibility)

Approach:
  - Web platforms (FB/IG): Chrome automation — mở conversation,
    type message, upload file qua file input
  - Desktop apps (Zalo/WeChat): AppleScript + AX UIElement
  - Telegram: gửi qua Bot API hoặc TDLib

Actions:
  send_message(platform, recipient, message, files=[]) → MessengerResult

Security:
  - File chỉ từ ~/Desktop, ~/Downloads, ~/Documents
  - Không exec() code tuỳ ý
  - Rate limit: max 1 message/3s để tránh spam

Entry point cho SmartRouter Fast Lane:
  run_messenger_task(goal, ipc_client, notify_fn) → MessengerResult
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.logger import get_logger

_log = get_logger("messenger_skills")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# Rate limit: tránh spam
_LAST_SEND: dict[str, float] = {}
_RATE_LIMIT_S = 3.0


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class MessengerResult:
    success:     bool
    action:      str
    platform:    str = ""
    recipient:   str = ""
    output:      Any = None
    error:       str = ""
    duration_ms: int = 0

    @property
    def summary(self) -> str:
        plat = f"[{self.platform}] " if self.platform else ""
        if self.success:
            return f"✅ {plat}{self.action} → {self.recipient}"
        return f"❌ {plat}{self.action}: {self.error[:80]}"


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

async def _run_applescript(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        ok = proc.returncode == 0
        return ok, (stdout if ok else stderr).decode().strip()
    except asyncio.TimeoutError:
        return False, f"Timeout {timeout}s"
    except Exception as exc:
        return False, str(exc)


async def _js(ipc_client: Any, code: str, timeout: float = 8.0) -> str:
    if not ipc_client:
        return ""
    try:
        resp = await asyncio.wait_for(
            ipc_client.send_action("browser_execute_js", {"js_code": code, "timeout_s": timeout}),
            timeout=timeout + 5,
        )
        if resp and resp.success:
            return "" if resp.result is None else str(resp.result)
    except Exception:
        pass
    return ""


def _safe_file_path(path: str) -> str | None:
    p = Path(path).expanduser().resolve()
    allowed = [Path.home() / d for d in ("Desktop", "Downloads", "Documents",
                                          "Pictures", "Movies")]
    if any(str(p).startswith(str(a)) for a in allowed) and p.exists():
        return str(p)
    return None


def _rate_check(platform: str) -> bool:
    """True nếu OK gửi (không bị rate limit)."""
    now = time.time()
    last = _LAST_SEND.get(platform, 0)
    if now - last < _RATE_LIMIT_S:
        return False
    _LAST_SEND[platform] = now
    return True


# ══════════════════════════════════════════════════════════════════
# Platform implementations
# ══════════════════════════════════════════════════════════════════

class _FacebookMessenger:
    """Gửi tin nhắn Facebook Messenger qua Chrome."""

    MESSENGER_URL = "https://www.messenger.com"

    async def send(
        self,
        recipient: str,
        message: str,
        files: list[str],
        ipc_client: Any,
    ) -> MessengerResult:
        t0 = time.time()
        _vlog("💬", f"FB Messenger → {recipient}: {message[:50]}")

        # Mở Messenger
        search_url = f"https://www.messenger.com/search/{recipient.replace(' ', '%20')}"
        script_open = f'''
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then make new window
    set URL of active tab of front window to "{search_url}"
end tell
'''
        ok, err = await _run_applescript(script_open, timeout=8.0)
        if not ok:
            return MessengerResult(False, "send_message", "messenger",
                                   recipient, error=f"Không mở được Chrome: {err}")
        await asyncio.sleep(3.0)

        # Click vào kết quả đầu tiên trong search
        click_first = """
(function() {
    var links = document.querySelectorAll('a[href*="/t/"]');
    if (links.length > 0) { links[0].click(); return 'clicked'; }
    var items = document.querySelectorAll('[role="link"]');
    if (items.length > 0) { items[0].click(); return 'clicked_role'; }
    return 'not_found';
})()
"""
        result = await _js(ipc_client, click_first)
        if "not_found" in result:
            return MessengerResult(False, "send_message", "messenger",
                                   recipient, error=f"Không tìm thấy conversation với '{recipient}'")
        await asyncio.sleep(2.0)

        # Upload files nếu có
        if files:
            for fpath in files:
                _vlog("📎", f"Upload file: {os.path.basename(fpath)}")
                # Click attach button
                attach_js = """
(function() {
    var btns = document.querySelectorAll('[aria-label*="Attach"]');
    if (btns.length > 0) { btns[btns.length-1].click(); return 'ok'; }
    return 'not_found';
})()
"""
                await _js(ipc_client, attach_js)
                await asyncio.sleep(0.8)
                # Type file path in dialog
                type_script = f'''
tell application "System Events"
    delay 0.5
    keystroke "{fpath}"
    delay 0.3
    keystroke return
    delay 1.5
end tell
'''
                await _run_applescript(type_script, timeout=8.0)

        # Type message
        if message:
            # Focus input box và type
            type_js = f"""
(function() {{
    var input = document.querySelector('[contenteditable="true"][role="textbox"]');
    if (!input) input = document.querySelector('[aria-label*="message"]');
    if (input) {{
        input.focus();
        document.execCommand('insertText', false, {repr(message)});
        return 'typed';
    }}
    return 'no_input';
}})()
"""
            await _js(ipc_client, type_js)
            await asyncio.sleep(0.5)

            # Nhấn Enter để gửi
            send_js = """
(function() {
    var event = new KeyboardEvent('keydown', {
        key: 'Enter', code: 'Enter', keyCode: 13,
        bubbles: true, cancelable: true
    });
    var input = document.querySelector('[contenteditable="true"][role="textbox"]');
    if (input) { input.dispatchEvent(event); return 'sent'; }
    return 'no_input';
})()
"""
            result = await _js(ipc_client, send_js)

        ms = int((time.time() - t0) * 1000)
        _vlog("✅", f"Messenger → {recipient} ({ms}ms)")
        return MessengerResult(True, "send_message", "messenger", recipient,
                               output={"message": message, "files": len(files)},
                               duration_ms=ms)


class _InstagramDM:
    """Gửi DM Instagram qua Chrome."""

    async def send(
        self,
        recipient: str,
        message: str,
        files: list[str],
        ipc_client: Any,
    ) -> MessengerResult:
        t0 = time.time()
        _vlog("📸", f"Instagram DM → {recipient}: {message[:50]}")

        # Mở Instagram DM search
        script_open = f'''
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then make new window
    set URL of active tab of front window to "https://www.instagram.com/direct/new/"
end tell
'''
        ok, err = await _run_applescript(script_open, timeout=8.0)
        if not ok:
            return MessengerResult(False, "send_message", "instagram",
                                   recipient, error=err)
        await asyncio.sleep(3.5)

        # Tìm người nhận trong search box
        search_js = f"""
(function() {{
    var inputs = document.querySelectorAll('input[placeholder*="Search"]');
    if (inputs.length == 0) inputs = document.querySelectorAll('input[aria-label*="Search"]');
    if (inputs.length > 0) {{
        inputs[0].focus();
        inputs[0].value = {repr(recipient)};
        inputs[0].dispatchEvent(new Event('input', {{bubbles: true}}));
        return 'searched';
    }}
    return 'no_search';
}})()
"""
        await _js(ipc_client, search_js)
        await asyncio.sleep(1.5)

        # Click kết quả đầu tiên
        click_js = """
(function() {
    var items = document.querySelectorAll('[role="button"]');
    for (var item of items) {
        if (item.innerText && item.innerText.trim().length > 0) {
            item.click(); return 'clicked';
        }
    }
    return 'not_found';
})()
"""
        await _js(ipc_client, click_js)
        await asyncio.sleep(1.0)

        # Click Next
        next_js = """
(function() {
    var btns = document.querySelectorAll('button');
    for (var b of btns) {
        if (b.innerText && b.innerText.toLowerCase().includes('next')) {
            b.click(); return 'next_clicked';
        }
    }
    return 'not_found';
})()
"""
        await _js(ipc_client, next_js)
        await asyncio.sleep(2.0)

        # Upload files
        if files:
            for fpath in files:
                _vlog("📎", f"Upload: {os.path.basename(fpath)}")
                # Click media/file button
                media_js = """
(function() {
    var btns = document.querySelectorAll('[aria-label*="Add Photo"]');
    if (btns.length > 0) { btns[0].click(); return 'ok'; }
    return 'not_found';
})()
"""
                await _js(ipc_client, media_js)
                await asyncio.sleep(0.8)
                type_script = f'''
tell application "System Events"
    delay 0.5
    keystroke "{fpath}"
    delay 0.3
    keystroke return
    delay 2
end tell
'''
                await _run_applescript(type_script, timeout=10.0)

        # Type & send message
        if message:
            type_js = f"""
(function() {{
    var input = document.querySelector('textarea[placeholder*="Message"]');
    if (!input) input = document.querySelector('[contenteditable="true"]');
    if (input) {{
        input.focus();
        if (input.tagName === 'TEXTAREA') {{
            input.value = {repr(message)};
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
        }} else {{
            document.execCommand('insertText', false, {repr(message)});
        }}
        return 'typed';
    }}
    return 'no_input';
}})()
"""
            await _js(ipc_client, type_js)
            await asyncio.sleep(0.5)

            # Send
            send_script = '''
tell application "System Events"
    key code 36
end tell
'''
            await _run_applescript(send_script, timeout=5.0)

        ms = int((time.time() - t0) * 1000)
        _vlog("✅", f"Instagram DM → {recipient} ({ms}ms)")
        return MessengerResult(True, "send_message", "instagram", recipient,
                               output={"message": message, "files": len(files)},
                               duration_ms=ms)


class _ZaloDesktop:
    """Gửi tin nhắn Zalo qua Desktop app (AX accessibility)."""

    async def send(
        self,
        recipient: str,
        message: str,
        files: list[str],
        ipc_client: Any,
    ) -> MessengerResult:
        t0 = time.time()
        _vlog("💙", f"Zalo → {recipient}: {message[:50]}")

        # Kiểm tra Zalo có đang chạy không
        check_script = '''
tell application "System Events"
    set zalo_running to (count of (processes whose name is "Zalo")) > 0
    return zalo_running
end tell
'''
        ok, result = await _run_applescript(check_script, timeout=5.0)
        if not ok or result != "true":
            # Mở Zalo
            _vlog("🔍", "Đang mở Zalo...")
            open_script = '''
tell application "Zalo" to activate
delay 2
'''
            await _run_applescript(open_script, timeout=8.0)
            await asyncio.sleep(2.0)

        # Focus Zalo + search người nhận
        search_script = f'''
tell application "Zalo" to activate
delay 0.5
tell application "System Events"
    tell process "Zalo"
        -- Cmd+F để mở search
        keystroke "f" using command down
        delay 0.8
        keystroke "{recipient}"
        delay 1.5
        -- Nhấn Enter hoặc click kết quả đầu tiên
        key code 36
        delay 1
    end tell
end tell
'''
        ok, err = await _run_applescript(search_script, timeout=12.0)
        if not ok:
            return MessengerResult(False, "send_message", "zalo",
                                   recipient, error=f"Không mở được conversation: {err}")

        await asyncio.sleep(1.5)

        # Gửi file nếu có
        if files:
            for fpath in files:
                _vlog("📎", f"Gửi file Zalo: {os.path.basename(fpath)}")
                # Kéo thả file vào cửa sổ Zalo (drag & drop)
                drag_script = f'''
tell application "System Events"
    tell process "Zalo"
        -- Dùng file input shortcut
        keystroke "f" using {{command down, shift down}}
        delay 1
        keystroke "{fpath}"
        delay 0.5
        key code 36
        delay 1
    end tell
end tell
'''
                await _run_applescript(drag_script, timeout=10.0)
                await asyncio.sleep(1.5)

        # Gửi message
        if message:
            type_script = f'''
tell application "System Events"
    tell process "Zalo"
        -- Click vào text input area (thường ở cuối cửa sổ)
        keystroke tab
        delay 0.3
        keystroke "{message}"
        delay 0.3
        key code 36
    end tell
end tell
'''
            ok, err = await _run_applescript(type_script, timeout=10.0)
            if not ok:
                return MessengerResult(False, "send_message", "zalo",
                                       recipient, error=f"Gửi thất bại: {err}")

        ms = int((time.time() - t0) * 1000)
        _vlog("✅", f"Zalo → {recipient} ({ms}ms)")
        return MessengerResult(True, "send_message", "zalo", recipient,
                               output={"message": message, "files": len(files)},
                               duration_ms=ms)


class _WeChatDesktop:
    """Gửi tin nhắn WeChat qua Desktop app."""

    async def send(
        self,
        recipient: str,
        message: str,
        files: list[str],
        ipc_client: Any,
    ) -> MessengerResult:
        t0 = time.time()
        _vlog("💚", f"WeChat → {recipient}: {message[:50]}")

        # Mở WeChat
        open_script = '''
tell application "WeChat" to activate
delay 1.5
'''
        await _run_applescript(open_script, timeout=8.0)
        await asyncio.sleep(1.5)

        # Search người nhận (Cmd+F trong WeChat)
        search_script = f'''
tell application "System Events"
    tell process "WeChat"
        keystroke "f" using command down
        delay 0.8
        keystroke "{recipient}"
        delay 1.5
        key code 36
        delay 1
    end tell
end tell
'''
        ok, err = await _run_applescript(search_script, timeout=12.0)
        if not ok:
            return MessengerResult(False, "send_message", "wechat",
                                   recipient, error=err)

        await asyncio.sleep(1.5)

        # Gửi file
        if files:
            for fpath in files:
                _vlog("📎", f"Gửi file WeChat: {os.path.basename(fpath)}")
                # WeChat: kéo file vào chat window
                drop_script = f'''
tell application "System Events"
    tell process "WeChat"
        keystroke "f" using {{command down, shift down}}
        delay 1
        keystroke "{fpath}"
        delay 0.5
        key code 36
        delay 1.5
    end tell
end tell
'''
                await _run_applescript(drop_script, timeout=10.0)
                await asyncio.sleep(1.5)

        # Gửi message
        if message:
            type_script = f'''
tell application "System Events"
    tell process "WeChat"
        keystroke "{message}"
        delay 0.3
        key code 36
    end tell
end tell
'''
            ok, err = await _run_applescript(type_script, timeout=10.0)
            if not ok:
                return MessengerResult(False, "send_message", "wechat",
                                       recipient, error=err)

        ms = int((time.time() - t0) * 1000)
        _vlog("✅", f"WeChat → {recipient} ({ms}ms)")
        return MessengerResult(True, "send_message", "wechat", recipient,
                               output={"message": message, "files": len(files)},
                               duration_ms=ms)


class _TelegramSend:
    """Gửi tin nhắn Telegram — qua Bot API (nếu có token) hoặc Desktop app."""

    def __init__(self, bot_token: str = "", bot_app: Any = None) -> None:
        self._token = bot_token
        self._app = bot_app  # python-telegram-bot Application instance

    async def send(
        self,
        recipient: str,  # username (@user) hoặc chat_id
        message: str,
        files: list[str],
        ipc_client: Any,
    ) -> MessengerResult:
        t0 = time.time()
        _vlog("✈️ ", f"Telegram → {recipient}: {message[:50]}")

        # Method 1: Bot API (nếu có app instance)
        if self._app:
            try:
                # recipient có thể là @username hoặc chat_id số
                chat_id = recipient
                if not recipient.startswith("@") and not recipient.lstrip("-").isdigit():
                    # Tên người dùng không có @ → thêm vào
                    chat_id = f"@{recipient}"

                # Gửi file trước (nếu có)
                for fpath in files:
                    _vlog("📎", f"Telegram send file: {os.path.basename(fpath)}")
                    try:
                        with open(fpath, "rb") as f:
                            await self._app.bot.send_document(
                                chat_id=chat_id,
                                document=f,
                                caption=message if message else None,
                            )
                        if message and files.index(fpath) == 0:
                            message = ""  # đã gửi message với file đầu
                    except Exception as exc:
                        _vlog("⚠️ ", f"Send file error: {exc}")

                # Gửi text message (nếu còn)
                if message:
                    await self._app.bot.send_message(chat_id=chat_id, text=message)

                ms = int((time.time() - t0) * 1000)
                _vlog("✅", f"Telegram Bot API → {recipient} ({ms}ms)")
                return MessengerResult(True, "send_message", "telegram", recipient,
                                       output={"message": message, "files": len(files)},
                                       duration_ms=ms)
            except Exception as exc:
                _vlog("⚠️ ", f"Bot API error: {exc} — thử Desktop app...")

        # Method 2: Telegram Desktop app (fallback)
        open_script = '''
tell application "Telegram" to activate
delay 1.5
'''
        await _run_applescript(open_script, timeout=8.0)
        await asyncio.sleep(1.5)

        # Cmd+K để mở search
        search_script = f'''
tell application "System Events"
    tell process "Telegram"
        keystroke "k" using command down
        delay 0.8
        keystroke "{recipient}"
        delay 1.5
        key code 36
        delay 1
    end tell
end tell
'''
        ok, err = await _run_applescript(search_script, timeout=12.0)
        if not ok:
            return MessengerResult(False, "send_message", "telegram",
                                   recipient, error=err)
        await asyncio.sleep(1.0)

        # Gửi file
        if files:
            for fpath in files:
                attach_script = f'''
tell application "System Events"
    tell process "Telegram"
        keystroke "u" using command down
        delay 1
        keystroke "{fpath}"
        delay 0.5
        key code 36
        delay 2
    end tell
end tell
'''
                await _run_applescript(attach_script, timeout=10.0)
                await asyncio.sleep(1.5)

        # Gửi message
        if message:
            type_script = f'''
tell application "System Events"
    tell process "Telegram"
        keystroke "{message}"
        delay 0.3
        key code 36
    end tell
end tell
'''
            await _run_applescript(type_script, timeout=10.0)

        ms = int((time.time() - t0) * 1000)
        _vlog("✅", f"Telegram Desktop → {recipient} ({ms}ms)")
        return MessengerResult(True, "send_message", "telegram", recipient,
                               output={"message": message, "files": len(files)},
                               duration_ms=ms)


# ══════════════════════════════════════════════════════════════════
# MessengerSkills — Unified interface
# ══════════════════════════════════════════════════════════════════

PLATFORM_ALIASES = {
    "messenger":   ["messenger", "facebook messenger", "fb messenger", "fb chat", "fbm"],
    "instagram":   ["instagram", "ig", "instagram dm", "ig dm", "insta"],
    "zalo":        ["zalo"],
    "wechat":      ["wechat", "we chat", "微信"],
    "telegram":    ["telegram", "tele", "tg"],
}


def _detect_platform(text: str) -> str:
    """Detect nền tảng từ câu lệnh. FIX v1.0: tránh nhầm email."""
    tl = text.lower()
    # Không phải messenger nếu câu rõ ràng là email
    if re.search(r"\bgửi\s+email\b|\bsend\s+email\b|\bsend\s+mail\b", tl):
        return ""
    for platform, aliases in PLATFORM_ALIASES.items():
        if any(a in tl for a in aliases):
            return platform
    return ""


def _extract_recipient(text: str) -> str:
    """Tìm tên người nhận. FIX v1.0: dừng tại động từ/nội dung."""
    # @username trước
    m = re.search(r"@(\w+)", text)
    if m:
        return "@" + m.group(1)
    # "cho/to <tên>" — dừng tại động từ tiếp theo hoặc dấu câu
    m = re.search(
        r"(?:cho|to|send to|gửi cho|gửi tới)\s+"
        r"([\w\sÀ-ỹ]{1,30}?)"
        r"(?=\s*(?::|,|nội dung|message|họp|lúc|nhắn|xin|hello|file|báo|\Z))",
        text, re.I
    )
    if m:
        return m.group(1).strip()
    # Fallback: lấy 1-2 từ sau "cho/to"
    m = re.search(r"(?:cho|to|send to|gửi cho|gửi tới)\s+(\S+(?:\s+\S+)?)", text, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _extract_message(text: str) -> str:
    """Tìm nội dung tin nhắn."""
    for kw in ("nội dung", "message", "nói", "với nội dung", "tin nhắn", "nhắn"):
        if kw in text.lower():
            idx = text.lower().find(kw) + len(kw)
            return text[idx:].strip().strip('"\'')[:500]
    # Fallback: bỏ phần đầu chứa platform và recipient
    cleaned = re.sub(
        r"(?:gửi|send|nhắn|tin nhắn|file|đến|cho|to)\s+\S+\s*(?:qua|on|trên|via)?\s*\S*",
        "", text, flags=re.I
    ).strip()
    return cleaned[:500] if cleaned else ""


def _extract_files(text: str) -> list[str]:
    """Tìm file đính kèm."""
    paths = []
    for m in re.finditer(r"(~/[\S]+|(?:Desktop|Downloads|Documents)/[\S]+)", text):
        safe = _safe_file_path(m.group(0))
        if safe:
            paths.append(safe)
    return paths


class MessengerSkills:
    """Unified messenger interface cho tất cả platforms."""

    def __init__(
        self,
        ipc_client: Any = None,
        telegram_app: Any = None,
        telegram_token: str = "",
    ) -> None:
        self._ipc = ipc_client
        self._fb   = _FacebookMessenger()
        self._ig   = _InstagramDM()
        self._zalo = _ZaloDesktop()
        self._wc   = _WeChatDesktop()
        self._tg   = _TelegramSend(bot_token=telegram_token, bot_app=telegram_app)

    async def send_message(
        self,
        platform: str,
        recipient: str,
        message: str = "",
        files: list[str] | None = None,
    ) -> MessengerResult:
        """
        Gửi tin nhắn + file đến người nhận trên platform chỉ định.

        Args:
            platform: "messenger" | "instagram" | "zalo" | "wechat" | "telegram"
            recipient: tên người nhận hoặc @username
            message: nội dung tin nhắn
            files: list đường dẫn file (optional)
        """
        if not recipient:
            return MessengerResult(False, "send_message", platform, "",
                                   error="Thiếu tên người nhận")
        if not message and not files:
            return MessengerResult(False, "send_message", platform, recipient,
                                   error="Cần ít nhất tin nhắn hoặc file để gửi")

        # Rate limit check
        if not _rate_check(platform):
            await asyncio.sleep(_RATE_LIMIT_S)

        # Validate files
        safe_files = []
        if files:
            for f in files:
                safe = _safe_file_path(f)
                if safe:
                    safe_files.append(safe)
                else:
                    _vlog("⚠️ ", f"File không hợp lệ/không tồn tại: {f}")

        plat = platform.lower()

        if plat == "messenger":
            return await self._fb.send(recipient, message, safe_files, self._ipc)
        elif plat == "instagram":
            return await self._ig.send(recipient, message, safe_files, self._ipc)
        elif plat == "zalo":
            return await self._zalo.send(recipient, message, safe_files, self._ipc)
        elif plat == "wechat":
            return await self._wc.send(recipient, message, safe_files, self._ipc)
        elif plat == "telegram":
            return await self._tg.send(recipient, message, safe_files, self._ipc)
        else:
            return MessengerResult(False, "send_message", platform, recipient,
                                   error=f"Platform '{platform}' không được hỗ trợ. "
                                         "Dùng: messenger, instagram, zalo, wechat, telegram")


# ══════════════════════════════════════════════════════════════════
# Entry point cho SmartRouter
# ══════════════════════════════════════════════════════════════════

_MESSENGER = MessengerSkills()


async def run_messenger_task(
    goal: str,
    ipc_client: Any = None,
    notify_fn: Any = None,
    telegram_app: Any = None,
) -> MessengerResult:
    """
    Entry point cho SmartRouter Fast Lane (messenger intent).

    Examples:
        "gửi tin nhắn cho Lan qua messenger: chào bạn"
        "nhắn zalo cho sếp file báo cáo Desktop/report.pdf"
        "gửi instagram DM cho @johndoe ảnh này Desktop/photo.jpg"
        "telegram cho @teamlead: họp lúc 3h chiều"
        "gửi wechat cho 小明: 你好"
    """
    skills = MessengerSkills(ipc_client=ipc_client, telegram_app=telegram_app)

    # Detect platform
    platform = _detect_platform(goal)
    if not platform:
        result = MessengerResult(
            False, "send_message", "", "",
            error="Không xác định được nền tảng. Vui lòng nêu rõ: "
                  "messenger / instagram / zalo / wechat / telegram"
        )
    else:
        recipient = _extract_recipient(goal)
        message   = _extract_message(goal)
        files     = _extract_files(goal)

        if not recipient:
            result = MessengerResult(
                False, "send_message", platform, "",
                error=f"Không tìm thấy người nhận. Ví dụ: 'gửi {platform} cho Lan: xin chào'"
            )
        else:
            result = await skills.send_message(
                platform=platform,
                recipient=recipient,
                message=message,
                files=files,
            )

    # Notify Telegram
    if notify_fn:
        try:
            PLATFORM_ICONS = {
                "messenger":  "💬",
                "instagram":  "📸",
                "zalo":       "💙",
                "wechat":     "💚",
                "telegram":   "✈️",
            }
            icon = PLATFORM_ICONS.get(result.platform, "📱")
            if result.success:
                output = result.output or {}
                msg = (
                    f"{icon} *Đã gửi thành công!*\n\n"
                    f"📱 *Platform:* {result.platform.capitalize()}\n"
                    f"👤 *Đến:* {result.recipient}\n"
                )
                if output.get("message"):
                    msg += f"💬 *Tin nhắn:* _{output['message'][:100]}_\n"
                if output.get("files"):
                    msg += f"📎 *File:* {output['files']} file đính kèm"
            else:
                msg = (
                    f"❌ *Gửi thất bại!*\n\n"
                    f"📱 *Platform:* {result.platform or 'không xác định'}\n"
                    f"⚠️ *Lỗi:* {result.error[:200]}"
                )
            await notify_fn(msg)
        except Exception:
            pass

    return result
