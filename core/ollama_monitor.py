# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/ollama_monitor.py — Phidipus v2.4 Priority 2 / B2
═══════════════════════════════════════════════════════════════════

Background health checker for Ollama LLM service.

Features:
  - Polls http://127.0.0.1:11434/api/tags every 30s
  - Auto-restarts Ollama after 3 consecutive failures
  - Exposes .healthy property for UI status indicator
  - Emits events for Admin Panel WebSocket

Usage:
    monitor = OllamaMonitor()
    await monitor.start()
    if monitor.healthy:
        ...
    await monitor.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
import urllib.error
from typing import Any, Optional

_log = logging.getLogger("phidipus.ollama_monitor")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


class OllamaMonitor:
    """
    Background Ollama health monitor.

    Checks Ollama API every CHECK_INTERVAL seconds.
    After MAX_FAIL_BEFORE_RESTART consecutive failures, attempts restart.
    Max MAX_RESTART_ATTEMPTS restarts before giving up.
    """

    CHECK_INTERVAL: int = 30          # seconds between checks
    MAX_FAIL_BEFORE_RESTART: int = 3  # failures before restart attempt
    MAX_RESTART_ATTEMPTS: int = 3     # max restart attempts per session
    OLLAMA_URL: str = "http://127.0.0.1:11434/api/tags"

    def __init__(self) -> None:
        self._healthy: bool = False
        self._consecutive_fails: int = 0
        self._restart_attempts: int = 0
        self._last_check: float = 0
        self._last_models: list[str] = []
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._event_bus: Any = None  # injected

    # ── Properties ────────────────────────────────────────────

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def models(self) -> list[str]:
        return list(self._last_models)

    @property
    def stats(self) -> dict:
        return {
            "healthy": self._healthy,
            "consecutive_fails": self._consecutive_fails,
            "restart_attempts": self._restart_attempts,
            "last_check": self._last_check,
            "models": self._last_models,
            "monitoring": self._running,
        }

    def inject_event_bus(self, bus: Any) -> None:
        self._event_bus = bus

    # ── Start/Stop ────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Initial check
        await self._check_health()
        # Start background loop
        self._task = asyncio.create_task(self._monitor_loop())
        status = "✅ Online" if self._healthy else "❌ Offline"
        models = ", ".join(self._last_models[:3]) if self._last_models else "none"
        _vlog("🔍", f"Ollama Monitor started — {status} ({models})")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _vlog("🔴", "Ollama Monitor stopped")

    # ── Monitor Loop ──────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.CHECK_INTERVAL)
                await self._check_health()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.warning(f"Monitor error: {exc}")

    async def _check_health(self) -> None:
        self._last_check = time.time()
        try:
            data = await asyncio.to_thread(self._http_get_tags)
            self._healthy = True
            self._consecutive_fails = 0
            # Parse model names
            models = data.get("models", [])
            self._last_models = [m.get("name", "?") for m in models]
            # Emit event
            if self._event_bus:
                try:
                    await self._event_bus.publish("ollama_health", {
                        "healthy": True,
                        "models": self._last_models,
                    })
                except Exception:
                    pass
        except Exception:
            was_healthy = self._healthy
            self._healthy = False
            self._consecutive_fails += 1

            if was_healthy:
                _vlog("🔴", f"Ollama went OFFLINE (fail #{self._consecutive_fails})")

            # Emit event
            if self._event_bus:
                try:
                    await self._event_bus.publish("ollama_health", {
                        "healthy": False,
                        "consecutive_fails": self._consecutive_fails,
                    })
                except Exception:
                    pass

            # Auto-restart after N failures
            if (self._consecutive_fails >= self.MAX_FAIL_BEFORE_RESTART
                    and self._restart_attempts < self.MAX_RESTART_ATTEMPTS):
                await self._try_restart()

    # ── HTTP check ────────────────────────────────────────────

    def _http_get_tags(self) -> dict:
        req = urllib.request.Request(self.OLLAMA_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    # ── Auto-restart ──────────────────────────────────────────

    async def _try_restart(self) -> None:
        """
        Attempt to restart Ollama service.

        NOTE: BUG #5 FIX — Dùng asyncio.create_subprocess_shell thay os.system.
        os.system() chạy synchronous trong thread pool → blocking event loop khi
        thread bị delay. create_subprocess_shell native async, không block.
        Đây là INFRASTRUCTURE management (restart service), không phải OS automation,
        nên subprocess là chấp nhận được trong context này (không vi phạm R-02 spirit).
        """
        self._restart_attempts += 1
        _vlog("🔄", f"Ollama restart attempt {self._restart_attempts}/{self.MAX_RESTART_ATTEMPTS}...")

        try:
            # BUG #5 FIX: dùng asyncio.create_subprocess_shell thay os.system
            # Kill any existing ollama process
            kill_proc = await asyncio.create_subprocess_shell(
                "pkill -f 'ollama serve' 2>/dev/null || true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await kill_proc.wait()
            await asyncio.sleep(1)

            # Start ollama serve in background (nohup, detached)
            start_proc = await asyncio.create_subprocess_shell(
                "nohup ollama serve > /dev/null 2>&1 &",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await start_proc.wait()

            _vlog("⏳", "Waiting 8s for Ollama to initialize...")
            await asyncio.sleep(8)

            # Verify
            try:
                data = await asyncio.to_thread(self._http_get_tags)
                self._healthy = True
                self._consecutive_fails = 0
                models = [m.get("name", "?") for m in data.get("models", [])]
                self._last_models = models
                _vlog("✅", f"Ollama restarted successfully! Models: {', '.join(models[:3])}")
            except Exception:
                _vlog("❌", f"Ollama restart failed — still offline")
        except FileNotFoundError:
            _vlog("❌", "Ollama binary not found — cannot restart")
        except Exception as exc:
            _vlog("❌", f"Ollama restart error: {exc}")


# ── Singleton ─────────────────────────────────────────────────

_instance: Optional[OllamaMonitor] = None

def get_ollama_monitor() -> OllamaMonitor:
    global _instance
    if _instance is None:
        _instance = OllamaMonitor()
    return _instance
