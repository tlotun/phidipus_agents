# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
wechat/wechat_config.py — Phidipus WeChat Configuration Manager
═══════════════════════════════════════════════════════════════════

Manages WeChat Work (企业微信) credentials and admin user list.
Config stored in wechat/wechat_config.json.

Setup guide:
  1. Đăng ký WeChat Work: https://work.weixin.qq.com
  2. Tạo ứng dụng tự xây dựng (Self-built App)
  3. Lấy: Corp ID, Agent ID, Agent Secret
  4. Cấu hình callback URL: http://your-ip:8913/wechat/callback
  5. Paste Token + EncodingAESKey vào config

Usage:
    cfg = WeChatConfig()
    cfg.set_credentials(corp_id="ww1234", agent_id=1000002, secret="xxx")
    cfg.set_callback(token="abc", aes_key="def")
    cfg.add_admin("UserID_from_wechat_work")
    cfg.save()
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DEFAULT_PATH = Path(__file__).parent / "wechat_config.json"


class WeChatConfig:
    """
    Manages WeChat Work bot configuration.

    Credentials:
      corp_id:    企业ID (Corp ID)
      agent_id:   应用ID (Agent ID, integer)
      secret:     应用Secret
      token:      回调Token (for message encryption)
      aes_key:    回调EncodingAESKey (for message encryption)

    Admin control:
      admin_user_ids: List of WeChat Work UserIDs authorized to control agent.
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._path = Path(config_path) if config_path else _DEFAULT_PATH
        self._corp_id: str = ""
        self._agent_id: int = 0
        self._secret: str = ""
        self._token: str = ""
        self._aes_key: str = ""
        self._admin_user_ids: list[str] = []
        self._webhook_port: int = 8913
        self._enabled: bool = False
        self._bot_name: str = "Phidipus"
        self._created_at: str = ""
        self._updated_at: str = ""
        self._notes: str = ""
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self) -> None:
        if self._path.exists():
            try:
                d = json.loads(self._path.read_text("utf-8"))
                self._corp_id = d.get("corp_id", "")
                self._agent_id = int(d.get("agent_id", 0))
                self._secret = d.get("secret", "")
                self._token = d.get("token", "")
                self._aes_key = d.get("aes_key", "")
                self._admin_user_ids = list(d.get("admin_user_ids", []))
                self._webhook_port = int(d.get("webhook_port", 8913))
                self._enabled = d.get("enabled", False)
                self._bot_name = d.get("bot_name", "Phidipus")
                self._created_at = d.get("created_at", "")
                self._updated_at = d.get("updated_at", "")
                self._notes = d.get("notes", "")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass
        # Environment variable fallback
        if not self._corp_id:
            self._corp_id = os.environ.get("PHIDIPUS_WX_CORP_ID", "")
        if not self._secret:
            self._secret = os.environ.get("PHIDIPUS_WX_SECRET", "")
        if self._agent_id == 0:
            try:
                self._agent_id = int(os.environ.get("PHIDIPUS_WX_AGENT_ID", "0"))
            except ValueError:
                pass

    def save(self) -> None:
        self._updated_at = _utc_iso()
        if not self._created_at:
            self._created_at = self._updated_at
        data = {
            "version": "2.3.1",
            "corp_id": self._corp_id,
            "agent_id": self._agent_id,
            "secret": self._secret,
            "token": self._token,
            "aes_key": self._aes_key,
            "admin_user_ids": self._admin_user_ids,
            "webhook_port": self._webhook_port,
            "enabled": self._enabled,
            "bot_name": self._bot_name,
            "notes": self._notes,
            "created_at": self._created_at,
            "updated_at": self._updated_at,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    # ── Credentials ───────────────────────────────────────────

    @property
    def corp_id(self) -> str:
        return self._corp_id

    @property
    def agent_id(self) -> int:
        return self._agent_id

    @property
    def secret(self) -> str:
        return self._secret

    @property
    def secret_masked(self) -> str:
        if not self._secret:
            return ""
        if len(self._secret) <= 8:
            return "***"
        return self._secret[:4] + "•" * (len(self._secret) - 8) + self._secret[-4:]

    @property
    def token(self) -> str:
        return self._token

    @property
    def aes_key(self) -> str:
        return self._aes_key

    @property
    def credentials_set(self) -> bool:
        return bool(self._corp_id and self._secret and self._agent_id)

    def set_credentials(self, corp_id: str = "", agent_id: int = 0, secret: str = "") -> None:
        if corp_id:
            self._corp_id = corp_id.strip()
        if agent_id:
            self._agent_id = agent_id
        if secret:
            self._secret = secret.strip()
        self.save()

    def set_callback(self, token: str = "", aes_key: str = "") -> None:
        if token:
            self._token = token.strip()
        if aes_key:
            self._aes_key = aes_key.strip()
        self.save()

    # ── Admin User IDs ────────────────────────────────────────

    @property
    def admin_user_ids(self) -> list[str]:
        return list(self._admin_user_ids)

    def add_admin(self, user_id: str) -> bool:
        uid = user_id.strip()
        if uid in self._admin_user_ids:
            return False
        self._admin_user_ids.append(uid)
        self.save()
        return True

    def remove_admin(self, user_id: str) -> bool:
        uid = user_id.strip()
        if uid not in self._admin_user_ids:
            return False
        self._admin_user_ids.remove(uid)
        self.save()
        return True

    # ── Settings ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, val: bool) -> None:
        self._enabled = val
        self.save()

    @property
    def webhook_port(self) -> int:
        return self._webhook_port

    @property
    def bot_name(self) -> str:
        return self._bot_name

    # ── Export ─────────────────────────────────────────────────

    def to_dict(self, *, mask_secret: bool = True) -> dict:
        return {
            "corp_id": self._corp_id,
            "agent_id": self._agent_id,
            "secret": self.secret_masked if mask_secret else self._secret,
            "credentials_set": self.credentials_set,
            "token_set": bool(self._token),
            "aes_key_set": bool(self._aes_key),
            "admin_user_ids": self._admin_user_ids,
            "admin_count": len(self._admin_user_ids),
            "webhook_port": self._webhook_port,
            "enabled": self._enabled,
            "bot_name": self._bot_name,
            "notes": self._notes,
            "created_at": self._created_at,
            "updated_at": self._updated_at,
        }

    def validate(self) -> dict:
        issues = []
        if not self._corp_id:
            issues.append("Corp ID chưa thiết lập")
        if not self._agent_id:
            issues.append("Agent ID chưa thiết lập")
        if not self._secret:
            issues.append("Secret chưa thiết lập")
        if not self._admin_user_ids:
            issues.append("Chưa có admin user ID")
        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "ready": self.credentials_set and len(self._admin_user_ids) > 0,
        }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
