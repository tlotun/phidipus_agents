#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
mcp/mcp_client.py — MCP Client for Phidipus
═══════════════════════════════════════════════════════

Connects to external MCP servers via HTTP+SSE or stdio transport.
Supports: tool discovery, tool invocation, resource listing.

Usage:
    client = MCPClient("https://mcp.slack.com/sse", name="slack")
    await client.connect()
    tools = await client.list_tools()
    result = await client.call_tool("send_message", {"channel": "#general", "text": "Hello"})
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# ══════════════════════════════════════════════════════════
# Data Types
# ══════════════════════════════════════════════════════════

@dataclass
class MCPTool:
    """Represents a tool exposed by an MCP server."""
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    server_name: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "server_name": self.server_name,
        }


@dataclass
class MCPResource:
    """Represents a resource exposed by an MCP server."""
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""

    def to_dict(self) -> dict:
        return {"uri": self.uri, "name": self.name, "description": self.description}


@dataclass
class MCPResult:
    """Result from an MCP tool call."""
    success: bool
    content: Any = None
    error: str = ""
    is_error: bool = False

    def to_dict(self) -> dict:
        return {"success": self.success, "content": self.content, "error": self.error}


# ══════════════════════════════════════════════════════════
# MCP Client — HTTP+SSE Transport
# ══════════════════════════════════════════════════════════

class MCPClient:
    """
    Connects to an MCP server via HTTP+SSE.
    
    Supports two transport modes:
    - HTTP+SSE: For web-based MCP servers (Slack, Gmail, Asana...)
    - Stdio: For local MCP servers (launched as subprocess)
    """

    def __init__(
        self,
        url: str = "",
        name: str = "mcp-server",
        transport: str = "sse",       # "sse" or "stdio"
        command: str = "",             # for stdio: e.g. "npx @modelcontextprotocol/server-slack"
        args: list[str] = None,       # for stdio: additional args
        env: dict[str, str] = None,   # for stdio: environment vars
        headers: dict[str, str] = None,  # for sse: custom headers
        timeout: float = 30.0,
    ):
        self.url = url
        self.name = name
        self.transport = transport
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.headers = headers or {}
        self.timeout = timeout

        self._connected = False
        self._tools: list[MCPTool] = []
        self._resources: list[MCPResource] = []
        self._process: Optional[subprocess.Popen] = None
        self._message_endpoint: str = ""  # for SSE: URL to send messages
        self._request_id = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    def _next_id(self) -> str:
        self._request_id += 1
        return str(self._request_id)

    # ── Connect ──

    async def connect(self) -> bool:
        """Connect to MCP server and discover tools."""
        try:
            if self.transport == "sse":
                return await self._connect_sse()
            elif self.transport == "stdio":
                return await self._connect_stdio()
            return False
        except Exception as e:
            print(f"[MCP] Connection failed ({self.name}): {e}")
            return False

    async def _connect_sse(self) -> bool:
        """Connect via HTTP+SSE."""
        import urllib.request

        # Step 1: Connect to SSE endpoint to get message URL
        try:
            req = urllib.request.Request(self.url, headers={
                "Accept": "text/event-stream",
                "User-Agent": "Phidipus-MCP/1.0",
                **self.headers,
            })
            resp = urllib.request.urlopen(req, timeout=self.timeout)

            # Read SSE stream for endpoint event
            for line in resp:
                line = line.decode("utf-8").strip()
                if line.startswith("event: endpoint"):
                    next_line = resp.readline().decode("utf-8").strip()
                    if next_line.startswith("data: "):
                        endpoint_path = next_line[6:].strip()
                        # Resolve relative URL
                        if endpoint_path.startswith("/"):
                            from urllib.parse import urlparse
                            parsed = urlparse(self.url)
                            self._message_endpoint = f"{parsed.scheme}://{parsed.netloc}{endpoint_path}"
                        else:
                            self._message_endpoint = endpoint_path
                        break
            resp.close()
        except Exception as e:
            # Fallback: try /message endpoint directly
            from urllib.parse import urlparse
            parsed = urlparse(self.url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            self._message_endpoint = f"{base}/message"
            print(f"[MCP] SSE endpoint fallback: {self._message_endpoint}")

        if not self._message_endpoint:
            return False

        # Step 2: Initialize
        init_result = await self._send_jsonrpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "Phidipus", "version": "3.0"},
        })

        if not init_result:
            return False

        # Step 3: Send initialized notification
        await self._send_jsonrpc("notifications/initialized", {}, is_notification=True)

        # Step 4: List tools
        await self.list_tools()

        self._connected = True
        print(f"[MCP] Connected to {self.name} — {len(self._tools)} tools")
        return True

    async def _connect_stdio(self) -> bool:
        """Connect via stdio subprocess."""
        env = {**os.environ, **self.env}
        cmd = [self.command] + self.args if self.command else self.args

        if not cmd:
            return False

        try:
            self._process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env,
            )

            # Initialize
            init_result = await self._send_stdio_jsonrpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "Phidipus", "version": "3.0"},
            })

            if not init_result:
                return False

            await self._send_stdio_jsonrpc("notifications/initialized", {}, is_notification=True)
            await self.list_tools()

            self._connected = True
            print(f"[MCP] Connected to {self.name} (stdio) — {len(self._tools)} tools")
            return True
        except Exception as e:
            print(f"[MCP] Stdio connect failed: {e}")
            return False

    # ── JSON-RPC Communication ──

    async def _send_jsonrpc(
        self, method: str, params: dict, is_notification: bool = False
    ) -> dict | None:
        """Send JSON-RPC message via HTTP."""
        if self.transport == "stdio":
            return await self._send_stdio_jsonrpc(method, params, is_notification)

        import urllib.request
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not is_notification:
            msg["id"] = self._next_id()

        data = json.dumps(msg).encode("utf-8")
        try:
            req = urllib.request.Request(
                self._message_endpoint,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Phidipus-MCP/1.0",
                    **self.headers,
                },
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if "error" in body:
                    print(f"[MCP] Error: {body['error']}")
                    return None
                return body.get("result", body)
        except Exception as e:
            print(f"[MCP] Request failed ({method}): {e}")
            return None

    async def _send_stdio_jsonrpc(
        self, method: str, params: dict, is_notification: bool = False
    ) -> dict | None:
        """Send JSON-RPC message via stdio."""
        if not self._process or self._process.poll() is not None:
            return None

        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not is_notification:
            msg["id"] = self._next_id()

        line = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(line.encode("utf-8"))
            self._process.stdin.flush()

            if is_notification:
                return {}

            # Read response
            resp_line = self._process.stdout.readline().decode("utf-8").strip()
            if resp_line:
                body = json.loads(resp_line)
                return body.get("result", body)
        except Exception as e:
            print(f"[MCP] Stdio error ({method}): {e}")
        return None

    # ── Tool Operations ──

    async def list_tools(self) -> list[MCPTool]:
        """Discover available tools from MCP server."""
        result = await self._send_jsonrpc("tools/list", {})
        if result and "tools" in result:
            self._tools = [
                MCPTool(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                    server_name=self.name,
                )
                for t in result["tools"]
            ]
        return self._tools

    async def call_tool(self, name: str, arguments: dict = None) -> MCPResult:
        """Invoke a tool on the MCP server."""
        result = await self._send_jsonrpc("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

        if result is None:
            return MCPResult(success=False, error="No response from server")

        if result.get("isError"):
            error_text = ""
            for c in result.get("content", []):
                if c.get("type") == "text":
                    error_text += c.get("text", "")
            return MCPResult(success=False, error=error_text, is_error=True)

        # Extract content
        content = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                content.append(c.get("text", ""))
            elif c.get("type") == "image":
                content.append({"image": c.get("data", ""), "mimeType": c.get("mimeType", "")})
            else:
                content.append(c)

        return MCPResult(
            success=True,
            content=content[0] if len(content) == 1 else content,
        )

    # ── Resource Operations ──

    async def list_resources(self) -> list[MCPResource]:
        """List available resources."""
        result = await self._send_jsonrpc("resources/list", {})
        if result and "resources" in result:
            self._resources = [
                MCPResource(
                    uri=r.get("uri", ""),
                    name=r.get("name", ""),
                    description=r.get("description", ""),
                    mime_type=r.get("mimeType", ""),
                )
                for r in result["resources"]
            ]
        return self._resources

    # ── Lifecycle ──

    async def disconnect(self):
        """Disconnect from MCP server."""
        if self._process:
            self._process.terminate()
            self._process = None
        self._connected = False
        self._tools = []
        print(f"[MCP] Disconnected from {self.name}")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "transport": self.transport,
            "connected": self._connected,
            "tools_count": len(self._tools),
            "tools": [t.to_dict() for t in self._tools],
        }
