# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/mcp_server.py — Phidipus Memory as an MCP server (stdio) — v4.3
═══════════════════════════════════════════════════════════════════════

Lets any MCP client (Claude Desktop, Claude Code, Cursor, …) read and write
Phidipus' long-term memory.  Pure standard library — no `mcp` package needed.

Run manually:
    ./venv/bin/python -m memory.mcp_server

Claude Desktop (claude_desktop_config.json):
    {
      "mcpServers": {
        "phidipus-agents-memory": {
          "command": "/ABSOLUTE/PATH/phidipus/venv/bin/python",
          "args": ["-m", "memory.mcp_server"],
          "cwd": "/ABSOLUTE/PATH/phidipus"
        }
      }
    }

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout.  Everything else
(logs, warnings) goes to stderr so the protocol stream stays clean.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SERVER_INFO = {"name": "phidipus-agents-memory", "version": "1.0.0"}
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": "Search Phidipus Agents long-term memory (facts, preferences, profile, learned procedures, "
                       "past task outcomes). Hybrid keyword + semantic search; Vietnamese with or without accents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for"},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "include_history": {"type": "boolean", "default": False,
                                    "description": "Also return superseded (outdated) memories"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_add",
        "description": "Store a durable fact or preference about the user. Duplicates are merged and changed "
                       "facts supersede old ones (history kept). Secrets (keys, passwords, OTP) are refused.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "One self-contained statement"},
                "kind": {"type": "string", "enum": ["fact", "preference", "profile"],
                         "description": "Optional; auto-detected when omitted"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_forget",
        "description": "Delete memories by id or by a query whose every word must appear in the memory.",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Memory id or keywords"}},
            "required": ["target"],
        },
    },
    {
        "name": "memory_profile",
        "description": "Return the always-in-context core memory blocks (user profile, preferences).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_stats",
        "description": "Counts of stored memories by kind, learned procedures and storage info.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _agent():
    from memory.memory_agent import get_memory_agent
    agent = get_memory_agent()
    if agent is None:
        raise RuntimeError("Memory Agent is disabled (memory_agent.enabled: false in config.yaml)")
    return agent


def _fmt_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or 0))


async def call_tool(name: str, args: dict[str, Any]) -> tuple[str, bool, Any]:
    """Returns (text, is_error, structured)."""
    agent = _agent()
    if name == "memory_search":
        query = str(args.get("query", "")).strip()
        if not query:
            return "query is required", True, None
        k = max(1, min(20, int(args.get("k", 5) or 5)))
        hits = await agent.recall(query, k=k, include_history=bool(args.get("include_history")))
        if not hits:
            return "No relevant memories.", False, {"results": []}
        lines = [f"- [{_fmt_day(h.updated_at)}] ({h.kind}{'' if h.is_valid else ', outdated'}) "
                 f"{h.content}  [id {h.id}]" for h in hits]
        return "\n".join(lines), False, {"results": [
            {"id": h.id, "content": h.content, "kind": h.kind, "score": h.score,
             "updated": _fmt_day(h.updated_at), "valid": h.is_valid} for h in hits]}
    if name == "memory_add":
        content = str(args.get("content", "")).strip()
        if not content:
            return "content is required", True, None
        kind = args.get("kind") if args.get("kind") in ("fact", "preference", "profile") else None
        res = await agent.remember(content, kind=kind, source="mcp")
        is_err = res.get("op") == "REJECT"
        return json.dumps(res, ensure_ascii=False), is_err, res
    if name == "memory_forget":
        target = str(args.get("target", "")).strip()
        if not target:
            return "target is required", True, None
        res = await agent.forget(target)
        return json.dumps(res, ensure_ascii=False), False, res
    if name == "memory_profile":
        blocks = agent.blocks()
        text = "\n\n".join(f"## {k}\n{v}" for k, v in blocks.items() if v) or "No profile yet."
        return text, False, blocks
    if name == "memory_stats":
        st = agent.stats()
        return json.dumps(st, ensure_ascii=False), False, st
    return f"Unknown tool: {name}", True, None


async def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; returns the response (None for notifications)."""
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if is_notification:
        return None   # notifications/initialized, notifications/cancelled, …
    if method == "initialize":
        requested = str(params.get("protocolVersion", ""))
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "Phidipus Agents long-term memory. Use memory_search before answering questions "
                            "about the user; store durable facts with memory_add.",
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name", ""))
        if name not in {t["name"] for t in TOOLS}:
            return err(-32602, f"Unknown tool: {name}")
        try:
            text, is_error, structured = await call_tool(name, params.get("arguments") or {})
        except Exception as exc:
            text, is_error, structured = f"Error: {exc}", True, None
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if isinstance(structured, dict):
            result["structuredContent"] = structured
        return ok(result)
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, f"Method not found: {method}")


def main() -> None:
    out = sys.stdout
    sys.stdout = sys.stderr          # any stray print() must not corrupt the protocol
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                           "error": {"code": -32700, "message": "Parse error"}}
        else:
            batch = msg if isinstance(msg, list) else [msg]
            responses = [r for r in (loop.run_until_complete(handle(m)) for m in batch
                                     if isinstance(m, dict)) if r is not None]
            resp = (responses if isinstance(msg, list) else (responses[0] if responses else None))
        if resp:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
