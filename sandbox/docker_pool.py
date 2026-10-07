# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
sandbox/docker_pool.py — Phidipus v1.0
Pre-warm queue for ephemeral Docker sandbox containers.

Architecture contract (R-12 / R-14 / H-7):
  "Disable container reuse.  Pool is pre-warm queue only.  Wrap docker
   inspect in asyncio.to_thread (H-7).  Fresh container per execution."

DockerPool maintains a queue of pre-warmed (created but not yet used)
DockerSandbox instances.  When a caller requests a sandbox, it receives
a pre-warmed instance rather than waiting for Docker to create a new
container from scratch — reducing latency for the first execution.

Critical distinction: the pool is a PRE-WARM QUEUE, not a container
reuse mechanism.  Each DockerSandbox instance in the pool is used
exactly once and then discarded.  After use, a replacement is pre-warmed
asynchronously.

Container reuse is explicitly prohibited (R-12).  This means:
  - No sandbox instance is ever returned to the pool after use.
  - No cross-task state can accumulate in a container.
  - Every execution starts from a clean container image.

asyncio.to_thread (H-7)
-----------------------
Docker API calls (image pull check, container inspect) are blocking
network operations.  DockerPool wraps them in asyncio.to_thread() so
the orchestrator event loop is not blocked during pool maintenance.

Process: orchestrator (L1)

Security invariants enforced here:
  R-12  No container is ever reused.  acquire() gives a fresh container;
        release() DISCARDS the sandbox — never returns it to the pool.
  R-14  All containers are created with the same security parameters
        as direct DockerSandbox construction (delegated to SandboxManager).

Used by:
  core/agent_loop.py   — optional pool injection for lower latency

Dependencies:
  sandbox/sandbox_manager.py — SandboxManager, SandboxManagerConfig
  sandbox/docker_runner.py   — DockerSandbox, SandboxConfig, ExecutionResult
  config/config_loader.py    — PhidipusConfig
  utils/logger.py            — get_logger()
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from config.config_loader import PhidipusConfig
from sandbox.docker_runner import (
    DockerSandbox,
    SandboxConfig,
    SandboxExecutionError,
    SandboxSecurityError,
)
from sandbox.sandbox_manager import SandboxManager, SandboxManagerConfig
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PoolError(RuntimeError):
    """
    Raised when the DockerPool cannot fulfil a request.

    Attributes:
        reason: Short machine-readable reason code.
    """

    def __init__(self, message: str, *, reason: str = "POOL_ERROR") -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"[{self.reason}] {super().__str__()}"


# ---------------------------------------------------------------------------
# DockerPool
# ---------------------------------------------------------------------------

class DockerPool:
    """
    Pre-warm queue of ephemeral Docker sandbox instances.

    Maintains up to *pool_size* pre-warmed SandboxManager instances in an
    asyncio queue.  When a caller calls acquire(), it receives a pre-warmed
    manager and a replacement is started asynchronously.

    IMPORTANT: The pool is a latency optimisation, not a reuse mechanism.
    Every sandbox instance is used exactly once (R-12).

    Usage (async context manager):

        pool = DockerPool(cfg)
        await pool.start()
        try:
            manager = await pool.acquire()
            result  = manager.run_python("print('hello')")
            # manager is discarded after use — never return it to the pool
        finally:
            await pool.stop()

    Or with async context manager::

        async with DockerPool(cfg) as pool:
            manager = await pool.acquire()
            result  = manager.run_python("print('hello')")

    Args:
        cfg:        Validated PhidipusConfig.
        pool_size:  Maximum number of pre-warmed sandboxes.  Defaults to
                    cfg.sandbox.pool_size (typically 2).
    """

    def __init__(
        self,
        cfg: PhidipusConfig,
        pool_size: int | None = None,
    ) -> None:
        self._pool_size: int = pool_size if pool_size is not None else cfg.sandbox.pool_size
        self._manager_cfg = SandboxManagerConfig(
            image                = cfg.sandbox.docker_image,
            seccomp_profile_path = Path(cfg.sandbox.seccomp_profile_path),
            default_memory       = cfg.sandbox.memory_limit,
            default_cpu_quota    = cfg.sandbox.cpu_quota,
            default_timeout      = cfg.sandbox.execution_timeout_seconds,
        )
        self._queue: asyncio.Queue[SandboxManager] = asyncio.Queue(
            maxsize=self._pool_size
        )
        self._running: bool = False
        self._fill_task: asyncio.Task[None] | None = None

        _log.info(
            "DockerPool configured",
            extra={
                "pool_size":    self._pool_size,
                "docker_image": cfg.sandbox.docker_image,
            },
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Start the pool and pre-warm up to pool_size containers.

        Raises:
            PoolError: if already running.
        """
        if self._running:
            raise PoolError("DockerPool.start() called while already running.",
                            reason="ALREADY_RUNNING")
        self._running = True
        # Kick off background fill task
        self._fill_task = asyncio.create_task(self._fill_loop())
        _log.info("DockerPool started", extra={"pool_size": self._pool_size})

    async def stop(self) -> None:
        """
        Stop the pool and drain remaining pre-warmed sandboxes.

        Pre-warmed but unused sandboxes are stopped to release Docker
        resources.
        """
        self._running = False
        if self._fill_task is not None:
            self._fill_task.cancel()
            try:
                await self._fill_task
            except asyncio.CancelledError:
                pass
            self._fill_task = None

        # Drain and stop pre-warmed sandboxes
        drained = 0
        while not self._queue.empty():
            try:
                mgr = self._queue.get_nowait()
                # SandboxManager holds no persistent container — just discard
                del mgr
                drained += 1
            except asyncio.QueueEmpty:
                break

        _log.info("DockerPool stopped", extra={"drained": drained})

    async def __aenter__(self) -> "DockerPool":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Acquire
    # ------------------------------------------------------------------

    async def acquire(self, timeout: float = 10.0) -> SandboxManager:
        """
        Acquire a pre-warmed SandboxManager for a single execution.

        The returned SandboxManager must be used exactly once and then
        discarded — never returned to the pool (R-12).  After use, a
        replacement sandbox is automatically pre-warmed by the background
        fill task.

        Args:
            timeout: Maximum seconds to wait for a pre-warmed sandbox.
                     Falls back to creating a new one on-demand if the
                     queue is empty after this duration.

        Returns:
            A fresh SandboxManager ready for one execution.

        Raises:
            PoolError: if the pool is not running.
        """
        if not self._running:
            raise PoolError(
                "DockerPool.acquire() called before start().",
                reason="NOT_RUNNING",
            )

        try:
            manager = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            _log.debug(
                "DockerPool: pre-warmed sandbox acquired",
                extra={"queue_size": self._queue.qsize()},
            )
            # Trigger refill of the slot we just consumed
            asyncio.create_task(self._prewarm_one())
            return manager
        except asyncio.TimeoutError:
            # Pool is empty — create one on-demand (still fresh, R-12 safe)
            _log.warning(
                "DockerPool: queue empty — creating on-demand sandbox",
                extra={"timeout": timeout},
            )
            return self._create_manager()

    # ------------------------------------------------------------------
    # Background fill loop
    # ------------------------------------------------------------------

    async def _fill_loop(self) -> None:
        """
        Background coroutine that keeps the pre-warm queue full.

        Runs until self._running is False or the task is cancelled.
        Wraps blocking Docker operations in asyncio.to_thread (H-7).
        """
        while self._running:
            if self._queue.qsize() < self._pool_size:
                await self._prewarm_one()
            # Brief sleep to avoid busy-looping when pool is full
            await asyncio.sleep(0.5)

    async def _prewarm_one(self) -> None:
        """
        Create one SandboxManager and enqueue it.
        M-11 FIX: Uses finally to ensure partial allocations are cleaned up
        on failure, preventing Docker resource leaks.
        """
        if self._queue.full():
            return
        manager = None
        try:
            manager = await asyncio.to_thread(self._create_manager)
            if not self._queue.full():
                await self._queue.put(manager)
                manager = None  # ownership transferred to queue
                _log.debug(
                    "DockerPool: pre-warmed sandbox enqueued",
                    extra={"queue_size": self._queue.qsize()},
                )
        except (SandboxSecurityError, SandboxExecutionError, Exception) as exc:
            _log.error(
                "DockerPool: pre-warm failed",
                extra={"error": str(exc)},
            )
        finally:
            # M-11 FIX: If manager was created but not enqueued (exception path),
            # clean it up to avoid Docker container resource leak
            if manager is not None:
                try:
                    await asyncio.to_thread(manager.cleanup)
                except Exception:
                    pass

    def _create_manager(self) -> SandboxManager:
        """
        Synchronously construct a fresh SandboxManager.

        Called from asyncio.to_thread (H-7) — must not use await.
        """
        return SandboxManager(self._manager_cfg)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def queue_size(self) -> int:
        """Current number of pre-warmed sandboxes available."""
        return self._queue.qsize()

    @property
    def pool_size(self) -> int:
        """Configured maximum pool size."""
        return self._pool_size
