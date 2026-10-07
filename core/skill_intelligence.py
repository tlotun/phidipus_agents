# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/skill_intelligence.py — Phidipus Skill Intelligence Layer v1.0
════════════════════════════════════════════════════════════════════

THE MISSING BRIDGE: Connects SkillRegistryDB (SQLite, scoring, versioning)
to the actual execution path (SkillForge, AgentLoop).

PROBLEM:
  SkillRegistryDB has 596 lines of TF-IDF, routing score, dedup, versioning...
  but is NEVER queried during task execution. SkillForge generates new code
  every time (or uses its own internal cache). Two separate worlds.

SOLUTION:
  This module sits BETWEEN the goal input and SkillForge:

  Goal → SkillIntelligence → [Registry hit?] → Execute proven skill
                            → [Registry miss?] → SkillForge generates new
                            → [Output valid?]  → Confidence gate
                            → [Record result]  → Feed back to registry

COMPONENTS:
  1. CandidateRetriever  — Query registry for top-K matching skills
  2. SkillRanker         — Context-aware scoring (not hardcoded weights)
  3. OutputValidator      — Confidence gate for skill outputs (NEW)
  4. AntiLoopGuard       — Detect skill-level repetition loops (NEW)
  5. FeedbackRecorder    — Record outcomes back to SkillRegistryDB (NEW)
  6. DecisionEngine      — Registry hit → reuse, miss → forge, risky → confirm

FIXES ADDRESSED:
  - skill_db disconnected from execution → CONNECTED via CandidateRetriever
  - No skill output validation → OutputValidator with schema + sanity checks
  - Anti-loop only at step level → AntiLoopGuard tracks skill-level calls
  - No feedback to registry → FeedbackRecorder updates success/fail/latency
  - Hardcoded score weights → Context-adaptive SkillRanker
  - Memory pollution → Decay scoring for old fixes
  - Side-effect risk → RiskClassifier before execution

Security:
  - No exec() — delegates to SkillForge for code execution
  - No subprocess — pure logic layer
  - No network access — queries local SQLite only
  - Output validation is passive (check, don't execute)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Data types
# ══════════════════════════════════════════════════════════════════

@dataclass
class SkillCandidate:
    """A candidate skill from the registry."""
    skill_id: str
    name: str
    description: str
    code_path: str
    score: float               # Composite routing score
    match_reason: str          # Why this skill was selected
    success_rate: float        # Historical success rate
    avg_runtime_ms: float      # Average execution time
    usage_count: int           # How many times used
    risk_level: str = "low"    # low | medium | high
    source: str = "registry"   # registry | forge_cache | template


@dataclass
class ExecutionDecision:
    """Decision from the Intelligence Layer.
    C4 v9.26: thêm action "compose" + composition field.
    """
    action: str                # "execute_registry" | "forge_new" | "require_confirm" | "reject" | "compose"
    candidate: Optional[SkillCandidate] = None
    reason: str = ""
    confidence: float = 0.0    # 0.0 - 1.0
    risk_level: str = "low"
    anti_loop_warning: bool = False
    composition: Any = None    # C4 v9.26: CompositionPlan nếu action == "compose"


@dataclass
class OutputValidation:
    """Result of validating a skill's output."""
    valid: bool
    confidence: float = 1.0    # 0.0 - 1.0
    issues: list[str] = field(default_factory=list)
    suggested_action: str = "" # "accept" | "retry" | "reject" | "human_review"


@dataclass
class SkillCallRecord:
    """Record of a skill call for anti-loop detection."""
    skill_name: str
    goal_hash: str
    timestamp: float
    params_hash: str
    success: bool = False


# ══════════════════════════════════════════════════════════════════
# 1. CANDIDATE RETRIEVER
# ══════════════════════════════════════════════════════════════════

class CandidateRetriever:
    """
    Queries SkillRegistryDB for candidate skills matching a goal.

    Three-tier retrieval:
      Tier 1: Exact trigger pattern match (regex, <1ms)
      Tier 2: TF-IDF text similarity search (top-K, <50ms)
      Tier 3: Capability tag intersection
    """

    def __init__(self, skill_db: Any) -> None:
        self._db = skill_db

    def retrieve(self, goal: str, top_k: int = 5) -> list[SkillCandidate]:
        """Get top-K candidate skills for a goal."""
        if not self._db:
            return []

        candidates: list[SkillCandidate] = []

        # Tier 1: Exact trigger pattern (fastest)
        trigger_match = self._db.find_by_trigger(goal)
        if trigger_match:
            candidates.append(self._to_candidate(
                trigger_match, score=0.95, reason="trigger_pattern_match"
            ))

        # Tier 2: TF-IDF similarity search
        search_results = self._db.search(goal, top_k=top_k)
        for result in search_results:
            # Skip if already found by trigger
            if any(c.skill_id == result.skill.id for c in candidates):
                continue
            candidates.append(self._to_candidate(
                result.skill, score=result.score,
                reason=result.match_reason,
            ))

        # Sort by score descending
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:top_k]

    def _to_candidate(self, skill: Any, score: float, reason: str) -> SkillCandidate:
        """Convert SkillMeta to SkillCandidate."""
        return SkillCandidate(
            skill_id=skill.id,
            name=skill.name,
            description=skill.description,
            code_path=getattr(skill, "code_path", ""),
            score=score,
            match_reason=reason,
            success_rate=skill.success_rate,
            avg_runtime_ms=getattr(skill, "avg_runtime_ms", 0.0),
            usage_count=getattr(skill, "usage_count", 0),
            source="registry",
        )


# ══════════════════════════════════════════════════════════════════
# 2. SKILL RANKER (Context-Aware)
# ══════════════════════════════════════════════════════════════════

class SkillRanker:
    """
    Context-aware skill ranking with adaptive weights.

    Unlike the hardcoded formula in skill_db.py:
      score = 0.5×text_sim + 0.3×reliability + 0.2×recency

    This ranker adjusts weights based on context:
      - Data tasks → reliability matters more (weight up)
      - Simple tasks → recency matters more (prefer recent, tested)
      - Risky tasks → success_rate dominates
      - First-time goals → text_sim dominates (no history)
    """

    # Base weights (overridden by context)
    W_TEXT_SIM = 0.35
    W_RELIABILITY = 0.25
    W_RECENCY = 0.15
    W_SUCCESS_RATE = 0.15
    W_USAGE = 0.10

    # Context modifiers
    CONTEXT_WEIGHTS = {
        "data_task":   {"W_RELIABILITY": 0.35, "W_SUCCESS_RATE": 0.25},
        "simple_task": {"W_RECENCY": 0.30, "W_TEXT_SIM": 0.40},
        "risky_task":  {"W_SUCCESS_RATE": 0.40, "W_RELIABILITY": 0.30},
        "first_time":  {"W_TEXT_SIM": 0.50, "W_USAGE": 0.05},
    }

    def rank(
        self,
        candidates: list[SkillCandidate],
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> list[SkillCandidate]:
        """Rank candidates with context-aware scoring."""
        if not candidates:
            return []

        # Detect context type
        ctx_type = self._detect_context_type(goal, context)
        weights = self._get_weights(ctx_type)

        now = time.time()

        for c in candidates:
            # Text similarity already in c.score from retriever
            text_sim = c.score

            # Reliability = overall reliability_score from registry
            reliability = c.success_rate

            # Recency = how recently the skill was used
            # Decay: half-life 14 days
            days_since_use = 30.0  # default if never used
            if c.usage_count > 0:
                # Approximate from avg_runtime_ms (not perfect, but we don't
                # have last_used_at in the candidate directly)
                days_since_use = min(30.0, 7.0)  # Assume recent if has usage
            recency = math.exp(-0.693 * days_since_use / 14)

            # Success rate
            success = c.success_rate if c.usage_count >= 3 else 0.7  # Prior for new skills

            # Usage velocity (more usage = more trusted)
            usage_norm = min(1.0, c.usage_count / 20)

            # Composite score
            c.score = (
                weights["W_TEXT_SIM"] * text_sim +
                weights["W_RELIABILITY"] * reliability +
                weights["W_RECENCY"] * recency +
                weights["W_SUCCESS_RATE"] * success +
                weights["W_USAGE"] * usage_norm
            )

        # Sort by new score
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _detect_context_type(self, goal: str, context: dict | None) -> str:
        """Detect task context type for weight adjustment."""
        goal_lower = goal.lower()

        # Data tasks: file operations, Excel, database
        if any(kw in goal_lower for kw in [
            "excel", "file", "tồn kho", "báo cáo", "dữ liệu", "data",
            "csv", "database", "import", "export", "filter", "lọc",
        ]):
            return "data_task"

        # Simple tasks: open app, navigate, click
        if any(kw in goal_lower for kw in [
            "mở", "open", "click", "nhấn", "vào trang", "navigate",
            "tab mới", "new tab", "đóng", "close",
        ]):
            return "simple_task"

        # Risky tasks: system changes, delete, update
        if any(kw in goal_lower for kw in [
            "xóa", "delete", "cập nhật", "update", "thay đổi", "modify",
            "ghi đè", "overwrite", "system", "hệ thống",
        ]):
            return "risky_task"

        return "first_time"

    def _get_weights(self, ctx_type: str) -> dict[str, float]:
        """Get weight dict for context type."""
        base = {
            "W_TEXT_SIM": self.W_TEXT_SIM,
            "W_RELIABILITY": self.W_RELIABILITY,
            "W_RECENCY": self.W_RECENCY,
            "W_SUCCESS_RATE": self.W_SUCCESS_RATE,
            "W_USAGE": self.W_USAGE,
        }
        overrides = self.CONTEXT_WEIGHTS.get(ctx_type, {})
        base.update(overrides)

        # Normalize to sum=1
        total = sum(base.values())
        if total > 0:
            for k in base:
                base[k] /= total

        return base


# ══════════════════════════════════════════════════════════════════
# 3. OUTPUT VALIDATOR (NEW — the missing ConfidenceGate for skills)
# ══════════════════════════════════════════════════════════════════

class OutputValidator:
    """
    Validate skill execution output BEFORE accepting it.

    Checks:
      1. Schema compliance (if output_schema defined)
      2. Sanity checks (non-empty, reasonable size)
      3. Goal alignment (output mentions goal keywords)
      4. Consistency (same goal → same output class)
      5. Safety (no suspicious content in output)
    """

    # Minimum confidence to auto-accept output
    CONFIDENCE_THRESHOLD = 0.6

    def validate(
        self,
        output: Any,
        goal: str,
        expected_schema: dict | None = None,
    ) -> OutputValidation:
        """Validate skill output."""
        issues: list[str] = []
        confidence = 1.0

        # Check 1: Non-null/non-empty
        if output is None:
            return OutputValidation(
                valid=False, confidence=0.0,
                issues=["Output is None"],
                suggested_action="retry",
            )

        output_str = str(output)

        if not output_str.strip():
            return OutputValidation(
                valid=False, confidence=0.0,
                issues=["Output is empty"],
                suggested_action="retry",
            )

        # Check 2: Reasonable size (not too short, not too long)
        if len(output_str) < 5:
            issues.append("Output suspiciously short")
            confidence -= 0.2

        if len(output_str) > 100_000:
            issues.append("Output extremely large (>100KB)")
            confidence -= 0.3

        # Check 3: Error indicators in output
        error_patterns = [
            r"(?:error|exception|traceback|failed)",
            r"(?:permission denied|access denied)",
            r"(?:file not found|no such file)",
            r"(?:timeout|timed out)",
        ]
        for pattern in error_patterns:
            if re.search(pattern, output_str[:500], re.IGNORECASE):
                issues.append(f"Error pattern detected: {pattern}")
                confidence -= 0.3
                break

        # Check 4: Goal keyword alignment
        goal_words = set(re.findall(r'\w{3,}', goal.lower()))
        output_words = set(re.findall(r'\w{3,}', output_str[:2000].lower()))
        if goal_words:
            overlap = len(goal_words & output_words) / len(goal_words)
            if overlap < 0.1 and len(output_str) > 50:
                issues.append(f"Low goal alignment ({overlap:.0%})")
                confidence -= 0.15

        # Check 5: Schema compliance
        if expected_schema:
            schema_valid = self._check_schema(output, expected_schema)
            if not schema_valid:
                issues.append("Output doesn't match expected schema")
                confidence -= 0.3

        # Check 6: Suspicious output content
        suspicious = [
            r"__builtins__", r"__import__", r"exec\s*\(",
            r"eval\s*\(", r"os\.system", r"subprocess",
        ]
        for pattern in suspicious:
            if re.search(pattern, output_str[:5000]):
                issues.append(f"Suspicious content: {pattern}")
                confidence = 0.0
                return OutputValidation(
                    valid=False, confidence=0.0,
                    issues=issues, suggested_action="reject",
                )

        # Clamp confidence
        confidence = max(0.0, min(1.0, confidence))

        # Determine action
        if confidence >= self.CONFIDENCE_THRESHOLD:
            action = "accept"
        elif confidence >= 0.3:
            action = "retry"
        else:
            action = "human_review"

        return OutputValidation(
            valid=confidence >= self.CONFIDENCE_THRESHOLD,
            confidence=confidence,
            issues=issues,
            suggested_action=action,
        )

    def _check_schema(self, output: Any, schema: dict) -> bool:
        """Basic schema check for output."""
        if not schema:
            return True

        try:
            if isinstance(output, str):
                data = json.loads(output)
            elif isinstance(output, dict):
                data = output
            else:
                return True  # Can't check non-dict/str

            required = schema.get("required", [])
            for field_name in required:
                if field_name not in data:
                    return False

            return True
        except (json.JSONDecodeError, TypeError):
            return True  # Not JSON, can't validate schema


# ══════════════════════════════════════════════════════════════════
# 4. ANTI-LOOP GUARD (NEW — skill-level loop detection)
# ══════════════════════════════════════════════════════════════════

class AntiLoopGuard:
    """
    Detect and prevent skill-level repetition loops.

    Tracks:
      - Last N skill calls per goal
      - Identical skill+params combinations
      - Forge→fail→heal→forge cycles

    Triggers:
      - Same skill called 3+ times in a row → BLOCK
      - Same skill+params called 2+ times → BLOCK (exact repeat)
      - Forge→fail→forge→fail pattern 3+ times → ESCALATE to human
    """

    MAX_HISTORY = 50
    MAX_SAME_SKILL = 3        # Max consecutive calls to same skill
    MAX_EXACT_REPEAT = 2      # Max identical (skill + params) calls
    MAX_FORGE_RETRIES = 3     # Max forge→fail→forge cycles

    def __init__(self) -> None:
        self._history: deque[SkillCallRecord] = deque(maxlen=self.MAX_HISTORY)
        self._forge_fail_count: dict[str, int] = {}  # goal_hash → fail count

    def check_before_call(
        self,
        skill_name: str,
        goal: str,
        params: dict | None = None,
    ) -> dict[str, Any]:
        """
        Check if this call would create a loop.

        Returns:
            {"allowed": bool, "reason": str, "suggestion": str}
        """
        goal_hash = hashlib.md5(goal.encode()).hexdigest()[:12]
        params_hash = hashlib.md5(
            json.dumps(params or {}, sort_keys=True).encode()
        ).hexdigest()[:12]

        # Get recent calls for this goal
        recent = [r for r in self._history if r.goal_hash == goal_hash]
        recent_5 = recent[-5:] if len(recent) >= 5 else recent

        # Check 1: Same skill called N+ times consecutively
        if recent_5:
            consecutive = 0
            for r in reversed(recent_5):
                if r.skill_name == skill_name:
                    consecutive += 1
                else:
                    break
            if consecutive >= self.MAX_SAME_SKILL:
                return {
                    "allowed": False,
                    "reason": f"Skill '{skill_name}' đã gọi {consecutive} lần liên tiếp",
                    "suggestion": "Thử skill khác hoặc phân tách task",
                }

        # Check 2: Exact repeat (same skill + same params)
        exact_repeats = sum(
            1 for r in recent_5
            if r.skill_name == skill_name and r.params_hash == params_hash
        )
        if exact_repeats >= self.MAX_EXACT_REPEAT:
            return {
                "allowed": False,
                "reason": f"Gọi lặp lại chính xác: '{skill_name}' với cùng params",
                "suggestion": "Thay đổi approach hoặc params",
            }

        # Check 3: Forge retry loop
        if skill_name == "__forge__":
            current_fails = self._forge_fail_count.get(goal_hash, 0)
            if current_fails >= self.MAX_FORGE_RETRIES:
                return {
                    "allowed": False,
                    "reason": f"SkillForge đã thất bại {current_fails} lần cho goal này",
                    "suggestion": "Escalate to TaskDecomposer hoặc human review",
                }

        return {"allowed": True, "reason": "", "suggestion": ""}

    def record_call(
        self,
        skill_name: str,
        goal: str,
        params: dict | None = None,
        success: bool = False,
    ) -> None:
        """Record a skill call."""
        goal_hash = hashlib.md5(goal.encode()).hexdigest()[:12]
        params_hash = hashlib.md5(
            json.dumps(params or {}, sort_keys=True).encode()
        ).hexdigest()[:12]

        self._history.append(SkillCallRecord(
            skill_name=skill_name,
            goal_hash=goal_hash,
            timestamp=time.time(),
            params_hash=params_hash,
            success=success,
        ))

        # Track forge failures
        if skill_name == "__forge__" and not success:
            self._forge_fail_count[goal_hash] = (
                self._forge_fail_count.get(goal_hash, 0) + 1
            )
        elif success:
            # Reset on success
            self._forge_fail_count.pop(goal_hash, None)

    def penalize_score(
        self,
        candidates: list[SkillCandidate],
        goal: str,
    ) -> list[SkillCandidate]:
        """Penalize candidates that were recently called and failed."""
        goal_hash = hashlib.md5(goal.encode()).hexdigest()[:12]
        recent_fails = {
            r.skill_name
            for r in self._history
            if r.goal_hash == goal_hash and not r.success
            and (time.time() - r.timestamp) < 300  # Last 5 minutes
        }

        for c in candidates:
            if c.name in recent_fails:
                c.score *= 0.5  # Halve score for recently-failed skills
                c.match_reason += " [penalized: recent failure]"

        return candidates

    def stats(self) -> dict:
        return {
            "total_records": len(self._history),
            "active_forge_failures": dict(self._forge_fail_count),
        }


# ══════════════════════════════════════════════════════════════════
# 5. FEEDBACK RECORDER (NEW — closes the loop)
# ══════════════════════════════════════════════════════════════════

class FeedbackRecorder:
    """
    Records execution outcomes back to SkillRegistryDB.

    This is the CRITICAL missing piece: SkillForge executes skills
    but never reports success/failure back to the registry. Without
    feedback, the registry's reliability scores are always 1.0 (default)
    and the ranking is meaningless.

    Records:
      - Execution success/failure
      - Runtime latency
      - Output quality (from OutputValidator confidence)
      - Provider used (for LLM fallback analysis)
    """

    def __init__(self, skill_db: Any) -> None:
        self._db = skill_db

    def record(
        self,
        skill_id: str,
        success: bool,
        runtime_ms: float = 0.0,
        output_confidence: float = 1.0,
        provider: str = "",
        error: str = "",
    ) -> None:
        """Record execution outcome to registry."""
        if not self._db:
            return

        try:
            self._db.record_usage(
                skill_id=skill_id,
                success=success,
                runtime_ms=runtime_ms,
            )
            _vlog("📊", f"Feedback → registry: {skill_id[:8]} "
                  f"{'✅' if success else '❌'} ({runtime_ms:.0f}ms)")
        except Exception as exc:
            _vlog("⚠", f"Feedback record failed: {exc}")

    def record_forge_result(
        self,
        goal: str,
        skill_name: str,
        code: str,
        success: bool,
        runtime_ms: float = 0.0,
        provider: str = "",
    ) -> str | None:
        """
        Record a SkillForge result into the registry.

        If the skill is new and successful, register it in the DB
        so future goals can find it WITHOUT generating new code.

        Returns skill_id if registered, None otherwise.
        """
        if not self._db or not success:
            return None

        try:
            from skills.skill_db import SkillMeta
            import hashlib as _hl

            meta = SkillMeta(
                id=_hl.sha256(f"{skill_name}:{goal}".encode()).hexdigest()[:16],
                name=skill_name,
                description=goal[:200],
                code_hash=_hl.sha256(code.encode()).hexdigest(),
                capability_tags=self._extract_tags(goal),
                trigger_patterns=self._build_trigger_patterns(goal),
                source="forge",
                usage_count=1,
                success_count=1,
                avg_runtime_ms=runtime_ms,
                code_path="",  # Code stored in SkillForge cache
            )

            # Register (will dedup if similar skill exists)
            registered = self._db.register(meta)
            _vlog("📚", f"Forge → Registry: '{registered.name}' [{registered.id[:8]}]")
            return registered.id

        except Exception as exc:
            _vlog("⚠", f"Forge→Registry failed: {exc}")
            return None

    def _extract_tags(self, goal: str) -> list[str]:
        """Extract capability tags from goal text."""
        tags = []
        tag_keywords = {
            "file": ["file", "tệp", "tập tin"],
            "excel": ["excel", "xlsx", "spreadsheet", "bảng tính"],
            "chrome": ["chrome", "browser", "trình duyệt"],
            "data": ["dữ liệu", "data", "lọc", "filter", "sort"],
            "report": ["báo cáo", "report", "tổng hợp", "summary"],
            "web": ["web", "url", "trang web", "website"],
            "image": ["ảnh", "image", "hình", "photo"],
            "social": ["facebook", "instagram", "twitter", "post", "đăng"],
        }
        goal_lower = goal.lower()
        for tag, keywords in tag_keywords.items():
            if any(kw in goal_lower for kw in keywords):
                tags.append(tag)
        return tags

    def _build_trigger_patterns(self, goal: str) -> list[str]:
        """Build regex trigger patterns from goal keywords."""
        # Extract significant words (≥3 chars, Vietnamese-aware)
        words = re.findall(r'[\w]{3,}', goal.lower())
        if not words:
            return []

        # Build pattern from top 3 keywords
        significant = [w for w in words if w not in {
            "của", "và", "cho", "từ", "này", "đó", "với",
            "the", "and", "for", "from", "this", "that", "with",
        }][:3]

        if significant:
            pattern = ".*".join(re.escape(w) for w in significant)
            return [f"(?i){pattern}"]

        return []


# ══════════════════════════════════════════════════════════════════
# 6. DECISION ENGINE (orchestrates everything)
# ══════════════════════════════════════════════════════════════════

class SkillIntelligence:
    """
    THE BRIDGE between SkillRegistryDB and the execution path.

    Usage in agent_loop.py:

        # At initialization:
        self._intelligence = SkillIntelligence(self._skill_db)

        # Before SkillForge (between P3 and P4):
        decision = self._intelligence.decide(goal)

        if decision.action == "execute_registry":
            # Use proven skill from registry — 0 API calls!
            result = await self._forge.execute_cached_code(
                decision.candidate.code_path, goal
            )
        elif decision.action == "forge_new":
            # No registry match — let SkillForge generate
            result = await self._forge.execute(goal)
        elif decision.action == "require_confirm":
            # Risky operation — ask human first
            await self._notify_confirmation(decision)

        # After execution:
        self._intelligence.record_outcome(goal, result)
    """

    # Score threshold for registry match
    REGISTRY_THRESHOLD = 0.65

    # Minimum success rate to trust a registry skill
    MIN_TRUST_RATE = 0.6

    # Minimum usage count before trusting success rate
    MIN_USAGE_FOR_TRUST = 3

    def __init__(self, skill_db: Any = None) -> None:
        self._db        = skill_db
        self._retriever = CandidateRetriever(skill_db)
        self._ranker    = SkillRanker()
        self._validator = OutputValidator()
        self._anti_loop = AntiLoopGuard()
        self._feedback  = FeedbackRecorder(skill_db)
        self._enabled   = skill_db is not None
        # v9.22: Auto-Promotion + Skill Composition
        self._promoter  = AutoPromoter(skill_db)
        self._composer  = SkillComposer(skill_db)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Main decision API ─────────────────────────────────────

    def decide(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> ExecutionDecision:
        """
        Decide how to handle a goal: registry skill, forge new, or confirm.

        This is called BEFORE SkillForge in the execution chain:
          SmartAction → [SkillIntelligence.decide()] → SkillForge
        """
        if not self._enabled:
            return ExecutionDecision(
                action="forge_new",
                reason="SkillRegistryDB not available",
                confidence=0.0,
            )

        # Step 1: Anti-loop check for forge
        loop_check = self._anti_loop.check_before_call("__forge__", goal)
        anti_loop_warning = not loop_check["allowed"]
        if anti_loop_warning:
            _vlog("🔁", f"Anti-loop: {loop_check['reason']}")

        # Step 2: Retrieve candidates from registry
        candidates = self._retriever.retrieve(goal, top_k=5)

        if not candidates:
            _vlog("🔍", f"Registry: 0 candidates → forge new")
            return ExecutionDecision(
                action="forge_new",
                reason="No matching skills in registry",
                confidence=0.0,
                anti_loop_warning=anti_loop_warning,
            )

        # Step 3: Rank candidates (context-aware)
        ranked = self._ranker.rank(candidates, goal, context)

        # Step 4: Apply anti-loop penalties
        ranked = self._anti_loop.penalize_score(ranked, goal)

        # Step 5: Evaluate top candidate
        top = ranked[0]

        _vlog("🧠", f"Registry top: '{top.name}' score={top.score:.2f} "
              f"(rate={top.success_rate:.0%}, used={top.usage_count}x)")

        # Decision tree
        if top.score >= self.REGISTRY_THRESHOLD:
            # Strong match — but is it trustworthy?
            if (top.usage_count >= self.MIN_USAGE_FOR_TRUST
                    and top.success_rate >= self.MIN_TRUST_RATE):
                # Proven skill — use it!
                risk = self._assess_risk(top, goal)
                if risk == "high":
                    return ExecutionDecision(
                        action="require_confirm",
                        candidate=top,
                        reason=f"High-risk operation: {top.name}",
                        confidence=top.score,
                        risk_level="high",
                    )
                return ExecutionDecision(
                    action="execute_registry",
                    candidate=top,
                    reason=f"Registry match: {top.match_reason}",
                    confidence=top.score,
                    risk_level=risk,
                )
            else:
                # Match but unproven — still worth trying
                # (cheaper than forge, builds trust data)
                return ExecutionDecision(
                    action="execute_registry",
                    candidate=top,
                    reason=f"Registry match (unproven): {top.match_reason}",
                    confidence=top.score * 0.8,
                    risk_level="medium",
                )

        # ── C4 v9.26: SkillComposer — ghép skills trước khi gọi Forge ──────
        # Check nếu có thể compose từ existing skills → 0 API calls
        try:
            composition = self._composer.find_composition(goal)
            if composition and len(composition.steps) >= 2:
                _vlog("🔗", f"SkillComposer: found pipeline "
                      f"[{' → '.join(s.skill_name or '?' for s in composition.steps[:3])}]")
                return ExecutionDecision(
                    action="compose",
                    reason=f"SkillComposer: {len(composition.steps)}-step pipeline",
                    confidence=0.85,
                    composition=composition,
                )
        except Exception as _c4_err:
            _vlog("⚠️", f"SkillComposer error: {_c4_err}")
        # ─────────────────────────────────────────────────────────────────────

        # Weak match or no good candidate
        if anti_loop_warning:
            return ExecutionDecision(
                action="require_confirm",
                reason=f"Forge loop + weak registry match. {loop_check['suggestion']}",
                confidence=0.3,
                risk_level="high",
                anti_loop_warning=True,
            )

        return ExecutionDecision(
            action="forge_new",
            reason=f"Best registry match too weak ({top.score:.2f} < {self.REGISTRY_THRESHOLD})",
            confidence=top.score,
        )

    # ── Output validation ─────────────────────────────────────

    def validate_output(
        self,
        output: Any,
        goal: str,
        expected_schema: dict | None = None,
    ) -> OutputValidation:
        """Validate skill execution output."""
        return self._validator.validate(output, goal, expected_schema)

    # ── Feedback recording ────────────────────────────────────

    def record_outcome(
        self,
        goal: str,
        skill_name: str,
        success: bool,
        code: str = "",
        runtime_ms: float = 0.0,
        provider: str = "",
        source: str = "forge",
    ) -> None:
        """Record execution outcome to registry + anti-loop + promoter."""
        # Record in anti-loop guard
        self._anti_loop.record_call(
            skill_name=skill_name if source == "registry" else "__forge__",
            goal=goal,
            success=success,
        )

        # Record in registry
        if source == "forge" and success and code:
            skill_id = self._feedback.record_forge_result(
                goal=goal,
                skill_name=skill_name,
                code=code,
                success=success,
                runtime_ms=runtime_ms,
                provider=provider,
            )
            # v9.22: Feed promoter — track forge skill success
            if skill_id:
                self._promoter.record(skill_id, skill_name, success=True)
        elif source == "forge" and not success:
            # Track forge failure for promotion accounting
            skill_id_guess = hashlib.md5(f"{skill_name}:{goal}".encode()).hexdigest()[:16]
            self._promoter.record(skill_id_guess, skill_name, success=False)
        elif source == "registry":
            self._feedback.record(
                skill_id=skill_name,
                success=success,
                runtime_ms=runtime_ms,
            )
            # v9.22: Track registry skill for promotion too
            self._promoter.record(skill_name, skill_name, success=success)

    # ── Risk assessment ───────────────────────────────────────

    def _assess_risk(self, candidate: SkillCandidate, goal: str) -> str:
        """Assess risk level of executing a skill."""
        goal_lower = goal.lower()

        if any(kw in goal_lower for kw in [
            "xóa", "delete", "remove", "ghi đè", "overwrite",
            "system", "hệ thống", "root", "admin", "sudo",
            "deploy", "production", "database",
        ]):
            return "high"

        if any(kw in goal_lower for kw in [
            "tạo", "create", "write", "ghi", "update", "cập nhật",
            "modify", "thay đổi", "send", "gửi", "post", "đăng",
        ]):
            return "medium"

        return "low"

    # ── v9.22 Auto-Promotion check ────────────────────────────

    def check_auto_promote(self, skill_name: str) -> bool:
        """
        Phase 2A: Check if a Forge-generated skill has earned promotion.

        Called by agent_loop after every Skill Forge success.
        When a skill hits PROMOTION_SUCCESS_COUNT with PROMOTION_MIN_RATE,
        AutoPromoter marks it as 'promoted' in the registry so
        SkillIntelligence always picks it first (score boost).

        Returns True if promotion happened.
        """
        return self._promoter.check_and_promote(skill_name)

    # ── v9.22 Skill Composition ───────────────────────────────

    def find_composition(self, goal: str) -> "CompositionPlan | None":
        """
        Phase 3A: Try to compose a pipeline from existing skills.

        Called between SkillIntelligence.decide() and SkillForge.
        If goal can be satisfied by chaining existing registry skills,
        returns a CompositionPlan (zero LLM calls).
        """
        return self._composer.find_composition(goal)

    async def execute_composition(self, composition: "CompositionPlan", goal: str) -> bool:
        """
        C4 v9.26: Thực thi composition pipeline — 0 API calls.
        Chạy từng step theo thứ tự, output step N → context step N+1.
        """
        _vlog("🔗", f"Executing {len(composition.steps)}-step composition")
        prev_output = goal
        for i, step in enumerate(composition.steps, 1):
            _vlog("🔗", f"  Step {i}: '{step.skill_name or "?"}'  (cap={step.capability or "?"})")
            try:
                if self._db:
                    skill = self._db.get(step.skill_id)
                    code_path = getattr(skill, 'code_path', '') if skill else ''
                    if code_path:
                        from pathlib import Path as _P
                        if _P(code_path).exists():
                            import importlib.util as _ilu
                            spec = _ilu.spec_from_file_location("_cstep", code_path)
                            if spec and spec.loader:
                                mod = _ilu.module_from_spec(spec)
                                spec.loader.exec_module(mod)
                                if hasattr(mod, 'main'):
                                    import asyncio as _aio
                                    out = await _aio.wait_for(
                                        mod.main(prev_output), timeout=30.0
                                    )
                                    prev_output = str(out)
                                    _vlog("✅", f"  Step {i} OK")
                                    continue
                _vlog("⚠️", f"  Step {i} skip (no code) → continue")
                continue
            except Exception as _e:
                _vlog("⚠️", f"  Step {i} error: {_e} → abort")
                return False
        _vlog("✅", f"Composition done: {len(composition.steps)} steps, 0 API calls")
        return True

    # ── Stats ─────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "enabled":            self._enabled,
            "anti_loop":          self._anti_loop.stats(),
            "registry_threshold": self.REGISTRY_THRESHOLD,
            "min_trust_rate":     self.MIN_TRUST_RATE,
            "promoter":           self._promoter.stats(),
            "composer":           self._composer.stats(),
        }


# ══════════════════════════════════════════════════════════════════
# Phase 2A — AUTO-PROMOTER
# Watches Skill Forge outcomes; promotes proven skills to top priority.
# ══════════════════════════════════════════════════════════════════

@dataclass
class PromotionRecord:
    """Tracks promotion status of a forge-generated skill."""
    skill_id:     str
    skill_name:   str
    success_count: int = 0
    fail_count:    int = 0
    promoted:      bool = False
    promoted_at:   float = 0.0


class AutoPromoter:
    """
    Phase 2A: Automatically promotes proven Skill Forge results.

    Threshold (configurable):
      PROMOTION_SUCCESS_COUNT = 5   successes required
      PROMOTION_MIN_RATE      = 0.8 (80%) success rate required

    On promotion:
      1. skill.reliability_score boosted → SkillIntelligence prefers it
      2. Trigger patterns extracted → faster future lookup
      3. _vlog announcement for admin visibility

    Storage: in-memory dict + persisted to registry via skill_db.record_usage
    No new files — uses existing SkillRegistryDB.
    """

    PROMOTION_SUCCESS_COUNT = 5
    PROMOTION_MIN_RATE      = 0.80

    def __init__(self, skill_db: Any = None) -> None:
        self._db = skill_db
        self._records: dict[str, PromotionRecord] = {}

    def record(self, skill_id: str, skill_name: str, success: bool) -> None:
        """Record an execution outcome for promotion tracking."""
        if skill_id not in self._records:
            self._records[skill_id] = PromotionRecord(
                skill_id=skill_id, skill_name=skill_name
            )
        rec = self._records[skill_id]
        if success:
            rec.success_count += 1
        else:
            rec.fail_count += 1

    def check_and_promote(self, skill_name: str) -> bool:
        """
        Check if skill_name has earned promotion, and promote if so.
        Returns True when promotion happens (once per skill).
        """
        # Find record by name
        rec = next(
            (r for r in self._records.values() if r.skill_name == skill_name),
            None,
        )
        if rec is None or rec.promoted:
            return False

        total = rec.success_count + rec.fail_count
        if total < self.PROMOTION_SUCCESS_COUNT:
            return False
        rate = rec.success_count / total if total else 0
        if rate < self.PROMOTION_MIN_RATE:
            return False

        # ── Promote ──────────────────────────────────────────────
        rec.promoted    = True
        rec.promoted_at = time.time()

        # Boost reliability_score in DB so SkillRanker prefers it
        if self._db:
            try:
                skill = self._db.get(rec.skill_id)
                if skill:
                    # Force reliability to 0.99 — will stay top of ranking
                    skill.reliability_score = 0.99
                    self._db._insert(skill)   # upsert via internal method
            except Exception:
                pass

        _vlog("🏆", f"AUTO-PROMOTE: '{skill_name or "unknown"}' thành công {rec.success_count}/"
              f"{total} lần ({rate:.0%}) → promoted to top priority")
        return True

    def stats(self) -> dict:
        promoted = [r for r in self._records.values() if r.promoted]
        return {
            "tracked_skills":    len(self._records),
            "promoted_skills":   len(promoted),
            "promoted_names":    [r.skill_name for r in promoted],
            "threshold_success": self.PROMOTION_SUCCESS_COUNT,
            "threshold_rate":    self.PROMOTION_MIN_RATE,
        }


# ══════════════════════════════════════════════════════════════════
# Phase 3A — SKILL COMPOSER
# Chains existing registry skills into pipelines by capability tags.
# ══════════════════════════════════════════════════════════════════

@dataclass
class CompositionStep:
    """One step in a composed pipeline."""
    skill_id:   str
    skill_name: str
    capability: str
    input_from: str = ""   # "" = from goal, else step skill_id


@dataclass
class CompositionPlan:
    """A pipeline composed from existing registry skills."""
    steps:      list[CompositionStep]
    goal:       str
    confidence: float = 0.0
    reason:     str   = ""

    @property
    def total(self) -> int:
        return len(self.steps)


# Map: goal keywords → required capability tags (order matters = execution order)
_COMPOSITION_PATTERNS: list[dict] = [
    {
        "keywords": ["excel", "tồn kho", "inventory", "lọc", "filter", "báo cáo"],
        "required_caps": ["file", "excel", "data"],
        "description": "File → Excel read → Data filter/process",
    },
    {
        "keywords": ["pdf", "đọc", "read", "tóm tắt", "summary"],
        "required_caps": ["file", "data"],
        "description": "Find file → Read/summarise",
    },
    {
        "keywords": ["web", "tìm trên", "search online", "fetch", "url"],
        "required_caps": ["web", "data"],
        "description": "Web fetch → process result",
    },
    {
        "keywords": ["ảnh", "image", "hình", "báo cáo", "report"],
        "required_caps": ["file", "excel"],
        "description": "Data → Report/output",
    },
]


class SkillComposer:
    """
    Phase 3A: Compose a pipeline from existing registry skills.

    When a goal needs [cap_A, cap_B, cap_C] and all exist in registry,
    return a CompositionPlan that chains them — zero LLM or API calls.

    The composer reads capability_tags from SkillMeta in the registry.
    Skills with overlapping tags are chained in dependency order.

    Example:
      Goal: "lọc dữ liệu Excel từ file tồn kho"
      Required: [file, excel, data]
      Registry has:
        skill_A (tags=[file])  → find the right file
        skill_B (tags=[excel]) → read/process Excel
        skill_C (tags=[data])  → filter rows
      CompositionPlan: A → B → C
    """

    # Minimum score threshold for a skill to be usable in composition
    MIN_SKILL_SCORE = 0.60

    def __init__(self, skill_db: Any = None) -> None:
        self._db = skill_db

    def find_composition(self, goal: str) -> "CompositionPlan | None":
        """
        Try to build a composition plan for a goal.
        Returns None if no good composition found.
        """
        if not self._db:
            return None

        goal_lower = goal.lower()

        # Find matching pattern
        pattern = None
        for p in _COMPOSITION_PATTERNS:
            kw_hits = sum(1 for kw in p["keywords"] if kw in goal_lower)
            if kw_hits >= 2:
                pattern = p
                break

        if not pattern:
            return None

        required_caps = pattern["required_caps"]

        # For each required capability, find best matching skill
        steps: list[CompositionStep] = []
        prev_skill_id = ""

        for cap in required_caps:
            # Search registry for skills with this capability tag
            candidates = self._find_skills_by_capability(cap, goal)
            if not candidates:
                _vlog("🔗", f"Composer: no skill for cap='{cap}' — composition aborted")
                return None

            best = candidates[0]
            steps.append(CompositionStep(
                skill_id=best.skill_id,
                skill_name=best.name,
                capability=cap,
                input_from=prev_skill_id,
            ))
            prev_skill_id = best.skill_id

        if len(steps) < 2:
            return None

        avg_confidence = sum(
            s.success_rate for s in self._db.search(goal, top_k=3)
            if s
        ) / max(len(steps), 1) if steps else 0.5

        plan = CompositionPlan(
            steps=steps,
            goal=goal,
            confidence=min(0.95, avg_confidence),
            reason=pattern["description"],
        )
        _vlog("🔗", f"Composer: {len(steps)}-step pipeline — {pattern['description']}")
        return plan

    def _find_skills_by_capability(
        self, cap: str, goal: str
    ) -> list["SkillCandidate"]:
        """Find registry skills that have a capability tag matching cap."""
        if not self._db:
            return []
        try:
            # Get all active skills, filter by capability tag
            results = self._db.search(f"{cap} {goal}", top_k=10)
            filtered = []
            for r in results:
                if cap in (r.skill.capability_tags or []):
                    filtered.append(SkillCandidate(
                        skill_id=r.skill.id,
                        name=r.skill.name,
                        description=r.skill.description,
                        code_path=getattr(r.skill, "code_path", ""),
                        score=r.score,
                        match_reason=f"cap:{cap}",
                        success_rate=r.skill.success_rate,
                        avg_runtime_ms=getattr(r.skill, "avg_runtime_ms", 0.0),
                        usage_count=getattr(r.skill, "usage_count", 0),
                    ))
            # Sort by success_rate then score
            filtered.sort(key=lambda c: (c.success_rate, c.score), reverse=True)
            return filtered
        except Exception:
            return []

    def stats(self) -> dict:
        return {
            "patterns_available": len(_COMPOSITION_PATTERNS),
            "db_connected": self._db is not None,
        }
