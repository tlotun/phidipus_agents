# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/atomic_file.py — Phidipus v1.0.1
Crash-safe atomic file I/O using write-to-temp + fsync + rename.

Design:
  - Writes go to a sibling temporary file in the same directory (guaranteed
    same filesystem as the target, so rename(2) is atomic on POSIX).
  - The file descriptor is fsync()'d before rename to ensure durability on
    crash: a partial write never becomes visible as the target.
  - If the process dies between write and rename, the .tmp file is left behind
    but the original target is unmodified — safe for recovery.
  - fcntl.flock-based advisory locking is available via atomic_file_lock for
    use cases that require serialised cross-process write access (e.g. rate-
    limit counters per R-19).

Patch v9.11.1:
  FIX-5  atomic_append_line() now acquires an exclusive fcntl advisory lock
         on a sibling ``<path>.lock`` file for the full duration of its
         read-modify-write cycle.  Without this lock, two concurrent callers
         racing on the same file could both read the same ``existing`` bytes
         and each write a version that silently drops the other's line —
         corrupting the HMAC-signed audit log.  The lock file name is derived
         deterministically from the data file path so all writers across all
         processes can rendezvous on the same lock without out-of-band
         coordination.

Used by:
  patcher/runtime_patcher.py — patch-ledger append (R-18, R-19)
  memory/episodic_memory.py  — episode ring-buffer flush
  skill_validator/skill_signer.py — .sig file write

Thread/process safety:
  - atomic_write() itself is safe to call concurrently from different threads
    or processes; only one rename will succeed if two writers race on the same
    target (last writer wins).
  - atomic_append_line() now holds a cross-process exclusive lock for the
    entire read-modify-write sequence (FIX-5).
  - For other serialised multi-step operations (read-modify-write beyond
    append), use atomic_file_lock directly.
"""

from __future__ import annotations

import fcntl
import io
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Union

# ---------------------------------------------------------------------------
# Core atomic write
# ---------------------------------------------------------------------------

def atomic_write_bytes(
    path: Union[str, Path],
    data: bytes,
    *,
    mode: int = 0o644,
    sync_dir: bool = True,
) -> None:
    """
    Write *data* to *path* atomically.

    Steps:
      1. Create a temporary file in the same directory as *path*.
      2. Write *data* and call fsync() on the file descriptor.
      3. Set file permissions to *mode*.
      4. Rename the temp file to *path* (atomic on POSIX).
      5. If *sync_dir* is True, fsync the parent directory to persist the
         rename itself (required for full crash durability on ext4 etc.).

    On any error the temporary file is deleted and the original *path* is
    left untouched.

    Args:
        path:     Destination file path.  Parent directory must exist.
        data:     Bytes to write.
        mode:     POSIX permission bits for the destination file.
        sync_dir: Sync the parent directory after rename.  Slightly slower
                  but guarantees durability across power loss.

    Raises:
        OSError: on any I/O failure.
    """
    path = Path(path)
    parent = path.parent

    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.tmp.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, stat.S_IMODE(mode))
        # FIX M-08: os.replace is atomic on POSIX (overwrites destination).
        # Fallback to shutil.move for the (rare) cross-device edge case
        # when parent is a symlink pointing across filesystems.
        try:
            os.replace(tmp_path, path)
        except OSError:
            shutil.move(tmp_path, path)
        tmp_path = None  # Renamed successfully — nothing to clean up
    except BaseException:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    if sync_dir:
        _sync_directory(parent)


def atomic_write_text(
    path: Union[str, Path],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o644,
    sync_dir: bool = True,
) -> None:
    """
    Write *text* to *path* atomically (UTF-8 by default).

    Convenience wrapper around :func:`atomic_write_bytes`.
    """
    atomic_write_bytes(
        path,
        text.encode(encoding),
        mode=mode,
        sync_dir=sync_dir,
    )


def atomic_append_line(
    path: Union[str, Path],
    line: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o644,
) -> None:
    """
    Append *line* + newline to *path* atomically using read-then-rewrite,
    protected by an exclusive cross-process advisory lock.

    This is safe for append-only logs (e.g. patch ledger, action audit log)
    where the file grows monotonically and no entry is ever modified.

    Patch v9.11.1 / FIX-5:
        The read-modify-write cycle is now wrapped in an exclusive
        ``atomic_file_lock`` on a sibling ``<path>.lock`` file for the
        full duration of the operation.  Without this lock, two concurrent
        callers could both read the same ``existing`` bytes, each compute
        independent ``new_data``, and each write — silently dropping one
        caller's line.  The lock file path is derived deterministically
        (``<path>.lock``) so any process that knows the data file path can
        participate in the same mutual-exclusion protocol.

    Args:
        path:     File to append to.  Created if it does not exist.
        line:     Text line to append.  A newline is added automatically.
        encoding: Text encoding.
        mode:     POSIX permission bits if the file is created.
    """
    path = Path(path)
    # Derive a deterministic lock file path from the data file.
    # Using <path>.lock (i.e. append ".lock" to the full name) keeps the
    # lock sibling clearly associated with its data file in directory listings.
    lock_path = path.with_name(path.name + ".lock")

    with atomic_file_lock(lock_path, exclusive=True):
        existing = safe_read_bytes(path) or b""
        new_data = existing + (line + "\n").encode(encoding)
        atomic_write_bytes(path, new_data, mode=mode, sync_dir=True)


# ---------------------------------------------------------------------------
# Advisory locking
# ---------------------------------------------------------------------------

@contextmanager
def atomic_file_lock(
    path: Union[str, Path],
    *,
    exclusive: bool = True,
    timeout: float = 5.0,
) -> Generator[None, None, None]:
    """
    Context manager that holds an fcntl advisory lock on *path*.

    The lock file is created if it does not exist.  The lock is released
    when the context exits (even on exception).

    This satisfies R-19: rate-limit state persisted to disk uses flock,
    not an in-memory counter.

    Args:
        path:      Path to the lock file (typically ``<data>.lock``).
        exclusive: If True (default), acquire an exclusive (write) lock.
                   If False, acquire a shared (read) lock.
        timeout:   Raise TimeoutError if the lock cannot be acquired within
                   this many seconds.  Uses non-blocking flock + polling.

    Raises:
        TimeoutError: if the lock cannot be acquired within *timeout* seconds.
        OSError:      on unexpected I/O errors.

    Usage::

        with atomic_file_lock("/data/rate_limit.lock"):
            count = int(Path("/data/rate_limit.dat").read_text())
            count += 1
            atomic_write_text("/data/rate_limit.dat", str(count))
    """
    import time

    lock_op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    path = Path(path)
    deadline = time.monotonic() + timeout

    lock_fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(lock_fd, lock_op | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire lock on {path} within {timeout}s"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sync_directory(path: Path) -> None:
    """
    Call fsync on a directory file descriptor to flush the rename to stable
    storage.  No-op on platforms where directory fsyncing is unsupported.
    """
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Some filesystems (e.g. FAT, SMB) do not support directory fsync.
        # Silently skip — the write is still atomic from a visibility standpoint.
        pass


# ---------------------------------------------------------------------------
# Safe read helpers
# ---------------------------------------------------------------------------

def safe_read_bytes(path: Union[str, Path]) -> bytes | None:
    """
    Read and return the bytes of *path*, or None if the file does not exist.

    Does not mask unexpected OSError (e.g. permission denied).
    """
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def safe_read_text(
    path: Union[str, Path],
    *,
    encoding: str = "utf-8",
) -> str | None:
    """
    Read and return the text of *path*, or None if the file does not exist.
    """
    try:
        return Path(path).read_text(encoding=encoding)
    except FileNotFoundError:
        return None
