# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/failure_memory.py — Phidipus Failure Memory v9.20
═════════════════════════════════════════════════════════

Stores error history + fix strategies so agent:
  - Doesn't repeat the same mistake
  - Applies known fixes instantly (< 0.1s) instead of re-diagnosing
  - Gets smarter over time

Storage: data/memory/failure_memory.json

Usage:
    fm = FailureMemory()
    
    # Record a failure + fix
    fm.record_failure(
        error_type="FileNotFoundError",
        context={"goal": "đọc file tồn kho", "file": "inventory.xlsx"},
        fix_strategy="search_files('*inventory*tồn kho*')",
        fix_success=True,
    )
    
    # Lookup fix for similar error
    fix = fm.find_fix("FileNotFoundError", {"file": "inventory.xlsx"})
    # → {"strategy": "search_files('*inventory*tồn kho*')", "success_rate": 0.92}
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")



@dataclass
class FailureRecord:
    """One recorded failure + fix attempt."""
    error_type: str          # FileNotFoundError, TimeoutError, etc.
    error_message: str       # Full error message (truncated)
    context: dict[str, Any]  # goal, file, app, step, etc.
    fix_strategy: str        # What was done to fix
    fix_success: bool        # Did the fix work?
    timestamp: float = field(default_factory=time.time)
    goal: str = ""           # Original user goal


@dataclass
class FixStrategy:
    """Aggregated fix strategy with success tracking."""
    error_type: str
    strategy: str
    success_count: int = 0
    fail_count: int = 0
    last_used: float = 0.0
    context_patterns: list[str] = field(default_factory=list)
    hint: str = ""     # v2: actionable fix hint for LLM prompt

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        if total == 0:
            return 0.0
        raw_rate = self.success_count / total

        # v9.21: CEILING — prevent runaway success amplification
        # A fix CANNOT have > 0.9 success_rate unless it has 10+ total uses.
        # This prevents a fix that succeeded 2/2 (100%) from outranking
        # a proven fix with 15/20 (75%) just because the sample is small.
        if total < 5:
            return min(raw_rate, 0.7)    # Cap at 70% with < 5 samples
        if total < 10:
            return min(raw_rate, 0.85)   # Cap at 85% with < 10 samples
        return min(raw_rate, 0.95)       # Never 100% — always room for doubt

    @property
    def total_uses(self) -> int:
        return self.success_count + self.fail_count

    @property
    def time_decayed_score(self) -> float:
        """
        v9.21: Score that factors in recency decay.
        Fixes not used recently lose ranking even if success_rate is high.
        Half-life: 14 days.
        """
        base = self.success_rate
        if self.last_used <= 0:
            return base * 0.3  # Never used → big penalty

        days_idle = (time.time() - self.last_used) / 86400
        if days_idle <= 1:
            return base  # Used today, no decay
        decay = 2.0 ** (-days_idle / 14)  # Half-life 14 days
        return base * (0.3 + 0.7 * decay)  # Floor at 30% of base


class FailureMemory:
    """
    Persistent failure memory with fix strategy lookup.

    Stores error patterns + fixes so self-healing can:
    1. Find fix for known error instantly
    2. Rank fixes by success rate
    3. Avoid fixes that have failed before
    """

    def __init__(self, storage_dir: str = "data/memory") -> None:
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "failure_memory.json"

        self._records: list[FailureRecord] = []
        self._strategies: dict[str, list[FixStrategy]] = {}
        # [C-14 FIX] Bounds: max 200 error types, max 10 strategies per type
        self._MAX_STRATEGY_TYPES: int = 200
        self._MAX_STRATEGIES_PER_TYPE: int = 10
        self._max_records: int = 500
        self._record_counter: int = 0  # v9.21: decay trigger

        self._load()

    # ══════════════════════════════════════════════════════════
    # Record failures
    # ══════════════════════════════════════════════════════════

    def record_failure(
        self,
        error_type: str,
        error_message: str = "",
        context: dict[str, Any] | None = None,
        fix_strategy: str = "",
        fix_success: bool = False,
        goal: str = "",
    ) -> None:
        """Record a failure and its fix attempt."""
        record = FailureRecord(
            error_type=error_type,
            error_message=error_message[:300],
            context=context or {},
            fix_strategy=fix_strategy,
            fix_success=fix_success,
            goal=goal,
        )
        self._records.append(record)

        # Trim old records
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records:]

        # Update strategy stats
        if fix_strategy:
            self._update_strategy(error_type, fix_strategy, fix_success, context)

        # v9.21: Periodic strategy decay (every 30 records)
        self._record_counter += 1
        if self._record_counter % 30 == 0:
            self.decay_strategies()

        self._save()

    def _update_strategy(self, error_type: str, strategy: str,
                         success: bool, context: dict | None) -> None:
        """Update aggregated strategy stats."""
        if error_type not in self._strategies:
            # [C-14 FIX] Enforce global type count cap
            if len(self._strategies) >= self._MAX_STRATEGY_TYPES:
                # Evict the error type with fewest strategies (least useful)
                evict_key = min(
                    self._strategies.keys(),
                    key=lambda k: len(self._strategies[k])
                )
                del self._strategies[evict_key]
            self._strategies[error_type] = []

        # Find existing strategy
        existing = None
        for s in self._strategies[error_type]:
            if s.strategy == strategy:
                existing = s
                break

        if existing:
            if success:
                existing.success_count += 1
            else:
                existing.fail_count += 1
            existing.last_used = time.time()
        else:
            patterns = []
            if context:
                # Extract context patterns for matching
                for k, v in context.items():
                    if isinstance(v, str) and v:
                        patterns.append(f"{k}={v[:50]}")
            # [C-14 FIX] Enforce per-type strategy cap
            if len(self._strategies[error_type]) >= self._MAX_STRATEGIES_PER_TYPE:
                # Remove oldest strategy (index 0)
                self._strategies[error_type].pop(0)
            self._strategies[error_type].append(FixStrategy(
                error_type=error_type,
                strategy=strategy,
                success_count=1 if success else 0,
                fail_count=0 if success else 1,
                last_used=time.time(),
                context_patterns=patterns,
            ))

    # ══════════════════════════════════════════════════════════
    # Find fixes
    # ══════════════════════════════════════════════════════════

    def find_fix(self, error_type: str, context: dict[str, Any] | None = None) -> dict | None:
        """
        Find the best fix for an error type.
        v9.21: Uses time_decayed_score instead of raw success_rate.

        Returns:
            {"strategy": str, "success_rate": float, "uses": int} or None
        """
        strategies = self._strategies.get(error_type, [])
        if not strategies:
            return None

        # v9.21: Sort by decayed score, not raw success_rate
        ranked = sorted(
            strategies,
            key=lambda s: (s.time_decayed_score, s.total_uses),
            reverse=True,
        )

        # Filter out strategies with < 30% decayed score (proven bad or stale)
        viable = [s for s in ranked if s.time_decayed_score >= 0.3 or s.total_uses < 3]

        if not viable:
            return None

        best = viable[0]
        return {
            "strategy": best.strategy,
            "success_rate": round(best.success_rate, 2),
            "decayed_score": round(best.time_decayed_score, 2),
            "uses": best.total_uses,
            "last_used": best.last_used,
        }

    def find_all_fixes(self, error_type: str) -> list[dict]:
        """
        Find all known fixes for error_type, ranked by decayed score.
        v9.21: uses time_decayed_score, includes ceiling info.
        """
        strategies = self._strategies.get(error_type, [])
        ranked = sorted(strategies, key=lambda s: s.time_decayed_score, reverse=True)
        return [
            {
                "strategy": s.strategy,
                "success_rate": round(s.success_rate, 2),
                "decayed_score": round(s.time_decayed_score, 2),
                "uses": s.total_uses,
                "last_used": s.last_used,
                "context_patterns": list(s.context_patterns),
                "hint": getattr(s, "hint", ""),
            }
            for s in ranked
        ]

    def has_seen_error(self, error_type: str) -> bool:
        """Check if we've encountered this error type before."""
        return error_type in self._strategies

    # ══════════════════════════════════════════════════════════
    # v9.21: Strategy decay & cleanup
    # ══════════════════════════════════════════════════════════

    STRATEGY_STALE_DAYS = 60    # Remove strategies unused for 60+ days
    STRATEGY_MIN_RATE = 0.15    # Remove strategies below this decayed score after 5+ uses

    def decay_strategies(self) -> dict[str, int]:
        """
        Remove stale and proven-bad strategies.

        Prevents:
          - Positive feedback loop on bad fixes (dampened by ceiling + decay)
          - Unlimited growth of strategy lists
          - Stale fixes persisting forever

        Returns: {"removed": int, "remaining": int}
        """
        now = time.time()
        threshold_ts = now - (self.STRATEGY_STALE_DAYS * 86400)
        total_removed = 0

        for error_type in list(self._strategies.keys()):
            strats = self._strategies[error_type]
            survivors = []

            for s in strats:
                # Remove if stale (unused for 60+ days) and not proven
                if s.last_used < threshold_ts and s.total_uses < 5:
                    total_removed += 1
                    continue

                # Remove if proven bad after sufficient samples
                if s.total_uses >= 5 and s.time_decayed_score < self.STRATEGY_MIN_RATE:
                    total_removed += 1
                    _vlog("🧹", f"Pruned fix: '{s.strategy[:40]}' "
                          f"(score={s.time_decayed_score:.2f}, uses={s.total_uses})")
                    continue

                survivors.append(s)

            if survivors:
                self._strategies[error_type] = survivors
            else:
                del self._strategies[error_type]

        if total_removed:
            _vlog("🧹", f"Failure memory: pruned {total_removed} stale/bad strategies")
            self._save()

        remaining = sum(len(v) for v in self._strategies.values())
        return {"removed": total_removed, "remaining": remaining}

    # ══════════════════════════════════════════════════════════
    # Built-in fix strategies (hardcoded knowledge)
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def builtin_fix(error_type: str) -> str | None:
        """
        Return hardcoded fix strategy for common errors.
        Used when no learned fix exists yet.
        """
        fixes = {
            "FileNotFoundError": "search_files with broader pattern",
            "PermissionError": "check file permissions, try alternative path",
            "TimeoutError": "retry with longer timeout",
            "ConnectionError": "check network, retry after 5s",
            "KeyError": "inspect data structure, check column names",
            "ValueError": "validate input data format",
            "ModuleNotFoundError": "pip install missing module",
            "JSONDecodeError": "check file encoding, try utf-8-sig",
            "UnicodeDecodeError": "try encoding: utf-8, latin-1, cp1252",
        }
        return fixes.get(error_type)

    # ══════════════════════════════════════════════════════════
    # Persistence
    # ══════════════════════════════════════════════════════════

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            # Load records
            for item in data.get("records", [])[-self._max_records:]:
                self._records.append(FailureRecord(**item))
            # Load strategies
            for error_type, strats in data.get("strategies", {}).items():
                self._strategies[error_type] = [FixStrategy(**s) for s in strats]
        except Exception:
            pass

    def _save(self) -> None:
        try:
            data = {
                "records": [
                    {
                        "error_type": r.error_type,
                        "error_message": r.error_message,
                        "context": r.context,
                        "fix_strategy": r.fix_strategy,
                        "fix_success": r.fix_success,
                        "timestamp": r.timestamp,
                        "goal": r.goal,
                    }
                    for r in self._records[-self._max_records:]
                ],
                "strategies": {
                    etype: [
                        {
                            "error_type": s.error_type,
                            "strategy": s.strategy,
                            "success_count": s.success_count,
                            "fail_count": s.fail_count,
                            "last_used": s.last_used,
                            "context_patterns": s.context_patterns,
                        }
                        for s in strats
                    ]
                    for etype, strats in self._strategies.items()
                },
            }
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # Stats
    # ══════════════════════════════════════════════════════════

    # ══ Provider switch tracking (LLM Fallback Stack) ════════════

    def record_switch(
        self,
        goal_hash: str,
        from_provider: str,
        to_provider: str,
        reason: str,
    ) -> None:
        """
        Record an LLM provider switch for smart future routing.
        Stored under error_type='provider_switch' with next_provider key.
        """
        self.record_failure(
            error_type="provider_switch",
            error_message=reason[:200],
            context={
                "goal_hash": goal_hash,
                "from_provider": from_provider,
                "to_provider": to_provider,
                "next_provider": to_provider,
            },
            goal=f"forge:{goal_hash}",
        )
        _vlog("🔀", f"Provider switch: {from_provider} → {to_provider} ({reason[:50]})")

    def get_recommended_provider(self, goal_hash: str) -> str | None:
        """
        Return provider that succeeded before for goal_hash.
        Looks up most recent successful provider_switch record.
        Returns provider name or None.
        """
        try:
            matches: list[tuple[float, str]] = []
            for record in self._records:
                if (record.error_type == "provider_switch"
                        and record.context.get("goal_hash") == goal_hash
                        and record.context.get("to_provider")):
                    matches.append((record.timestamp, record.context["to_provider"]))
            if matches:
                matches.sort(reverse=True)
                provider = matches[0][1]
                _vlog("💡", f"FM recommend: {provider} (từ lịch sử goal {goal_hash[:8]})")
                return provider
        except Exception:
            pass
        return None

    def stats(self) -> dict[str, Any]:

        return {
            "total_records": len(self._records),
            "error_types": len(self._strategies),
            "strategies": {
                etype: len(strats) for etype, strats in self._strategies.items()
            },
            "top_errors": sorted(
                [(etype, len(recs)) for etype, recs in self._strategies.items()],
                key=lambda x: x[1], reverse=True,
            )[:5],
        }
