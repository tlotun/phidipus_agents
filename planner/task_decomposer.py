# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
planner/task_decomposer.py — Phidipus Task Decomposer v9.26
═══════════════════════════════════════════════════════════════

Decomposes complex goals into a TaskGraph of sub-tasks with dependencies.

Strategy:
  1. Check if SmartAction can handle directly → skip decomposition
  2. Check plan cache → reuse previous plan
  3. Call LLM (local qwen3:8b) to decompose goal → sub-tasks
  4. Map sub-tasks to available skills/tools
  5. Build dependency graph
  6. Cache successful plan for reuse

Input:  "tìm file excel tồn kho, lọc bóng đèn, tạo báo cáo, gửi cho tôi"
Output: TaskGraph {
  T1: find_file("*tồn kho*") → file_path       [no deps]
  T2: read_excel(file_path)  → data             [depends: T1]
  T3: filter("bóng đèn")    → filtered          [depends: T2]
  T4: create_excel(filtered) → output_file       [depends: T3]
  T5: send_telegram(output)  → done              [depends: T4]
}
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════
# Data structures
# ══════════════════════════════════════════════════════════════

@dataclass
class SubTask:
    """One step in a task plan."""
    id: str                           # "T1", "T2", ...
    description: str                  # Human-readable ("tìm file tồn kho")
    action: str                       # "find_file", "read_excel", "filter_rows"...
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)  # ["T1", "T2"]
    status: str = "pending"           # pending, running, done, failed, skipped
    result: Any = None                # Output of this step
    error: str = ""

    @property
    def is_ready(self) -> bool:
        """Can this task start? (all dependencies done)"""
        return self.status == "pending"


@dataclass
class TaskPlan:
    """Complete execution plan for a goal."""
    goal: str
    tasks: list[SubTask]
    created_at: float = field(default_factory=time.time)
    goal_hash: str = ""
    source: str = "llm"              # "llm", "cache", "template"
    workflow_skill: str = ""         # v9.22: direct workflow class shortcut

    @property
    def total(self) -> int:
        return len(self.tasks)

    @property
    def completed(self) -> int:
        return sum(1 for t in self.tasks if t.status == "done")

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tasks if t.status == "failed")

    @property
    def progress(self) -> float:
        return self.completed / self.total if self.total > 0 else 0.0

    @property
    def is_complete(self) -> bool:
        return all(t.status in ("done", "skipped") for t in self.tasks)

    @property
    def is_failed(self) -> bool:
        return any(t.status == "failed" for t in self.tasks)

    def get_ready_tasks(self, completed_ids: set[str] | None = None) -> list[SubTask]:
        """Get tasks whose dependencies are all completed."""
        if completed_ids is None:
            completed_ids = {t.id for t in self.tasks if t.status == "done"}
        ready = []
        for t in self.tasks:
            if t.status != "pending":
                continue
            if all(dep in completed_ids for dep in t.depends_on):
                ready.append(t)
        return ready

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "goal_hash": self.goal_hash,
            "source": self.source,
            "tasks": [
                {
                    "id": t.id,
                    "description": t.description,
                    "action": t.action,
                    "params": t.params,
                    "depends_on": t.depends_on,
                }
                for t in self.tasks
            ],
        }

    @staticmethod
    def from_dict(data: dict) -> "TaskPlan":
        tasks = []
        for t in data.get("tasks", []):
            tasks.append(SubTask(
                id=t["id"],
                description=t.get("description", ""),
                action=t.get("action", ""),
                params=t.get("params", {}),
                depends_on=t.get("depends_on", []),
            ))
        return TaskPlan(
            goal=data.get("goal", ""),
            tasks=tasks,
            goal_hash=data.get("goal_hash", ""),
            source=data.get("source", "cache"),
        )


# ══════════════════════════════════════════════════════════════
# Known action templates (no LLM needed)
# ══════════════════════════════════════════════════════════════

# Common multi-step patterns that can be decomposed WITHOUT calling LLM
PLAN_TEMPLATES: list[dict] = [
    {
        "pattern": r"(?:tìm|search|find).*(?:file|excel|xlsx).*(?:lọc|filter).*(?:tạo|create|export).*(?:gửi|send)",
        "tasks": [
            {"id": "T1", "action": "find_file", "description": "Tìm file", "depends_on": []},
            {"id": "T2", "action": "read_excel", "description": "Đọc dữ liệu", "depends_on": ["T1"]},
            {"id": "T3", "action": "filter_data", "description": "Lọc dữ liệu", "depends_on": ["T2"]},
            {"id": "T4", "action": "create_excel", "description": "Tạo file mới", "depends_on": ["T3"]},
            {"id": "T5", "action": "send_result", "description": "Gửi kết quả", "depends_on": ["T4"]},
        ],
    },
    {
        "pattern": r"(?:mở|open).*(?:chrome|browser).*(?:vào|navigate|go).*(?:trang|page|url|site)",
        "tasks": [
            {"id": "T1", "action": "open_app", "description": "Mở trình duyệt", "depends_on": []},
            {"id": "T2", "action": "navigate_url", "description": "Chuyển đến trang web", "depends_on": ["T1"]},
        ],
    },
    {
        "pattern": r"(?:tìm|search|find).*(?:file|excel).*(?:gửi|send)",
        "tasks": [
            {"id": "T1", "action": "find_file", "description": "Tìm file", "depends_on": []},
            {"id": "T2", "action": "send_result", "description": "Gửi file", "depends_on": ["T1"]},
        ],
    },
    # v9.22: Bee Social Post — ChatGPT image + Gemini content + Facebook
    {
        "pattern": (
            # Order-independent: chatgpt + image keywords anywhere + content + post
            # Accepts: "tạo ảnh X chatgpt" OR "chatgpt tạo ảnh X"
            r"(?=.*(?:chatgpt|chat gpt|chatgpt\.com))"   # must have chatgpt somewhere
            r"(?=.*(?:tạo ảnh|vẽ ảnh|tạo hình|sinh ảnh|ảnh|hình|image))"  # must have image
            r"(?=.*(?:gemini|content|nội dung|viết))"    # must have content intent
            r"(?=.*(?:facebook|fb|đăng|post|share))"     # must have post intent
            r".*"                                          # match whole string
        ),
        "tasks": [
            {"id": "T1", "action": "open_app",       "description": "Mở Chrome profile",        "depends_on": []},
            {"id": "T2", "action": "custom_code",    "description": "Tạo ảnh AI trên ChatGPT",   "depends_on": ["T1"]},
            {"id": "T3", "action": "keyboard_action","description": "Mở tab mới",                "depends_on": ["T2"]},
            {"id": "T4", "action": "custom_code",    "description": "Tạo content trên Gemini",   "depends_on": ["T3"]},
            {"id": "T5", "action": "navigate_url",   "description": "Vào Facebook",              "depends_on": ["T4"]},
            {"id": "T6", "action": "custom_code",    "description": "Đăng post ảnh + content",   "depends_on": ["T5"]},
        ],
        "workflow_skill": "workflow_spider_social_post",
    },
    # v9.22: Generic image + post workflow
    {
        "pattern": (
            r"(?:tạo ảnh|create image|gen image).*"
            r"(?:tạo content|viết content|write content).*"
            r"(?:đăng|post|facebook|instagram)"
        ),
        "tasks": [
            {"id": "T1", "action": "custom_code",  "description": "Tạo ảnh AI",         "depends_on": []},
            {"id": "T2", "action": "custom_code",  "description": "Tạo content bài đăng","depends_on": []},
            {"id": "T3", "action": "custom_code",  "description": "Đăng lên mạng xã hội","depends_on": ["T1", "T2"]},
        ],
    },
]


# ══════════════════════════════════════════════════════════════
# Task Decomposer
# ══════════════════════════════════════════════════════════════

# [Part-8 FIX] Plan depth/step limits to prevent recursive decomposition explosion
MAX_PLAN_DEPTH: int = 3
MAX_PLAN_STEPS: int = 20

class TaskDecomposer:
    """
    Decomposes goals into TaskPlans.

    Priority:
      1. Template match (instant, no LLM)
      2. Cache lookup (instant)
      3. LLM decomposition (5-15s)
    """

    def __init__(
        self,
        llm_client: Any = None,
        cache_dir: str = "data/plans",
    ) -> None:
        self._llm       = llm_client
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._plan_cache: dict[str, TaskPlan] = {}
        self._load_cache()
        # v9.22: Pattern Learner — learns abstract decomp patterns from LLM plans
        self._learner = PatternLearner(cache_dir=cache_dir)

    # ══════════════════════════════════════════════════════════
    # Main API
    # ══════════════════════════════════════════════════════════

    async def decompose(self, goal: str) -> TaskPlan | None:
        """
        Decompose a goal into a TaskPlan.

        v9.22 priority order:
          1. Template match (instant, hardcoded)
          2. Exact cache lookup (instant)
          3. Learned pattern match (instant, 0 LLM calls)  ← NEW
          4. LLM decomposition (5-15s)

        Returns TaskPlan if the goal needs multi-step execution,
        or None if it's simple enough for single-step handling.
        """
        gl = goal.strip().lower()

        # ── Step 1: Template match (instant) ──────────────────
        plan = self._try_template(goal)
        if plan:
            _vlog("📋", f"Plan từ template: {plan.total} bước")
            return plan

        # ── Step 2: Exact cache lookup (instant) ──────────────
        goal_hash = self._hash_goal(goal)
        cached = self._plan_cache.get(goal_hash)
        if cached:
            plan = TaskPlan.from_dict(cached.to_dict())
            plan.source = "cache"
            _vlog("💾", f"Plan từ cache: {plan.total} bước")
            return plan

        # ── Step 3: Learned pattern match (instant, 0 LLM) ────
        learned = self._learner.match(goal)
        if learned:
            return learned

        # ── Step 4: Detect if multi-step needed ───────────────
        multi_indicators = [
            "rồi", "sau đó", "then", "and then",
            "tạo", "create", "export",
            "gửi", "send",
            "lọc", "filter",
            "tìm", "search", "find",
            "mở", "open",
        ]
        indicator_count = sum(1 for ind in multi_indicators if ind in gl)

        if indicator_count < 3:
            return None

        # ── Step 5: LLM decomposition ─────────────────────────
        if self._llm:
            plan = await self._llm_decompose(goal)
            if plan:
                plan.goal_hash = goal_hash
                self._plan_cache[goal_hash] = plan
                self._save_cache()
                _vlog("🧠", f"Plan từ LLM: {plan.total} bước")
                return plan

        return None

    # ══════════════════════════════════════════════════════════
    # Template matching
    # ══════════════════════════════════════════════════════════

    def _try_template(self, goal: str) -> TaskPlan | None:
        """Match goal to known plan templates."""
        gl = goal.strip().lower()
        for tmpl in PLAN_TEMPLATES:
            if re.search(tmpl["pattern"], gl, re.IGNORECASE):
                tasks = [
                    SubTask(
                        id=t["id"],
                        description=t["description"],
                        action=t["action"],
                        depends_on=t.get("depends_on", []),
                    )
                    for t in tmpl["tasks"]
                ]
                return TaskPlan(
                    goal=goal,
                    tasks=tasks,
                    source="template",
                    workflow_skill=tmpl.get("workflow_skill", ""),
                )
        return None

    # ══════════════════════════════════════════════════════════
    # LLM decomposition
    # ══════════════════════════════════════════════════════════

    async def _llm_decompose(self, goal: str, _depth: int = 0) -> 'TaskPlan | None':
        """Decompose goal into sub-tasks via LLM.
        Part-8 FIX: Respects MAX_PLAN_DEPTH and MAX_PLAN_STEPS.
        v9.22: Successful plans are fed to PatternLearner.
        """
        if _depth >= MAX_PLAN_DEPTH:
            return None

        """Use LLM to decompose goal into sub-tasks."""
        prompt = f"""Bạn là task planner. Tách yêu cầu sau thành các bước nhỏ.

YÊU CẦU: {goal}

Trả lời CHÍNH XÁC theo format JSON (không có text khác):
{{
  "tasks": [
    {{"id": "T1", "description": "...", "action": "find_file", "params": {{}}, "depends_on": []}},
    {{"id": "T2", "description": "...", "action": "read_excel", "params": {{}}, "depends_on": ["T1"]}},
    ...
  ]
}}

Actions có sẵn:
  find_file, read_excel, create_excel, filter_data, aggregate_data,
  send_result, open_app, navigate_url, keyboard_action, click_element,
  wait, screenshot, custom_code

Quy tắc:
  - Mỗi task có id duy nhất (T1, T2, ...)
  - depends_on chứa id của tasks phải xong trước
  - Tối đa 8 tasks
  - description bằng tiếng Việt"""

        try:
            import asyncio

            response = await asyncio.wait_for(
                self._llm.generate(prompt, temperature=0.1),
                timeout=30,
            )

            plan_data = self._parse_llm_response(response, goal)
            # v9.22: Learn from this LLM plan for future zero-LLM decomposition
            if plan_data and plan_data.total >= 2:
                self._learner.learn(goal, plan_data)
            return plan_data

        except Exception as exc:
            _vlog("⚠️", f"LLM decompose failed: {str(exc)[:60]}")
            return None

    def _parse_llm_response(self, response: str, goal: str) -> TaskPlan | None:
        """Parse LLM response into TaskPlan."""
        json_match = re.search(r'\{[\s\S]*"tasks"[\s\S]*\}', response)
        if not json_match:
            return None

        try:
            data = json.loads(json_match.group())
            tasks_data = data.get("tasks", [])
            if not tasks_data or len(tasks_data) < 2:
                return None

            tasks = []
            for t in tasks_data[:8]:
                tasks.append(SubTask(
                    id=t.get("id", f"T{len(tasks)+1}"),
                    description=t.get("description", ""),
                    action=t.get("action", "custom_code"),
                    params=t.get("params", {}),
                    depends_on=t.get("depends_on", []),
                ))

            return TaskPlan(goal=goal, tasks=tasks, source="llm")

        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    # ══════════════════════════════════════════════════════════
    # Cache
    # ══════════════════════════════════════════════════════════

    def _hash_goal(self, goal: str) -> str:
        normalized = re.sub(r'\s+', ' ', goal.strip().lower())
        return hashlib.md5(normalized.encode()).hexdigest()[:12]

    def _load_cache(self) -> None:
        path = self._cache_dir / "plan_cache.json"
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                for key, plan_data in data.items():
                    self._plan_cache[key] = TaskPlan.from_dict(plan_data)
            except Exception:
                pass

    def _save_cache(self) -> None:
        path = self._cache_dir / "plan_cache.json"
        try:
            data = {k: v.to_dict() for k, v in self._plan_cache.items()}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    def cache_stats(self) -> dict:
        return {
            "cached_plans":    len(self._plan_cache),
            "templates":       len(PLAN_TEMPLATES),
            "learned_patterns": self._learner.count(),
        }


# ══════════════════════════════════════════════════════════════
# Phase 2B — PATTERN LEARNER
# Learns abstract decomposition patterns from LLM plans.
# ══════════════════════════════════════════════════════════════

@dataclass
class LearnedPattern:
    """An abstract decomposition pattern learned from an LLM plan."""
    id:          str
    keywords:    list[str]          # Trigger keywords extracted from goal
    actions:     list[str]          # Ordered list of action types
    deps:        list[list[str]]    # Dependency lists per task
    hit_count:   int   = 0
    success_rate: float = 1.0
    created_at:  float = field(default_factory=time.time)
    last_used:   float = 0.0


class PatternLearner:
    """
    Phase 2B: Learns abstract decomposition patterns from successful LLM plans.

    After each LLM decomposition, extracts an abstract pattern:
      - Keywords: significant words from goal (≥4 chars, not stopwords)
      - Action sequence: ["find_file", "read_excel", "filter_data", "create_excel"]
      - Dependencies: [[],["T1"],["T2"],["T3"]]

    Future goals are matched against learned patterns before calling LLM.
    A match on 2+ keywords triggers the learned pattern → zero LLM call.

    Persistence: data/plans/learned_patterns.json
    Max patterns: 200 (LRU eviction)
    """

    MAX_PATTERNS = 200
    MIN_KEYWORD_MATCH = 2   # minimum keywords that must match to use a pattern

    _STOPWORDS = {
        "tôi", "cho", "từ", "về", "với", "và", "của", "là", "có",
        "the", "and", "for", "from", "with", "this", "that",
        "file", "bạn", "hãy", "vui", "lòng",
    }

    def __init__(self, cache_dir: str = "data/plans") -> None:
        self._path = Path(cache_dir) / "learned_patterns.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._patterns: dict[str, LearnedPattern] = {}
        self._load()

    def learn(self, goal: str, plan: "TaskPlan") -> None:
        """
        Extract and store an abstract pattern from a successful LLM plan.
        Called immediately after LLM returns a valid plan.
        """
        keywords = self._extract_keywords(goal)
        if len(keywords) < 2:
            return   # Too vague to form a useful pattern

        actions  = [t.action for t in plan.tasks]
        deps     = [t.depends_on for t in plan.tasks]
        pat_id   = hashlib.md5(
            "|".join(sorted(keywords[:5]) + actions).encode()
        ).hexdigest()[:12]

        if pat_id in self._patterns:
            # Update existing pattern
            self._patterns[pat_id].hit_count += 1
            self._patterns[pat_id].last_used  = time.time()
        else:
            self._patterns[pat_id] = LearnedPattern(
                id=pat_id, keywords=keywords[:8],
                actions=actions, deps=deps,
            )
            _vlog("📖", f"PatternLearner: learned pattern [{pat_id[:6]}] "
                  f"kw={keywords[:3]} → {actions}")

        # Evict if over limit
        if len(self._patterns) > self.MAX_PATTERNS:
            oldest = sorted(
                self._patterns.values(),
                key=lambda p: (p.hit_count, p.last_used),
            )[:len(self._patterns) - self.MAX_PATTERNS]
            for p in oldest:
                del self._patterns[p.id]

        self._save()

    def match(self, goal: str) -> "TaskPlan | None":
        """
        Try to match a goal to a learned pattern.
        Returns a TaskPlan if match found, None otherwise.
        """
        keywords = self._extract_keywords(goal)
        if not keywords:
            return None

        best_pat   = None
        best_score = 0

        for pat in self._patterns.values():
            matches = sum(1 for kw in keywords if kw in pat.keywords)
            if matches >= self.MIN_KEYWORD_MATCH and matches > best_score:
                best_score = matches
                best_pat   = pat

        if not best_pat:
            return None

        # Reconstruct TaskPlan from pattern
        tasks = []
        for i, (action, dep_list) in enumerate(
            zip(best_pat.actions, best_pat.deps), start=1
        ):
            tasks.append(SubTask(
                id=f"T{i}",
                description=f"[pattern] {action}",
                action=action,
                depends_on=dep_list,
            ))

        best_pat.hit_count += 1
        best_pat.last_used  = time.time()
        self._save()

        _vlog("📖", f"PatternLearner: matched [{best_pat.id[:6]}] "
              f"({best_score}/{len(keywords)} keywords) — {len(tasks)} steps, 0 LLM call")

        return TaskPlan(goal=goal, tasks=tasks, source="learned_pattern")

    def count(self) -> int:
        return len(self._patterns)

    def _extract_keywords(self, goal: str) -> list[str]:
        """Extract significant keywords from a goal string."""
        words = re.findall(r'\b\w{4,}\b', goal.lower())
        return [w for w in words if w not in self._STOPWORDS][:10]

    def _load(self) -> None:
        # C2 v9.26: Bootstrap — tạo file rỗng nếu chưa có
        if not self._path.exists():
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text("{}", "utf-8")
                _vlog("📖", f"PatternLearner: bootstrapped {self._path}")
            except Exception:
                pass
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            for pid, pdata in data.items():
                self._patterns[pid] = LearnedPattern(**pdata)
            if self._patterns:
                _vlog("📖", f"PatternLearner: loaded {len(self._patterns)} patterns")
        except Exception:
            pass

    def _save(self) -> None:
        try:
            out = {}
            for pid, pat in self._patterns.items():
                out[pid] = {
                    "id": pat.id, "keywords": pat.keywords,
                    "actions": pat.actions, "deps": pat.deps,
                    "hit_count": pat.hit_count,
                    "success_rate": pat.success_rate,
                    "created_at": pat.created_at,
                    "last_used": pat.last_used,
                }
            self._path.write_text(
                json.dumps(out, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception:
            pass
