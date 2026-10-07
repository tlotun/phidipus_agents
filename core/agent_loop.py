# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/agent_loop.py — Phidipus v1.0
Main task lifecycle: emit IPC actions only, no direct OS calls.

Architecture contract (R-01 / R-02 / R-05):
  "Remove all direct pyautogui/subprocess calls.  Emit only typed IPC
   messages.  Remove hot-reload trigger.  Emit correlation_id for each
   task."

AgentLoop drives the main ReAct execution loop for a single task.  It
coordinates all subsystems but never touches OS automation directly:
  - Actions are dispatched exclusively via IPCClient.send_action() (R-01, R-02)
  - VLM detections are gated by ConfidenceGate before dispatch (R-21)
  - Startup calls MemoryGuard.scan_integrity() before the loop begins (R-20)
  - Each task gets a UUID4 correlation_id (for audit log linkage)
  - Hot-reload trigger removed (would bypass integrity checks)

Loop flow per task
------------------
  1. scan_integrity() — verify episode memory at startup (R-20)
  2. begin_task() — initialise TaskMemory
  3. For each step up to max_steps:
       a. perceive()    — get gated UI elements
       b. react.step()  — get validated IPC action
       c. ipc.send()    — dispatch action to daemon
       d. record obs    — store in TaskMemory
       e. check health  — stuck-loop detection
  4. Distil episode → EpisodicMemory (R-16)
  5. task_memory.clear() (M-7)

Process: orchestrator (L1)

Security invariants enforced here:
  R-01  No pyautogui, xdotool, or OS automation import.
  R-02  No subprocess.run(), os.system(), or shell execution.
  R-05  All OS actions go through ipc_client.send_action().
  R-20  scan_integrity() called at startup before loop begins.
  R-21  VLM elements gated before dispatch (via PerceptionPipeline).
  R-22  PARTIAL — ConfidenceGate.check() preserves require_confirm=True
        when set.  The flag is SET by the Automation Daemon (L2, Phase 4)
        when it detects an action targets a non-focused window via AT-SPI.
        Phase 3 only provides the gate pass-through; Phase 4 provides
        the focused-window detection that sets the flag.
  M-7   task_memory.clear() called after every task.

Used by:
  (entrypoint scripts / daemon launcher)

Dependencies:
  core/llm_client.py            — LLMClient
  core/skill_router.py          — SkillRouter
  core/runtime_monitor.py       — RuntimeMonitor
  ipc/ipc_client.py             — IPCClient
  memory/episodic_memory.py     — EpisodicMemory
  memory/task_memory.py         — TaskMemory
  memory_integrity/memory_guard.py — MemoryGuard (scan_integrity, R-20)
  planner/react_reasoner.py     — ReactReasoner
  planner/task_graph.py         — TaskGraph
  vision/perception_pipeline.py — PerceptionPipeline (owns ConfidenceGate internally)
  config/config_loader.py       — PhidipusConfig
  utils/logger.py               — get_logger(), correlation_scope
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from config.config_loader import PhidipusConfig
from core.llm_client import LLMClient
from core.runtime_monitor import RuntimeMonitor
from core.skill_router import SkillRouter
from ipc.ipc_client import IPCClient, IPCResponse
from memory.episodic_memory import EpisodicMemory
from memory.task_memory import TaskMemory
from memory_integrity.memory_guard import MemoryGuard
from planner.react_reasoner import ReactReasoner, ReActError
from planner.task_graph import TaskGraph
from vision.perception_pipeline import PerceptionPipeline
from utils.logger import get_logger, correlation_scope
from utils.smart_actions import SmartActionEngine

# v9.33: Inner Monologue
try:
    from vision.inner_monologue import MonologueStore
    _HAS_MONOLOGUE = True
except ImportError:
    _HAS_MONOLOGUE = False

# v9.34 B1: LoopDetector
try:
    from core.loop_detector import LoopDetector, LoopSignal
    _HAS_LOOP_DETECTOR = True
except ImportError:
    _HAS_LOOP_DETECTOR = False

# v9.36: SmartRouter + UniversalParser
try:
    from core.smart_router import SmartRouter
    from core.universal_parser import UniversalParser
    _HAS_SMART_ROUTER = True
except ImportError:
    _HAS_SMART_ROUTER = False
from utils.skill_forge import SkillForge
from core.event_bus import get_event_bus, EventBus
from core.checkpoint import CheckpointManager
from core.environment import EnvironmentGraph
from memory.failure_memory import FailureMemory
from planner.task_decomposer import TaskDecomposer
from skills.skill_db import SkillRegistryDB, SkillMeta
from core.skill_intelligence import SkillIntelligence
from core.recovery_engine import RecoveryEngine
from core.goal_tracker import GoalTracker
from core.self_healing import SelfHealer
from memory.semantic_memory import SemanticMemory
from core.resource_guard import get_resource_guard, ResourceGuard
from utils.wait_until import wait_until, file_exists, wait_for_file, retry_with_backoff
from core.scheduler import TaskScheduler, ScheduledTask, plan_to_scheduled_tasks, analyze_parallelism
from skills.workflow_library import WorkflowLibrary

# v2.4 C1: TaskRouter + TaskMonitor (additive — agent_loop still works if missing)
_HAS_TASK_ROUTER = False
_HAS_TASK_MONITOR = False
_HAS_TASK_EXECUTOR = False
try:
    from core.task_router import TaskRouter as _TaskRouter, RouteResult as _RouteResult
    _HAS_TASK_ROUTER = True
except ImportError:
    pass
try:
    from core.task_monitor import TaskMonitor as _TaskMonitor, get_task_monitor as _get_task_monitor
    _HAS_TASK_MONITOR = True
except ImportError:
    pass
try:
    from core.task_executor import TaskExecutor as _TaskExecutor, get_task_executor as _get_task_executor
    _HAS_TASK_EXECUTOR = True
except ImportError:
    pass

# P3: Brain Pipeline (fine-tuned Qwen3-4B classifier)
_HAS_BRAIN = False
try:
    from core.brain_pipeline import BrainPipeline as _BrainPipeline
    _HAS_BRAIN = True
except ImportError:
    pass

_log = get_logger(__name__, process="orchestrator")

# ── Vietnamese terminal log (hiện trên terminal thay JSON) ────────
def _vlog(icon: str, msg: str) -> None:
    """Print Vietnamese colored log to terminal."""
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ---------------------------------------------------------------------------
# Task result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskResult:
    """
    Immutable record of a completed task.

    Attributes:
        task_id:        UUID4 string used as correlation_id.
        goal:           Sanitized goal string.
        success:        True if the task completed without error.
        steps_taken:    Number of ReAct loop iterations.
        episode_id:     ID of the stored episode (if success).
        error:          Error message if success=False.
    """

    task_id:     str
    goal:        str
    success:     bool
    steps_taken: int
    episode_id:  str = ""
    error:       str = ""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AgentLoopError(RuntimeError):
    """
    Raised when the agent loop encounters an unrecoverable error.

    Attributes:
        task_id: The task ID that failed.
        reason:  Short machine-readable reason code.
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str = "",
        reason:  str = "LOOP_ERROR",
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.reason  = reason

    def __str__(self) -> str:
        base  = super().__str__()
        parts = [f"[{self.reason}]"]
        if self.task_id:
            parts.append(f"task_id={self.task_id!r}")
        return " ".join(parts) + f" {base}"


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------

class AgentLoop:
    """
    Main task execution loop for Phidipus v1.0.

    Coordinates vision, planning, IPC dispatch, and memory — without
    any direct OS automation calls (R-01, R-02).

    Usage::

        loop = AgentLoop(cfg)
        await loop.startup()   # scan_integrity (R-20)

        result = await loop.run_task("Open browser and go to example.com")
        print(result.success, result.steps_taken)

    Args:
        cfg:        Validated PhidipusConfig.
        a11y_fn:    Optional async callable for accessibility API elements.
    """

    def __init__(
        self,
        cfg:     PhidipusConfig,
        a11y_fn: Any | None = None,
        app_scanner: Any | None = None,
    ) -> None:
        self._cfg          = cfg
        self._max_steps    = cfg.agent.max_steps

        # Core subsystems
        self._llm          = LLMClient(cfg)
        # Register global singleton for workflow_executor, openclaw, etc.
        from core.llm_client import set_llm_client
        set_llm_client(self._llm)
        self._ipc          = IPCClient(cfg)
        self._guard        = MemoryGuard(cfg)
        self._episodic     = EpisodicMemory(cfg, self._guard)
        self._task_memory  = TaskMemory()
        self._monitor      = RuntimeMonitor(cfg)
        self._skill_router = SkillRouter(cfg)
        self._perception   = PerceptionPipeline(cfg, a11y_fn=a11y_fn)
        self._reasoner     = ReactReasoner(cfg, self._llm)

        # Smart Action Engine — priorities 1-3 (fast, no LLM/VLM)
        self._smart = SmartActionEngine(self._ipc, app_scanner=app_scanner)

        # Skill Forge — priority 4 (Gemini + FallbackStack)
        forge_kwargs = (
            cfg.skill_forge.as_forge_kwargs()
            if hasattr(cfg.skill_forge, "as_forge_kwargs")
            else {
                "gemini_api_key": cfg.skill_forge.gemini_api_key,
                "gemini_model": cfg.skill_forge.gemini_model,
                "skills_dir": cfg.skill_forge.skills_dir,
                "max_retries": cfg.skill_forge.max_retries,
                "mistral_api_key": getattr(cfg.skill_forge, "mistral_api_key", ""),
                "cerebras_api_key": getattr(cfg.skill_forge, "cerebras_api_key", ""),
                "openrouter_api_key": getattr(cfg.skill_forge, "openrouter_api_key", ""),
                "ollama_base_url": getattr(cfg.skill_forge, "ollama_base_url", "http://127.0.0.1:11434"),
                "ollama_coder_model": getattr(cfg.skill_forge, "ollama_coder_model", "qwen2.5-coder:7b"),
            }
        )
        self._forge = SkillForge(app_scanner=app_scanner, **forge_kwargs)
        self._telegram_send_fn = None  # TEXT notifications — set by Telegram bot
        # FIX v4.3: files (Skill Forge outputs) use a separate sender; the old
        # single attribute was overwritten by either a text or a file sender.
        self._telegram_send_file_fn = None
        self._wechat_send_fn = None    # FIX #11: Set by WeChat bot inject
        self._pending_ttl_s = 24 * 3600   # Brain approvals expire after 24h
        # v4.3 Memory Agent: most specific method per goal, recorded once by run_task
        self._memory_hints: dict[str, tuple[str, str]] = {}

        # P3: Brain Pipeline — lazy singleton, created on first use
        self._brain: Any = None
        # Pending brain workflow approvals: task_id → {workflow, goal, nodes_preview}
        self._pending_brain_workflows: dict = {}

        # v9.20: Event Bus — decoupled module communication
        self._bus: EventBus = get_event_bus()

        # v9.20: Failure Memory — learn from errors
        self._failure_memory = FailureMemory()

        # v9.20: Task Decomposer — multi-step planning
        self._decomposer = TaskDecomposer(llm_client=self._llm)

        # v9.36: SmartRouter — thay thế workflow_skill hardcode check
        self._router: "SmartRouter | None" = None
        if _HAS_SMART_ROUTER:
            self._router = SmartRouter(
                agent_loop=self,
                llm_client=self._llm,
                ipc_client=self._ipc,
            )
            # v9.39: Pre-embed SkillRegistry trong background (không block startup)
            try:
                from core.semantic_router import SemanticRouter as _SR
                _sr = _SR()
                import asyncio as _asyncio
                _asyncio.get_event_loop().create_task(
                    _sr.ensure_embeddings()
                ) if _asyncio.get_event_loop().is_running() else None
            except Exception:
                pass

        # v9.20: Checkpoint — resume after crash
        self._checkpoint = CheckpointManager()

        # v9.20: Environment Graph — live machine state
        self._env = EnvironmentGraph(app_scanner=app_scanner)
        self._task_sem = asyncio.Semaphore(3)  # C-05 FIX

        # v9.20: Skill Registry DB — anti skill-explosion
        self._skill_db = SkillRegistryDB()

        # v9.21: Skill Intelligence Layer — bridge registry ↔ execution
        self._intelligence = SkillIntelligence(self._skill_db)

        # v9.21: Recovery Engine — DOM capture + skill-type policy + re-planning
        self._recovery = RecoveryEngine(decomposer=self._decomposer)

        # v9.20 Phase 2: Goal Tracker — prevent drift
        self._goal_tracker = GoalTracker()

        # v9.20 Phase 2: Self-Healing — detect → diagnose → fix → retry
        self._healer = SelfHealer(self._failure_memory, self._ipc)

        # v9.20 Phase 2: Semantic Memory — system knowledge accumulation
        self._semantic = SemanticMemory()

        # v9.20 Phase 3: Resource Guard — RAM/CPU monitoring + hard limits
        limits = {}
        if hasattr(cfg, 'resource_guard'):
            limits = cfg.resource_guard.as_limits_dict()
        self._resource_guard: ResourceGuard = get_resource_guard(limits)

        # v9.20 Phase 4A: Dependency Scheduler — parallel task execution
        self._scheduler = TaskScheduler(
            executor_fn=self._schedule_executor,
            max_concurrency=3,
            event_bus=self._bus,
        )

        # v9.20 Phase 4C: Workflow Library — plan cache + reusable workflows
        self._workflow_lib = WorkflowLibrary()

        # v2.4 C1: TaskRouter + TaskMonitor + TaskExecutor
        self._task_router: "object | None" = None
        self._task_monitor_c1: "object | None" = None
        self._task_executor_c1: "object | None" = None
        if _HAS_TASK_ROUTER:
            self._task_router = _TaskRouter(config=cfg)
        if _HAS_TASK_MONITOR:
            self._task_monitor_c1 = _get_task_monitor()
        if _HAS_TASK_EXECUTOR:
            self._task_executor_c1 = _get_task_executor()

        # [M-14 FIX] In-progress goal set to prevent duplicate concurrent tasks
        # Two simultaneous identical goals will share result via the same execution
        self._in_progress_goals: set[str] = set()
        self._in_progress_lock = asyncio.Lock()

        _log.info(
            "AgentLoop initialised",
            extra={
                "max_steps":     self._max_steps,
                "ipc_socket":    cfg.ipc.socket_path,
            },
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """
        Run startup checks before the loop begins.

        Performs startup integrity scan on all episode namespaces (R-20).
        Logs a warning if any episodes fail verification, but does not
        abort — the operator must decide whether to proceed.
        """
        _log.info("AgentLoop: startup — running integrity scan (R-20)")
        result = self._guard.scan_integrity()
        if not result.all_valid:
            _log.error(
                "R-20: startup integrity scan found corrupt episodes",
                extra={
                    "invalid_hmac": result.invalid_hmac,
                    "unreadable":   result.unreadable,
                    "invalid_ids":  result.invalid_ids[:10],
                },
            )
        else:
            _log.info(
                "R-20: startup integrity scan OK",
                extra={"total_episodes": result.total_files},
            )

        # Health-check the IPC connection to the daemon
        try:
            alive = await self._ipc.ping()
            if alive:
                _log.info("AgentLoop: IPC daemon is reachable")
            else:
                _log.warning("AgentLoop: IPC ping returned False — daemon may not be ready")
        except Exception as exc:
            _log.warning(
                "AgentLoop: IPC ping failed",
                extra={"error": str(exc)},
            )

        # v9.20 Phase 4: Inject EventBus into SmartActionEngine
        if hasattr(self._smart, 'inject_event_bus'):
            self._smart.inject_event_bus(self._bus)
        # Inject LLM cho natural language profile parsing
        if hasattr(self._smart, 'inject_llm'):
            self._smart.inject_llm(self._llm)

        # v4.3: Skill Forge tool bridge performs browser/clipboard actions via IPC
        if hasattr(self._forge, 'inject_ipc'):
            self._forge.inject_ipc(self._ipc)

        # v9.20 Phase 4: Inject memory into SkillForge
        if hasattr(self._forge, 'inject_memory'):
            self._forge.inject_memory(
                failure_memory=self._failure_memory,
                semantic_memory=self._semantic,
            )

        # v2.4 C1: Inject dependencies into TaskExecutor
        if _HAS_TASK_EXECUTOR and self._task_executor_c1:
            self._task_executor_c1.inject(
                smart=self._smart,
                forge=self._forge,
                decomposer=self._decomposer,
                intelligence=self._intelligence,
                llm=self._llm,
                ipc=self._ipc,
                notify_fn=self._telegram_send_fn,
                bus=self._bus,
                monitor=self._monitor,
            )

        # C3 v9.26: Inject SemanticMemory + FailureMemory vào SkillTemplateEngine
        # Mỗi lần template run thành công → extract_from_task() → học patterns
        # Mỗi lần fail → FailureMemory.record_failure() → tránh lỗi cũ
        try:
            from skills.skill_templates import SkillTemplateEngine
            if hasattr(self, '_template_engine') and self._template_engine:
                self._template_engine.inject(
                    semantic_memory=self._semantic,
                    failure_memory=self._failure_memory,
                )
                _vlog("🧠", "C3: SkillTemplateEngine ← SemanticMemory + FailureMemory injected")
            else:
                # Khởi tạo engine nếu chưa có, inject luôn
                _te = SkillTemplateEngine()
                _te.inject(
                    semantic_memory=self._semantic,
                    failure_memory=self._failure_memory,
                )
                self._template_engine = _te
                _vlog("🧠", "C3: SkillTemplateEngine created + injected")
        except Exception as _c3_err:
            _vlog("⚠️", f"C3 SkillTemplateEngine inject: {_c3_err}")

        # v9.20 Phase 3: Start resource monitoring
        rg_cfg = getattr(self._cfg, 'resource_guard', None)
        if rg_cfg is None or getattr(rg_cfg, 'enabled', True):
            await self._resource_guard.start_monitoring()
            snap = self._resource_guard.current
            _log.info(
                "AgentLoop: ResourceGuard started",
                extra={"ram_pct": snap.ram_used_percent, "cpu_pct": snap.cpu_percent},
            )

    # ------------------------------------------------------------------
    # v4.3 helpers: file delivery + memory agent hooks
    # ------------------------------------------------------------------

    async def _file_sender(self, path: str) -> None:
        """Deliver a generated file (Skill Forge output) to the owner."""
        try:
            from core import notify_hub
            if await notify_hub.send_file(str(path), f"📎 {str(path).rsplit('/', 1)[-1]}"):
                return
        except Exception:
            pass
        if self._telegram_send_file_fn:
            await self._telegram_send_file_fn(str(path))

    def _memory(self):
        try:
            from memory.memory_agent import get_memory_agent
            return get_memory_agent()
        except Exception:
            return None

    def _observe_memory(self, goal: str, success: bool, method: str = "",
                        error: str = "", detail: str = "") -> None:
        """Remember which concrete method handled *goal*; run_task records the
        outcome exactly once (see _record_task_memory)."""
        self._memory_hints[goal.strip().lower()] = (method, detail)

    def _record_task_memory(self, goal: str, result: Any, duration_s: float = 0.0) -> None:
        """Episode + procedure learning in the Memory Agent (never raises/blocks)."""
        method, detail = self._memory_hints.pop(goal.strip().lower(), ("", ""))
        mem = self._memory()
        if mem is None:
            return
        try:
            method = (method or getattr(result, "lane_used", "") or getattr(result, "skill_used", "")
                      or "agent")
            mem.observe_task_nowait(goal=goal, success=bool(result.success), method=method,
                                    error=getattr(result, "error", "") or "", detail=detail,
                                    duration_s=duration_s)
        except Exception:
            pass

    def _purge_expired_approvals(self) -> None:
        now = time.time()
        for aid in [a for a, p in self._pending_brain_workflows.items()
                    if now - p.get("created_at", now) > self._pending_ttl_s]:
            self._pending_brain_workflows.pop(aid, None)

    # ------------------------------------------------------------------
    # P3 Brain: approve / reject pending workflows
    # ------------------------------------------------------------------

    async def execute_brain_approved_workflow(self, approve_id: str) -> dict:
        """
        Thực thi workflow do Brain generate sau khi user /approve.
        Gọi từ TelegramBot._cmd_approve().

        Returns: {"success": bool, "message": str, "steps_done": int}
        """
        self._purge_expired_approvals()
        pending = self._pending_brain_workflows.pop(approve_id, None)
        if not pending:
            return {"success": False, "message": f"Không tìm thấy workflow với ID `{approve_id}`. Đã timeout hoặc không tồn tại."}

        wf_def  = pending["workflow"]
        goal    = pending["goal"]
        task_id = pending.get("task_id", "brain_approved")

        _vlog("✅", f"Brain workflow approved ({approve_id}): chạy ngay...")
        try:
            from core.workflow_executor import WorkflowExecutor as _WFExec
            _wfe = _WFExec(ipc_client=self._ipc, notify_fn=self._telegram_send_fn)
            result = await asyncio.wait_for(
                _wfe.run(wf_def, goal=goal),
                timeout=300,
            )
            success = result.get("success", False)
            steps   = result.get("steps_done", 0)
            err     = result.get("error", "")
            _vlog("✅" if success else "❌", f"Brain approved WF: success={success} steps={steps}")
            return {
                "success": success,
                "message": f"✅ Hoàn thành {steps} bước" if success else f"❌ Thất bại: {err[:120]}",
                "steps_done": steps,
            }
        except asyncio.TimeoutError:
            return {"success": False, "message": "⏱️ Workflow timeout (300s). Thử lại với workflow đơn giản hơn.", "steps_done": 0}
        except Exception as exc:
            return {"success": False, "message": f"❌ Lỗi chạy workflow: {str(exc)[:200]}", "steps_done": 0}

    def reject_brain_workflow(self, approve_id: str) -> bool:
        """Xoá pending brain workflow. Gọi từ TelegramBot._cmd_reject()."""
        return self._pending_brain_workflows.pop(approve_id, None) is not None

    def list_pending_brain_workflows(self) -> list:
        """Trả danh sách pending approvals. Dùng trong /status Telegram."""
        self._purge_expired_approvals()
        out = []
        for aid, p in self._pending_brain_workflows.items():
            nodes = p["workflow"].get("nodes", [])
            out.append({
                "approve_id": aid,
                "goal": p["goal"][:60],
                "node_count": len(nodes),
                "action": p.get("action", "create_workflow"),
            })
        return out

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    async def run_task(self, raw_goal: str) -> TaskResult:
        """
        Execute a single task from goal to completion.
        C-05 FIX: Semaphore limits concurrent tasks to 3.
        M-14 FIX: Deduplicates identical in-progress goals.
        FIX v1.0: Auto load/unload qwen3:4b cho mỗi task.
        """
        task_id = str(uuid.uuid4())
        # Sanitize goal first (R-24)
        try:
            goal = self._llm.sanitize_goal(raw_goal)
        except Exception as exc:
            return TaskResult(
                task_id=task_id, goal=raw_goal, success=False,
                steps_taken=0, error=str(exc),
            )

        # [M-14 FIX] Dedup: if same goal is already running, wait briefly then check cache
        goal_key = goal.strip().lower()
        async with self._in_progress_lock:
            if goal_key in self._in_progress_goals:
                _vlog("♻️", f"[M-14] Goal already in progress, skipping duplicate: {goal[:60]}")
                return TaskResult(
                    task_id=task_id, goal=goal, success=False,
                    steps_taken=0,
                    error="Tác vụ tương tự đang chạy. Vui lòng đợi hoàn thành.",
                )
            self._in_progress_goals.add(goal_key)

        # ── FIX v1.0: Load qwen3:4b vào VRAM trước task ────────
        _llm_parser_loaded = False
        try:
            from core.llm_intent_parser import get_intent_parser
            _parser = get_intent_parser()
            if _parser._enabled and not _parser._loaded:
                _llm_parser_loaded = await _parser.load_model()
        except Exception:
            pass

        # [C-05 FIX] Semaphore: max 3 concurrent tasks
        try:
            async with self._task_sem:
                with correlation_scope(task_id):
                    rcheck = self._resource_guard.check_before_task(goal[:60])
                    if not rcheck.ok:
                        _vlog("🛡️", f"Resource quá tải: {rcheck.summary()}")
                    available = await self._resource_guard.wait_until_available(timeout=30)
                    if not available:
                        _vlog("⚠️", "Tiếp tục dù resource thấp (timeout 30s)")
                _t_task0 = time.time()
                _task_result = await self._execute_task(task_id, goal)
                self._record_task_memory(goal, _task_result, time.time() - _t_task0)

            # ── FIX v1.0: Store task in RAG memory ──────────────────
            try:
                from memory.rag_engine import get_rag_engine_sync
                _rag = get_rag_engine_sync()
                if _rag and _rag._ready:
                    asyncio.create_task(_rag.store_task(
                        goal=goal,
                        result={
                            "success": _task_result.success,
                            "error": _task_result.error,
                            "steps": _task_result.steps_taken,
                            "lane": getattr(_task_result, "lane_used", ""),
                            "skill": getattr(_task_result, "skill_used", ""),
                        },
                        duration_s=getattr(_task_result, "duration_s", 0),
                    ))
                    # Auto-learn from result
                    asyncio.create_task(_rag.learn_from_task(
                        goal=goal,
                        result={
                            "success": _task_result.success,
                            "error": _task_result.error,
                            "skill": getattr(_task_result, "skill_used", ""),
                            "lane": getattr(_task_result, "lane_used", ""),
                        },
                    ))
            except Exception:
                pass

            return _task_result
        except Exception as _task_exc:
            return TaskResult(
                task_id=task_id, goal=goal, success=False,
                steps_taken=0, error=str(_task_exc)[:200],
            )
        finally:
            # [M-14 FIX] Always release in-progress dedup lock
            async with self._in_progress_lock:
                self._in_progress_goals.discard(goal_key)

            # ── FIX v1.0: Unload qwen3:4b khỏi VRAM sau task ───
            if _llm_parser_loaded:
                try:
                    from core.llm_intent_parser import get_intent_parser
                    await get_intent_parser().unload_model()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Internal execution (priorities 4-5 handled here via LLM/VLM loop)
    # ------------------------------------------------------------------

    async def _execute_task(self, task_id: str, goal: str) -> TaskResult:
        """
        Internal task loop with 6-priority execution.

        P1-3: SmartActionEngine (instant)
        P4:   SkillForge (Gemini, 10-30s)
        P4b:  TaskDecomposer → multi-step (5-60s)
        P5:   LLM + VLM loop (30-90s, last resort)
        """
        self._task_memory.begin_task(task_id=task_id, goal=goal)
        self._monitor.record_task_start(task_id=task_id, goal=goal)
        _vlog("🎯", f"Bắt đầu tác vụ: \033[1m{goal[:80]}\033[0m")

        # v2.4 C1: TaskMonitor — track task start + wire dependencies
        if _HAS_TASK_MONITOR and self._task_monitor_c1:
            _tm = self._task_monitor_c1
            _tm.inject(
                notify_fn=self._telegram_send_fn,
                # BUG #9 FIX: inject wechat_fn — WeChat notifications hoạt động
                wechat_fn=getattr(self, "_wechat_send_fn", None),
                ws_fn=self._ws_broadcast if hasattr(self, "_ws_broadcast") else None,
                event_bus=self._bus,
            )
            _tm.start_task(task_id=task_id, goal=goal, total_steps=0)

        # v9.33: Khởi tạo MonologueStore cho task mới
        if _HAS_MONOLOGUE:
            self._monologue_store = MonologueStore(task_id=task_id)

        # v9.34 B1: Khởi tạo LoopDetector cho task mới
        if _HAS_LOOP_DETECTOR:
            self._loop_detector = LoopDetector()

        # v9.20: Refresh environment state
        try:
            env_state = await self._env.refresh()
            _vlog("👁️", f"Env: {env_state.focused_app} | "
                  f"{len(env_state.apps)} apps | {len(env_state.windows)} windows | "
                  f"RAM {env_state.ram_used_percent:.0f}%")
        except Exception:
            pass

        # Event: task received
        await self._bus.publish("TASK_RECEIVED", {
            "task_id": task_id, "goal": goal,
        }, source="agent_loop")

        # ── Smart Action Engine: priorities 1-3 (no LLM/VLM) ──────
        smart_result = await self._smart.execute(goal)
        if smart_result is not None:
            self._observe_memory(goal, smart_result.success, method=f"smart:{smart_result.method}")
            if not smart_result.success and smart_result.method == "kb_declined":
                # v4.3: the user refused (or the command is too risky) — do NOT
                # fall through to Brain / Skill Forge, which could do it anyway.
                self._monitor.record_task_end(task_id=task_id, success=False)
                return TaskResult(task_id=task_id, goal=goal, success=False, steps_taken=0,
                                  error=smart_result.error or "Đã huỷ theo yêu cầu người dùng")
            if smart_result.success:
                # [FIX v4.1] Chỉ log "Hoàn thành" khi THỰC SỰ thành công
                _vlog("📊", f"Hoàn thành bởi ưu tiên {smart_result.priority} "
                      f"({smart_result.method}) trong {smart_result.duration_ms}ms")
                await self._bus.publish("TASK_COMPLETED", {
                    "task_id": task_id, "goal": goal, "method": smart_result.method,
                    "priority": smart_result.priority,
                }, source="smart_action")
                self._monitor.record_task_end(task_id=task_id, success=True)
                return TaskResult(
                    task_id=task_id, goal=goal,
                    success=True,
                    steps_taken=1,
                    error="",
                )
            else:
                # [FIX v4.1] Smart action FAIL → KHÔNG return sớm, fall-through xuống Brain
                # Trước đây: return TaskResult(success=False) ngay lập tức và log ✅ giả
                _vlog("⚠️", f"Smart action thất bại ({smart_result.method}): "
                      f"{smart_result.error[:100] if smart_result.error else 'unknown'} "
                      f"— chuyển sang Brain Pipeline")
                await self._bus.publish("TASK_FAILED", {
                    "task_id": task_id, "goal": goal, "method": smart_result.method,
                    "error": smart_result.error,
                }, source="smart_action")
                # Không return — tiếp tục xuống P3 Brain Pipeline bên dưới

        # ── P3: Brain Pipeline (fine-tuned Qwen3-4B — last resort classifier) ──
        # Chỉ gọi khi P1-3 SmartAction MISS. ~5-10% requests thực tế.
        # Brain classify → 8 actions: run_workflow / create / compose /
        #   refuse / warn / suggest_fix / clarify / answer_knowledge
        if _HAS_BRAIN:
            try:
                # Lazy init brain singleton
                if self._brain is None:
                    self._brain = _BrainPipeline(model="phidipus-brain-v7", verbose=False)
                _vlog("🧠", f"Brain P3: classifying '{goal[:60]}'...")
                brain_result = await self._brain.process(goal)
                _vlog("🧠", f"Brain → action={brain_result.action} "
                      f"source={brain_result.source} latency={brain_result.latency_ms}ms")

                # ── run_workflow: delegate to SmartRouter / WorkflowExecutor ──
                if brain_result.action == "run_workflow":
                    wf_name = brain_result.workflow_name or (
                        brain_result.response.get("workflow_name", "") if brain_result.response else ""
                    )
                    if wf_name:
                        # FIX v4.3: the old code rewrote the goal to
                        # "run_workflow:<name>" (no router understood it) and then
                        # called WorkflowLibrary.get() which does not exist, so
                        # the 15 bundled workflows never ran from the Brain.
                        try:
                            from core.workflow_nodes_ext import resolve_workflow as _resolve_wf
                            from core.workflow_executor import (
                                WorkflowExecutor as _WFExec, find_matching_workflow as _fmw,
                            )
                            _wf_def = _resolve_wf(wf_name)
                            if _wf_def:
                                _m, _vars = _fmw(goal, workflows=[_wf_def])
                                _vars = dict(_vars or {}) if _m else {}
                                _vlog("▶️ ", f"Brain: chạy WF '{_wf_def.get('name', wf_name)}'")
                                _wfe = _WFExec(ipc_client=self._ipc, notify_fn=self._telegram_send_fn)
                                _wr = await asyncio.wait_for(
                                    _wfe.run(_wf_def, goal=goal, variables=_vars), timeout=300,
                                )
                                self._monitor.record_task_end(task_id=task_id, success=_wr.get("success", False))
                                self._observe_memory(goal, _wr.get("success", False),
                                                     method=f"workflow:{_wf_def.get('id', wf_name)}",
                                                     error=_wr.get("error", ""))
                                return TaskResult(
                                    task_id=task_id, goal=goal,
                                    success=_wr.get("success", False),
                                    steps_taken=_wr.get("steps_done", 0),
                                    error=_wr.get("error", ""),
                                )
                            _vlog("⚠️ ", f"Brain: không tìm thấy workflow '{wf_name}' trong data/workflows")
                        except asyncio.TimeoutError:
                            _vlog("⏱️", f"Brain run_workflow '{wf_name}' timeout (300s)")
                            self._monitor.record_task_end(task_id=task_id, success=False)
                            return TaskResult(task_id=task_id, goal=goal, success=False, steps_taken=0,
                                              error=f"Workflow '{wf_name}' timeout (300s)")
                        except Exception as _wfl_err:
                            _vlog("⚠️ ", f"Brain run_workflow error: {_wfl_err}")
                    # run_workflow nhưng không resolve được → fall through

                # ── refuse: block hoàn toàn, thông báo lý do ──
                elif brain_result.action == "refuse":
                    _reason = ""
                    if brain_result.response:
                        _reason = brain_result.response.get("reason", "") or brain_result.response.get("message", "")
                    _category = brain_result.response.get("category", "illegal") if brain_result.response else ""
                    _msg = f"❌ Không thể thực hiện\n\n{_reason}"
                    if _category:
                        _msg += f"\n\n_Lý do: {_category}_"
                    if self._telegram_send_fn:
                        await self._telegram_send_fn(_msg)
                    _vlog("🚫", f"Brain refuse: {_reason[:80]}")
                    self._monitor.record_task_end(task_id=task_id, success=False)
                    return TaskResult(task_id=task_id, goal=goal, success=False, steps_taken=0,
                                      error=f"Brain refused: {_reason[:120]}")

                # ── warn: thông báo rủi ro qua Telegram, fall through SkillForge ──
                elif brain_result.action == "warn":
                    _wmsg = ""
                    _risks = []
                    if brain_result.response:
                        _wmsg = brain_result.response.get("message", "")
                        _risks = brain_result.response.get("risks", [])
                    _tg_warn = f"⚠️ Cảnh báo\n\n{_wmsg}"
                    if _risks:
                        _tg_warn += "\n\nRủi ro:\n" + "\n".join(f"• {r}" for r in _risks[:4])
                    _can_proceed = brain_result.response.get("can_proceed", True) if brain_result.response else True
                    if not _can_proceed:
                        _tg_warn += "\n\n_Tác vụ bị dừng do rủi ro quá cao._"
                        if self._telegram_send_fn:
                            await self._telegram_send_fn(_tg_warn)
                        self._monitor.record_task_end(task_id=task_id, success=False)
                        return TaskResult(task_id=task_id, goal=goal, success=False, steps_taken=0,
                                          error=f"Brain warn (no proceed): {_wmsg[:120]}")
                    if self._telegram_send_fn:
                        await self._telegram_send_fn(_tg_warn)
                    _vlog("⚠️ ", f"Brain warn (tiếp tục): {_wmsg[:60]}")
                    # Fall through to SkillForge with original goal

                # ── suggest_fix: gửi diagnosis + fixes, task complete ──
                elif brain_result.action == "suggest_fix":
                    _diag = ""
                    _fixes = []
                    if brain_result.response:
                        _diag = brain_result.response.get("diagnosis", "")
                        _fixes = brain_result.response.get("fixes", [])
                    _fix_msg = f"🔧 Chẩn đoán\n\n{_diag}"
                    if _fixes:
                        _fix_msg += "\n\n*Cách sửa:*\n" + "\n".join(f"{i+1}. {f}" for i, f in enumerate(_fixes[:5]))
                    # Kèm fixed_workflow nếu có
                    _fixed_wf = brain_result.response.get("fixed_workflow") if brain_result.response else None
                    if _fixed_wf and isinstance(_fixed_wf, dict):
                        _nodes_preview = ", ".join(
                            n.get("type", "?") for n in _fixed_wf.get("nodes", [])[:6]
                        )
                        _fix_msg += f"\n\n*Workflow đã sửa:* `{_nodes_preview}{'...' if len(_fixed_wf.get('nodes',[])) > 6 else ''}`"
                    if self._telegram_send_fn:
                        await self._telegram_send_fn(_fix_msg)
                    _vlog("🔧", f"Brain suggest_fix sent: {_diag[:60]}")
                    self._monitor.record_task_end(task_id=task_id, success=True)
                    return TaskResult(task_id=task_id, goal=goal, success=True, steps_taken=0, error="")

                # ── answer_knowledge: trả lời kiến thức, task complete ──
                elif brain_result.action == "answer_knowledge":
                    _answer = ""
                    _details = {}
                    if brain_result.response:
                        _answer = brain_result.response.get("answer", "")
                        _details = brain_result.response.get("details", {})
                    _know_msg = f"📚 {_answer}"
                    if _details and isinstance(_details, dict):
                        _det_lines = [f"• *{k}*: {v}" for k, v in list(_details.items())[:5]]
                        if _det_lines:
                            _know_msg += "\n\n" + "\n".join(_det_lines)
                    if self._telegram_send_fn:
                        await self._telegram_send_fn(_know_msg)
                    _vlog("📚", f"Brain knowledge: {_answer[:60]}")
                    self._monitor.record_task_end(task_id=task_id, success=True)
                    return TaskResult(task_id=task_id, goal=goal, success=True, steps_taken=0, error="")

                # ── clarify: hỏi thêm thông tin, task pending ──
                elif brain_result.action == "clarify" and (brain_result.response or {}).get("questions"):
                    # v4.3: a clarify without questions (backend failure) used to
                    # end the task with an empty "❓ Cần thêm thông tin" message.
                    _questions = brain_result.response.get("questions", [])
                    _clr_msg = "❓ Cần thêm thông tin:\n\n"
                    _clr_msg += "\n".join(f"{i+1}. {q}" for i, q in enumerate(_questions[:4]))
                    if self._telegram_send_fn:
                        await self._telegram_send_fn(_clr_msg)
                    _vlog("❓", f"Brain clarify: {_questions[:2]}")
                    self._monitor.record_task_end(task_id=task_id, success=True)
                    return TaskResult(task_id=task_id, goal=goal, success=True, steps_taken=0, error="")

                # ── create_workflow / compose_workflows: preview + /approve ──
                elif brain_result.action in ("create_workflow", "compose_workflows"):
                    _wf_payload = None
                    if brain_result.response:
                        _wf_payload = brain_result.response.get("workflow") or brain_result.workflow
                    if _wf_payload and isinstance(_wf_payload, dict):
                        _nodes = _wf_payload.get("nodes", [])
                        _wf_name = _wf_payload.get("name", "brain_generated")
                        _n_count = len(_nodes)
                        _nodes_desc = " → ".join(
                            n.get("type", "?") for n in _nodes[:8]
                        ) + ("..." if _n_count > 8 else "")
                        # Store cho approval handler
                        import uuid as _uuid
                        _approve_id = _uuid.uuid4().hex[:8]
                        self._pending_brain_workflows[_approve_id] = {
                            "workflow": _wf_payload,
                            "goal": goal,
                            "task_id": task_id,
                            "action": brain_result.action,
                            "sub_tasks": brain_result.response.get("sub_tasks", []) if brain_result.response else [],
                            "created_at": time.time(),
                        }
                        _action_label = "Tạo mới" if brain_result.action == "create_workflow" else "Ghép"
                        _preview_msg = (
                            f"🧠 Brain {_action_label} workflow\n\n"
                            f"*Mục tiêu:* {goal[:80]}\n"
                            f"*Tên:* `{_wf_name}`\n"
                            f"*Nodes ({_n_count}):* `{_nodes_desc}`\n\n"
                            f"Dùng lệnh:\n"
                            f"  `/approve {_approve_id}` — Chấp nhận & chạy\n"
                            f"  `/reject {_approve_id}` — Từ chối"
                        )
                        if self._telegram_send_fn:
                            await self._telegram_send_fn(_preview_msg)
                        _vlog("🧠", f"Brain {brain_result.action}: preview sent, approve_id={_approve_id}")
                        # Task suspends here — will resume when user /approve
                        self._monitor.record_task_end(task_id=task_id, success=True)
                        return TaskResult(task_id=task_id, goal=goal, success=True, steps_taken=0,
                                          error=f"Pending /approve {_approve_id}")
                    else:
                        # [FIX v4.2] Auto-retry Brain với prompt bổ sung khi thiếu payload
                        # Trước đây: fall-through ngay → SkillForge match nhầm workflow
                        _vlog("🔄", f"Brain {brain_result.action}: thiếu workflow payload — retry với hint...")
                        try:
                            _retry_hint = (
                                f"LẦN TRƯỚC bạn trả action={brain_result.action} nhưng THIẾU workflow nodes. "
                                f"Hãy trả JSON ĐẦY ĐỦ với workflow.nodes cho lệnh: {goal[:120]}"
                            )
                            _retry_result = await self._brain.process(_retry_hint)
                            _retry_payload = None
                            if _retry_result.response:
                                _retry_payload = _retry_result.response.get("workflow")
                            if _retry_payload and isinstance(_retry_payload, dict) and _retry_payload.get("nodes"):
                                _vlog("✅", f"Brain retry thành công: {len(_retry_payload['nodes'])} nodes")
                                # Re-process as if original result had payload
                                brain_result.response["workflow"] = _retry_payload
                                # Loop back to create preview (use goto-like pattern)
                                _nodes = _retry_payload.get("nodes", [])
                                _wf_name = _retry_payload.get("name", "brain_retry")
                                _n_count = len(_nodes)
                                _nodes_desc = " → ".join(
                                    n.get("type", "?") for n in _nodes[:8]
                                ) + ("..." if _n_count > 8 else "")
                                import uuid as _uuid2
                                _approve_id = _uuid2.uuid4().hex[:8]
                                self._pending_brain_workflows[_approve_id] = {
                                    "workflow": _retry_payload,
                                    "goal": goal,
                                    "task_id": task_id,
                                    "action": brain_result.action,
                                    "sub_tasks": brain_result.response.get("sub_tasks", []) if brain_result.response else [],
                                    "created_at": time.time(),
                                }
                                _action_label = "Tạo mới" if brain_result.action == "create_workflow" else "Ghép"
                                _preview_msg = (
                                    f"🧠 Brain {_action_label} workflow (retry)\n\n"
                                    f"*Mục tiêu:* {goal[:80]}\n"
                                    f"*Tên:* `{_wf_name}`\n"
                                    f"*Nodes ({_n_count}):* `{_nodes_desc}`\n\n"
                                    f"Dùng lệnh:\n"
                                    f"  `/approve {_approve_id}` — Chấp nhận & chạy\n"
                                    f"  `/reject {_approve_id}` — Từ chối"
                                )
                                if self._telegram_send_fn:
                                    await self._telegram_send_fn(_preview_msg)
                                self._monitor.record_task_end(task_id=task_id, success=True)
                                return TaskResult(task_id=task_id, goal=goal, success=True, steps_taken=0,
                                                  error=f"Pending /approve {_approve_id}")
                            else:
                                _vlog("⚠️ ", f"Brain retry vẫn thiếu payload — fall through")
                        except Exception as _retry_err:
                            _vlog("⚠️ ", f"Brain retry error: {_retry_err}")
                        # Fall through to Skill Intelligence

            except asyncio.CancelledError:
                raise
            except Exception as _brain_err:
                _vlog("⚠️ ", f"Brain P3 error (non-fatal): {_brain_err} — fall through")
                # Non-fatal: fall through to P3.5 Skill Intelligence

        # ── P3.5: Skill Intelligence Layer (Registry → Forge bridge) ──
        _registry_resolved = False
        if self._intelligence.enabled:
            decision = self._intelligence.decide(goal)
            _vlog("🧠", f"Intelligence: {decision.action} "
                  f"(confidence={decision.confidence:.2f})")

            if decision.anti_loop_warning:
                _vlog("🔁", f"Anti-loop: {decision.reason}")

            if decision.action == "execute_registry" and decision.candidate:
                candidate = decision.candidate
                _vlog("📚", f"Registry hit: '{candidate.name}' "
                      f"(rate={candidate.success_rate:.0%}, "
                      f"used={candidate.usage_count}x) — 0 API calls")

                # FIX v4.3: the registry decision used to call forge.execute(goal)
                # which regenerated code (API calls) instead of running the
                # proven skill.  Run the stored code when it is available.
                forge_result = None
                if getattr(candidate, "code_path", "") and hasattr(self._forge, "execute_saved"):
                    forge_result = await self._forge.execute_saved(
                        candidate.code_path, goal, telegram_send_fn=self._file_sender,
                    )
                if forge_result is None or (not forge_result.success and not forge_result.error.startswith("[SEC")):
                    forge_result = await self._forge.execute(
                        goal, telegram_send_fn=self._file_sender,
                    )
                if forge_result.success:
                    validation = self._intelligence.validate_output(
                        forge_result.message, goal,
                    )
                    if validation.valid:
                        self._intelligence.record_outcome(
                            goal=goal, skill_name=candidate.skill_id,
                            success=True, runtime_ms=forge_result.duration_ms,
                            source="registry",
                        )
                        _vlog("✅", f"Registry skill '{candidate.name}' OK "
                              f"(output confidence={validation.confidence:.0%})")
                        await self._bus.publish("TASK_COMPLETED", {
                            "task_id": task_id, "goal": goal,
                            "method": "registry_skill",
                            "skill": candidate.name,
                        }, source="skill_intelligence")
                        self._monitor.record_task_end(task_id=task_id, success=True)
                        return TaskResult(
                            task_id=task_id, goal=goal, success=True,
                            steps_taken=1, error="",
                        )
                    else:
                        _vlog("⚠️", f"Output invalid: {validation.issues}")
                        self._intelligence.record_outcome(
                            goal=goal, skill_name=candidate.skill_id,
                            success=False, source="registry",
                        )
                else:
                    self._intelligence.record_outcome(
                        goal=goal, skill_name=candidate.skill_id,
                        success=False, source="registry",
                    )

            elif decision.action == "compose" and decision.composition:
                # C4 v9.26: Thực thi composition pipeline — 0 API calls
                _vlog("🔗", f"Composition: {len(decision.composition.steps)} steps — 0 API calls")
                try:
                    compose_ok = await self._intelligence.execute_composition(
                        decision.composition, goal,
                    )
                    if compose_ok:
                        await self._bus.publish("TASK_COMPLETED", {
                            "task_id": task_id, "goal": goal,
                            "method": "skill_composition",
                            "steps": len(decision.composition.steps),
                        }, source="skill_composer")
                        self._monitor.record_task_end(task_id=task_id, success=True)
                        return TaskResult(
                            task_id=task_id, goal=goal, success=True,
                            steps_taken=len(decision.composition.steps), error="",
                        )
                    else:
                        _vlog("⚠️", "Composition thất bại → fallback SkillForge")
                except Exception as _c4e:
                    _vlog("⚠️", f"Composition error: {_c4e} → fallback")

            elif decision.action == "require_confirm":
                _vlog("⚠️", f"Cần xác nhận: {decision.reason}")

        # ── v2.4 C1: TaskRouter pre-check (additive, non-blocking) ──────────
        if _HAS_TASK_ROUTER and self._task_router:
            try:
                _tr_result = await self._task_router.route(goal)
                _vlog("🗺️", f"C1 TaskRouter: lane={_tr_result.lane} "
                         f"conf={_tr_result.confidence:.2f} "
                         f"{_tr_result.elapsed_ms:.0f}ms")
                # v2.4 C1: If TaskRouter found a workflow, try TaskExecutor
                if _HAS_TASK_EXECUTOR and self._task_executor_c1 and _tr_result.lane == "workflow":
                    # [FIX v4.2] Minimum confidence check — prevent low-score match
                    # Trước đây: score=0.50 vẫn chạy → match nhầm "check email" cho lệnh không liên quan
                    if _tr_result.confidence < 0.70:
                        _vlog("⚠️", f"C1 TaskRouter: confidence {_tr_result.confidence:.2f} < 0.70 — skip workflow, fall through")
                    else:
                        _vlog("🚀", f"C1 TaskExecutor: delegating workflow '{_tr_result.workflow.get('name', '?')}'")
                        try:
                            from core.workflow_executor import WorkflowExecutor
                            _wf_exec = WorkflowExecutor(
                                ipc_client=self._ipc,
                                notify_fn=self._telegram_send_fn,
                            )
                            _wf_result = await asyncio.wait_for(
                                _wf_exec.run(_tr_result.workflow, goal=goal, variables=_tr_result.variables),
                                timeout=300,
                            )
                            if _wf_result.get("success") or _wf_result.get("steps_done", 0) > 0:
                                self._monitor.record_task_end(task_id=task_id, success=_wf_result.get("success", False))
                                return TaskResult(
                                    task_id=task_id, goal=goal,
                                    success=_wf_result.get("success", False),
                                    steps_taken=_wf_result.get("steps_done", 0),
                                    error=_wf_result.get("error", ""),
                                )
                        except asyncio.TimeoutError:
                            _vlog("⏱️", "C1 workflow timeout (300s)")
                        except Exception as _wf_err:
                            _vlog("⚠️", f"C1 workflow error: {_wf_err}")
                # Non-workflow routes fall through to SmartRouter below
            except Exception as _c1_err:
                _vlog("⚠️", f"C1 TaskRouter error (non-fatal): {_c1_err}")

        # ── v9.36: SmartRouter — thay thế workflow_skill hardcode ────────────
        # SmartRouter.route() → Fast Lane (workflow cứng) hoặc Explorer Lane
        # Nếu handled=True → đã xử lý xong, return luôn
        # Nếu handled=False → tiếp tục xuống SkillForge / ReAct như cũ
        if _HAS_SMART_ROUTER and self._router:
            # Đảm bảo router có telegram_send_fn và llm_fallback mới nhất
            self._router._telegram_send = self._telegram_send_fn
            self._router._ipc           = self._ipc
            if self._forge.enabled:
                self._router._llm_fallback = self._forge._get_stack()

            route_result = await self._router.route(goal, task_id=task_id)

            if route_result.handled:
                tr = route_result.task_result
                if tr is not None:
                    _vlog("🧭", f"SmartRouter handled [{route_result.lane_used}] "
                          f"{'✅' if tr.success else '❌'} "
                          f"{route_result.latency_ms}ms")

                    # ── FIX v1.0: ReAct verify + self-correct ───
                    try:
                        from core.react_controller import get_react_controller
                        _react = get_react_controller(
                            llm_client=self._llm,
                            notify_fn=self._telegram_send_fn,
                        )
                        tr = await _react.maybe_correct(
                            goal=goal,
                            result=tr,
                            method=route_result.lane_used,
                            router=self._router,
                            task_id=task_id,
                        )
                    except Exception:
                        pass  # Verify failed — use original result

                    self._monitor.record_task_end(task_id=task_id, success=tr.success)
                    return TaskResult(
                        task_id=task_id, goal=goal,
                        success=tr.success,
                        steps_taken=getattr(tr, "steps_taken", 0),
                        episode_id=getattr(tr, "episode_id", ""),
                        error=getattr(tr, "error", ""),
                    )
                # clarify lane — handled=True but no task_result
                self._monitor.record_task_end(task_id=task_id, success=True)
                return TaskResult(task_id=task_id, goal=goal, success=True,
                                  steps_taken=0, episode_id="", error="")

            # Explorer lane — inject intent context vào goal nếu có
            if route_result.intent and route_result.lane_used == "explorer":
                intent = route_result.intent
                _vlog("🔍", f"Explorer: intent={intent.intent_type} "
                      f"apps={intent.apps_needed} "
                      f"complexity={intent.complexity}")
                # Enrich goal với context để ReAct LLM biết hướng
                if intent.complexity in ("medium", "complex") and intent.steps:
                    goal = (
                        f"{goal}\n"
                        f"[Steps hint: {' → '.join(intent.steps[:4])}]"
                    )

        else:
            # ── Fallback: v9.22 PRE-CHECK workflow_skill (backward compat) ───
            # Giữ lại khi SmartRouter chưa available
            _early_plan = await self._decomposer.decompose(goal)
            if _early_plan and getattr(_early_plan, "workflow_skill", ""):
                ws = _early_plan.workflow_skill
                _vlog("🕷️", f"Workflow shortcut ({ws}) — gọi trực tiếp")
                if ws == "workflow_spider_social_post":
                    try:
                        from skills.workflow_spider_social_post import run_spider_social_post_workflow
                        wf_result = await run_spider_social_post_workflow(
                            ipc_client=self._ipc,
                            llm_fallback=self._forge._get_stack() if self._forge.enabled else None,
                            telegram_send_fn=self._telegram_send_fn,
                            goal=goal,
                        )
                        self._monitor.record_task_end(task_id=task_id, success=wf_result.success)
                        return TaskResult(
                            task_id=task_id, goal=goal,
                            success=wf_result.success,
                            steps_taken=wf_result.steps_done,
                            episode_id="",
                            error=wf_result.error if not wf_result.success else "",
                        )
                    except Exception as exc:
                        _vlog("⚠️", f"Workflow {ws} error: {str(exc)[:80]} → fallback")

        # ── Skill Forge: priority 4 (Gemini code generation) ─────
        if self._forge.enabled:
            # Anti-loop check
            _forge_allowed = True
            if self._intelligence.enabled:
                _lc = self._intelligence._anti_loop.check_before_call("__forge__", goal)
                if not _lc.get("allowed", True):
                    _vlog("🔁", f"Forge bị chặn: {_lc['reason']}")
                    _forge_allowed = False

            if _forge_allowed:
                _vlog("🔨", "SmartAction không khớp → thử Skill Forge (Gemini)...")
                semantic_ctx = self._semantic.get_context_for(goal)
                if semantic_ctx:
                    _vlog("🧠", f"Semantic context: {semantic_ctx[:60]}...")
                forge_result = await self._forge.execute(
                    goal, telegram_send_fn=self._file_sender,
                )
                if forge_result.success:
                    # Validate output
                    _out_ok = True
                    if self._intelligence.enabled:
                        _val = self._intelligence.validate_output(
                            forge_result.message, goal,
                        )
                        _vlog("🔍", f"Output: confidence={_val.confidence:.0%} "
                              f"{'✅' if _val.valid else '⚠️ ' + str(_val.issues[:2])}")
                        _out_ok = _val.valid

                    # Record to registry for FUTURE reuse (THE BRIDGE)
                    if self._intelligence.enabled:
                        self._intelligence.record_outcome(
                            goal=goal,
                            skill_name=forge_result.skill_name or "",
                            success=True,
                            code=getattr(forge_result, 'code', ''),
                            runtime_ms=forge_result.duration_ms,
                            provider=getattr(forge_result, 'provider_used', ''),
                            source="forge",
                        )
                        # v9.22: Check auto-promotion after every Forge success
                        if forge_result.skill_name:
                            promoted = self._intelligence.check_auto_promote(
                                forge_result.skill_name
                            )
                            if promoted:
                                await self._bus.publish("SKILL_PROMOTED", {
                                    "skill_name": forge_result.skill_name,
                                    "goal": goal,
                                }, source="auto_promoter")

                    _vlog("📊", f"Skill Forge thành công: {forge_result.skill_name} "
                          f"({forge_result.api_calls} API calls, {forge_result.duration_ms}ms)")
                    if forge_result.message:
                        _vlog("💬", forge_result.message[:100])
                    # FIX v4.3: _out_ok used to be computed and ignored.
                    if not _out_ok:
                        self._monitor.record_task_end(task_id=task_id, success=False)
                        self._observe_memory(goal, False, method="skill_forge",
                                             error="output validation failed")
                        return TaskResult(task_id=task_id, goal=goal, success=False, steps_taken=1,
                                          error=f"Skill chạy xong nhưng kết quả không hợp lệ: "
                                                f"{(forge_result.message or '')[:150]}")
                    self._observe_memory(goal, True, method=f"skill_forge:{forge_result.skill_name}",
                                         detail=(forge_result.message or "")[:300])
                    if self._telegram_send_fn and forge_result.message:
                        try:
                            await self._telegram_send_fn(f"✅ {forge_result.message[:3500]}")
                        except Exception:
                            pass
                    await self._bus.publish("TASK_COMPLETED", {
                        "task_id": task_id, "goal": goal, "method": "skill_forge",
                        "skill": forge_result.skill_name,
                    }, source="skill_forge")
                    self._monitor.record_task_end(task_id=task_id, success=True)
                    return TaskResult(task_id=task_id, goal=goal, success=True,
                                      steps_taken=1, error="")
                else:
                    _vlog("⚠️", f"Skill Forge thất bại: {forge_result.error[:80]}")
                    if self._intelligence.enabled:
                        self._intelligence.record_outcome(
                            goal=goal,
                            skill_name=forge_result.skill_name or "__forge__",
                            success=False, source="forge",
                        )
                    self._failure_memory.record_failure(
                        error_type="SkillForgeError",
                        error_message=forge_result.error[:200],
                        context={"goal": goal},
                        goal=goal,
                    )

        # ── Workflow Library: priority 4b-cache (0 API calls) ───
        cached_plan = self._workflow_lib.get_cached_plan(goal)
        if cached_plan:
            _vlog("📦", f"Dùng cached plan ({cached_plan.hit_count} lần đã dùng)")
            try:
                from planner.task_decomposer import TaskPlan
                plan = TaskPlan.from_dict(cached_plan.plan_dict) if hasattr(
                    __import__("planner.task_decomposer", fromlist=["TaskPlan"]), "TaskPlan"
                ) else None
                if plan and plan.total >= 2:
                    result = await self._execute_plan_scheduled(task_id, goal, plan)
                    self._workflow_lib.record_plan_outcome(goal, result.success)
                    # v2: find matching template and record outcome
                    wf_match = self._workflow_lib.find_workflow(goal, min_score=0.85)
                    if wf_match:
                        self._workflow_lib.record_workflow_outcome(
                            wf_match.id, success=result.success
                        )
                    return result
            except Exception as exc:
                _vlog("⚠️", f"Cached plan load lỗi: {str(exc)[:60]}, decompose mới")

        # ── Task Decomposer: priority 4b (multi-step plan) ──────
        _vlog("🧩", "Thử phân tách tác vụ thành nhiều bước...")
        plan = await self._decomposer.decompose(goal)
        if plan and plan.total >= 2:
            _vlog("📋", f"Kế hoạch {plan.total} bước ({plan.source}):")
            for t in plan.tasks:
                deps = f" [sau: {','.join(t.depends_on)}]" if t.depends_on else ""
                _vlog("  ▸", f"{t.id}: {t.description}{deps}")

            # v9.20 Phase 4: Analyze and log parallelism potential
            scheduled_tasks = plan_to_scheduled_tasks(plan)
            parallelism = analyze_parallelism(scheduled_tasks)
            if parallelism["max_parallel"] > 1:
                _vlog("⚡", f"Parallel: {parallelism['waves']} waves, "
                      f"max {parallelism['max_parallel']} tasks đồng thời")

            await self._bus.publish("TASK_PLANNED", {
                "task_id": task_id, "goal": goal, "steps": plan.total,
                "source": plan.source, "parallelism": parallelism,
            }, source="decomposer")

            # Cache plan for future reuse (0 API calls next time)
            self._workflow_lib.cache_plan(goal, plan.to_dict(), source=plan.source)

            # Execute via Scheduler (parallel where possible)
            result = await self._execute_plan_scheduled(task_id, goal, plan)
            return result

        _vlog("🔄", "Không phân tách được → chuyển sang LLM + VLM loop...")

        # ── Priority 5: Full LLM + VLM loop ─────────────────────
        task_graph = TaskGraph(goal=goal)

        # v4.3: relevant long-term memories for the (local) reasoner
        _memory_ctx = ""
        try:
            _mem = self._memory()
            if _mem is not None:
                _memory_ctx = await asyncio.wait_for(_mem.build_context(goal, max_chars=1200), timeout=5)
        except Exception:
            _memory_ctx = ""

        steps_taken = 0
        success     = False
        error_msg   = ""
        # v4.3 lazy vision: after keyboard / accessibility actions the cheap
        # ui_snapshot describes the new state, so the VLM is skipped.
        _last_action, _last_ok = "", True
        try:
            from core.model_registry import config_section as _cfg_section
            _lazy_vision = bool(_cfg_section("models").get("lazy_vision", True))
        except Exception:
            _lazy_vision = True

        try:
            for step_n in range(1, self._max_steps + 1):
                steps_taken = step_n
                _vlog("🔄", f"Bước {step_n}/{self._max_steps} — đang suy luận...")

                # ── Perceive + ReAct (Phase 2: parallel) ──────────────────
                # VLM perception and LLM reasoning are independent at each
                # step — perception captures the current screen state while
                # the reasoner decides the next action based on task_memory
                # context from previous steps.  Running them in parallel
                # saves 2–6s per step (the full VLM latency).
                #
                # FIX C-01/H-05: use return_exceptions=True so perception
                # failure does NOT cancel the reasoning task.
                context = self._task_memory.get_context()

                # FIX v4.3: perceive FIRST, then reason about what is actually on
                # screen.  The old code ran both in parallel and never passed the
                # perception result to the reasoner, so the LLM guessed
                # coordinates blind.
                _ui_state = await self._ui_state()
                _need_vision = (not _lazy_vision or step_n == 1 or not _last_ok
                                or _last_action not in self._NO_VISION_AFTER or _ui_state is None)
                if _need_vision:
                    try:
                        percept_result = await self._perception.perceive()
                    except Exception as _pe:
                        percept_result = _pe
                    _screen_summary = (
                        "" if isinstance(percept_result, BaseException)
                        else self._summarize_percept(percept_result)
                    )
                else:
                    percept_result = None
                    _vlog("⚡", "Bỏ qua VLM (thao tác bàn phím/Accessibility) — dùng ui_snapshot")
                    _screen_summary = ("(screen not re-captured after a keyboard/accessibility action — "
                                       "use ui_snapshot / element_find, or screenshot_capture to look again)")
                try:
                    reason_result = await self._reasoner.step_streaming(
                        goal=goal,
                        context=context,
                        step_n=step_n,
                        screen_summary=_screen_summary,
                        memory_context=_memory_ctx,
                        ui_state=_ui_state,
                    )
                except Exception as _re:
                    reason_result = _re

                # Perception failure is non-fatal
                if isinstance(percept_result, BaseException):
                    _log.warning(
                        "AgentLoop: perception failed — continuing without vision",
                        extra={
                            "step":  step_n,
                            "error": str(percept_result)[:120],
                        },
                    )

                # Reasoning failure IS fatal
                if isinstance(reason_result, BaseException):
                    if isinstance(reason_result, ReActError):
                        raise reason_result
                    raise ReActError(
                        f"LLM reasoning failed at step {step_n}: {reason_result}",
                        step_n=step_n,
                        reason="REASONING_FAILED",
                    ) from reason_result

                react_result = reason_result

                # Record thought
                self._task_memory.add_thought(react_result.thought)

                # v9.33: Record monologue vào MonologueStore
                if _HAS_MONOLOGUE and hasattr(self, "_monologue_store"):
                    self._monologue_store.record(
                        step_n=react_result.step_n,
                        action=react_result.action_name,
                        monologue=react_result.monologue,
                        confidence=react_result.confidence,
                        is_critical=react_result.is_critical,
                    )

                # ── Dispatch via IPC (R-01, R-02, R-05) ───────────────────
                action_name    = react_result.action_name
                action_payload = react_result.action_payload

                # FIX VLM-01 (v4.3): coordinates given to the reasoner are already
                # converted to screen points in _summarize_percept, the same space
                # as Accessibility results (element_find / ui_snapshot).  The old
                # post-hoc scaling multiplied AX coordinates too and used
                # end_x/end_y while mouse_drag uses from_x/from_y/to_x/to_y.

                self._task_memory.add_action(action_name, action_payload)
                # Vietnamese log
                _payload_summary = str(action_payload)[:60]
                _vlog("▸ ", f"Thực hiện: \033[1m{action_name}\033[0m ({_payload_summary})")

                try:
                    ipc_response: IPCResponse = await self._ipc.send_action(
                        action_name, action_payload
                    )
                    observation = (
                        f"Action {action_name!r} succeeded. "
                        f"Result: {self._format_observation(action_name, ipc_response.result)}"
                        if ipc_response.success
                        else f"Action {action_name!r} failed: {ipc_response.error_code} "
                             f"{str(ipc_response.result or '')[:200]}"
                    )
                    _last_action, _last_ok = action_name, bool(ipc_response.success)

                    # v9.34 B1: LoopDetector — check sau mỗi dispatch
                    if _HAS_LOOP_DETECTOR and hasattr(self, "_loop_detector"):
                        _x = action_payload.get("x", 0)
                        _y = action_payload.get("y", 0)
                        _loop_signal = self._loop_detector.record(
                            action_name=action_name,
                            action_payload=action_payload,
                            screenshot=None,   # screenshot optional, tránh overhead mỗi step
                            x=_x, y=_y,
                        )
                        if _loop_signal.value != LoopSignal.CLEAR.value:
                            await self._loop_detector.handle(
                                signal=_loop_signal,
                                ipc_client=self._ipc,
                                notify_fn=self._telegram_send_fn,
                            )

                    # Phase 3: If primary action failed and fallback exists,
                    # try the fallback action automatically.
                    # FIX S8: only check fallback_action — payload {} is valid
                    if (not ipc_response.success
                            and react_result.fallback_action):
                        _log.info(
                            "AgentLoop: primary action failed — trying fallback",
                            extra={
                                "step":     step_n,
                                "primary":  action_name,
                                "fallback": react_result.fallback_action,
                            },
                        )
                        try:
                            fb_response: IPCResponse = await self._ipc.send_action(
                                react_result.fallback_action,
                                react_result.fallback_payload,
                            )
                            if fb_response.success:
                                observation = (
                                    f"Fallback {react_result.fallback_action!r} "
                                    f"succeeded. Result: {str(fb_response.result)[:256]}"
                                )
                        except Exception as fb_exc:
                            _log.debug(
                                "AgentLoop: fallback also failed",
                                extra={"error": str(fb_exc)[:100]},
                            )

                except Exception as exc:
                    # Phase 3: try fallback on IPC dispatch error too
                    observation = f"IPC dispatch error: {str(exc)[:256]}"
                    _last_action, _last_ok = action_name, False
                    self._monitor.record_error(str(exc), task_id=task_id)

                    # FIX S8: only check fallback_action — payload {} is valid
                    if react_result.fallback_action:
                        _log.info(
                            "AgentLoop: dispatch error — trying fallback",
                            extra={
                                "step":     step_n,
                                "fallback": react_result.fallback_action,
                            },
                        )
                        try:
                            fb_response = await self._ipc.send_action(
                                react_result.fallback_action,
                                react_result.fallback_payload,
                            )
                            if fb_response.success:
                                observation = (
                                    f"Fallback {react_result.fallback_action!r} "
                                    f"succeeded after dispatch error. "
                                    f"Result: {str(fb_response.result)[:256]}"
                                )
                        except Exception:
                            pass  # fallback also failed — keep original error

                self._task_memory.add_observation(observation)

                # v4.3: the model reported the goal cannot be achieved (task_complete
                # with success=false) — stop instead of burning the step budget.
                if react_result.thought.startswith("Task failed:"):
                    error_msg = react_result.thought[len("Task failed:"):].strip()[:300] or "Không thể hoàn thành"
                    _vlog("⛔", f"Agent dừng: {error_msg[:80]}")
                    break

                # Check completion FIRST — if done, no need to check stuck
                if self._is_task_complete(react_result.thought, observation):
                    success = True
                    _vlog("✅", f"Tác vụ hoàn thành sau {step_n} bước!")
                    _log.info(
                        "AgentLoop: task completed",
                        extra={"task_id": task_id, "steps": step_n},
                    )
                    break

                # FIX S1: Check stuck AFTER step fully executes.
                # Was at top of loop → step N never executed when N=max_steps.
                # Now at bottom → all max_steps steps run before abort.
                self._monitor.record_loop_iteration()
                health = self._monitor.check_health()
                if health.stuck:
                    error_msg = (
                        f"Task aborted: max_steps={self._max_steps} reached "
                        "without completion."
                    )
                    _vlog("⛔", f"Tác vụ bị huỷ — đã thực hiện {self._max_steps} bước mà chưa xong")
                    _log.error(
                        "AgentLoop: max_steps reached — aborting task",
                        extra={"task_id": task_id, "steps": step_n},
                    )
                    break

        except Exception as exc:
            error_msg = str(exc)
            _vlog("❌", f"Lỗi: {error_msg[:80]}")
            self._monitor.record_error(error_msg, task_id=task_id)
            _log.error(
                "AgentLoop: unexpected error",
                extra={"task_id": task_id, "error": error_msg},
                exc_info=True,
            )

        # ── Distil episode (R-16) ──────────────────────────────────────────
        episode_id = ""
        if success:
            episode_id = self._store_episode(task_id, goal, steps_taken)

        # ── Cleanup (M-7) ──────────────────────────────────────────────────
        self._task_memory.clear()
        self._monitor.record_task_end(success=success)

        # v9.33: Log monologue summary + clear store
        if _HAS_MONOLOGUE and hasattr(self, "_monologue_store"):
            _vlog("🧠", f"Monologue: {self._monologue_store.summary()}")
            self._monologue_store.clear()

        # v9.34 B1: Log loop stats + reset
        if _HAS_LOOP_DETECTOR and hasattr(self, "_loop_detector"):
            stats = self._loop_detector.stats()
            if stats["intervention_count"] > 0:
                _vlog("🔁", f"LoopDetector: {stats['intervention_count']} can thiệp "
                      f"| last={stats['last_signal']}")
            self._loop_detector.reset()
        return TaskResult(
            task_id=task_id,
            goal=goal,
            success=success,
            steps_taken=steps_taken,
            episode_id=episode_id,
            error=error_msg,
        )

    # ------------------------------------------------------------------
    # Plan execution (priority 4b: multi-step tasks)
    # ------------------------------------------------------------------

    async def _execute_plan(self, task_id: str, goal: str, plan) -> TaskResult:
        """
        Execute a TaskPlan step by step.

        Each step is executed via SkillForge (code generation) or
        SmartAction (instant) depending on the action type.
        Results are passed between steps via context dict.
        """
        from planner.task_decomposer import SubTask

        # ── v9.36: workflow_skill shortcut qua SmartRouter ──────────────────
        ws = getattr(plan, "workflow_skill", "")
        if ws == "workflow_spider_social_post":
            _vlog("🕷️", "Detected spider_social_post workflow → chạy trực tiếp SpiderSocialPostWorkflow")
            try:
                from skills.workflow_spider_social_post import run_spider_social_post_workflow
                wf_result = await run_spider_social_post_workflow(
                    ipc_client=self._ipc,
                    llm_fallback=self._forge._get_stack() if self._forge.enabled else None,
                    telegram_send_fn=self._telegram_send_fn,
                    goal=goal,   # v9.22: pass goal so workflow can parse image/content params
                )
                if wf_result.success:
                    _vlog("🕷️", (
                        f"Workflow OK — ảnh: {wf_result.image_path} | "
                        f"content: {len(wf_result.content_text)} ký tự | "
                        f"{wf_result.duration_s:.0f}s"
                    ))
                    return TaskResult(
                        task_id=task_id,
                        goal=goal,
                        success=True,
                        steps_taken=wf_result.steps_done,
                        episode_id="",
                        error="",
                    )
                else:
                    _vlog("⚠️", f"Workflow thất bại ở B{wf_result.steps_done}: {wf_result.error[:80]}")
                    # Fall through to step-by-step as last resort
            except Exception as exc:
                _vlog("⚠️", f"workflow_spider_social_post import/run error: {exc} — fallback to subtasks")

        context: dict[str, Any] = {"goal": goal, "task_id": task_id}
        step_outputs: dict[str, Any] = {}
        steps_done = 0

        # v9.20: Start goal tracking
        sub_task_dicts = [{"id": t.id, "description": t.description, "status": "pending"}
                          for t in plan.tasks]
        self._goal_tracker.begin(goal, sub_task_dicts)

        for step in plan.tasks:
            # Check dependencies
            dep_ok = all(
                any(t.id == dep and t.status == "done" for t in plan.tasks)
                for dep in step.depends_on
            )
            if not dep_ok:
                step.status = "skipped"
                _vlog("⏭️", f"{step.id}: bỏ qua (dependency chưa xong)")
                continue

            # v9.20: Check goal alignment BEFORE executing
            alignment = self._goal_tracker.check_alignment(step.description)
            if not alignment["aligned"]:
                _vlog("🎯", f"Drift detected — bỏ qua bước không liên quan")
                if alignment.get("should_replan"):
                    _vlog("🔄", "Quá nhiều drift — cần replan")
                    break
                continue

            step.status = "running"
            _vlog("▶️ ", f"{step.id}/{plan.total}: {step.description}")
            # Show progress bar
            prog_bar = self._goal_tracker.progress_bar()
            if prog_bar:
                _vlog("📊", prog_bar)

            await self._bus.publish("TASK_STEP_START", {
                "task_id": task_id, "step_id": step.id,
                "action": step.action, "description": step.description,
            }, source="plan_executor")

            try:
                # Build step goal for SkillForge
                step_goal = step.description
                if step.params:
                    step_goal += f" (params: {json.dumps(step.params, ensure_ascii=False)})"

                # Add context from previous steps
                if context.get("previous_result"):
                    step_goal += f"\nKết quả bước trước: {str(context['previous_result'])[:200]}"

                # Try SmartAction first for simple actions
                if step.action in ("open_app", "keyboard_action", "navigate_url"):
                    smart_r = await self._smart.execute(step_goal)
                    if smart_r and smart_r.success:
                        step.status = "done"
                        step.result = smart_r.result
                        context["previous_result"] = smart_r.result
                        steps_done += 1
                        step_outputs[step.id] = smart_r.result
                        self._checkpoint.save(task_id, goal, plan.to_dict(), steps_done - 1, step_outputs)
                        self._goal_tracker.record_step(step.id, step.action, True)
                        _vlog("✅", f"{step.id}: xong (SmartAction)")
                        continue

                # Use SkillForge for data/code tasks
                if self._forge.enabled:
                    forge_r = await self._forge.execute(
                        step_goal, telegram_send_fn=self._file_sender,
                    )
                    if forge_r.success:
                        # v9.21: Validate output + record to registry
                        if self._intelligence.enabled:
                            _sv = self._intelligence.validate_output(forge_r.message, step_goal)
                            if not _sv.valid:
                                _vlog("⚠️", f"Step output invalid: {_sv.issues[:2]}")
                            self._intelligence.record_outcome(
                                goal=step_goal, skill_name=forge_r.skill_name or "",
                                success=True, code=getattr(forge_r, 'code', ''),
                                runtime_ms=forge_r.duration_ms,
                                provider=getattr(forge_r, 'provider_used', ''),
                                source="forge",
                            )
                        step.status = "done"
                        step.result = forge_r.message
                        context["previous_result"] = forge_r.message
                        if forge_r.files:
                            context["output_files"] = forge_r.files
                        steps_done += 1
                        step_outputs[step.id] = forge_r.message
                        self._checkpoint.save(task_id, goal, plan.to_dict(), steps_done - 1, step_outputs)
                        self._goal_tracker.record_step(step.id, step.action, True)
                        _vlog("✅", f"{step.id}: xong (SkillForge)")
                        continue
                    else:
                        # v9.21: Record forge failure to intelligence
                        if self._intelligence.enabled:
                            self._intelligence.record_outcome(
                                goal=step_goal, skill_name="__forge__",
                                success=False, source="forge",
                            )
                        # Record failure
                        self._failure_memory.record_failure(
                            error_type=f"StepFailed_{step.action}",
                            error_message=forge_r.error[:200],
                            context={"step_id": step.id, "goal": step_goal[:100]},
                            goal=goal,
                        )

                        # v9.20 Phase 4 Self-Healing v2: adaptive strategy
                        _vlog("🔧", f"Self-Healing v2 cho bước {step.id}...")
                        heal_result = await self._healer.heal(
                            error=forge_r.error,
                            context={"goal": goal, "step": step.action,
                                     "step_goal": step_goal, "file": step_goal[:50]},
                            attempt=1,
                        )
                        if heal_result.healed:
                            _vlog("💊", f"Fix [{heal_result.source}] "
                                  f"confidence={heal_result.confidence:.0%}: "
                                  f"{heal_result.strategy_description[:60]}")
                            # v2: use build_fix_prompt_suffix for richer hint
                            fix_suffix = self._healer.build_fix_prompt_suffix(heal_result)
                            healed_goal = step_goal + fix_suffix

                            # v2: provider-aware — switch if strategy demands it
                            if heal_result.next_provider and hasattr(self._forge, "_get_stack"):
                                _vlog("🔌", f"Healing → force provider: {heal_result.next_provider}")

                            retry_r = await self._forge.execute(
                                healed_goal, telegram_send_fn=self._file_sender,
                            )
                            if retry_r.success:
                                step.status = "done"
                                step.result = retry_r.message
                                context["previous_result"] = retry_r.message
                                steps_done += 1
                                step_outputs[step.id] = retry_r.message
                                self._healer.record_outcome(
                                    heal_result.error_type, heal_result.strategy_name,
                                    success=True, goal=goal,
                                    context={"step": step.action},
                                )
                                _vlog("✅", f"{step.id}: xong (Self-Healing v2)")
                                self._goal_tracker.record_step(step.id, step.action, True)
                                continue
                            else:
                                self._healer.record_outcome(
                                    heal_result.error_type, heal_result.strategy_name,
                                    success=False, goal=goal,
                                    context={"step": step.action},
                                )

                step.status = "failed"
                step.error = "Không thể thực hiện bước này"
                self._goal_tracker.record_step(step.id, step.action, False)
                _vlog("❌", f"{step.id}: thất bại")

                await self._bus.publish("TASK_STEP_DONE", {
                    "task_id": task_id, "step_id": step.id,
                    "success": False, "error": step.error,
                }, source="plan_executor")

                # ── v9.21: Recovery Engine — DOM capture + re-planning ──
                _policy = self._recovery.get_policy(step_goal, step.action)
                _vlog("🛡", f"Recovery policy: type={_policy['skill_type']}, "
                      f"strategies={_policy['strategies'][:2]}")

                # DOM capture for UI/network tasks
                _dom_diag = {}
                if _policy.get("capture_dom"):
                    _vlog("🌐", "Capturing DOM state...")
                    _dom = await self._recovery.capture_dom()
                    if _dom:
                        _dom_diag = self._recovery.diagnose_dom(_dom)
                        _vlog("🌐", f"DOM: page={_dom_diag.get('page_type','?')}, "
                              f"errors={_dom_diag.get('has_errors')}, "
                              f"modal={_dom_diag.get('has_modal')}")
                        # Inject DOM context into failure memory for future healing
                        self._failure_memory.record_failure(
                            error_type=f"StepFailed_{step.action}",
                            error_message=step.error,
                            context={
                                "step_id": step.id,
                                "goal": step_goal[:100],
                                "dom_page_type": _dom_diag.get("page_type", ""),
                                "dom_errors": _dom_diag.get("error_messages", [])[:2],
                                "dom_buttons": _dom_diag.get("available_buttons", [])[:5],
                            },
                            goal=goal,
                        )

                # Dynamic re-planning: try alternative approach
                _completed_ids = [t.id for t in plan.tasks if t.status == "done"]
                replan = await self._recovery.try_replan(
                    task_id=task_id,
                    goal=goal,
                    failed_step_id=step.id,
                    failed_step_desc=step.description,
                    failed_step_action=step.action,
                    failure_reason=step.error,
                    completed_steps=_completed_ids,
                    step_outputs=step_outputs,
                    dom_diagnostics=_dom_diag,
                )

                if replan.success and replan.new_plan:
                    _vlog("🔄", f"Re-plan thành công! {replan.new_plan.total} bước mới")
                    _vlog("📋", replan.explanation)
                    # Execute the new plan recursively
                    replan_result = await self._execute_plan_sequential(
                        task_id, goal, replan.new_plan, context,
                    )
                    self._recovery.reset_task(task_id)
                    return replan_result
                else:
                    _vlog("⚠️", f"Re-plan thất bại: {replan.explanation}")

                # If no re-plan available, break (original behavior)
                break

            except Exception as exc:
                step.status = "failed"
                step.error = str(exc)[:200]
                # v9.21: Capture DOM if UI/network task
                _exc_policy = self._recovery.get_policy(step_goal, step.action)
                _exc_dom_ctx = {}
                if _exc_policy.get("capture_dom"):
                    try:
                        _exc_dom = await self._recovery.capture_dom()
                        if _exc_dom:
                            _exc_dom_ctx = self._recovery.diagnose_dom(_exc_dom)
                    except Exception:
                        pass
                self._failure_memory.record_failure(
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                    context={
                        "step_id": step.id,
                        "dom_page_type": _exc_dom_ctx.get("page_type", ""),
                    },
                    goal=goal,
                )
                _vlog("❌", f"{step.id}: lỗi — {str(exc)[:60]}")
                break

        # ── Plan result ───────────────────────────────────────────
        success = plan.is_complete
        if success:
            _vlog("✅", f"Kế hoạch hoàn thành! {steps_done}/{plan.total} bước")
            self._checkpoint.complete(task_id)

            # v9.20: Learn from success — extract knowledge
            step_dicts = [{"action": t.action, "description": t.description,
                           "result": t.result} for t in plan.tasks if t.status == "done"]
            learned = self._semantic.extract_from_task(goal, step_dicts, success=True)
            if learned:
                _vlog("🧠", f"Học được {learned} kiến thức mới")

            await self._bus.publish("TASK_COMPLETED", {
                "task_id": task_id, "goal": goal, "method": "plan_executor",
                "steps_done": steps_done, "total_steps": plan.total,
            }, source="plan_executor")
        else:
            _vlog("⚠️", f"Kế hoạch dừng tại bước {steps_done}/{plan.total}")
            self._checkpoint.fail(task_id, f"Stopped at step {steps_done}/{plan.total}")
            await self._bus.publish("TASK_FAILED", {
                "task_id": task_id, "goal": goal, "method": "plan_executor",
                "steps_done": steps_done,
            }, source="plan_executor")

        # v9.20: End goal tracking
        goal_summary = self._goal_tracker.end()
        if goal_summary:
            _vlog("📊", f"Goal summary: {goal_summary.get('steps_done', 0)} bước, "
                  f"drift {goal_summary.get('drift_count', 0)}, "
                  f"thời gian {goal_summary.get('duration_s', 0)}s")

        self._monitor.record_task_end(task_id=task_id, success=success)
        self._task_memory.clear()

        return TaskResult(
            task_id=task_id, goal=goal,
            success=success,
            steps_taken=steps_done,
            error="" if success else f"Dừng tại bước {steps_done}/{plan.total}",
        )

    async def _execute_plan_sequential(
        self, task_id: str, goal: str, plan, context: dict | None = None,
    ) -> "TaskResult":
        """
        Run a re-planned TaskPlan step by step.

        FIX v4.3: called by the Recovery Engine re-plan path but never
        defined → AttributeError, so a successful re-plan was never executed.
        Depth-guarded so a re-plan cannot recurse forever.
        """
        depth = getattr(self, "_replan_depth", 0)
        if depth >= 2:
            return TaskResult(task_id=task_id, goal=goal, success=False, steps_taken=0,
                              error="Re-plan depth limit reached (2)")
        self._replan_depth = depth + 1
        try:
            return await self._execute_plan(task_id, goal, plan)
        finally:
            self._replan_depth = depth

    # ------------------------------------------------------------------
    # Scheduled plan execution (Phase 4A: parallel via TaskScheduler)
    # ------------------------------------------------------------------

    async def _execute_plan_scheduled(self, task_id: str, goal: str, plan) -> "TaskResult":
        """
        Execute plan via TaskScheduler — runs independent tasks in parallel.
        Falls back to sequential _execute_plan() if scheduler fails.
        """
        try:
            scheduled_tasks = plan_to_scheduled_tasks(plan)
            if not scheduled_tasks:
                return await self._execute_plan(task_id, goal, plan)

            sched_result = await self._scheduler.run(
                task_id=task_id,
                goal=goal,
                tasks=scheduled_tasks,
                context={"goal": goal, "task_id": task_id},
            )

            success = sched_result.success
            if success:
                _vlog("✅", f"Scheduler: {sched_result.done_count}/{sched_result.total_tasks} tasks "
                      f"({sched_result.total_duration_ms}ms, saved ~{sched_result.parallel_savings_ms}ms)")
                # Extract workflow for future reuse
                step_dicts = [
                    {"action": t.action, "description": t.description,
                     "result": str(t.result)[:100] if t.result else ""}
                    for t in scheduled_tasks if t.status == "done"
                ]
                learned = self._semantic.extract_from_task(goal, step_dicts, success=True)
                if learned:
                    _vlog("🧠", f"Học được {learned} kiến thức mới")
                # Auto-extract workflow template (v2: updates score)
                extracted = self._workflow_lib.auto_extract_workflow(
                    task_id, goal, step_dicts, sched_result.total_duration_ms
                )
                if extracted:
                    self._workflow_lib.record_workflow_outcome(
                        extracted.id, success=True,
                        duration_ms=sched_result.total_duration_ms,
                    )

            await self._bus.publish("TASK_COMPLETED" if success else "TASK_FAILED", {
                "task_id": task_id, "goal": goal, "method": "scheduler",
                "done": sched_result.done_count, "total": sched_result.total_tasks,
                "parallel_savings_ms": sched_result.parallel_savings_ms,
            }, source="scheduler")

            self._checkpoint.complete(task_id)
            self._monitor.record_task_end(task_id=task_id, success=success)
            self._task_memory.clear()

            return TaskResult(
                task_id=task_id, goal=goal, success=success,
                steps_taken=sched_result.done_count,
                error="" if success else f"{sched_result.failed_count} tasks thất bại",
            )

        except Exception as exc:
            _vlog("⚠️", f"Scheduler error: {str(exc)[:80]}, fallback → sequential")
            return await self._execute_plan(task_id, goal, plan)

    async def _schedule_executor(
        self, task: "ScheduledTask", ctx: dict
    ) -> "tuple[bool, Any, str]":
        """
        Executor function called by TaskScheduler for each ScheduledTask.
        Tries SmartAction → SkillForge → SelfHealing.
        Returns (success, result, error).
        """
        goal = task.description
        if ctx.get("previous_result"):
            goal += f"\nKết quả trước: {str(ctx['previous_result'])[:150]}"

        # Try SmartAction first for instant tasks
        if task.action in ("open_app", "keyboard_action", "navigate_url", "app_launch"):
            smart_r = await self._smart.execute(goal)
            if smart_r and smart_r.success:
                return True, smart_r.result, ""

        # SkillForge for data/code tasks
        if self._forge.enabled:
            forge_r = await self._forge.execute(
                goal, telegram_send_fn=self._file_sender
            )
            if forge_r.success:
                return True, forge_r.message, ""

            # Self-healing retry
            self._failure_memory.record_failure(
                error_type=f"Step_{task.action}",
                error_message=forge_r.error[:200],
                context={"step_id": task.id, "goal": goal[:100]},
                goal=ctx.get("goal", goal),
            )
            heal = await self._healer.heal(
                error=forge_r.error,
                context={"goal": ctx.get("goal", goal), "step": task.action,
                         "step_goal": goal[:80]},
                attempt=1,
            )
            if heal.healed:
                # v2: richer fix prompt via build_fix_prompt_suffix
                fix_suffix = self._healer.build_fix_prompt_suffix(heal)
                healed_goal = goal + fix_suffix
                retry_r = await self._forge.execute(
                    healed_goal, telegram_send_fn=self._file_sender
                )
                self._healer.record_outcome(
                    heal.error_type, heal.strategy_name, success=retry_r.success,
                    goal=ctx.get("goal", goal),
                    context={"step": task.action},
                )
                if retry_r.success:
                    return True, retry_r.message, ""
                return False, None, retry_r.error

            return False, None, forge_r.error

        return False, None, "Không có executor phù hợp"

    @staticmethod
    def _format_observation(action: str, result: Any) -> str:
        """v4.3: informational actions return text the model must read in full."""
        limits = {"ui_snapshot": 1500, "menu_list": 3000, "element_find": 2500,
                  "element_get_text": 2000, "clipboard_get": 2000, "window_list": 1500}
        try:
            if action == "menu_list" and isinstance(result, dict):
                text = "\n".join(
                    " > ".join(i.get("path", [])) + (f"  [{i['shortcut']}]" if i.get("shortcut") else "")
                    + ("" if i.get("enabled", True) else "  (disabled)")
                    for i in result.get("items", []))
            elif action == "element_find" and isinstance(result, list):
                text = "\n".join(f"{e.get('element_id')} role={e.get('role')} title={e.get('title')!r} "
                                 f"center=({e.get('center_x')},{e.get('center_y')})" for e in result[:30])
            elif action == "ui_snapshot" and isinstance(result, dict):
                from core.keyboard_first import UIContext
                text = UIContext.from_snapshot(result).summary(600)
            else:
                text = str(result)
        except Exception:
            text = str(result)
        return text[:limits.get(action, 256)]

    async def _ui_state(self) -> dict | None:
        """Cheap UI state through the ui_snapshot IPC action (None if unavailable)."""
        try:
            resp = await asyncio.wait_for(
                self._ipc.send_action("ui_snapshot", {"include_value": True, "max_chars": 600}), timeout=4)
            if getattr(resp, "success", False) and isinstance(resp.result, dict):
                return resp.result
        except Exception:
            pass
        return None

    _NO_VISION_AFTER = frozenset({
        "keyboard_type", "keyboard_press", "keyboard_hotkey", "menu_select", "menu_list",
        "ui_snapshot", "element_find", "element_get_text", "element_set_value",
        "clipboard_get", "clipboard_copy", "window_list", "user_confirm", "ping",
    })

    @staticmethod
    def _summarize_percept(percept: Any, limit: int = 40) -> str:
        """Compact text view of detected UI elements for the reasoning LLM."""
        try:
            elements = list(getattr(percept, "elements", []) or [])
        except Exception:
            return ""
        if not elements:
            return "(không phát hiện phần tử UI nào)"
        elements.sort(key=lambda e: (getattr(e, "y", 0), getattr(e, "x", 0)))
        try:
            sf = float(getattr(percept, "scale_factor", 1.0) or 1.0)
        except (TypeError, ValueError):
            sf = 1.0
        if sf <= 1.01:
            sf = 1.0
        lines = []
        for e in elements[:limit]:
            label = str(getattr(e, "label", "") or "").replace("\n", " ")[:60]
            # screen points (VLM image coordinates × scale factor)
            cx = int(getattr(e, "center_x", getattr(e, "x", 0)) * sf)
            cy = int(getattr(e, "center_y", getattr(e, "y", 0)) * sf)
            lines.append(f"- [{getattr(e, 'elem_type', '?')}] \"{label}\" at ({cx},{cy}) "
                         f"conf={float(getattr(e, 'confidence', 0)):.2f}")
        more = len(elements) - limit
        if more > 0:
            lines.append(f"... (+{more} phần tử khác)")
        return "\n".join(lines)

    def _store_episode(
        self,
        task_id:     str,
        goal:        str,
        steps_taken: int,
    ) -> str:
        """
        Distil completed task context into an episode (R-16).

        Returns the episode_id, or empty string on failure.
        """
        episode_id = f"ep-{task_id[:8]}"
        episode = {
            "id":          episode_id,
            "task_id":     task_id,
            "goal":        goal,
            "steps_taken": steps_taken,
            "steps":       self._task_memory.get_context()["steps"],
            "outcome":     "success",
        }
        try:
            self._episodic.store(episode)
            _log.debug(
                "R-16: task episode stored",
                extra={"episode_id": episode_id},
            )
            return episode_id
        except Exception as exc:
            _log.error(
                "AgentLoop: failed to store episode",
                extra={"episode_id": episode_id, "error": str(exc)},
            )
            return ""

    # FIX C-02: completion patterns with word boundaries — no more false
    # positives on "not done yet", "unfinished", "loading is done".
    _COMPLETION_PATTERNS = [
        re.compile(r'\btask\s+(?:is\s+)?complete[d]?\b', re.I),
        re.compile(r'\bgoal\s+(?:is\s+)?achieved\b', re.I),
        re.compile(r'\bsuccessfully\s+completed\b', re.I),
        re.compile(r'\ball\s+steps?\s+(?:are\s+)?done\b', re.I),
        re.compile(r'\btask\s+(?:is\s+)?finished\b', re.I),
        re.compile(r'\btask\s+(?:is\s+)?done\b', re.I),
    ]
    _NEGATION_BEFORE = re.compile(
        r'\b(?:not|never|no|un|isn.t|wasn.t|aren.t|hasn.t|haven.t)\b.{0,30}$',
        re.I,
    )

    def _verify_plan_hmac(self, cached_plan: Any) -> bool:
        """[C-09 FIX] Verify HMAC signature on cached workflow plan."""
        try:
            import hmac as _hmac, hashlib as _hashlib
            key = getattr(self, "_hmac_key", b"phidipus-default-key")
            if isinstance(key, str):
                key = key.encode()
            plan_json = __import__("json").dumps(
                cached_plan.plan_dict, sort_keys=True
            ).encode()
            expected = _hmac.new(key, plan_json, _hashlib.sha256).hexdigest()
            stored_sig = getattr(cached_plan, "hmac_sig", None)
            if stored_sig is None:
                # Legacy unsigned plan — allow but log warning
                import logging
                logging.getLogger("phidipus.agent").warning(
                    "[C-09] No HMAC on cached plan (legacy) — accepting with warning"
                )
                return True
            return _hmac.compare_digest(expected, stored_sig)
        except Exception:
            return True  # Fail open for legacy plans


    # FIX v4.3: the @staticmethod decorator had drifted onto _verify_plan_hmac,
    # so self._is_task_complete(thought, obs) raised TypeError (3 positional
    # args) and the P5 LLM+VLM loop crashed at its first step.
    @staticmethod
    def _is_task_complete(thought: str, observation: str) -> bool:
        """
        Heuristic check: did the LLM signal task completion?

        FIX C-02: Uses regex with word boundaries and negation detection
        to avoid false positives like "not done yet", "unfinished",
        "loading is done, now click submit".

        Only the LLM *thought* is checked (not the IPC observation),
        because observation may contain words like "done" from app output
        that do not indicate task completion (FIX M-05).
        """
        # Only check the LLM thought — observation from IPC responses
        # can contain "done" in app output without meaning task completion.
        text = thought
        for pat in AgentLoop._COMPLETION_PATTERNS:
            m = pat.search(text)
            if m:
                # Check no negation word in the 30 chars before match
                prefix = text[:m.start()]
                if not AgentLoop._NEGATION_BEFORE.search(prefix):
                    return True
        return False
