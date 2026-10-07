# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/workflow_nodes_ext.py — Phidipus v4.3 workflow node extensions
═══════════════════════════════════════════════════════════════════════

Why this module exists
----------------------
Brain v7 (fine-tuned Qwen3.5-4B) was trained to emit 25 node types such as
``chrome_navigate``, ``notify_telegram``, ``terminal_run``, ``file_write``,
``loop``, ``store_variable``, ``email_send``, ``db_query`` … but
WorkflowExecutor only implemented the "taught workflow" vocabulary
(``chrome``, ``notify``, ``terminal``, ``file`` …).  Every Brain workflow
therefore failed at its first node with "[EXEC-02] Node type không được nhận
diện" (confirmed by data/plans/trajectory_memory.json: 3/3 runs failed at
``chrome_navigate``).

This module provides:
  * BRAIN_NODE_ALIASES + normalize_brain_node()   — vocabulary bridge
  * builtin_variables() / find_placeholders()     — {{date}}, {{today}} …
  * file_operation()                               — direct, safe file ops
  * email_send() / email_read()                    — Gmail (Chrome UI) nodes
  * db_query()                                     — read-only SQLite queries
  * evaluate_expression()                          — Brain-style conditions
  * parse_schedule() / register_scheduled_workflow() — "scheduler" node
  * resolve_workflow() / list_workflows()          — Brain WF names → files
"""
from __future__ import annotations

import ast
import glob as _glob
import json
import operator
import re
import shutil
import sqlite3
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = _ROOT / "data" / "workflows"

# ══════════════════════════════════════════════════════════════════
# 1. Brain node vocabulary bridge
# ══════════════════════════════════════════════════════════════════

# brain type → (executor type, fixed config overrides)
BRAIN_NODE_ALIASES: dict[str, tuple[str, dict[str, Any]]] = {
    "chrome_navigate": ("chrome", {"action": "navigate"}),
    "chrome_get_text": ("chrome", {"action": "get_text"}),
    "notify_telegram": ("notify", {}),
    "terminal_run":    ("terminal", {}),
    "ocr_read":        ("vision_read", {"data_type": "text"}),
}

# Config key synonyms the Brain (or humans) commonly use → canonical keys
_KEY_SYNONYMS: dict[str, dict[str, str]] = {
    "chrome":   {"link": "url", "href": "url", "page": "url"},
    "notify":   {"text": "message", "content": "message", "msg": "message"},
    "terminal": {"cmd": "command", "script": "command", "shell": "command", "dir": "cwd"},
    "vision_read": {"region": "region_description", "target": "region_description",
                    "description": "region_description"},
    "file_write": {"file": "path", "file_path": "path", "filename": "path",
                   "text": "content", "data": "content"},
    "file_read":  {"file": "path", "file_path": "path", "filename": "path"},
    "file_copy":  {"source": "path", "src": "path", "from": "path",
                   "dest": "destination", "target": "destination", "to": "destination"},
    "email_send": {"recipient": "to", "email": "to", "title": "subject",
                   "content": "body", "message": "body", "text": "body"},
    "db_query":   {"database": "db_path", "db": "db_path", "sql": "query"},
    "store_variable": {"key": "name", "variable": "name", "var": "name", "data": "value"},
    "get_variable":   {"key": "name", "variable": "name", "var": "name"},
}

# Node types implemented in this module (dispatched by WorkflowExecutor)
EXT_NODE_TYPES = frozenset({
    "file_write", "file_read", "file_copy", "email_send", "email_read",
    "db_query", "store_variable", "get_variable", "scheduler", "loop",
    # v4.3 keyboard-first nodes (no vision): catalog shortcut, menu path, macro
    "shortcut", "menu", "macro",
})


def _apply_synonyms(ntype: str, cfg: dict[str, Any]) -> dict[str, Any]:
    syn = _KEY_SYNONYMS.get(ntype)
    if not syn:
        return dict(cfg)
    out = dict(cfg)
    for alt, canon in syn.items():
        if alt in out and canon not in out:
            out[canon] = out.pop(alt)
    return out


def normalize_brain_node(ntype: str, cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a Brain node type/config onto what the executor understands."""
    cfg = dict(cfg or {})
    if ntype in BRAIN_NODE_ALIASES:
        target, fixed = BRAIN_NODE_ALIASES[ntype]
        cfg = _apply_synonyms(target, cfg)
        if ntype == "ocr_read":
            cfg = _apply_synonyms("vision_read", cfg)
        merged = {**cfg, **{k: v for k, v in fixed.items() if k not in cfg or k == "action"}}
        if target == "notify" and not merged.get("message"):
            merged["message"] = "{{result}}"
        return target, merged
    return ntype, _apply_synonyms(ntype, cfg)


# ══════════════════════════════════════════════════════════════════
# 2. Built-in variables & placeholder analysis
# ══════════════════════════════════════════════════════════════════

_WEEKDAYS_VI = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
DYNAMIC_VARS = frozenset({"result", "prev_result", "goal", "loop_item", "loop_index",
                          "query", "prompt", "topic"})


def builtin_variables() -> dict[str, str]:
    now = datetime.now()
    return {
        "date":      now.strftime("%Y-%m-%d"),
        "today":     now.strftime("%Y-%m-%d"),
        "date_vi":   now.strftime("%d/%m/%Y"),
        "date_compact": now.strftime("%Y%m%d"),
        "time":      now.strftime("%H:%M"),
        "datetime":  now.strftime("%Y-%m-%d %H:%M"),
        "weekday":   _WEEKDAYS_VI[now.weekday()],
        "timestamp": str(int(time.time())),
        "home":      str(Path.home()),
    }


def find_placeholders(obj: Any) -> set[str]:
    """All {{name}} placeholders inside nested config structures."""
    found: set[str] = set()
    if isinstance(obj, str):
        found.update(_PLACEHOLDER_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= find_placeholders(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= find_placeholders(v)
    return found


def missing_parameters(workflow: dict[str, Any], variables: dict[str, Any]) -> list[str]:
    """Placeholders that no variable/default/builtin can fill (excluding dynamic ones)."""
    needed: set[str] = set()
    for node in workflow.get("nodes", []):
        if node.get("type") == "trigger":
            continue
        needed |= find_placeholders(node.get("config", {}))
    # variables produced by earlier nodes (store_as / store_variable) are fine
    produced: set[str] = set()
    for node in workflow.get("nodes", []):
        cfg = node.get("config", {}) or {}
        for key in ("store_as", "name"):
            if isinstance(cfg.get(key), str) and cfg.get(key):
                produced.add(cfg[key])
    known = set(variables) | DYNAMIC_VARS | produced | set(builtin_variables())
    return sorted(n for n in needed if n not in known)


# ══════════════════════════════════════════════════════════════════
# 3. Safe file operations
# ══════════════════════════════════════════════════════════════════

def _safe_path(path: str) -> Path:
    from utils.sanitizer import restrict_path_to_safe_roots
    return restrict_path_to_safe_roots(str(Path(path).expanduser()))


def _patterns(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("pattern", "*") or "*"
    if isinstance(raw, list):
        return [str(p).strip() for p in raw if str(p).strip()] or ["*"]
    return [p.strip() for p in str(raw).split(",") if p.strip()] or ["*"]


def _iter_matches(base: Path, patterns: list[str], recursive: bool, limit: int) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        glob_pat = str(base / ("**" if recursive else "") / pat) if recursive else str(base / pat)
        for p in _glob.iglob(glob_pat, recursive=recursive):
            pp = Path(p)
            if pp.is_file() and pp not in out:
                out.append(pp)
            if len(out) >= limit:
                return out
    return out


def _read_file_text(p: Path, max_chars: int = 20000) -> str:
    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # type: ignore
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            ws = wb.worksheets[0]
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                rows.append(" | ".join("" if v is None else str(v) for v in row))
                if i >= 300:
                    break
            return "\n".join(rows)[:max_chars]
        except Exception as exc:
            return f"(không đọc được Excel: {exc})"
    if suffix == ".docx":
        try:
            import docx  # type: ignore
            return "\n".join(par.text for par in docx.Document(str(p)).paragraphs)[:max_chars]
        except Exception as exc:
            return f"(không đọc được Word: {exc})"
    if suffix == ".pdf":
        try:
            from utils.atomic_tools import read_pdf  # type: ignore
            return str(read_pdf(str(p)))[:max_chars]
        except Exception as exc:
            return f"(không đọc được PDF: {exc})"
    return p.read_text(encoding="utf-8", errors="replace")[:max_chars]


def file_operation(action: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a file action inside the allowed roots (~/Desktop, ~/Documents,
    ~/Downloads, ~/Phidipus, ~/Pictures, /tmp/phidipus_output).

    Actions: find | list | read | write | copy | move | delete (→ Trash) |
             zip | create_folder
    """
    action = (action or "find").lower()
    try:
        path = _safe_path(cfg.get("path") or "~/Desktop")
    except PermissionError as exc:
        return {"success": False, "error": f"[SEC] {exc}"}
    dest_raw = cfg.get("destination", "")
    dest: Path | None = None
    if dest_raw:
        try:
            dest = _safe_path(dest_raw)
        except PermissionError as exc:
            return {"success": False, "error": f"[SEC] destination: {exc}"}

    limit = max(1, min(2000, int(cfg.get("max_files", 500))))
    recursive = bool(cfg.get("recursive", True))
    try:
        if action in ("find", "search"):
            base = path if path.is_dir() else path.parent
            files = _iter_matches(base, _patterns(cfg), recursive, limit)
            listing = "\n".join(str(f) for f in files)
            return {"success": True, "result": listing or "(không có file phù hợp)",
                    "files": [str(f) for f in files], "count": len(files)}

        if action == "list":
            if not path.is_dir():
                return {"success": False, "error": f"Không phải thư mục: {path}"}
            items = sorted(path.iterdir())[:limit]
            lines = [f"{'📁' if i.is_dir() else '📄'} {i.name}" for i in items]
            return {"success": True, "result": "\n".join(lines), "count": len(items)}

        if action == "read":
            if not path.is_file():
                return {"success": False, "error": f"Không tìm thấy file: {path}"}
            return {"success": True, "result": _read_file_text(path, int(cfg.get("max_chars", 20000)))}

        if action == "write":
            content = cfg.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if str(cfg.get("mode", "overwrite")).lower() in ("append", "a") else "w"
            with open(path, mode, encoding="utf-8") as fh:
                fh.write(content)
            return {"success": True, "result": f"Đã ghi {len(content)} ký tự vào {path}",
                    "file_path": str(path)}

        if action in ("copy", "move"):
            if dest is None:
                return {"success": False, "error": f"{action}: thiếu destination"}
            sources = [path] if path.is_file() else _iter_matches(path, _patterns(cfg), recursive, limit)
            if not sources:
                return {"success": False, "error": f"{action}: không có file nguồn trong {path}"}
            dest.mkdir(parents=True, exist_ok=True) if (len(sources) > 1 or not dest.suffix) else dest.parent.mkdir(parents=True, exist_ok=True)
            done = 0
            for src in sources:
                target = dest / src.name if dest.is_dir() or len(sources) > 1 else dest
                if action == "copy":
                    shutil.copy2(src, target)
                else:
                    shutil.move(str(src), str(target))
                done += 1
            verb = "Đã copy" if action == "copy" else "Đã di chuyển"
            return {"success": True, "result": f"{verb} {done} file → {dest}", "count": done}

        if action == "delete":
            trash = Path.home() / ".Trash"
            trash.mkdir(exist_ok=True)
            targets = [path] if path.exists() and (path.is_file() or not cfg.get("pattern")) \
                else _iter_matches(path, _patterns(cfg), recursive, limit)
            moved = 0
            for t in targets:
                if not t.exists():
                    continue
                target = trash / f"{t.stem}_{int(time.time())}{t.suffix}"
                shutil.move(str(t), str(target))
                moved += 1
            return {"success": True, "result": f"Đã chuyển {moved} mục vào Thùng rác (có thể khôi phục)",
                    "count": moved}

        if action == "zip":
            if not path.exists():
                return {"success": False, "error": f"zip: không tồn tại {path}"}
            max_files = int(cfg.get("max_files", 1000))
            max_mb = int(cfg.get("max_mb", 500))
            if path.is_dir():
                files = [f for f in path.rglob("*") if f.is_file()]
                total_mb = sum(f.stat().st_size for f in files) / 1048576
                if len(files) > max_files or total_mb > max_mb:
                    return {"success": False,
                            "error": f"zip bị chặn: {len(files)} file / {total_mb:.0f} MB vượt giới hạn"}
            out_base = (dest or path.parent / path.name)
            archive = shutil.make_archive(str(out_base.with_suffix("")), "zip",
                                          root_dir=str(path.parent), base_dir=path.name)
            return {"success": True, "result": f"Đã nén → {archive}", "file_path": archive}

        if action in ("create_folder", "mkdir"):
            path.mkdir(parents=True, exist_ok=True)
            return {"success": True, "result": f"Đã tạo thư mục {path}"}

        return {"success": False, "error": f"file action không hỗ trợ: {action}"}
    except Exception as exc:
        return {"success": False, "error": f"file {action} lỗi: {exc}"}


# ══════════════════════════════════════════════════════════════════
# 4. Email nodes (Gmail through Chrome — no API key needed)
# ══════════════════════════════════════════════════════════════════

async def email_send(ipc_client: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    to = cfg.get("to", "")
    if isinstance(to, str):
        to = [a.strip() for a in re.split(r"[,;]", to) if a.strip()]
    subject = str(cfg.get("subject", "")).strip()
    body = str(cfg.get("body", "")).strip()
    if not to or not subject:
        return {"success": False, "error": "email_send cần 'to' và 'subject'"}
    try:
        from skills.apps.gmail_skills import GmailSkills
        r = await GmailSkills(ipc_client=ipc_client).send_email(
            to=to, subject=subject, body=body, attachments=cfg.get("attachments") or None)
        return {"success": r.success, "result": r.summary, "error": r.error}
    except Exception as exc:
        return {"success": False, "error": f"email_send lỗi: {exc}"}


async def email_read(ipc_client: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        from skills.apps.gmail_skills import GmailSkills
        g = GmailSkills(ipc_client=ipc_client)
        query = str(cfg.get("query") or cfg.get("filter") or cfg.get("from") or "").strip()
        limit = int(cfg.get("max", cfg.get("limit", 10)))
        if query:
            r = await g.search_emails(query, max=limit)
        else:
            r = await g.read_emails(max=limit, unread_only=bool(cfg.get("unread_only", True)))
        if not r.success:
            return {"success": False, "error": r.error or "email_read thất bại"}
        out = r.output
        text = json.dumps(out, ensure_ascii=False, indent=1) if isinstance(out, (list, dict)) else str(out)
        return {"success": True, "result": text, "emails": out}
    except Exception as exc:
        return {"success": False, "error": f"email_read lỗi: {exc}"}


# ══════════════════════════════════════════════════════════════════
# 5. Read-only SQLite query
# ══════════════════════════════════════════════════════════════════

_READONLY_SQL = re.compile(r"^\s*(select|with|pragma\s+table_info|explain)\b", re.I)


def db_query(cfg: dict[str, Any]) -> dict[str, Any]:
    db_path = cfg.get("db_path", "")
    query = str(cfg.get("query", "")).strip()
    if not db_path or not query:
        return {"success": False, "error": "db_query cần 'db_path' và 'query'"}
    if not _READONLY_SQL.match(query) or ";" in query.rstrip(";"):
        return {"success": False, "error": "db_query chỉ cho phép 1 câu SELECT/WITH (read-only)"}
    candidate = Path(db_path).expanduser()
    if not candidate.is_absolute():
        candidate = _ROOT / candidate
    resolved = candidate.resolve()
    allowed_data = (_ROOT / "data").resolve()
    try:
        if not str(resolved).startswith(str(allowed_data)):
            resolved = _safe_path(str(resolved))
    except PermissionError as exc:
        return {"success": False, "error": f"[SEC] {exc}"}
    if not resolved.exists():
        return {"success": False, "error": f"Không tìm thấy database: {resolved}"}
    try:
        con = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.execute(query, tuple(cfg.get("params", []) or ()))
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(int(cfg.get("max_rows", 200)))
        finally:
            con.close()
        data = [dict(zip(cols, r)) for r in rows]
        return {"success": True, "result": json.dumps(data, ensure_ascii=False, default=str),
                "rows": data, "row_count": len(data)}
    except Exception as exc:
        return {"success": False, "error": f"db_query lỗi: {exc}"}


# ══════════════════════════════════════════════════════════════════
# 6. Expression evaluation for Brain-style condition nodes
# ══════════════════════════════════════════════════════════════════

_CMP_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le,
}


def _to_number(v: Any) -> Any:
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(" ", "")
    digits = re.sub(r"[^\d.,\-]", "", s)
    if not digits:
        return v
    # Vietnamese thousands separators: 1.250.000 → 1250000
    if digits.count(".") > 1 or (digits.count(".") == 1 and len(digits.split(".")[-1]) == 3 and "," not in digits):
        digits = digits.replace(".", "")
    digits = digits.replace(",", ".") if digits.count(",") == 1 and "." not in digits else digits.replace(",", "")
    try:
        return float(digits)
    except ValueError:
        return v


def evaluate_expression(expr: str) -> bool | None:
    """
    Evaluate a *simple* comparison safely (no eval of arbitrary code).
    Supports: a == b, a != b, a > b, a >= b, a < b, a <= b,
              'x' in 'text', text contains x, and/or/not, true/false/yes/no.
    Returns None when the expression cannot be interpreted.
    """
    text = (expr or "").strip()
    if not text:
        return None
    low = text.lower()
    if low in ("true", "yes", "có", "1", "đúng"):
        return True
    if low in ("false", "no", "không", "0", "sai", ""):
        return False
    m = re.match(r"^(.*?)\s+(?:contains|chứa)\s+(.*)$", text, re.I)
    if m:
        return m.group(2).strip().strip("'\"").lower() in m.group(1).strip().strip("'\"").lower()
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        # bare words like: price > 1.250.000đ → normalise numbers
        cleaned = re.sub(r"(\d[\d.,]*)\s*(?:đ|vnd|usd|\$)?", lambda mm: str(_to_number(mm.group(1))), text, flags=re.I)
        try:
            tree = ast.parse(cleaned, mode="eval")
        except SyntaxError:
            return None

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return {"true": True, "false": False, "none": None}.get(node.id.lower(), node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not ev(node.operand)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_to_number(ev(node.operand))
        if isinstance(node, ast.BoolOp):
            vals = [ev(v) for v in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                if isinstance(op, ast.In):
                    ok = str(left).lower() in str(right).lower()
                elif isinstance(op, ast.NotIn):
                    ok = str(left).lower() not in str(right).lower()
                elif type(op) in _CMP_OPS:
                    a, b = _to_number(left), _to_number(right)
                    if isinstance(a, float) != isinstance(b, float):
                        a, b = str(left), str(right)
                    ok = _CMP_OPS[type(op)](a, b)
                else:
                    raise ValueError("unsupported operator")
                if not ok:
                    return False
                left = right
            return True
        raise ValueError(f"unsupported expression node {type(node).__name__}")

    try:
        return bool(ev(tree))
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# 7. Scheduler node support
# ══════════════════════════════════════════════════════════════════

def parse_schedule(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """
    Accepts {"interval_minutes": 60} | {"cron": "0 9 * * 1-5"} |
            {"time": "09:00", "days": [1,2,3,4,5]} | {"every": "day", "at": "08:30"}.
    Returns a scheduler_v2-compatible dict (time + days or interval_minutes).
    Day numbering follows scheduler_v2: 0=Sun, 1=Mon … 6=Sat.
    """
    if cfg.get("interval_minutes"):
        return {"interval_minutes": max(5, int(cfg["interval_minutes"]))}
    cron = str(cfg.get("cron", "")).strip()
    if cron:
        parts = cron.split()
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
            days = list(range(7))
            dow = parts[4]
            if dow != "*":
                days = []
                for seg in dow.split(","):
                    if "-" in seg:
                        a, b = seg.split("-", 1)
                        lo, hi = int(a), int(b)
                        days.extend(d % 7 for d in range(lo, max(lo, hi) + 1))
                    elif seg.isdigit():
                        days.append(int(seg) % 7)
            return {"time": f"{int(parts[1]):02d}:{int(parts[0]):02d}", "days": sorted(set(days))}
        return None
    at = str(cfg.get("time") or cfg.get("at") or "").strip()
    if re.match(r"^\d{1,2}:\d{2}$", at):
        h, m = at.split(":")
        days = cfg.get("days") or list(range(7))
        if str(cfg.get("every", "")).lower() in ("weekday", "weekdays", "thứ 2-6"):
            days = [1, 2, 3, 4, 5]
        return {"time": f"{int(h):02d}:{int(m):02d}", "days": [int(d) % 7 for d in days]}
    return None


def register_scheduled_workflow(workflow: dict[str, Any], schedule: dict[str, Any],
                                goal: str = "") -> dict[str, Any]:
    """Save the workflow (if needed) and add/refresh a scheduler_v2 entry."""
    from core.scheduler_v2 import save_schedule
    sched_dir = WORKFLOWS_DIR / "scheduled"
    sched_dir.mkdir(parents=True, exist_ok=True)
    name = workflow.get("name") or "brain_workflow"
    slug = re.sub(r"[^a-z0-9]+", "_", _ascii(name).lower()).strip("_")[:40] or "workflow"
    wf_file = sched_dir / f"{slug}.json"
    to_save = dict(workflow)
    to_save.setdefault("id", f"wf_sched_{slug}")
    to_save["active"] = False  # not a trigger-phrase workflow; only run by scheduler
    wf_file.write_text(json.dumps(to_save, ensure_ascii=False, indent=2), encoding="utf-8")
    entry = {
        "id": f"sched_{slug}",
        "name": name,
        "source": "workflow",
        "workflow_file": str(wf_file.relative_to(_ROOT)),
        "workflow_id": to_save["id"],
        "goal": goal[:300],
        "enabled": True,
        "notify": True,
        "created_at": time.time(),
        **schedule,
    }
    save_schedule(entry)
    return entry


# ══════════════════════════════════════════════════════════════════
# 8. Workflow resolution (Brain names ↔ data/workflows files)
# ══════════════════════════════════════════════════════════════════

# Brain v7's 15 workflow names → workflow ids in data/workflows/*.json
LEGACY_BRAIN_WORKFLOWS: dict[str, str] = {
    "bao_cao_doanh_so":     "wf_sale_daily",
    "theo_doi_gia_doi_thu": "wf_price_monitor",
    "email_follow_up":      "wf_email_followup",
    "kpi_dashboard":        "wf_kpi_dashboard",
    "dang_bai_linkedin":    "wf_linkedin_post",
    "tim_lead_google":      "wf_find_leads",
    "check_email":          "wf_email_summary",
    "tao_bao_gia":          "wf_quotation",
    "dat_lich_hop":         "wf_schedule_meeting",
    "backup_file":          "wf_weekly_backup",
    "kiem_tra_ton_kho":     "wf_inventory_check",
    "cham_soc_messenger":   "wf_messenger_care",
    "tong_hop_tin_tuc":     "wf_news_digest",
    "xuat_crm":             "wf_crm_export",
    "nghien_cuu_doi_thu":   "wf_competitor_research",
}


def _ascii(text: str) -> str:
    t = unicodedata.normalize("NFD", str(text)).replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _norm_name(text: str) -> str:
    t = _ascii(text).lower()
    t = re.sub(r"^run_workflow:", "", t)
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return re.sub(r"^\d+_", "", t)


def list_workflows(include_inactive: bool = True) -> list[tuple[Path, dict[str, Any]]]:
    out: list[tuple[Path, dict[str, Any]]] = []
    if not WORKFLOWS_DIR.exists():
        return out
    for f in sorted(WORKFLOWS_DIR.rglob("*.json")):
        try:
            wf = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(wf, dict) or not wf.get("nodes"):
            continue
        if not include_inactive and not wf.get("active", True):
            continue
        out.append((f, wf))
    return out


def resolve_workflow(name: str) -> dict[str, Any] | None:
    """Find a workflow by id, brain_name/aliases, file stem or display name."""
    if not name:
        return None
    key = _norm_name(name)
    target_id = LEGACY_BRAIN_WORKFLOWS.get(key, "")
    for f, wf in list_workflows():
        aliases = {_norm_name(a) for a in (wf.get("aliases") or [])}
        if wf.get("brain_name"):
            aliases.add(_norm_name(wf["brain_name"]))
        candidates = {
            _norm_name(wf.get("id", "")),
            _norm_name(f.stem),
            _norm_name(wf.get("name", "")),
        } | aliases
        if key in candidates or (target_id and wf.get("id") == target_id):
            return wf
    return None
