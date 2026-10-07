# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
ipc/action_log.py — Phidipus v1.0
Append-only HMAC-SHA256 signed audit log of all IPC actions dispatched.

Architecture contract (C.3):
  "Append-only HMAC-signed audit log of all actions dispatched.
   Write-only for daemon. Orchestrator may not modify audit log."

ActionLog is called by IPCServer immediately after a message passes
validate_request() and before OSController.dispatch() is invoked.  Every
dispatched action — successful or not — produces exactly one audit entry.

On-disk format
--------------
The log is a newline-delimited JSON file (one entry per line).  Each line
is a self-contained JSON object::

    {
      "ts":             "2026-03-14T01:23:45.123Z",
      "action":         "mouse_click",
      "cid":            "550e8400-e29b-41d4-a716-446655440000",
      "payload_sha256": "a3f1...",
      "hmac":           "7b2c..."
    }

HMAC coverage
-------------
The HMAC-SHA256 is computed over the UTF-8 encoding of::

    <ts>|<action>|<cid>|<payload_sha256>

using the 32-byte key from ``keys/memory_hmac.key`` (same key store as
MemoryGuard — one key file serves both subsystems to minimize key
management surface).

The raw payload is **not** stored in the log to:
  (a) keep the log compact and greppable, and
  (b) avoid leaking sensitive keyboard/clipboard content to any process
      that can read the log file.

The ``payload_sha256`` field allows an operator to verify that the payload
of a specific logged action matches a retained copy, without the log
itself needing to store the payload.

Cross-process safety
--------------------
``atomic_append_line()`` from ``utils/atomic_file.py`` acquires an
exclusive ``fcntl.flock`` on a sibling ``<log_file>.lock`` file for the
full duration of its read-modify-write cycle (FIX-5).  This makes
``ActionLog.append()`` safe to call from multiple daemon processes on the
same log file simultaneously.

Process: automation_daemon (L2)

Security invariants enforced here:
  R-03  No LLMClient import or LLM calls of any kind.
  R-05  Every IPC action is logged (called by IPCServer after validation).
        The log entry is written BEFORE OSController.dispatch() executes.
  R-19  Append uses atomic_append_line() with fcntl.flock — not an in-
        memory counter (R-19 scope: rate-limit state; this satisfies the
        same durability expectation for audit state).

Used by:
  ipc/ipc_server.py  — await action_log.append(msg) before dispatch
  (operator tooling) — verify_log() for post-hoc integrity checking

Dependencies:
  utils/hash_utils.py   — hmac_sign(), sha256_hex()
  utils/atomic_file.py  — atomic_append_line() (cross-process flock, FIX-5)
  utils/json_utils.py   — dumps(), loads() for entry serialisation and verification
  utils/logger.py       — get_logger()
  ipc/action_schema.py  — _utc_now() (single source of truth for timestamp format)
  config/config_loader.py — PhidipusConfig (cfg.paths.data_dir,
                             cfg.memory.hmac_key_file)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from ipc.action_schema import _utc_now   # single source of truth for UTC timestamp format
from utils.atomic_file import atomic_append_line
from utils.hash_utils import hmac_sign, hmac_verify, sha256_hex
from utils.json_utils import dumps, loads, JsonDecodeError
from utils.logger import get_logger

_log = get_logger(__name__, process="daemon")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default log filename inside cfg.paths.data_dir.
_DEFAULT_LOG_FILENAME: str = "action_audit.log"

#: Separator used in the HMAC input string.
#: Must be a character that cannot appear in action names, CIDs, or
#: ISO-8601 timestamps.  Pipe is safe here.
_HMAC_SEP: str = "|"

#: HMAC-SHA256 produces 32 bytes → 64 hex chars.
_HMAC_HEX_LEN: int = 64

#: SHA-256 produces 32 bytes → 64 hex chars.
_SHA256_HEX_LEN: int = 64


# ---------------------------------------------------------------------------
# Log entry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LogEntry:
    """
    Immutable representation of one audit log line.

    Attributes:
        ts:             ISO-8601 UTC timestamp with millisecond precision.
        action:         IPC action name (e.g. ``"mouse_click"``).
        cid:            UUID4 correlation ID from the IPC message envelope.
        payload_sha256: Lowercase hex SHA-256 of the raw payload JSON bytes.
        hmac:           Lowercase hex HMAC-SHA256 over
                        ``<ts>|<action>|<cid>|<payload_sha256>``.
    """

    ts:             str
    action:         str
    cid:            str
    payload_sha256: str
    hmac:           str

    def to_dict(self) -> dict[str, str]:
        """Return a plain dict suitable for JSON serialisation."""
        return {
            "ts":             self.ts,
            "action":         self.action,
            "cid":            self.cid,
            "payload_sha256": self.payload_sha256,
            "hmac":           self.hmac,
        }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ActionLogError(RuntimeError):
    """
    Raised when ActionLog cannot write a log entry.

    A write failure is non-fatal for the IPC dispatch pipeline (IPCServer
    continues to dispatch after logging an ERROR), but it must be surfaced
    as a distinct exception type so the caller can alert monitoring.

    Attributes:
        action:  The IPC action that could not be logged.
        reason:  Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        reason: str = "APPEND_FAILED",
    ) -> None:
        super().__init__(message)
        self.action = action
        self.reason = reason

    def __str__(self) -> str:
        base = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.action:
            parts.append(f"action={self.action!r}")
        return " ".join(parts) + f" {base}"


class LogVerificationError(RuntimeError):
    """
    Raised by ``ActionLog.verify_log()`` when a log entry fails HMAC
    verification, indicating the log file may have been tampered with.

    Attributes:
        line_number:  1-based line number of the corrupt entry.
        entry_action: The ``action`` field of the suspect entry (if parseable).
        entry_cid:    The ``cid`` field of the suspect entry (if parseable).
        reason:       Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        line_number:  int = 0,
        entry_action: str = "",
        entry_cid:    str = "",
        reason:       str = "HMAC_INVALID",
    ) -> None:
        super().__init__(message)
        self.line_number  = line_number
        self.entry_action = entry_action
        self.entry_cid    = entry_cid
        self.reason       = reason

    def __str__(self) -> str:
        base = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.line_number:
            parts.append(f"line={self.line_number}")
        if self.entry_action:
            parts.append(f"action={self.entry_action!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# ActionLog
# ---------------------------------------------------------------------------

class ActionLog:
    """
    Append-only HMAC-SHA256 signed IPC action audit log.

    Each call to ``append()`` writes one JSON line to the log file.  The
    log is safe for concurrent writes from multiple processes (protected by
    ``fcntl.flock`` via ``atomic_append_line()``).

    No read or delete methods are exposed.  The only mutation operation
    is ``append()``.  Post-hoc integrity verification is available via
    the class method ``verify_log()``.

    Thread / asyncio safety:
        ``append()`` is an ``async`` method that offloads the blocking
        ``atomic_append_line()`` call to a thread pool via
        ``asyncio.to_thread()``.  This keeps the daemon's event loop
        unblocked while the flock is held.

    Args:
        cfg:      Validated PhidipusConfig.  The log file path is derived
                  from ``cfg.paths.data_dir``.  The HMAC key is loaded from
                  ``cfg.memory.hmac_key_file``.
        filename: Log filename relative to ``cfg.paths.data_dir``.
                  Defaults to ``_DEFAULT_LOG_FILENAME``.
    """

    def __init__(
        self,
        cfg:      PhidipusConfig,
        filename: str = _DEFAULT_LOG_FILENAME,
    ) -> None:
        # ── Resolve log file path ─────────────────────────────────────────
        data_dir = Path(cfg.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._log_path: Path = data_dir / filename

        # ── Load HMAC key ─────────────────────────────────────────────────
        hmac_key_path = Path(cfg.memory.hmac_key_file)
        if not hmac_key_path.exists():
            raise FileNotFoundError(
                f"HMAC key file not found: {hmac_key_path}. "
                "Run the installer to generate keys before starting the daemon."
            )
        self._hmac_key: bytes = hmac_key_path.read_bytes()
        if len(self._hmac_key) != 32:
            raise ValueError(
                f"HMAC key at {hmac_key_path} is {len(self._hmac_key)} bytes; "
                "expected exactly 32 bytes.  The key file may be corrupt."
            )

        _log.info(
            "ActionLog initialised",
            extra={
                "log_path":      str(self._log_path),
                "hmac_key_path": str(hmac_key_path),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def log_path(self) -> Path:
        """Resolved path to the audit log file (read-only)."""
        return self._log_path

    async def append(self, msg: dict[str, Any]) -> LogEntry:
        """
        Build a signed log entry from *msg* and append it to the audit log.

        This method is ``async`` and offloads the blocking ``atomic_append_line()``
        call to a thread pool so it does not block the daemon's event loop
        while holding the flock.

        Entry construction:
          1. Extract ``action``, ``correlation_id``, ``timestamp`` from envelope.
          2. Compute SHA-256 of the serialised payload (``msg["payload"]``).
          3. Build HMAC input string: ``<ts>|<action>|<cid>|<payload_sha256>``.
          4. Compute HMAC-SHA256 over the input string.
          5. Serialise the entry to a single JSON line.
          6. Append atomically with cross-process flock.

        Args:
            msg: Validated IPC message dict (already passed validate_request()).
                 Must have ``action``, ``correlation_id``, ``timestamp``, and
                 ``payload`` fields.

        Returns:
            The ``LogEntry`` that was written (for test assertions / tracing).

        Raises:
            ActionLogError: if the entry cannot be written (I/O failure,
                            serialisation error).  The caller (IPCServer)
                            must log this as a WARNING and continue dispatch.
        """
        if not isinstance(msg, dict):
            raise ActionLogError(
                f"append() requires a dict, got {type(msg).__name__!r}.",
                reason="INVALID_INPUT",
            )

        action = msg.get("action", "unknown")

        try:
            entry = self._build_entry(msg)
        except Exception as exc:
            raise ActionLogError(
                f"Failed to build log entry for action {action!r}: {exc}",
                action=action,
                reason="BUILD_FAILED",
            ) from exc

        # ── Serialise to one JSON line ────────────────────────────────────
        try:
            line = dumps(entry.to_dict())
        except ValueError as exc:
            raise ActionLogError(
                f"Log entry for action {action!r} is not JSON-serialisable: {exc}",
                action=action,
                reason="SERIALISE_FAILED",
            ) from exc

        # ── Offload blocking I/O + flock to thread pool ───────────────────
        try:
            await asyncio.to_thread(
                atomic_append_line,
                self._log_path,
                line,
                mode=0o600,
            )
        except Exception as exc:
            raise ActionLogError(
                f"Failed to append audit log entry for action {action!r}: {exc}",
                action=action,
                reason="APPEND_FAILED",
            ) from exc

        _log.debug(
            "R-05: action logged to audit trail",
            extra={
                "action": entry.action,
                "cid":    entry.cid,
                "ts":     entry.ts,
            },
        )
        return entry

    @classmethod
    def verify_log(
        cls,
        log_path: Path,
        hmac_key: bytes,
    ) -> int:
        """
        Verify every HMAC in the log file.

        Reads each line, parses the JSON entry, recomputes the HMAC, and
        compares it against the stored value.  Raises ``LogVerificationError``
        on the first invalid entry.

        This is an operator / test utility.  It is NOT called in the normal
        daemon hot path.

        Args:
            log_path: Path to the audit log file.
            hmac_key: 32-byte HMAC key (same as used to write the log).

        Returns:
            Number of entries successfully verified.

        Raises:
            LogVerificationError: on the first entry that fails HMAC
                                  verification or cannot be parsed.
            FileNotFoundError:    if ``log_path`` does not exist.
        """
        path = Path(log_path)
        if not path.exists():
            raise FileNotFoundError(f"Audit log not found: {path}")

        verified = 0
        for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            # Parse JSON via utils.json_utils (consistent with rest of codebase)
            try:
                entry_dict = loads(raw_line)
            except JsonDecodeError as exc:
                raise LogVerificationError(
                    f"Line {lineno} is not valid JSON: {exc}",
                    line_number=lineno,
                    reason="JSON_CORRUPT",
                ) from exc

            if not isinstance(entry_dict, dict):
                raise LogVerificationError(
                    f"Line {lineno} is not a JSON object.",
                    line_number=lineno,
                    reason="NOT_OBJECT",
                )

            # Extract fields
            for field in ("ts", "action", "cid", "payload_sha256", "hmac"):
                if field not in entry_dict:
                    raise LogVerificationError(
                        f"Line {lineno} is missing field {field!r}.",
                        line_number=lineno,
                        entry_action=entry_dict.get("action", ""),
                        entry_cid=entry_dict.get("cid", ""),
                        reason="MISSING_FIELD",
                    )

            stored_hmac_hex = entry_dict["hmac"]
            if (not isinstance(stored_hmac_hex, str) or
                    len(stored_hmac_hex) != _HMAC_HEX_LEN):
                raise LogVerificationError(
                    f"Line {lineno}: 'hmac' field has unexpected format.",
                    line_number=lineno,
                    entry_action=entry_dict["action"],
                    entry_cid=entry_dict["cid"],
                    reason="HMAC_FORMAT",
                )

            # Recompute HMAC
            hmac_input = _build_hmac_input(
                ts=entry_dict["ts"],
                action=entry_dict["action"],
                cid=entry_dict["cid"],
                payload_sha256=entry_dict["payload_sha256"],
            )
            try:
                stored_mac = bytes.fromhex(stored_hmac_hex)
            except ValueError as exc:
                raise LogVerificationError(
                    f"Line {lineno}: 'hmac' field is not valid hex: {exc}",
                    line_number=lineno,
                    entry_action=entry_dict["action"],
                    entry_cid=entry_dict["cid"],
                    reason="HMAC_FORMAT",
                ) from exc

            if not hmac_verify(hmac_key, hmac_input, stored_mac):
                raise LogVerificationError(
                    f"Line {lineno}: HMAC verification FAILED for action "
                    f"{entry_dict['action']!r} cid={entry_dict['cid']!r}. "
                    "The audit log may have been tampered with.",
                    line_number=lineno,
                    entry_action=entry_dict["action"],
                    entry_cid=entry_dict["cid"],
                    reason="HMAC_INVALID",
                )

            verified += 1

        _log.info(
            "Audit log verification complete",
            extra={
                "log_path": str(log_path),
                "entries":  verified,
            },
        )
        return verified

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_entry(self, msg: dict[str, Any]) -> LogEntry:
        """
        Build a ``LogEntry`` from a validated IPC message dict.

        Steps:
          1. Extract timestamp from message envelope (or generate now).
          2. Extract action name and correlation_id.
          3. Serialise payload with sorted keys for deterministic SHA-256.
          4. Compute payload SHA-256.
          5. Build HMAC input string.
          6. Compute HMAC-SHA256.
          7. Return LogEntry.

        Args:
            msg: Validated IPC message dict.

        Returns:
            Signed LogEntry ready for serialisation.
        """
        # ── Extract envelope fields ───────────────────────────────────────
        # Use the message's own timestamp if present; fall back to now.
        # This keeps the log entry timestamp consistent with the IPC message
        # timestamp rather than the wall-clock time of the log write, which
        # could differ by milliseconds under event-loop scheduling.
        ts  = msg.get("timestamp") or _utc_now()
        action = str(msg.get("action", "unknown"))
        cid    = str(msg.get("correlation_id", ""))

        # ── Payload SHA-256 ───────────────────────────────────────────────
        # Serialize with sort_keys=True for deterministic bytes.
        # The raw payload is NOT stored in the log (see module docstring).
        payload = msg.get("payload", {})
        payload_json = dumps(payload, sort_keys=True)
        payload_sha256 = sha256_hex(payload_json.encode("utf-8"))

        # ── HMAC ─────────────────────────────────────────────────────────
        hmac_input = _build_hmac_input(
            ts=ts,
            action=action,
            cid=cid,
            payload_sha256=payload_sha256,
        )
        mac: bytes = hmac_sign(self._hmac_key, hmac_input)
        mac_hex: str = mac.hex()

        return LogEntry(
            ts=ts,
            action=action,
            cid=cid,
            payload_sha256=payload_sha256,
            hmac=mac_hex,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _build_hmac_input(
    *,
    ts:             str,
    action:         str,
    cid:            str,
    payload_sha256: str,
) -> bytes:
    """
    Build the canonical HMAC input bytes from the four log-entry fields.

    Format: ``<ts>|<action>|<cid>|<payload_sha256>`` encoded as UTF-8.

    Using a pipe-delimited string prevents length-extension confusion
    between fields (e.g. ``ts="a|b"`` and ``action="c"`` vs
    ``ts="a"`` and ``action="b|c"``).  All four fields are fixed-format
    (ISO-8601 timestamp, alphanumeric action name, UUID4 CID, hex SHA-256),
    so the pipe character cannot appear in any of them.

    Args:
        ts:             ISO-8601 UTC timestamp.
        action:         IPC action name.
        cid:            UUID4 correlation ID.
        payload_sha256: Lowercase hex SHA-256 of the payload JSON bytes.

    Returns:
        UTF-8 bytes suitable for passing to ``hmac_sign()`` or
        ``hmac_verify()``.
    """
    canonical = _HMAC_SEP.join([ts, action, cid, payload_sha256])
    return canonical.encode("utf-8")

