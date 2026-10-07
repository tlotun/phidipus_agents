# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/wait_until.py — Phidipus Temporal Stability v9.20
════════════════════════════════════════════════════════

Thay thế toàn bộ asyncio.sleep() cứng nhắc bằng event-driven waiting.

VẤN ĐỀ với sleep():
    await asyncio.sleep(3)
    click_button()  # ← Có thể fail nếu app chưa load xong!

GIẢI PHÁP với wait_until():
    await wait_until(window_exists("Excel"), timeout=10)
    click_button()  # ← Chắc chắn app đã sẵn sàng

Chức năng:
  - wait_until()         — Poll condition với timeout + exponential backoff
  - wait_for_file()      — Chờ file xuất hiện / thay đổi kích thước
  - wait_for_app()       — Chờ app mở (check qua AppScanner)
  - wait_for_window()    — Chờ window title xuất hiện (AppleScript)
  - wait_for_network()   — Chờ kết nối internet
  - window_exists()      — Condition: window có tên nhất định đang mở
  - file_exists()        — Condition: file tồn tại + size > 0
  - file_stable()        — Condition: file không thay đổi size trong N giây
  - retry_with_backoff() — Retry async fn với exponential backoff

Thiết kế:
  - stdlib-only (không cần dependencies ngoài)
  - Async-native: dùng asyncio.sleep() giữa các poll
  - Backoff: bắt đầu nhanh (0.2s), tăng dần → không quá 2s
  - Timeout rõ ràng — không loop vô hạn
  - Callable conditions: bất kỳ coroutine hoặc hàm sync
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Coroutine


# ── Vietnamese log helper ──────────────────────────────────────────
def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Core: wait_until
# ══════════════════════════════════════════════════════════════════

async def wait_until(
    condition: Callable[[], bool | Coroutine[Any, Any, bool]],
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.2,
    backoff_factor: float = 1.5,
    max_interval: float = 2.0,
    description: str = "",
    raise_on_timeout: bool = False,
) -> bool:
    """
    Poll *condition* until it returns True or *timeout* seconds elapse.

    Args:
        condition:        Sync or async callable returning bool.
        timeout:          Max seconds to wait. Default 10s.
        poll_interval:    Initial poll interval in seconds. Default 0.2s.
        backoff_factor:   Multiply interval by this each poll. Default 1.5×.
        max_interval:     Cap poll interval at this value. Default 2.0s.
        description:      Human-readable description for logs.
        raise_on_timeout: If True, raise TimeoutError on timeout.

    Returns:
        True if condition became True, False if timeout.

    Examples::

        # Wait for Excel window (up to 15s)
        ok = await wait_until(window_exists("Microsoft Excel"), timeout=15)

        # Wait for output file to appear (up to 30s)
        ok = await wait_until(file_exists("/tmp/report.xlsx"), timeout=30)

        # Wait with custom condition
        async def chromium_ready():
            return os.path.exists("/tmp/chrome.pid")
        await wait_until(chromium_ready, timeout=10, description="Chrome start")
    """
    deadline = time.monotonic() + timeout
    interval = poll_interval
    attempt = 0

    while True:
        attempt += 1
        try:
            result = condition()
            if asyncio.iscoroutine(result):
                satisfied = await result
            else:
                satisfied = bool(result)
        except Exception as exc:
            _vlog("⚠️", f"wait_until condition error (attempt {attempt}): {exc!s:.60}")
            satisfied = False

        if satisfied:
            if description:
                elapsed = time.monotonic() - (deadline - timeout)
                _vlog("⏱️", f"{description} — sẵn sàng sau {elapsed:.1f}s")
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if description:
                _vlog("⏰", f"Timeout {timeout}s: {description}")
            if raise_on_timeout:
                raise TimeoutError(
                    f"wait_until timeout ({timeout}s): {description or 'condition not met'}"
                )
            return False

        sleep_time = min(interval, remaining, max_interval)
        await asyncio.sleep(sleep_time)
        interval = min(interval * backoff_factor, max_interval)


# ══════════════════════════════════════════════════════════════════
# Built-in conditions
# ══════════════════════════════════════════════════════════════════

def file_exists(path: str | Path, min_size_bytes: int = 0) -> Callable[[], bool]:
    """
    Condition: file tồn tại và có kích thước >= min_size_bytes.

    Usage::
        await wait_until(file_exists("/tmp/report.xlsx"), timeout=30)
        await wait_until(file_exists("/tmp/data.csv", min_size_bytes=10), timeout=15)
    """
    _path = Path(path)

    def _check() -> bool:
        if not _path.exists():
            return False
        if min_size_bytes > 0:
            try:
                return _path.stat().st_size >= min_size_bytes
            except OSError:
                return False
        return True

    return _check


def file_stable(path: str | Path, stable_duration: float = 1.0) -> Callable[[], bool]:
    """
    Condition: file kích thước không thay đổi trong *stable_duration* giây.

    Dùng khi chờ file được ghi xong (ví dụ: export Excel).

    Usage::
        # Chờ file ghi xong (stable 2 giây)
        await wait_until(file_stable("/tmp/export.xlsx", 2.0), timeout=60)
    """
    _path = Path(path)
    _last_size: list[int] = [-1]
    _stable_since: list[float] = [0.0]

    def _check() -> bool:
        if not _path.exists():
            _last_size[0] = -1
            _stable_since[0] = 0.0
            return False
        try:
            size = _path.stat().st_size
        except OSError:
            return False

        now = time.monotonic()
        if size != _last_size[0]:
            _last_size[0] = size
            _stable_since[0] = now
            return False

        return (now - _stable_since[0]) >= stable_duration

    return _check


def window_exists(title_substring: str) -> Callable[[], bool]:
    """
    Condition: có window macOS với title chứa *title_substring*.

    Dùng AppleScript để liệt kê tất cả window titles.
    Trả về False ngay nếu không phải macOS.

    Usage::
        await wait_until(window_exists("Microsoft Excel"), timeout=20)
        await wait_until(window_exists("Google Chrome"), timeout=10)
    """
    def _check() -> bool:
        try:
            script = (
                'tell application "System Events" to get name of every window '
                'of every process whose visible is true'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                return title_substring.lower() in result.stdout.lower()
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            pass
        return False

    return _check


def app_running(app_name: str) -> Callable[[], bool]:
    """
    Condition: app với tên *app_name* đang chạy trên macOS.

    Dùng AppleScript để check. Trả về False nếu không phải macOS.

    Usage::
        await wait_until(app_running("Google Chrome"), timeout=15)
        await wait_until(app_running("Microsoft Excel"), timeout=10)
    """
    def _check() -> bool:
        try:
            script = f'application "{app_name}" is running'
            result = subprocess.run(
                ["osascript", "-e", f'tell application "System Events" to return {script}'],
                capture_output=True, text=True, timeout=3,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"
        except Exception:
            pass
        return False

    return _check


def network_reachable(host: str = "8.8.8.8", timeout_s: float = 2.0) -> Callable[[], bool]:
    """
    Condition: có thể kết nối internet đến *host*.

    Usage::
        await wait_until(network_reachable(), timeout=30)
    """
    def _check() -> bool:
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(int(timeout_s * 1000)), host],
                capture_output=True, timeout=timeout_s + 1,
            )
            return result.returncode == 0
        except Exception:
            return False

    return _check


def process_exists(pid: int) -> Callable[[], bool]:
    """
    Condition: process với PID *pid* đang chạy.

    Usage::
        await wait_until(process_exists(chrome_pid), timeout=5)
    """
    def _check() -> bool:
        try:
            os.kill(pid, 0)  # signal 0 = check existence
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False

    return _check


# ══════════════════════════════════════════════════════════════════
# High-level waiters
# ══════════════════════════════════════════════════════════════════

async def wait_for_file(
    path: str | Path,
    *,
    timeout: float = 30.0,
    min_size_bytes: int = 1,
    stable: bool = False,
    stable_duration: float = 1.0,
) -> bool:
    """
    Chờ file xuất hiện (và tuỳ chọn: ghi xong).

    Args:
        path:            Đường dẫn file cần chờ.
        timeout:         Timeout tối đa (giây).
        min_size_bytes:  Kích thước tối thiểu (bytes). Mặc định 1 byte.
        stable:          Nếu True, chờ thêm đến khi file không thay đổi.
        stable_duration: Thời gian ổn định (giây). Chỉ dùng khi stable=True.

    Usage::
        # Chờ file xuất hiện
        found = await wait_for_file("/tmp/report.xlsx", timeout=30)

        # Chờ file ghi xong (stable)
        ready = await wait_for_file("/tmp/export.csv", timeout=60, stable=True)
    """
    _path = Path(path)
    desc = f"file {_path.name}"

    # Step 1: Wait for file to appear
    appeared = await wait_until(
        file_exists(_path, min_size_bytes),
        timeout=timeout,
        description=desc,
    )
    if not appeared:
        return False

    if not stable:
        return True

    # Step 2: Wait for file to stabilize (write complete)
    stabilized = await wait_until(
        file_stable(_path, stable_duration),
        timeout=min(timeout, 30.0),
        description=f"{desc} (stable)",
    )
    return stabilized


async def wait_for_window(
    title: str,
    *,
    timeout: float = 15.0,
    initial_sleep: float = 0.5,
) -> bool:
    """
    Chờ window với title xuất hiện trên macOS.

    Args:
        title:         Chuỗi con của window title cần chờ.
        timeout:       Timeout tối đa (giây).
        initial_sleep: Sleep trước khi bắt đầu poll (giây).
                       Cho app thời gian khởi động trước khi poll.

    Usage::
        # Mở Excel → chờ window xuất hiện
        await ipc.send_action("app_launch", {"app_name": "excel"})
        opened = await wait_for_window("Microsoft Excel", timeout=20)
    """
    if initial_sleep > 0:
        await asyncio.sleep(initial_sleep)

    return await wait_until(
        window_exists(title),
        timeout=timeout,
        description=f"window '{title}'",
    )


async def wait_for_app(
    app_name: str,
    *,
    timeout: float = 15.0,
    initial_sleep: float = 1.0,
) -> bool:
    """
    Chờ app khởi động xong trên macOS.

    Args:
        app_name:      Tên app (ví dụ: "Google Chrome", "Microsoft Excel").
        timeout:       Timeout tối đa (giây).
        initial_sleep: Sleep trước khi bắt đầu poll.

    Usage::
        subprocess.Popen(["open", "-a", "Google Chrome"])
        ready = await wait_for_app("Google Chrome", timeout=20)
    """
    if initial_sleep > 0:
        await asyncio.sleep(initial_sleep)

    return await wait_until(
        app_running(app_name),
        timeout=timeout,
        description=f"app '{app_name}'",
    )


# ══════════════════════════════════════════════════════════════════
# retry_with_backoff
# ══════════════════════════════════════════════════════════════════

async def retry_with_backoff(
    fn: Callable[[], Coroutine[Any, Any, Any]],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    backoff_factor: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    description: str = "",
) -> Any:
    """
    Retry async *fn* với exponential backoff.

    Args:
        fn:             Async callable cần retry.
        max_retries:    Số lần retry tối đa (không tính lần đầu).
        base_delay:     Thời gian chờ sau lần thất bại đầu (giây).
        max_delay:      Thời gian chờ tối đa giữa các retry.
        backoff_factor: Nhân delay mỗi lần retry.
        exceptions:     Chỉ catch các exception này.
        description:    Mô tả để log.

    Returns:
        Kết quả của *fn* khi thành công.

    Raises:
        Lại exception cuối cùng nếu hết retry.

    Usage::
        result = await retry_with_backoff(
            lambda: fetch_data(),
            max_retries=3,
            base_delay=2.0,
            description="fetch data from API",
        )
    """
    last_exc: Exception | None = None
    delay = base_delay

    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except exceptions as exc:
            last_exc = exc
            if attempt < max_retries:
                actual_delay = min(delay, max_delay)
                desc_str = f" [{description}]" if description else ""
                _vlog("🔄", f"Retry {attempt + 1}/{max_retries}{desc_str} "
                      f"sau {actual_delay:.1f}s — {str(exc)[:60]}")
                await asyncio.sleep(actual_delay)
                delay *= backoff_factor
            else:
                _vlog("❌", f"Hết {max_retries} lần retry{' [' + description + ']' if description else ''}")

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_with_backoff: no attempts made")


# ══════════════════════════════════════════════════════════════════
# smart_sleep — thay thế asyncio.sleep() thông minh hơn
# ══════════════════════════════════════════════════════════════════

async def smart_sleep(
    seconds: float,
    *,
    condition: Callable[[], bool] | None = None,
    check_interval: float = 0.2,
) -> bool:
    """
    Sleep *seconds* nhưng có thể thoát sớm nếu *condition* thành True.

    Args:
        seconds:        Thời gian sleep tối đa.
        condition:      Nếu trả về True → thoát sớm.
        check_interval: Kiểm tra condition mỗi N giây.

    Returns:
        True nếu condition thành True (thoát sớm), False nếu hết thời gian.

    Usage::
        # Chờ tối đa 5s, nhưng thoát ngay khi file xuất hiện
        thoat_som = await smart_sleep(5.0, condition=file_exists("/tmp/out.xlsx"))
    """
    if condition is None:
        await asyncio.sleep(seconds)
        return False

    deadline = time.monotonic() + seconds
    while True:
        try:
            if condition():
                return True
        except Exception:
            pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False

        await asyncio.sleep(min(check_interval, remaining))
