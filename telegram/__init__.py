# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
telegram — Phidipus v1.0 Telegram Bot Interface

Provides full agent control + monitoring via Telegram.
Mirrors all Admin Panel functionality.

Usage:
    from telegram.telegram_bot import PhidipusBot
    bot = PhidipusBot(token="BOT_TOKEN", admin_ids=[123456789])
    bot.inject(agent_loop=loop, config=cfg, ...)
    await bot.start()
"""
