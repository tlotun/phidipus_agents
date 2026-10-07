# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/notify_hub.py — Phidipus v4.3 notification hub
═══════════════════════════════════════════════════════

One place where messaging front-ends (Telegram, WeChat, …) register how to
deliver TEXT messages and FILES to the owner/admins.

Why
---
Before v4.3 a single attribute ``agent_loop._telegram_send_fn`` was used for
two incompatible purposes:
  * Brain previews / clarify questions / notifications sent *text*;
  * Skill Forge sent *file paths* through the same function.
main.py installed a file sender, the Telegram bot replaced it with a text
sender, so depending on timing either text notifications were dropped or
generated files arrived as plain-text paths.  Report/document nodes also
called ``telegram_bot.get_bot_instance()`` which never existed.

Usage
-----
    from core import notify_hub
    notify_hub.register_text_sender("telegram", send_text_fn)   # async fn(str)
    notify_hub.register_file_sender("telegram", send_file_fn)   # async fn(path, caption)

    await notify_hub.send_text("✅ Done")
    await notify_hub.send_file("/path/report.html", caption="Báo cáo")

Senders never raise into callers; failures are swallowed and reported via the
boolean return value.
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable

TextSender = Callable[[str], Awaitable[None]]
FileSender = Callable[[str, str], Awaitable[None]]

_text_senders: dict[str, TextSender] = {}
_file_senders: dict[str, FileSender] = {}


def register_text_sender(name: str, fn: TextSender | None) -> None:
    if fn is None:
        _text_senders.pop(name, None)
    else:
        _text_senders[name] = fn


def register_file_sender(name: str, fn: FileSender | None) -> None:
    if fn is None:
        _file_senders.pop(name, None)
    else:
        _file_senders[name] = fn


def has_text_sender() -> bool:
    return bool(_text_senders)


def has_file_sender() -> bool:
    return bool(_file_senders)


async def send_text(text: str) -> bool:
    """Deliver *text* through every registered channel. True if any succeeded."""
    ok = False
    for fn in list(_text_senders.values()):
        try:
            await fn(str(text))
            ok = True
        except Exception:
            continue
    return ok


async def send_file(path: str, caption: str = "") -> bool:
    """Deliver the file at *path* through every registered channel."""
    if not path or not os.path.isfile(path):
        return False
    ok = False
    for fn in list(_file_senders.values()):
        try:
            await fn(path, caption)
            ok = True
        except Exception:
            continue
    return ok


async def send_text_or_file(value: str) -> bool:
    """Compatibility shim: route an existing file path to send_file, else text."""
    if value and os.path.isfile(value):
        return await send_file(value, os.path.basename(value))
    return await send_text(value)
