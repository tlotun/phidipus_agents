# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/logger.py — Phidipus v1.0
Structured JSON logger with correlation ID support and process-name tagging.

Design:
  - Every log record is emitted as a single-line JSON object to stderr.
  - Correlation IDs propagate through a thread-local context so that all log
    lines within a task share the same cid without callers passing it explicitly.
  - Process name is fixed at logger-creation time (e.g. "orchestrator", "daemon").
  - No external dependencies — stdlib only.
  - Thread-safe: uses threading.local for correlation context and a per-handler
    lock (provided by stdlib logging.StreamHandler).

Usage:
    from utils.logger import get_logger, set_correlation_id, new_correlation_id

    logger = get_logger("core.agent_loop", process="orchestrator")
    set_correlation_id("abc-123")
    logger.info("task started", extra={"goal": "open browser"})
    logger.error("unexpected failure", exc_info=True)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Thread-local correlation context
# ---------------------------------------------------------------------------

_ctx = threading.local()


def set_correlation_id(cid: str) -> None:
    """Bind a correlation ID to the current thread's log context."""
    _ctx.correlation_id = cid


def get_correlation_id() -> str:
    """Return the current thread's correlation ID, or an empty string."""
    return getattr(_ctx, "correlation_id", "")


def new_correlation_id() -> str:
    """Generate a fresh correlation ID and bind it to the current thread."""
    import uuid
    cid = uuid.uuid4().hex
    set_correlation_id(cid)
    return cid


def clear_correlation_id() -> None:
    """Remove the correlation ID from the current thread's context."""
    _ctx.correlation_id = ""


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """
    Formats every log record as a single-line JSON object.

    JSON fields emitted:
        ts        — ISO-8601 UTC timestamp with milliseconds
        level     — log level name (INFO, ERROR, …)
        process   — process label supplied at logger creation
        logger    — logger name (module hierarchy)
        pid       — OS process ID
        cid       — correlation ID from thread-local context
        msg       — formatted message string
        exc       — exception traceback string (only when exc_info is present)
        <extra>   — any additional key/value pairs passed via extra={}
    """

    # Fields that exist on every LogRecord and must not be forwarded as extras.
    _RESERVED: frozenset[str] = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message",
    })

    def __init__(self, process_name: str) -> None:
        super().__init__()
        self._process_name = process_name

    def format(self, record: logging.LogRecord) -> str:
        # Core fields
        record_dict: dict[str, Any] = {
            "ts":      datetime.fromtimestamp(record.created, tz=timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%S.") +
                        f"{int(record.msecs):03d}Z",
            "level":   record.levelname,
            "process": self._process_name,
            "logger":  record.name,
            "pid":     os.getpid(),
            "cid":     get_correlation_id(),
            "msg":     record.getMessage(),
        }

        # Exception info
        if record.exc_info:
            record_dict["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            record_dict["exc"] = record.exc_text

        # Stack info (e.g. from logger.warning(..., stack_info=True))
        if record.stack_info:
            record_dict["stack"] = self.formatStack(record.stack_info)

        # Extra key/value pairs passed via extra={} or LoggerAdapter.extra
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                # Attempt JSON serialisation; fall back to repr() for exotic types.
                try:
                    json.dumps(value)
                    record_dict[key] = value
                except (TypeError, ValueError):
                    record_dict[key] = repr(value)

        try:
            return json.dumps(record_dict, ensure_ascii=False, separators=(",", ":"))
        except Exception:  # pragma: no cover
            # Last-resort fallback — never raise inside format()
            return json.dumps({"ts": record_dict["ts"], "level": "ERROR",
                                "msg": "log serialisation failed"})


# ---------------------------------------------------------------------------
# Handler factory (one stderr handler per process, shared via root logger)
# ---------------------------------------------------------------------------

_HANDLER_LOCK = threading.Lock()
_HANDLER_INSTALLED: dict[str, bool] = {}


def _ensure_handler(process_name: str, level: int) -> None:
    """
    Install a JSON StreamHandler on the root logger once per process_name.
    Idempotent — safe to call multiple times from different threads.
    """
    with _HANDLER_LOCK:
        if _HANDLER_INSTALLED.get(process_name):
            return
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter(process_name))
        handler.setLevel(level)
        root = logging.getLogger()
        # Remove any pre-existing handlers that might have been added by
        # third-party imports before we install ours.
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level)
        _HANDLER_INSTALLED[process_name] = True


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def get_logger(
    name: str,
    *,
    process: str = "phidipus",
    level: int | str = logging.DEBUG,
) -> logging.Logger:
    """
    Return a stdlib Logger configured to emit structured JSON.

    Args:
        name:    Dotted logger name, typically __name__ of the calling module.
        process: Human-readable process label (e.g. "orchestrator", "daemon").
                 Used in every log record so multi-process log streams can be
                 filtered by process without examining PID.
        level:   Minimum log level.  Accepts int (logging.INFO) or string ("INFO").

    Returns:
        A standard logging.Logger.  Callers may pass additional structured data
        via the extra= keyword::

            logger.info("action dispatched", extra={"action": "click", "x": 100})
    """
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())

    _ensure_handler(process, level)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    # Propagate to root handler; do not add a second handler here.
    logger.propagate = True
    return logger


# ---------------------------------------------------------------------------
# Context-manager helper for scoped correlation IDs
# ---------------------------------------------------------------------------

class correlation_scope:
    """
    Context manager that sets and then clears a correlation ID.

    Usage::

        with correlation_scope("task-abc-123"):
            agent_loop.run()
        # correlation ID cleared after block exits
    """

    def __init__(self, cid: str) -> None:
        self._cid = cid
        self._prev: str = ""

    def __enter__(self) -> "correlation_scope":
        self._prev = get_correlation_id()
        set_correlation_id(self._cid)
        return self

    def __exit__(self, *_: object) -> None:
        set_correlation_id(self._prev)
