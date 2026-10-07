# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/smart_router.py — Phidipus v1.36
═══════════════════════════════════════════════════════════════════════

Smart Router — Cổng điều phối thông minh cho mọi task.

Nhận IntentResult từ UniversalParser → quyết định:
  ⚡ Fast Lane    → gọi workflow/skill cứng đã có (0 VLM overhead)
  🔍 Explorer    → kích hoạt AgentLoop ReAct động
  ❓ Clarify      → hỏi user trước khi làm
  🧠 Learning    → sau Explorer thành công, tự tạo skill mới

Đây là trung tâm điều phối — KHÔNG chứa business logic,
chỉ routing và handoff.

Tích hợp vào agent_loop.py:
  # Thay đoạn _early_plan / workflow_skill check hiện tại bằng:
  router = SmartRouter(agent_loop=self)
  route_result = await router.route(goal)
  if route_result.handled:
      return route_result.task_result

Flow chi tiết:
  goal → UniversalParser.parse() → IntentResult
       → SmartRouter.route() →
           Fast Lane:   gọi workflow class trực tiếp
           Explorer:    gọi agent_loop._execute_react_loop()
           Clarify:     gửi Telegram hỏi user
           Learning:    Explorer thành công → SkillForge.compile()
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.universal_parser import UniversalParser, IntentResult
from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# RouteResult
# ══════════════════════════════════════════════════════════════════

@dataclass
class RouteResult:
    """Kết quả routing — trả về cho agent_loop biết đã xử lý chưa."""
    handled:     bool        = False   # True = router đã xử lý, agent_loop không cần làm gì thêm
    task_result: Any         = None    # TaskResult nếu handled=True
    intent:      Optional[IntentResult] = None
    lane_used:   str         = ""      # "fast" | "explorer" | "clarify" | "skipped"
    latency_ms:  int         = 0


# ══════════════════════════════════════════════════════════════════
# Fast Lane Registry
# ══════════════════════════════════════════════════════════════════

# Map workflow_skill → callable factory
# Thêm workflow mới vào đây khi build thêm skills
_FAST_LANE_REGISTRY: dict[str, str] = {
    "workflow_spider_social_post":       "skills.workflow_spider_social_post.run_spider_social_post_workflow",
    "workflow_finder_task":           "skills.apps.finder_skills.run_finder_task",
    "workflow_terminal_task":         "skills.apps.terminal_skills.run_terminal_task",
    "workflow_gmail_task":            "skills.apps.gmail_skills.run_gmail_task",
    "workflow_messenger_task":        "skills.apps.messenger_skills.run_messenger_task",
}

# Map intent_type → gợi ý action cho Explorer Lane
# Dùng để inject context vào ReAct system prompt
_INTENT_CONTEXT: dict[str, dict] = {
    "file_op": {
        "hint": "This is a file operation task. Use Finder, AppleScript, or terminal commands.",
        "tools_priority": ["finder", "terminal", "applescript"],
    },
    "terminal": {
        "hint": "This is a terminal/command task. Open Terminal and execute commands.",
        "tools_priority": ["terminal"],
    },
    "email": {
        "hint": "This is an email task. Navigate to Gmail or use Mail app.",
        "tools_priority": ["chrome", "gmail"],
    },
    "web_task": {
        "hint": "This is a web browsing task. Use Chrome.",
        "tools_priority": ["chrome"],
    },
    "calendar": {
        "hint": "This is a calendar task. Use Calendar app or Google Calendar.",
        "tools_priority": ["calendar", "chrome"],
    },
    "social_post": {
        "hint": "This is a social media post task. Use Chrome with appropriate profile.",
        "tools_priority": ["chrome"],
    },
    "screenshot": {
        "hint": "Take a screenshot using screencapture command.",
        "tools_priority": ["system"],
    },
    "general": {
        "hint": "Analyze the task and determine the best approach.",
        "tools_priority": ["chrome", "finder"],
    },
}


# ══════════════════════════════════════════════════════════════════
# SmartRouter
# ══════════════════════════════════════════════════════════════════

class SmartRouter:
    """
    Cổng điều phối thông minh cho mọi loại task.

    Args:
        agent_loop:      AgentLoop instance (để gọi ReAct loop)
        llm_client:      LLMClient (truyền cho UniversalParser)
        telegram_send_fn: async callable(msg) để gửi clarification
        ipc_client:      IPCClient (truyền cho workflow calls)
        llm_fallback:    LLM FallbackStack (truyền cho workflow calls)
        enable_learning: Có tự compile skill mới sau Explorer success không
    """

    def __init__(
        self,
        agent_loop:      Any = None,
        llm_client:      Any = None,
        telegram_send_fn: Any = None,
        ipc_client:      Any = None,
        llm_fallback:    Any = None,
        enable_learning: bool = True,
    ) -> None:
        self._agent_loop      = agent_loop
        self._telegram_send   = telegram_send_fn
        self._ipc             = ipc_client
        self._llm_fallback    = llm_fallback
        self._enable_learning = enable_learning
        self._parser          = UniversalParser(llm_client=llm_client)

        # Stats
        self._route_counts: dict[str, int] = {
            "fast": 0, "explorer": 0, "clarify": 0, "error": 0
        }

    async def route(
        self,
        goal: str,
        task_id: str = "",
    ) -> RouteResult:
        """
        Route goal sang đúng execution lane.

        Args:
            goal:    User goal string
            task_id: Task ID cho logging

        Returns:
            RouteResult — handled=True nếu đã xử lý xong
        """
        t0 = time.time()

        # ── v4.3: explicit "run_workflow:<name>" (Brain vocabulary, scheduler
        # commands, admin "run") → run that workflow directly by name/id.
        if goal.strip().lower().startswith("run_workflow:"):
            wf_name = goal.split(":", 1)[1].strip()
            try:
                from core.workflow_nodes_ext import resolve_workflow
                from core.workflow_executor import WorkflowExecutor
                wf = resolve_workflow(wf_name)
                if wf:
                    _vlog("▶️", f"run_workflow: {wf.get('name', wf_name)}")
                    executor = WorkflowExecutor(ipc_client=self._ipc, notify_fn=self._telegram_send)
                    r = await executor.run(wf, goal=goal, variables={})
                else:
                    r = {"success": False, "steps_done": 0,
                         "error": f"Không tìm thấy workflow '{wf_name}' trong data/workflows"}
            except Exception as exc:
                r = {"success": False, "steps_done": 0, "error": str(exc)[:200]}

            class _RWR:
                def __init__(self, res, tid, g):
                    self.task_id = tid; self.goal = g
                    self.success = res.get("success", False)
                    self.steps_taken = res.get("steps_done", 0)
                    self.episode_id = ""; self.error = res.get("error", "")

            self._route_counts["fast"] += 1
            return RouteResult(
                handled=True,
                task_result=_RWR(r, task_id, goal),
                intent=IntentResult(intent_type="run_workflow", lane="fast",
                                    raw_goal=goal, parse_tier="explicit"),
                lane_used="taught",
                latency_ms=int((time.time() - t0) * 1000),
            )

        # ── FIX v1.0: Taught workflows get HIGHEST priority ─────
        # Check if goal matches any workflow taught via Workflow Teacher
        try:
            from core.workflow_executor import find_matching_workflow, WorkflowExecutor
            taught, wf_vars = find_matching_workflow(goal)
            if taught:
                _vlog("🎓", f"Taught workflow match: '{taught.get('name','')}' "
                      f"({len(taught.get('nodes',[]))} nodes) vars={wf_vars}")
                executor = WorkflowExecutor(
                    ipc_client=self._ipc,
                    notify_fn=self._telegram_send,
                )
                wf_result = await executor.run(taught, goal=goal, variables=wf_vars)

                class _WTR:
                    def __init__(self, r, tid, g):
                        self.task_id = tid; self.goal = g
                        self.success = r.get("success", False)
                        self.steps_taken = r.get("steps_done", 0)
                        self.episode_id = ""; self.error = r.get("error", "")

                self._route_counts["fast"] += 1
                return RouteResult(
                    handled=True,
                    task_result=_WTR(wf_result, task_id, goal),
                    intent=IntentResult(intent_type="taught_workflow",
                                       lane="fast", raw_goal=goal,
                                       parse_tier="workflow_teacher"),
                    lane_used="taught",
                    latency_ms=int((time.time()-t0)*1000),
                )
        except ImportError:
            pass
        except Exception as exc:
            _vlog("⚠️", f"Workflow Teacher error: {str(exc)[:60]} → normal routing")

        # ── FIX v1.0: OpenClaw installed skills ─────────────────
        try:
            from core.openclaw_bridge import get_installed_skills
            oc_skills = get_installed_skills()
            if oc_skills:
                goal_lower = goal.lower().strip()
                for sk in oc_skills:
                    if not sk.get("enabled", True):
                        continue
                    # Match by trigger patterns
                    for trigger in sk.get("trigger_patterns", []):
                        if not trigger:
                            continue
                        tl = trigger.lower().strip()
                        # v4.3: short goals ("mở") used to match every trigger
                        # that contained them.
                        if (len(tl) >= 4 and tl in goal_lower) or (
                                len(goal_lower) >= 8 and goal_lower in tl):
                            _vlog("🦞", f"OpenClaw skill match: {sk.get('name','')} via '{trigger}'")
                            # Execute via wrapper
                            wrapper = sk.get("wrapper_path", "")
                            if wrapper and Path(wrapper).exists():
                                import importlib.util
                                spec = importlib.util.spec_from_file_location("oc_skill", wrapper)
                                mod = importlib.util.module_from_spec(spec)
                                spec.loader.exec_module(mod)
                                if hasattr(mod, "run"):
                                    oc_result = await mod.run(goal, {"task_id": task_id})
                                    oc_success = oc_result.get("success", False)

                                    class _OCR:
                                        def __init__(self, r, tid, g):
                                            self.task_id = tid; self.goal = g
                                            self.success = r.get("success", False)
                                            self.steps_taken = 1
                                            self.episode_id = ""; self.error = r.get("error", "")

                                    self._route_counts["fast"] += 1
                                    return RouteResult(
                                        handled=True,
                                        task_result=_OCR(oc_result, task_id, goal),
                                        intent=IntentResult(intent_type="openclaw_skill",
                                                           lane="fast", raw_goal=goal,
                                                           parse_tier="openclaw"),
                                        lane_used="openclaw",
                                        latency_ms=int((time.time()-t0)*1000),
                                    )
                            break
        except ImportError:
            pass
        except Exception as exc:
            _vlog("⚠️", f"OpenClaw match error: {str(exc)[:60]}")

        # ── FIX v1.0: RAG Memory — retrieve context before routing ──
        _rag_context = ""
        try:
            from memory.rag_engine import get_rag_engine_sync
            _rag = get_rag_engine_sync()
            if _rag and _rag._ready:
                similar = await _rag.recall_similar_tasks(goal, k=3, success_only=True)
                if similar:
                    best = similar[0]
                    _vlog("🧠", f"RAG recall: '{best['goal'][:40]}' → {best['skill'] or best['lane']} "
                          f"(score={best['score']}, {'✅' if best['success'] else '❌'})")
                    _rag_context = await _rag.build_context(goal)
        except Exception:
            pass

        # ── Parse intent ──────────────────────────────────────────
        intent = await self._parser.parse(goal)
        _vlog("🧭", f"Route: intent={intent.intent_type} "
              f"lane={intent.lane} "
              f"app={intent.primary_app} "
              f"complexity={intent.complexity} "
              f"[{intent.parse_tier} {intent.parse_ms}ms]")

        # ── Clarify lane ──────────────────────────────────────────
        if intent.lane == "clarify":
            await self._ask_clarification(goal, intent)
            self._route_counts["clarify"] += 1
            return RouteResult(
                handled=True,
                intent=intent,
                lane_used="clarify",
                latency_ms=int((time.time()-t0)*1000),
            )

        # ── Fast lane ─────────────────────────────────────────────
        # FIX v1.0: Thử Fast Lane nếu:
        #   (a) intent.lane == "fast" VÀ có workflow_skill, HOẶC
        #   (b) intent_type match built-in skill (web_task, file_op, terminal, email, messenger)
        #       → luôn thử Fast Lane trước, bất kể lane value
        _KNOWN_FAST_TYPES = {"web_task", "file_op", "terminal", "email", "messenger", "social_post"}
        _try_fast = (
            (intent.lane == "fast" and intent.workflow_skill)
            or intent.intent_type in _KNOWN_FAST_TYPES
        )

        if _try_fast:
            result = await self._fast_lane(goal, intent, task_id)
            if result.handled:
                self._route_counts["fast"] += 1
                result.latency_ms = int((time.time()-t0)*1000)
                return result
            # Fast lane fail → fallthrough to explorer

        # ── Explorer lane ─────────────────────────────────────────
        # Inject intent context vào ReAct để LLM biết đang làm task gì
        enriched_goal = self._enrich_goal(goal, intent)
        self._route_counts["explorer"] += 1

        _vlog("🔍", f"Explorer Lane: '{goal[:60]}' "
              f"(apps={intent.apps_needed})")

        # Trả về handled=False → agent_loop tự chạy ReAct với enriched_goal
        return RouteResult(
            handled=False,
            intent=intent,
            lane_used="explorer",
            # task_result = None → agent_loop sẽ fill sau
        )

    async def _fast_lane(
        self,
        goal: str,
        intent: IntentResult,
        task_id: str,
    ) -> RouteResult:
        """
        Thực thi Fast Lane — gọi workflow class trực tiếp.
        """
        ws = intent.workflow_skill
        _vlog("⚡", f"Fast Lane → workflow='{ws}'")

        # ── workflow_spider_social_post ───────────────────────────────
        if ws == "workflow_spider_social_post":
            try:
                from skills.workflow_spider_social_post import run_spider_social_post_workflow
                wf_result = await run_spider_social_post_workflow(
                    ipc_client=self._ipc,
                    llm_fallback=self._llm_fallback,
                    telegram_send_fn=self._telegram_send,
                    goal=goal,
                )
                from dataclasses import dataclass as _dc
                # Wrap thành TaskResult-compatible
                class _TR:
                    def __init__(self, wf):
                        self.task_id    = task_id
                        self.goal       = goal
                        self.success    = wf.success
                        self.steps_taken= wf.steps_done
                        self.error      = wf.error if not wf.success else ""
                        self.episode_id = ""
                _vlog("⚡" if wf_result.success else "❌",
                      f"Fast Lane done: {wf_result.steps_done} bước "
                      f"{'✅' if wf_result.success else '❌'} "
                      f"{wf_result.duration_s:.0f}s")
                return RouteResult(
                    handled=True,
                    task_result=_TR(wf_result),
                    intent=intent,
                    lane_used="fast",
                )
            except ImportError as e:
                _vlog("⚠️ ", f"Fast Lane import error: {e} → Explorer fallback")
                return RouteResult(handled=False, intent=intent, lane_used="fast_failed")
            except Exception as exc:
                _vlog("⚠️ ", f"Fast Lane error: {exc} → Explorer fallback")
                return RouteResult(handled=False, intent=intent, lane_used="fast_failed")

        # ── Finder task (file_op intent) ──────────────────────────
        if intent.intent_type == "file_op":
            try:
                from skills.apps.finder_skills import run_finder_task
                fr = await run_finder_task(
                    goal=goal, ipc_client=self._ipc, notify_fn=self._telegram_send,
                )
                class _FTR:
                    def __init__(self, r, tid, g):
                        self.task_id=tid; self.goal=g
                        self.success=r.success; self.steps_taken=1
                        self.episode_id=""; self.error=r.error
                _vlog("⚡" if fr.success else "❌", f"Finder: {fr.summary}")
                return RouteResult(handled=True, task_result=_FTR(fr, task_id, goal),
                                   intent=intent, lane_used="fast")
            except Exception as exc:
                _vlog("⚠️ ", f"Finder error: {exc} → Explorer")

        # ── Terminal task ─────────────────────────────────────────
        if intent.intent_type == "terminal":
            try:
                from skills.apps.terminal_skills import run_terminal_task
                tr = await run_terminal_task(
                    goal=goal, ipc_client=self._ipc, notify_fn=self._telegram_send,
                )
                class _TTR:
                    def __init__(self, r, tid, g):
                        self.task_id=tid; self.goal=g
                        self.success=r.success and not r.blocked; self.steps_taken=1
                        self.episode_id=""; self.error=r.stderr[:100] if not r.success else ""
                _vlog("⚡" if tr.success else "❌", f"Terminal: {tr.summary}")
                return RouteResult(handled=True, task_result=_TTR(tr, task_id, goal),
                                   intent=intent, lane_used="fast")
            except Exception as exc:
                _vlog("⚠️ ", f"Terminal error: {exc} → Explorer")

        # ── Web / Chrome task ────────────────────────────────────
        if intent.intent_type == "web_task":
            try:
                from skills.apps.chrome_skills import run_chrome_task
                cr = await run_chrome_task(
                    goal=goal, ipc_client=self._ipc, notify_fn=self._telegram_send,
                )
                class _CTR:
                    def __init__(self, r, tid, g):
                        self.task_id=tid; self.goal=g
                        self.success=r.success; self.steps_taken=1
                        self.episode_id=""; self.error=r.error
                _vlog("⚡" if cr.success else "❌", f"Chrome: {cr.summary}")
                return RouteResult(handled=True, task_result=_CTR(cr, task_id, goal),
                                   intent=intent, lane_used="fast")
            except Exception as exc:
                _vlog("⚠️ ", f"Chrome task error: {exc} → Explorer")

        # ── Email task (Gmail) ────────────────────────────────────
        if intent.intent_type == "email":
            try:
                from skills.apps.gmail_skills import run_gmail_task
                gr = await run_gmail_task(
                    goal=goal, ipc_client=self._ipc, notify_fn=self._telegram_send,
                )
                class _GTR:
                    def __init__(self, r, tid, g):
                        self.task_id=tid; self.goal=g
                        self.success=r.success; self.steps_taken=1
                        self.episode_id=""; self.error=r.error
                _vlog("⚡" if gr.success else "❌", f"Gmail: {gr.summary}")
                return RouteResult(handled=True, task_result=_GTR(gr, task_id, goal),
                                   intent=intent, lane_used="fast")
            except Exception as exc:
                _vlog("⚠️ ", f"Gmail task error: {exc} → Explorer")

        # ── Messenger task ────────────────────────────────────────
        if intent.intent_type == "messenger":
            try:
                from skills.apps.messenger_skills import run_messenger_task
                # Inject telegram_app nếu có
                _tg_app = getattr(self, "_telegram_app", None)
                mr = await run_messenger_task(
                    goal=goal, ipc_client=self._ipc,
                    notify_fn=self._telegram_send,
                    telegram_app=_tg_app,
                )
                class _MTR:
                    def __init__(self, r, tid, g):
                        self.task_id=tid; self.goal=g
                        self.success=r.success; self.steps_taken=1
                        self.episode_id=""; self.error=r.error
                _vlog("⚡" if mr.success else "❌", f"Messenger: {mr.summary}")
                return RouteResult(handled=True, task_result=_MTR(mr, task_id, goal),
                                   intent=intent, lane_used="fast")
            except Exception as exc:
                _vlog("⚠️ ", f"Messenger task error: {exc} → Explorer")

        # ── Unknown workflow skill ────────────────────────────────
        _vlog("⚠️ ", f"Unknown workflow skill '{ws}' → Explorer fallback")
        return RouteResult(handled=False, intent=intent, lane_used="fast_unknown")

    def _enrich_goal(self, goal: str, intent: IntentResult) -> str:
        """
        Thêm context từ IntentResult vào goal để ReAct LLM biết hướng.
        Không thay đổi goal gốc — chỉ thêm metadata comment.
        """
        ctx = _INTENT_CONTEXT.get(intent.intent_type, _INTENT_CONTEXT["general"])
        hint = ctx.get("hint", "")
        apps = ", ".join(intent.apps_needed) if intent.apps_needed else intent.primary_app

        # Chỉ enrich nếu có ích — tránh làm nhiễu goal đơn giản
        if intent.complexity in ("trivial", "simple"):
            return goal

        enriched = (
            f"{goal}\n"
            f"[Context: task_type={intent.intent_type}, "
            f"apps={apps}, "
            f"complexity={intent.complexity}. "
            f"{hint}]"
        )
        return enriched

    async def _ask_clarification(self, goal: str, intent: IntentResult) -> None:
        """Gửi Telegram hỏi user khi task mơ hồ."""
        if not self._telegram_send:
            return
        try:
            await self._telegram_send(
                f"❓ *Cần thêm thông tin*\n\n"
                f"Yêu cầu: _{goal[:200]}_\n\n"
                f"Bạn có thể mô tả chi tiết hơn không? Ví dụ:\n"
                f"• File nào / thư mục nào?\n"
                f"• Gửi đến ai?\n"
                f"• Kết quả mong muốn là gì?"
            )
        except Exception:
            pass

    def stats(self) -> dict:
        """Thống kê routing để debug."""
        total = sum(self._route_counts.values())
        return {
            "total": total,
            "fast":    self._route_counts.get("fast", 0),
            "explorer":self._route_counts.get("explorer", 0),
            "clarify": self._route_counts.get("clarify", 0),
            "error":   self._route_counts.get("error", 0),
            "fast_rate": f"{self._route_counts.get('fast',0)/max(total,1):.0%}",
        }

    def register_workflow(self, intent_type: str, workflow_skill: str) -> None:
        """
        Đăng ký workflow mới cho Fast Lane.
        Gọi khi SkillForge compile được skill mới.

        Example:
            router.register_workflow("file_op", "workflow_file_organizer")
        """
        # Thêm rule vào parser
        from core.universal_parser import _RULES, _RulePattern
        # Chưa implement full — placeholder cho Phase 2
        _vlog("📚", f"Registered workflow: {intent_type} → {workflow_skill}")
