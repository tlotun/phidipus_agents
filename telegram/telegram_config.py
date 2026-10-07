# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
telegram/telegram_config.py — Phidipus Telegram Bot Configuration Manager
═════════════════════════════════════════════════════════════════════════

Manages Telegram bot token and admin user IDs in a local JSON file.
Allows adding/editing/removing tokens and admin IDs from Admin Panel
without restarting or editing environment variables.

Config file: telegram/bot_config.json

Usage:
    cfg = TelegramConfig()
    cfg.set_token("123456:ABC-DEF...")
    cfg.add_admin(987654321)
    cfg.save()

    # Later:
    token = cfg.token
    admin_ids = cfg.admin_ids
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DEFAULT_PATH = Path(__file__).parent / "bot_config.json"


class TelegramConfig:
    """
    Manages Telegram bot configuration (token + admin IDs).

    Stored in a local JSON file alongside the bot code.
    Token is masked when returned via API (only last 8 chars shown).

    Args:
        config_path: Path to config JSON file.
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._path = Path(config_path) if config_path else _DEFAULT_PATH
        self._token: str = ""
        self._admin_ids: list[int] = []
        self._bot_username: str = ""
        self._bot_name: str = ""
        self._created_at: str = ""
        self._updated_at: str = ""
        self._auto_start: bool = False
        self._notes: str = ""
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text("utf-8"))
                self._token = data.get("token", "")
                self._admin_ids = list(data.get("admin_ids", []))
                self._bot_username = data.get("bot_username", "")
                self._bot_name = data.get("bot_name", "")
                self._created_at = data.get("created_at", "")
                self._updated_at = data.get("updated_at", "")
                self._auto_start = data.get("auto_start", False)
                self._notes = data.get("notes", "")
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        # Also check environment variables as fallback
        if not self._token:
            self._token = os.environ.get("PHIDIPUS_TG_TOKEN", "")
        if not self._admin_ids:
            env_admin = os.environ.get("PHIDIPUS_TG_ADMIN", "")
            if env_admin:
                self._admin_ids = [
                    int(x.strip()) for x in env_admin.split(",")
                    if x.strip().isdigit()
                ]

    def save(self) -> None:
        """Save config to JSON file."""
        self._updated_at = _utc_iso()
        if not self._created_at:
            self._created_at = self._updated_at
        data = {
            "version": "9.18",
            "token": self._token,
            "admin_ids": self._admin_ids,
            "bot_username": self._bot_username,
            "bot_name": self._bot_name,
            "auto_start": self._auto_start,
            "notes": self._notes,
            "created_at": self._created_at,
            "updated_at": self._updated_at,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Token ─────────────────────────────────────────────────

    @property
    def token(self) -> str:
        return self._token

    @property
    def token_masked(self) -> str:
        """Return token with middle portion masked."""
        if not self._token:
            return ""
        if len(self._token) <= 12:
            return "***"
        return self._token[:4] + "•" * (len(self._token) - 12) + self._token[-8:]

    @property
    def token_set(self) -> bool:
        return bool(self._token and ":" in self._token)

    def set_token(self, token: str) -> None:
        """Set the bot token. Validates basic format (contains ':')."""
        token = token.strip()
        if token and ":" not in token:
            raise ValueError("Token không hợp lệ — phải có dấu ':' (ví dụ: 123456:ABC-DEF...)")
        self._token = token
        self.save()

    def clear_token(self) -> None:
        self._token = ""
        self.save()

    # ── Admin IDs ─────────────────────────────────────────────

    @property
    def admin_ids(self) -> list[int]:
        return list(self._admin_ids)

    @property
    def admin_count(self) -> int:
        return len(self._admin_ids)

    def add_admin(self, user_id: int) -> bool:
        """Add an admin user ID. Returns False if already exists."""
        if user_id in self._admin_ids:
            return False
        self._admin_ids.append(user_id)
        self.save()
        return True

    def remove_admin(self, user_id: int) -> bool:
        """Remove an admin user ID. Returns False if not found."""
        if user_id not in self._admin_ids:
            return False
        self._admin_ids.remove(user_id)
        self.save()
        return True

    def set_admins(self, user_ids: list[int]) -> None:
        """Replace entire admin list."""
        self._admin_ids = list(set(user_ids))
        self.save()

    # ── Bot info ──────────────────────────────────────────────

    @property
    def bot_username(self) -> str:
        return self._bot_username

    @property
    def bot_name(self) -> str:
        return self._bot_name

    def set_bot_info(self, username: str = "", name: str = "") -> None:
        if username:
            self._bot_username = username.lstrip("@")
        if name:
            self._bot_name = name
        self.save()

    # ── Settings ──────────────────────────────────────────────

    @property
    def auto_start(self) -> bool:
        return self._auto_start

    def set_auto_start(self, enabled: bool) -> None:
        self._auto_start = enabled
        self.save()

    @property
    def notes(self) -> str:
        return self._notes

    def set_notes(self, text: str) -> None:
        self._notes = text[:500]
        self.save()

    # ── Export ─────────────────────────────────────────────────

    def to_dict(self, *, mask_token: bool = True) -> dict:
        """Export config as dict (token masked by default)."""
        return {
            "token": self.token_masked if mask_token else self._token,
            "token_set": self.token_set,
            "admin_ids": self._admin_ids,
            "admin_count": self.admin_count,
            "bot_username": self._bot_username,
            "bot_name": self._bot_name,
            "auto_start": self._auto_start,
            "notes": self._notes,
            "config_path": str(self._path),
            "created_at": self._created_at,
            "updated_at": self._updated_at,
        }

    def validate(self) -> dict:
        """Check config readiness and return issues."""
        issues = []
        if not self.token_set:
            issues.append("Token chưa được thiết lập")
        if not self._admin_ids:
            issues.append("Chưa có admin ID nào")
        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "ready": self.token_set and len(self._admin_ids) > 0,
        }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
