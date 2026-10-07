# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/keyboard_first.py — Phidipus keyboard-first action layer (v4.3)
═══════════════════════════════════════════════════════════════════════

Why
---
"Screenshot → VLM → click (x, y)" costs 1.5-25 s per step, needs a vision
model in RAM and misses whenever the layout shifts.  Most desktop commands
already have an exact, instant, layout-independent form on macOS:

    1. a keyboard shortcut              (⌘T new tab, ⇧⌘G go to folder …)
    2. a menu-bar command               (File ▸ Export as PDF…) via Accessibility
    3. a short keyboard macro           (⌘L → type URL → ⏎)
    4. an accessibility element         (element_find / element_click)
    5. vision — only when 1-4 cannot express the step

This module owns tiers 1-3 for L1: it loads data/shortcuts/macos.yaml,
reads the cheap UI state through the ``ui_snapshot`` IPC action (frontmost
app, focused window/element, URL — milliseconds, no screenshot), matches a
command (Vietnamese/English, accent-insensitive, FULL-phrase match so it never
hijacks a longer request), runs it, verifies the effect with a second
snapshot and falls back to the menu path when a shortcut had no effect.

Used by
-------
  utils/smart_actions.py      fast path before Brain / LLM (no model call)
  planner/react_reasoner.py   "shortcut" / "menu_select" tools for the agent
  core/workflow_executor.py   "shortcut" / "menu" / "macro" workflow nodes
"""
from __future__ import annotations

import asyncio
import platform
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = _ROOT / "data" / "shortcuts" / "macos.yaml"

MODIFIERS = {"cmd", "command", "alt", "option", "opt", "shift", "ctrl", "control", "fn"}
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


def fold(text: str) -> str:
    """lower-case, strip Vietnamese diacritics (đ → d), collapse spaces, trim punctuation."""
    t = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "D")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip(" .!?;,")


def _fold_char(ch: str) -> str:
    """fold() for one character, always exactly one character (alignment)."""
    if ch in "đĐ":
        return "d"
    base = "".join(c for c in unicodedata.normalize("NFD", ch) if unicodedata.category(c) != "Mn").lower()
    return base if len(base) == 1 else (ch.lower()[:1] or " ")


def _macos_major() -> int:
    try:
        return int((platform.mac_ver()[0] or "0").split(".")[0])
    except ValueError:
        return 0


# Polite / command words that may wrap a shortcut request (already folded)
_PREFIX_RE = re.compile(
    r"^(?:(?:hay|giup toi|giup minh|lam on|please|ban oi|cho toi|nhan|bam|press|"
    r"dung phim tat|phim tat|thuc hien|hit)\s+)+")
_SUFFIX_RE = re.compile(
    r"(?:\s+(?:di|nhe|nha|giup|gium|dum|giup toi|gium toi|ngay|now|please|cho toi|luon))+$")


# ══════════════════════════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════════════════════════
@dataclass
class UIContext:
    """Cheap description of what is on screen (from the ui_snapshot action)."""
    ok: bool = False
    app: str = ""
    bundle_id: str = ""
    window_title: str = ""
    window_count: int = -1
    focused_role: str = ""
    focused_label: str = ""
    focused_is_text: bool = False
    focused_value: str = ""
    selected_text: str = ""
    url: str = ""
    document: str = ""
    sheet_open: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, d: dict[str, Any] | None) -> "UIContext":
        d = d or {}
        if not isinstance(d, dict) or not d.get("bundle_id") and not d.get("app"):
            return cls()
        return cls(
            ok=True, app=str(d.get("app", "")), bundle_id=str(d.get("bundle_id", "")),
            window_title=str(d.get("window_title", "")), window_count=int(d.get("window_count", -1) or -1),
            focused_role=str(d.get("focused_role", "")), focused_label=str(d.get("focused_label", "")),
            focused_is_text=bool(d.get("focused_is_text", False)),
            focused_value=str(d.get("focused_value", "")), selected_text=str(d.get("selected_text", "")),
            url=str(d.get("url", "")), document=str(d.get("document", "")),
            sheet_open=bool(d.get("sheet_open", False)), raw=d,
        )

    def summary(self, max_value: int = 300) -> str:
        """One compact paragraph for an LLM prompt (no screenshot needed)."""
        if not self.ok:
            return ""
        parts = [f"App: {self.app} ({self.bundle_id})"]
        if self.window_title:
            parts.append(f"window: {self.window_title!r}")
        if self.window_count >= 0:
            parts.append(f"{self.window_count} window(s)")
        if self.url:
            parts.append(f"URL: {self.url}")
        if self.document:
            parts.append(f"document: {self.document}")
        if self.sheet_open:
            parts.append("a dialog/sheet is open")
        if self.focused_role:
            foc = f"focus: {self.focused_role}"
            if self.focused_label:
                foc += f" {self.focused_label!r}"
            if self.focused_is_text:
                foc += " (text input)"
            parts.append(foc)
        if self.focused_value:
            parts.append(f"field value: {self.focused_value[:max_value]!r}")
        if self.selected_text:
            parts.append(f"selected text: {self.selected_text[:max_value]!r}")
        return " · ".join(parts)


@dataclass
class Shortcut:
    group: str
    id: str
    keys: str = ""
    desc: str = ""
    say: list[str] = field(default_factory=list)
    risk: str = "low"
    when: str = ""
    url: str = ""
    expect: str = ""
    menu: list[list[str]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    since: int = 0
    bundles: list[str] = field(default_factory=lambda: ["*"])
    specificity: int = 0          # 0 any app · 1 app family alias · 2 explicit app
    app: str = ""                 # app key a macro needs in front
    param_rules: dict[str, str] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return f"{self.group}.{self.id}"

    @property
    def is_macro(self) -> bool:
        return bool(self.steps)

    def key_list(self) -> list[str]:
        return [k for k in self.keys.split("+") if k]

    def pretty_keys(self) -> str:
        sym = {"cmd": "⌘", "alt": "⌥", "shift": "⇧", "ctrl": "⌃", "fn": "fn"}
        return "".join(sym.get(k, k.upper() if len(k) == 1 else k) for k in self.key_list())


@dataclass
class Match:
    shortcut: Shortcut
    params: dict[str, str] = field(default_factory=dict)
    app_key: str = ""
    phrase: str = ""


@dataclass
class KFResult:
    success: bool
    method: str                 # "shortcut" | "macro" | "menu"
    name: str                   # qualified shortcut id or menu path
    verified: Optional[bool] = None
    output: str = ""
    error: str = ""
    duration_ms: int = 0


# ══════════════════════════════════════════════════════════════════
# Catalog
# ══════════════════════════════════════════════════════════════════
class ShortcutCatalog:
    def __init__(self, path: Path = CATALOG_PATH) -> None:
        import yaml
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        self.apps: dict[str, dict[str, Any]] = data.get("apps") or {}
        self._aliases: dict[str, list[str]] = data.get("aliases") or {}
        self.items: list[Shortcut] = []
        self._phrases: dict[str, list[Shortcut]] = {}
        self._templates: list[tuple[re.Pattern, Shortcut, str]] = []
        self.macos_major = _macos_major()
        for g in data.get("groups") or []:
            for s in g.get("shortcuts") or []:
                self._add(self._make(s, g.get("name", "group"), g))
        for m in data.get("macros") or []:
            self._add(self._make(m, "macro", m))

    # ── loading ───────────────────────────────────────────────────
    def _expand(self, bundles: list[str]) -> tuple[list[str], int]:
        out: list[str] = []
        spec = 2
        for b in bundles or ["*"]:
            if b == "*":
                return ["*"], 0
            if str(b).startswith("@"):
                spec = 1
                inner, _ = self._expand(self._aliases.get(b, []))
                out.extend(inner)
            else:
                out.append(str(b))
        return out, spec

    def _make(self, s: dict[str, Any], group: str, g: dict[str, Any]) -> Shortcut:
        bundles, spec = self._expand(s.get("bundles") or g.get("bundles") or ["*"])
        menu = s.get("menu") or []
        steps = s.get("steps") or []
        return Shortcut(
            group=group, id=str(s["id"]), keys=str(s.get("keys", "") or "").lower(),
            desc=str(s.get("desc", "")), say=[str(x) for x in s.get("say") or []],
            risk=str(s.get("risk", "low")), when=str(s.get("when") or g.get("when") or ""),
            url=str(s.get("url") or g.get("url") or ""), expect=str(s.get("expect", "") or ""),
            menu=[list(map(str, p)) for p in menu], steps=list(steps), since=int(s.get("since", 0) or 0),
            bundles=bundles, specificity=spec, app=str(s.get("app", "") or ""),
            param_rules=dict(s.get("param_rules") or {}),
        )

    def _add(self, sc: Shortcut) -> None:
        self.items.append(sc)
        for phrase in sc.say:
            if "{" in phrase:
                names = re.findall(r"\{(\w+)\}", phrase)
                rx = re.escape(fold(phrase))
                for n in names:
                    rx = rx.replace(re.escape("{" + n + "}"), f"(?P<{n}>.+)")
                self._templates.append((re.compile(f"^{rx}$"), sc, phrase))
            else:
                self._phrases.setdefault(fold(phrase), []).append(sc)

    # ── applicability ─────────────────────────────────────────────
    def applicable(self, sc: Shortcut, ui: UIContext, bundle: str = "") -> bool:
        bundle = bundle or ui.bundle_id
        if "*" not in sc.bundles and bundle not in sc.bundles:
            return False
        if sc.since and self.macos_major and self.macos_major < sc.since:
            return False
        if sc.when == "text_focus" and not ui.focused_is_text:
            return False
        if sc.when == "no_text_focus" and ui.focused_is_text:
            return False
        if sc.url and sc.url not in (ui.url or ""):
            return False
        return True

    def app_bundle(self, app_key: str) -> str:
        return str((self.apps.get(app_key) or {}).get("bundle", ""))

    def app_name(self, app_key: str) -> str:
        return str((self.apps.get(app_key) or {}).get("name", app_key))

    # ── matching ──────────────────────────────────────────────────
    def _split_app(self, cmd: str) -> tuple[str, str]:
        """'mo tab moi trong chrome' → ('mo tab moi', 'chrome')."""
        for key, app in self.apps.items():
            for name in sorted((fold(x) for x in app.get("say") or [key]), key=len, reverse=True):
                m = re.match(rf"^(.+?)\s+(?:trong|tren|o|in|on|cua|voi|bang)\s+(?:app\s+|ung dung\s+)?"
                             rf"{re.escape(name)}$", cmd)
                if m:
                    return m.group(1).strip(), key
        return cmd, ""

    def _variants(self, cmd: str) -> list[str]:
        out = [cmd]
        stripped = _SUFFIX_RE.sub("", _PREFIX_RE.sub("", cmd)).strip()
        if stripped and stripped != cmd:
            out.append(stripped)
        return out

    def find(self, command: str, ui: UIContext) -> Optional[Match]:
        cmd = fold(command)
        if not cmd:
            return None
        cmd, app_key = self._split_app(cmd)
        target_bundle = self.app_bundle(app_key) if app_key else ui.bundle_id
        ctx = ui if not app_key or target_bundle == ui.bundle_id else \
            UIContext(ok=True, bundle_id=target_bundle, app=self.app_name(app_key))
        best: Optional[Match] = None
        best_rank = (-1, -1)
        for variant in self._variants(cmd):
            for sc in self._phrases.get(variant, []):
                if not self.applicable(sc, ctx, target_bundle):
                    continue
                rank = (sc.specificity, 1)
                if rank > best_rank:
                    best, best_rank = Match(sc, {}, app_key, variant), rank
            if best is not None:
                break
            for rx, sc, phrase in self._templates:
                m = rx.match(variant)
                if not m or not self.applicable(sc, ctx, target_bundle):
                    continue
                params = self._params(command, m)
                if not params or not all(re.search(rule, params.get(k, "")) for k, rule in sc.param_rules.items()):
                    continue
                rank = (sc.specificity, 0)
                if rank > best_rank:
                    best, best_rank = Match(sc, params, app_key or sc.app, phrase), rank
            if best is not None:
                break
        return best

    @staticmethod
    def _params(original: str, m: re.Match) -> dict[str, str]:
        """Recover parameter text with its original case/accents."""
        out = {}
        norm = re.sub(r"\s+", " ", unicodedata.normalize("NFC", original.strip())).strip(" .!?;,")
        aligned = "".join(_fold_char(ch) for ch in norm)
        for name, value in m.groupdict().items():
            value = (value or "").strip()
            if not value:
                continue
            idx = aligned.rfind(value)
            out[name] = norm[idx:idx + len(value)].strip() if idx >= 0 else value
        return out

    def could_match(self, command: str) -> bool:
        """Cheap pre-check (no IPC): is *command* possibly a catalog command?"""
        cmd = fold(command)
        if not cmd:
            return False
        cmd, _ = self._split_app(cmd)
        for v in self._variants(cmd):
            if v in self._phrases or any(rx.match(v) for rx, _s, _p in self._templates):
                return True
        return bool(KeyboardFirst._MENU_CMD.match(cmd))

    def by_name(self, name: str, ui: UIContext) -> Optional[Shortcut]:
        """'chromium.new_tab' or bare 'new_tab' (best applicable)."""
        name = name.strip()
        cands = [s for s in self.items if s.qualified == name] or [s for s in self.items if s.id == name]
        cands = [s for s in cands if self.applicable(s, ui)] or cands
        return max(cands, key=lambda s: s.specificity) if cands else None

    def tools_for(self, ui: UIContext, limit: int = 60) -> list[Shortcut]:
        """Single-chord shortcuts usable right now (app-specific first)."""
        out = [s for s in self.items if not s.is_macro and s.keys and s.risk != "critical"
               and self.applicable(s, ui)]
        out.sort(key=lambda s: (-s.specificity, s.group, s.id))
        seen, uniq = set(), []
        for s in out:
            if s.qualified not in seen:
                seen.add(s.qualified)
                uniq.append(s)
        return uniq[:limit]


_CATALOG: Optional[ShortcutCatalog] = None


def get_catalog() -> ShortcutCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = ShortcutCatalog()
    return _CATALOG


def chord_action(sc: Shortcut) -> tuple[str, dict[str, Any]]:
    """IPC action for a single-chord shortcut."""
    keys = sc.key_list()
    if len(keys) == 1 and keys[0] not in MODIFIERS:
        return "keyboard_press", {"key": keys[0]}
    return "keyboard_hotkey", {"keys": keys}


# ══════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════
class KeyboardFirst:
    """Resolve + execute keyboard/menu commands through the IPC client."""

    def __init__(self, ipc: Any, catalog: Optional[ShortcutCatalog] = None) -> None:
        self._ipc = ipc
        self.catalog = catalog or get_catalog()
        self._menu_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    # ── IPC helpers ───────────────────────────────────────────────
    async def _send(self, action: str, payload: dict[str, Any]) -> tuple[bool, Any, str]:
        try:
            resp = await self._ipc.send_action(action, payload)
        except Exception as exc:
            return False, None, str(exc)
        ok = bool(getattr(resp, "success", False))
        return ok, getattr(resp, "result", None), "" if ok else str(
            getattr(resp, "error", "") or getattr(resp, "result", "") or "failed")

    async def snapshot(self) -> UIContext:
        ok, result, _ = await self._send("ui_snapshot", {"include_value": False})
        return UIContext.from_snapshot(result if ok else None)

    # ── public API ────────────────────────────────────────────────
    async def try_command(self, command: str, *, max_risk: str = "high") -> Optional[KFResult]:
        """Run *command* if it is a known shortcut / macro / menu command."""
        if not self.catalog.could_match(command):
            return None                      # no IPC round-trip for ordinary goals
        ui = await self.snapshot()
        match = self.catalog.find(command, ui)
        if match is not None:
            if _RISK_ORDER.get(match.shortcut.risk, 0) > _RISK_ORDER.get(max_risk, 2):
                return KFResult(False, "shortcut", match.shortcut.qualified,
                                error=f"rủi ro {match.shortcut.risk} — cần xác nhận thủ công")
            return await self.execute(match, ui)
        return await self.try_menu_command(command, ui)

    async def execute(self, match: Match, ui: UIContext) -> KFResult:
        t0 = time.monotonic()
        sc = match.shortcut
        if sc.risk in ("high", "critical"):
            ok, _, err = await self._send("user_confirm", {
                "message": f"Phidipus Agents sắp thực hiện: {sc.desc} ({sc.pretty_keys() or sc.id})",
                "action_description": f"Phím tắt {sc.qualified}"[:256],
            })
            if not ok:
                return KFResult(False, "shortcut", sc.qualified, error=f"người dùng không xác nhận ({err[:60]})")
        if match.app_key:
            bundle = self.catalog.app_bundle(match.app_key)
            if bundle and bundle != ui.bundle_id:
                ok, _, err = await self._send("app_launch", {"app_name": self.catalog.app_name(match.app_key)})
                if not ok:
                    return KFResult(False, "shortcut", sc.qualified, error=f"không mở được app: {err[:80]}")
                await asyncio.sleep(0.8)
                ui = await self.snapshot()
        before = ui
        if sc.is_macro:
            res = await self._run_steps(sc, match.params)
        else:
            action, payload = chord_action(sc)
            ok, _, err = await self._send(action, payload)
            res = KFResult(ok, "shortcut", sc.qualified, error=err)
        if res.success and sc.expect in ("window", "app") and before.ok:
            await asyncio.sleep(0.35)
            after = await self.snapshot()
            res.verified = self._changed(sc.expect, before, after) if after.ok else None
        elif res.success and sc.expect == "clipboard" and res.output:
            res.verified = True
        if sc.menu and (not res.success or res.verified is False):
            for path in sc.menu:
                ok, _, err = await self._send("menu_select", {"path": path})
                if ok:
                    res = KFResult(True, "menu", " > ".join(path), verified=None)
                    break
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        icon = "⌨️ " if res.success else "⚠️ "
        _vlog(icon, f"Keyboard-first {res.method}: {res.name}"
                    + (f" ({sc.pretty_keys()})" if sc.keys and res.method == "shortcut" else "")
                    + ("" if res.verified is None else (" ✓" if res.verified else " (không thấy thay đổi)")))
        return res

    async def _run_steps(self, sc: Shortcut, params: dict[str, str]) -> KFResult:
        output = ""

        def fill(value: str) -> str:
            for k, v in params.items():
                value = value.replace("{" + k + "}", v)
            return value

        for step in sc.steps:
            if "wait" in step:
                await asyncio.sleep(min(3.0, float(step["wait"])))
                continue
            if "hotkey" in step:
                keys = [k for k in str(step["hotkey"]).lower().split("+") if k]
                action, payload = ("keyboard_hotkey", {"keys": keys}) if len(keys) > 1 else \
                    ("keyboard_press", {"key": keys[0]})
            elif "press" in step:
                action, payload = "keyboard_press", {"key": str(step["press"]).lower()}
            elif "type" in step:
                action, payload = "keyboard_type", {"text": fill(str(step["type"]))[:8192]}
            elif "app" in step:
                action, payload = "app_launch", {"app_name": self.catalog.app_name(str(step["app"]))}
            elif "menu" in step:
                paths = step["menu"] if step["menu"] and isinstance(step["menu"][0], list) else [step["menu"]]
                for path in paths:
                    ok, _, err = await self._send("menu_select", {"path": [str(x) for x in path]})
                    if ok:
                        break
                else:
                    return KFResult(False, "macro", sc.qualified, error=f"không thấy menu phù hợp ({err[:80]})")
                continue
            elif "read_clipboard" in step:
                await asyncio.sleep(0.15)
                ok, result, err = await self._send("clipboard_get", {})
                output = str(result or "") if ok else ""
                continue
            else:
                continue
            ok, _, err = await self._send(action, payload)
            if not ok:
                return KFResult(False, "macro", sc.qualified, error=f"{action}: {err[:120]}")
        return KFResult(True, "macro", sc.qualified, output=output)

    @staticmethod
    def _changed(expect: str, a: UIContext, b: UIContext) -> bool:
        if expect == "app":
            return a.bundle_id != b.bundle_id or a.window_count != b.window_count
        return (a.window_title != b.window_title or a.window_count != b.window_count
                or a.url != b.url or a.document != b.document or a.bundle_id != b.bundle_id
                or a.raw.get("window_frame") != b.raw.get("window_frame"))

    # ── menu commands ─────────────────────────────────────────────
    _MENU_CMD = re.compile(r"^(?:chon |bam |nhan |click |mo |vao )?menu\s+(.+)$")

    async def menu_items(self, ui: UIContext, ttl: float = 600.0) -> list[dict[str, Any]]:
        key = f"{ui.bundle_id}"
        hit = self._menu_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        ok, result, _ = await self._send("menu_list", {"max_items": 600})
        items = list((result or {}).get("items") or []) if ok and isinstance(result, dict) else []
        if items:
            self._menu_cache[key] = (time.time(), items)
        return items

    async def try_menu_command(self, command: str, ui: Optional[UIContext] = None) -> Optional[KFResult]:
        """'chọn menu File > Export as PDF' / 'menu Tệp xuất pdf' (explicit menu requests only)."""
        original = re.sub(r"\s+", " ", command.strip())
        m = self._MENU_CMD.match(fold(original))
        if not m:
            return None
        ui = ui or await self.snapshot()
        spec = original[-len(m.group(1)):] if len(original) >= len(m.group(1)) else m.group(1)
        parts = [p.strip() for p in re.split(r"\s*(?:>|›|▸|→|/)\s*", spec) if p.strip()]
        path: Optional[list[str]] = parts if len(parts) >= 2 else None
        if path is None:
            items = await self.menu_items(ui)
            want = fold(parts[0]) if parts else ""
            exact = [i for i in items if fold(i["path"][-1]) == want and i.get("enabled", True)]
            fuzzy = [i for i in items if want and want in fold(" ".join(i["path"])) and i.get("enabled", True)]
            pick = exact[0] if len(exact) == 1 else (fuzzy[0] if len(fuzzy) == 1 else None)
            if pick is None:
                return KFResult(False, "menu", parts[0] if parts else "",
                                error=f"không xác định được mục menu ({len(exact) or len(fuzzy)} kết quả)")
            path = list(pick["path"])
        t0 = time.monotonic()
        ok, _, err = await self._send("menu_select", {"path": path})
        return KFResult(ok, "menu", " > ".join(path), error=err, duration_ms=int((time.monotonic() - t0) * 1000))


_RUNNERS: dict[int, KeyboardFirst] = {}


def get_keyboard_first(ipc: Any) -> KeyboardFirst:
    key = id(ipc)
    if key not in _RUNNERS:
        _RUNNERS[key] = KeyboardFirst(ipc)
    return _RUNNERS[key]
