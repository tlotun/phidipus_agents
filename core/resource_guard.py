# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/resource_guard.py — Phidipus Resource Awareness v9.20
══════════════════════════════════════════════════════════

Giám sát RAM/CPU và thực thi hard limits để bảo vệ hệ thống.

VẤN ĐỀ không có Resource Guard:
  - Agent mở 50 Chrome profiles → hết RAM
  - 10 task chạy đồng thời → CPU 100%, hệ thống đứng
  - SkillForge chạy 5 phút không có timeout → blocked

GIẢI PHÁP:
  ┌─────────────────────────────────────┐
  │ Hard Limits:                        │
  │   max_windows = 20                  │
  │   max_chrome_profiles = 10 (open)   │
  │   max_ram_percent = 70%             │
  │   max_cpu_percent = 80%             │
  │   max_skill_exec_time = 60s         │
  └─────────────────────────────────────┘
  
  ResourceGuard monitor RAM/CPU mỗi 5 giây.
  Khi vượt ngưỡng:
    - WARN: log cảnh báo
    - PAUSE: block task mới (không kill task đang chạy)
    - RECOVER: tự recover sau khi resource giảm

Sử dụng:
  guard = ResourceGuard()
  await guard.start_monitoring()
  
  # Trước khi chạy task nặng
  check = guard.check_before_task("tổng hợp dữ liệu lớn")
  if not check.ok:
      await guard.wait_until_available(timeout=60)
  
  # API cho Admin Panel
  stats = guard.stats()  # RAM, CPU, windows count, status

Thiết kế:
  - stdlib-only: psutil (nếu có) hoặc fallback qua /proc + subprocess
  - Lightweight: không async loop nặng, dùng asyncio.create_task
  - Non-blocking: check() trả về ngay, không block
  - Safe: nếu không lấy được metric → assume OK (fail-open)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any


# ── Vietnamese log helper ──────────────────────────────────────────
def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class ResourceSnapshot:
    """Point-in-time resource state."""
    ram_used_percent: float = 0.0       # 0–100%
    ram_used_mb: float = 0.0            # MB
    ram_total_mb: float = 0.0           # MB
    cpu_percent: float = 0.0            # 0–100%
    open_windows: int = 0               # macOS windows count
    chrome_profiles_open: int = 0       # Chrome windows
    disk_free_gb: float = 999.0         # GB free on home disk
    captured_at: float = field(default_factory=time.time)


@dataclass
class ResourceCheck:
    """Result of a pre-task resource check."""
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)  # Must resolve before running

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "OK"
        parts = self.blocking + self.warnings
        return " | ".join(parts[:3])


# ══════════════════════════════════════════════════════════════════
# Resource collection helpers (stdlib + psutil fallback)
# ══════════════════════════════════════════════════════════════════

def _get_ram_info() -> tuple[float, float, float]:
    """
    Get (used_percent, used_mb, total_mb) — chính xác trên macOS.

    Vấn đề với psutil trên macOS ARM:
      psutil.available đôi khi KHÔNG tính cached/purgeable pages là available
      → báo RAM 86% trong khi Activity Monitor chỉ 42% (64GB machine)

    Fix: đọc vm_stat trực tiếp → tính đúng như Activity Monitor:
      available = (free + inactive + purgeable + speculative) × page_size
      used      = total - available
      percent   = used / total × 100

    Chỉ tính "App Memory + Wired" là thật sự dùng.
    Cached Files (inactive/purgeable) = OS sẽ tự giải phóng khi cần → KHÔNG tính là used.
    """
    import platform

    # ── Phương pháp 1: vm_stat (macOS only, chính xác nhất) ──────
    if platform.system() == "Darwin":
        try:
            # Lấy total RAM
            sc = subprocess.run(
                ["sysctl", "hw.memsize"],
                capture_output=True, text=True, timeout=2
            )
            total_bytes = 0
            if sc.returncode == 0:
                total_bytes = int(sc.stdout.split(":")[1].strip())

            if total_bytes > 0:
                vm = subprocess.run(
                    ["vm_stat"], capture_output=True, text=True, timeout=3
                )
                if vm.returncode == 0:
                    # Parse page size từ header: "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
                    page_size = 16384  # Apple Silicon default
                    for line in vm.stdout.splitlines():
                        if "page size of" in line:
                            try:
                                page_size = int(line.split("page size of")[1].split()[0])
                            except Exception:
                                pass
                            break

                    # Parse từng loại page
                    stats: dict[str, int] = {}
                    keys = (
                        "Pages free",
                        "Pages active",
                        "Pages inactive",
                        "Pages speculative",
                        "Pages wired down",
                        "Pages purgeable",
                        "Pages occupied by compressor",
                    )
                    for line in vm.stdout.splitlines():
                        for k in keys:
                            if line.startswith(k):
                                try:
                                    stats[k] = int(line.split(":")[1].strip().rstrip("."))
                                except (ValueError, IndexError):
                                    pass

                    # Tính như Activity Monitor:
                    # App Memory  = active pages (thật sự đang dùng)
                    # Wired       = wired down (kernel, không giải phóng được)
                    # Compressed  = compressed pages
                    # Cached/Free = inactive + purgeable + free + speculative → CÓ THỂ giải phóng
                    app_mem    = stats.get("Pages active", 0)
                    wired      = stats.get("Pages wired down", 0)
                    compressed = stats.get("Pages occupied by compressor", 0)
                    free       = stats.get("Pages free", 0)
                    inactive   = stats.get("Pages inactive", 0)
                    purgeable  = stats.get("Pages purgeable", 0)
                    specul     = stats.get("Pages speculative", 0)

                    # "Thật sự dùng" = App Memory + Wired + Compressed
                    real_used_bytes = (app_mem + wired + compressed) * page_size
                    # "Có thể dùng ngay" = free + inactive + purgeable + speculative
                    available_bytes = (free + inactive + purgeable + specul) * page_size

                    total_mb    = total_bytes / 1024 / 1024
                    used_mb     = real_used_bytes / 1024 / 1024
                    used_pct    = round(real_used_bytes / total_bytes * 100, 1)

                    return (used_pct, used_mb, total_mb)
        except Exception:
            pass

    # ── Phương pháp 2: psutil fallback (Linux / Windows / macOS nếu vm_stat fail) ──
    try:
        import psutil
        mem = psutil.virtual_memory()
        if platform.system() == "Darwin":
            # psutil trên macOS: dùng available (free + inactive + purgeable)
            pct = round((1 - mem.available / mem.total) * 100, 1)
            used_mb = (mem.total - mem.available) / 1024 / 1024
        else:
            pct = mem.percent
            used_mb = mem.used / 1024 / 1024
        return (pct, used_mb, mem.total / 1024 / 1024)
    except ImportError:
        pass

    return (0.0, 0.0, 0.0)


def _get_cpu_percent() -> float:
    """
    Get CPU usage percent (0–100).
    Tries psutil first, falls back to macOS top.
    """
    try:
        import psutil
        return psutil.cpu_percent(interval=0.5)
    except ImportError:
        pass

    # Fallback: macOS top (1 sample)
    try:
        result = subprocess.run(
            ["top", "-l", "1", "-n", "0"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "CPU usage" in line:
                # "CPU usage: 12.34% user, 5.67% sys, 81.98% idle"
                parts = line.split(",")
                idle_pct = 100.0
                for part in parts:
                    if "idle" in part:
                        try:
                            idle_pct = float(part.strip().split("%")[0])
                        except ValueError:
                            pass
                return 100.0 - idle_pct
    except Exception:
        pass

    return 0.0


def _get_window_count() -> tuple[int, int]:
    """
    Get (total_windows, chrome_windows) on macOS via AppleScript.
    Returns (0, 0) on non-macOS or on error.
    """
    try:
        # Count all visible windows
        script = (
            'tell application "System Events"\n'
            '  set win_count to 0\n'
            '  set chrome_count to 0\n'
            '  repeat with p in (processes whose visible is true)\n'
            '    set win_count to win_count + (count of windows of p)\n'
            '    if name of p contains "Chrome" then\n'
            '      set chrome_count to chrome_count + (count of windows of p)\n'
            '    end if\n'
            '  end repeat\n'
            '  return win_count & "," & chrome_count\n'
            'end tell'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=4
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(",")
            if len(parts) >= 2:
                return int(parts[0].strip()), int(parts[1].strip())
            elif len(parts) == 1:
                return int(parts[0].strip()), 0
    except Exception:
        pass

    return (0, 0)


def _get_disk_free_gb(path: str = os.path.expanduser("~")) -> float:
    """Get free disk space in GB for the given path."""
    try:
        import psutil
        usage = psutil.disk_usage(path)
        return usage.free / 1024 / 1024 / 1024
    except ImportError:
        pass

    try:
        result = subprocess.run(
            ["df", "-g", path], capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 4:
                    return float(parts[3])
    except Exception:
        pass

    return 999.0  # Assume plenty if can't check


# ══════════════════════════════════════════════════════════════════
# Hard limit defaults
# ══════════════════════════════════════════════════════════════════

_DEFAULTS = {
    "max_ram_percent": 85.0,         # Pause new tasks if RAM > 85% (real App Memory)
    "warn_ram_percent": 75.0,        # Log warning if RAM > 75%
    "max_cpu_percent": 80.0,         # Pause if CPU > 80%
    "warn_cpu_percent": 65.0,        # Warning if CPU > 65%
    "max_windows": 20,               # Max open windows
    "max_chrome_profiles": 10,       # Max concurrent Chrome windows
    "min_disk_free_gb": 2.0,         # Pause if disk < 2GB free
    "monitor_interval_s": 5.0,       # Poll every 5 seconds
    "pause_check_interval_s": 2.0,   # Check pause condition every 2s
}


# ══════════════════════════════════════════════════════════════════
# ResourceGuard
# ══════════════════════════════════════════════════════════════════

class ResourceGuard:
    """
    Monitor RAM/CPU và thực thi hard limits.

    Chạy background monitoring task. Cung cấp check API cho AgentLoop
    để hỏi trước khi chạy task nặng.

    Thread-safety: không dùng threading, chỉ dùng asyncio.
    Non-blocking: tất cả check() trả về ngay.
    """

    def __init__(self, limits: dict[str, Any] | None = None) -> None:
        self._limits = {**_DEFAULTS, **(limits or {})}
        self._snapshot = ResourceSnapshot()
        self._paused = False
        self._monitor_task: asyncio.Task | None = None
        self._running = False
        self._history: list[ResourceSnapshot] = []
        self._events: list[dict] = []

        # ── Auto-scale ngưỡng RAM theo tổng RAM thật của máy ────────
        # Máy RAM lớn (32GB+) không nên bị chặn ở mức % thấp
        # vì 70% của 64GB = 44GB thật sự dùng → quá thấp, gây false alarm
        try:
            _, _, total_mb = _get_ram_info()
            if total_mb > 0:
                total_gb = total_mb / 1024
                if total_gb >= 48:        # 64GB máy → warn 85%, stop 93%
                    # [FIX v4.2] Tăng từ 80/90 → 85/93 cho Mac 64GB
                    # Ollama + Brain v7 + Worker LLM cần ~15-20GB peak
                    # 93% of 64GB = ~60GB used → vẫn còn ~4GB headroom
                    self._limits.setdefault("warn_ram_percent", 85.0)
                    self._limits.setdefault("max_ram_percent",  93.0)
                elif total_gb >= 24:      # 32GB máy → warn 78%, stop 88%
                    self._limits.setdefault("warn_ram_percent", 78.0)
                    self._limits.setdefault("max_ram_percent",  88.0)
                elif total_gb >= 12:      # 16GB máy → warn 75%, stop 85%
                    self._limits.setdefault("warn_ram_percent", 75.0)
                    self._limits.setdefault("max_ram_percent",  85.0)
                # < 12GB giữ default 75/85 đã set ở _DEFAULTS
                _vlog("🛡️", f"ResourceGuard — RAM {total_gb:.0f}GB → "
                      f"ngưỡng warn={self._limits['warn_ram_percent']:.0f}% "
                      f"stop={self._limits['max_ram_percent']:.0f}%")
        except Exception:
            pass

    # ── Lifecycle ────────────────────────────────────────────────

    async def start_monitoring(self) -> None:
        """
        Bắt đầu background monitoring task.
        Gọi một lần trong main.py sau khi boot.
        """
        if self._running:
            return
        self._running = True
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="resource_guard_monitor",
        )
        # Take initial snapshot
        await self._update_snapshot()
        _vlog("🛡️", f"ResourceGuard — RAM {self._snapshot.ram_used_percent:.0f}% | "
              f"CPU {self._snapshot.cpu_percent:.0f}%")

    async def stop(self) -> None:
        """Dừng background monitoring."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await asyncio.wait_for(self._monitor_task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    # ── Check API ────────────────────────────────────────────────

    def check_before_task(self, task_description: str = "") -> ResourceCheck:
        """
        Kiểm tra resource trước khi chạy task.
        Trả về ResourceCheck ngay (không block).

        Usage::
            check = guard.check_before_task("tổng hợp dữ liệu")
            if not check.ok:
                _vlog("⚠️", f"Resource thấp: {check.summary()}")
                await guard.wait_until_available(timeout=60)
        """
        snap = self._snapshot
        result = ResourceCheck()

        # RAM check
        if snap.ram_used_percent > self._limits["max_ram_percent"]:
            result.ok = False
            result.blocking.append(
                f"RAM {snap.ram_used_percent:.0f}% > giới hạn {self._limits['max_ram_percent']:.0f}%"
            )
        elif snap.ram_used_percent > self._limits["warn_ram_percent"]:
            result.warnings.append(
                f"RAM {snap.ram_used_percent:.0f}% — đang cao"
            )

        # CPU check
        if snap.cpu_percent > self._limits["max_cpu_percent"]:
            result.ok = False
            result.blocking.append(
                f"CPU {snap.cpu_percent:.0f}% > giới hạn {self._limits['max_cpu_percent']:.0f}%"
            )
        elif snap.cpu_percent > self._limits["warn_cpu_percent"]:
            result.warnings.append(
                f"CPU {snap.cpu_percent:.0f}% — đang cao"
            )

        # Window count check
        max_win = self._limits["max_windows"]
        if snap.open_windows > max_win:
            result.ok = False
            result.blocking.append(
                f"Quá nhiều windows ({snap.open_windows} > {max_win})"
            )

        # Chrome profiles check
        max_chrome = self._limits["max_chrome_profiles"]
        if snap.chrome_profiles_open > max_chrome:
            result.warnings.append(
                f"Chrome profiles {snap.chrome_profiles_open} > khuyến nghị {max_chrome}"
            )

        # Disk check
        min_disk = self._limits["min_disk_free_gb"]
        if snap.disk_free_gb < min_disk:
            result.ok = False
            result.blocking.append(
                f"Disk trống {snap.disk_free_gb:.1f}GB < {min_disk:.1f}GB"
            )

        # Log if issues found
        if not result.ok:
            _vlog("⚠️", f"ResourceGuard BLOCK [{task_description[:40]}]: {result.summary()}")
        elif result.has_warnings:
            _vlog("🟡", f"ResourceGuard WARN [{task_description[:40]}]: {result.summary()}")

        return result

    async def wait_until_available(
        self,
        timeout: float = 60.0,
        check_interval: float = 2.0,
    ) -> bool:
        """
        Block cho đến khi resource về dưới giới hạn hoặc timeout.

        Usage::
            if not guard.check_before_task().ok:
                available = await guard.wait_until_available(timeout=60)
                if not available:
                    _vlog("❌", "Không đủ tài nguyên sau 60s")
                    return
        """
        deadline = time.monotonic() + timeout
        _vlog("⏳", f"Đang chờ resource giải phóng (max {timeout:.0f}s)...")

        while time.monotonic() < deadline:
            await self._update_snapshot()
            check = self.check_before_task()
            if check.ok:
                _vlog("✅", "Resource sẵn sàng")
                return True
            await asyncio.sleep(check_interval)

        _vlog("⏰", f"Timeout {timeout:.0f}s — resource vẫn chưa đủ")
        return False

    # ── Current state ────────────────────────────────────────────

    @property
    def current(self) -> ResourceSnapshot:
        """Snapshot hiện tại (có thể hơi cũ, cập nhật mỗi 5s)."""
        return self._snapshot

    @property
    def is_under_pressure(self) -> bool:
        """True nếu RAM hoặc CPU đang vượt ngưỡng WARN."""
        snap = self._snapshot
        return (
            snap.ram_used_percent > self._limits["warn_ram_percent"]
            or snap.cpu_percent > self._limits["warn_cpu_percent"]
        )

    @property
    def is_overloaded(self) -> bool:
        """True nếu RAM hoặc CPU vượt giới hạn tối đa."""
        snap = self._snapshot
        return (
            snap.ram_used_percent > self._limits["max_ram_percent"]
            or snap.cpu_percent > self._limits["max_cpu_percent"]
        )

    # ── Cleanup actions ──────────────────────────────────────────

    async def cleanup_temp_files(self, temp_dir: str = "/tmp") -> int:
        """Xoá temp files của Phidipus trong /tmp. Returns số file xoá."""
        removed = 0
        try:
            import glob
            patterns = ["phidipus_*", "ph_skill_*", "forge_output_*"]
            for pattern in patterns:
                for path in glob.glob(os.path.join(temp_dir, pattern)):
                    try:
                        os.unlink(path)
                        removed += 1
                    except OSError:
                        pass
        except Exception:
            pass

        if removed > 0:
            _vlog("🧹", f"Cleanup: đã xoá {removed} temp files")
        return removed

    async def close_excess_windows(self, keep_count: int = 10) -> int:
        """
        Đóng bớt Chrome windows nếu quá nhiều.
        Returns số window đã đóng.
        """
        snap = self._snapshot
        excess = snap.chrome_profiles_open - keep_count
        if excess <= 0:
            return 0

        _vlog("🪟", f"Chrome windows = {snap.chrome_profiles_open}, đóng {excess} bớt...")
        closed = 0
        try:
            # Close Chrome windows via AppleScript (đóng windows, không đóng app)
            script = f"""
tell application "Google Chrome"
  set win_count to count of windows
  if win_count > {keep_count} then
    repeat with i from win_count to {keep_count + 1} by -1
      close window i
    end repeat
  end if
end tell
"""
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                closed = excess
        except Exception:
            pass

        return closed

    # ── Stats for Admin Panel ────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Full stats cho Admin Panel."""
        snap = self._snapshot
        return {
            "status": "overloaded" if self.is_overloaded else (
                "under_pressure" if self.is_under_pressure else "healthy"
            ),
            "ram_used_percent": round(snap.ram_used_percent, 1),
            "ram_used_mb": round(snap.ram_used_mb, 0),
            "ram_total_mb": round(snap.ram_total_mb, 0),
            "cpu_percent": round(snap.cpu_percent, 1),
            "open_windows": snap.open_windows,
            "chrome_profiles_open": snap.chrome_profiles_open,
            "disk_free_gb": round(snap.disk_free_gb, 1),
            "limits": {
                "max_ram_percent": self._limits["max_ram_percent"],
                "max_cpu_percent": self._limits["max_cpu_percent"],
                "max_windows": self._limits["max_windows"],
                "max_chrome_profiles": self._limits["max_chrome_profiles"],
                "min_disk_free_gb": self._limits["min_disk_free_gb"],
            },
            "is_monitoring": self._running,
            "snapshot_age_s": round(time.time() - snap.captured_at, 1),
            "recent_events": self._events[-5:],  # Last 5 events
        }

    def recent_history(self, n: int = 12) -> list[dict]:
        """Last N snapshots (1 min at 5s interval)."""
        return [
            {
                "ram_pct": round(s.ram_used_percent, 1),
                "cpu_pct": round(s.cpu_percent, 1),
                "t": round(s.captured_at, 0),
            }
            for s in self._history[-n:]
        ]

    # ── Internal ─────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Background monitoring task."""
        interval = self._limits["monitor_interval_s"]
        while self._running:
            try:
                await self._update_snapshot()
                self._check_and_log_transitions()
            except Exception as exc:
                # Monitor must never crash
                _vlog("⚠️", f"ResourceGuard monitor error: {str(exc)[:60]}")

            await asyncio.sleep(interval)

    async def _update_snapshot(self) -> None:
        """Collect current resource metrics (runs in thread to avoid blocking)."""
        loop = asyncio.get_event_loop()

        # Run blocking I/O in thread pool
        try:
            ram_pct, ram_mb, ram_total = await loop.run_in_executor(
                None, _get_ram_info
            )
            cpu_pct = await loop.run_in_executor(None, _get_cpu_percent)
        except Exception:
            ram_pct, ram_mb, ram_total, cpu_pct = 0.0, 0.0, 0.0, 0.0

        # Window count (AppleScript — lightweight but macOS only)
        win_count, chrome_count = 0, 0
        try:
            win_count, chrome_count = await loop.run_in_executor(
                None, _get_window_count
            )
        except Exception:
            pass

        # Disk (fast)
        disk_free = 999.0
        try:
            disk_free = await loop.run_in_executor(None, _get_disk_free_gb)
        except Exception:
            pass

        self._snapshot = ResourceSnapshot(
            ram_used_percent=ram_pct,
            ram_used_mb=ram_mb,
            ram_total_mb=ram_total,
            cpu_percent=cpu_pct,
            open_windows=win_count,
            chrome_profiles_open=chrome_count,
            disk_free_gb=disk_free,
            captured_at=time.time(),
        )

        # Keep history (last 60 = 5 min at 5s)
        self._history.append(self._snapshot)
        if len(self._history) > 60:
            self._history.pop(0)

    def _check_and_log_transitions(self) -> None:
        """Log khi resource vượt hoặc thoát ngưỡng."""
        snap = self._snapshot
        now_overloaded = self.is_overloaded
        was_paused = self._paused

        if now_overloaded and not was_paused:
            self._paused = True
            msg = (f"OVERLOAD — RAM {snap.ram_used_percent:.0f}% "
                   f"CPU {snap.cpu_percent:.0f}%")
            _vlog("🔴", f"ResourceGuard: {msg}")
            self._events.append({"t": time.time(), "type": "overload", "msg": msg})
            if len(self._events) > 50:
                self._events.pop(0)

        elif not now_overloaded and was_paused:
            self._paused = False
            msg = (f"RECOVERED — RAM {snap.ram_used_percent:.0f}% "
                   f"CPU {snap.cpu_percent:.0f}%")
            _vlog("🟢", f"ResourceGuard: {msg}")
            self._events.append({"t": time.time(), "type": "recovered", "msg": msg})

        elif self.is_under_pressure and len(self._events) == 0:
            # First warning only — don't repeat every tick
            self._events.append({"t": time.time(), "type": "warning", "msg": f"RAM {snap.ram_used_percent:.0f}%"})
            _vlog("🟡", f"ResourceGuard: RAM {snap.ram_used_percent:.0f}% "
                  f"CPU {snap.cpu_percent:.0f}%")


# ══════════════════════════════════════════════════════════════════
# Singleton accessor
# ══════════════════════════════════════════════════════════════════

_guard_instance: ResourceGuard | None = None


def get_resource_guard(limits: dict[str, Any] | None = None) -> ResourceGuard:
    """
    Lấy hoặc tạo singleton ResourceGuard instance.

    Usage::
        guard = get_resource_guard()
        await guard.start_monitoring()
    """
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = ResourceGuard(limits)
    return _guard_instance
