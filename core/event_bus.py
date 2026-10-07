# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/event_bus.py — Phidipus Event Bus v9.20
═════════════════════════════════════════════

Lightweight async pub/sub event system.
Modules publish events → subscribers react asynchronously.
No module calls another directly for cross-cutting concerns.

Events:
  TASK_RECEIVED       — Telegram/Admin nhận lệnh
  TASK_PLANNED        — Planner tạo xong plan
  TASK_STEP_START     — Bắt đầu 1 step
  TASK_STEP_DONE      — Step hoàn thành (success/fail)
  TASK_COMPLETED      — Task thành công
  TASK_FAILED         — Task thất bại
  SKILL_CREATED       — Skill Forge tạo skill mới
  SKILL_CACHED        — Skill được cache lại
  MEMORY_UPDATED      — Memory có dữ liệu mới
  ENVIRONMENT_CHANGED — File/app/window thay đổi
  RESOURCE_WARNING    — RAM/CPU vượt ngưỡng
  AGENT_HEALING       — Self-healing đang chạy

Usage:
    bus = EventBus()
    bus.subscribe("TASK_COMPLETED", memory_handler)
    bus.subscribe("TASK_FAILED", failure_memory_handler)
    await bus.publish("TASK_COMPLETED", {"task_id": "abc", "goal": "..."})
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


@dataclass
class Event:
    """Immutable event object passed to subscribers."""
    type: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    source: str = ""  # module that published


# Type alias for handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """
    Lightweight async event broker.

    - Subscribers are async functions: async def handler(event: Event)
    - publish() fires all subscribers concurrently (asyncio.gather)
    - Subscriber errors are caught and logged, never crash the bus
    - Thread-safe for single asyncio event loop
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        self._history: list[Event] = []
        self._max_history: int = 200
        self._stats: dict[str, int] = defaultdict(int)
        # [M-12 FIX] Track critical handlers whose failure should raise an alert
        self._critical_handlers: set = set()

    # ── Pub/Sub API ───────────────────────────────────────────

    def subscribe(self, event_type: str, handler: EventHandler, critical: bool = False) -> None:
        """Register an async handler for an event type.
        M-12 FIX: Mark critical=True for handlers whose failure must be alerted.
        """
        if handler not in self._subscribers[event_type]:
            self._subscribers[event_type].append(handler)
        if critical:
            self._critical_handlers.add(id(handler))

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a handler."""
        subs = self._subscribers.get(event_type, [])
        if handler in subs:
            subs.remove(handler)

    async def publish(self, event_type: str, data: dict[str, Any] | None = None,
                      source: str = "") -> int:
        """
        Publish an event to all subscribers.

        Args:
            event_type: Event name (e.g. "TASK_COMPLETED")
            data: Event payload dict
            source: Module name that published

        Returns:
            Number of subscribers that were notified
        """
        event = Event(type=event_type, data=data or {}, source=source)

        # Store in history
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        self._stats[event_type] += 1

        handlers = self._subscribers.get(event_type, [])
        if not handlers:
            return 0

        # Fire all handlers concurrently — errors are caught per handler
        results = await asyncio.gather(
            *[self._safe_call(h, event) for h in handlers],
            return_exceptions=True,
        )

        # Count successes
        success_count = sum(1 for r in results if r is None or r is True)
        return success_count

    async def _safe_call(self, handler: EventHandler, event: Event) -> bool | None:
        """Call handler safely — catch all exceptions.
        M-12 FIX: Critical handlers emit a loud alert on failure.
        """
        try:
            await handler(event)
            return True
        except Exception as exc:
            is_critical = id(handler) in self._critical_handlers
            if is_critical:
                # Critical handler failure — loud alert, not just a warning
                import logging
                logging.getLogger("phidipus.event_bus").error(
                    f"[M-12] CRITICAL event handler FAILED [{event.type}] "
                    f"handler={getattr(handler, '__name__', repr(handler))}: {str(exc)[:120]}",
                    exc_info=True,
                )
                _vlog("🚨", f"[M-12] CRITICAL subscriber failed [{event.type}]: {str(exc)[:80]}")
            else:
                _vlog("⚠️", f"Event handler error [{event.type}]: {str(exc)[:80]}")
            return False

    # ── Query API ─────────────────────────────────────────────

    def recent_events(self, event_type: str = "", limit: int = 20) -> list[Event]:
        """Get recent events, optionally filtered by type."""
        if event_type:
            filtered = [e for e in self._history if e.type == event_type]
        else:
            filtered = self._history
        return filtered[-limit:]

    def stats(self) -> dict[str, Any]:
        """Return event statistics for Admin Panel."""
        return {
            "total_events": sum(self._stats.values()),
            "event_counts": dict(self._stats),
            "subscriber_counts": {k: len(v) for k, v in self._subscribers.items() if v},
            "history_size": len(self._history),
        }

    def subscriber_count(self, event_type: str = "") -> int:
        """Count subscribers for an event type (or all)."""
        if event_type:
            return len(self._subscribers.get(event_type, []))
        return sum(len(v) for v in self._subscribers.values())


# ══════════════════════════════════════════════════════════════
# Singleton global bus
# ══════════════════════════════════════════════════════════════

_global_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get or create the global event bus singleton."""
    global _global_bus
    if _global_bus is None:
        _global_bus = EventBus()
    return _global_bus
