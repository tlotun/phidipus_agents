# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
social/telegram_content_handler.py — Phidipus Content Pipeline ↔ Telegram Integration v1.0
═══════════════════════════════════════════════════════════════════════════════════════════

Mixin class tích hợp Content Pipeline vào PhidipusBot.

CÁCH TÍCH HỢP vào telegram_bot.py:
═══════════════════════════════════

1. Import module này trong telegram_bot.py:
   from social.telegram_content_handler import ContentPipelineMixin

2. Thêm ContentPipelineMixin vào class PhidipusBot:
   class PhidipusBot(ContentPipelineMixin):
       ...

3. Gọi self._init_content_pipeline() trong __init__()

4. Trong _on_text_message(), thêm check pipeline TRƯỚC auto-detect:
   # Check content pipeline first
   if await self._handle_content_pipeline_response(update, text):
       return

5. Trong _register_handlers(), thêm:
   app.add_handler(CommandHandler("post", self._cmd_post))
   app.add_handler(CommandHandler("cancel", self._cmd_cancel_pipeline))

6. Trong boot_full() (main.py), inject content pipeline:
   bot.inject(content_pipeline=pipeline)

HOẶC: Copy các methods bên dưới trực tiếp vào class PhidipusBot.

═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

# These will be available when imported inside telegram_bot.py context
# from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
# from telegram.ext import ContextTypes
# from telegram.constants import ParseMode


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


class ContentPipelineMixin:
    """
    Mixin class cung cấp Content Pipeline functionality cho PhidipusBot.

    Attributes cần có trên PhidipusBot:
      self._app           — Telegram Application instance
      self._admin_ids     — Set of authorized user IDs
      self._content_pipeline  — ContentPipeline instance (injected)
    """

    # ══════════════════════════════════════════════════════════════
    # Initialization
    # ══════════════════════════════════════════════════════════════

    def _init_content_pipeline(self) -> None:
        """
        Khởi tạo Content Pipeline. Gọi trong PhidipusBot.__init__().
        """
        self._content_pipeline: Any = None

        try:
            from social.content_pipeline import ContentPipeline
            from social.chatgpt_image_gen import ChatGPTImageGenerator
            from social.social_poster import SocialPoster

            pipeline = ContentPipeline()
            self._content_pipeline = pipeline
            _vlog("📱", "Content Pipeline — initialized")

        except ImportError as exc:
            _vlog("⚠", f"Content Pipeline import failed: {exc}")
        except Exception as exc:
            _vlog("⚠", f"Content Pipeline init failed: {exc}")

    def _wire_content_pipeline(self) -> None:
        """
        Wire Content Pipeline với Telegram send functions.
        Gọi SAU khi bot đã start (self._app available).
        """
        if not self._content_pipeline:
            return

        pipeline = self._content_pipeline
        app = getattr(self, '_app', None)
        if not app:
            return

        # Inject Telegram send functions
        async def send_message(chat_id: int, text: str) -> None:
            try:
                from telegram.constants import ParseMode
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception as exc:
                # Retry without markdown
                try:
                    plain = text.replace("\\", "").replace("*", "").replace("_", "")
                    await app.bot.send_message(chat_id=chat_id, text=plain)
                except Exception:
                    _vlog("⚠", f"Send message failed: {exc}")

        async def send_photo(chat_id: int, photo_path: str, caption: str = "") -> None:
            try:
                from telegram.constants import ParseMode
                with open(photo_path, "rb") as f:
                    await app.bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=caption[:1024],  # Telegram caption limit
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
            except Exception as exc:
                # Retry as document
                try:
                    with open(photo_path, "rb") as f:
                        await app.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            caption=caption[:1024] if caption else os.path.basename(photo_path),
                        )
                except Exception:
                    _vlog("⚠", f"Send photo failed: {exc}")

        async def send_document(chat_id: int, doc_path: str, caption: str = "") -> None:
            try:
                with open(doc_path, "rb") as f:
                    await app.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        caption=caption or os.path.basename(doc_path),
                    )
            except Exception as exc:
                _vlog("⚠", f"Send document failed: {exc}")

        pipeline.inject(
            send_message=send_message,
            send_photo=send_photo,
            send_document=send_document,
        )

        # Inject IPC client if available
        ipc_client = getattr(self, '_ipc_client', None)
        if ipc_client:
            from social.chatgpt_image_gen import ChatGPTImageGenerator
            from social.social_poster import SocialPoster

            image_gen = ChatGPTImageGenerator(ipc_client=ipc_client)
            poster = SocialPoster(ipc_client=ipc_client)

            pipeline.inject(
                image_gen=image_gen,
                social_poster=poster,
                ipc_client=ipc_client,
            )

        # Inject LLM fallback if available
        llm_fallback = getattr(self, '_llm_fallback', None)
        if llm_fallback:
            pipeline.inject(llm_fallback=llm_fallback)

        _vlog("📱", "Content Pipeline — wired to Telegram")

    # ══════════════════════════════════════════════════════════════
    # Command: /post
    # ══════════════════════════════════════════════════════════════

    async def _cmd_post(self, update: Any, ctx: Any) -> None:
        """
        /post <lệnh> — Bắt đầu Content Pipeline.

        Ví dụ:
          /post tạo ảnh con ong robot theo phong cách cyberpunk
                sau đó tạo content về AI rồi post lên facebook

          /post tạo ảnh sunset style watercolor, viết content
                về thiên nhiên, đăng lên instagram
        """
        from telegram.constants import ParseMode

        # Auth check
        if not self._is_authorized(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Không có quyền.")
            return

        if not self._content_pipeline:
            await update.effective_message.reply_text(
                "⚠️ Content Pipeline chưa được khởi tạo\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Get command text
        command = " ".join(ctx.args) if ctx.args else ""

        if not command:
            # Show help
            await update.effective_message.reply_text(
                "📱 *Content Pipeline*\n\n"
                "*Cách dùng:*\n"
                "/post tạo ảnh \\<chủ đề\\> theo phong cách \\<style\\> "
                "sau đó tạo content về \\<chủ đề\\> rồi post lên \\<mạng xã hội\\>\n\n"
                "*Ví dụ:*\n"
                "• `/post tạo ảnh con ong robot theo phong cách cyberpunk "
                "sau đó tạo content về AI rồi post lên facebook`\n\n"
                "• `/post tạo ảnh sunset style watercolor, "
                "viết content về thiên nhiên, đăng lên instagram`\n\n"
                "• `/post tạo ảnh sản phẩm LED theo phong cách minimalist "
                "rồi tạo content về đèn LED và post lên x`\n\n"
                "*Trong quá trình chạy:*\n"
                "  ✅ *OK* — Duyệt \\(ảnh/content\\)\n"
                "  📝 Nhắn bất kỳ — Yêu cầu tạo lại\n"
                "  /cancel — Huỷ pipeline\n\n"
                "*Hỗ trợ:* Facebook, Instagram, X",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Wire pipeline nếu chưa
        self._wire_content_pipeline()

        # Check if already running
        uid = update.effective_user.id
        if self._content_pipeline.has_active_session(uid):
            await update.effective_message.reply_text(
                "⚠️ Bạn đang có 1 pipeline đang chạy\\.\n"
                "Gõ /cancel để huỷ trước khi tạo mới\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Start pipeline
        session = await self._content_pipeline.start_pipeline(
            user_id=uid,
            chat_id=update.effective_chat.id,
            command=command,
        )

        if not session:
            await update.effective_message.reply_text(
                "❌ Không hiểu lệnh\\. Hãy bao gồm:\n"
                "  • *tạo ảnh* \\<chủ đề\\>\n"
                "  • *theo phong cách* \\<style\\>\n"
                "  • *tạo content* về \\<chủ đề\\>\n"
                "  • *post lên* facebook/instagram/x\n\n"
                "Ví dụ: `/post tạo ảnh con mèo theo phong cách anime "
                "sau đó tạo content về thú cưng rồi post lên facebook`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    async def _cmd_cancel_pipeline(self, update: Any, ctx: Any) -> None:
        """/cancel — Huỷ Content Pipeline đang chạy."""
        from telegram.constants import ParseMode

        if not self._is_authorized(update.effective_user.id):
            return

        if self._content_pipeline:
            cancelled = await self._content_pipeline.cancel(update.effective_user.id)
            if cancelled:
                return  # Pipeline sẽ tự gửi thông báo huỷ

        await update.effective_message.reply_text(
            "ℹ️ Không có pipeline nào đang chạy\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    # ══════════════════════════════════════════════════════════════
    # Message handler integration
    # ══════════════════════════════════════════════════════════════

    async def _handle_content_pipeline_response(
        self, update: Any, text: str
    ) -> bool:
        """
        Xử lý phản hồi từ user cho Content Pipeline.

        Gọi trong _on_text_message() TRƯỚC auto-detect goal.
        Returns True nếu pipeline đã xử lý message.
        """
        if not self._content_pipeline:
            return False

        uid = update.effective_user.id

        if not self._content_pipeline.has_active_session(uid):
            return False

        # Wire nếu chưa
        self._wire_content_pipeline()

        # Delegate to pipeline
        handled = await self._content_pipeline.handle_user_response(
            user_id=uid,
            text=text,
        )

        return handled

    # ══════════════════════════════════════════════════════════════
    # Auto-detect content creation commands
    # ══════════════════════════════════════════════════════════════

    def _is_content_pipeline_command(self, text: str) -> bool:
        """
        Detect if text is a content creation command.

        Patterns:
          "tạo ảnh ... post lên ..."
          "create image ... post to ..."
          "vẽ ảnh ... đăng lên ..."
        """
        text_lower = text.lower()

        has_image = any(kw in text_lower for kw in [
            "tạo ảnh", "vẽ ảnh", "sinh ảnh", "tạo hình",
            "create image", "generate image", "make image",
        ])

        has_post = any(kw in text_lower for kw in [
            "post lên", "đăng lên", "share to", "post to",
            "chia sẻ lên", "đăng bài",
        ])

        return has_image and has_post


# ══════════════════════════════════════════════════════════════════════
# HƯỚNG DẪN TÍCH HỢP CHI TIẾT
# ══════════════════════════════════════════════════════════════════════
#
# ┌────────────────────────────────────────────────────────────────────┐
# │ FILE: telegram/telegram_bot.py                                     │
# ├────────────────────────────────────────────────────────────────────┤
# │                                                                    │
# │ 1. IMPORTS — Thêm ở đầu file (sau các import hiện có):           │
# │                                                                    │
# │    from social.telegram_content_handler import ContentPipelineMixin│
# │                                                                    │
# │ 2. CLASS DEFINITION — Thêm mixin:                                 │
# │                                                                    │
# │    class PhidipusBot(ContentPipelineMixin):                        │
# │        # ... existing code ...                                     │
# │                                                                    │
# │ 3. __init__() — Thêm ở cuối __init__:                            │
# │                                                                    │
# │    self._init_content_pipeline()                                   │
# │                                                                    │
# │ 4. _register_handlers() — Thêm 2 dòng:                           │
# │                                                                    │
# │    app.add_handler(CommandHandler("post", self._cmd_post))         │
# │    app.add_handler(CommandHandler("cancel", self._cmd_cancel_pipeline))│
# │                                                                    │
# │ 5. _on_text_message() — Thêm ngay SAU auth check:                │
# │                                                                    │
# │    # ── Content Pipeline check ──                                  │
# │    if self._content_pipeline and \                                 │
# │       await self._handle_content_pipeline_response(update, text):  │
# │        return                                                      │
# │                                                                    │
# │    # ── Auto-detect content pipeline command ──                    │
# │    if self._content_pipeline and \                                 │
# │       self._is_content_pipeline_command(text):                     │
# │        self._wire_content_pipeline()                               │
# │        session = await self._content_pipeline.start_pipeline(      │
# │            user_id=uid, chat_id=update.effective_chat.id,          │
# │            command=text,                                           │
# │        )                                                          │
# │        if session:                                                 │
# │            return                                                  │
# │                                                                    │
# │ 6. _set_commands() — Thêm vào danh sách commands:                │
# │                                                                    │
# │    BotCommand("post", "📱 Tạo & đăng content MXH"),              │
# │    BotCommand("cancel", "⏹ Huỷ pipeline đang chạy"),            │
# │                                                                    │
# │ 7. start() — Thêm SAU self._register_handlers():                 │
# │                                                                    │
# │    self._wire_content_pipeline()                                   │
# │                                                                    │
# ├────────────────────────────────────────────────────────────────────┤
# │ FILE: main.py (boot_full function)                                 │
# ├────────────────────────────────────────────────────────────────────┤
# │                                                                    │
# │ Sau phần bot.inject() hiện có, thêm:                              │
# │                                                                    │
# │    # ── Content Pipeline injection ──                              │
# │    if hasattr(bot, '_content_pipeline') and bot._content_pipeline: │
# │        from ipc.ipc_client import IPCClient                        │
# │        _ipc_for_content = IPCClient(cfg)                           │
# │        bot._content_pipeline.inject(                               │
# │            ipc_client=_ipc_for_content,                            │
# │        )                                                           │
# │        ok("Content Pipeline ↔ IPC — connected")                   │
# │                                                                    │
# └────────────────────────────────────────────────────────────────────┘
