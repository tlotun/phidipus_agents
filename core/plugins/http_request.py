# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/plugins/http_request.py — Example Plugin: HTTP Request Node
═══════════════════════════════════════════════════════════════════

Custom node that makes HTTP GET/POST requests.
Demonstrates how to create a workflow node plugin.

Usage in workflow:
  - type: http_request
    config:
      url: "https://api.example.com/data"
      method: "GET"
      headers: {"Authorization": "Bearer xxx"}
      body: ""
      store_as: "api_response"
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
import urllib.error
from core.node_registry import NodeRegistry


@NodeRegistry.register(
    "http_request",
    schema={
        "required": ["url"],
        "values": {"method": ["GET", "POST", "PUT", "DELETE"]},
        "types": {"timeout": (int, float)},
    },
    defaults={
        "url": "",
        "method": "GET",
        "headers": {},
        "body": "",
        "timeout": 15,
        "store_as": "api_response",
    },
    palette={
        "icon": "🌐",
        "color": "#3b82f6",
        "label": "HTTP Request",
    },
    i18n={
        "desc": {
            "vi": "Gửi HTTP request (GET/POST) đến API",
            "en": "Send HTTP request (GET/POST) to API",
            "zh": "发送HTTP请求(GET/POST)到API",
            "ko": "API에 HTTP 요청(GET/POST) 전송",
        },
        "hint": {
            "vi": "Gọi API bên ngoài:\n• REST API (GET/POST)\n• Webhook trigger\n• Tích hợp dịch vụ bên thứ 3",
            "en": "Call external APIs:\n• REST API (GET/POST)\n• Webhook triggers\n• 3rd party integration",
        },
    },
    version="1.0",
    author="phidipus",
)
async def exec_http_request(config: dict, prev_result=None) -> dict:
    """Execute HTTP request and return response."""
    url = config.get("url", "")
    method = config.get("method", "GET").upper()
    headers = config.get("headers", {})
    body = config.get("body", "")
    timeout = int(config.get("timeout", 15))

    if not url:
        return {"success": False, "error": "URL is required"}

    # FIX BUG#3: SSRF protection — block internal/private IPs
    import re as _re
    _parsed = urllib.parse.urlparse(url)
    _host = _parsed.hostname or ""
    _BLOCKED_HOSTS = [
        r"^127\.", r"^10\.", r"^172\.(1[6-9]|2\d|3[01])\.", r"^192\.168\.",
        r"^0\.", r"^localhost$", r"^0\.0\.0\.0$", r"^\[::1\]$", r"^::1$",
        r"^169\.254\.",  # link-local
        r"^fc00:", r"^fe80:",  # IPv6 private
    ]
    for pattern in _BLOCKED_HOSTS:
        if _re.match(pattern, _host, _re.I):
            return {"success": False, "error": f"SSRF blocked: cannot access internal host '{_host}'"}
    if not _parsed.scheme or _parsed.scheme not in ("http", "https"):
        return {"success": False, "error": f"Only http/https URLs allowed, got: {_parsed.scheme}"}

    def _do_request():
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "Phidipus/2.4")
        for k, v in headers.items():
            req.add_header(k, v)
        if body and "Content-Type" not in headers:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                response_body = r.read().decode("utf-8", errors="replace")
                return {
                    "success": True,
                    "status_code": r.status,
                    "result": response_body[:5000],
                    "headers": dict(r.headers),
                }
        except urllib.error.HTTPError as e:
            return {
                "success": False,
                "status_code": e.code,
                "error": f"HTTP {e.code}: {e.reason}",
                "result": e.read().decode("utf-8", errors="replace")[:2000],
            }
        except urllib.error.URLError as e:
            return {"success": False, "error": f"URL Error: {e.reason}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    result = await asyncio.to_thread(_do_request)
    return result
