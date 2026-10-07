# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
admin/spider_hub_config.py — SpiderHub Admin Panel Config v9.20
══════════════════════════════════════════════════════════

Theme, shortcuts, privacy levels, feature flags for the SpiderHub panel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Privacy levels for Shadow Mode ────────────────────────────────

PRIVACY_LEVELS = {
    "low":    {"record_screen": True,  "record_keys": True,  "record_files": True},
    "medium": {"record_screen": True,  "record_keys": False, "record_files": True},
    "god":    {"record_screen": False, "record_keys": False, "record_files": False},
}

# ── Keyboard shortcuts (Cmd+K palette + quick keys) ───────────────

KEYBOARD_SHORTCUTS = {
    "?":      "show_help",
    "1":      "nav_home",
    "2":      "nav_chrome",
    "3":      "nav_skill_forge",
    "4":      "nav_shadow",
    "5":      "nav_task_graph",
    "6":      "nav_memory",
    "7":      "nav_resource",
    "8":      "nav_security",
    "9":      "nav_god_mode",
    "cmd+k":  "open_command_palette",
    "cmd+enter": "god_button_submit",
    "cmd+s":  "shadow_toggle",
    "cmd+r":  "shadow_replay_last",
    "cmd+p":  "profile_quick_launch",
    "esc":    "close_modal",
}

# ── Theme tokens (glass morphism dark) ────────────────────────────

THEME = {
    "bg_primary":    "#0a0a0f",
    "bg_glass":      "rgba(255,255,255,0.05)",
    "bg_glass_hover":"rgba(255,255,255,0.09)",
    "border_glass":  "rgba(255,255,255,0.12)",
    "accent_spider":    "#f5c518",      # Bee yellow
    "accent_honey":  "#ff8c00",      # Honey orange
    "accent_hive":   "#00d4aa",      # Hive teal
    "text_primary":  "#f0f0f0",
    "text_muted":    "#888",
    "danger":        "#ff4444",
    "success":       "#00c851",
}

# ── Feature flags ──────────────────────────────────────────────────

@dataclass
class SpiderHubConfig:
    """Runtime config for SpiderHub admin panel."""

    # Shadow Mode
    shadow_mode_enabled: bool = False
    shadow_privacy_level: str = "medium"        # low | medium | god
    shadow_max_segments: int = 50               # Max recording segments
    shadow_learn_last_n: int = 10               # Learn from last N tasks

    # Screen thumbnail
    screen_thumb_refresh_s: float = 5.0
    screen_thumb_width: int = 1280

    # WebSocket
    ws_heartbeat_s: float = 3.0
    ws_broadcast_interval_s: float = 1.0

    # Task graph
    task_graph_layout: str = "dagre"            # dagre | cose | breadthfirst

    # Resource monitoring
    resource_refresh_s: float = 3.0

    # God mode
    god_mode_confirm_required: bool = True

    # Privacy
    audit_log_enabled: bool = True
    audit_log_hmac: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "shadow_mode_enabled": self.shadow_mode_enabled,
            "shadow_privacy_level": self.shadow_privacy_level,
            "privacy_levels": PRIVACY_LEVELS,
            "keyboard_shortcuts": KEYBOARD_SHORTCUTS,
            "theme": THEME,
            "screen_thumb_refresh_s": self.screen_thumb_refresh_s,
            "ws_heartbeat_s": self.ws_heartbeat_s,
            "task_graph_layout": self.task_graph_layout,
            "god_mode_confirm_required": self.god_mode_confirm_required,
        }


# Singleton
_config = SpiderHubConfig()


def get_config() -> SpiderHubConfig:
    return _config
