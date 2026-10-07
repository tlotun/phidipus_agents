# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
social/social_manager.py — Phidipus Social Media Account Manager
═════════════════════════════════════════════════════════════════

Manages social media account connections for Phidipus's dedicated
Chrome profile.  Accounts are stored in a local JSON file with
encrypted credentials (optional).

Supported platforms:
    Facebook, Instagram, TikTok, Twitter/X, Google, Threads

Chrome profile path (macOS default):
    ~/Library/Application Support/Google/Chrome/Phidipus

Usage:
    mgr = SocialManager()
    mgr.add_account("facebook", username="user@email.com", display_name="Tên FB")
    mgr.set_connected("facebook", True)
    accounts = mgr.list_accounts()
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ══════════════════════════════════════════════════════════════════
# Platform definitions
# ══════════════════════════════════════════════════════════════════

@dataclass
class PlatformInfo:
    """Static metadata for a social platform."""
    id: str
    name: str
    icon: str
    color: str
    login_url: str
    home_url: str
    cookie_domain: str          # domain to check for login cookie
    cookie_name: str            # cookie name indicating logged-in state
    profile_url_template: str   # {username} → profile URL


PLATFORMS: dict[str, PlatformInfo] = {
    "facebook": PlatformInfo(
        id="facebook", name="Facebook", icon="📘", color="#1877F2",
        login_url="https://www.facebook.com/login",
        home_url="https://www.facebook.com",
        cookie_domain=".facebook.com",
        cookie_name="c_user",
        profile_url_template="https://www.facebook.com/{username}",
    ),
    "instagram": PlatformInfo(
        id="instagram", name="Instagram", icon="📸", color="#E4405F",
        login_url="https://www.instagram.com/accounts/login",
        home_url="https://www.instagram.com",
        cookie_domain=".instagram.com",
        cookie_name="ds_user_id",
        profile_url_template="https://www.instagram.com/{username}",
    ),
    "tiktok": PlatformInfo(
        id="tiktok", name="TikTok", icon="🎵", color="#000000",
        login_url="https://www.tiktok.com/login",
        home_url="https://www.tiktok.com",
        cookie_domain=".tiktok.com",
        cookie_name="sessionid",
        profile_url_template="https://www.tiktok.com/@{username}",
    ),
    "twitter": PlatformInfo(
        id="twitter", name="Twitter / X", icon="𝕏", color="#1DA1F2",
        login_url="https://twitter.com/i/flow/login",
        home_url="https://twitter.com/home",
        cookie_domain=".twitter.com",
        cookie_name="auth_token",
        profile_url_template="https://twitter.com/{username}",
    ),
    "google": PlatformInfo(
        id="google", name="Google Account", icon="🔵", color="#4285F4",
        login_url="https://accounts.google.com/signin",
        home_url="https://myaccount.google.com",
        cookie_domain=".google.com",
        cookie_name="SID",
        profile_url_template="https://myaccount.google.com",
    ),
    "threads": PlatformInfo(
        id="threads", name="Threads", icon="🔗", color="#000000",
        login_url="https://www.threads.net/login",
        home_url="https://www.threads.net",
        cookie_domain=".threads.net",
        cookie_name="ig_did",
        profile_url_template="https://www.threads.net/@{username}",
    ),
}


# ══════════════════════════════════════════════════════════════════
# Account data model
# ══════════════════════════════════════════════════════════════════

@dataclass
class SocialAccount:
    """A social media account connected to Phidipus."""
    platform: str               # platform id (facebook, instagram, etc.)
    username: str = ""          # login email/username
    display_name: str = ""      # display name on the platform
    connected: bool = False     # True if currently logged in
    auto_login: bool = False    # True if Phidipus should auto-login
    last_check: str = ""        # ISO timestamp of last connection check
    last_used: str = ""         # ISO timestamp of last use by agent
    notes: str = ""             # user notes
    profile_url: str = ""       # computed profile URL
    added_at: str = ""          # ISO timestamp when account was added

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SocialAccount":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ══════════════════════════════════════════════════════════════════
# Chrome profile manager
# ══════════════════════════════════════════════════════════════════

def default_chrome_profile_path() -> Path:
    """Return the default Phidipus Chrome profile path on macOS."""
    home = Path.home()
    return home / "Library" / "Application Support" / "Google" / "Chrome" / "Phidipus"


def chrome_profile_exists(profile_path: Path | None = None) -> bool:
    """Check if the Phidipus Chrome profile directory exists."""
    p = profile_path or default_chrome_profile_path()
    return p.is_dir()


def create_chrome_profile(profile_path: Path | None = None) -> Path:
    """Create the Phidipus Chrome profile directory if it doesn't exist."""
    p = profile_path or default_chrome_profile_path()
    p.mkdir(parents=True, exist_ok=True)
    return p


def chrome_launch_command(profile_path: Path | None = None, url: str = "") -> str:
    """
    Return the shell command to launch Chrome with the Phidipus profile.

    Example:
        /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
            --user-data-dir="~/Library/.../Phidipus"
            --profile-directory="Default"
            https://facebook.com/login
    """
    p = profile_path or default_chrome_profile_path()
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    cmd = f'"{chrome}" --user-data-dir="{p}" --profile-directory="Default"'
    if url:
        cmd += f' "{url}"'
    return cmd


# ══════════════════════════════════════════════════════════════════
# Social Manager
# ══════════════════════════════════════════════════════════════════

class SocialManager:
    """
    Manages social media accounts for Phidipus's Chrome profile.

    Accounts are persisted in a JSON file alongside the Phidipus project.
    No passwords are stored — authentication relies on Chrome's saved
    sessions/cookies in the dedicated profile.

    Args:
        data_file:      Path to accounts JSON file.
        profile_path:   Path to Chrome profile directory.
    """

    def __init__(
        self,
        data_file: str | Path = "social/accounts.json",
        profile_path: str | Path | None = None,
    ) -> None:
        self._data_file = Path(data_file)
        self._profile_path = Path(profile_path) if profile_path else default_chrome_profile_path()
        self._accounts: dict[str, SocialAccount] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────

    def _load(self) -> None:
        """Load accounts from JSON file."""
        if self._data_file.exists():
            try:
                data = json.loads(self._data_file.read_text("utf-8"))
                for platform_id, acct_data in data.get("accounts", {}).items():
                    self._accounts[platform_id] = SocialAccount.from_dict(acct_data)
            except (json.JSONDecodeError, KeyError):
                pass

    def _save(self) -> None:
        """Save accounts to JSON file."""
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "9.18",
            "chrome_profile": str(self._profile_path),
            "updated_at": _utc_iso(),
            "accounts": {pid: acct.to_dict() for pid, acct in self._accounts.items()},
        }
        self._data_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Public API ────────────────────────────────────────────

    def list_accounts(self) -> list[dict]:
        """Return all accounts with platform metadata."""
        result = []
        for pid, pinfo in PLATFORMS.items():
            acct = self._accounts.get(pid)
            entry = {
                "platform": pinfo.id,
                "name": pinfo.name,
                "icon": pinfo.icon,
                "color": pinfo.color,
                "login_url": pinfo.login_url,
                "home_url": pinfo.home_url,
                "connected": False,
                "username": "",
                "display_name": "",
                "auto_login": False,
                "last_check": "",
                "last_used": "",
                "notes": "",
                "profile_url": "",
            }
            if acct:
                entry.update({
                    "connected": acct.connected,
                    "username": acct.username,
                    "display_name": acct.display_name,
                    "auto_login": acct.auto_login,
                    "last_check": acct.last_check,
                    "last_used": acct.last_used,
                    "notes": acct.notes,
                    "profile_url": acct.profile_url or pinfo.profile_url_template.format(
                        username=acct.username
                    ),
                })
            result.append(entry)
        return result

    def get_account(self, platform: str) -> dict | None:
        """Get a single account by platform ID."""
        for a in self.list_accounts():
            if a["platform"] == platform:
                return a
        return None

    def add_account(
        self,
        platform: str,
        *,
        username: str = "",
        display_name: str = "",
        auto_login: bool = False,
        notes: str = "",
    ) -> dict:
        """Add or update a social account."""
        if platform not in PLATFORMS:
            raise ValueError(f"Platform không hỗ trợ: {platform}. "
                             f"Hỗ trợ: {', '.join(PLATFORMS.keys())}")

        pinfo = PLATFORMS[platform]
        existing = self._accounts.get(platform)

        acct = SocialAccount(
            platform=platform,
            username=username or (existing.username if existing else ""),
            display_name=display_name or (existing.display_name if existing else ""),
            connected=existing.connected if existing else False,
            auto_login=auto_login,
            notes=notes or (existing.notes if existing else ""),
            profile_url=pinfo.profile_url_template.format(username=username) if username else "",
            added_at=existing.added_at if existing else _utc_iso(),
        )
        self._accounts[platform] = acct
        self._save()
        return self.get_account(platform)

    def remove_account(self, platform: str) -> bool:
        """Remove a social account."""
        if platform in self._accounts:
            del self._accounts[platform]
            self._save()
            return True
        return False

    def set_connected(self, platform: str, connected: bool) -> dict | None:
        """Update connection status of an account."""
        acct = self._accounts.get(platform)
        if not acct:
            return None
        acct.connected = connected
        acct.last_check = _utc_iso()
        self._save()
        return self.get_account(platform)

    def set_last_used(self, platform: str) -> None:
        """Mark an account as just used by the agent."""
        acct = self._accounts.get(platform)
        if acct:
            acct.last_used = _utc_iso()
            self._save()

    def get_login_url(self, platform: str) -> str:
        """Get the login URL for a platform."""
        pinfo = PLATFORMS.get(platform)
        return pinfo.login_url if pinfo else ""

    def get_launch_command(self, platform: str) -> str:
        """Get Chrome launch command to open a platform's login page."""
        url = self.get_login_url(platform)
        return chrome_launch_command(self._profile_path, url)

    def get_chrome_profile_info(self) -> dict:
        """Return Chrome profile status."""
        return {
            "path": str(self._profile_path),
            "exists": self._profile_path.is_dir(),
            "launch_command": chrome_launch_command(self._profile_path),
        }

    def get_summary(self) -> dict:
        """Return a summary of all connections."""
        accounts = self.list_accounts()
        connected = sum(1 for a in accounts if a["connected"])
        return {
            "total_platforms": len(PLATFORMS),
            "connected": connected,
            "disconnected": len(PLATFORMS) - connected,
            "accounts": accounts,
            "chrome_profile": self.get_chrome_profile_info(),
        }

    @staticmethod
    def supported_platforms() -> list[dict]:
        """Return list of supported platforms."""
        return [
            {"id": p.id, "name": p.name, "icon": p.icon, "color": p.color}
            for p in PLATFORMS.values()
        ]


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
