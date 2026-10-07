# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/workflow_library.py — Phidipus Workflow Library v2 (v9.20)
══════════════════════════════════════════════════════════════════

Plan Cache + Reusable Workflows với persistent scoring và auto-prune.

Nâng cấp so với v1:
  ❌ v1: TTL đơn giản (30 ngày), evict theo số lượng, reliability = success/total
  ✅ v2: ELO-style scoring, adaptive TTL, prune theo score thay số lượng,
          plan scoring từ execution history, decay theo thời gian không dùng

Scoring formula (v2):
  template_score = 0.5 × reliability
                 + 0.3 × recency_score      (decay half-life 14 ngày)
                 + 0.2 × usage_velocity     (lần dùng trong 7 ngày gần)

Plan cache TTL (v2 adaptive):
  hit_count ≥ 5 và success_rate ≥ 0.8  → TTL 90 ngày (promoted)
  hit_count 1-4                          → TTL 30 ngày (default)
  success_rate < 0.3                     → TTL 7 ngày  (demoted)

Auto-prune (v2):
  Chạy tự động mỗi 50 lần save.
  Prune templates:
    - score < PRUNE_SCORE_THRESHOLD (0.10) SAU ≥ 5 lần dùng
    - không dùng > PRUNE_IDLE_DAYS (60 ngày)
    - fail_count > success_count × 3 (heavily failing)
  Prune cache:
    - expired (adaptive TTL)
    - success_count = 0 sau hit_count ≥ 3 (consistently useless)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ── [C-09 FIX] HMAC signing for cached plans ─────────────────────
def _get_hmac_key() -> bytes:
    """Load or generate persistent HMAC key for plan integrity."""
    key_path = Path("data/memory/.workflow_hmac.key")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        try:
            raw = key_path.read_bytes()
            if len(raw) >= 32:
                return raw
        except Exception:
            pass
    # Generate new key
    key = os.urandom(32)
    try:
        key_path.write_bytes(key)
        key_path.chmod(0o600)
    except Exception:
        pass
    return key


def _sign_plan(plan_dict: dict) -> str:
    """Generate HMAC-SHA256 signature for a plan dict."""
    key = _get_hmac_key()
    payload = json.dumps(plan_dict, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _verify_plan(plan_dict: dict, signature: str) -> bool:
    """Verify HMAC-SHA256 signature of a plan dict."""
    expected = _sign_plan(plan_dict)
    return hmac.compare_digest(expected, signature)


# ══════════════════════════════════════════════════════════════════
# Data classes (v2: new score / velocity fields)
# ══════════════════════════════════════════════════════════════════

@dataclass
class WorkflowStep:
    """Single step in a workflow template."""
    index: int
    action: str
    description: str
    params_template: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowTemplate:
    """Reusable workflow with persistent ELO-style scoring."""
    id: str
    name: str
    description: str
    steps: list[WorkflowStep]
    capability_tags: list[str] = field(default_factory=list)
    trigger_patterns: list[str] = field(default_factory=list)
    success_count: int = 0
    fail_count: int = 0
    avg_duration_ms: float = 0.0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    source_task_ids: list[str] = field(default_factory=list)
    # v2 fields
    score: float = 0.5                  # Composite score (updated on each use)
    recent_uses: list[float] = field(default_factory=list)  # Timestamps last 30 days
    prune_protected: bool = False        # Manual override: never prune

    @property
    def reliability(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 1.0

    @property
    def total_uses(self) -> int:
        return self.success_count + self.fail_count

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkflowTemplate":
        steps_raw = d.pop("steps", [])
        steps = [WorkflowStep(**s) for s in steps_raw]
        known = set(cls.__dataclass_fields__)
        obj = cls(**{k: v for k, v in d.items() if k in known})
        obj.steps = steps
        return obj


@dataclass
class PlanCacheEntry:
    """Cached task plan with adaptive TTL."""
    goal_hash: str
    goal_sample: str
    plan_dict: dict[str, Any]
    source: str = "decomposer"
    hit_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    promoted: bool = False              # v2: long-TTL flag

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.0

    @property
    def adaptive_ttl_days(self) -> int:
        """v2: TTL adapts based on success rate and hit count."""
        if self.promoted or (self.hit_count >= 5 and self.success_rate >= 0.8):
            return 90   # High-quality cached plan: keep 3 months
        if self.success_rate < 0.3 and self.fail_count >= 2:
            return 7    # Unreliable plan: expire fast
        return 30       # Default

    @property
    def is_expired(self) -> bool:
        ttl_s = self.adaptive_ttl_days * 86400
        return (time.time() - self.created_at) > ttl_s

    @property
    def is_consistently_useless(self) -> bool:
        """v2: mark for prune if consistently fails after multiple hits."""
        return self.hit_count >= 3 and self.success_count == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanCacheEntry":
        """M-05 FIX: Schema validation with type coercion before construction."""
        if not isinstance(d, dict):
            raise ValueError(f"PlanCacheEntry.from_dict: expected dict, got {type(d)}")
        known = set(cls.__dataclass_fields__)
        filtered: dict[str, Any] = {}
        for k, v in d.items():
            if k not in known:
                continue
            # Type coercion for known numeric fields
            if k in ("hit_count", "success_count", "fail_count") and not isinstance(v, int):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = 0
            elif k in ("last_used", "created_at", "avg_duration_s") and not isinstance(v, float):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = 0.0
            elif k == "goal_hash" and not isinstance(v, str):
                v = str(v) if v is not None else ""
            elif k == "plan_dict" and not isinstance(v, dict):
                raise ValueError(f"PlanCacheEntry: plan_dict must be dict, got {type(v)}")
            filtered[k] = v
        return cls(**filtered)


# ══════════════════════════════════════════════════════════════════
# Text similarity
# ══════════════════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    return [t for t in re.sub(r"[^\w\s]", " ", text.lower()).split() if len(t) >= 2]


def _cosine_sim(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0

    def tf(tokens: list[str]) -> dict[str, float]:
        freq: dict[str, int] = {}
        for t in tokens:
            freq[t] = freq.get(t, 0) + 1
        n = len(tokens)
        return {t: c / n for t, c in freq.items()}

    fa, fb = tf(ta), tf(tb)
    vocab = set(fa) | set(fb)
    dot = sum(fa.get(t, 0.0) * fb.get(t, 0.0) for t in vocab)
    mag_a = math.sqrt(sum(v * v for v in fa.values()))
    mag_b = math.sqrt(sum(v * v for v in fb.values()))
    return min(1.0, dot / (mag_a * mag_b)) if mag_a and mag_b else 0.0


def _goal_hash(goal: str) -> str:
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", goal.lower())).strip()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════
# Scoring helpers
# ══════════════════════════════════════════════════════════════════

_HALF_LIFE_S = 14 * 86400   # 14-day half-life for recency decay


def _recency_score(last_used_at: float) -> float:
    """Exponential decay score. 1.0 = used now, 0.5 = used 14 days ago."""
    if not last_used_at:
        return 0.0
    age_s = time.time() - last_used_at
    return math.exp(-math.log(2) * age_s / _HALF_LIFE_S)


def _usage_velocity(recent_uses: list[float], window_days: int = 7) -> float:
    """
    Uses in last window_days as fraction of max expected (10/week = 1.0).
    Capped at 1.0.
    """
    cutoff = time.time() - window_days * 86400
    count = sum(1 for t in recent_uses if t >= cutoff)
    return min(1.0, count / 10.0)


def _compute_template_score(tmpl: WorkflowTemplate) -> float:
    """
    Composite template score (0.0–1.0):
      0.5 × reliability + 0.3 × recency + 0.2 × velocity
    """
    return (
        0.5 * tmpl.reliability
        + 0.3 * _recency_score(tmpl.last_used_at)
        + 0.2 * _usage_velocity(tmpl.recent_uses)
    )


# ══════════════════════════════════════════════════════════════════
# WorkflowLibrary v2
# ══════════════════════════════════════════════════════════════════

class WorkflowLibrary:
    """
    Plan Cache + Reusable Workflow Templates v2.

    Nâng cấp chính:
      - ELO-style template scoring (cập nhật sau mỗi use)
      - Adaptive TTL cho plan cache (7/30/90 ngày tuỳ chất lượng)
      - Auto-prune theo score thay vì chỉ theo số lượng
      - Usage velocity: ưu tiên templates được dùng thường xuyên gần đây
      - Plan promotion: cache entries tốt được promoted lên TTL 90 ngày
      - Prune report: log rõ lý do prune từng item

    Usage::
        lib = WorkflowLibrary()

        # Before LLM call
        cached = lib.get_cached_plan(goal)
        if cached:
            return reconstruct(cached.plan_dict)   # 0 API calls

        # After success
        lib.cache_plan(goal, plan.to_dict())
        lib.auto_extract_workflow(task_id, goal, steps, duration_ms)
        lib.record_plan_outcome(goal, success=True)
    """

    # Matching thresholds
    TEMPLATE_SIM_THRESHOLD = 0.75
    DEDUP_THRESHOLD = 0.85
    MIN_STEPS_TO_EXTRACT = 2

    # Limits
    MAX_CACHE_ENTRIES = 500
    MAX_TEMPLATES = 100

    # v2 prune thresholds
    PRUNE_SCORE_THRESHOLD = 0.10    # Remove templates below this after ≥5 uses
    PRUNE_IDLE_DAYS = 60            # Remove templates unused > 60 days
    PRUNE_FAIL_RATIO = 3.0          # Remove if fail_count > success_count × 3

    # Auto-prune interval
    _PRUNE_EVERY_N_SAVES = 50

    def __init__(self, data_dir: str = "data/skills") -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "workflow_library.json"

        self._plan_cache: dict[str, PlanCacheEntry] = {}
        self._templates: dict[str, WorkflowTemplate] = {}
        self._next_wf_id = 1
        self._save_count = 0            # v2: triggers auto-prune
        self._prune_log: list[dict] = []  # Recent prune events

        self._load()

    # ══ Plan Cache ════════════════════════════════════════════════

    def get_cached_plan(self, goal: str) -> PlanCacheEntry | None:
        """
        Exact + soft-match plan cache lookup.
        v2: checks adaptive TTL and consistent-useless flag.
        """
        h = _goal_hash(goal)
        entry = self._plan_cache.get(h)

        if not entry:
            # Soft match at 0.92 similarity
            best_sim, best_entry = 0.0, None
            for e in self._plan_cache.values():
                if e.is_expired or e.is_consistently_useless:
                    continue
                sim = _cosine_sim(goal, e.goal_sample)
                if sim > best_sim and sim >= 0.92:
                    best_sim, best_entry = sim, e
            if best_entry:
                _vlog("📦", f"Plan cache soft-match (sim={best_sim:.2f}): '{goal[:40]}'")
                best_entry.hit_count += 1
                best_entry.last_used_at = time.time()
                self._save()
                return best_entry
            return None

        # Exact match found — check validity
        if entry.is_expired:
            _vlog("🗑️", f"Plan cache expired (TTL={entry.adaptive_ttl_days}d): '{goal[:40]}'")
            del self._plan_cache[h]
            self._save()
            return None

        if entry.is_consistently_useless:
            _vlog("🗑️", f"Plan cache useless (0 success/{entry.hit_count} hits): '{goal[:40]}'")
            del self._plan_cache[h]
            self._save()
            return None

        entry.hit_count += 1
        entry.last_used_at = time.time()

        # v2: auto-promote high-quality cache
        if not entry.promoted and entry.hit_count >= 5 and entry.success_rate >= 0.8:
            entry.promoted = True
            _vlog("⬆️", f"Plan promoted TTL→90d: '{goal[:40]}'")

        ttl_label = f"TTL={entry.adaptive_ttl_days}d"
        _vlog("📦", f"Plan cache hit #{entry.hit_count} ({ttl_label}, "
              f"rate={entry.success_rate:.0%}): '{goal[:40]}'")
        self._save()
        return entry

    def cache_plan(
        self,
        goal: str,
        plan_dict: dict[str, Any],
        source: str = "decomposer",
    ) -> None:
        """Cache a plan. Overwrites existing entry for same goal."""
        h = _goal_hash(goal)
        existing = self._plan_cache.get(h)
        if existing:
            # Preserve hit history on re-cache
            existing.plan_dict = plan_dict
            existing.source = source
        else:
            self._plan_cache[h] = PlanCacheEntry(
                goal_hash=h,
                goal_sample=goal[:100],
                plan_dict=plan_dict,
                source=source,
            )
        _vlog("💾", f"Plan cached [{source}]: '{goal[:50]}'")

        if len(self._plan_cache) > self.MAX_CACHE_ENTRIES:
            self._prune_cache()
        self._save()

    def record_plan_outcome(self, goal: str, success: bool) -> None:
        """Update success/fail count for cached plan."""
        h = _goal_hash(goal)
        e = self._plan_cache.get(h)
        if not e:
            return
        if success:
            e.success_count += 1
        else:
            e.fail_count += 1
        self._save()

    # ══ Workflow Templates ════════════════════════════════════════

    def auto_extract_workflow(
        self,
        task_id: str,
        goal: str,
        step_dicts: list[dict[str, Any]],
        duration_ms: float = 0.0,
    ) -> WorkflowTemplate | None:
        """
        Extract reusable workflow from successful task.
        v2: updates existing template score on dedup match.
        """
        if len(step_dicts) < self.MIN_STEPS_TO_EXTRACT:
            return None

        workflow_summary = f"{goal} | " + " ".join(
            s.get("description", "") for s in step_dicts
        )[:120]

        # Dedup check
        for tmpl in self._templates.values():
            existing_desc = f"{tmpl.description} | " + " ".join(
                s.description for s in tmpl.steps
            )
            sim = _cosine_sim(workflow_summary, existing_desc)
            if sim >= self.DEDUP_THRESHOLD:
                # v2: update score + velocity on dedup merge
                tmpl.success_count += 1
                tmpl.source_task_ids.append(task_id)
                tmpl.last_used_at = time.time()
                tmpl.recent_uses.append(time.time())
                tmpl.recent_uses = [t for t in tmpl.recent_uses
                                    if time.time() - t < 30 * 86400]
                n = tmpl.total_uses
                tmpl.avg_duration_ms = (
                    (tmpl.avg_duration_ms * (n - 1) + duration_ms) / n
                )
                tmpl.score = _compute_template_score(tmpl)
                _vlog("♻️", f"Workflow dedup (sim={sim:.2f}): "
                      f"'{tmpl.name}' score→{tmpl.score:.2f}")
                self._save()
                return tmpl

        # New template
        if len(self._templates) >= self.MAX_TEMPLATES:
            self._prune_templates(force=True)

        wf_id = f"WF-{self._next_wf_id:03d}"
        self._next_wf_id += 1

        steps = [
            WorkflowStep(
                index=i + 1,
                action=sd.get("action", "execute"),
                description=sd.get("description", ""),
                params_template=self._extract_params_template(sd),
            )
            for i, sd in enumerate(step_dicts)
        ]

        tmpl = WorkflowTemplate(
            id=wf_id,
            name=self._generate_name(goal, steps),
            description=f"Workflow cho: {goal[:80]}",
            steps=steps,
            capability_tags=self._extract_tags(goal, step_dicts),
            trigger_patterns=self._generate_trigger_patterns(goal),
            success_count=1,
            avg_duration_ms=duration_ms,
            source_task_ids=[task_id],
            last_used_at=time.time(),
            recent_uses=[time.time()],
            score=0.6,   # New template starts at moderate score
        )

        self._templates[wf_id] = tmpl
        _vlog("🔧", f"Workflow mới: {wf_id} '{tmpl.name}' "
              f"({len(steps)} steps, score={tmpl.score:.2f})")
        self._save()
        return tmpl

    def find_workflow(
        self,
        goal: str,
        min_score: float = 0.55,
    ) -> WorkflowTemplate | None:
        """
        Find best matching template.
        v2: ranking by composite score × similarity (not just similarity).
        Templates with low score are deprioritised even if similar.
        """
        if not self._templates:
            return None

        # 1. Fast trigger check (regex)
        for tmpl in self._templates.values():
            for pat in tmpl.trigger_patterns:
                try:
                    if re.search(pat, goal, re.IGNORECASE):
                        tmpl.score = _compute_template_score(tmpl)
                        if tmpl.score >= 0.15:  # Don't use trigger if badly scored
                            _vlog("🔧", f"Workflow trigger: {tmpl.id} "
                                  f"'{tmpl.name}' (score={tmpl.score:.2f})")
                            return tmpl
                except re.error:
                    continue

        # 2. Score × similarity ranking
        best_composite, best_tmpl = 0.0, None
        for tmpl in self._templates.values():
            sim = _cosine_sim(
                goal,
                f"{tmpl.description} {' '.join(tmpl.capability_tags)}",
            )
            tmpl_score = _compute_template_score(tmpl)
            composite = sim * (0.6 + 0.4 * tmpl_score)  # Score modulates weight
            if composite > best_composite:
                best_composite, best_tmpl = composite, tmpl

        if best_composite >= min_score and best_tmpl:
            best_tmpl.score = _compute_template_score(best_tmpl)
            _vlog("🔧", f"Workflow match composite={best_composite:.2f} "
                  f"(score={best_tmpl.score:.2f}): '{best_tmpl.name}'")
            return best_tmpl

        return None

    def adapt_workflow(
        self,
        template: WorkflowTemplate,
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Adapt template for new goal. Returns plan_dict."""
        ctx = context or {}
        tasks = []
        for step in template.steps:
            desc = self._parameterize_description(step.description, goal, ctx)
            tasks.append({
                "id": f"T{step.index}",
                "description": desc,
                "action": step.action,
                "params": {**step.params_template, **ctx},
                "depends_on": [f"T{step.index - 1}"] if step.index > 1 else [],
                "status": "pending",
                "result": None,
                "error": "",
            })
        return {
            "goal": goal,
            "tasks": tasks,
            "source": f"workflow:{template.id}",
            "total": len(tasks),
        }

    def record_workflow_outcome(
        self,
        template_id: str,
        success: bool,
        duration_ms: float = 0.0,
    ) -> None:
        """
        Update template after execution.
        v2: recomputes composite score and updates velocity window.
        """
        tmpl = self._templates.get(template_id)
        if not tmpl:
            return

        now = time.time()
        if success:
            tmpl.success_count += 1
            n = tmpl.total_uses
            tmpl.avg_duration_ms = (tmpl.avg_duration_ms * (n - 1) + duration_ms) / n
        else:
            tmpl.fail_count += 1

        tmpl.last_used_at = now
        tmpl.recent_uses.append(now)
        # Trim velocity window to 30 days
        tmpl.recent_uses = [t for t in tmpl.recent_uses if now - t < 30 * 86400]

        # v2: recompute composite score
        old_score = tmpl.score
        tmpl.score = _compute_template_score(tmpl)
        delta = tmpl.score - old_score
        icon = "📈" if delta >= 0 else "📉"
        _vlog(icon, f"Workflow {template_id} score: "
              f"{old_score:.2f} → {tmpl.score:.2f} "
              f"(rel={tmpl.reliability:.0%})")

        self._save()

    # ══ v2 Auto-Prune ═════════════════════════════════════════════

    def run_prune(self, force: bool = False) -> dict[str, int]:
        """
        Run full prune cycle: templates + cache.
        Called automatically every _PRUNE_EVERY_N_SAVES saves,
        or explicitly with force=True.

        Returns:
            {"templates_pruned": N, "cache_pruned": M}
        """
        t_pruned = self._prune_templates(force=force)
        c_pruned = self._prune_cache()
        if t_pruned + c_pruned > 0:
            _vlog("🗑️", f"Auto-prune: -{t_pruned} templates, -{c_pruned} cache entries")
        return {"templates_pruned": t_pruned, "cache_pruned": c_pruned}

    def _prune_templates(self, force: bool = False) -> int:
        """
        Remove low-quality templates.

        Prune criteria (v2):
          A. score < PRUNE_SCORE_THRESHOLD after ≥ 5 uses
          B. not used in PRUNE_IDLE_DAYS days (and not prune_protected)
          C. fail_count > success_count × PRUNE_FAIL_RATIO (with ≥ 3 uses)
        """
        now = time.time()
        to_remove: list[tuple[str, str]] = []   # (wf_id, reason)

        for wf_id, tmpl in self._templates.items():
            if tmpl.prune_protected:
                continue

            # Recompute score before pruning decision
            tmpl.score = _compute_template_score(tmpl)

            # A: low score after enough uses
            if (tmpl.total_uses >= 5
                    and tmpl.score < self.PRUNE_SCORE_THRESHOLD):
                to_remove.append((wf_id,
                                  f"low score {tmpl.score:.2f} after {tmpl.total_uses} uses"))
                continue

            # B: idle too long
            idle_days = (now - tmpl.last_used_at) / 86400 if tmpl.last_used_at else 999
            if idle_days > self.PRUNE_IDLE_DAYS:
                to_remove.append((wf_id, f"idle {idle_days:.0f} days"))
                continue

            # C: heavily failing
            if (tmpl.total_uses >= 3
                    and tmpl.fail_count > tmpl.success_count * self.PRUNE_FAIL_RATIO):
                to_remove.append((wf_id,
                                  f"fail ratio {tmpl.fail_count}/{tmpl.success_count}"))

        for wf_id, reason in to_remove:
            tmpl = self._templates.pop(wf_id, None)
            if tmpl:
                _vlog("🗑️", f"Prune template {wf_id} '{tmpl.name}': {reason}")
                self._prune_log.append({
                    "t": now, "type": "template", "id": wf_id,
                    "name": tmpl.name, "reason": reason,
                })

        # If still over limit after criteria-based prune, remove lowest-scored
        if len(self._templates) > self.MAX_TEMPLATES:
            overflow = len(self._templates) - self.MAX_TEMPLATES
            sorted_by_score = sorted(
                [(wid, t) for wid, t in self._templates.items()
                 if not t.prune_protected],
                key=lambda x: x[1].score,
            )
            for wid, tmpl in sorted_by_score[:overflow]:
                self._templates.pop(wid, None)
                _vlog("🗑️", f"Prune overflow template {wid}: score={tmpl.score:.2f}")
                to_remove.append((wid, f"overflow score={tmpl.score:.2f}"))

        # Trim prune log
        self._prune_log = self._prune_log[-100:]
        return len(to_remove)

    def _prune_cache(self) -> int:
        """
        Remove expired + useless plan cache entries.

        Prune criteria (v2):
          A. is_expired (adaptive TTL)
          B. is_consistently_useless (0 success after ≥3 hits)
        """
        to_remove: list[str] = []
        for h, entry in self._plan_cache.items():
            if entry.is_expired:
                to_remove.append(h)
                _vlog("🗑️", f"Cache expired (TTL={entry.adaptive_ttl_days}d): "
                      f"'{entry.goal_sample[:40]}'")
            elif entry.is_consistently_useless:
                to_remove.append(h)
                _vlog("🗑️", f"Cache useless (0/{entry.hit_count}): "
                      f"'{entry.goal_sample[:40]}'")

        for h in to_remove:
            self._plan_cache.pop(h, None)

        # If still over limit, remove least-used unexpired
        if len(self._plan_cache) > self.MAX_CACHE_ENTRIES:
            overflow = len(self._plan_cache) - self.MAX_CACHE_ENTRIES
            sorted_entries = sorted(
                self._plan_cache.items(),
                key=lambda x: (x[1].hit_count, x[1].last_used_at),
            )
            for h, e in sorted_entries[:overflow]:
                self._plan_cache.pop(h, None)
                to_remove.append(h)

        return len(to_remove)

    # ══ Stats & Admin ══════════════════════════════════════════════

    def stats(self) -> dict[str, Any]:
        """Full stats for Admin Panel and main.py boot log."""
        active_cache = [e for e in self._plan_cache.values() if not e.is_expired]
        top_cache = sorted(active_cache, key=lambda e: e.hit_count, reverse=True)[:3]

        # Sort templates by score descending
        sorted_tmpls = sorted(
            self._templates.values(),
            key=lambda t: t.score, reverse=True,
        )
        top_tmpls = sorted_tmpls[:3]

        return {
            "plan_cache_entries": len(active_cache),
            "plan_cache_promoted": sum(1 for e in active_cache if e.promoted),
            "workflow_templates": len(self._templates),
            "max_templates": self.MAX_TEMPLATES,
            "top_cached_goals": [e.goal_sample[:40] for e in top_cache],
            "top_workflows": [
                {
                    "id": t.id, "name": t.name,
                    "score": round(t.score, 2),
                    "reliability": round(t.reliability, 2),
                    "uses": t.total_uses,
                }
                for t in top_tmpls
            ],
            "recent_prunes": len(self._prune_log),
            "last_prune_log": self._prune_log[-3:],
            "storage_path": str(self._path),
        }

    def list_templates(self) -> list[dict[str, Any]]:
        """All templates sorted by score for Admin Panel."""
        return [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "steps": len(t.steps),
                "success_count": t.success_count,
                "fail_count": t.fail_count,
                "reliability": round(t.reliability, 2),
                "score": round(t.score, 2),
                "avg_duration_ms": round(t.avg_duration_ms),
                "tags": t.capability_tags,
                "prune_protected": t.prune_protected,
            }
            for t in sorted(
                self._templates.values(),
                key=lambda t: t.score, reverse=True,
            )
        ]

    def list_cache(self, top_n: int = 20) -> list[dict[str, Any]]:
        """Top plan cache entries for Admin Panel."""
        active = [e for e in self._plan_cache.values() if not e.is_expired]
        active.sort(key=lambda e: e.hit_count, reverse=True)
        return [
            {
                "goal": e.goal_sample[:60],
                "hit_count": e.hit_count,
                "success_rate": round(e.success_rate, 2),
                "ttl_days": e.adaptive_ttl_days,
                "promoted": e.promoted,
                "source": e.source,
            }
            for e in active[:top_n]
        ]

    def protect_template(self, template_id: str) -> bool:
        """Mark template as prune-protected (manual override)."""
        tmpl = self._templates.get(template_id)
        if not tmpl:
            return False
        tmpl.prune_protected = True
        self._save()
        _vlog("🔒", f"Template {template_id} protected from prune")
        return True

    # ══ Internal helpers ══════════════════════════════════════════

    def _generate_name(self, goal: str, steps: list[WorkflowStep]) -> str:
        words = [w for w in goal.split()[:6] if len(w) >= 3]
        base = " ".join(words[:4]) if words else "workflow"
        return f"{base} ({len(steps)} steps)"

    def _generate_trigger_patterns(self, goal: str) -> list[str]:
        words = _tokenize(goal)
        keywords = [w for w in words if len(w) >= 4][:3]
        if not keywords:
            return []
        combined = r"(?=.*" + r")(?=.*".join(
            rf"\b{re.escape(kw)}\b" for kw in keywords
        ) + r")"
        return [combined]

    def _extract_tags(self, goal: str, step_dicts: list[dict]) -> list[str]:
        tags: set[str] = set()
        all_text = (goal + " " + " ".join(
            s.get("action", "") + " " + s.get("description", "")
            for s in step_dicts
        )).lower()
        TAG_MAP = {
            "excel": "data_excel", "csv": "data_csv", "find_files": "filesystem",
            "chrome": "browser", "navigate": "browser", "tổng hợp": "aggregation",
            "lọc": "filter", "filter": "filter", "báo cáo": "report",
            "report": "report", "gửi": "messaging", "send": "messaging",
            "mở": "app_control", "open": "app_control", "tìm": "search",
            "read": "data_read", "write": "data_write", "create": "data_create",
        }
        for kw, tag in TAG_MAP.items():
            if kw in all_text:
                tags.add(tag)
        return list(tags)[:6]

    def _extract_params_template(self, step_dict: dict) -> dict[str, Any]:
        params = step_dict.get("params", {}) or {}
        clean: dict[str, Any] = {}
        for k, v in params.items():
            if isinstance(v, str) and ("/" in v or "\\" in v):
                clean[k] = "{{file_path}}"
            elif isinstance(v, str) and len(v) > 100:
                clean[k] = "{{long_value}}"
            else:
                clean[k] = v
        return clean

    def _parameterize_description(
        self, template_desc: str, goal: str, ctx: dict
    ) -> str:
        desc = template_desc.replace("{{goal}}", goal[:60])
        for k, v in ctx.items():
            if isinstance(v, str):
                desc = desc.replace(f"{{{{{k}}}}}", v[:50])
        return desc

    # ══ Persistence ═══════════════════════════════════════════════

    def _save(self) -> None:
        self._save_count += 1
        if self._save_count % self._PRUNE_EVERY_N_SAVES == 0:
            self.run_prune()

        try:
            # C-09 FIX: Sign each plan_dict with HMAC before saving
            plan_cache_data: dict = {}
            for h, e in self._plan_cache.items():
                if not e.is_expired and not e.is_consistently_useless:
                    entry_dict = e.to_dict()
                    entry_dict["_hmac"] = _sign_plan(entry_dict.get("plan_dict", {}))
                    plan_cache_data[h] = entry_dict

            data = {
                "version": "9.20.v2",
                "next_wf_id": self._next_wf_id,
                "save_count": self._save_count,
                "prune_log": self._prune_log[-50:],
                "plan_cache": plan_cache_data,
                "templates": {
                    wf_id: t.to_dict()
                    for wf_id, t in self._templates.items()
                },
            }
            self._path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception as exc:
            _vlog("⚠️", f"WorkflowLibrary save error: {str(exc)[:60]}")

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            self._next_wf_id = data.get("next_wf_id", 1)
            self._save_count = data.get("save_count", 0)
            self._prune_log = data.get("prune_log", [])

            for h, d in data.get("plan_cache", {}).items():
                try:
                    # C-09 FIX: Verify HMAC before using cached plan
                    stored_sig = d.pop("_hmac", None)
                    plan_dict = d.get("plan_dict", {})
                    if stored_sig is not None and not _verify_plan(plan_dict, stored_sig):
                        _vlog("🛡️", f"[C-09] Plan HMAC mismatch for {h[:16]} — skipping (possible tampering)")
                        continue
                    e = PlanCacheEntry.from_dict(d)
                    if not e.is_expired and not e.is_consistently_useless:
                        self._plan_cache[h] = e
                except Exception:
                    continue

            for wf_id, d in data.get("templates", {}).items():
                try:
                    self._templates[wf_id] = WorkflowTemplate.from_dict(d)
                except Exception:
                    continue

            # Recompute all scores on load
            for tmpl in self._templates.values():
                tmpl.score = _compute_template_score(tmpl)

            _vlog("📚", f"WorkflowLibrary v2: {len(self._plan_cache)} plans "
                  f"({sum(1 for e in self._plan_cache.values() if e.promoted)} promoted), "
                  f"{len(self._templates)} templates")
        except Exception as exc:
            _vlog("⚠️", f"WorkflowLibrary load error: {str(exc)[:60]}")
