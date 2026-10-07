# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
ipc/ipc_client.py — Phidipus v1.0
Unix domain socket IPC client running in the orchestrator process (L1).

Architecture contract (C.3):
  "No raw Python object passing. Serialized JSON only."

IPCClient is the Orchestrator-side counterpart of IPCServer.  Every OS action
dispatched by the agent loop, planner, or vision pipeline must go through
``send_action()`` — never via direct pyautogui / subprocess calls (R-01, R-02).

Protocol
--------
Each ``send_action()`` call opens a fresh connection, sends one REQUEST, reads
one RESPONSE or ERROR, then closes.  The server guarantees one response per
connection (see ipc_server.py), so the client never needs to multiplex or
keep connections alive.

Wire format::

    Client → Server:   UTF-8 JSON REQUEST message (validated before send)
    Server → Client:   UTF-8 JSON RESPONSE or ERROR message (validated on recv)

All messages are newline-terminated to allow length-framed reading, but the
client reads up to ``max_message_bytes + 1`` in one shot to detect oversized
responses before parsing.

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import of any kind.
  R-02  No subprocess.run(), os.system(), or shell execution primitive.
  R-05  Every outbound message passes validate_request() before transmission.
        Validation failure raises IPCValidationError — nothing is sent.
  R-10  Outbound: dumps_bytes() → UTF-8 JSON only.
        Inbound:  load_bytes() → parsed dict only.  No pickle, no raw objects.

Process: orchestrator (L1)

Used by:
  core/agent_loop.py         — dispatch typed IPC action messages
  planner/react_reasoner.py  — emit validated action JSON after schema check
  vision/confidence_gate.py  — check() returns payload; caller wraps in send_action()

Dependencies:
  ipc/action_schema.py    — make_request(), validate_request(),
                            validate_response(), IPCValidationError,
                            TYPE_REQUEST, TYPE_RESPONSE, TYPE_ERROR
  utils/json_utils.py     — dumps_bytes(), load_bytes(), JsonDecodeError
  utils/logger.py         — get_logger()
  config/config_loader.py — PhidipusConfig (cfg.ipc.*)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from ipc.action_schema import (
    IPCValidationError,
    TYPE_ERROR,
    TYPE_RESPONSE,
    make_request,
    validate_response,
)
from utils.json_utils import JsonDecodeError, dumps_bytes, load_bytes
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ── [C-10 FIX] Per-message HMAC authentication ───────────────────
def _get_ipc_hmac_key() -> bytes:
    """Load or generate HMAC key for IPC message authentication."""
    key_path = Path("data/memory/.ipc_hmac.key")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        try:
            raw = key_path.read_bytes()
            if len(raw) >= 32:
                return raw
        except Exception:
            pass
    key = os.urandom(32)
    try:
        key_path.write_bytes(key)
        key_path.chmod(0o600)
    except Exception:
        pass
    return key


def _sign_ipc_message(msg: dict) -> str:
    """Sign IPC message dict (excluding _hmac field) with HMAC-SHA256."""
    key = _get_ipc_hmac_key()
    # Exclude the _hmac field itself from signing
    payload = {k: v for k, v in msg.items() if k != "_hmac"}
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _verify_ipc_message(msg: dict) -> bool:
    """Verify HMAC signature of an IPC message."""
    stored = msg.get("_hmac", "")
    if not stored:
        return False
    expected = _sign_ipc_message(msg)
    return hmac.compare_digest(expected, stored)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default maximum retry attempts for transient connection failures.
_DEFAULT_MAX_RETRIES: int = 3

#: Base delay between retries in seconds (doubles each attempt).
_RETRY_BASE_DELAY:    float = 0.1

#: Maximum retry delay cap in seconds.
_RETRY_MAX_DELAY:     float = 2.0

#: FIX v4.3: some L2 actions legitimately take longer than the default
#: recv timeout (5 s) — JS through AppleScript, long typing, confirmation
#: dialogs, launching Chrome.  Minimum response timeout per action:
_ACTION_MIN_TIMEOUT: dict[str, float] = {
    "browser_execute_js": 12.0,
    "ui_snapshot":        8.0,
    "menu_list":          15.0,
    "menu_select":        75.0,   # may show a confirmation dialog
    "keyboard_type":      15.0,
    "user_confirm":       75.0,
    "app_launch":         20.0,
    "screenshot_capture": 15.0,
    "element_find":       12.0,
    "element_click":      12.0,
    "element_get_text":   12.0,
    "element_set_value":  20.0,
    "mouse_drag":         15.0,
    "mouse_scroll":       10.0,
    "browser_navigate":   10.0,
    "window_focus":       8.0,
}

#: Extra bytes read beyond max_message_bytes to detect oversized responses.
_OVERSIZE_SENTINEL:   int = 1


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IPCResponse:
    """
    Immutable record of a successful IPC response.

    Attributes:
        action:         The action name echoed from the request.
        correlation_id: UUID4 string linking this response to its request.
        success:        True iff the Automation Daemon reports success.
        result:         Optional result payload from the daemon (any JSON value).
        error_code:     Short machine-readable error code if success=False.
        message:        Human-readable description from the daemon (optional).
    """

    action:         str
    correlation_id: str
    success:        bool
    result:         Any   = None
    error_code:     str   = ""
    message:        str   = ""

    # FIX v4.3: several callers treated responses as dicts (resp.get(...),
    # resp.data).  These helpers keep that code working.
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default) if key != "data" else self.result

    def __getitem__(self, key: str) -> Any:
        if key == "data":
            return self.result
        return getattr(self, key)

    @property
    def data(self) -> Any:
        return self.result

    @property
    def error(self) -> str:
        return self.message or self.error_code


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class IPCConnectionError(OSError):
    """
    Raised when the client cannot connect to the IPC socket after all retries.

    Attributes:
        socket_path: Path to the Unix socket that could not be reached.
        attempts:    Number of connection attempts made.
        reason:      Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        socket_path: str = "",
        attempts:    int = 0,
        reason:      str = "CONNECTION_FAILED",
    ) -> None:
        super().__init__(message)
        self.socket_path = socket_path
        self.attempts    = attempts
        self.reason      = reason

    def __str__(self) -> str:
        base = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.socket_path:
            parts.append(f"socket={self.socket_path!r}")
        if self.attempts:
            parts.append(f"attempts={self.attempts}")
        return " ".join(parts) + f" {base}"


class IPCTimeoutError(TimeoutError):
    """
    Raised when the server does not respond within the configured timeout.

    Attributes:
        action:          The action that timed out.
        correlation_id:  The request's correlation ID.
        timeout_seconds: The timeout limit that was exceeded.
        reason:          Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        action:          str   = "",
        correlation_id:  str   = "",
        timeout_seconds: float = 0.0,
        reason:          str   = "RECV_TIMEOUT",
    ) -> None:
        super().__init__(message)
        self.action          = action
        self.correlation_id  = correlation_id
        self.timeout_seconds = timeout_seconds
        self.reason          = reason


class IPCRemoteError(RuntimeError):
    """
    Raised when the Automation Daemon returns an ERROR message type (as
    opposed to a RESPONSE with success=False, which is a normal outcome).

    ERROR messages indicate protocol-level failures (schema violation,
    rate limit, dispatch crash) rather than action-level failures.

    Attributes:
        action:         The action that caused the error.
        correlation_id: UUID4 linking this to the original request.
        code:           Machine-readable error code from the daemon.
        field:          Dot-path of the offending field, if provided.
        reason:         Short machine-readable reason code (always "REMOTE_ERROR").
    """

    def __init__(
        self,
        message: str,
        *,
        action:         str = "",
        correlation_id: str = "",
        code:           str = "",
        field:          str = "",
        reason:         str = "REMOTE_ERROR",
    ) -> None:
        super().__init__(message)
        self.action         = action
        self.correlation_id = correlation_id
        self.code           = code
        self.field          = field
        self.reason         = reason

    def __str__(self) -> str:
        base = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.action:
            parts.append(f"action={self.action!r}")
        if self.code:
            parts.append(f"code={self.code!r}")
        return " ".join(parts) + f" {base}"


class IPCResponseError(RuntimeError):
    """
    Raised when the response received from the daemon fails
    validate_response() — indicating a malformed or tampered reply.

    Attributes:
        correlation_id: The request's correlation ID (may be empty if the
                        response could not be parsed).
        reason:         Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        correlation_id: str = "",
        reason:         str = "INVALID_RESPONSE",
    ) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id
        self.reason         = reason


# ---------------------------------------------------------------------------
# IPCClient
# ---------------------------------------------------------------------------

class IPCClient:
    """
    Async IPC client for the Orchestrator process (L1).

    Each ``send_action()`` call is a complete request-response cycle on a
    fresh Unix socket connection.  The client is stateless between calls and
    safe to share across coroutines in the same event loop.

    Thread safety: NOT thread-safe.  Use one instance per asyncio task or
    protect with an asyncio.Lock if sharing across concurrent tasks.

    Usage::

        client = IPCClient(cfg)

        # Send a validated IPC action and await the response:
        response = await client.send_action("mouse_click", {
            "x": 100, "y": 200, "button": "left",
            "confidence": 0.95,
        })
        if response.success:
            print("Click succeeded:", response.result)

        # Or use as an async context manager:
        async with IPCClient(cfg) as client:
            response = await client.send_action("ping", {})

    Args:
        cfg:         Validated PhidipusConfig.  IPC parameters are read from
                     cfg.ipc.socket_path, cfg.ipc.recv_timeout_seconds, and
                     cfg.ipc.max_message_bytes.
        max_retries: Maximum number of reconnect attempts on transient
                     connection failures (ConnectionRefusedError, OSError).
                     Defaults to _DEFAULT_MAX_RETRIES (3).
                     Validation errors (IPCValidationError) are never retried.
    """

    def __init__(
        self,
        cfg:         PhidipusConfig,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self._socket_path:   str   = cfg.ipc.socket_path
        self._recv_timeout:  float = cfg.ipc.recv_timeout_seconds
        self._max_bytes:     int   = cfg.ipc.max_message_bytes
        self._max_retries:   int   = max(0, max_retries)

        _log.info(
            "IPCClient initialised",
            extra={
                "socket_path":  self._socket_path,
                "recv_timeout": self._recv_timeout,
                "max_bytes":    self._max_bytes,
                "max_retries":  self._max_retries,
            },
        )

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "IPCClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        # Stateless client — nothing to clean up.
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_action(
        self,
        action:  str,
        payload: dict[str, Any],
    ) -> IPCResponse:
        """
        Build, validate, and send an IPC action request to the Automation
        Daemon.  Await and return the validated response.

        Processing pipeline:
          1. Generate a fresh UUID4 correlation_id.
          2. Build and validate the REQUEST message via make_request() (R-05).
             If make_request() raises IPCValidationError, the call fails
             immediately — nothing is sent to the daemon.
          3. Serialize to UTF-8 JSON bytes via dumps_bytes() (R-10).
          4. Connect to the Unix domain socket.
          5. Send serialized bytes and close the write-end.
          6. Await response bytes with recv_timeout.
          7. Parse response via load_bytes() (R-10).
          8. Validate response via validate_response().
          9. Handle ERROR vs RESPONSE message types.
         10. Return IPCResponse.

        Transient connection failures (ConnectionRefusedError, broken pipe,
        OS errors) trigger retry with exponential backoff up to max_retries.
        IPCValidationError is never retried — it is a caller bug.

        Args:
            action:  IPC action name (must be in ALLOWED_ACTIONS).
            payload: Action-specific payload dict (must be JSON-serialisable).
                     Must not contain raw Python objects — only JSON-compatible
                     types (R-10).

        Returns:
            IPCResponse with the daemon's result.

        Raises:
            IPCValidationError:  if the request fails schema validation (R-05).
                                  Nothing was sent to the daemon.
            IPCConnectionError:  if the socket is unreachable after all retries.
            IPCTimeoutError:     if no response arrives within recv_timeout_seconds.
            IPCRemoteError:      if the daemon returns an ERROR message type.
            IPCResponseError:    if the response fails validate_response().
            JsonDecodeError:     if the response is not valid JSON.
        """
        # ── Step 1: fresh correlation ID ──────────────────────────────────
        correlation_id = str(uuid.uuid4())

        # ── Step 2: build + validate request (R-05) ───────────────────────
        try:
            message = make_request(
                action,
                payload,
                correlation_id=correlation_id,
            )
        except IPCValidationError:
            _log.error(
                "R-05: outbound IPC message failed validation — not sent",
                extra={
                    "action":         action,
                    "correlation_id": correlation_id,
                },
            )
            raise

        # ── Step 2b: [C-10 FIX] Add HMAC nonce for per-message auth ───────
        message["_ts"] = int(time.time())
        message["_hmac"] = _sign_ipc_message(message)

        # ── Step 3: serialize to bytes (R-10) ─────────────────────────────
        try:
            request_bytes = dumps_bytes(message)
        except ValueError as exc:
            raise IPCValidationError(
                f"Request payload is not JSON-serialisable: {exc}",
                code="SERIALISATION_ERROR",
            ) from exc

        # ── Steps 4-10: connect, send, receive, parse ─────────────────────
        return await self._send_with_retry(
            action=action,
            correlation_id=correlation_id,
            request_bytes=request_bytes,
        )

    async def ping(self) -> bool:
        """
        Send a health-check ``ping`` to the Automation Daemon.

        Returns:
            True if the daemon responded successfully, False otherwise.
            Never raises (absorbs all exceptions — safe for health checks).
        """
        try:
            response = await self.send_action("ping", {})
            return response.success
        except Exception as exc:
            _log.warning(
                "IPC ping failed",
                extra={
                    "socket_path": self._socket_path,
                    "error":       str(exc),
                },
            )
            return False

    # ------------------------------------------------------------------
    # Internal: retry loop
    # ------------------------------------------------------------------

    async def _send_with_retry(
        self,
        *,
        action:         str,
        correlation_id: str,
        request_bytes:  bytes,
    ) -> IPCResponse:
        """
        Attempt to send *request_bytes* and receive a response, retrying
        on transient connection errors up to self._max_retries times.

        Non-retriable errors (IPCValidationError, IPCTimeoutError,
        IPCRemoteError, IPCResponseError) propagate immediately.

        Args:
            action:         Action name (for logging and exception attrs).
            correlation_id: UUID4 string (for logging and exception attrs).
            request_bytes:  Already-serialized UTF-8 JSON request.

        Returns:
            IPCResponse on success.

        Raises:
            IPCConnectionError: after all retries are exhausted.
            IPCTimeoutError:    on recv timeout (not retried).
            IPCRemoteError:     on daemon ERROR response (not retried).
            IPCResponseError:   on invalid response format (not retried).
        """
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                delay = min(
                    _RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                    _RETRY_MAX_DELAY,
                )
                _log.warning(
                    "IPC connection failed — retrying",
                    extra={
                        "action":         action,
                        "correlation_id": correlation_id,
                        "attempt":        attempt,
                        "max_retries":    self._max_retries,
                        "delay_seconds":  delay,
                    },
                )
                await asyncio.sleep(delay)

            try:
                return await self._send_once(
                    action=action,
                    correlation_id=correlation_id,
                    request_bytes=request_bytes,
                )
            except (IPCTimeoutError, IPCRemoteError, IPCResponseError,
                    IPCValidationError, JsonDecodeError):
                # These are definitive failures — retrying won't help.
                raise
            except (ConnectionRefusedError, FileNotFoundError,
                    ConnectionResetError, BrokenPipeError, OSError) as exc:
                last_exc = exc
                _log.debug(
                    "IPC transient connection error",
                    extra={
                        "action":    action,
                        "cid":       correlation_id,
                        "attempt":   attempt,
                        "error":     str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
                continue

        # All retries exhausted.
        raise IPCConnectionError(
            f"Cannot connect to IPC socket after {self._max_retries + 1} "
            f"attempt(s): {last_exc}",
            socket_path=self._socket_path,
            attempts=self._max_retries + 1,
            reason="CONNECTION_FAILED",
        ) from last_exc

    async def _send_once(
        self,
        *,
        action:         str,
        correlation_id: str,
        request_bytes:  bytes,
    ) -> IPCResponse:
        """
        Single connection attempt: open → write → read → close.

        Args:
            action:         Action name (logging / exception attrs).
            correlation_id: UUID4 (logging / exception attrs).
            request_bytes:  Serialized UTF-8 JSON request bytes.

        Returns:
            IPCResponse on success.

        Raises:
            IPCTimeoutError:    if recv_timeout_seconds is exceeded.
            IPCRemoteError:     if the daemon returns an ERROR message.
            IPCResponseError:   if the response fails validate_response().
            JsonDecodeError:    if the response is not valid JSON.
            OSError:            on socket I/O errors (caller retries these).
        """
        # ── Open connection ───────────────────────────────────────────────
        reader, writer = await asyncio.open_unix_connection(self._socket_path)

        try:
            # ── Send request bytes (R-10: JSON only) ──────────────────────
            writer.write(request_bytes)
            # Close write-end so the server knows the request is complete.
            writer.write_eof()
            await writer.drain()

            _log.debug(
                "IPC request sent",
                extra={
                    "action":         action,
                    "correlation_id": correlation_id,
                    "bytes_sent":     len(request_bytes),
                },
            )

            # ── Receive response with timeout ─────────────────────────────
            # Read max_bytes + 1 to detect oversized responses.
            _timeout = max(self._recv_timeout, _ACTION_MIN_TIMEOUT.get(action, 0.0))

            async def _read_all() -> bytes:
                chunks: list[bytes] = []
                total = 0
                limit = self._max_bytes + _OVERSIZE_SENTINEL
                while total < limit:
                    chunk = await reader.read(limit - total)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                return b"".join(chunks)

            try:
                raw_response = await asyncio.wait_for(_read_all(), timeout=_timeout)
            except asyncio.TimeoutError as exc:
                raise IPCTimeoutError(
                    f"No response from daemon within "
                    f"{_timeout:.1f}s for action {action!r}.",
                    action=action,
                    correlation_id=correlation_id,
                    timeout_seconds=_timeout,
                ) from exc

            if not raw_response:
                raise IPCResponseError(
                    f"Daemon closed connection without sending a response "
                    f"for action {action!r}.",
                    correlation_id=correlation_id,
                    reason="EMPTY_RESPONSE",
                )

            if len(raw_response) > self._max_bytes:
                raise IPCResponseError(
                    f"Response size {len(raw_response)} bytes exceeds "
                    f"max_message_bytes {self._max_bytes} for action {action!r}.",
                    correlation_id=correlation_id,
                    reason="RESPONSE_TOO_LARGE",
                )

            # ── Parse response (R-10: JSON only) ──────────────────────────
            try:
                response_msg = load_bytes(raw_response)
            except JsonDecodeError as exc:
                raise IPCResponseError(
                    f"Daemon response for action {action!r} is not valid JSON: {exc}",
                    correlation_id=correlation_id,
                    reason="JSON_DECODE_ERROR",
                ) from exc

            if not isinstance(response_msg, dict):
                raise IPCResponseError(
                    f"Daemon response for action {action!r} is not a JSON object.",
                    correlation_id=correlation_id,
                    reason="NOT_OBJECT",
                )

        finally:
            # Always close the writer (and underlying socket).
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

        # ── Handle ERROR message type ─────────────────────────────────────
        # ERROR messages indicate protocol-level failures in the daemon
        # (schema violation, rate limit, dispatch crash).  They are distinct
        # from RESPONSE{success: False} which is a normal action-level outcome.
        msg_type = response_msg.get("type", "")
        if msg_type == TYPE_ERROR:
            err_payload = response_msg.get("payload", {})
            raise IPCRemoteError(
                f"Daemon returned ERROR for action {action!r}: "
                f"{err_payload.get('message', '(no message)')}",
                action=action,
                correlation_id=correlation_id,
                code=err_payload.get("code", ""),
                field=err_payload.get("field", ""),
            )

        # ── Validate response schema ───────────────────────────────────────
        # validate_response() raises IPCValidationError if the message is
        # not a well-formed RESPONSE.  Re-raise as IPCResponseError so callers
        # only need to handle one client-side exception type.
        try:
            validate_response(response_msg, max_bytes=self._max_bytes)
        except IPCValidationError as exc:
            raise IPCResponseError(
                f"Daemon response for action {action!r} failed validation: {exc}",
                correlation_id=correlation_id,
                reason="INVALID_RESPONSE",
            ) from exc

        # ── Build IPCResponse ─────────────────────────────────────────────
        payload = response_msg.get("payload", {})
        response = IPCResponse(
            action         = response_msg.get("action", action),
            correlation_id = response_msg.get("correlation_id", correlation_id),
            success        = bool(payload.get("success", False)),
            result         = payload.get("result"),
            error_code     = payload.get("error_code", ""),
            message        = payload.get("message", ""),
        )

        _log.debug(
            "IPC response received",
            extra={
                "action":         response.action,
                "correlation_id": response.correlation_id,
                "success":        response.success,
                "error_code":     response.error_code,
            },
        )

        return response
