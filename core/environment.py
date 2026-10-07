# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/environment.py — Phidipus Environment Graph v9.20
══════════════════════════════════════════════════════

Live state graph of the entire machine. Agent queries this BEFORE acting.

Tracks:
  - Running apps + PIDs
  - Open windows (title, position, size, focused)
  - Active browser tabs (Chrome)
  - File system recent changes
  - Screen layout (monitors, resolution)
  - Clipboard content
  - System resources (RAM, CPU)

Agent uses this for smart decisions:
  "mở chrome" → Chrome already running? → don't reopen, just activate
  "mở 5 profile" → already 3 windows? → only open 2 more
  "vào gemini.com" → Chrome focused? → just Cmd+L + type URL

Refreshed on-demand (not polling). Cached for 5 seconds.

Usage:
    env = EnvironmentGraph()
    await env.refresh()
    
    state = env.state
    # state.apps → [{"name": "Chrome", "pid": 1234, "running": True}]
    # state.windows → [{"title": "...", "app": "Chrome", "focused": True}]
    # state.screen → {"width": 3440, "height": 1440, "monitors": 2}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


@dataclass
class AppInfo:
    name: str
    pid: int = 0
    running: bool = True
    bundle_id: str = ""


@dataclass
class WindowInfo:
    title: str
    app: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    focused: bool = False
    minimized: bool = False


@dataclass
class ScreenInfo:
    width: int = 3440
    height: int = 1440
    monitors: int = 1
    scale: float = 1.0


@dataclass
class EnvironmentState:
    """Complete machine state snapshot."""
    apps: list[AppInfo] = field(default_factory=list)
    windows: list[WindowInfo] = field(default_factory=list)
    screen: ScreenInfo = field(default_factory=ScreenInfo)
    focused_app: str = ""
    focused_window: str = ""
    clipboard: str = ""
    ram_used_percent: float = 0.0
    cpu_percent: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def app_is_running(self, name: str) -> bool:
        """Check if an app is running (fuzzy match)."""
        nl = name.lower()
        return any(nl in a.name.lower() for a in self.apps)

    def get_app_windows(self, app_name: str) -> list[WindowInfo]:
        """Get all windows for an app."""
        nl = app_name.lower()
        return [w for w in self.windows if nl in w.app.lower()]

    def window_count(self, app_name: str = "") -> int:
        if app_name:
            return len(self.get_app_windows(app_name))
        return len(self.windows)

    def to_context_string(self) -> str:
        """Compact string for LLM/Gemini context injection."""
        lines = [
            f"Screen: {self.screen.width}x{self.screen.height} ({self.screen.monitors} monitors)",
            f"Focused: {self.focused_app} — {self.focused_window[:50]}",
            f"RAM: {self.ram_used_percent:.0f}%, CPU: {self.cpu_percent:.0f}%",
            f"Running apps ({len(self.apps)}): {', '.join(a.name for a in self.apps[:10])}",
            f"Windows ({len(self.windows)}):",
        ]
        for w in self.windows[:8]:
            focus = " ⭐" if w.focused else ""
            lines.append(f"  [{w.app}] {w.title[:40]}{focus}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "apps": [{"name": a.name, "pid": a.pid} for a in self.apps],
            "windows": [{"title": w.title, "app": w.app, "focused": w.focused} for w in self.windows],
            "screen": {"width": self.screen.width, "height": self.screen.height, "monitors": self.screen.monitors},
            "focused_app": self.focused_app,
            "ram_percent": self.ram_used_percent,
            "timestamp": self.timestamp,
        }


class EnvironmentGraph:
    """
    Live environment state with caching.

    Scans macOS system state via AppleScript + system commands.
    Cached for cache_ttl seconds to avoid excessive scanning.
    """

    def __init__(self, cache_ttl: float = 5.0, app_scanner: Any = None) -> None:
        self._cache_ttl = cache_ttl
        self._scanner = app_scanner
        self._state = EnvironmentState()
        self._last_refresh: float = 0.0

    @property
    def state(self) -> EnvironmentState:
        return self._state

    @property
    def stale(self) -> bool:
        return (time.time() - self._last_refresh) > self._cache_ttl

    async def refresh(self, force: bool = False) -> EnvironmentState:
        """Refresh environment state (cached unless force=True)."""
        if not force and not self.stale:
            return self._state

        state = EnvironmentState()

        # Run all scans concurrently
        results = await asyncio.gather(
            self._scan_apps(),
            self._scan_windows(),
            self._scan_screen(),
            self._scan_focused(),
            self._scan_resources(),
            return_exceptions=True,
        )

        if not isinstance(results[0], BaseException):
            state.apps = results[0]
        if not isinstance(results[1], BaseException):
            state.windows = results[1]
        if not isinstance(results[2], BaseException):
            state.screen = results[2]
        if not isinstance(results[3], BaseException):
            state.focused_app, state.focused_window = results[3]
        if not isinstance(results[4], BaseException):
            state.ram_used_percent, state.cpu_percent = results[4]

        state.timestamp = time.time()
        self._state = state
        self._last_refresh = time.time()
        return state

    # ══════════════════════════════════════════════════════════
    # Smart queries (for agent decision-making)
    # ══════════════════════════════════════════════════════════

    async def should_open_app(self, app_name: str) -> dict[str, Any]:
        """
        Check if an app needs to be opened.
        Returns {"should_open": bool, "reason": str, "existing_windows": int}
        """
        await self.refresh()
        running = self._state.app_is_running(app_name)
        windows = self._state.get_app_windows(app_name)

        if not running:
            return {"should_open": True, "reason": "App chưa chạy", "existing_windows": 0}

        if not windows:
            return {"should_open": True, "reason": "App chạy nhưng không có window", "existing_windows": 0}

        return {
            "should_open": False,
            "reason": f"App đã mở ({len(windows)} windows)",
            "existing_windows": len(windows),
            "focused": any(w.focused for w in windows),
        }

    async def get_chrome_window_count(self) -> int:
        """Count Chrome windows (useful for multi-profile tiling)."""
        await self.refresh()
        return self._state.window_count("Chrome")

    # ══════════════════════════════════════════════════════════
    # macOS scanners (via AppleScript + shell)
    # ══════════════════════════════════════════════════════════

    async def _scan_apps(self) -> list[AppInfo]:
        """Get running applications via AppleScript."""
        script = '''
tell application "System Events"
    set appList to ""
    repeat with p in (every process whose background only is false)
        set appList to appList & name of p & "||" & unix id of p & "\\n"
    end repeat
end tell
return appList
'''
        try:
            result = await self._run_osascript(script)
            apps = []
            for line in result.strip().split("\n"):
                if "||" in line:
                    parts = line.split("||")
                    apps.append(AppInfo(
                        name=parts[0].strip(),
                        pid=int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0,
                    ))
            return apps
        except Exception:
            return []

    async def _scan_windows(self) -> list[WindowInfo]:
        """Get open windows via AppleScript."""
        script = '''
tell application "System Events"
    set winList to ""
    repeat with p in (every process whose background only is false)
        try
            repeat with w in (every window of p)
                set winList to winList & name of p & "||" & name of w & "||" & (position of w as string) & "||" & (size of w as string) & "\\n"
            end repeat
        end try
    end repeat
end tell
return winList
'''
        try:
            result = await self._run_osascript(script)
            windows = []
            for line in result.strip().split("\n"):
                if "||" in line:
                    parts = line.split("||")
                    if len(parts) >= 2:
                        w = WindowInfo(
                            app=parts[0].strip(),
                            title=parts[1].strip(),
                        )
                        # Parse position and size if available
                        if len(parts) >= 3:
                            pos = re.findall(r'\d+', parts[2])
                            if len(pos) >= 2:
                                w.x, w.y = int(pos[0]), int(pos[1])
                        if len(parts) >= 4:
                            size = re.findall(r'\d+', parts[3])
                            if len(size) >= 2:
                                w.width, w.height = int(size[0]), int(size[1])
                        windows.append(w)
            return windows
        except Exception:
            return []

    async def _scan_screen(self) -> ScreenInfo:
        """Get screen resolution."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "system_profiler", "SPDisplaysDataType",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            output = stdout.decode()

            # Count monitors
            monitors = len(re.findall(r'Resolution:', output))

            # Get primary resolution
            m = re.search(r'(\d{3,5})\s*x\s*(\d{3,5})', output)
            if m:
                return ScreenInfo(
                    width=int(m.group(1)), height=int(m.group(2)),
                    monitors=max(1, monitors),
                )
        except Exception:
            pass
        return ScreenInfo()

    async def _scan_focused(self) -> tuple[str, str]:
        """Get focused app and window title."""
        script = '''
tell application "System Events"
    set frontApp to name of first process whose frontmost is true
    try
        set frontWin to name of front window of (first process whose frontmost is true)
    on error
        set frontWin to ""
    end try
end tell
return frontApp & "||" & frontWin
'''
        try:
            result = await self._run_osascript(script)
            parts = result.strip().split("||")
            app = parts[0].strip() if parts else ""
            win = parts[1].strip() if len(parts) > 1 else ""
            return app, win
        except Exception:
            return "", ""

    async def _scan_resources(self) -> tuple[float, float]:
        """Get RAM and CPU usage."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "vm_stat",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            output = stdout.decode()

            # Parse vm_stat output for RAM
            pages_free = 0
            pages_active = 0
            pages_inactive = 0
            pages_wired = 0
            for line in output.split("\n"):
                if "Pages free" in line:
                    pages_free = int(re.search(r'(\d+)', line).group(1))
                elif "Pages active" in line:
                    pages_active = int(re.search(r'(\d+)', line).group(1))
                elif "Pages inactive" in line:
                    pages_inactive = int(re.search(r'(\d+)', line).group(1))
                elif "Pages wired" in line:
                    pages_wired = int(re.search(r'(\d+)', line).group(1))

            total = pages_free + pages_active + pages_inactive + pages_wired
            used = pages_active + pages_wired
            ram_percent = (used / total * 100) if total > 0 else 0.0

            # CPU from top (quick sample)
            proc2 = await asyncio.create_subprocess_exec(
                "top", "-l", "1", "-n", "0",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
            cpu_match = re.search(r'CPU usage:\s+([\d.]+)%\s+user', stdout2.decode())
            cpu_percent = float(cpu_match.group(1)) if cpu_match else 0.0

            return ram_percent, cpu_percent
        except Exception:
            return 0.0, 0.0

    async def _run_osascript(self, script: str, timeout: float = 5.0) -> str:
        """Run AppleScript and return output."""
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode()
