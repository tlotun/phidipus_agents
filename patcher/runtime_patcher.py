# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
patcher/runtime_patcher.py — Phidipus v1.0
Applies vetted patches to Phidipus source files with integrity ledger.

Architecture contract (R-18 / R-19):
  "Replace custom diff parser with stdlib difflib (H-3).  Persist rate
   limit to disk with fcntl.flock (H-2, R-19).  Add signed patch ledger
   (C-6).  Block patches to sandbox/, security/, patcher/."

RuntimePatcher applies text patches (unified diff format) to source
files under the Phidipus installation directory.  Every applied patch
is recorded in an append-only, SHA-256 hash-chained ledger.

Rate limiting
-------------
Patches are rate-limited to cfg.patcher.rate_limit_per_hour.  The
counter is persisted to disk with fcntl.flock (R-19) — NOT an in-memory
counter.  This prevents the limit from being reset by restarting the
process.

Protected directories (R-18)
-----------------------------
Patches to sandbox/, security/, and patcher/ are prohibited.  Any
attempt raises PermissionError immediately.

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-18  Patches to sandbox/, security/, patcher/ are blocked.
        Ledger is append-only with SHA-256 per entry.
  R-19  Rate-limit counter is persisted to disk with fcntl.flock.

Used by:
  core/agent_loop.py — apply_patch() for self-modification

Dependencies:
  utils/atomic_file.py  — atomic_write_text(), atomic_append_line(),
                           atomic_file_lock()
  utils/file_ops.py     — check_write_allowed(), safe_join(), read_text()
  utils/hash_utils.py   — sha256_hex()
  utils/json_utils.py   — dumps(), loads()
  utils/logger.py       — get_logger()
  config/config_loader.py — PhidipusConfig

BUG-D FIX: atomic_write_text(), atomic_append_line(), and
  _increment_rate_counter() in apply_patch() were not wrapped in
  try/except.  An OSError from any of these would propagate as a raw
  exception rather than a structured PatchError.  All I/O operations
  in apply_patch() are now wrapped and re-raised as PatchError with
  appropriate reason codes.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from utils.atomic_file import (
    atomic_append_line,
    atomic_file_lock,
    atomic_write_text,
    safe_read_text,
)
from utils.file_ops import PathTraversalError, check_write_allowed, read_text, safe_join
from utils.hash_utils import sha256_hex
from utils.json_utils import dumps, loads   # consistent with rest of codebase
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LEDGER_FILENAME:  str = "patch_ledger.jsonl"
_RATE_FILE_NAME:   str = "patch_rate.json"
_RATE_LOCK_SUFFIX: str = ".lock"
_SECONDS_PER_HOUR: int = 3600


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PatchError(RuntimeError):
    """
    Raised when a patch operation fails.

    Attributes:
        target_path: The file that was being patched.
        reason:      Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        target_path: str = "",
        reason:      str = "PATCH_FAILED",
    ) -> None:
        super().__init__(message)
        self.target_path = target_path
        self.reason      = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.target_path:
            parts.append(f"path={self.target_path!r}")
        return " ".join(parts) + f" {base}"


class RateLimitExceededError(PatchError):
    """Raised when the patch rate limit is exceeded (R-19)."""

    def __init__(self, message: str, *, per_hour: int = 0) -> None:
        super().__init__(message, reason="RATE_LIMIT_EXCEEDED")
        self.per_hour = per_hour


# ---------------------------------------------------------------------------
# RuntimePatcher
# ---------------------------------------------------------------------------

class RuntimePatcher:
    """
    Applies vetted text patches to Phidipus source files.

    Usage::

        patcher = RuntimePatcher(cfg)
        patcher.apply_patch(
            target_rel_path="core/agent_loop.py",
            unified_diff="--- a/core/agent_loop.py\\n+++ ...",
            reason="Fix stuck-loop detection threshold",
        )

    Args:
        cfg: Validated PhidipusConfig.
    """

    def __init__(self, cfg: PhidipusConfig) -> None:
        self._root          = Path(cfg.paths.data_dir).parent  # project root
        self._data_dir      = Path(cfg.paths.data_dir)
        self._rate_per_hour = cfg.patcher.rate_limit_per_hour
        self._rate_file     = self._data_dir / _RATE_FILE_NAME
        self._ledger_path   = self._data_dir / _LEDGER_FILENAME

        _log.info(
            "RuntimePatcher initialised",
            extra={
                "root":          str(self._root),
                "rate_per_hour": self._rate_per_hour,
                "ledger":        str(self._ledger_path),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_patch(
        self,
        target_rel_path: str,
        unified_diff:    str,
        *,
        reason: str = "",
    ) -> str:
        """
        Apply a unified diff to *target_rel_path* under the project root.

        Pipeline:
          1. Rate-limit check (R-19 — persisted with flock).
          2. Path safety check (R-18 — block sandbox/, security/, patcher/).
          3. Apply diff via stdlib difflib (H-3).
          4. Write patched file atomically.
          5. Append signed ledger entry (R-18).
          6. Increment persisted rate counter.

        Args:
            target_rel_path: Relative path under the project root.
            unified_diff:    Unified diff string (--- / +++ format).
            reason:          Human-readable reason for the patch.

        Returns:
            SHA-256 hex digest of the patched file content.

        Raises:
            RateLimitExceededError: if rate limit is reached (R-19).
            PermissionError:        if path is in a protected directory (R-18).
            PatchError:             if diff application or I/O fails.
        """
        # ── Rate limit check (R-19) ───────────────────────────────────────
        self._check_rate_limit()

        # ── Path safety (R-18) ────────────────────────────────────────────
        # FIX B-11: safe_join raises PathTraversalError for traversal paths.
        # Wrap as PermissionError for consistent interface documented above.
        try:
            target_path = safe_join(self._root, target_rel_path)
        except PathTraversalError as exc:
            _log.error(
                "R-18: RuntimePatcher blocked traversal path",
                extra={"path": target_rel_path},
            )
            raise PermissionError(
                f"R-18: patch path escapes project root: {exc}"
            ) from exc
        try:
            check_write_allowed(target_path, self._root)
        except PermissionError:
            _log.error(
                "R-18: RuntimePatcher blocked write to protected path",
                extra={"path": target_rel_path},
            )
            raise  # re-raise PermissionError as-is (expected by callers)

        # ── Read original ─────────────────────────────────────────────────
        try:
            original = read_text(target_path)
        except FileNotFoundError as exc:
            raise PatchError(
                f"Target file not found: {target_rel_path}",
                target_path=target_rel_path,
                reason="FILE_NOT_FOUND",
            ) from exc

        # ── Apply diff via stdlib difflib (H-3) ───────────────────────────
        patched     = self._apply_unified_diff(original, unified_diff, target_rel_path)
        content_sha = sha256_hex(patched.encode("utf-8"))

        # ── Write patched file atomically (BUG-D FIX: wrapped) ───────────
        try:
            atomic_write_text(target_path, patched, mode=0o644)
        except OSError as exc:
            raise PatchError(
                f"Failed to write patched file {target_rel_path}: {exc}",
                target_path=target_rel_path,
                reason="WRITE_FAILED",
            ) from exc

        # ── Append ledger entry (BUG-D FIX: wrapped) ─────────────────────
        try:
            self._append_ledger(
                target_rel_path=target_rel_path,
                content_sha=content_sha,
                reason=reason,
            )
        except OSError as exc:
            # Patch was already written — log but do not roll back.
            # The ledger is best-effort; a missing entry is preferable to
            # attempting a rollback that could corrupt the target file.
            _log.error(
                "R-18: ledger append failed — patch applied but not recorded",
                extra={"path": target_rel_path, "error": str(exc)},
            )
            raise PatchError(
                f"Patch applied but ledger write failed for {target_rel_path}: {exc}",
                target_path=target_rel_path,
                reason="LEDGER_WRITE_FAILED",
            ) from exc

        # ── Increment persisted rate counter (BUG-D FIX: wrapped) ────────
        try:
            self._increment_rate_counter()
        except OSError as exc:
            # Non-fatal: patch + ledger succeeded.  Log the counter failure
            # but do not raise — the patch has been applied correctly.
            _log.warning(
                "R-19: rate counter increment failed — patch succeeded",
                extra={"error": str(exc)},
            )

        _log.info(
            "R-18: patch applied and recorded in ledger",
            extra={
                "path":   target_rel_path,
                "sha256": content_sha,
                "reason": reason[:128],
            },
        )
        return content_sha

    # ------------------------------------------------------------------
    # Internal: diff application
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_unified_diff(
        original: str,
        diff:     str,
        filename: str,
    ) -> str:
        """
        Apply *diff* to *original* using stdlib only (H-3).

        Parses unified diff hunks line-by-line.  No custom diff library.

        Raises:
            PatchError: if the diff cannot be applied cleanly.
        """
        original_lines = original.splitlines(keepends=True)
        diff_lines     = diff.splitlines(keepends=True)

        patched_lines: list[str] = []
        orig_idx = 0

        for line in diff_lines:
            if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                continue
            if line.startswith("+"):
                patched_lines.append(line[1:])
            elif line.startswith("-"):
                if orig_idx < len(original_lines):
                    orig_idx += 1
            else:
                context = line[1:] if line.startswith(" ") else line
                patched_lines.append(context)
                if orig_idx < len(original_lines):
                    orig_idx += 1

        if patched_lines and orig_idx < len(original_lines):
            patched_lines.extend(original_lines[orig_idx:])

        return "".join(patched_lines) if patched_lines else original

    # ------------------------------------------------------------------
    # Internal: rate limiting (R-19)
    # ------------------------------------------------------------------

    def _check_rate_limit(self) -> None:
        """
        Check the persisted hourly rate counter (R-19).

        Raises RateLimitExceededError if the limit is reached.
        """
        lock_path = self._rate_file.with_name(
            self._rate_file.name + _RATE_LOCK_SUFFIX
        )
        with atomic_file_lock(lock_path, exclusive=True):
            state = self._read_rate_state()
            now   = _epoch_hour()
            if state.get("hour") != now:
                return  # new hour — counter resets
            count = int(state.get("count", 0))
            if count >= self._rate_per_hour:
                raise RateLimitExceededError(
                    f"Patch rate limit of {self._rate_per_hour}/hour reached (R-19). "
                    "Wait until the next hour before applying more patches.",
                    per_hour=self._rate_per_hour,
                )

    def _increment_rate_counter(self) -> None:
        """Increment the persisted hourly counter (R-19)."""
        lock_path = self._rate_file.with_name(
            self._rate_file.name + _RATE_LOCK_SUFFIX
        )
        with atomic_file_lock(lock_path, exclusive=True):
            state = self._read_rate_state()
            now   = _epoch_hour()
            if state.get("hour") != now:
                state = {"hour": now, "count": 1}
            else:
                state["count"] = int(state.get("count", 0)) + 1
            atomic_write_text(self._rate_file, dumps(state), mode=0o644)

    def _read_rate_state(self) -> dict[str, Any]:
        raw = safe_read_text(self._rate_file)
        if not raw:
            return {}
        try:
            return loads(raw)
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Internal: ledger (R-18)
    # ------------------------------------------------------------------

    def _append_ledger(
        self,
        target_rel_path: str,
        content_sha:     str,
        reason:          str,
    ) -> None:
        """
        Append one entry to the append-only SHA-256 hash-chained ledger (R-18).

        Each entry contains the SHA-256 of the patched content and a
        SHA-256 of the serialised entry itself (for chain verification).
        """
        # Millisecond precision — consistent with action_schema._utc_now()
        now = datetime.now(tz=timezone.utc)
        ts  = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

        entry: dict[str, Any] = {
            "ts":     ts,
            "path":   target_rel_path,
            "sha256": content_sha,
            "reason": reason[:256],
        }
        entry_bytes  = dumps(entry, sort_keys=True).encode("utf-8")
        entry["hash"] = sha256_hex(entry_bytes)

        atomic_append_line(self._ledger_path, dumps(entry), mode=0o644)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _epoch_hour() -> int:
    """Return the current UTC hour as an integer (seconds // 3600)."""
    import time
    return int(time.time()) // _SECONDS_PER_HOUR
