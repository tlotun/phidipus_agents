# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/semantic_memory.py — Phidipus Semantic Memory v9.20
══════════════════════════════════════════════════════════

Long-term knowledge about the user's system and patterns.
Accumulates automatically from successful task executions.

Knowledge types:
  - File patterns: "file tồn kho thường ở ~/Documents, pattern *inventory*.xlsx"
  - App behaviors: "Chrome profile picker cần quit+relaunch"
  - Data schemas: "Excel tồn kho có columns: Product, Price, Quantity"
  - User preferences: "User hay dùng profile 00Fujin và 37 phantienluat"

Usage:
    sm = SemanticMemory()
    
    # Learn from successful task
    sm.learn("file_pattern", {
        "subject": "file tồn kho",
        "location": "~/Documents",
        "pattern": "*inventory*.xlsx",
    })
    
    # Query knowledge
    results = sm.query("file tồn kho")
    # → [{"type": "file_pattern", "subject": "file tồn kho", "location": "~/Documents"}]
    
    # Get context string for LLM/Gemini
    context = sm.get_context_for("tìm file tồn kho lọc bóng đèn")
    # → "Known: file tồn kho ở ~/Documents, pattern *inventory*.xlsx
    #    Excel columns: Product, Price, Quantity"
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


@dataclass
class KnowledgeItem:
    """One piece of system knowledge."""
    id: str
    type: str           # file_pattern, app_behavior, data_schema, user_preference
    subject: str        # What this knowledge is about
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0    # 0-1, increases with repeated confirmation
    usage_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0
    source: str = ""    # Which task/skill learned this


class SemanticMemory:
    """
    Persistent knowledge base about the user's system.

    Grows automatically from successful task executions.
    Provides context to SkillForge and LLM for smarter planning.
    """

    def __init__(self, storage_path: str = "data/memory/semantic_memory.json") -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, KnowledgeItem] = {}
        self._max_items: int = 500
        self._learn_counter: int = 0  # v9.21: cleanup trigger
        self._load()

    # ══════════════════════════════════════════════════════════
    # Learn (add/update knowledge)
    # ══════════════════════════════════════════════════════════

    def learn(self, ktype: str, data: dict[str, Any], source: str = "") -> str:
        """
        Add or update a knowledge item.

        Args:
            ktype: Knowledge type (file_pattern, app_behavior, data_schema, user_preference)
            data: Knowledge data dict (must contain "subject" key)
            source: Where this knowledge came from (task_id, skill_name, etc.)

        Returns:
            Knowledge item ID
        """
        subject = data.get("subject", str(data)[:50])
        kid = self._make_id(ktype, subject)

        existing = self._items.get(kid)
        if existing:
            # Update existing — merge data, increase confidence
            existing.data.update(data)
            existing.confidence = min(1.0, existing.confidence + 0.1)
            existing.usage_count += 1
            existing.last_used = time.time()
        else:
            self._items[kid] = KnowledgeItem(
                id=kid,
                type=ktype,
                subject=subject,
                data=data,
                confidence=0.5,  # Start at 0.5, grows with confirmation
                source=source,
            )

        # Trim if too many
        if len(self._items) > self._max_items:
            self._trim()

        # v9.21: Periodic decay cleanup
        self._learn_counter += 1
        if self._learn_counter % self.CLEANUP_INTERVAL == 0:
            self.cleanup()

        self._save()
        return kid

    def learn_file_pattern(self, description: str, location: str,
                           pattern: str = "", source: str = "") -> str:
        """Shortcut: learn where a type of file is typically found."""
        return self.learn("file_pattern", {
            "subject": description,
            "location": location,
            "pattern": pattern,
        }, source)

    def learn_data_schema(self, file_desc: str, columns: list[str],
                          sample_row: list[str] | None = None, source: str = "") -> str:
        """Shortcut: learn Excel/CSV column structure."""
        return self.learn("data_schema", {
            "subject": file_desc,
            "columns": columns,
            "sample_row": sample_row or [],
        }, source)

    def learn_app_behavior(self, app: str, behavior: str, source: str = "") -> str:
        """Shortcut: learn app-specific knowledge."""
        return self.learn("app_behavior", {
            "subject": app,
            "behavior": behavior,
        }, source)

    def learn_user_preference(self, preference: str, value: Any, source: str = "") -> str:
        """Shortcut: learn user habits/preferences."""
        return self.learn("user_preference", {
            "subject": preference,
            "value": value,
        }, source)

    # ══════════════════════════════════════════════════════════
    # Query knowledge
    # ══════════════════════════════════════════════════════════

    def query(self, search: str, ktype: str = "", top_k: int = 5) -> list[dict]:
        """
        Search knowledge by keyword.

        Returns list of matching items sorted by relevance.
        """
        search_words = set(re.findall(r'\w+', search.lower()))
        results = []

        for item in self._items.values():
            if ktype and item.type != ktype:
                continue

            # Score by word overlap
            item_text = f"{item.subject} {json.dumps(item.data, ensure_ascii=False)}"
            item_words = set(re.findall(r'\w+', item_text.lower()))

            overlap = search_words & item_words
            if not overlap:
                continue

            score = len(overlap) / max(len(search_words), 1)
            score += item.confidence * 0.2  # Boost confident knowledge
            score += min(0.2, item.usage_count * 0.02)  # Boost frequently used

            # v9.21: Freshness penalty — old unused items score lower
            last_active = item.last_used if item.last_used > 0 else item.created_at
            days_idle = (time.time() - last_active) / 86400
            freshness = max(0.0, 1.0 - days_idle / 60)  # 0 after 60 days idle
            score *= (0.5 + 0.5 * freshness)  # Halve score for very old items

            results.append({
                "id": item.id,
                "type": item.type,
                "subject": item.subject,
                "data": item.data,
                "score": round(score, 3),
                "confidence": item.confidence,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def get(self, kid: str) -> KnowledgeItem | None:
        """Get specific knowledge item by ID."""
        item = self._items.get(kid)
        if item:
            item.usage_count += 1
            item.last_used = time.time()
        return item

    # ══════════════════════════════════════════════════════════
    # v9.21: Decay & Forget — prevent knowledge rot
    # ══════════════════════════════════════════════════════════

    # Decay config
    DECAY_HALF_LIFE_DAYS = 30   # Confidence halves every 30 days unused
    STALE_THRESHOLD_DAYS = 90   # Items older than 90 days with no use → delete
    MIN_CONFIDENCE = 0.05       # Below this → delete
    CLEANUP_INTERVAL = 50       # Run cleanup every N learn() calls

    def decay_all(self) -> int:
        """
        Apply time-based confidence decay to ALL items.

        Formula: confidence *= 2^(-days_since_last_use / half_life)

        Items that haven't been used recently lose confidence.
        Items used frequently maintain or grow confidence.
        Items below MIN_CONFIDENCE are deleted.

        Returns number of items decayed.
        """
        now = time.time()
        decayed = 0
        to_delete = []

        for kid, item in self._items.items():
            last_active = item.last_used if item.last_used > 0 else item.created_at
            days_idle = (now - last_active) / 86400

            if days_idle <= 1:
                continue  # Used today, skip

            # Exponential decay
            decay_factor = 2.0 ** (-days_idle / self.DECAY_HALF_LIFE_DAYS)
            new_conf = item.confidence * decay_factor

            if new_conf < self.MIN_CONFIDENCE:
                to_delete.append(kid)
            else:
                item.confidence = round(new_conf, 4)
                decayed += 1

        # Delete dead items
        for kid in to_delete:
            del self._items[kid]

        if to_delete:
            _vlog("🧹", f"Semantic decay: {decayed} decayed, {len(to_delete)} deleted")

        return decayed + len(to_delete)

    def forget(self, kid: str) -> bool:
        """Explicitly forget a knowledge item."""
        if kid in self._items:
            del self._items[kid]
            self._save()
            return True
        return False

    def forget_stale(self) -> int:
        """Delete items that haven't been used in STALE_THRESHOLD_DAYS."""
        now = time.time()
        threshold = now - (self.STALE_THRESHOLD_DAYS * 86400)
        to_delete = []

        for kid, item in self._items.items():
            last_active = item.last_used if item.last_used > 0 else item.created_at
            if last_active < threshold and item.usage_count < 3:
                to_delete.append(kid)

        for kid in to_delete:
            del self._items[kid]

        if to_delete:
            _vlog("🧹", f"Semantic forget: {len(to_delete)} stale items removed")
            self._save()

        return len(to_delete)

    def cleanup(self) -> dict[str, int]:
        """
        Full cleanup: decay + forget stale + save.
        Called periodically (every CLEANUP_INTERVAL learn() calls).
        """
        decayed = self.decay_all()
        stale = self.forget_stale()
        self._save()
        return {"decayed": decayed, "stale_removed": stale, "remaining": len(self._items)}

    # ══════════════════════════════════════════════════════════
    # Context generation (for LLM/Gemini prompts)
    # ══════════════════════════════════════════════════════════

    def get_context_for(self, goal: str, max_items: int = 5) -> str:
        """
        Generate knowledge context string for a goal.
        Injected into SkillForge/LLM prompts for smarter code generation.
        """
        relevant = self.query(goal, top_k=max_items)
        if not relevant:
            return ""

        lines = ["Kiến thức đã biết:"]
        for item in relevant:
            if item["type"] == "file_pattern":
                loc = item["data"].get("location", "")
                pat = item["data"].get("pattern", "")
                lines.append(f"  - File '{item['subject']}' thường ở {loc}" +
                            (f", pattern: {pat}" if pat else ""))
            elif item["type"] == "data_schema":
                cols = item["data"].get("columns", [])
                lines.append(f"  - '{item['subject']}' có columns: {', '.join(cols[:8])}")
            elif item["type"] == "app_behavior":
                lines.append(f"  - {item['subject']}: {item['data'].get('behavior', '')}")
            elif item["type"] == "user_preference":
                lines.append(f"  - User preference: {item['subject']} = {item['data'].get('value', '')}")

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════
    # Auto-learn from task execution
    # ══════════════════════════════════════════════════════════

    def extract_from_task(self, goal: str, steps: list[dict], success: bool) -> int:
        """
        Auto-extract knowledge from a completed task.

        Scans step results for file paths, column names, patterns.
        Only learns from SUCCESSFUL tasks (confidence in accuracy).

        Returns number of items learned.
        """
        if not success:
            return 0

        learned = 0
        goal_lower = goal.lower()

        for step in steps:
            result = step.get("result", "")
            action = step.get("action", "")
            result_str = str(result)

            # Learn file locations
            file_paths = re.findall(r'(/[^\s"]+\.(?:xlsx|xls|csv|json|txt|pdf))', result_str)
            for fp in file_paths:
                dirname = str(Path(fp).parent)
                filename = Path(fp).name
                # Extract a keyword from goal for subject
                keywords = re.findall(r'\w{3,}', goal_lower)
                subject = " ".join(keywords[:3]) if keywords else filename
                self.learn_file_pattern(subject, dirname, f"*{filename}*",
                                        source=f"task:{goal[:30]}")
                learned += 1

            # Learn column names from read_excel results
            if action in ("read_excel", "read_csv") and isinstance(result, dict):
                headers = result.get("headers", [])
                if headers:
                    self.learn_data_schema(
                        step.get("description", goal[:30]),
                        headers[:15],
                        source=f"task:{goal[:30]}",
                    )
                    learned += 1

        if learned:
            _vlog("🧠", f"Semantic memory: học được {learned} kiến thức mới từ task")

        return learned

    # ══════════════════════════════════════════════════════════
    # Persistence
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _make_id(ktype: str, subject: str) -> str:
        words = re.findall(r'\w+', subject.lower())[:4]
        return f"{ktype}__{'_'.join(words)}" if words else f"{ktype}__{int(time.time())}"

    def _trim(self) -> None:
        """Remove low-value items when over limit."""
        items = sorted(
            self._items.values(),
            key=lambda i: (i.confidence, i.usage_count, i.last_used),
        )
        # Remove bottom 20%
        remove_count = len(items) // 5
        for item in items[:remove_count]:
            del self._items[item.id]

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            for kid, item in data.items():
                self._items[kid] = KnowledgeItem(**item)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            data = {}
            for kid, item in self._items.items():
                data[kid] = {
                    "id": item.id, "type": item.type, "subject": item.subject,
                    "data": item.data, "confidence": item.confidence,
                    "usage_count": item.usage_count, "created_at": item.created_at,
                    "last_used": item.last_used, "source": item.source,
                }
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # Stats
    # ══════════════════════════════════════════════════════════

    def stats(self) -> dict:
        by_type: dict[str, int] = {}
        for item in self._items.values():
            by_type[item.type] = by_type.get(item.type, 0) + 1
        return {
            "total_items": len(self._items),
            "by_type": by_type,
            "avg_confidence": round(
                sum(i.confidence for i in self._items.values()) / max(len(self._items), 1), 2
            ),
        }
