#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
mcp/mcp_registry.py — MCP Server Registry & Tool Router
═══════════════════════════════════════════════════════════

Manages multiple MCP server connections.
Integrates external MCP tools into Phidipus workflow engine.

Usage:
    registry = MCPRegistry()
    await registry.add_server("slack", url="https://mcp.slack.com/sse")
    await registry.add_server("gmail", url="https://gmail.mcp.example.com/sse")
    
    # List all tools across all servers
    tools = registry.all_tools()
    
    # Call a tool by name (auto-routes to correct server)
    result = await registry.call("slack__send_message", {"channel": "#general", "text": "Hi"})
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .mcp_client import MCPClient, MCPTool, MCPResult

# ══════════════════════════════════════════════════════════
# Well-known MCP servers
# ══════════════════════════════════════════════════════════

WELL_KNOWN_SERVERS = {
    "slack": {
        "name": "Slack",
        "url": "",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-slack"],
        "env_keys": ["SLACK_BOT_TOKEN"],
        "description": "Gửi tin nhắn, tạo channel, quản lý Slack",
        "icon": "💬",
    },
    "github": {
        "name": "GitHub",
        "url": "",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-github"],
        "env_keys": ["GITHUB_TOKEN"],
        "description": "Issues, PRs, repos, commits",
        "icon": "🐙",
    },
    "filesystem": {
        "name": "Filesystem",
        "url": "",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-filesystem", "/Users"],
        "env_keys": [],
        "description": "Đọc/ghi file trên máy",
        "icon": "📁",
    },
    "brave-search": {
        "name": "Brave Search",
        "url": "",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-brave-search"],
        "env_keys": ["BRAVE_API_KEY"],
        "description": "Tìm kiếm web qua Brave",
        "icon": "🔍",
    },
    "google-drive": {
        "name": "Google Drive",
        "url": "",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-google-drive"],
        "env_keys": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
        "description": "Google Drive files, docs, sheets",
        "icon": "📄",
    },
    "desktop-commander": {
        "name": "Desktop Commander",
        "url": "",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-desktop-commander"],
        "env_keys": [],
        "description": "Điều khiển desktop (click, type, screenshot)",
        "icon": "🖥️",
    },
    "memory": {
        "name": "Memory",
        "url": "",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-memory"],
        "env_keys": [],
        "description": "Bộ nhớ dài hạn cho agent",
        "icon": "🧠",
    },
}

# ══════════════════════════════════════════════════════════
# MCP Registry
# ══════════════════════════════════════════════════════════

class MCPRegistry:
    """Manages multiple MCP server connections."""

    def __init__(self, config_path: str = ""):
        self._clients: dict[str, MCPClient] = {}
        self._config_path = config_path or str(
            Path(__file__).parent.parent / "data" / "mcp_servers.json"
        )

    # ── Server Management ──

    async def add_server(
        self,
        name: str,
        url: str = "",
        transport: str = "sse",
        command: str = "",
        args: list[str] = None,
        env: dict[str, str] = None,
        auto_connect: bool = True,
    ) -> bool:
        """Add and optionally connect to an MCP server."""
        if name in self._clients:
            await self._clients[name].disconnect()

        client = MCPClient(
            url=url, name=name, transport=transport,
            command=command, args=args or [], env=env or {},
        )
        self._clients[name] = client

        if auto_connect:
            ok = await client.connect()
            if ok:
                self._save_config()
            return ok
        return True

    async def add_well_known(self, server_id: str, env_overrides: dict = None) -> bool:
        """Add a well-known MCP server by ID."""
        if server_id not in WELL_KNOWN_SERVERS:
            print(f"[MCP] Unknown server: {server_id}")
            return False

        info = WELL_KNOWN_SERVERS[server_id]
        env = {}
        for key in info.get("env_keys", []):
            env[key] = os.environ.get(key, "")
        if env_overrides:
            env.update(env_overrides)

        return await self.add_server(
            name=server_id,
            url=info.get("url", ""),
            transport=info.get("transport", "stdio"),
            command=info.get("command", ""),
            args=info.get("args", []),
            env=env,
        )

    async def remove_server(self, name: str):
        """Disconnect and remove an MCP server."""
        if name in self._clients:
            await self._clients[name].disconnect()
            del self._clients[name]
            self._save_config()

    async def reconnect(self, name: str) -> bool:
        """Reconnect to a server."""
        if name in self._clients:
            return await self._clients[name].connect()
        return False

    # ── Tool Discovery ──

    def all_tools(self) -> list[dict]:
        """List all tools across all connected servers."""
        tools = []
        for name, client in self._clients.items():
            if client.connected:
                for tool in client.tools:
                    tools.append({
                        **tool.to_dict(),
                        "qualified_name": f"{name}__{tool.name}",
                        "server": name,
                    })
        return tools

    def find_tool(self, qualified_name: str) -> tuple[Optional[MCPClient], Optional[MCPTool]]:
        """Find a tool by qualified name (server__tool_name)."""
        if "__" in qualified_name:
            server_name, tool_name = qualified_name.split("__", 1)
        else:
            # Search all servers
            for name, client in self._clients.items():
                for tool in client.tools:
                    if tool.name == qualified_name:
                        return client, tool
            return None, None

        client = self._clients.get(server_name)
        if not client:
            return None, None
        for tool in client.tools:
            if tool.name == tool_name:
                return client, tool
        return None, None

    # ── Tool Execution ──

    async def call(self, qualified_name: str, arguments: dict = None) -> MCPResult:
        """Call a tool by qualified name. Auto-routes to correct server."""
        client, tool = self.find_tool(qualified_name)
        if not client or not tool:
            return MCPResult(success=False, error=f"Tool not found: {qualified_name}")

        if not client.connected:
            return MCPResult(success=False, error=f"Server not connected: {client.name}")

        return await client.call_tool(tool.name, arguments or {})

    # ── Status ──

    def status(self) -> dict:
        """Get status of all servers."""
        servers = []
        for name, client in self._clients.items():
            servers.append(client.to_dict())
        return {
            "servers": servers,
            "total_tools": sum(len(c.tools) for c in self._clients.values() if c.connected),
            "connected": sum(1 for c in self._clients.values() if c.connected),
        }

    # ── Config Persistence ──

    def _save_config(self):
        """Save server config to JSON."""
        config = {}
        for name, client in self._clients.items():
            config[name] = {
                "url": client.url,
                "transport": client.transport,
                "command": client.command,
                "args": client.args,
            }
        try:
            Path(self._config_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_path, "w") as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            print(f"[MCP] Config save error: {e}")

    async def load_config(self):
        """Load and reconnect saved servers."""
        if not os.path.exists(self._config_path):
            return
        try:
            with open(self._config_path) as f:
                config = json.load(f)
            for name, info in config.items():
                await self.add_server(
                    name=name, url=info.get("url", ""),
                    transport=info.get("transport", "sse"),
                    command=info.get("command", ""),
                    args=info.get("args", []),
                    auto_connect=True,
                )
        except Exception as e:
            print(f"[MCP] Config load error: {e}")

    # ── Cleanup ──

    async def disconnect_all(self):
        """Disconnect all servers."""
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()


# Global registry instance
_registry: Optional[MCPRegistry] = None

def get_registry() -> MCPRegistry:
    """Get or create global MCP registry."""
    global _registry
    if _registry is None:
        _registry = MCPRegistry()
    return _registry
