# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/app_scanner.py — Phidipus v1.18
Scans installed applications and Chrome profiles on macOS.

On startup, Phidipus scans:
  1. /Applications + ~/Applications → list of all .app bundles
  2. Chrome profiles → list of all profile names + directories

Results are cached in memory. Use refresh() to rescan.

Usage:
    scanner = AppScanner()
    scanner.scan()                          # scan on startup
    apps = scanner.find_app("chrome")       # fuzzy match → "Google Chrome"
    profiles = scanner.list_chrome_profiles()
    scanner.refresh()                       # rescan
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class InstalledApp:
    """Represents an installed macOS application."""
    name: str              # Display name without .app (e.g. "Google Chrome")
    path: str              # Full path (e.g. "/Applications/Google Chrome.app")
    bundle_id: str = ""    # Bundle identifier (e.g. "com.google.Chrome")


@dataclass
class ChromeProfile:
    """Represents a Chrome profile."""
    name: str              # Display name (e.g. "00fujin")
    directory: str         # Directory name (e.g. "Profile 5")
    path: str = ""         # Full path to profile directory


class AppScanner:
    """
    Scans and caches installed apps + Chrome profiles.

    Designed for macOS but degrades gracefully on other platforms.
    """

    def __init__(self) -> None:
        self._apps: list[InstalledApp] = []
        self._chrome_profiles: list[ChromeProfile] = []
        self._scanned: bool = False

    # ── Public API ────────────────────────────────────────────

    def scan(self) -> dict[str, int]:
        """
        Scan all installed apps and Chrome profiles.
        Returns counts: {"apps": N, "chrome_profiles": M}
        """
        self._apps = self._scan_apps()
        self._chrome_profiles = self._scan_chrome_profiles()
        self._scanned = True
        return {
            "apps": len(self._apps),
            "chrome_profiles": len(self._chrome_profiles),
        }

    def refresh(self) -> dict[str, int]:
        """Alias for scan() — rescan everything."""
        return self.scan()

    @property
    def scanned(self) -> bool:
        return self._scanned

    # ── App lookup ────────────────────────────────────────────

    def find_app(self, query: str) -> InstalledApp | None:
        """
        Fuzzy-find an app by name. Returns best match or None.

        Matching priority:
          1. Exact name match (case-insensitive)
          2. Name starts with query
          3. Query is contained in name
        """
        q = query.strip().lower()
        if not q:
            return None

        # Exact match
        for app in self._apps:
            if app.name.lower() == q:
                return app

        # Starts with
        for app in self._apps:
            if app.name.lower().startswith(q):
                return app

        # Contains
        for app in self._apps:
            if q in app.name.lower():
                return app

        return None

    def list_apps(self) -> list[dict[str, str]]:
        """Return all apps as dicts."""
        return [{"name": a.name, "path": a.path} for a in self._apps]

    def app_exists(self, name: str) -> bool:
        """Check if an app exists (fuzzy)."""
        return self.find_app(name) is not None

    # ── Chrome profiles ───────────────────────────────────────

    def find_chrome_profile(self, query: str) -> ChromeProfile | None:
        """
        Find a Chrome profile by name — 4-tier matching:

        1. Exact match:      "01 Fujin Marketing" == "01 Fujin Marketing"
        2. Numeric prefix:   "01" → matches "01 Fujin Marketing"
                             "1"  → matches "01 Fujin Marketing" (pad to 2 digits)
        3. Contains match:   "Fujin" → matches "01 Fujin Marketing"
        4. Partial number+name: "01 Fujin" → matches "01 Fujin Marketing"
        """
        import re as _re
        q = query.strip().lower()
        if not q:
            return None

        # ── Tier 1: Exact match ──────────────────────────────────
        for p in self._chrome_profiles:
            if p.name.lower() == q:
                return p

        # ── Tier 2: Numeric prefix match ────────────────────────
        # User types "01", "1", "5" → match profile starting with that number
        if _re.fullmatch(r'\d{1,3}', q):
            # Pad to 2 digits (1 → "01", 5 → "05", 10 → "10")
            padded = q.zfill(2)
            for p in self._chrome_profiles:
                pname = p.name.strip()
                # Match: name starts with "01 " or "01_" or "01-" or "01:"
                if _re.match(rf'^{_re.escape(padded)}[\s_\-:.]', pname, _re.I):
                    return p
                # Also match exact prefix without separator (e.g. "01Fujin")
                if pname.lower().startswith(padded):
                    return p
            # Try unpadded too (in case user named "1 Fujin" not "01 Fujin")
            for p in self._chrome_profiles:
                if _re.match(rf'^{_re.escape(q)}[\s_\-:.]', p.name, _re.I):
                    return p

        # ── Tier 3: Contains match ───────────────────────────────
        for p in self._chrome_profiles:
            if q in p.name.lower():
                return p

        return None

    def find_profiles_by_number_range(
        self, start: int, end: int
    ) -> list:
        """
        Return profiles whose numeric prefix falls in [start, end] inclusive.

        Example: start=1, end=5 → profiles "01 Fujin", "02 Tam", ..., "05 An"
        """
        import re as _re
        result = []
        for p in self._chrome_profiles:
            m = _re.match(r'^(\d+)', p.name.strip())
            if m:
                num = int(m.group(1))
                if start <= num <= end:
                    result.append(p)
        # Sort by numeric prefix
        result.sort(key=lambda p: int(_re.match(r'^(\d+)', p.name.strip()).group(1)))
        return result

    def get_profile_by_number(self, num: int):
        """Return profile whose numeric prefix == num (e.g. 5 → '05 Fujin')."""
        import re as _re
        padded = str(num).zfill(2)
        for p in self._chrome_profiles:
            m = _re.match(r'^(\d+)', p.name.strip())
            if m and int(m.group(1)) == num:
                return p
        return None

    def list_chrome_profiles(self) -> list[dict[str, str]]:
        """Return all Chrome profiles as dicts."""
        return [{"name": p.name, "directory": p.directory} for p in self._chrome_profiles]

    # ── Summary ───────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Return scan summary for Admin Panel / Telegram."""
        browsers = [a.name for a in self._apps
                    if any(b in a.name.lower() for b in ("chrome", "firefox", "safari", "brave", "arc", "edge"))]
        return {
            "scanned": self._scanned,
            "total_apps": len(self._apps),
            "browsers": browsers,
            "chrome_profiles": len(self._chrome_profiles),
            "chrome_profile_names": [p.name for p in self._chrome_profiles[:20]],  # first 20
        }

    # ── Internal: scan apps ───────────────────────────────────

    def _scan_apps(self) -> list[InstalledApp]:
        """Scan /Applications and ~/Applications for .app bundles."""
        apps = []
        dirs_to_scan = [
            Path("/Applications"),
            Path.home() / "Applications",
            Path("/System/Applications"),
        ]

        for app_dir in dirs_to_scan:
            if not app_dir.exists():
                continue
            try:
                for entry in app_dir.iterdir():
                    if entry.suffix == ".app" and entry.is_dir():
                        name = entry.stem  # "Google Chrome.app" → "Google Chrome"
                        bundle_id = self._get_bundle_id(entry)
                        apps.append(InstalledApp(
                            name=name,
                            path=str(entry),
                            bundle_id=bundle_id,
                        ))
            except PermissionError:
                continue

        # Sort by name
        apps.sort(key=lambda a: a.name.lower())
        return apps

    @staticmethod
    def _get_bundle_id(app_path: Path) -> str:
        """Read bundle identifier from Info.plist (best effort)."""
        plist = app_path / "Contents" / "Info.plist"
        if not plist.exists():
            return ""
        try:
            result = subprocess.run(
                ["defaults", "read", str(plist), "CFBundleIdentifier"],
                capture_output=True, text=True, timeout=2,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    # ── Internal: scan Chrome profiles ────────────────────────

    def _scan_chrome_profiles(self) -> list[ChromeProfile]:
        """Scan Chrome's Local State for all profile names."""
        profiles = []

        chrome_dir = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        local_state = chrome_dir / "Local State"

        if not local_state.exists():
            return profiles

        try:
            data = json.loads(local_state.read_text(encoding="utf-8"))
            info_cache = data.get("profile", {}).get("info_cache", {})

            for dir_name, info in info_cache.items():
                name = info.get("name", dir_name)
                profiles.append(ChromeProfile(
                    name=name,
                    directory=dir_name,
                    path=str(chrome_dir / dir_name),
                ))
        except Exception:
            pass

        profiles.sort(key=lambda p: p.name.lower())
        return profiles

    def chrome_launch_command(self, profile_name: str = "") -> list[str]:
        """
        Build the macOS command to launch Chrome with a specific profile.

        Returns:
            ["open", "-a", "Google Chrome", "--args", "--profile-directory=Profile 5"]
        """
        cmd = ["open", "-a", "Google Chrome"]

        if profile_name:
            profile = self.find_chrome_profile(profile_name)
            if profile:
                cmd.extend(["--args", f"--profile-directory={profile.directory}"])

        return cmd
