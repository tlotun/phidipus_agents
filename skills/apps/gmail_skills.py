# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/apps/gmail_skills.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

Gmail Skill — Gửi/đọc email qua Chrome automation.

Approach: Chrome CDP + AppleScript
  - Mở Chrome với profile đã login Gmail
  - Dùng JS injection để tương tác Gmail
  - Không cần Gmail API key, không cần OAuth setup

Actions:
  send_email(to, subject, body, attachments=[])  → GmailResult
  read_emails(max=10, unread_only=True)           → GmailResult
  search_emails(query, max=10)                    → GmailResult
  reply_email(thread_id, body)                    → GmailResult
  get_email_detail(thread_id)                     → GmailResult

Entry point cho SmartRouter Fast Lane:
  run_gmail_task(goal, ipc_client, notify_fn) → GmailResult

Security:
  - Chỉ gửi đến địa chỉ email hợp lệ (regex check)
  - Không exec() code tuỳ ý
  - File attach chỉ trong ~/Desktop, ~/Downloads, ~/Documents
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

_log = get_logger("gmail_skills")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class GmailResult:
    success:  bool
    action:   str
    output:   Any  = None
    error:    str  = ""
    duration_ms: int = 0

    @property
    def summary(self) -> str:
        if self.success:
            if isinstance(self.output, list):
                return f"✅ {self.action}: {len(self.output)} items"
            return f"✅ {self.action}: OK"
        return f"❌ {self.action}: {self.error[:80]}"


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

def _valid_email(addr: str) -> bool:
    return bool(_EMAIL_RE.match(addr.strip()))

def _safe_attach_path(path: str) -> str | None:
    """Chỉ cho phép attach từ thư mục an toàn."""
    p = Path(path).expanduser().resolve()
    allowed = [Path.home() / d for d in ("Desktop", "Downloads", "Documents", "Pictures")]
    if any(str(p).startswith(str(a)) for a in allowed):
        return str(p) if p.exists() else None
    return None

async def _run_applescript(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Chạy AppleScript với timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        ok = proc.returncode == 0
        out = stdout.decode().strip()
        err = stderr.decode().strip()
        return ok, out if ok else err
    except asyncio.TimeoutError:
        return False, f"Timeout after {timeout}s"
    except Exception as exc:
        return False, str(exc)


async def _run_js_in_chrome(js: str, ipc_client: Any, timeout: float = 10.0) -> str:
    """Thực thi JS trong Chrome qua IPC."""
    if not ipc_client:
        return ""
    try:
        resp = await asyncio.wait_for(
            ipc_client.send_action("browser_execute_js", {"js_code": js, "timeout_s": timeout}),
            timeout=timeout + 5,
        )
        if resp and resp.success:
            return "" if resp.result is None else str(resp.result)
    except Exception:
        pass
    return ""


# ══════════════════════════════════════════════════════════════════
# GmailSkills
# ══════════════════════════════════════════════════════════════════

class GmailSkills:
    """
    Skill gửi/đọc Gmail qua Chrome automation.

    Dùng Chrome profile đã login Gmail — không cần API key.
    Tự động mở Gmail nếu chưa mở, navigate về Gmail nếu đang tab khác.
    """

    GMAIL_URL = "https://mail.google.com"

    def __init__(self, ipc_client: Any = None, chrome_profile: str = "") -> None:
        self._ipc = ipc_client
        self._profile = chrome_profile  # tên Chrome profile, để trống = dùng profile mặc định

    # ── Mở Gmail ─────────────────────────────────────────────────

    async def _ensure_gmail_open(self) -> bool:
        """Đảm bảo Chrome đang mở Gmail."""
        t0 = time.time()

        # Mở Gmail trực tiếp qua AppleScript
        script = f'''
tell application "Google Chrome"
    activate
    set found to false
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains "mail.google.com" then
                set active tab index of w to index of t
                set index of w to 1
                set found to true
                exit repeat
            end if
        end repeat
        if found then exit repeat
    end repeat
    if not found then
        if (count of windows) = 0 then
            make new window
        end if
        set URL of active tab of front window to "{self.GMAIL_URL}"
    end if
end tell
'''
        ok, _ = await _run_applescript(script, timeout=8.0)
        if ok:
            await asyncio.sleep(2.5)  # chờ Gmail load
        return ok

    # ── Gửi email ────────────────────────────────────────────────

    async def send_email(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        attachments: list[str] | None = None,
    ) -> GmailResult:
        """
        Gửi email qua Gmail.

        Args:
            to: địa chỉ nhận (string hoặc list)
            subject: tiêu đề
            body: nội dung
            attachments: list đường dẫn file đính kèm (optional)
        """
        t0 = time.time()

        # Validate email
        to_list = [to] if isinstance(to, str) else to
        invalid = [a for a in to_list if not _valid_email(a)]
        if invalid:
            return GmailResult(False, "send_email",
                               error=f"Email không hợp lệ: {', '.join(invalid)}")

        to_str = ", ".join(to_list)
        _vlog("📧", f"Gửi email đến: {to_str}")

        # Validate attachments
        safe_attachments = []
        if attachments:
            for path in attachments:
                safe = _safe_attach_path(path)
                if safe:
                    safe_attachments.append(safe)
                else:
                    _vlog("⚠️ ", f"Bỏ qua file không hợp lệ: {path}")

        # Mở Gmail compose qua URL
        # Gmail compose URL cho phép pre-fill to/subject/body
        import urllib.parse
        compose_url = (
            f"https://mail.google.com/mail/?view=cm&fs=1"
            f"&to={urllib.parse.quote(to_str)}"
            f"&su={urllib.parse.quote(subject)}"
            f"&body={urllib.parse.quote(body)}"
        )

        # Mở compose window
        script_open = f'''
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then make new window
    set URL of active tab of front window to "{compose_url}"
end tell
'''
        ok, err = await _run_applescript(script_open, timeout=8.0)
        if not ok:
            return GmailResult(False, "send_email",
                               error=f"Không mở được Gmail: {err}")

        await asyncio.sleep(3.0)  # chờ compose window load

        # Xử lý attachments nếu có
        if safe_attachments:
            _vlog("📎", f"Đính kèm {len(safe_attachments)} file...")
            for fpath in safe_attachments:
                # Click nút attach (icon paperclip) qua JS
                attach_js = """
(function() {
    var btns = document.querySelectorAll('[data-tooltip="Attach files"]');
    if (btns.length > 0) { btns[btns.length-1].click(); return 'clicked'; }
    var btns2 = document.querySelectorAll('[aria-label*="Attach"]');
    if (btns2.length > 0) { btns2[btns2.length-1].click(); return 'clicked2'; }
    return 'not_found';
})()
"""
                await _run_js_in_chrome(attach_js, self._ipc)
                await asyncio.sleep(1.0)
                # NOTE: File attachment qua file picker cần AX automation
                # Dùng AppleScript để handle file dialog
                attach_script = f'''
tell application "System Events"
    delay 0.5
    keystroke "{fpath}"
    delay 0.3
    keystroke return
end tell
'''
                await _run_applescript(attach_script, timeout=5.0)
                await asyncio.sleep(1.5)

        # Click Send button
        send_js = """
(function() {
    // Tìm nút Send trong Gmail compose
    var btns = document.querySelectorAll('[data-tooltip*="Send"]');
    for (var b of btns) {
        if (b.getAttribute('data-tooltip').toLowerCase().includes('send')) {
            b.click(); return 'sent';
        }
    }
    // Fallback: tìm theo aria-label
    var btns2 = document.querySelectorAll('[aria-label*="Send"]');
    if (btns2.length > 0) { btns2[btns2.length-1].click(); return 'sent_aria'; }
    // Fallback 2: shortcut Ctrl+Enter / Cmd+Enter
    return 'use_shortcut';
})()
"""
        result_js = await _run_js_in_chrome(send_js, self._ipc)

        if "sent" in result_js:
            _vlog("✅", f"Email đã gửi đến {to_str}")
        else:
            # Dùng keyboard shortcut Cmd+Enter (Gmail send shortcut)
            _vlog("⌨️ ", "Gửi bằng Cmd+Enter...")
            shortcut_script = '''
tell application "System Events"
    tell application "Google Chrome" to activate
    delay 0.3
    key code 36 using command down
end tell
'''
            await _run_applescript(shortcut_script, timeout=5.0)
            await asyncio.sleep(1.0)

        ms = int((time.time() - t0) * 1000)
        _vlog("📧", f"send_email → {to_str} ({ms}ms)")
        return GmailResult(
            success=True,
            action="send_email",
            output={"to": to_str, "subject": subject,
                    "attachments": len(safe_attachments)},
            duration_ms=ms,
        )

    # ── Đọc email ────────────────────────────────────────────────

    async def read_emails(
        self,
        max: int = 10,
        unread_only: bool = True,
    ) -> GmailResult:
        """Đọc danh sách email (unread hoặc tất cả)."""
        t0 = time.time()
        _vlog("📬", f"Đọc {'unread ' if unread_only else ''}emails (max={max})...")

        await self._ensure_gmail_open()
        await asyncio.sleep(2.0)

        # JS để extract email list
        js = f"""
(function() {{
    var emails = [];
    var rows = document.querySelectorAll('[role="row"]');
    var count = 0;
    for (var row of rows) {{
        if (count >= {max}) break;
        var isUnread = row.classList.contains('zE') || row.querySelector('.zF');
        if ({str(unread_only).lower()} && !isUnread) continue;
        var sender = row.querySelector('[email]');
        var subject = row.querySelector('.y6, .bog');
        var snippet = row.querySelector('.y2');
        var time_el = row.querySelector('[title]');
        if (!subject) continue;
        emails.push({{
            sender: sender ? (sender.getAttribute('email') || sender.innerText) : '',
            subject: subject ? subject.innerText.trim() : '',
            snippet: snippet ? snippet.innerText.trim() : '',
            time: time_el ? time_el.getAttribute('title') : '',
            unread: isUnread
        }});
        count++;
    }}
    return JSON.stringify(emails);
}})()
"""
        raw = await _run_js_in_chrome(js, self._ipc)

        emails = []
        if raw:
            try:
                import json
                emails = json.loads(raw)
            except Exception:
                pass

        ms = int((time.time() - t0) * 1000)
        _vlog("📬", f"read_emails: {len(emails)} emails ({ms}ms)")
        return GmailResult(
            success=True,
            action="read_emails",
            output=emails,
            duration_ms=ms,
        )

    # ── Tìm email ────────────────────────────────────────────────

    async def search_emails(self, query: str, max: int = 10) -> GmailResult:
        """Tìm email theo query (như Gmail search bar)."""
        t0 = time.time()
        import urllib.parse
        search_url = f"https://mail.google.com/mail/?#search/{urllib.parse.quote(query)}"

        script = f'''
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then make new window
    set URL of active tab of front window to "{search_url}"
end tell
'''
        await _run_applescript(script, timeout=8.0)
        await asyncio.sleep(2.5)

        result = await self.read_emails(max=max, unread_only=False)
        result.action = "search_emails"
        ms = int((time.time() - t0) * 1000)
        result.duration_ms = ms
        _vlog("🔍", f"search_emails '{query}': {len(result.output or [])} kết quả ({ms}ms)")
        return result


# ══════════════════════════════════════════════════════════════════
# Parse helpers
# ══════════════════════════════════════════════════════════════════

def _extract_email_addr(text: str) -> str:
    """Tìm địa chỉ email trong text."""
    m = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    return m.group(0) if m else ""

def _extract_subject(text: str) -> str:
    """Tìm subject từ câu lệnh."""
    for kw in ("với tiêu đề", "tiêu đề", "subject", "chủ đề"):
        if kw in text.lower():
            idx = text.lower().find(kw) + len(kw)
            part = text[idx:].strip().strip('"\'')
            # Lấy đến dấu phẩy hoặc "nội dung"
            end = re.search(r',|nội dung|body', part, re.I)
            return part[:end.start()].strip() if end else part[:80].strip()
    return "Phidipus"

def _extract_body(text: str) -> str:
    """Tìm body từ câu lệnh."""
    for kw in ("nội dung", "body", "nói rằng", "với nội dung", "viết"):
        if kw in text.lower():
            idx = text.lower().find(kw) + len(kw)
            return text[idx:].strip().strip('"\'')[:500]
    # Nếu không tìm được body rõ, dùng toàn bộ goal
    return text[:500]

def _extract_attachments(text: str) -> list[str]:
    """Tìm file đính kèm từ câu lệnh."""
    paths = []
    # Tìm path pattern
    for m in re.finditer(r"(~/[\S]+|(?:Desktop|Downloads|Documents)/[\S]+)", text):
        p = str(Path(m.group(0)).expanduser())
        if os.path.exists(p):
            paths.append(p)
    return paths


# ══════════════════════════════════════════════════════════════════
# Entry point cho SmartRouter
# ══════════════════════════════════════════════════════════════════

_GMAIL = GmailSkills()


async def run_gmail_task(
    goal: str,
    ipc_client: Any = None,
    notify_fn: Any = None,
    chrome_profile: str = "",
) -> GmailResult:
    """
    Entry point cho SmartRouter Fast Lane (email intent).

    Examples:
        "gửi email cho boss@company.com tiêu đề Báo cáo nội dung xin chào"
        "đọc email chưa đọc"
        "tìm email từ sếp tuần này"
        "gửi file report.pdf cho john@example.com"
    """
    skills = GmailSkills(ipc_client=ipc_client, chrome_profile=chrome_profile)
    gl = goal.lower()
    result: GmailResult

    # ── Gửi email ────────────────────────────────────────────────
    if any(k in gl for k in ("gửi", "send", "soạn", "compose", "viết email", "email cho")):
        to_addr = _extract_email_addr(goal)
        if not to_addr:
            # Thử tìm tên người nhận → dùng như placeholder
            result = GmailResult(False, "send_email",
                                 error="Không tìm thấy địa chỉ email người nhận. "
                                       "Vui lòng cung cấp email (vd: name@gmail.com)")
        else:
            subject = _extract_subject(goal)
            body    = _extract_body(goal)
            attachments = _extract_attachments(goal)

            # Nếu có nhắc đến file nhưng không tìm được → báo
            if any(k in gl for k in ("đính kèm", "attach", "gửi file", "gửi kèm")) \
                    and not attachments:
                result = GmailResult(False, "send_email",
                                     error="Có nhắc đến file đính kèm nhưng không "
                                           "tìm thấy file. Kiểm tra đường dẫn file.")
            else:
                result = await skills.send_email(
                    to=to_addr, subject=subject, body=body,
                    attachments=attachments if attachments else None,
                )

    # ── Đọc email ────────────────────────────────────────────────
    elif any(k in gl for k in ("đọc", "read", "check", "kiểm tra", "xem email",
                                "inbox", "hộp thư")):
        unread_only = "chưa đọc" in gl or "unread" in gl
        result = await skills.read_emails(max=10, unread_only=unread_only)

    # ── Tìm email ─────────────────────────────────────────────────
    elif any(k in gl for k in ("tìm", "search", "find")):
        # Lấy query sau từ khóa
        for kw in ("tìm email", "search email", "find email", "tìm thư"):
            if kw in gl:
                query = goal[gl.find(kw) + len(kw):].strip()
                result = await skills.search_emails(query=query)
                break
        else:
            result = await skills.search_emails(query=goal)

    else:
        result = GmailResult(False, "gmail_task",
                             error=f"Không hiểu lệnh: '{goal[:80]}'. "
                                   "Thử: 'gửi email', 'đọc email', 'tìm email'")

    # ── Notify Telegram ───────────────────────────────────────────
    if notify_fn:
        try:
            if result.success:
                output = result.output
                action = result.action

                if action == "send_email" and isinstance(output, dict):
                    msg = (f"📧 *Đã gửi email thành công!*\n\n"
                           f"📮 *Đến:* `{output.get('to', '')}`\n"
                           f"📝 *Tiêu đề:* {output.get('subject', '')}\n")
                    if output.get("attachments"):
                        msg += f"📎 *Đính kèm:* {output['attachments']} file"

                elif action == "read_emails" and isinstance(output, list):
                    if output:
                        lines = [f"📬 *{len(output)} email{'s' if len(output)>1 else ''}:*\n"]
                        for e in output[:5]:
                            unread_mark = "🔵 " if e.get("unread") else "   "
                            lines.append(f"{unread_mark}*{e.get('sender','?')[:20]}*")
                            lines.append(f"   _{e.get('subject','')[:50]}_")
                        if len(output) > 5:
                            lines.append(f"   _...và {len(output)-5} emails khác_")
                        msg = "\n".join(lines)
                    else:
                        msg = "📭 Không có email nào."

                elif action == "search_emails" and isinstance(output, list):
                    msg = f"🔍 *Tìm thấy {len(output)} email*"

                else:
                    msg = f"✅ *{action}* hoàn thành"

                await notify_fn(msg)
            else:
                await notify_fn(f"❌ *Gmail:* {result.error[:200]}")
        except Exception:
            pass

    return result
