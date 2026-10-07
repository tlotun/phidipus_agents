#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
mcp/mcp_chrome_server.py — Expose Chrome Skills as MCP Tools
═══════════════════════════════════════════════════════════════

Runs as HTTP+SSE MCP server so external agents (Claude Desktop,
browser-use, n8n, etc.) can use Phidipus Chrome automation.

Usage:
    python mcp/mcp_chrome_server.py --port 3100
    
Then in Claude Desktop config:
    {
      "mcpServers": {
        "phidipus-chrome": {
          "type": "url",
          "url": "http://localhost:3100/sse"
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════
# Tool Definitions — Chrome Skills exposed as MCP tools
# ══════════════════════════════════════════════════════════

CHROME_TOOLS = [
    {
        "name": "navigate",
        "description": "Navigate browser to a URL",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to navigate to"}},
            "required": ["url"],
        },
    },
    {
        "name": "search_google",
        "description": "Search Google and return results (titles + URLs + snippets)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_page_text",
        "description": "Get text content of current page (cleaned, no HTML)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_length": {"type": "integer", "description": "Max text length", "default": 5000},
            },
        },
    },
    {
        "name": "get_page_title",
        "description": "Get title of current page",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_current_url",
        "description": "Get URL of current page",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "click_element",
        "description": "Click an element on the page by text, CSS selector, or XPath",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector, XPath, or visible text"},
                "method": {"type": "string", "enum": ["text", "css", "xpath"], "default": "text"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "fill_form",
        "description": "Fill a form field with text",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector or label text"},
                "value": {"type": "string", "description": "Text to fill"},
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "extract_links",
        "description": "Extract all links from current page",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter_domain": {"type": "string", "description": "Filter by domain (optional)"},
            },
        },
    },
    {
        "name": "take_screenshot",
        "description": "Take a screenshot of current browser tab",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_path": {"type": "string", "description": "Save path (optional)"},
            },
        },
    },
    {
        "name": "scroll_page",
        "description": "Scroll the page up or down",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["down", "up"], "default": "down"},
                "amount": {"type": "integer", "description": "Pixels to scroll", "default": 500},
            },
        },
    },
    {
        "name": "open_new_tab",
        "description": "Open a new browser tab",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL (optional)"}},
        },
    },
    {
        "name": "run_javascript",
        "description": "Execute JavaScript code in the browser",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "JavaScript code"}},
            "required": ["code"],
        },
    },
    {
        "name": "read_table",
        "description": "Read an HTML table from the page as structured data",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_index": {"type": "integer", "description": "Table index (0-based)", "default": 0},
            },
        },
    },
]


# ══════════════════════════════════════════════════════════
# Tool Execution — Bridge to ChromeSkills
# ══════════════════════════════════════════════════════════

_chrome_skills = None

def _get_chrome():
    global _chrome_skills
    if _chrome_skills is None:
        try:
            from skills.apps.chrome_skills import ChromeSkills
            _chrome_skills = ChromeSkills()
        except ImportError:
            pass
    return _chrome_skills


async def execute_tool(name: str, arguments: dict) -> dict:
    """Execute a Chrome tool and return MCP-formatted result."""
    chrome = _get_chrome()
    if not chrome:
        return {"isError": True, "content": [{"type": "text", "text": "Chrome skills not available"}]}

    try:
        result = None
        if name == "navigate":
            result = await chrome.navigate(arguments["url"])
        elif name == "search_google":
            result = await chrome.search_google(arguments["query"], max_results=arguments.get("num_results", 5))
        elif name == "get_page_text":
            result = await chrome.get_page_text(max_length=arguments.get("max_length", 5000))
        elif name == "get_page_title":
            result = await chrome.get_page_title()
        elif name == "get_current_url":
            result = await chrome.get_current_url()
        elif name == "click_element":
            result = await chrome.click_element(arguments["selector"])
        elif name == "fill_form":
            result = await chrome.fill_form(arguments)
        elif name == "extract_links":
            result = await chrome.extract_links(arguments.get("filter_domain", ""))
        elif name == "take_screenshot":
            result = await chrome.take_screenshot(arguments.get("output_path", ""))
        elif name == "scroll_page":
            result = await chrome.scroll_page(arguments.get("direction", "down"), arguments.get("amount", 500))
        elif name == "open_new_tab":
            result = await chrome.open_new_tab(arguments.get("url", ""))
        elif name == "run_javascript":
            result = await chrome.run_js(arguments["code"])
        elif name == "read_table":
            result = await chrome.read_page_table(arguments.get("table_index", 0))
        else:
            return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}

        # Convert ChromeResult to MCP format
        if hasattr(result, "data"):
            text = json.dumps(result.data, ensure_ascii=False, default=str) if isinstance(result.data, (dict, list)) else str(result.data)
        else:
            text = str(result)

        return {"content": [{"type": "text", "text": text}]}

    except Exception as e:
        return {"isError": True, "content": [{"type": "text", "text": f"Error: {str(e)}"}]}


# ══════════════════════════════════════════════════════════
# HTTP+SSE Server
# ══════════════════════════════════════════════════════════

def create_mcp_app():
    """Create FastAPI app that serves MCP protocol over HTTP+SSE."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, StreamingResponse
    except ImportError:
        print("FastAPI required: pip install fastapi uvicorn")
        sys.exit(1)

    app = FastAPI(title="Phidipus Chrome MCP Server")
    sessions = {}

    @app.get("/sse")
    async def sse_endpoint(request: Request):
        """SSE endpoint — client connects here first."""
        session_id = str(uuid.uuid4())
        sessions[session_id] = {"created": datetime.now().isoformat()}

        async def event_stream():
            # Send endpoint event
            yield f"event: endpoint\ndata: /message?session={session_id}\n\n"
            # Keep alive
            try:
                while True:
                    await asyncio.sleep(30)
                    yield ": keepalive\n\n"
            except asyncio.CancelledError:
                sessions.pop(session_id, None)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/message")
    async def message_endpoint(request: Request, session: str = ""):
        """Handle JSON-RPC messages."""
        body = await request.json()
        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        result = None

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "phidipus-chrome", "version": "3.0"},
            }
        elif method == "notifications/initialized":
            return JSONResponse({"jsonrpc": "2.0"})
        elif method == "tools/list":
            result = {"tools": CHROME_TOOLS}
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = await execute_tool(tool_name, arguments)
        elif method == "resources/list":
            result = {"resources": []}
        else:
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })

        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})

    @app.get("/health")
    async def health():
        return {"status": "ok", "tools": len(CHROME_TOOLS), "server": "phidipus-chrome-mcp"}

    return app


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phidipus Chrome MCP Server")
    parser.add_argument("--port", type=int, default=3100)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"\n🕷️ Phidipus Chrome MCP Server")
    print(f"   SSE endpoint: http://{args.host}:{args.port}/sse")
    print(f"   Tools: {len(CHROME_TOOLS)}")
    print(f"\n   Add to Claude Desktop:")
    print(f'   {{"mcpServers": {{"phidipus": {{"type": "url", "url": "http://{args.host}:{args.port}/sse"}}}}}}')
    print()

    import uvicorn
    uvicorn.run(create_mcp_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
