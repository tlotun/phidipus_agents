# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
ipc/ipc_server.py — Phidipus v1.0
Unix domain socket IPC server running in the automation_daemon process (L2).

Architecture contract (C.3):
  "Rejects any message failing schema. Logs all messages. Rate-limits."

This server is the *sole* entry point through which the Orchestrator (L1)
dispatches actions to the Automation Daemon (L2).  Every inbound message is
processed in strict order:

  1. Size-checked against cfg.ipc.max_message_bytes before JSON parsing.
  2. Decoded from UTF-8 JSON (JsonDecodeError → silent drop, no correlation_id).
  3. Rate-limited at cfg.ipc.rate_limit_per_second (token-bucket, global).
  4. Validated against the full IPC schema via validate_request() (R-05).
  5. Appended to the ActionLog HMAC audit trail (R-05 support).
  6. Dispatched to OSController.dispatch(action, payload).
  7. Result serialised and written back as a RESPONSE message.

Every failure path that has a valid correlation_id sends a typed ERROR response
so the Orchestrator can handle failures gracefully.  Failures without a
parseable correlation_id close the connection silently.

Security invariants enforced here:
  R-03  No LLMClient import; no LLM API calls of any kind.
  R-04  Server binds only to a Unix domain socket (no TCP/UDP).
        Socket file is created with mode 0o600 (owner read/write only).
  R-05  OSController.dispatch() is called only after validate_request() passes.
        Invalid messages receive an ERROR response and are never dispatched.
  R-10  All wire communication is UTF-8 JSON.  No raw Python objects cross the
        socket boundary.

Process: automation_daemon (L2)

Used by:
  automation/os_controller.py  — receives dispatched (action, payload) calls
  ipc/ipc_client.py            — the Orchestrator-side counterpart

Dependencies:
  ipc/action_schema.py   — validate_request(), make_error(), make_response(),
                           IPCValidationError
  ipc/action_log.py      — ActionLog (append-only HMAC audit log; injected)
  utils/json_utils.py    — load_bytes(), dumps_bytes(), JsonDecodeError
  utils/logger.py        — get_logger()
  config/config_loader.py — PhidipusConfig (cfg.ipc.*)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from config.config_loader import PhidipusConfig
from ipc.action_log import ActionLog
from ipc.action_schema import (
    IPCValidationError,
    make_error,
    make_response,
    validate_request,
)
from utils.json_utils import JsonDecodeError, dumps_bytes, load_bytes
from utils.logger import get_logger

# ---------------------------------------------------------------------------
# TYPE_CHECKING-only imports
# ---------------------------------------------------------------------------
# OSController lives in automation/ which is implemented in Phase 4.
# ActionLog (ipc/action_log.py) is a Phase 1 module and is imported directly
# above — it no longer needs to be deferred.

if TYPE_CHECKING:
    from automation.os_controller import OSController  # type: ignore[import]

_log = get_logger(__name__, process="daemon")

# ── [C-10 FIX] IPC per-message HMAC verification ─────────────────
_IPC_HMAC_KEY_PATH = Path("data/memory/.ipc_hmac.key")
_IPC_MSG_TIMESTAMP_TOLERANCE = 30  # seconds


def _get_ipc_hmac_key() -> bytes:
    if _IPC_HMAC_KEY_PATH.exists():
        try:
            raw = _IPC_HMAC_KEY_PATH.read_bytes()
            if len(raw) >= 32:
                return raw
        except Exception:
            pass
    key = os.urandom(32)
    try:
        _IPC_HMAC_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _IPC_HMAC_KEY_PATH.write_bytes(key)
        _IPC_HMAC_KEY_PATH.chmod(0o600)
    except Exception:
        pass
    return key


# ── SEC-R7: Nonce tracker — prevents replay within the 30s HMAC window ─────
class _NonceTracker:
    """
    Track processed correlation_ids to prevent replay attacks.

    The HMAC timestamp check (±30s) prevents replaying messages that are
    older than 30 seconds.  Without a nonce tracker, the same HMAC-valid
    message can be replayed unlimited times within that 30-second window.

    This tracker maintains a dict of {correlation_id: timestamp} and
    rejects any id seen before.  Expired entries (older than the tolerance
    window + 5s safety margin) are purged lazily on each check.
    """

    def __init__(self, window_seconds: int = _IPC_MSG_TIMESTAMP_TOLERANCE + 5) -> None:
        self._window = window_seconds
        self._seen: dict[str, float] = {}

    def check_and_mark(self, correlation_id: str) -> bool:
        """
        Return True if this correlation_id is fresh (not seen before).
        Return False if it is a replay.
        Side-effect: marks the id as seen on True return.
        """
        if not correlation_id:
            return True  # empty id — let HMAC/schema handle it
        now = time.time()
        # Lazy cleanup of expired nonces
        expired = [k for k, t in self._seen.items() if now - t > self._window]
        for k in expired:
            del self._seen[k]
        if correlation_id in self._seen:
            return False  # replay detected
        self._seen[correlation_id] = now
        return True


def _verify_ipc_hmac(msg: dict) -> bool:
    """C-10: Verify per-message HMAC. Rejects tampered or stale messages."""
    stored_sig = msg.get("_hmac", "")
    msg_ts = msg.get("_ts", 0)
    if not stored_sig or not msg_ts:
        return False
    if abs(time.time() - int(msg_ts)) > _IPC_MSG_TIMESTAMP_TOLERANCE:
        return False
    key = _get_ipc_hmac_key()
    payload = {k: v for k, v in msg.items() if k != "_hmac"}
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    expected = hmac.new(key, data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, stored_sig)

# ---------------------------------------------------------------------------
# Error codes (machine-readable; sent in ERROR response payloads)
# ---------------------------------------------------------------------------

_EC_RATE_LIMIT   = "RATE_LIMIT_EXCEEDED"
_EC_MSG_TOO_LARGE = "MESSAGE_TOO_LARGE"
_EC_JSON_DECODE  = "JSON_DECODE_ERROR"
_EC_DISPATCH_ERR = "DISPATCH_ERROR"
_EC_AUDIT_WARN   = "AUDIT_LOG_FAILURE"   # non-fatal; logged, not sent


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Token-bucket rate limiter for the IPC server.

    Tokens are added continuously at *rate_per_second* tokens/s up to a
    maximum of *rate_per_second* tokens (i.e. burst == 1 second's worth).
    Each accepted message consumes one token.

    This implementation is NOT thread-safe.  It is designed for use inside
    a single-threaded asyncio event loop (one call to ``allow()`` at a time).

    Args:
        rate_per_second: Maximum number of messages accepted per second.
                         Must be >= 1.
    """

    __slots__ = ("_rate", "_tokens", "_last_refill")

    def __init__(self, rate_per_second: int) -> None:
        if rate_per_second < 1:
            raise ValueError(
                f"rate_per_second must be >= 1, got {rate_per_second}"
            )
        self._rate: float = float(rate_per_second)
        self._tokens: float = float(rate_per_second)  # start full
        self._last_refill: float = time.monotonic()

    def allow(self) -> bool:
        """
        Consume one token and return True if the message is permitted,
        False if the rate limit is exceeded.

        Tokens are refilled continuously based on elapsed time since the
        last call.  The bucket is capped at ``rate_per_second`` tokens to
        prevent large burst accumulation during idle periods.
        """
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._rate,
            self._tokens + elapsed * self._rate,
        )
        self._last_refill = now

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def tokens_available(self) -> float:
        """Current token level (informational; not synchronized)."""
        return self._tokens


# ---------------------------------------------------------------------------
# Server statistics (read-only snapshot)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IPCServerStats:
    """
    Immutable snapshot of IPC server counters.

    Returned by :attr:`IPCServer.stats` for monitoring / health-check use.

    Attributes:
        messages_accepted:  Total messages that passed validation and were
                            dispatched to OSController.
        messages_rejected:  Total messages rejected for any reason (schema
                            violation, size, JSON error, rate limit).
        rate_limit_drops:   Subset of rejected: messages dropped due to
                            rate limit.
        active_connections: Number of connections currently being processed.
        dispatch_errors:    Number of times OSController.dispatch() raised.
    """

    messages_accepted:  int = 0
    messages_rejected:  int = 0
    rate_limit_drops:   int = 0
    active_connections: int = 0
    dispatch_errors:    int = 0


# ---------------------------------------------------------------------------
# IPCServer
# ---------------------------------------------------------------------------

class IPCServer:
    """
    Async Unix domain socket server for the IPC L1→L2 trust boundary.

    One instance should run for the lifetime of the automation_daemon
    process.  It binds to the socket path declared in config, accepts
    connections from the Orchestrator, and dispatches validated action
    messages to OSController.

    Each client connection carries exactly one request-response pair.
    The connection is closed after the response (or error) is written.
    This keeps the server stateless per-connection and simplifies error
    recovery.

    Lifecycle (async context manager preferred)::

        async with IPCServer(cfg, controller, action_log=log) as server:
            await asyncio.Event().wait()   # run until cancelled

    Or manual::

        server = IPCServer(cfg, controller, action_log=log)
        await server.start()
        try:
            await asyncio.Event().wait()
        finally:
            await server.stop()

    Args:
        cfg:          Validated PhidipusConfig.  IPC parameters are read
                      from cfg.ipc.*.
        os_controller: OSController instance (L2 only; injected to avoid
                      importing automation.* at module level in IPC layer).
        action_log:   Optional ActionLog instance.  When None, the audit
                      log step is skipped (Phase 1 compatibility).  In
                      production this must be provided.

    Raises:
        RuntimeError: if start() is called more than once without an
                      intervening stop().
    """

    def __init__(
        self,
        cfg: PhidipusConfig,
        os_controller: Any,        # OSController — injected; type checked below
        *,
        action_log: ActionLog | None = None,
    ) -> None:
        self._socket_path    = Path(cfg.ipc.socket_path)
        self._max_bytes: int = cfg.ipc.max_message_bytes
        self._recv_timeout: float = cfg.ipc.recv_timeout_seconds
        self._backlog: int   = cfg.ipc.backlog
        self._rate_limiter   = _RateLimiter(cfg.ipc.rate_limit_per_second)
        self._os_controller  = os_controller
        self._action_log     = action_log
        self._server: asyncio.AbstractServer | None = None
        # SEC-R7: Nonce tracker — prevents replay within the 30s HMAC window
        self._nonce_tracker  = _NonceTracker()

        # Mutable counters — only modified inside the event loop (single-threaded)
        self._n_accepted:     int = 0
        self._n_rejected:     int = 0
        self._n_rate_drops:   int = 0
        self._n_active:       int = 0
        self._n_dispatch_err: int = 0

        _log.info(
            "IPCServer configured",
            extra={
                "socket_path":   str(self._socket_path),
                "max_bytes":     self._max_bytes,
                "recv_timeout":  self._recv_timeout,
                "backlog":       self._backlog,
                "rate_limit":    cfg.ipc.rate_limit_per_second,
                "action_log":    action_log is not None,
            },
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Bind to the Unix domain socket and begin accepting connections.

        Removes a stale socket file if one exists from a previous run.
        Sets socket file permissions to 0o600 (owner read/write only)
        after binding — R-04: only the daemon's own user can connect.

        Raises:
            RuntimeError: if the server is already running.
            OSError:      if the socket cannot be created or bound.
        """
        if self._server is not None:
            raise RuntimeError(
                "IPCServer.start() called while already running. "
                "Call stop() first."
            )

        # Remove stale socket file from a previous (crashed) daemon run.
        # This is safe: if another process is still bound to this path,
        # unlink() succeeds but the other process keeps its fd; our bind()
        # below will succeed on the new path entry.
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
                _log.info(
                    "Removed stale IPC socket file",
                    extra={"socket_path": str(self._socket_path)},
                )
            except OSError as exc:
                _log.warning(
                    "Could not remove stale socket file — attempting bind anyway",
                    extra={
                        "socket_path": str(self._socket_path),
                        "error":       str(exc),
                    },
                )

        # Ensure parent directory exists (it should, but be defensive)
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._socket_path),
            backlog=self._backlog,
        )

        # Restrict access: only the daemon process owner may connect (R-04).
        try:
            os.chmod(str(self._socket_path), 0o600)
        except OSError as exc:
            # Non-fatal: log and continue — the socket is still functional.
            _log.warning(
                "Could not set socket file permissions to 0o600",
                extra={
                    "socket_path": str(self._socket_path),
                    "error":       str(exc),
                },
            )

        _log.info(
            "IPCServer started — listening for connections",
            extra={"socket_path": str(self._socket_path)},
        )

    async def stop(self) -> None:
        """
        Stop accepting new connections, close the server, and remove the
        socket file.

        In-flight connections are allowed to complete normally (asyncio
        wait_closed handles this).
        """
        if self._server is None:
            return

        self._server.close()
        await self._server.wait_closed()
        self._server = None

        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                "Could not remove socket file on shutdown",
                extra={
                    "socket_path": str(self._socket_path),
                    "error":       str(exc),
                },
            )

        _log.info(
            "IPCServer stopped",
            extra={
                "accepted":    self._n_accepted,
                "rejected":    self._n_rejected,
                "rate_drops":  self._n_rate_drops,
                "dispatch_err": self._n_dispatch_err,
            },
        )

    async def __aenter__(self) -> "IPCServer":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def stats(self) -> IPCServerStats:
        """Return an immutable snapshot of server counters."""
        return IPCServerStats(
            messages_accepted  = self._n_accepted,
            messages_rejected  = self._n_rejected,
            rate_limit_drops   = self._n_rate_drops,
            active_connections = self._n_active,
            dispatch_errors    = self._n_dispatch_err,
        )

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Handle a single client connection: read one request, write one
        response, then close.

        This method is registered as the asyncio connection callback.
        Each call runs concurrently for each accepted connection, but
        because we use a single-threaded event loop, the rate limiter
        and counter mutations are safe without locks.

        Processing pipeline (failures at any step return early):
          1. Rate limit check
          2. Read raw bytes with recv_timeout
          3. Size validation
          4. JSON decode
          5. Schema validation via validate_request()   ← R-05 gate
          6. Audit log append                           ← R-05 support
          7. Dispatch to OSController
          8. Send RESPONSE
        """
        self._n_active += 1

        _log.debug(
            "IPC connection accepted",
            extra={"active_connections": self._n_active},
        )

        try:
            await self._process_one_request(reader, writer)
        except Exception as exc:
            # Catch-all: unexpected exceptions must not crash the server.
            _log.error(
                "Unexpected exception in IPC connection handler",
                extra={"error": str(exc)},
                exc_info=True,
            )
        finally:
            self._n_active -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    async def _process_one_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Core request-processing logic, extracted for testability.

        All early-return paths increment self._n_rejected.  The happy
        path increments self._n_accepted.
        """

        # ── Step 1: Rate limit ────────────────────────────────────────────
        # Check before reading bytes to avoid wasting I/O on flood.
        # Without a correlation_id we cannot send a proper typed ERROR
        # response, so we close silently — consistent with network-layer
        # rate limiting behaviour.
        if not self._rate_limiter.allow():
            self._n_rejected   += 1
            self._n_rate_drops += 1
            _log.warning(
                "R-05: IPC rate limit exceeded — dropping connection",
                extra={
                    "rate_limit_drops": self._n_rate_drops,
                    "tokens_available": self._rate_limiter.tokens_available,
                },
            )
            return

        # ── Step 2: Read raw bytes with timeout ───────────────────────────
        # Read up to max_bytes + 1 so we can detect oversized messages.
        # FIX v4.3: a single read() may return only part of a large message
        # (e.g. keyboard_type with a long Vietnamese text).  The client calls
        # write_eof(), so keep reading until EOF or until the size cap.
        async def _read_all() -> bytes:
            chunks: list[bytes] = []
            total = 0
            while total <= self._max_bytes:
                chunk = await reader.read(self._max_bytes + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return b"".join(chunks)

        try:
            raw = await asyncio.wait_for(_read_all(), timeout=self._recv_timeout)
        except asyncio.TimeoutError:
            self._n_rejected += 1
            _log.warning(
                "IPC connection timed out waiting for message",
                extra={"recv_timeout": self._recv_timeout},
            )
            return

        if not raw:
            # Client connected but sent nothing — drop silently.
            return

        # ── Step 3: Size validation ───────────────────────────────────────
        # A message that is exactly max_bytes is acceptable; one that
        # caused read() to return max_bytes + 1 bytes is not.
        if len(raw) > self._max_bytes:
            self._n_rejected += 1
            _log.error(
                "R-05: IPC message too large — rejecting before JSON parse",
                extra={
                    "received_bytes": len(raw),
                    "max_bytes":      self._max_bytes,
                    "code":           _EC_MSG_TOO_LARGE,
                },
            )
            # No correlation_id available; close without ERROR response.
            return

        # ── Step 4: JSON decode ───────────────────────────────────────────
        try:
            msg = load_bytes(raw)
        except JsonDecodeError as exc:
            self._n_rejected += 1
            _log.error(
                "R-05: IPC message JSON decode failed — rejecting",
                extra={
                    "error": str(exc),
                    "code":  _EC_JSON_DECODE,
                },
            )
            # No valid message dict to extract correlation_id from.
            return

        # From here we have a parsed object.  Extract fields defensively
        # (validate_request will enforce structure, but we need correlation_id
        # for error responses before that point).
        correlation_id: str = (
            msg.get("correlation_id", "") if isinstance(msg, dict) else ""
        )
        action: str = (
            msg.get("action", "unknown") if isinstance(msg, dict) else "unknown"
        )

        # ── Step 5: Schema validation (R-05) ─────────────────────────────
        try:
            validate_request(msg, max_bytes=self._max_bytes)
        except IPCValidationError as exc:
            self._n_rejected += 1
            _log.error(
                "R-05: IPC message schema validation failed — rejecting",
                extra={
                    "action":     action,
                    "cid":        correlation_id,
                    "error_code": exc.code,
                    "field":      exc.field,
                    "error":      str(exc),
                },
            )
            # Send a typed ERROR response if we have a correlation_id.
            if correlation_id:
                await self._send_error(
                    writer,
                    action=action,
                    correlation_id=correlation_id,
                    code=exc.code,
                    message=str(exc),
                    field=exc.field,
                )
            return

        # ── Step 5b: [C-10 FIX] HMAC per-message authentication ──────────
        if not _verify_ipc_hmac(msg):
            self._n_rejected += 1
            _log.error(
                "[C-10] IPC message HMAC verification failed — possible tampering or replay",
                extra={"action": action, "cid": correlation_id},
            )
            if correlation_id:
                await self._send_error(
                    writer,
                    action=action,
                    correlation_id=correlation_id,
                    code="HMAC_VERIFICATION_FAILED",
                    message="Message authentication failed",
                    field="_hmac",
                )
            return

        # ── Step 5c: [SEC-R7] Nonce / replay deduplication ───────────────
        if not self._nonce_tracker.check_and_mark(correlation_id):
            self._n_rejected += 1
            _log.error(
                "[SEC-R7] IPC replay detected — duplicate correlation_id within window",
                extra={"action": action, "cid": correlation_id},
            )
            if correlation_id:
                await self._send_error(
                    writer,
                    action=action,
                    correlation_id=correlation_id,
                    code="REPLAY_DETECTED",
                    message="Duplicate message: replay attack rejected",
                    field="correlation_id",
                )
            return

        # ── Step 6: Audit log (R-05 support) ─────────────────────────────
        # Non-fatal: a log failure must not block dispatch, but it must be
        # surfaced loudly so operators know the audit trail has a gap.
        if self._action_log is not None:
            try:
                await self._action_log.append(msg)
            except Exception as exc:
                _log.error(
                    "ActionLog.append() failed — audit trail has a gap",
                    extra={
                        "action": action,
                        "cid":    correlation_id,
                        "error":  str(exc),
                        "code":   _EC_AUDIT_WARN,
                    },
                )
                # Continue dispatch — an audit failure is not a security
                # violation that should stop the user's action, but it must
                # be investigated.  Alert monitoring should fire on this log
                # line.
        else:
            # No ActionLog injected — expected during Phase 1 integration.
            _log.debug(
                "ActionLog not configured — skipping audit log step",
                extra={"action": action, "cid": correlation_id},
            )

        # ── Step 7: Dispatch to OSController ─────────────────────────────
        payload: dict[str, Any] = msg["payload"]

        try:
            result = await self._os_controller.dispatch(action, payload)
        except Exception as exc:
            self._n_dispatch_err += 1
            self._n_rejected     += 1
            _log.error(
                "OSController.dispatch() raised an exception",
                extra={
                    "action": action,
                    "cid":    correlation_id,
                    "error":  str(exc),
                    "code":   _EC_DISPATCH_ERR,
                },
                exc_info=True,
            )
            await self._send_error(
                writer,
                action=action,
                correlation_id=correlation_id,
                code=_EC_DISPATCH_ERR,
                message=f"Action dispatch failed: {exc}",
            )
            return

        # ── Step 8: Send RESPONSE ─────────────────────────────────────────
        # make_response() calls validate_response() internally, which calls
        # dumps() to check serializability.  If the dispatch result contains
        # a non-JSON-serializable object, make_response() raises
        # IPCValidationError.  dumps_bytes() can also raise ValueError.
        # Either failure must be caught here so the client receives a typed
        # ERROR response rather than a silent connection drop.
        # _n_accepted is only incremented after _send_bytes() succeeds to
        # keep the counter accurate.
        try:
            # FIX v4.3: backends return {"success": bool, "result": ...}.  The
            # old code always sent success=True and nested that dict inside
            # `result`, so IPCResponse.success was True even when the OS action
            # failed and callers received a dict repr instead of the value.
            if isinstance(result, dict) and "success" in result:
                ok = bool(result.get("success"))
                inner = result.get("result")
                err_text = result.get("error") or (inner if (not ok and isinstance(inner, str)) else "")
                response = make_response(
                    action,
                    correlation_id,
                    success=ok,
                    result=inner,
                    error_code="" if ok else str(result.get("error_code") or "ACTION_FAILED")[:64],
                    message=str(err_text or "")[:500],
                )
            else:
                response = make_response(
                    action,
                    correlation_id,
                    success=True,
                    result=result,
                )
            response_bytes = dumps_bytes(response)
        except (IPCValidationError, ValueError) as exc:
            self._n_rejected += 1
            _log.error(
                "Step 8: failed to serialise RESPONSE — sending ERROR to client",
                extra={
                    "action":     action,
                    "cid":        correlation_id,
                    "error":      str(exc),
                    "error_type": type(exc).__name__,
                    "code":       _EC_DISPATCH_ERR,
                },
            )
            await self._send_error(
                writer,
                action=action,
                correlation_id=correlation_id,
                code=_EC_DISPATCH_ERR,
                message=f"Response serialisation failed: {exc}",
            )
            return

        await self._send_bytes(writer, response_bytes)
        self._n_accepted += 1

        _log.debug(
            "IPC action dispatched successfully",
            extra={
                "action":   action,
                "cid":      correlation_id,
                "accepted": self._n_accepted,
            },
        )

    # ------------------------------------------------------------------
    # Send helpers
    # ------------------------------------------------------------------

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        *,
        action: str,
        correlation_id: str,
        code: str,
        message: str,
        field: str = "",
    ) -> None:
        """
        Construct and write an ERROR message to *writer*.

        Silently absorbs OSError so a write failure does not mask the
        root cause that triggered this error response.
        """
        try:
            error_msg = make_error(
                action,
                correlation_id,
                code=code,
                message=message,
                field=field,
            )
            await self._send_bytes(writer, dumps_bytes(error_msg))
        except Exception as exc:
            _log.warning(
                "Failed to send ERROR response to IPC client",
                extra={
                    "action": action,
                    "cid":    correlation_id,
                    "error":  str(exc),
                },
            )

    async def _send_bytes(
        self,
        writer: asyncio.StreamWriter,
        data: bytes,
    ) -> None:
        """
        Write *data* to *writer* and drain the write buffer.

        Raises OSError on write failure (caller decides how to handle).
        """
        writer.write(data)
        await writer.drain()
