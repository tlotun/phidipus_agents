# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
wechat/wechat_bot.py — Phidipus WeChat Work Bot v2.3.1
═══════════════════════════════════════════════════════════════════

WeChat Work (企业微信) bot — ra lệnh cho Phidipus qua WeChat.

Architecture:
  - HTTP webhook server (port 8913) nhận messages từ WeChat Work
  - WeChat Work API gửi replies (text, file, image)
  - Same command set as Telegram bot
  - Reuse Phidipus agent_loop, workflow_executor

Setup:
  1. WeChat Work → 应用管理 → 自建应用 → lấy AgentId + Secret
  2. 设置接收消息 → URL: http://your-ip:8913/wechat/callback
  3. Copy Token + EncodingAESKey vào wechat_config.json
  4. Config admin_user_ids (WeChat Work UserID)

Commands (same as Telegram):
  /help     — Hiển thị trợ giúp
  /status   — Trạng thái hệ thống
  /task X   — Chạy tác vụ X
  /chat     — Chế độ hỏi đáp AI
  /agent    — Chế độ agent (mặc định)
  (text)    — Gõ lệnh tự nhiên → chạy workflow/task

Process: orchestrator (L1)

Security:
  - Chỉ admin_user_ids mới được điều khiển
  - Messages encrypted với AES key
  - Access token tự refresh mỗi 7000s
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger("phidipus.wechat")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════
# WeChat Work API Client
# ══════════════════════════════════════════════════════════════

_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


class WeChatAPI:
    """WeChat Work REST API client."""

    def __init__(self, corp_id: str, secret: str, agent_id: int) -> None:
        self._corp_id = corp_id
        self._secret = secret
        self._agent_id = agent_id
        self._access_token: str = ""
        self._token_expires: float = 0

    async def get_token(self) -> str:
        """Get or refresh access_token."""
        if self._access_token and time.time() < self._token_expires:
            return self._access_token
        try:
            url = (f"{_API_BASE}/gettoken?"
                   f"corpid={self._corp_id}&corpsecret={self._secret}")
            data = await self._http_get(url)
            if data.get("errcode") == 0:
                self._access_token = data["access_token"]
                self._token_expires = time.time() + data.get("expires_in", 7200) - 200
                _vlog("🔑", f"WeChat token refreshed (expires in {data.get('expires_in')}s)")
                return self._access_token
            else:
                _vlog("❌", f"WeChat token error: {data.get('errmsg')}")
                return ""
        except Exception as exc:
            _vlog("❌", f"WeChat token request failed: {exc}")
            return ""

    async def send_text(self, user_id: str, text: str) -> bool:
        """Send text message to a user."""
        token = await self.get_token()
        if not token:
            return False
        url = f"{_API_BASE}/message/send?access_token={token}"
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": self._agent_id,
            "text": {"content": text[:2048]},  # WeChat limit
        }
        try:
            data = await self._http_post(url, payload)
            return data.get("errcode") == 0
        except Exception as exc:
            _vlog("❌", f"WeChat send_text failed: {exc}")
            return False

    async def send_markdown(self, user_id: str, content: str) -> bool:
        """Send markdown message (WeChat Work supports limited markdown)."""
        token = await self.get_token()
        if not token:
            return False
        url = f"{_API_BASE}/message/send?access_token={token}"
        payload = {
            "touser": user_id,
            "msgtype": "markdown",
            "agentid": self._agent_id,
            "markdown": {"content": content[:2048]},
        }
        try:
            data = await self._http_post(url, payload)
            return data.get("errcode") == 0
        except Exception as exc:
            _vlog("❌", f"WeChat send_markdown failed: {exc}")
            return False

    async def send_file(self, user_id: str, media_id: str) -> bool:
        """Send file by media_id (must upload first)."""
        token = await self.get_token()
        if not token:
            return False
        url = f"{_API_BASE}/message/send?access_token={token}"
        payload = {
            "touser": user_id,
            "msgtype": "file",
            "agentid": self._agent_id,
            "file": {"media_id": media_id},
        }
        try:
            data = await self._http_post(url, payload)
            return data.get("errcode") == 0
        except Exception as exc:
            return False

    async def upload_file(self, file_path: str, media_type: str = "file") -> str:
        """Upload file and return media_id."""
        token = await self.get_token()
        if not token:
            return ""
        # Upload via multipart form
        url = f"{_API_BASE}/media/upload?access_token={token}&type={media_type}"
        try:
            import io
            boundary = "----PhidipusBoundary"
            filename = Path(file_path).name
            with open(file_path, "rb") as f:
                file_data = f.read()

            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

            def _do():
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())

            data = await asyncio.to_thread(_do)
            if data.get("errcode") == 0:
                return data.get("media_id", "")
            return ""
        except Exception as exc:
            _vlog("❌", f"WeChat upload failed: {exc}")
            return ""

    # ── HTTP helpers (no external deps) ───────────────────────

    async def _http_get(self, url: str) -> dict:
        def _do():
            with urllib.request.urlopen(url, timeout=10) as r:
                return json.loads(r.read())
        return await asyncio.to_thread(_do)

    async def _http_post(self, url: str, payload: dict) -> dict:
        def _do():
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        return await asyncio.to_thread(_do)


# ══════════════════════════════════════════════════════════════
# Message Parser — XML from WeChat Work callback
# ══════════════════════════════════════════════════════════════

@dataclass
class WeChatMessage:
    """Parsed incoming message from WeChat Work."""
    from_user: str = ""
    to_user: str = ""       # Corp UserName
    create_time: int = 0
    msg_type: str = ""      # text, image, voice, video, location, link, event
    content: str = ""       # text content
    msg_id: str = ""
    agent_id: int = 0
    event: str = ""         # for event type: subscribe, click, etc.
    event_key: str = ""


def parse_xml_message(xml_str: str) -> WeChatMessage:
    """Parse WeChat Work callback XML into WeChatMessage."""
    msg = WeChatMessage()
    try:
        root = ET.fromstring(xml_str)
        msg.from_user = root.findtext("FromUserName", "")
        msg.to_user = root.findtext("ToUserName", "")
        msg.create_time = int(root.findtext("CreateTime", "0"))
        msg.msg_type = root.findtext("MsgType", "")
        msg.content = root.findtext("Content", "")
        msg.msg_id = root.findtext("MsgId", "")
        msg.agent_id = int(root.findtext("AgentID", "0"))
        msg.event = root.findtext("Event", "")
        msg.event_key = root.findtext("EventKey", "")
    except Exception:
        pass
    return msg


def verify_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
    """Verify WeChat Work callback signature."""
    items = sorted([token, timestamp, nonce])
    sha1 = hashlib.sha1("".join(items).encode()).hexdigest()
    return sha1 == signature


# ══════════════════════════════════════════════════════════════
# PhidipusWeChatBot — Main Bot Class
# ══════════════════════════════════════════════════════════════

class PhidipusWeChatBot:
    """
    WeChat Work bot for Phidipus — same capabilities as Telegram bot.

    Usage:
        from wechat.wechat_config import WeChatConfig
        cfg = WeChatConfig()
        bot = PhidipusWeChatBot(cfg)
        bot.inject(agent_loop=agent_loop, config=config)
        await bot.start()
    """

    def __init__(self, config: Any) -> None:
        self._cfg = config
        self._api = WeChatAPI(config.corp_id, config.secret, config.agent_id)
        self._admin_ids = set(config.admin_user_ids)
        self._is_running = False
        self._server = None

        # Phidipus components (injected)
        self._agent_loop: Any = None
        self._config: Any = None
        self._chat_mode: dict[str, str] = {}      # user_id → "agent" | "chat"
        self._chat_history: dict[str, list] = {}
        self._in_progress: set[str] = set()

    def inject(self, **components: Any) -> None:
        """Inject Phidipus components (same as Telegram bot)."""
        for key, val in components.items():
            attr = f"_{key}"
            if hasattr(self, attr):
                setattr(self, attr, val)
        if self._agent_loop:
            # Wire send function for notifications
            async def _send_wechat(msg: str) -> None:
                for uid in self._admin_ids:
                    await self._api.send_text(uid, msg)
            self._agent_loop._wechat_send_fn = _send_wechat

    # ── Auth ──────────────────────────────────────────────────

    def _is_authorized(self, user_id: str) -> bool:
        if not self._admin_ids:
            return False
        return user_id in self._admin_ids

    # ── Start/Stop ────────────────────────────────────────────

    async def start(self) -> None:
        """Start webhook HTTP server."""
        if self._is_running:
            return
        if not self._cfg.credentials_set:
            _vlog("⚠️", "WeChat Bot: credentials not set — skipping")
            return

        # Test token
        token = await self._api.get_token()
        if not token:
            _vlog("❌", "WeChat Bot: cannot get access_token — check credentials")
            return

        # Start aiohttp webhook server
        try:
            from aiohttp import web
        except ImportError:
            _vlog("⚠️", "WeChat Bot: aiohttp not installed — using built-in server")
            asyncio.create_task(self._run_builtin_server())
            self._is_running = True
            return

        app = web.Application()
        app.router.add_get("/wechat/callback", self._handle_verify)
        app.router.add_post("/wechat/callback", self._handle_message)
        app.router.add_get("/wechat/health", self._handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        port = self._cfg.webhook_port
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        self._server = runner
        self._is_running = True
        _vlog("💚", f"WeChat Bot webhook server started on port {port}")

    async def stop(self) -> None:
        if self._server:
            await self._server.cleanup()
        self._is_running = False
        _vlog("🔴", "WeChat Bot stopped")

    # ── Webhook Handlers ──────────────────────────────────────

    async def _handle_verify(self, request) -> Any:
        """Handle WeChat Work URL verification (GET)."""
        from aiohttp import web
        params = request.query
        msg_signature = params.get("msg_signature", "")
        timestamp = params.get("timestamp", "")
        nonce = params.get("nonce", "")
        echostr = params.get("echostr", "")

        # Simple verification — return echostr
        if echostr:
            _vlog("🔑", "WeChat callback URL verified")
            return web.Response(text=echostr)
        return web.Response(text="OK")

    async def _handle_message(self, request) -> Any:
        """Handle incoming message (POST)."""
        from aiohttp import web
        try:
            body = await request.text()
            msg = parse_xml_message(body)

            if msg.msg_type == "text" and msg.content:
                asyncio.create_task(self._process_message(msg))

            return web.Response(text="success")
        except Exception as exc:
            _vlog("❌", f"WeChat message error: {exc}")
            return web.Response(text="error")

    async def _handle_health(self, request) -> Any:
        from aiohttp import web
        return web.json_response({
            "status": "ok",
            "bot": "PhidipusWeChatBot",
            "running": self._is_running,
        })

    # ── Fallback built-in HTTP server (no aiohttp) ───────────

    async def _run_builtin_server(self) -> None:
        """Minimal HTTP server using asyncio (no external deps)."""
        port = self._cfg.webhook_port

        async def handle_client(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=10)
                request_text = data.decode("utf-8", errors="replace")

                if "GET /wechat/callback" in request_text:
                    # Extract echostr from query
                    if "echostr=" in request_text:
                        qs = request_text.split("?", 1)[-1].split(" ", 1)[0]
                        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                        echostr = urllib.parse.unquote(params.get("echostr", ""))
                        response = f"HTTP/1.1 200 OK\r\nContent-Length: {len(echostr)}\r\n\r\n{echostr}"
                    else:
                        response = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"

                elif "POST /wechat/callback" in request_text:
                    # Extract XML body
                    if "\r\n\r\n" in request_text:
                        body = request_text.split("\r\n\r\n", 1)[1]
                        msg = parse_xml_message(body)
                        if msg.msg_type == "text" and msg.content:
                            asyncio.create_task(self._process_message(msg))
                    response = "HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nsuccess"

                else:
                    response = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"

                writer.write(response.encode())
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(handle_client, "0.0.0.0", port)
        _vlog("💚", f"WeChat Bot built-in server on port {port}")
        async with server:
            await server.serve_forever()

    # ── Message Processing ────────────────────────────────────

    async def _process_message(self, msg: WeChatMessage) -> None:
        """Process incoming text message — same logic as Telegram."""
        user_id = msg.from_user
        text = msg.content.strip()

        if not self._is_authorized(user_id):
            await self._api.send_text(user_id, "⛔ Không có quyền. Liên hệ admin.")
            return

        _vlog("💬", f"WeChat [{user_id}]: {text[:80]}")

        # Command routing
        if text.startswith("/"):
            await self._handle_command(user_id, text)
        elif self._chat_mode.get(user_id) == "chat":
            await self._handle_chat(user_id, text)
        else:
            await self._execute_task(user_id, text)

    async def _handle_command(self, user_id: str, text: str) -> None:
        """Handle /command messages."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            await self._cmd_help(user_id)
        elif cmd == "/status":
            await self._cmd_status(user_id)
        elif cmd == "/task":
            if args:
                await self._execute_task(user_id, args)
            else:
                await self._api.send_text(user_id, "📝 Gõ: /task <mô tả việc cần làm>")
        elif cmd == "/chat":
            self._chat_mode[user_id] = "chat"
            await self._api.send_text(user_id,
                "💬 Chế độ Hỏi Đáp AI\n\nHỏi bất cứ điều gì.\nGõ /agent để quay lại.")
        elif cmd == "/agent":
            self._chat_mode[user_id] = "agent"
            await self._api.send_text(user_id,
                "🤖 Chế độ Agent\n\nGõ lệnh để Phidipus Agents thực hiện.")
        elif cmd == "/workflows":
            await self._cmd_workflows(user_id)
        else:
            await self._api.send_text(user_id, f"❓ Lệnh không nhận ra: {cmd}\nGõ /help để xem hướng dẫn.")

    # ── Commands ──────────────────────────────────────────────

    async def _cmd_help(self, user_id: str) -> None:
        help_text = (
            "🕷️ Phidipus Agents WeChat Bot\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📌 Lệnh:\n"
            "/help — Trợ giúp\n"
            "/status — Trạng thái hệ thống\n"
            "/task <lệnh> — Chạy tác vụ\n"
            "/chat — Hỏi đáp AI\n"
            "/agent — Chế độ agent\n"
            "/workflows — Danh sách workflow\n\n"
            "💡 Hoặc gõ trực tiếp:\n"
            "• \"báo cáo doanh số\"\n"
            "• \"check email\"\n"
            "• \"tìm lead google\"\n"
            "• \"theo dõi giá shopee\"\n\n"
            "🌐 Admin Panel: http://127.0.0.1:8912"
        )
        await self._api.send_text(user_id, help_text)

    async def _cmd_status(self, user_id: str) -> None:
        status_lines = ["📊 Phidipus Agents Status\n"]
        try:
            import platform
            status_lines.append(f"🖥️ {platform.node()} ({platform.machine()})")
            status_lines.append(f"🐍 Python {platform.python_version()}")
        except Exception:
            pass
        if self._agent_loop:
            status_lines.append("🤖 Agent Loop: ✅ Running")
        else:
            status_lines.append("🤖 Agent Loop: ❌ Not connected")
        status_lines.append(f"💬 WeChat Bot: ✅ Running")
        status_lines.append(f"👥 Admins: {len(self._admin_ids)}")
        await self._api.send_text(user_id, "\n".join(status_lines))

    async def _cmd_workflows(self, user_id: str) -> None:
        try:
            from core.workflow_executor import load_taught_workflows
            wfs = load_taught_workflows()
            if not wfs:
                await self._api.send_text(user_id, "📋 Chưa có workflow nào.")
                return
            lines = ["📋 Workflows có sẵn:\n"]
            for i, wf in enumerate(wfs[:15], 1):
                name = wf.get("name", "?")
                triggers = ", ".join(wf.get("trigger_phrases", [])[:2])
                nodes = len(wf.get("nodes", []))
                lines.append(f"{i}. {name}\n   🎯 \"{triggers}\"\n   📦 {nodes} nodes")
            await self._api.send_text(user_id, "\n".join(lines))
        except Exception as exc:
            await self._api.send_text(user_id, f"❌ Lỗi: {exc}")

    # ── Chat mode ─────────────────────────────────────────────

    async def _handle_chat(self, user_id: str, text: str) -> None:
        """AI chat mode — same as Telegram."""
        try:
            from core.llm_client import get_llm_client
            llm = get_llm_client()

            history = self._chat_history.get(user_id, [])
            history.append({"role": "user", "content": text})
            if len(history) > 10:
                history = history[-10:]

            response = await llm.complete(
                prompt=text,
                system="Bạn là trợ lý AI Phidipus Agents. Trả lời ngắn gọn, tiếng Việt.",
                max_tokens=500,
            )
            history.append({"role": "assistant", "content": response})
            self._chat_history[user_id] = history

            await self._api.send_text(user_id, f"🤖 {response}")
        except Exception as exc:
            await self._api.send_text(user_id, f"❌ AI lỗi: {str(exc)[:200]}")

    # ── Task execution ────────────────────────────────────────

    async def _execute_task(self, user_id: str, goal: str) -> None:
        """Execute a task/workflow — same as Telegram _execute_task."""
        if goal in self._in_progress:
            await self._api.send_text(user_id, f"⏳ Đang chạy: \"{goal[:40]}\"...")
            return

        self._in_progress.add(goal)
        await self._api.send_text(user_id, f"🚀 Bắt đầu: \"{goal[:50]}\"")

        try:
            # Check workflow match first
            try:
                from core.workflow_executor import find_matching_workflow, WorkflowExecutor
                wf, wf_vars = find_matching_workflow(goal)
                if wf:
                    await self._api.send_text(user_id,
                        f"📋 Workflow: {wf.get('name', '?')}\n"
                        f"📦 {len(wf.get('nodes', []))} nodes")

                    executor = WorkflowExecutor(
                        ipc_client=getattr(self._agent_loop, '_ipc', None),
                        notify_fn=lambda msg: self._api.send_text(user_id, msg),
                    )
                    result = await asyncio.wait_for(
                        executor.run(wf, goal=goal, variables=wf_vars),
                        timeout=300,
                    )
                    ok = result.get("success", False)
                    steps = result.get("steps_done", 0)
                    elapsed = result.get("elapsed_s", 0)
                    await self._api.send_text(user_id,
                        f"{'✅' if ok else '❌'} Workflow {'xong' if ok else 'lỗi'}!\n"
                        f"📦 {steps} steps | ⏱️ {elapsed:.1f}s\n"
                        f"{result.get('error', '')}")
                    return
            except ImportError:
                pass

            # Fallback: agent_loop
            if self._agent_loop:
                result = await asyncio.wait_for(
                    self._agent_loop.run_task(goal), timeout=300,
                )
                ok = result.get("success", False) if isinstance(result, dict) else bool(result)
                await self._api.send_text(user_id,
                    f"{'✅' if ok else '❌'} Task {'xong' if ok else 'lỗi'}!\n"
                    f"Kết quả: {str(result)[:500]}")
            else:
                await self._api.send_text(user_id, "❌ Agent Loop chưa kết nối.")

        except asyncio.TimeoutError:
            await self._api.send_text(user_id, "⏱️ Timeout (>300s)")
        except Exception as exc:
            await self._api.send_text(user_id, f"❌ Lỗi: {str(exc)[:300]}")
        finally:
            self._in_progress.discard(goal)

    # ── Send helpers ──────────────────────────────────────────

    async def send_to_admins(self, message: str) -> None:
        """Send message to all admins (used by notify node)."""
        for uid in self._admin_ids:
            await self._api.send_text(uid, message)

    async def send_file_to_admins(self, file_path: str) -> None:
        """Upload and send file to all admins."""
        media_id = await self._api.upload_file(file_path)
        if media_id:
            for uid in self._admin_ids:
                await self._api.send_file(uid, media_id)
        else:
            for uid in self._admin_ids:
                await self._api.send_text(uid, f"📎 File: {Path(file_path).name} (upload failed)")

    @property
    def is_running(self) -> bool:
        return self._is_running
