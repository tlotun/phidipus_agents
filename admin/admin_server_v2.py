# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
admin/admin_server_v2.py — Phidipus SpiderHub Admin Panel v2 (v9.20)
═══════════════════════════════════════════════════════════════════

FastAPI backend for SpiderHub v2: WebSocket realtime, Shadow Mode,
Skill Forge Radar, Task Graph, Chrome Swarm, God Mode.

API v2 endpoints:
  GET  /api/v2/ws                      — WebSocket realtime feed
  GET  /api/v2/screen/thumb            — Live screen thumbnail
  POST /api/v2/shadow/toggle           — Toggle Shadow Mode
  GET  /api/v2/shadow/replay/{seg_id} — Replay shadow segment
  GET  /api/v2/task/graph              — Task dependency graph
  GET  /api/v2/chrome/profiles/status  — All 107 profiles + status
  GET  /api/v2/skill/forge/radar       — Skill Forge provider radar
  POST /api/v2/memory/semantic/search  — Semantic memory search
  POST /api/v2/system/god-reset        — God mode full reset

Backward compatible: v1 admin_server.py endpoints still served.
Binds ONLY to 127.0.0.1:8912.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Query, Request
    from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
except ImportError:
    raise SystemExit("pip install fastapi uvicorn websockets --break-system-packages")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


import secrets
import functools
import stat
import time as _time_mod
from fastapi import Request, Depends

# ── COMMERCIAL-RBAC: 2-level token auth (admin + readonly) ───────────────────
# Admin token  → full access (run tasks, approve skills, god mode, config)
# Readonly token → monitoring only (stats, health, task list, skill list)
#
# Why 2 levels for commercial:
#   - Investors / dashboards need read access without risk of running tasks
#   - CI/CD pipelines can check health without admin privileges
#   - Multi-team: ops team gets readonly, admin team gets full access
#
# Token files:
#   ~/.config/phidipus/.admin_token    (0o600)  — full access
#   ~/.config/phidipus/.readonly_token (0o600)  — monitoring only
#
# FIX #4: Moved from /tmp → ~/.config/phidipus/ (0o700 dir, 0o600 files)
_TOKEN_TTL_SECONDS = 8 * 3600        # 8 hours
_ADMIN_TOKEN:    str   = secrets.token_hex(32)
_READONLY_TOKEN: str   = secrets.token_hex(32)
_TOKEN_CREATED_AT: float = _time_mod.time()

def _get_token_file() -> Path:
    """Return ~/.config/phidipus/.admin_token path, creating dir with 0o700."""
    import os as _os_raw
    try:
        config_dir = Path.home() / ".config" / "phidipus"
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        current_mode = _os_raw.stat(str(config_dir)).st_mode & 0o777
        if current_mode != 0o700:
            _os_raw.chmod(str(config_dir), 0o700)
        return config_dir / ".admin_token"
    except Exception:
        _vlog("⚠️", "[RBAC] Không thể tạo ~/.config/phidipus — dùng /tmp fallback")
        return Path("/tmp/.phidipus_admin_token")

_TOKEN_FILE = _get_token_file()
_TOKEN_LOCK = asyncio.Lock()


def _write_token_file() -> None:
    """Write BOTH admin + readonly tokens atomically with 0o600."""
    import os as _os_raw
    token_path = _get_token_file()
    readonly_path = token_path.parent / ".readonly_token"
    for path, token in [(token_path, _ADMIN_TOKEN), (readonly_path, _READONLY_TOKEN)]:
        try:
            fd = _os_raw.open(
                str(path),
                _os_raw.O_CREAT | _os_raw.O_WRONLY | _os_raw.O_TRUNC,
                0o600,
            )
            try:
                _os_raw.write(fd, token.encode())
            finally:
                _os_raw.close(fd)
        except Exception as e:
            _vlog("⚠️", f"[RBAC] Không thể ghi token file {path.name}: {e}")
    global _TOKEN_FILE
    _TOKEN_FILE = token_path


async def _rotate_token_if_expired() -> None:
    """Rotate BOTH tokens when TTL expires."""
    global _ADMIN_TOKEN, _READONLY_TOKEN, _TOKEN_CREATED_AT
    async with _TOKEN_LOCK:
        if _time_mod.time() - _TOKEN_CREATED_AT > _TOKEN_TTL_SECONDS:
            _ADMIN_TOKEN    = secrets.token_hex(32)
            _READONLY_TOKEN = secrets.token_hex(32)
            _TOKEN_CREATED_AT = _time_mod.time()
            _write_token_file()
            _vlog("🔄", "[RBAC] Both tokens rotated (TTL expired)")


async def _token_rotation_background() -> None:
    """Background task: check token expiry every 15 minutes."""
    while True:
        await asyncio.sleep(900)
        try:
            await _rotate_token_if_expired()
        except Exception:
            pass


def _get_request_token(request: Request) -> str:
    """Extract token from request header ONLY.
    SEC-05 FIX: Removed query_params fallback — tokens in URLs leak into
    server logs, browser history, proxy caches and Referer headers.
    """
    return request.headers.get("X-Phidipus-Token", "")


def _verify_token(request: Request) -> bool:
    """Returns True if EITHER admin or readonly token matches (for backward compat)."""
    token = _get_request_token(request)
    if not token:
        return False
    return (
        secrets.compare_digest(token, _ADMIN_TOKEN) or
        secrets.compare_digest(token, _READONLY_TOKEN)
    )


def _verify_admin_token(request: Request) -> bool:
    """Returns True only for admin token."""
    token = _get_request_token(request)
    return bool(token) and secrets.compare_digest(token, _ADMIN_TOKEN)


_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "cf-connecting-ip", "x-real-ip",
                  "true-client-ip", "cf-ray")
_EXTRA_ALLOWED: dict[str, Any] = {"ts": 0.0, "hosts": set(), "origins": set()}


def _split_host(host: str) -> str:
    """'127.0.0.1:8912' → '127.0.0.1', '[::1]:8912' → '::1'."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _extra_allowed() -> tuple[set[str], set[str]]:
    """Extra hosts / origins from config.yaml → admin.allowed_hosts / allowed_origins
    (e.g. a Cloudflare tunnel domain).  Cached for 30s."""
    now = _time_mod.time()
    if now - _EXTRA_ALLOWED["ts"] > 30:
        hosts: set[str] = set()
        origins: set[str] = set()
        try:
            import yaml
            cp = Path(__file__).parent.parent / "config.yaml"
            cfg = yaml.safe_load(cp.read_text(encoding="utf-8")) if cp.exists() else {}
            adm = (cfg or {}).get("admin") or {}
            hosts = {_split_host(h) for h in adm.get("allowed_hosts") or [] if h}
            origins = {str(o).rstrip("/").lower() for o in adm.get("allowed_origins") or [] if o}
        except Exception:
            pass
        _EXTRA_ALLOWED.update(ts=now, hosts=hosts, origins=origins)
    return _EXTRA_ALLOWED["hosts"], _EXTRA_ALLOWED["origins"]


def _host_is_local(host_header: str) -> bool:
    return _split_host(host_header) in _LOCAL_HOSTNAMES


def _origin_allowed(origin: str) -> bool:
    origin = (origin or "").rstrip("/").lower()
    if not origin or origin == "null":
        return False
    _, extra_origins = _extra_allowed()
    if origin in extra_origins:
        return True
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and (parts.hostname or "") in _LOCAL_HOSTNAMES


def _request_violation(scope_type: str, method: str, headers: dict[str, str]) -> str:
    """Return a reason string when a request must be rejected, else ''.

    Blocks (v4.3):
      * DNS rebinding — Host header must be localhost or explicitly allowed;
      * cross-site requests from web pages (CSRF / drive-by task execution);
      * cross-site WebSocket hijacking (browsers always send Origin for WS).
    """
    host = headers.get("host", "")
    extra_hosts, _ = _extra_allowed()
    if not (_host_is_local(host) or _split_host(host) in extra_hosts):
        return "Host not allowed"
    origin = headers.get("origin", "")
    fetch_site = headers.get("sec-fetch-site", "")
    if origin and not _origin_allowed(origin):
        return "Cross-origin request blocked"
    if not origin and fetch_site == "cross-site":
        return "Cross-site request blocked"
    if scope_type == "websocket" and origin == "null":
        return "Opaque origin blocked"
    return ""


class _OriginGuard:
    """Pure ASGI middleware (covers HTTP *and* WebSocket scopes)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        reason = _request_violation(scope["type"], scope.get("method", "GET"), headers)
        if not reason:
            await self.app(scope, receive, send)
            return
        _vlog("🛡️", f"[ORIGIN] {reason}: {scope.get('path', '')} "
                    f"(host={headers.get('host', '')!r} origin={headers.get('origin', '')!r})")
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": reason}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def _is_local_peer(client_host: str | None, headers: Any) -> bool:
    """Direct local connection, local Host, not relayed by a proxy/tunnel.

    Requests coming through a tunnel (cloudflared, ngrok) also originate from
    127.0.0.1 — they must NOT get the localhost auto-login.
    """
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        return False
    if any(h in headers for h in _PROXY_HEADERS):
        return False
    return _host_is_local(headers.get("host", ""))


def _is_localhost(request: Request) -> bool:
    """Check if request comes from a local user (see _is_local_peer)."""
    client = request.client
    return _is_local_peer(client.host if client else None, request.headers)


# ── FIX v1.0: JWT Session Auth for remote access ────────────
import hashlib as _hashlib
import hmac as _hmac
import base64 as _base64

_JWT_SECRET = secrets.token_hex(32)
_JWT_SESSIONS: dict[str, dict] = {}  # token → {user, created, expires}
_LOGIN_ATTEMPTS: dict[str, list] = {}  # ip → [timestamps] — rate limit
_MAX_LOGIN_ATTEMPTS = 5  # per minute

def _load_admin_credentials() -> dict:
    """Load admin credentials from config.yaml → admin_auth.

    v4.3: there is NO built-in default account any more (the old fallback
    ``admin / phidipus`` was public knowledge).  Without configured
    credentials remote login is disabled; local use needs no login.
    Returns {"username", "password_hash"} or {"username", "password"} or {}.
    """
    try:
        import yaml
        cp = Path(__file__).parent.parent / "config.yaml"
        if cp.exists():
            cfg = yaml.safe_load(cp.read_text(encoding="utf-8")) or {}
            auth = cfg.get("admin_auth") or {}
            if auth.get("username") and auth.get("password_hash"):
                return {"username": str(auth["username"]), "password_hash": str(auth["password_hash"])}
            if auth.get("username") and auth.get("password"):
                return {"username": str(auth["username"]), "password": str(auth["password"])}
    except Exception:
        pass
    return {}


def _credentials_match(creds: dict, username: str, password: str) -> bool:
    from admin.passwords import verify_password, is_legacy_hash
    if not creds or not _hmac.compare_digest(username.encode(), str(creds.get("username", "")).encode()):
        # still burn comparable CPU so timing does not reveal the username
        verify_password(password, "scrypt$16384$8$1$00$00")
        return False
    if creds.get("password_hash"):
        if is_legacy_hash(creds["password_hash"]):
            _vlog("⚠️", "[AUTH] admin_auth.password_hash dùng SHA-256 cũ — tạo hash mới: python -m admin.passwords")
        return verify_password(password, creds["password_hash"])
    _vlog("⚠️", "[AUTH] admin_auth.password để dạng chữ thường — nên đổi sang password_hash")
    return _hmac.compare_digest(password.encode(), str(creds.get("password", "")).encode())

def _create_jwt(username: str, hours: int = 24) -> str:
    """Create simple JWT-like session token."""
    token = secrets.token_hex(32)
    _JWT_SESSIONS[token] = {
        "user": username,
        "created": _time_mod.time(),
        "expires": _time_mod.time() + hours * 3600,
    }
    # Cleanup expired sessions
    now = _time_mod.time()
    expired = [k for k, v in _JWT_SESSIONS.items() if v["expires"] < now]
    for k in expired:
        _JWT_SESSIONS.pop(k, None)
    return token

def _verify_jwt(request: Request) -> bool:
    """Verify JWT session token from header or cookie."""
    # Try Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        session = _JWT_SESSIONS.get(token)
        if session and session["expires"] > _time_mod.time():
            return True
    # Try cookie
    token = request.cookies.get("ph_session", "")
    if token:
        session = _JWT_SESSIONS.get(token)
        if session and session["expires"] > _time_mod.time():
            return True
    # SEC-05 FIX: Do not accept session token via query param (URL leakage).
    # All auth tokens must come through request headers only.
    return False

def _check_login_rate(ip: str) -> bool:
    """Rate limit login attempts: max 5 per minute."""
    now = _time_mod.time()
    attempts = _LOGIN_ATTEMPTS.get(ip, [])
    attempts = [t for t in attempts if now - t < 60]
    _LOGIN_ATTEMPTS[ip] = attempts
    return len(attempts) < _MAX_LOGIN_ATTEMPTS


def _require_auth(request: Request) -> None:
    """Any valid token (admin or readonly). Localhost auto-passes. JWT for remote."""
    if _is_localhost(request):
        return  # v9.21: localhost auto-auth for Admin Panel
    if _verify_jwt(request):
        return  # FIX v1.0: JWT session valid
    if not _verify_token(request):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing token")


def _require_admin(request: Request) -> None:
    """Admin token only — for write/mutating endpoints. Localhost auto-passes."""
    if _is_localhost(request):
        return  # v9.21: localhost auto-auth for Admin Panel
    if _verify_jwt(request):
        return  # FIX v1.0: JWT session = admin level
    if not _verify_admin_token(request):
        raise HTTPException(
            status_code=403,
            detail="Forbidden: this endpoint requires admin token (not readonly)"
        )


# FastAPI Dependencies
def _auth_dep(request: Request) -> None:
    """Any valid token — read operations."""
    _require_auth(request)


def _admin_dep(request: Request) -> None:
    """Admin token only — write/execute operations."""
    _require_admin(request)

# [C-05 FIX] Global task semaphore — max 3 concurrent tasks
_task_semaphore = asyncio.Semaphore(3)
# [M-01 FIX] Per-connection WS task rate limiter (max 10 tasks/minute)
_ws_task_last: dict[str, list[float]] = {}
_WS_RATE_LIMIT = 10   # tasks per 60s window
_MAX_WS_CLIENTS = 20  # [M-04 FIX] Hard cap on WS connections

# SEC-R11: Hard queue cap — prevents DoS via task queue flooding
# Tracks total pending + running tasks across all submission paths.
# When at cap, new submissions are rejected with HTTP 429.
_pending_task_count: int = 0
_MAX_PENDING_TASKS: int = 20       # absolute cap: pending + running


# ══════════════════════════════════════════════════════════════════
# Shadow Mode state (in-memory, per-session)
# ══════════════════════════════════════════════════════════════════

class ShadowModeState:
    """
    Records live action chains for replay and learning.
    Segments = named groups of actions (one per task).
    """

    def __init__(self) -> None:
        self.enabled: bool = False
        self.privacy_level: str = "medium"   # low | medium | god
        self.segments: list[dict] = []       # Max 50
        self._current: dict | None = None
        self.heatmap_points: list[dict] = []  # {x, y, weight}
        self.learn_buffer: list[dict] = []    # Last 10 tasks

    def toggle(self, privacy: str = "medium") -> bool:
        self.enabled = not self.enabled
        self.privacy_level = privacy
        if self.enabled:
            _vlog("👁️", f"Shadow Mode ON (privacy={privacy})")
        else:
            _vlog("👁️", "Shadow Mode OFF")
        return self.enabled

    def start_segment(self, task_id: str, goal: str) -> str:
        seg_id = f"seg_{task_id[:8]}"
        self._current = {
            "id": seg_id,
            "task_id": task_id,
            "goal": goal[:80],
            "started_at": time.time(),
            "actions": [],
            "privacy": self.privacy_level,
        }
        return seg_id

    def record_action(self, action: str, payload: dict, result: str = "") -> None:
        if not self.enabled or not self._current:
            return
        if self.privacy_level == "god":
            return
        entry = {
            "t": time.time(),
            "action": action,
            "result": result[:100] if self.privacy_level != "low" else result[:500],
        }
        # Don't record keystrokes in medium privacy
        if self.privacy_level == "medium" and action in ("keyboard_type", "keyboard_press"):
            entry["payload"] = {"redacted": True}
        else:
            entry["payload"] = payload
        self._current["actions"].append(entry)

        # Track click heatmap
        if action == "mouse_click" and self.privacy_level == "low":
            self.heatmap_points.append({
                "x": payload.get("x", 0),
                "y": payload.get("y", 0),
                "weight": 1,
            })

    def close_segment(self, success: bool) -> dict | None:
        if not self._current:
            return None
        self._current["finished_at"] = time.time()
        self._current["success"] = success
        self._current["duration_s"] = round(
            self._current["finished_at"] - self._current["started_at"], 1
        )

        seg = dict(self._current)
        self.segments.append(seg)
        if len(self.segments) > 50:
            self.segments.pop(0)

        self.learn_buffer.append(seg)
        if len(self.learn_buffer) > 10:
            self.learn_buffer.pop(0)

        self._current = None
        return seg

    def get_replay(self, seg_id: str) -> dict | None:
        for seg in self.segments:
            if seg["id"] == seg_id:
                return seg
        return None

    def get_learn_insights(self) -> list[dict]:
        """Derive patterns from last 10 tasks."""
        insights = []
        if not self.learn_buffer:
            return insights
        success_rate = sum(1 for s in self.learn_buffer if s.get("success")) / len(self.learn_buffer)
        avg_dur = sum(s.get("duration_s", 0) for s in self.learn_buffer) / len(self.learn_buffer)
        action_counts: dict[str, int] = {}
        for seg in self.learn_buffer:
            for act in seg.get("actions", []):
                a = act.get("action", "unknown")
                action_counts[a] = action_counts.get(a, 0) + 1

        top_actions = sorted(action_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        insights.append({
            "type": "summary",
            "success_rate": round(success_rate, 2),
            "avg_duration_s": round(avg_dur, 1),
            "top_actions": [{"action": a, "count": c} for a, c in top_actions],
            "tasks_analyzed": len(self.learn_buffer),
        })
        return insights


_shadow = ShadowModeState()


# ══════════════════════════════════════════════════════════════════
# WebSocket manager
# ══════════════════════════════════════════════════════════════════

class _WSManager:
    """Broadcast hub for realtime SpiderHub updates."""

    def __init__(self) -> None:
        self._sockets: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> bool:
        # [M-04 FIX] Hard cap on WS connections
        if len(self._sockets) >= _MAX_WS_CLIENTS:
            await ws.close(code=1008, reason="Too many connections")
            _vlog("⚠️", f"[M-04] WS connection rejected (max={_MAX_WS_CLIENTS})")
            return False
        await ws.accept()
        self._sockets.append(ws)
        _vlog("🔌", f"WS client connected (total={len(self._sockets)})")
        return True

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._sockets:
            self._sockets.remove(ws)
        _vlog("🔌", f"WS client disconnected (total={len(self._sockets)})")

    async def broadcast(self, event: str, data: dict) -> None:
        if not self._sockets:
            return
        msg = json.dumps({"event": event, "data": data, "ts": time.time()})
        dead = []
        for ws in list(self._sockets):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self._sockets)


_ws_manager = _WSManager()

# ══════════════════════════════════════════════════════════════════
# Injected components (set by main.py)
# ══════════════════════════════════════════════════════════════════

_components: dict[str, Any] = {}


def _bot_is_polling(bot=None) -> bool:
    """Check if a PhidipusBot is actually polling Telegram right now.

    Uses bot._is_polling as the authoritative flag — it is set True only
    after start_polling() succeeds and cleared immediately in stop().
    Falls back to updater.running for bots created outside this module.
    Never raises.
    """
    if bot is None:
        bot = _components.get("telegram_bot")
    if bot is None:
        return False
    # Primary: explicit flag set by start() / cleared by stop()
    try:
        if bool(getattr(bot, "_is_polling", False)):
            return True
    except Exception:
        pass
    # Secondary: PTB updater.running (authoritative for externally created bots)
    try:
        app = getattr(bot, "_app", None)
        if app is not None:
            updater = getattr(app, "updater", None)
            if updater is not None:
                if bool(getattr(updater, "running", False)):
                    return True
    except Exception:
        pass
    return False


def _read_config_yaml() -> dict:
    """Read config.yaml FILE using absolute path from _components. Always works."""
    try:
        import yaml
        # 1. Try absolute path from inject_components_v2
        cp = _components.get("config_path", "")
        if cp:
            p = Path(cp)
            if p.exists():
                with open(p) as f:
                    return yaml.safe_load(f) or {}
        # 2. Try CWD
        p = Path("config.yaml")
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
        # 3. Try relative to admin_server_v2.py
        p = Path(__file__).parent.parent / "config.yaml"
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
    except Exception as exc:
        _vlog("⚠️", f"_read_config_yaml error: {exc}")
    return {}


# Simple state file for key status (more reliable than parsing YAML every time)
_STATE_FILE: Path | None = None

def _get_state_file() -> Path:
    global _STATE_FILE
    if _STATE_FILE:
        return _STATE_FILE
    cp = _components.get("config_path", "")
    if cp:
        _STATE_FILE = Path(cp).parent / ".phidipus_state.json"
    else:
        _STATE_FILE = Path(__file__).parent.parent / ".phidipus_state.json"
    return _STATE_FILE

def _read_state() -> dict:
    try:
        sf = _get_state_file()
        if sf.exists():
            return json.loads(sf.read_text("utf-8"))
    except Exception:
        pass
    return {}

def _write_state(data: dict) -> None:
    try:
        sf = _get_state_file()
        existing = _read_state()
        # Deep merge for "keys" dict (don't overwrite previous keys)
        if "keys" in data and "keys" in existing:
            existing["keys"].update(data["keys"])
            data_copy = dict(data)
            del data_copy["keys"]
            existing.update(data_copy)
        else:
            existing.update(data)
        sf.write_text(json.dumps(existing, indent=2), "utf-8")
        _vlog("💾", f"State saved → {sf}")
    except Exception as exc:
        _vlog("⚠️", f"_write_state error: {exc}")


def inject_components_v2(app: FastAPI, **kwargs: Any) -> None:
    """Inject live Phidipus components into v2 endpoints."""
    _components.update(kwargs)
    _write_token_file()

    token_dir = _TOKEN_FILE.parent
    readonly_path = token_dir / ".readonly_token"

    _vlog("🕷️", "SpiderHub v2 — COMMERCIAL RBAC enabled")
    _vlog("🔐", f"Admin token    → {_TOKEN_FILE} (full access: run/approve/config)")
    _vlog("🔐", f"Readonly token → {readonly_path} (stats/health/skills/memory)")
    _vlog("🔄", f"Token TTL: {_TOKEN_TTL_SECONDS//3600}h, auto-rotation every 15 min")
    _vlog("🛡️", "Write endpoints (8): require admin token")
    _vlog("📊", "Read endpoints: accept admin or readonly token")

    # Start background token rotation checker
    try:
        asyncio.ensure_future(_token_rotation_background())
    except RuntimeError:
        pass  # no event loop yet — rotation will start when loop runs


# ══════════════════════════════════════════════════════════════════
# Helper: screen thumbnail
# ══════════════════════════════════════════════════════════════════

async def _capture_screen_thumb(width: int = 1280) -> str | None:
    """Capture screen, resize, return base64 JPEG."""
    try:
        def _do():
            from PIL import ImageGrab, Image
            import io
            img = ImageGrab.grab()
            ratio = width / img.width
            h = int(img.height * ratio)
            img = img.resize((width, h), Image.LANCZOS)
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=65)
            return base64.b64encode(buf.getvalue()).decode()
        return await asyncio.to_thread(_do)
    except Exception as exc:
        _vlog("⚠️", f"Screen capture failed: {str(exc)[:60]}")
        return None


# ══════════════════════════════════════════════════════════════════
# App factory
# ══════════════════════════════════════════════════════════════════

def create_app_v2() -> FastAPI:
    app = FastAPI(
        title="Phidipus Agents — SpiderHub",
        version="9.20",
        docs_url=None,   # Disable swagger UI in production
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        # SEC-04 FIX: Replace allow_origins=["*"] with explicit localhost whitelist.
        # W3C spec forbids allow_credentials=True with wildcard origin.
        # If Cloudflare tunnel is used, add the tunnel domain explicitly here.
        allow_origins=[
            "http://localhost:8912",
            "http://127.0.0.1:8912",
            "http://localhost:3000",    # Vue dev server
            "http://127.0.0.1:3000",
        ],
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Phidipus-Token", "Authorization"],
        allow_credentials=True,
    )
    # v4.3: outermost guard — rejects DNS-rebinding Hosts, cross-site requests
    # and cross-site WebSocket connections before any route runs.
    app.add_middleware(_OriginGuard)

    # ── FIX v1.0: Auth login/logout endpoints ──────────────────
    @app.post("/api/v2/auth/login")
    async def auth_login(request: Request, body: dict = Body(default={})):
        """Login with username/password → JWT session token."""
        ip = request.client.host if request.client else "unknown"
        if not _check_login_rate(ip):
            raise HTTPException(429, "Too many login attempts. Wait 60 seconds.")

        username = body.get("username", "").strip()
        password = body.get("password", "")
        if not username or not password:
            raise HTTPException(400, "username and password required")

        # Record attempt
        _LOGIN_ATTEMPTS.setdefault(ip, []).append(_time_mod.time())

        # Verify credentials (salted scrypt; no built-in default account)
        creds = _load_admin_credentials()
        if not creds:
            raise HTTPException(
                403,
                "Đăng nhập từ xa đang tắt: chưa cấu hình admin_auth trong config.yaml "
                "(tạo hash: python -m admin.passwords)",
            )

        if await asyncio.to_thread(_credentials_match, creds, username, password):
            token = _create_jwt(username, hours=24)
            _vlog("🔐", f"Login OK: {username} from {ip}")
            response = JSONResponse({
                "ok": True, "token": token, "username": username,
                "expires_hours": 24,
            })
            response.set_cookie(
                key="ph_session", value=token,
                max_age=86400, httponly=True, samesite="strict",
                secure=request.url.scheme == "https",
            )
            return response
        else:
            _vlog("⚠️", f"Login FAILED: {username} from {ip}")
            raise HTTPException(401, "Invalid username or password")

    @app.post("/api/v2/auth/logout")
    async def auth_logout(request: Request):
        """Invalidate session."""
        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token:
            token = request.cookies.get("ph_session", "")
        if token:
            _JWT_SESSIONS.pop(token, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("ph_session")
        return response

    @app.get("/api/v2/auth/check")
    async def auth_check(request: Request):
        """Check if current session is valid."""
        if _is_localhost(request):
            return JSONResponse({"ok": True, "user": "localhost", "method": "local"})
        if _verify_jwt(request):
            # Find username from session
            auth_header = request.headers.get("Authorization", "")
            token = auth_header[7:] if auth_header.startswith("Bearer ") else request.cookies.get("ph_session", "")
            session = _JWT_SESSIONS.get(token, {})
            return JSONResponse({"ok": True, "user": session.get("user", "admin"), "method": "jwt"})
        if _verify_token(request):
            return JSONResponse({"ok": True, "user": "token", "method": "token"})
        raise HTTPException(401, "Not authenticated")

    # ── Static files (Vue build output) ──────────────────────────
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    # Mount BOTH /assets and /static/assets (Vite references /assets/xxx.js)
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    _vlog("📂", f"Static: {static_dir} | Assets: {assets_dir} ({len(list(assets_dir.glob('*')))} files)")

    # ── v9.21: Auto-token for local admin panel ──────────────────
    @app.get("/api/v2/auth/local-token")
    async def get_local_token(request: Request):
        """Return admin token for localhost connections (no auth required)."""
        if _is_localhost(request):
            return JSONResponse({"token": _ADMIN_TOKEN})
        raise HTTPException(403, "Only localhost")

    # ── SPA entry (serve index.html for all unmatched routes) ────
    # v9.21: Inject XHR interceptor into HTML so Vue app auto-authenticates
    # without needing to rebuild the frontend.

    def _inject_token(html: str) -> str:
        """Inject auth token into HTML so all XHR requests auto-authenticate."""
        inject = (
            '<script>'
            'window.__BEEHIVE_TOKEN__="' + _ADMIN_TOKEN + '";'
            '(function(){'
            '  var t=window.__BEEHIVE_TOKEN__;'
            '  if(!t)return;'
            '  var _open=XMLHttpRequest.prototype.open;'
            '  XMLHttpRequest.prototype.open=function(){'
            '    this.__spiderReady=true;'
            '    return _open.apply(this,arguments);'
            '  };'
            '  var _send=XMLHttpRequest.prototype.send;'
            '  XMLHttpRequest.prototype.send=function(){'
            '    if(this.__spiderReady){'
            '      try{this.setRequestHeader("X-Phidipus-Token",t);}catch(e){}'
            '    }'
            '    return _send.apply(this,arguments);'
            '  };'
            '})();'
            '</script>'
        )
        return html.replace("</head>", inject + "</head>", 1)

    @app.get("/")
    async def serve_spa():
        """D2 v9.27: Priority → dist/ → admin_panel.html → static/"""
        dist_idx = Path(__file__).parent / "dist" / "index.html"
        if dist_idx.exists():
            html = dist_idx.read_text(encoding="utf-8")
            return HTMLResponse(_inject_token(html))
        panel = Path(__file__).parent / "admin_panel.html"
        if panel.exists():
            html = panel.read_text(encoding="utf-8")
            return HTMLResponse(_inject_token(html))
        index = Path(__file__).parent / "static" / "index.html"
        if index.exists():
            html = index.read_text(encoding="utf-8")
            return HTMLResponse(_inject_token(html))
        return HTMLResponse(_get_fallback_html())
    @app.websocket("/api/v2/ws")
    async def ws_endpoint(ws: WebSocket):
        # [C-04 FIX] Verify token — localhost auto-passes
        client = ws.client
        is_local = _is_local_peer(client.host if client else None, ws.headers)
        if not is_local:
            # SEC-05 FIX: read token from header only, never from query params
            token = ws.headers.get("X-Phidipus-Token", "")
            if not secrets.compare_digest(token, _ADMIN_TOKEN):
                await ws.close(code=4401, reason="Unauthorized")
                _vlog("🛡️", "[C-04] WS connection rejected: invalid token")
                return
        connected = await _ws_manager.connect(ws)
        if not connected:
            return
        try:
            # Send initial state snapshot
            await ws.send_text(json.dumps({
                "event": "connected",
                "data": {
                    "server": "SpiderHub v2",
                    "shadow_enabled": _shadow.enabled,
                    "ws_clients": _ws_manager.count,
                },
                "ts": time.time(),
            }))

            # Heartbeat + push loop
            while True:
                try:
                    # Non-blocking check for incoming messages
                    data = await asyncio.wait_for(ws.receive_text(), timeout=3.0)
                    msg = json.loads(data)
                    # Handle ping
                    if msg.get("type") == "ping":
                        await ws.send_text(json.dumps({"event": "pong", "ts": time.time()}))
                    # Handle task submission from God Button
                    elif msg.get("type") == "run_task":
                        goal = msg.get("goal", "")
                        agent = _components.get("agent_loop")
                        if agent and goal:
                            # [M-01 FIX] Rate limit per WS client
                            ws_id = str(id(ws))
                            now = time.time()
                            _ws_task_last.setdefault(ws_id, [])
                            _ws_task_last[ws_id] = [t for t in _ws_task_last[ws_id] if now - t < 60]
                            if len(_ws_task_last[ws_id]) >= _WS_RATE_LIMIT:
                                await ws.send_text(json.dumps({
                                    "event": "error",
                                    "data": {"message": f"Rate limit: max {_WS_RATE_LIMIT} tasks/minute"},
                                }))
                            else:
                                _ws_task_last[ws_id].append(now)
                                # [C-05 FIX] Semaphore via _run_task_bg
                                asyncio.create_task(_run_task_bg(goal, ws))
                        else:
                            await ws.send_text(json.dumps({
                                "event": "error",
                                "data": {"message": "Agent chưa sẵn sàng"},
                            }))
                except asyncio.TimeoutError:
                    # [M-09 FIX] Use cached stats to avoid blocking event loop
                    stats = _get_cached_stats()
                    await ws.send_text(json.dumps({
                        "event": "heartbeat",
                        "data": stats,
                        "ts": time.time(),
                    }))
        except WebSocketDisconnect:
            _ws_manager.disconnect(ws)
        except Exception as exc:
            _vlog("⚠️", f"WS error: {str(exc)[:60]}")
            _ws_manager.disconnect(ws)

    # ── Screen thumbnail ──────────────────────────────────────────

    @app.get("/api/v2/screen/thumb")
    async def screen_thumb(
        request: Request,
        width: int = Query(default=1280, ge=100, le=4096),
        _auth: None = Depends(_auth_dep),
    ):
        b64 = await _capture_screen_thumb(width)
        if b64:
            return JSONResponse({"ok": True, "image": b64, "width": width, "ts": time.time()})
        return JSONResponse({"ok": False, "image": None})

    # ── Shadow Mode toggle ────────────────────────────────────────

    @app.post("/api/v2/shadow/toggle")
    async def shadow_toggle(request: Request, body: dict = Body(default={}), _auth: None = Depends(_admin_dep)):
        privacy = body.get("privacy", "medium")
        enabled = _shadow.toggle(privacy)
        await _ws_manager.broadcast("shadow_state", {
            "enabled": enabled,
            "privacy": privacy,
        })
        return JSONResponse({
            "ok": True,
            "enabled": enabled,
            "privacy": privacy,
            "segments": len(_shadow.segments),
        })

    @app.get("/api/v2/shadow/state")
    async def shadow_state(request: Request, _auth: None = Depends(_auth_dep)):
        return JSONResponse({
            "enabled": _shadow.enabled,
            "privacy": _shadow.privacy_level,
            "segments": [
                {
                    "id": s["id"],
                    "goal": s["goal"],
                    "duration_s": s.get("duration_s", 0),
                    "success": s.get("success", False),
                    "action_count": len(s.get("actions", [])),
                    "started_at": s.get("started_at", 0),
                }
                for s in reversed(_shadow.segments[-20:])
            ],
            "heatmap_points": _shadow.heatmap_points[-500:],
            "learn_insights": _shadow.get_learn_insights(),
        })

    # ── Shadow replay ─────────────────────────────────────────────

    @app.get("/api/v2/shadow/replay/{segment_id}")
    async def shadow_replay(segment_id: str, request: Request, _auth: None = Depends(_auth_dep)):
        seg = _shadow.get_replay(segment_id)
        if not seg:
            raise HTTPException(404, f"Segment '{segment_id}' not found")
        return JSONResponse(seg)

    # ── Task graph ────────────────────────────────────────────────

    @app.get("/api/v2/task/graph")
    async def task_graph(request: Request, _auth: None = Depends(_auth_dep)):
        """Return Cytoscape-compatible task graph JSON."""
        agent = _components.get("agent_loop")
        nodes, edges = [], []

        # Get active plan from workflow library
        wf_lib = getattr(agent, "_workflow_lib", None) if agent else None
        if wf_lib:
            stats = wf_lib.stats()
            for tmpl_info in stats.get("top_workflows", []):
                nodes.append({
                    "data": {
                        "id": tmpl_info["id"],
                        "label": tmpl_info["name"][:30],
                        "type": "workflow",
                        # FIX BUG#3: workflow nodes need a status so STATUS_COLORS resolves correctly
                        "status": "workflow",
                        "score": tmpl_info.get("score", 0),
                        "success_count": tmpl_info.get("success_count", 0),
                        "fail_count": tmpl_info.get("fail_count", 0),
                    }
                })

        # Current task plan from checkpoint manager
        checkpoint_mgr = getattr(agent, "_checkpoint", None) if agent else None
        if checkpoint_mgr:
            incomplete = checkpoint_mgr.find_incomplete()
            for cp in incomplete[:5]:
                plan = cp.get("plan", {})
                tasks = plan.get("tasks", [])
                for i, t in enumerate(tasks):
                    tid = t.get("id", f"T{i}")
                    nodes.append({
                        "data": {
                            "id": f"cp_{tid}",
                            "label": t.get("description", tid)[:30],
                            "type": "task",
                            "status": t.get("status", "pending"),
                            # FIX BUG#E: expose richer fields for Node Detail Panel
                            "action": t.get("action", ""),
                            "duration_ms": t.get("duration_ms", 0),
                            "error": t.get("error", ""),
                            "healing_strategy": t.get("healing_strategy", ""),
                        }
                    })
                    for dep in t.get("depends_on", []):
                        edges.append({
                            "data": {
                                "id": f"e_{dep}_{tid}",
                                "source": f"cp_{dep}",
                                "target": f"cp_{tid}",
                            }
                        })

        return JSONResponse({
            "nodes": nodes,
            "edges": edges,
            "layout": "dagre",
            "ts": time.time(),
        })

    # ── Chrome profiles status ────────────────────────────────────

    @app.get("/api/v2/chrome/profiles/status")
    async def chrome_profiles_status(request: Request, _auth: None = Depends(_auth_dep)):
        scanner = _components.get("app_scanner")
        if not scanner:
            return JSONResponse({"profiles": [], "total": 0})

        try:
            raw = scanner.list_chrome_profiles() if hasattr(scanner, "list_chrome_profiles") else []
        except Exception:
            raw = []

        profiles = []
        for p in raw:
            profiles.append({
                "name": p.get("name", ""),
                "directory": p.get("directory", ""),
                "avatar": p.get("avatar", ""),
                "status": "available",
            })

        return JSONResponse({
            "profiles": profiles,
            "total": len(profiles),
            "ts": time.time(),
        })

    @app.post("/api/v2/chrome/profiles/open")
    async def chrome_profile_open(request: Request,
                                  body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        """Open Chrome with a specific profile — direct subprocess, no Agent needed."""
        import subprocess
        profile_name = body.get("name", "").strip()
        profile_dir = body.get("directory", "").strip()
        if not profile_name and not profile_dir:
            raise HTTPException(400, "name or directory required")

        # Find profile directory from name if not provided
        if not profile_dir:
            scanner = _components.get("app_scanner")
            if scanner and hasattr(scanner, "list_chrome_profiles"):
                for p in scanner.list_chrome_profiles():
                    if p.get("name", "") == profile_name:
                        profile_dir = p.get("directory", "")
                        break
        if not profile_dir:
            raise HTTPException(404, f"Profile '{profile_name}' not found")

        try:
            # macOS: open Chrome with --profile-directory flag
            cmd = [
                "open", "-na", "Google Chrome", "--args",
                f"--profile-directory={profile_dir}"
            ]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _vlog("🌐", f"Chrome opened: {profile_name} ({profile_dir})")
            return JSONResponse({"ok": True, "name": profile_name, "directory": profile_dir})
        except Exception as exc:
            raise HTTPException(500, f"Failed to open Chrome: {str(exc)[:200]}")

    # ── Skill Forge radar ─────────────────────────────────────────

    @app.get("/api/v2/skill/forge/radar")
    async def skill_forge_radar(request: Request, _auth: None = Depends(_auth_dep)):
        """Return provider stack status + SAFETY events for radar chart."""
        agent = _components.get("agent_loop")
        forge = getattr(agent, "_forge", None) if agent else None

        providers = []
        safety_events = []

        if forge:
            stack = forge._fallback_stack
            if stack:
                for p in stack._providers:
                    providers.append({
                        "name": p.name,
                        "kind": p.kind,
                        "temperature": p.temperature,
                        "has_key": bool(p.api_key),
                        "calls": 0,
                        "success_rate": 1.0,
                    })

            fm = getattr(agent, "_failure_memory", None)
            if fm:
                for rec in getattr(fm, "_records", [])[-20:]:
                    if "safety" in str(rec.error_message).lower():
                        safety_events.append({
                            "t": rec.timestamp,
                            "provider": rec.context.get("provider", "unknown"),
                            "goal": rec.goal[:40],
                        })

        return JSONResponse({
            "providers": providers,
            "safety_events": safety_events[-5:],
            "cached_skills": len(getattr(forge, "_cache", {})) if forge else 0,
            "ts": time.time(),
        })

    # ── Semantic memory search ────────────────────────────────────

    @app.post("/api/v2/memory/semantic/search")
    async def semantic_search(request: Request, body: dict = Body(default={}), _auth: None = Depends(_auth_dep)):
        query = str(body.get("query", "")).strip()
        if not query:
            raise HTTPException(400, "query required")
        if len(query) > 500:
            raise HTTPException(400, "query too long (max 500 chars)")

        agent = _components.get("agent_loop")
        semantic = getattr(agent, "_semantic", None) if agent else None
        results = []

        if semantic and hasattr(semantic, "search"):
            try:
                raw = semantic.search(query, top_k=10)
                results = [{"text": r.get("text", ""), "score": r.get("score", 0)} for r in raw]
            except Exception:
                pass
        elif semantic and hasattr(semantic, "get_context_for"):
            ctx = semantic.get_context_for(query) or ""
            if ctx:
                results = [{"text": ctx, "score": 1.0}]

        return JSONResponse({"query": query, "results": results, "count": len(results)})

    # ── God reset ────────────────────────────────────────────────

    @app.post("/api/v2/system/god-reset")
    async def god_reset(request: Request, body: dict = Body(default={}), _auth: None = Depends(_admin_dep)):
        confirm = body.get("confirm", False)
        if not confirm:
            return JSONResponse({"ok": False, "message": "confirm=true required"})

        reset_log = []
        agent = _components.get("agent_loop")

        if agent:
            if hasattr(agent, "_task_memory"):
                try:
                    agent._task_memory.clear()
                    reset_log.append("task_memory cleared")
                except Exception:
                    pass
            _shadow.segments.clear()
            _shadow.heatmap_points.clear()
            _shadow.learn_buffer.clear()
            reset_log.append("shadow mode buffer cleared")

        await _ws_manager.broadcast("god_reset", {"log": reset_log, "ts": time.time()})
        _vlog("⚡", f"God Reset executed: {len(reset_log)} actions")
        return JSONResponse({"ok": True, "actions": reset_log})

    # ════════════════════════════════════════════════════════════
    # v2 supplementary endpoints
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/system/health")
    async def system_health(request: Request, _auth: None = Depends(_auth_dep)):
        """
        A1 v9.24: Health endpoint với gemini_configured flag.
        Admin Panel dùng để hiển thị badge Vision mode.
        """
        cfg_raw = _components.get("config_raw", {})
        sf = cfg_raw.get("skill_forge", {})
        gemini_key = sf.get("gemini_api_key", "")
        gemini_ok = bool(gemini_key and len(str(gemini_key).strip()) > 8)

        agent = _components.get("agent_loop")
        return JSONResponse({
            "status": "running",
            "version": "v9.24",
            "gemini_configured": gemini_ok,
            "vision_mode": "gemini-2.5-flash" if gemini_ok else "ollama-vision",
            "vision_speed": "~1.5s" if gemini_ok else "~15-25s",
            "agent_running": agent is not None,
            "timestamp": __import__("time").time(),
        })

    @app.get("/api/v2/system/stats")
    async def system_stats(request: Request, _auth: None = Depends(_auth_dep)):
        return JSONResponse(_collect_realtime_stats())

    @app.get("/api/v2/workflow/library")
    async def workflow_library(request: Request, _auth: None = Depends(_auth_dep)):
        agent = _components.get("agent_loop")
        wf = getattr(agent, "_workflow_lib", None) if agent else None
        if not wf:
            return JSONResponse({"templates": [], "cache": []})
        return JSONResponse({
            "templates": wf.list_templates(),
            "cache": wf.list_cache(top_n=20),
            "stats": wf.stats(),
        })

    @app.post("/api/v2/workflow/protect/{template_id}")
    async def protect_template(template_id: str, request: Request, _auth: None = Depends(_admin_dep)):
        agent = _components.get("agent_loop")
        wf = getattr(agent, "_workflow_lib", None) if agent else None
        if not wf:
            raise HTTPException(503, "Workflow Library not available")
        ok = wf.protect_template(template_id)
        return JSONResponse({"ok": ok, "template_id": template_id})

    @app.post("/api/v2/workflow/prune")
    async def trigger_prune(request: Request, _auth: None = Depends(_admin_dep)):
        agent = _components.get("agent_loop")
        wf = getattr(agent, "_workflow_lib", None) if agent else None
        if not wf:
            raise HTTPException(503, "Workflow Library not available")
        report = wf.run_prune(force=True)
        return JSONResponse({"ok": True, "report": report})

    @app.get("/api/v2/skill/registry")
    async def skill_registry(request: Request, _auth: None = Depends(_auth_dep)):
        agent = _components.get("agent_loop")
        db = getattr(agent, "_skill_db", None) if agent else None
        builtin_skills = []
        if db:
            builtin_skills = [
                {
                    "id": s.id[:12],
                    "name": s.name,
                    "reliability": round(s.reliability_score, 2),
                    "usage_count": s.usage_count,
                    "source": s.source,
                    "version": s.version,
                    "type": "builtin",
                }
                for s in db.list_active()
            ]
        # FIX v1.0: Include OpenClaw installed skills
        oc_skills = []
        try:
            from core.openclaw_bridge import get_installed_skills
            for sk in get_installed_skills():
                if sk.get("enabled", True):
                    oc_skills.append({
                        "id": sk.get("name", "")[:12],
                        "name": sk.get("display_name", sk.get("name", "")),
                        "description": sk.get("description", ""),
                        "reliability": sk.get("security_score", 0) / 100.0,
                        "usage_count": 0,
                        "source": "openclaw",
                        "version": sk.get("version", "1.0.0"),
                        "type": "openclaw",
                    })
        except Exception:
            pass
        all_skills = builtin_skills + oc_skills
        return JSONResponse({
            "skills": all_skills,
            "stats": db.stats() if db else {},
            "openclaw_count": len(oc_skills),
        })

    # ── [FIX #2] Skill human-approval endpoints ───────────────────────────

    @app.get("/api/v2/skill/pending")
    async def skill_pending(request: Request, _auth: None = Depends(_auth_dep)):
        """[FIX #2] List skills awaiting human admin approval."""
        agent = _components.get("agent_loop")
        registry = getattr(agent, "_skill_registry", None) if agent else None
        if not registry or not hasattr(registry, "list_pending_approval"):
            return JSONResponse({"pending": [], "count": 0})
        try:
            pending = registry.list_pending_approval()
            return JSONResponse({
                "pending": [
                    {
                        "name":       s.name,
                        "description": s.description,
                        "skill_path": s.skill_path,
                        "sig_path":   s.sig_path,
                        "quarantined": s.quarantined,
                    }
                    for s in pending
                ],
                "count": len(pending),
            })
        except Exception as exc:
            raise HTTPException(500, f"Error listing pending skills: {exc}")

    @app.post("/api/v2/skill/approve/{skill_name}")
    async def skill_approve(
        skill_name: str,
        request: Request,
        _auth: None = Depends(_admin_dep),
    ):
        """
        [FIX #2] Admin approves a pending skill for execution.

        Clears pending_human_approval and records audit trail (who, when).
        The skill still needs to run once successfully to exit quarantine.

        Security: requires valid X-Phidipus-Token (auth gate).
        """
        agent = _components.get("agent_loop")
        registry = getattr(agent, "_skill_registry", None) if agent else None
        if not registry or not hasattr(registry, "approve"):
            raise HTTPException(503, "Skill Registry not available")
        try:
            # Record admin identifier from request context
            token = request.headers.get("X-Phidipus-Token", "")[:8]
            approved_by = f"admin_panel:{token}"
            registry.approve(skill_name, approved_by=approved_by)
            return JSONResponse({
                "ok": True,
                "skill_name": skill_name,
                "approved_by": approved_by,
                "message": f"Skill '{skill_name}' approved. Still in quarantine until first successful run.",
            })
        except Exception as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/v2/skill/revoke/{skill_name}")
    async def skill_revoke(
        skill_name: str,
        request: Request,
        _auth: None = Depends(_admin_dep),
    ):
        """[FIX #2] Admin revokes a skill's approval and re-quarantines it."""
        agent = _components.get("agent_loop")
        registry = getattr(agent, "_skill_registry", None) if agent else None
        if not registry or not hasattr(registry, "revoke_approval"):
            raise HTTPException(503, "Skill Registry not available")
        try:
            registry.revoke_approval(skill_name)
            return JSONResponse({
                "ok": True,
                "skill_name": skill_name,
                "message": f"Approval for '{skill_name}' revoked. Skill is quarantined.",
            })
        except Exception as exc:
            raise HTTPException(400, str(exc))

    # ── [FIX #6] Trust boundary documentation & hardening ─────────────────
    # TRUST MODEL: Phidipus runs as a LOCAL DESKTOP APP.
    # The admin panel binds ONLY to 127.0.0.1 — no external network exposure.
    # Same-user processes can reach 127.0.0.1:8912, but they already have
    # equivalent privileges (same UID = same filesystem access).
    #
    # Defense layers (innermost to outermost):
    #   1. Token auth (64-char hex, 8h TTL, rotation) — every endpoint
    #   2. Token stored in ~/.config/phidipus/.admin_token (0o600, dir 0o700)
    #   3. Localhost-only binding — no external exposure
    #   4. CORS whitelist — no cross-origin requests
    #   5. Queue cap — prevents DoS from compromised local process
    #
    # Residual risk: a compromised process running as THE SAME USER can:
    #   a) Read ~/.config/phidipus/.admin_token and obtain the token
    #   b) Call 127.0.0.1:8912 with full admin access
    # This is ACCEPTED — if same-user code executes, the attacker already
    # has equivalent access to all user files. mTLS or OS-level sandbox
    # (macOS App Sandbox) would be required to close this gap fully.
    #
    # The token file MUST NOT be in /tmp — world-readable tmpfs makes it
    # accessible to any process on the system (multi-user machines).

    @app.get("/api/v2/trust/boundary")
    async def trust_boundary_info(request: Request, _auth: None = Depends(_auth_dep)):
        """[FIX #6] Returns current trust boundary configuration for audit."""
        import os
        token_path = str(_TOKEN_FILE)
        token_exists = _TOKEN_FILE.exists()
        token_stat = None
        if token_exists:
            try:
                st = os.stat(token_path)
                token_stat = {
                    "mode": oct(st.st_mode & 0o777),
                    "uid":  st.st_uid,
                    "size": st.st_len if hasattr(st, "st_len") else st.st_size,
                }
            except Exception:
                pass
        return JSONResponse({
            "binding":         "127.0.0.1:8912",
            "external_access": False,
            "token_path":      token_path,
            "token_in_tmp":    token_path.startswith("/tmp"),  # should be False
            "token_exists":    token_exists,
            "token_stat":      token_stat,
            "token_ttl_hours": _TOKEN_TTL_SECONDS // 3600,
            "residual_risk":   "same-uid process can read token file",
            "mitigation":      "localhost-only + 0o600 token file + queue cap",
        })

    @app.get("/api/v2/tokens")
    async def get_tokens(request: Request, _auth: None = Depends(_admin_dep)):
        """
        COMMERCIAL-RBAC: Return both tokens for initial setup.
        Admin-only endpoint — requires admin token (catch-22 resolved by token files).

        First-time setup: read tokens from ~/.config/phidipus/.admin_token
        and ~/.config/phidipus/.readonly_token directly.

        Returns token values so UI / CI/CD can display them.
        """
        token_dir = _TOKEN_FILE.parent
        readonly_path = token_dir / ".readonly_token"
        age_seconds = int(_time_mod.time() - _TOKEN_CREATED_AT)
        return JSONResponse({
            "admin_token":    _ADMIN_TOKEN,
            "readonly_token": _READONLY_TOKEN,
            "admin_path":     str(_TOKEN_FILE),
            "readonly_path":  str(readonly_path),
            "token_ttl_hours": _TOKEN_TTL_SECONDS // 3600,
            "token_age_minutes": age_seconds // 60,
            "expires_in_minutes": max(0, (_TOKEN_TTL_SECONDS - age_seconds) // 60),
            "rbac": {
                "admin_endpoints": [
                    "POST /api/v2/shadow/toggle",
                    "POST /api/v2/system/god-reset",
                    "POST /api/v2/workflow/protect/{id}",
                    "POST /api/v2/workflow/prune",
                    "POST /api/v2/skill/approve/{name}",
                    "POST /api/v2/skill/revoke/{name}",
                    "POST /api/v2/task/run",
                    "POST /api/v2/config/shadow",
                ],
                "readonly_endpoints": "all GET endpoints",
            },
        })

    @app.get("/api/v2/telemetry/summary")
    async def telemetry_summary(
        request: Request,
        date: str = "",
        _auth: None = Depends(_auth_dep),
    ):
        """
        COMMERCIAL: Daily telemetry summary.
        date param: YYYY-MM-DD (default today).
        Readonly token sufficient — safe to share with investors/dashboards.
        """
        from utils.telemetry import get_telemetry
        tel = get_telemetry()
        tel.flush()
        summary = tel.daily_summary(date if date else None)
        return JSONResponse(summary)

    @app.get("/api/v2/telemetry/weekly")
    async def telemetry_weekly(request: Request, _auth: None = Depends(_auth_dep)):
        """
        COMMERCIAL: Last-7-days aggregated telemetry for investor dashboards.
        Readonly token sufficient.
        """
        from utils.telemetry import get_telemetry
        tel = get_telemetry()
        tel.flush()
        return JSONResponse(tel.weekly_summary())

    @app.get("/api/v2/memory/failure")
    async def failure_memory(request: Request, _auth: None = Depends(_auth_dep)):
        agent = _components.get("agent_loop")
        fm = getattr(agent, "_failure_memory", None) if agent else None
        if not fm:
            return JSONResponse({"records": [], "stats": {}})
        stats = fm.stats()
        recent = [
            {
                "error_type": r.error_type,
                "fix": r.fix_strategy[:60],
                "success": r.fix_success,
                "goal": r.goal[:40],
                "t": r.timestamp,
            }
            for r in getattr(fm, "_records", [])[-20:]
        ]
        return JSONResponse({"recent": list(reversed(recent)), "stats": stats})

    @app.get("/api/v2/resource/history")
    async def resource_history(request: Request, _auth: None = Depends(_auth_dep)):
        agent = _components.get("agent_loop")
        rg = getattr(agent, "_resource_guard", None) if agent else None
        if not rg:
            return JSONResponse({"history": [], "current": {}})
        return JSONResponse({
            "history": rg.recent_history(n=60),
            "current": rg.stats(),
        })

    @app.post("/api/v2/task/run")
    async def run_task(request: Request, body: dict = Body(default={}), _auth: None = Depends(_admin_dep)):
        goal = body.get("goal", "").strip()
        if not goal:
            raise HTTPException(400, "goal required")
        agent = _components.get("agent_loop")
        if not agent:
            raise HTTPException(503, "Agent chưa sẵn sàng")
        # SEC-R11: Enforce queue cap on HTTP path too
        if _pending_task_count >= _MAX_PENDING_TASKS:
            raise HTTPException(429, f"Queue đầy ({_MAX_PENDING_TASKS} tasks). Thử lại sau.")
        task_id = str(uuid.uuid4())
        asyncio.create_task(_run_task_bg(goal, None))
        return JSONResponse({"ok": True, "task_id": task_id, "goal": goal})

    @app.get("/api/v2/config")
    async def get_config(request: Request, _auth: None = Depends(_auth_dep)):
        from admin.spider_hub_config import get_config
        cfg = get_config()
        return JSONResponse(cfg.to_dict())

    # ════════════════════════════════════════════════════════════
    # v9.21: Forensic Replay API
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/forensic/episodes")
    async def get_forensic_episodes(request: Request, _auth: None = Depends(_auth_dep)):
        """Get recent task episodes for forensic replay."""
        agent = _components.get("agent_loop")
        episodes = []
        try:
            ep_mem = getattr(agent, '_episodic', None) if agent else None
            if ep_mem:
                raw = ep_mem.list_episodes(limit=20)
                for ep in raw:
                    steps = []
                    for i, s in enumerate(ep.get("steps", [])):
                        steps.append({
                            "id": s.get("id", f"step_{i}"),
                            "index": i,
                            "timestamp": s.get("timestamp", ""),
                            "status": s.get("status", "done"),
                            "action": s.get("action", ""),
                            "description": s.get("description", ""),
                            "thought": s.get("thought", ""),
                            "observation": s.get("observation", ""),
                            "error": s.get("error", ""),
                            "duration_ms": s.get("duration_ms", 0),
                            "screenshot_url": "",
                            "dom_diagnostics": s.get("dom", None),
                            "healing": s.get("healing", None),
                            "replan": s.get("replan", None),
                        })
                    episodes.append({
                        "task_id": ep.get("task_id", ep.get("id", "")),
                        "goal": ep.get("goal", ""),
                        "started_at": ep.get("started_at", ""),
                        "finished_at": ep.get("finished_at", ""),
                        "success": ep.get("success", False),
                        "total_steps": len(steps),
                        "steps": steps,
                    })
        except Exception:
            pass
        if not episodes:
            try:
                history = getattr(agent, '_task_history', []) if agent else []
                for entry in list(history)[-20:]:
                    episodes.append({
                        "task_id": entry.get("task_id", ""),
                        "goal": entry.get("goal", ""),
                        "started_at": entry.get("started_at", ""),
                        "finished_at": "",
                        "success": entry.get("success", False),
                        "total_steps": entry.get("steps", 0),
                        "steps": [],
                    })
            except Exception:
                pass
        return JSONResponse({"episodes": episodes})

    # ════════════════════════════════════════════════════════════
    # v9.21: Provider Management API
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/providers")
    async def get_providers(request: Request, _auth: None = Depends(_auth_dep)):
        """Get all LLM providers with health status."""
        # Try from live agent first
        agent = _components.get("agent_loop")
        if agent:
            try:
                forge = getattr(agent, '_forge', None)
                if forge and hasattr(forge, '_get_stack'):
                    stack = forge._get_stack()
                    if stack:
                        return JSONResponse(stack.stats())
            except Exception as exc:
                _vlog("⚠️", f"Provider stats error: {exc}")

        # Fallback: build from config.yaml FILE
        try:
            raw = _read_config_yaml()
            sf = raw.get("skill_forge", {})
            if sf:
                from core.llm_fallback import build_provider_stack, FallbackStack
                prov_list = build_provider_stack(
                    gemini_api_key=sf.get('gemini_api_key', ''),
                    gemini_model=sf.get('gemini_model', 'gemini-2.5-flash'),
                    mistral_api_key=sf.get('mistral_api_key', ''),
                    cerebras_api_key=sf.get('cerebras_api_key', ''),
                    openrouter_api_key=sf.get('openrouter_api_key', ''),
                    ollama_base_url=sf.get('ollama_base_url', 'http://127.0.0.1:11434'),
                    ollama_coder_model=sf.get('ollama_coder_model', 'qwen2.5-coder:7b'),
                )
                fs = FallbackStack(prov_list)
                # Add custom providers from config
                from core.llm_fallback import Provider
                for cp in raw.get("custom_providers", []):
                    fs.add_provider(Provider(
                        name=cp.get("name", ""),
                        kind="custom",
                        model=cp.get("model", ""),
                        temperature=0.2,
                        timeout_s=30,
                        api_key=cp.get("api_key", ""),
                        base_url=cp.get("base_url", ""),
                        enabled=True,
                    ))
                return JSONResponse(fs.stats())
        except Exception as exc2:
            _vlog("⚠️", f"Provider fallback error: {exc2}")

        return JSONResponse({"providers": [], "error": "No providers available"})

    @app.get("/api/v2/providers/logs")
    async def get_provider_logs(request: Request, _auth: None = Depends(_auth_dep)):
        """Get recent provider call logs (max 20)."""
        agent = _components.get("agent_loop")
        if agent:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                if stack and hasattr(stack, 'get_logs'):
                    return JSONResponse({"logs": stack.get_logs()})
        return JSONResponse({"logs": []})

    @app.get("/api/v2/config/export")
    async def export_config(request: Request, _auth: None = Depends(_admin_dep)):
        """Export full config.yaml as JSON (for backup)."""
        raw = _read_config_yaml()
        if not raw:
            raise HTTPException(404, "config.yaml not found")
        return JSONResponse({"config": raw, "exported_at": time.time()})

    @app.post("/api/v2/config/import")
    async def import_config(request: Request, body: dict = Body(default={}),
                           _auth: None = Depends(_admin_dep)):
        """Import config from JSON (restore backup)."""
        import yaml
        cfg_data = body.get("config", {})
        if not cfg_data:
            raise HTTPException(400, "config data required")
        cp = _components.get("config_path", "")
        config_path = Path(cp) if cp else Path("config.yaml")
        if not config_path.parent.exists():
            config_path = Path(__file__).parent.parent / "config.yaml"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            _vlog("📦", f"Config imported → {config_path}")
            return JSONResponse({"ok": True, "path": str(config_path)})
        except Exception as exc:
            raise HTTPException(500, f"Import failed: {str(exc)[:200]}")

    @app.post("/api/v2/providers/optimize")
    async def optimize_providers(request: Request, _auth: None = Depends(_admin_dep)):
        """AI Assistant: analyze provider stats and suggest optimizations using Gemini."""
        import urllib.request
        # Collect current stats
        agent = _components.get("agent_loop")
        stats_data = []
        if agent:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                if stack:
                    for p_stat in stack.stats().get("providers", []):
                        stats_data.append(p_stat)
        if not stats_data:
            # Fallback from config
            raw = _read_config_yaml()
            sf = raw.get("skill_forge", {})
            stats_data = [
                {"name": "gemini-2.5-flash", "has_key": bool(sf.get("gemini_api_key")), "total_calls": 0, "avg_latency_ms": 0, "success_rate": 1.0},
                {"name": "mistral-small", "has_key": bool(sf.get("mistral_api_key")), "total_calls": 0, "avg_latency_ms": 0, "success_rate": 1.0},
                {"name": "cerebras-llama3.1-8b", "has_key": bool(sf.get("cerebras_api_key")), "total_calls": 0, "avg_latency_ms": 0, "success_rate": 1.0},
            ]

        # Get Gemini API key
        raw = _read_config_yaml()
        gemini_key = raw.get("skill_forge", {}).get("gemini_api_key", "")
        if not gemini_key:
            return JSONResponse({"suggestion": "⚠️ Cần Gemini API key để sử dụng AI Assistant. Thêm key trong phần API Keys ở trên.", "actions": []})

        # Build prompt
        prompt = f"""Bạn là AI Assistant cho Phidipus Agents LLM Router. Phân tích stack providers sau và đưa gợi ý tối ưu bằng tiếng Việt, ngắn gọn (max 200 từ).

Provider Stack hiện tại:
{json.dumps(stats_data, indent=2, ensure_ascii=False)}

Hãy phân tích:
1. Thứ tự ưu tiên có hợp lý không? Provider nào nên lên P0?
2. Provider nào có latency cao hoặc success rate thấp?
3. Gợi ý cụ thể: reorder, disable provider yếu, thêm provider mới (Groq miễn phí, DeepSeek rẻ)?
4. Nếu chưa có đủ data (0 calls), gợi ý chạy test.

Trả lời dạng markdown ngắn gọn."""

        try:
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
            payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
            req = urllib.request.Request(url, data=payload, method="POST",
                                         headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key})
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            text = result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if not text:
                text = "Không nhận được phản hồi từ Gemini."
            return JSONResponse({"suggestion": text, "provider_count": len(stats_data)})
        except Exception as exc:
            return JSONResponse({"suggestion": f"⚠️ Lỗi gọi Gemini: {str(exc)[:150]}\n\nGợi ý thủ công: Đặt provider có latency thấp nhất lên P0, disable provider fail liên tục.", "actions": []})

    @app.get("/api/v2/providers/budget")
    async def get_provider_budget(request: Request, _auth: None = Depends(_auth_dep)):
        """Budget guard: quota tracking per provider."""
        # Free tier limits (requests per minute / tokens per day)
        LIMITS = {
            "gemini-2.5-flash": {"rpm": 15, "tpd": 1000000, "tier": "free", "cost_per_1m": 0},
            "gemini-2.5-flash-lite": {"rpm": 15, "tpd": 1000000, "tier": "free", "cost_per_1m": 0},
            "mistral-small": {"rpm": 60, "tpm": 500000, "tier": "free", "cost_per_1m": 0},
            "cerebras-llama3.1-8b": {"rpm": 30, "tpd": 1000000, "tier": "free", "cost_per_1m": 0},
            "openrouter-free": {"rpm": 20, "tpd": 0, "tier": "free", "cost_per_1m": 0},
            "groq": {"rpm": 30, "tpd": 14400, "tier": "free", "cost_per_1m": 0},
            "deepseek": {"rpm": 60, "tpd": 0, "tier": "paid", "cost_per_1m": 0.14},
            "fireworks": {"rpm": 600, "tpd": 0, "tier": "paid", "cost_per_1m": 0.9},
            "together": {"rpm": 60, "tpd": 0, "tier": "free_credit", "cost_per_1m": 0},
        }

        agent = _components.get("agent_loop")
        budget_data = []
        if agent:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                if stack:
                    for p in stack.stats().get("providers", []):
                        name = p["name"]
                        limits = LIMITS.get(name, {"rpm": 0, "tpd": 0, "tier": "unknown", "cost_per_1m": 0})
                        calls = p.get("total_calls", 0)
                        rpm_limit = limits.get("rpm", 0)
                        # Estimate usage percentage (rough: calls today / estimated daily capacity)
                        daily_capacity = rpm_limit * 60 * 8 if rpm_limit else 0  # 8 hours active
                        usage_pct = round(calls / daily_capacity * 100, 1) if daily_capacity else 0
                        est_cost = round(calls * 500 / 1000000 * limits.get("cost_per_1m", 0), 4)  # ~500 tokens per call
                        warning = ""
                        if usage_pct > 80:
                            warning = "⚠️ Sắp hết quota!"
                        elif usage_pct > 50:
                            warning = "📊 Đã dùng > 50%"
                        budget_data.append({
                            "name": name,
                            "tier": limits["tier"],
                            "rpm_limit": rpm_limit,
                            "total_calls": calls,
                            "usage_pct": min(usage_pct, 100),
                            "est_cost_usd": est_cost,
                            "warning": warning,
                        })
        return JSONResponse({"budget": budget_data, "ts": time.time()})

    @app.post("/api/v2/providers/custom/add")
    async def add_custom_provider(request: Request, body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        """Add a custom OpenAI-compatible provider to the fallback stack."""
        name = body.get("name", "").strip()
        base_url = body.get("base_url", "").strip()
        model = body.get("model", "").strip()
        api_key = body.get("api_key", "").strip()
        if not name or not base_url or not model:
            raise HTTPException(400, "name, base_url, model required")

        from core.llm_fallback import Provider
        provider = Provider(
            name=name,
            kind="custom",
            model=model,
            temperature=0.2,
            timeout_s=30,
            api_key=api_key,
            base_url=base_url,
            enabled=True,
        )

        # Add to live stack if available
        agent = _components.get("agent_loop")
        added = False
        if agent:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                if stack:
                    added = stack.add_provider(provider)

        # Save to config.yaml
        raw = _read_config_yaml()
        if "custom_providers" not in raw:
            raw["custom_providers"] = []
        # Remove duplicate
        raw["custom_providers"] = [p for p in raw["custom_providers"] if p.get("name") != name]
        raw["custom_providers"].append({
            "name": name, "base_url": base_url, "model": model, "api_key": api_key,
        })
        try:
            import yaml
            cp = _components.get("config_path", "")
            config_path = Path(cp) if cp else Path("config.yaml")
            if not config_path.exists():
                config_path = Path(__file__).parent.parent / "config.yaml"
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        except Exception:
            pass

        _write_state({"keys": {name: bool(api_key)}})
        _vlog("➕", f"Custom provider added: {name} → {base_url} ({model})")

        # Return updated stack
        if agent and added:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                return JSONResponse(forge._get_stack().stats())
        return JSONResponse({"ok": True, "name": name, "added": added})

    @app.post("/api/v2/providers/custom/remove")
    async def remove_custom_provider(request: Request, body: dict = Body(default={}),
                                     _auth: None = Depends(_admin_dep)):
        """Remove a custom provider from the stack."""
        name = body.get("name", "").strip()
        if not name:
            raise HTTPException(400, "name required")

        agent = _components.get("agent_loop")
        removed = False
        if agent:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                if stack:
                    removed = stack.remove_provider(name)

        # Remove from config.yaml
        raw = _read_config_yaml()
        if "custom_providers" in raw:
            raw["custom_providers"] = [p for p in raw["custom_providers"] if p.get("name") != name]
            try:
                import yaml
                cp = _components.get("config_path", "")
                config_path = Path(cp) if cp else Path("config.yaml")
                if not config_path.exists():
                    config_path = Path(__file__).parent.parent / "config.yaml"
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            except Exception:
                pass

        _vlog("➖", f"Custom provider removed: {name}")
        return JSONResponse({"ok": True, "name": name, "removed": removed})

    @app.get("/api/v2/providers/custom/list")
    async def list_custom_providers(request: Request, _auth: None = Depends(_auth_dep)):
        """List custom providers from config."""
        raw = _read_config_yaml()
        customs = raw.get("custom_providers", [])
        return JSONResponse({"providers": customs})

    @app.get("/api/v2/ollama/models")
    async def list_ollama_models(request: Request, _auth: None = Depends(_auth_dep)):
        """List all Ollama models — 3-layer fallback: HTTP 127.0.0.1 → HTTP localhost → CLI subprocess."""
        import subprocess as _sp

        raw_data: dict | None = None
        ollama_running = False
        debug_errors: list[str] = []

        # ── Layer 1: HTTP to 127.0.0.1:11434 ──────────────────────────
        for _base in ("http://127.0.0.1:11434", "http://localhost:11434"):
            try:
                _req = urllib.request.Request(f"{_base}/api/tags", method="GET")
                with urllib.request.urlopen(_req, timeout=8) as _resp:
                    raw_data = json.loads(_resp.read().decode())
                ollama_running = True
                break
            except Exception as _e:
                debug_errors.append(f"HTTP {_base}: {str(_e)[:80]}")

        # ── Layer 2: CLI fallback — `ollama list` ──────────────────────
        # Works even when HTTP binding has IPv4/IPv6 mismatch on macOS
        if raw_data is None:
            try:
                result = _sp.run(
                    ["ollama", "list"],
                    capture_output=True, text=True, timeout=8
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Parse "ollama list" text output → synthetic model list
                    # Format: NAME  ID  SIZE  MODIFIED
                    cli_models = []
                    lines = result.stdout.strip().splitlines()
                    for line in lines[1:]:  # skip header
                        parts = line.split()
                        if not parts:
                            continue
                        name = parts[0]
                        # Try to parse size (e.g. "4.9 GB" or "4.9GB")
                        size_gb = 0.0
                        for i, p in enumerate(parts):
                            try:
                                if p.replace(".", "").isdigit():
                                    if i + 1 < len(parts) and parts[i+1].upper() in ("GB", "MB"):
                                        mult = 1.0 if parts[i+1].upper() == "GB" else 0.001
                                        size_gb = round(float(p) * mult, 1)
                                elif p.upper().endswith("GB"):
                                    size_gb = round(float(p[:-2]), 1)
                                elif p.upper().endswith("MB"):
                                    size_gb = round(float(p[:-2]) / 1024, 2)
                            except Exception:
                                pass
                        cli_models.append({
                            "name": name,
                            "size": int(size_gb * 1024**3),
                            "details": {"family": "", "parameter_size": "", "quantization_level": ""},
                        })
                    raw_data = {"models": cli_models}
                    ollama_running = True
                    debug_errors.append("HTTP failed — used CLI fallback (ollama list)")
                else:
                    debug_errors.append(f"CLI: exit={result.returncode} stderr={result.stderr[:60]}")
            except FileNotFoundError:
                debug_errors.append("CLI: 'ollama' binary not found in PATH")
            except Exception as _e:
                debug_errors.append(f"CLI: {str(_e)[:80]}")

        # ── Build stack membership sets ────────────────────────────────
        stack_model_names: set = set()
        stack_provider_names: set = set()
        agent = _components.get("agent_loop")
        if agent:
            forge = getattr(agent, '_forge', None)
            if forge:
                try:
                    live_stack = forge._fallback_stack
                    if live_stack is not None:
                        for p in live_stack._providers:
                            stack_model_names.add(p.model)
                            stack_provider_names.add(p.name)
                except Exception:
                    pass
        try:
            _raw_cfg = _read_config_yaml()
            for cp in _raw_cfg.get("custom_providers", []):
                if cp.get("kind") == "ollama":
                    stack_model_names.add(cp.get("model", ""))
                    stack_provider_names.add(cp.get("name", ""))
        except Exception:
            pass

        # ── Build response ─────────────────────────────────────────────
        if not ollama_running or raw_data is None:
            return JSONResponse({
                "models": [], "total": 0,
                "ollama_running": False,
                "debug": debug_errors,
                "error": " | ".join(debug_errors),
            })

        models = []
        for m in raw_data.get("models", []):
            name = m.get("name", "")
            size_bytes = m.get("size", 0)
            size_gb = round(size_bytes / 1024 / 1024 / 1024, 1)
            in_stack = (name in stack_model_names or f"ollama-{name}" in stack_provider_names)
            models.append({
                "name": name,
                "size_gb": size_gb,
                "family": m.get("details", {}).get("family", ""),
                "params": m.get("details", {}).get("parameter_size", ""),
                "quantization": m.get("details", {}).get("quantization_level", ""),
                "in_stack": in_stack,
            })
        return JSONResponse({
            "models": models, "total": len(models),
            "ollama_running": True,
            "debug": debug_errors,  # empty if HTTP worked, has note if CLI was used
        })

    @app.post("/api/v2/ollama/pull")
    async def pull_ollama_model(request: Request, body: dict = Body(default={}),
                                _auth: None = Depends(_admin_dep)):
        """Pull (download) an Ollama model. Non-blocking — starts download in background."""
        model_name = body.get("name", "").strip()
        if not model_name:
            raise HTTPException(400, "model name required")
        import subprocess
        try:
            proc = subprocess.Popen(
                ["ollama", "pull", model_name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            _vlog("📥", f"Ollama pull started: {model_name}")
            return JSONResponse({"ok": True, "name": model_name, "status": "downloading", "pid": proc.pid})
        except Exception as exc:
            raise HTTPException(500, f"Failed to pull: {str(exc)[:200]}")

    @app.get("/api/v2/ollama/pull-stream")
    async def pull_ollama_stream(name: str, request: Request,
                                 _auth: None = Depends(_admin_dep)):
        """Stream ollama pull progress as SSE.
        Primary: Ollama HTTP API /api/pull (NDJSON) → clean structured output.
        Fallback: CLI subprocess with proper \\r/spinner handling.
        """
        from fastapi.responses import StreamingResponse
        import re as _re, urllib.request as _ur, threading as _th, queue as _q

        model_name = name.strip()
        if not model_name:
            raise HTTPException(400, "model name required")

        # ── Detect bare HuggingFace name (no username prefix) ─────────
        def _looks_like_bare_hf(n: str) -> bool:
            # Bare name: no slash, no colon, has uppercase mid-word → likely model name, not tag
            return ('/' not in n and ':' not in n and
                    any(c.isupper() for c in n[1:]))

        # ── Format bytes nicely ───────────────────────────────────────
        def _fmt_bytes(b: int) -> str:
            if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} GB"
            if b >= 1_048_576:     return f"{b/1_048_576:.0f} MB"
            return f"{b/1024:.0f} KB"

        async def _generate():
            yield f"data: 🦙 Đang tải: {model_name}\n\n"

            # ── Pre-flight warning for bare HF names ─────────────────
            if _looks_like_bare_hf(model_name):
                yield  "data: ⚠️  Tên này không đúng format Ollama.\n\n"
                yield  "data:    Ollama dùng: model:tag  (vd: llama3.2:3b, qwen3:8b)\n\n"
                yield  "data:    HuggingFace: hf.co/username/repo  (cần có username)\n\n"
                yield f"data:    Tìm model đúng tên: https://ollama.com/search?q={model_name.lower()}\n\n"
                yield  "data: ─────────────────────────────────────────\n\n"
                # Still attempt the pull — user might know what they're doing
            
            # ══════════════════════════════════════════════════════════
            # METHOD 1: Ollama HTTP API /api/pull  (NDJSON streaming)
            # Clean, structured, no spinner garbage
            # ══════════════════════════════════════════════════════════
            http_ok = False
            for _base in ("http://127.0.0.1:11434", "http://localhost:11434"):
                try:
                    body = json.dumps({"name": model_name, "stream": True}).encode()
                    _req = _ur.Request(
                        f"{_base}/api/pull",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with _ur.urlopen(_req, timeout=300) as resp:
                        http_ok = True
                        last_status = ""
                        last_digest = ""
                        yield f"data: 🔗 Kết nối Ollama API ({_base})\n\n"

                        for raw_line in resp:
                            line_str = raw_line.decode("utf-8", errors="replace").strip()
                            if not line_str:
                                continue
                            try:
                                obj = json.loads(line_str)
                            except Exception:
                                continue

                            status  = obj.get("status", "")
                            total   = obj.get("total", 0)
                            done    = obj.get("completed", 0)
                            digest  = obj.get("digest", "")
                            err_msg = obj.get("error", "")

                            if err_msg:
                                yield f"data: ❌ {err_msg}\n\n"
                                if "file does not exist" in err_msg:
                                    base_name = model_name.split(":")[0].split("/")[-1].lower()
                                    tag_tried = model_name.split(":")[-1] if ":" in model_name else ""

                                    # ── Hardcoded tags cho các model phổ biến ─────
                                    # Ollama không có public API để list tags,
                                    # nên dùng danh sách cứng cập nhật thủ công.
                                    _KNOWN_TAGS: dict[str, list[str]] = {
                                        "qwen2.5":         ["0.5b","1.5b","3b","7b","14b","32b","72b","latest"],
                                        "qwen2.5-coder":   ["0.5b","1.5b","3b","7b","14b","32b","latest"],
                                        "qwen3":           ["0.6b","1.7b","4b","8b","14b","32b","30b-a3b","235b-a22b","latest"],
                                        "qwen3-vl":        ["7b","72b","latest"],
                                        "llama3.2":        ["1b","3b","latest"],
                                        "llama3.1":        ["8b","70b","405b","latest"],
                                        "llama3":          ["8b","70b","latest"],
                                        "llama3.3":        ["70b","latest"],
                                        "gemma3":          ["1b","4b","12b","27b","latest"],
                                        "gemma2":          ["2b","9b","27b","latest"],
                                        "mistral":         ["7b","latest"],
                                        "mistral-small":   ["22b","latest"],
                                        "phi4":            ["latest","14b"],
                                        "phi3.5":          ["3.8b","latest"],
                                        "phi3":            ["3.8b","14b","latest"],
                                        "deepseek-r1":     ["1.5b","7b","8b","14b","32b","70b","671b","latest"],
                                        "deepseek-v3":     ["latest"],
                                        "deepseek-coder-v2":["16b","236b","latest"],
                                        "nomic-embed-text":["latest","v1.5"],
                                        "bge-m3":          ["latest","567m"],
                                        "llava":           ["7b","13b","34b","latest"],
                                        "codellama":       ["7b","13b","34b","70b","latest"],
                                        "vicuna":          ["7b","13b","33b","latest"],
                                        "orca-mini":       ["3b","7b","13b","70b","latest"],
                                        "neural-chat":     ["7b","latest"],
                                        "starling-lm":     ["7b","latest"],
                                        "yi":              ["6b","34b","latest"],
                                        "solar":           ["10.7b","latest"],
                                        "smollm2":         ["135m","360m","1.7b","latest"],
                                        "internlm2":       ["1m","7b","20b","latest"],
                                    }

                                    yield  "data: ─────────────────────────────────────────\n\n"
                                    yield f"data: 💡 '{model_name}' không tìm thấy.\n\n"

                                    if base_name in _KNOWN_TAGS:
                                        known = _KNOWN_TAGS[base_name]
                                        if tag_tried and tag_tried not in known:
                                            yield f"data:    Tag ':{tag_tried}' không tồn tại.\n\n"
                                        yield f"data:    Tags hợp lệ cho '{base_name}':\n\n"
                                        # Show as one line for readability
                                        tags_str = "  ".join(f"{base_name}:{t}" for t in known)
                                        # Split into rows of 4
                                        for i in range(0, len(known), 4):
                                            row = "   ".join(f"{base_name}:{t}" for t in known[i:i+4])
                                            yield f"data:      {row}\n\n"
                                    else:
                                        yield f"data:    Xem tags đúng tại: https://ollama.com/library/{base_name}\n\n"
                                return

                            # Show status change + progress
                            if status == "success":
                                yield f"data: ✅ Tải xong: {model_name}\n\n"
                                return

                            if total > 0:
                                pct = int(done / total * 100)
                                # Build mini progress bar
                                filled = pct // 5  # 0-20 chars
                                bar = "█" * filled + "░" * (20 - filled)
                                digest_short = digest[7:19] if len(digest) > 12 else digest
                                msg = f"{status} [{bar}] {pct}%  {_fmt_bytes(done)}/{_fmt_bytes(total)}"
                                if digest_short:
                                    msg += f"  ({digest_short}...)"
                                # Only yield when pct changes significantly (avoid flood)
                                if digest != last_digest or pct % 5 == 0:
                                    yield f"data: {msg}\n\n"
                                    last_digest = digest
                            elif status != last_status:
                                # Status-only lines (no size): show once per unique status
                                yield f"data: ⏳ {status}\n\n"
                                last_status = status

                    # If we exited the loop without seeing "success", the stream ended
                    yield f"data: ✅ Hoàn thành: {model_name}\n\n"
                    return

                except _ur.HTTPError as e:
                    yield f"data: ⚠️  HTTP API lỗi {e.code}: {e.reason} — thử CLI...\n\n"
                    break
                except Exception as conn_err:
                    if "refused" in str(conn_err).lower() or "connect" in str(conn_err).lower():
                        continue  # try next base URL
                    yield f"data: ⚠️  HTTP API lỗi: {str(conn_err)[:80]} — thử CLI...\n\n"
                    break

            if http_ok:
                return  # Already handled above

            # ══════════════════════════════════════════════════════════
            # METHOD 2: CLI fallback — ollama pull (subprocess)
            # Uses \r-aware line buffer to suppress spinner animation
            # ══════════════════════════════════════════════════════════
            yield "data: ⚠️  Ollama HTTP không phản hồi — dùng CLI fallback\n\n"
            try:
                import os as _os
                proc = await asyncio.create_subprocess_exec(
                    "ollama", "pull", model_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env={**_os.environ, "TERM": "dumb", "NO_COLOR": "1", "OLLAMA_NO_SPINNER": "1"},
                )
                assert proc.stdout is not None

                # ANSI stripper (no \r — we handle \r separately)
                _ansi = _re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Z]|\x1b.')

                line_buf = ""    # current logical line
                last_emitted = ""

                while True:
                    chunk = await proc.stdout.read(512)
                    if not chunk:
                        break
                    text = _ansi.sub('', chunk.decode("utf-8", errors="replace"))

                    for ch in text:
                        if ch == '\r':
                            # Carriage return: discard current line buf (spinner overwrite)
                            line_buf = ""
                        elif ch == '\n':
                            clean = line_buf.strip()
                            if clean and clean != last_emitted:
                                yield f"data: {clean}\n\n"
                                last_emitted = clean
                            line_buf = ""
                        else:
                            line_buf += ch

                # Flush remaining
                if line_buf.strip() and line_buf.strip() != last_emitted:
                    yield f"data: {line_buf.strip()}\n\n"

                await proc.wait()
                if proc.returncode == 0:
                    yield f"data: ✅ Tải xong: {model_name}\n\n"
                else:
                    yield f"data: ❌ Lỗi (exit code {proc.returncode})\n\n"

            except FileNotFoundError:
                yield "data: ❌ Không tìm thấy lệnh 'ollama' — cài tại https://ollama.com\n\n"
            except Exception as exc:
                yield f"data: ❌ Lỗi: {str(exc)[:300]}\n\n"
            finally:
                yield "data: __DONE__\n\n"

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ══════════════════════════════════════════════════════════════
    # AI LAB ENDPOINTS — YouTube/TikTok search, transcripts, pipeline
    # ══════════════════════════════════════════════════════════════

    BASE_DIR = str(Path(__file__).parent.parent)  # project root (parent of admin/)

    @app.post("/api/v2/ailab/youtube-search")
    async def ailab_youtube_search(request: Request, body: dict = Body(default={}),
                                    _auth: None = Depends(_admin_dep)):
        """Search YouTube with sort options."""
        keywords = body.get("keywords", "").strip()
        max_results = min(int(body.get("max_results", 30)), 100)
        sort_by = body.get("sort_by", "relevance")  # relevance, date, view_count
        if not keywords:
            raise HTTPException(400, "keywords required")

        try:
            import yt_dlp
        except ImportError:
            raise HTTPException(500, "yt-dlp chưa cài. Chạy: pip install yt-dlp")

        videos = []
        # For date/view sorting, use YouTube search URL with sp parameter
        if sort_by == "date":
            search_query = f"https://www.youtube.com/results?search_query={keywords.replace(' ','+')}&sp=CAI%253D"
        elif sort_by == "view_count":
            search_query = f"https://www.youtube.com/results?search_query={keywords.replace(' ','+')}&sp=CAM%253D"
        else:
            search_query = f"ytsearch{max_results}:{keywords}"

        ydl_opts = {
            'quiet': True, 'no_warnings': True,
            'extract_flat': 'in_playlist',
            'skip_download': True, 'ignoreerrors': True,
            'playlist_items': f'1-{max_results}',
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(search_query, download=False)
                for entry in (result.get('entries') or []):
                    if not entry: continue
                    vid = entry.get('id', '')
                    url = entry.get('url', entry.get('webpage_url', ''))
                    if vid and 'youtube' not in str(url) and 'youtu.be' not in str(url):
                        url = f"https://www.youtube.com/watch?v={vid}"
                    videos.append({
                        'url': url or f"https://www.youtube.com/watch?v={vid}",
                        'title': entry.get('title', 'Unknown'),
                        'channel': entry.get('uploader', entry.get('channel', entry.get('uploader_id', ''))),
                        'duration': entry.get('duration') or 0,
                        'view_count': entry.get('view_count') or 0,
                        'id': vid,
                    })
            # Client-side sort fallback (for ytsearch which doesn't support sp)
            if sort_by == "view_count" and not search_query.startswith("http"):
                videos.sort(key=lambda v: v.get('view_count', 0), reverse=True)
            elif sort_by == "date":
                pass  # YouTube URL sort handles this

            print("[AI-Lab]", f"YouTube search '{keywords}' sort={sort_by} → {len(videos)} results")
        except Exception as e:
            print("[AI-Lab ERROR]", f"YouTube search error: {e}")
            raise HTTPException(500, f"YouTube search error: {e}")

        return JSONResponse({"videos": videos[:max_results], "count": len(videos), "sort": sort_by})

    @app.post("/api/v2/ailab/tiktok-search")
    async def ailab_tiktok_search(request: Request, body: dict = Body(default={}),
                                   _auth: None = Depends(_admin_dep)):
        """Get TikTok video info from direct URL. Search is not supported (TikTok blocks it)."""
        keywords = body.get("keywords", "").strip()
        urls = body.get("urls", [])

        try:
            import yt_dlp
        except ImportError:
            raise HTTPException(500, "yt-dlp chưa cài. Chạy: pip install yt-dlp")

        # TikTok blocks search — only direct URL works
        if not urls and keywords:
            # Check if keywords is actually a URL
            if 'tiktok.com' in keywords:
                urls = [keywords]
            else:
                return JSONResponse({
                    "videos": [],
                    "count": 0,
                    "message": "TikTok không hỗ trợ tìm kiếm. Hãy paste URL trực tiếp."
                })

        videos = []
        for url in (urls or []):
            url = url.strip()
            if not url or 'tiktok.com' not in url:
                continue
            try:
                ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info:
                        videos.append({
                            'url': info.get('webpage_url', url),
                            'title': (info.get('title') or info.get('description', 'TikTok video'))[:80],
                            'channel': info.get('uploader', info.get('creator', '')),
                            'duration': info.get('duration') or 0,
                            'view_count': info.get('view_count') or 0,
                            'id': info.get('id', ''),
                            'platform': 'tiktok',
                        })
            except Exception as e:
                print("[AI-Lab WARN]", f"TikTok info error for {url}: {e}")

        return JSONResponse({"videos": videos, "count": len(videos)})

    # ── Export Transcripts — runs in background thread ──
    _export_state = {"running": False, "log": [], "files": [], "errors": []}

    @app.post("/api/v2/ailab/export-transcripts")
    async def ailab_export_transcripts(request: Request, body: dict = Body(default={}),
                                        _auth: None = Depends(_admin_dep)):
        """Start transcript export in background. Poll /export-status for progress."""
        urls = body.get("urls", [])
        keywords = body.get("keywords", "")
        if not urls:
            raise HTTPException(400, "urls list required")
        if _export_state["running"]:
            raise HTTPException(409, "Export đang chạy — chờ hoàn tất")

        try:
            import yt_dlp
        except ImportError:
            raise HTTPException(500, "yt-dlp chưa cài. Chạy: pip install yt-dlp")

        import threading
        _export_state["running"] = True
        _export_state["log"] = [f"🚀 Bắt đầu xuất {len(urls)} video..."]
        _export_state["files"] = []
        _export_state["errors"] = []

        def _run_export():
            import yt_dlp as _ytdlp
            output_dir = os.path.join(BASE_DIR, "transcripts_export")
            tmp_audio_dir = os.path.join(output_dir, "_audio_temp")
            os.makedirs(tmp_audio_dir, exist_ok=True)

            # Pre-load whisper model once
            whisper_model = None
            try:
                import whisper
                _export_state["log"].append("📥 Đang tải Whisper model (lần đầu ~140MB)...")
                whisper_model = whisper.load_model("base")
                _export_state["log"].append("✅ Whisper model loaded")
            except ImportError:
                _export_state["log"].append("⚠️ Whisper chưa cài — chỉ tải audio, không transcribe")
            except Exception as e:
                _export_state["log"].append(f"⚠️ Whisper load error: {e}")

            for i, url in enumerate(urls[:30]):
                url = url.strip()
                if not url:
                    continue
                _export_state["log"].append(f"\n[{i+1}/{len(urls)}] Đang xử lý...")
                try:
                    # Download audio
                    _export_state["log"].append("  📥 Tải audio...")
                    ydl_opts = {
                        'format': 'bestaudio/best',
                        'outtmpl': os.path.join(tmp_audio_dir, '%(id)s.%(ext)s'),
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '64',
                        }],
                        'quiet': True, 'no_warnings': True,
                    }
                    with _ytdlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        vid_id = info.get('id', 'unknown')
                        title = info.get('title', url)
                    _export_state["log"].append(f"  ✅ Audio: {title[:50]}")

                    # Find audio file
                    import glob
                    audio_files = glob.glob(f"{tmp_audio_dir}/{vid_id}.*") + glob.glob(f"{tmp_audio_dir}/*{vid_id}*")
                    audio_path = None
                    for af in audio_files:
                        if af.endswith(('.mp3', '.wav', '.m4a', '.webm', '.ogg')):
                            audio_path = af
                            break
                    if not audio_path:
                        _export_state["log"].append("  ❌ Audio file not found")
                        _export_state["errors"].append(f"Audio not found: {vid_id}")
                        continue

                    # Transcribe
                    text = ""
                    if whisper_model:
                        duration = info.get('duration', 0)
                        est = max(1, int(duration * 0.3)) if duration else '?'
                        _export_state["log"].append(f"  🎙️ Whisper đang transcribe (~{est}s)...")
                        try:
                            result = whisper_model.transcribe(audio_path, language="vi", fp16=False)
                            text = result.get("text", "")
                            _export_state["log"].append(f"  ✅ Transcript: {len(text)} ký tự")
                        except Exception as we:
                            text = f"[Whisper error: {we}]"
                            _export_state["log"].append(f"  ❌ Whisper error: {we}")
                    else:
                        text = f"[Whisper chưa cài — audio: {audio_path}]\n[pip install openai-whisper]"
                        _export_state["log"].append("  ⚠️ Bỏ qua transcribe (Whisper chưa cài)")

                    # Save
                    safe_title = "".join(c for c in title if c.isalnum() or c in ' _-')[:50].strip().replace(' ', '_') or vid_id
                    out_name = f"{safe_title}.md"
                    out_path = os.path.join(output_dir, out_name)
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(f"# {title}\n# URL: {url}\n# Keywords: {keywords}\n\n{text}")
                    _export_state["files"].append(out_name)
                    _export_state["log"].append(f"  💾 Saved: {out_name}")

                    try:
                        os.remove(audio_path)
                    except OSError:
                        pass

                except Exception as e:
                    _export_state["errors"].append(f"{url}: {str(e)[:100]}")
                    _export_state["log"].append(f"  ❌ Lỗi: {str(e)[:100]}")

            _export_state["log"].append(f"\n✅ Hoàn tất — {len(_export_state['files'])} file đã xuất")
            _export_state["running"] = False

        threading.Thread(target=_run_export, daemon=True).start()
        return JSONResponse({"ok": True, "message": "Export started", "total": len(urls)})

    @app.get("/api/v2/ailab/export-status")
    async def ailab_export_status(request: Request, _auth: None = Depends(_admin_dep)):
        """Poll export progress."""
        return JSONResponse({
            "running": _export_state["running"],
            "log": _export_state["log"][-50:],
            "files": _export_state["files"],
            "errors": _export_state["errors"],
        })

    # ── Dataset Search — search HuggingFace, OpenML, etc. directly ──

    @app.post("/api/v2/ailab/dataset-search")
    async def ailab_dataset_search(request: Request, body: dict = Body(default={}),
                                    _auth: None = Depends(_admin_dep)):
        """Search datasets across multiple platforms."""
        query = body.get("query", "").strip()
        source = body.get("source", "huggingface")
        limit = min(int(body.get("limit", 20)), 50)
        if not query:
            raise HTTPException(400, "query required")

        import urllib.request as _ur
        import urllib.parse as _up
        results = []

        try:
            if source == "huggingface":
                url = f"https://huggingface.co/api/datasets?search={_up.quote(query)}&limit={limit}&sort=likes&direction=-1"
                req = _ur.Request(url, headers={"User-Agent": "Phidipus/1.0"})
                with _ur.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                    for ds in data:
                        results.append({
                            "id": ds.get("id", ""),
                            "name": ds.get("id", "").split("/")[-1],
                            "author": ds.get("id", "").split("/")[0] if "/" in ds.get("id","") else "",
                            "description": ds.get("description", "")[:200],
                            "likes": ds.get("likes", 0),
                            "downloads": ds.get("downloads", 0),
                            "tags": ds.get("tags", [])[:5],
                            "url": f"https://huggingface.co/datasets/{ds.get('id','')}",
                            "source": "huggingface",
                        })

            elif source == "openml":
                # OpenML REST API — use search with wildcard
                # The data_name filter supports SQL LIKE with %25 as wildcard
                encoded_q = _up.quote(f"%{query}%")
                url = f"https://www.openml.org/api/v1/json/data/list/data_name/{encoded_q}/limit/{limit}/status/active/output_format/json"
                try:
                    req = _ur.Request(url, headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                        "Accept": "application/json",
                    })
                    with _ur.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode())
                        datasets = data.get("data", {}).get("dataset", [])
                        if isinstance(datasets, dict):
                            datasets = [datasets]
                        for ds in datasets[:limit]:
                            did = ds.get("did", "")
                            n_inst = ds.get("qualities", {}).get("NumberOfInstances", ds.get("NumberOfInstances", ""))
                            results.append({
                                "id": str(did),
                                "name": ds.get("name", ""),
                                "author": "",
                                "description": f"{ds.get('format', 'unknown')} · {n_inst} instances · {ds.get('NumberOfFeatures', '?')} features",
                                "likes": int(n_inst or 0) if str(n_inst).isdigit() else 0,
                                "downloads": int(ds.get("runs", 0) or 0),
                                "tags": [t for t in [ds.get("format", ""), ds.get("status", "")] if t],
                                "url": f"https://www.openml.org/d/{did}",
                                "source": "openml",
                            })
                except Exception as oml_err:
                    # Fallback: redirect to OpenML search
                    search_url = f"https://www.openml.org/search?type=data&sort=runs&status=active&q={_up.quote(query)}"
                    results.append({
                        "id": "_redirect", "name": f"Tìm '{query}' trên OpenML",
                        "author": "openml.org", "description": f"Mở trình duyệt để tìm kiếm (API lỗi: {str(oml_err)[:50]})",
                        "likes": 0, "downloads": 0, "tags": ["redirect"],
                        "url": search_url, "source": "openml",
                    })

            elif source == "tensorflow":
                # TensorFlow Datasets — use GitHub API to search TFDS catalog
                url = f"https://api.github.com/search/code?q={_up.quote(query)}+repo:tensorflow/datasets+path:tensorflow_datasets/datasets&per_page={limit}"
                try:
                    req = _ur.Request(url, headers={
                        "User-Agent": "Phidipus/1.0",
                        "Accept": "application/vnd.github.v3+json",
                    })
                    with _ur.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode())
                        seen = set()
                        for item in data.get("items", []):
                            # Extract dataset name from path like "tensorflow_datasets/datasets/mnist/mnist.py"
                            path = item.get("path", "")
                            parts = path.split("/")
                            if len(parts) >= 3 and parts[1] == "datasets":
                                ds_name = parts[2]
                                if ds_name not in seen and ds_name != "__init__":
                                    seen.add(ds_name)
                                    results.append({
                                        "id": ds_name, "name": ds_name, "author": "tensorflow",
                                        "description": f"TensorFlow Dataset: {ds_name}",
                                        "likes": 0, "downloads": 0, "tags": ["tfds"],
                                        "url": f"https://www.tensorflow.org/datasets/catalog/{ds_name}",
                                        "source": "tensorflow",
                                    })
                except Exception as tf_err:
                    # Fallback: redirect to TFDS catalog
                    search_url = f"https://www.tensorflow.org/datasets/catalog/overview#all_datasets"
                    results.append({
                        "id": "_redirect", "name": f"Tìm '{query}' trên TensorFlow Datasets",
                        "author": "tensorflow.org", "description": f"Mở catalog trong trình duyệt",
                        "likes": 0, "downloads": 0, "tags": ["redirect"],
                        "url": search_url, "source": "tensorflow",
                    })

            elif source == "kaggle":
                # Kaggle needs API token — return search URL for browser
                search_url = f"https://www.kaggle.com/datasets?search={_up.quote(query)}"
                results.append({
                    "id": "_redirect", "name": f"Tìm '{query}' trên Kaggle",
                    "author": "kaggle.com", "description": "Mở trình duyệt để tìm kiếm (cần đăng nhập Kaggle)",
                    "likes": 0, "downloads": 0, "tags": ["redirect"],
                    "url": search_url, "source": "kaggle",
                })

            elif source == "google":
                search_url = f"https://datasetsearch.research.google.com/search?query={_up.quote(query)}"
                results.append({
                    "id": "_redirect", "name": f"Tìm '{query}' trên Google Dataset Search",
                    "author": "google.com", "description": "Mở trình duyệt để tìm kiếm",
                    "likes": 0, "downloads": 0, "tags": ["redirect"],
                    "url": search_url, "source": "google",
                })

        except Exception as e:
            print(f"[AI-Lab] Dataset search error ({source}): {e}")
            # Return empty with error message
            return JSONResponse({"results": [], "error": str(e), "source": source})

        return JSONResponse({"results": results, "count": len(results), "source": source})

    # ══════════════════════════════════════════════════════════════
    # MCP (Model Context Protocol) — Connect to external tools
    # ══════════════════════════════════════════════════════════════

    _mcp_registry = None

    def _get_mcp():
        nonlocal _mcp_registry
        if _mcp_registry is None:
            try:
                from mcp.mcp_registry import MCPRegistry
                _mcp_registry = MCPRegistry(os.path.join(BASE_DIR, "data", "mcp_servers.json"))
            except ImportError:
                pass
        return _mcp_registry

    @app.get("/api/v2/mcp/status")
    async def mcp_status(request: Request, _auth: None = Depends(_admin_dep)):
        """Get MCP servers status."""
        reg = _get_mcp()
        if not reg:
            return JSONResponse({"servers": [], "total_tools": 0, "connected": 0})
        return JSONResponse(reg.status())

    @app.get("/api/v2/mcp/well-known")
    async def mcp_well_known(request: Request, _auth: None = Depends(_admin_dep)):
        """List well-known MCP servers."""
        try:
            from mcp.mcp_registry import WELL_KNOWN_SERVERS
            return JSONResponse({"servers": WELL_KNOWN_SERVERS})
        except ImportError:
            return JSONResponse({"servers": {}})

    @app.post("/api/v2/mcp/connect")
    async def mcp_connect(request: Request, body: dict = Body(default={}),
                           _auth: None = Depends(_admin_dep)):
        """Connect to an MCP server."""
        reg = _get_mcp()
        if not reg:
            raise HTTPException(500, "MCP module not available")

        name = body.get("name", "").strip()
        url = body.get("url", "").strip()
        transport = body.get("transport", "sse")
        command = body.get("command", "")
        args = body.get("args", [])
        env = body.get("env", {})
        well_known = body.get("well_known", "")

        if well_known:
            ok = await reg.add_well_known(well_known, env_overrides=env)
        elif name and (url or command):
            ok = await reg.add_server(name, url=url, transport=transport,
                                       command=command, args=args, env=env)
        else:
            raise HTTPException(400, "name + url/command required, or well_known id")

        return JSONResponse({"ok": ok, "status": reg.status()})

    @app.post("/api/v2/mcp/disconnect")
    async def mcp_disconnect(request: Request, body: dict = Body(default={}),
                              _auth: None = Depends(_admin_dep)):
        """Disconnect an MCP server."""
        reg = _get_mcp()
        if not reg:
            raise HTTPException(500, "MCP module not available")
        name = body.get("name", "")
        await reg.remove_server(name)
        return JSONResponse({"ok": True})

    @app.get("/api/v2/mcp/tools")
    async def mcp_tools(request: Request, _auth: None = Depends(_admin_dep)):
        """List all MCP tools across connected servers."""
        reg = _get_mcp()
        if not reg:
            return JSONResponse({"tools": []})
        return JSONResponse({"tools": reg.all_tools()})

    @app.post("/api/v2/mcp/call")
    async def mcp_call_tool(request: Request, body: dict = Body(default={}),
                             _auth: None = Depends(_admin_dep)):
        """Call an MCP tool."""
        reg = _get_mcp()
        if not reg:
            raise HTTPException(500, "MCP module not available")
        tool_name = body.get("tool", "")
        arguments = body.get("arguments", {})
        result = await reg.call(tool_name, arguments)
        return JSONResponse(result.to_dict())

    @app.post("/api/v2/ailab/save-modelfile")
    async def ailab_save_modelfile(request: Request, body: dict = Body(default={}),
                                    _auth: None = Depends(_admin_dep)):
        """Save Modelfile content to elite_pipeline/Modelfile."""
        content = body.get("content", "")
        if not content:
            raise HTTPException(400, "content required")
        mf_path = os.path.join(BASE_DIR, "elite_pipeline", "Modelfile")
        os.makedirs(os.path.dirname(mf_path), exist_ok=True)
        with open(mf_path, "w", encoding="utf-8") as f:
            f.write(content)
        return JSONResponse({"ok": True, "path": mf_path})

    @app.post("/api/v2/ailab/create-ollama-model")
    async def ailab_create_ollama_model(request: Request, body: dict = Body(default={}),
                                         _auth: None = Depends(_admin_dep)):
        """Create custom Ollama model from Modelfile."""
        name = body.get("name", "psysales-elite").strip()
        base = body.get("base", "qwen3:4b").strip()
        mf_path = os.path.join(BASE_DIR, "elite_pipeline", "Modelfile")

        if not os.path.exists(mf_path):
            raise HTTPException(400, f"Modelfile not found: {mf_path}")

        import subprocess
        try:
            result = subprocess.run(
                ["ollama", "create", name, "-f", mf_path],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                return JSONResponse({"ok": True, "name": name, "output": result.stdout})
            else:
                raise HTTPException(500, f"ollama create failed: {result.stderr}")
        except FileNotFoundError:
            raise HTTPException(500, "ollama binary not found in PATH")
        except subprocess.TimeoutExpired:
            raise HTTPException(500, "Timeout — model quá lớn hoặc Ollama chưa chạy")

    # ── Pipeline endpoints ──

    _pipeline_state = {"running": False, "log": [], "step": ""}

    @app.post("/api/v2/ailab/pipeline/run")
    async def ailab_pipeline_run(request: Request, body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        """Start GCR pipeline in background thread."""
        if _pipeline_state["running"]:
            raise HTTPException(409, "Pipeline đang chạy")

        config = body.get("config", {})
        topics_text = config.get("topics", "")
        topics = [t.strip() for t in topics_text.split("\n") if t.strip() and not t.startswith("#")]
        if not topics:
            raise HTTPException(400, "Cần ít nhất 1 topic")

        import threading
        _pipeline_state["running"] = True
        _pipeline_state["log"] = [f"🚀 Pipeline bắt đầu — {len(topics)} topics"]
        _pipeline_state["step"] = "starting"

        def _run():
            try:
                pipeline_script = os.path.join(BASE_DIR, "elite_pipeline", "run_pipeline.py")
                model = config.get("base_model", "qwen3:4b")
                gcr = config.get("gcr_loops", 2)
                min_score = config.get("min_score", 8.0)

                for i, topic in enumerate(topics):
                    if not _pipeline_state["running"]:
                        _pipeline_state["log"].append("⏹ Đã dừng bởi user")
                        break
                    _pipeline_state["step"] = f"topic_{i+1}"
                    _pipeline_state["log"].append(f"\n[{i+1}/{len(topics)}] {topic}")

                    import subprocess
                    result = subprocess.run([
                        sys.executable, pipeline_script,
                        "--topic", topic, "--model", model,
                        "--gcr-loops", str(gcr), "--min-score", str(min_score),
                    ], capture_output=True, text=True, timeout=1200)

                    if result.stdout:
                        for line in result.stdout.strip().split("\n")[-5:]:
                            _pipeline_state["log"].append(line)
                    if result.returncode != 0 and result.stderr:
                        _pipeline_state["log"].append(f"❌ {result.stderr[:200]}")

                _pipeline_state["log"].append(f"\n✅ Pipeline hoàn tất — {len(topics)} topics")
            except Exception as e:
                _pipeline_state["log"].append(f"❌ Pipeline error: {e}")
            finally:
                _pipeline_state["running"] = False
                _pipeline_state["step"] = "done"

        threading.Thread(target=_run, daemon=True).start()
        return JSONResponse({"ok": True, "topics": len(topics)})

    @app.get("/api/v2/ailab/pipeline/status")
    async def ailab_pipeline_status(request: Request, _auth: None = Depends(_admin_dep)):
        return JSONResponse({
            "running": _pipeline_state["running"],
            "step": _pipeline_state["step"],
            "log": _pipeline_state["log"][-100:],
        })

    @app.post("/api/v2/ailab/pipeline/stop")
    async def ailab_pipeline_stop(request: Request, _auth: None = Depends(_admin_dep)):
        _pipeline_state["running"] = False
        _pipeline_state["log"].append("⏹ Dừng pipeline...")
        return JSONResponse({"ok": True})

    @app.post("/api/v2/ailab/brain/test")
    async def ailab_brain_test(request: Request, body: dict = Body(default={}),
                                _auth: None = Depends(_admin_dep)):
        """Test chatbot with Ollama model."""
        user_input = body.get("input", "").strip()
        if not user_input:
            raise HTTPException(400, "input required")

        import urllib.request as _ur
        model = body.get("model", "psysales-elite")
        payload = json.dumps({
            "model": model, "prompt": user_input, "stream": False,
            "options": {"temperature": 0.65, "num_predict": 2048},
        }).encode()
        try:
            req = _ur.Request("http://127.0.0.1:11434/api/generate",
                              data=payload, headers={"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
                return JSONResponse({"response": data.get("response", ""), "model": model})
        except Exception as e:
            raise HTTPException(500, f"Ollama error: {e}. Kiểm tra: ollama serve đang chạy?")

    @app.post("/api/v2/ollama/add-to-stack")
    async def add_ollama_to_stack(request: Request, body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        """Add an installed Ollama model to the fallback stack."""
        model_name = body.get("name", "").strip()
        if not model_name:
            raise HTTPException(400, "model name required")

        provider_name = f"ollama-{model_name}"
        from core.llm_fallback import Provider
        provider = Provider(
            name=provider_name,
            kind="ollama",
            model=model_name,
            temperature=0.2,
            timeout_s=120,
            base_url="http://127.0.0.1:11434",
            enabled=True,
        )

        agent = _components.get("agent_loop")
        added = False
        if agent:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                if stack:
                    added = stack.add_provider(provider)

        # Save to config
        raw = _read_config_yaml()
        if "custom_providers" not in raw:
            raw["custom_providers"] = []
        raw["custom_providers"] = [p for p in raw["custom_providers"] if p.get("name") != provider_name]
        raw["custom_providers"].append({
            "name": provider_name, "kind": "ollama",
            "base_url": "http://127.0.0.1:11434", "model": model_name, "api_key": "",
        })
        try:
            import yaml
            cp = _components.get("config_path", "")
            config_path = Path(cp) if cp else Path("config.yaml")
            if not config_path.exists():
                config_path = Path(__file__).parent.parent / "config.yaml"
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        except Exception:
            pass

        _vlog("🦙", f"Ollama model added to stack: {model_name} as {provider_name}")
        return JSONResponse({"ok": True, "name": provider_name, "model": model_name, "added": added})

    @app.post("/api/v2/providers/custom/test")
    async def test_custom_provider(request: Request, body: dict = Body(default={}),
                                   _auth: None = Depends(_admin_dep)):
        """Test a custom provider endpoint before adding."""
        base_url = body.get("base_url", "").strip()
        model = body.get("model", "").strip()
        api_key = body.get("api_key", "").strip()
        if not base_url or not model:
            raise HTTPException(400, "base_url and model required")

        from core.llm_fallback import Provider, check_provider_health_sync
        provider = Provider(
            name="test", kind="custom", model=model, temperature=0.2,
            timeout_s=15, api_key=api_key, base_url=base_url,
        )
        try:
            result = await asyncio.to_thread(check_provider_health_sync, provider)
            return JSONResponse(result)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:200]})

    @app.get("/api/v2/config/key-status")
    async def get_key_status(request: Request, _auth: None = Depends(_auth_dep)):
        """Key status — 3 layers of fallback to NEVER fail."""
        result = {"gemini": False, "mistral": False, "cerebras": False, "openrouter": False}

        # Layer 1: State file (.phidipus_state.json)
        try:
            state = _read_state()
            for k, v in state.get("keys", {}).items():
                if v: result[k] = True
        except Exception:
            pass

        # Layer 2: config.yaml via YAML parser
        try:
            raw = _read_config_yaml()
            sf = raw.get("skill_forge", {})
            if sf.get("gemini_api_key", ""): result["gemini"] = True
            if sf.get("mistral_api_key", ""): result["mistral"] = True
            if sf.get("cerebras_api_key", ""): result["cerebras"] = True
            if sf.get("openrouter_api_key", ""): result["openrouter"] = True
        except Exception:
            pass

        # Layer 3: BRUTE FORCE — read config.yaml as raw text, search for keys
        if not any(result.values()):
            try:
                for candidate in [
                    _components.get("config_path", ""),
                    str(Path("config.yaml").resolve()),
                    str(Path(__file__).parent.parent / "config.yaml"),
                ]:
                    if candidate and Path(candidate).exists():
                        text = Path(candidate).read_text("utf-8")
                        if "gemini_api_key:" in text:
                            # Extract value after colon
                            for line in text.splitlines():
                                ls = line.strip()
                                if ls.startswith("gemini_api_key:") and not ls.endswith("''") and not ls.endswith('""') and len(ls.split(":", 1)[1].strip().strip("'\"")) > 3:
                                    result["gemini"] = True
                                if ls.startswith("mistral_api_key:") and not ls.endswith("''") and not ls.endswith('""') and len(ls.split(":", 1)[1].strip().strip("'\"")) > 3:
                                    result["mistral"] = True
                                if ls.startswith("cerebras_api_key:") and not ls.endswith("''") and not ls.endswith('""') and len(ls.split(":", 1)[1].strip().strip("'\"")) > 3:
                                    result["cerebras"] = True
                                if ls.startswith("openrouter_api_key:") and not ls.endswith("''") and not ls.endswith('""') and len(ls.split(":", 1)[1].strip().strip("'\"")) > 3:
                                    result["openrouter"] = True
                        break  # found a config file, stop
            except Exception:
                pass

        return JSONResponse(result)

    @app.get("/api/v2/config/key-debug")
    async def get_key_debug(request: Request, _auth: None = Depends(_auth_dep)):
        """Debug endpoint — shows WHY key status is what it is."""
        import yaml as _yaml
        info = {"config_path_injected": _components.get("config_path", ""),
                "state_file": str(_get_state_file()),
                "state_file_exists": _get_state_file().exists(),
                "state_content": _read_state()}
        # Test all 3 config.yaml paths
        paths_tried = []
        cp = _components.get("config_path", "")
        if cp:
            p = Path(cp)
            paths_tried.append({"path": str(p), "exists": p.exists()})
        p2 = Path("config.yaml")
        paths_tried.append({"path": str(p2.resolve()), "exists": p2.exists()})
        p3 = Path(__file__).parent.parent / "config.yaml"
        paths_tried.append({"path": str(p3), "exists": p3.exists()})
        info["paths_tried"] = paths_tried
        raw = _read_config_yaml()
        sf = raw.get("skill_forge", {})
        info["config_yaml_found"] = bool(sf)
        info["keys_in_yaml"] = {
            "gemini": bool(sf.get("gemini_api_key", "")),
            "mistral": bool(sf.get("mistral_api_key", "")),
            "cerebras": bool(sf.get("cerebras_api_key", "")),
            "openrouter": bool(sf.get("openrouter_api_key", "")),
        }
        return JSONResponse(info)

    @app.get("/api/v2/debug/static")
    async def debug_static(request: Request):
        """Debug: show static files info."""
        sd = Path(__file__).parent / "static"
        ad = sd / "assets"
        files = list(ad.glob("*")) if ad.exists() else []
        return JSONResponse({
            "static_dir": str(sd),
            "static_exists": sd.exists(),
            "assets_dir": str(ad),
            "assets_exists": ad.exists(),
            "asset_files": [f.name for f in files[:20]],
            "asset_count": len(files),
            "index_exists": (sd / "index.html").exists(),
        })

    @app.post("/api/v2/providers/health-check")
    async def check_providers_health(request: Request, _auth: None = Depends(_admin_dep)):
        """Run health check on all providers."""
        agent = _components.get("agent_loop")
        if not agent:
            raise HTTPException(503, "Agent not ready")
        try:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                from core.llm_fallback import check_all_providers_health
                results = await check_all_providers_health(stack._providers)
                return JSONResponse({"results": results})
            raise HTTPException(503, "SkillForge not available")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/providers/{name}/toggle")
    async def toggle_provider(request: Request, name: str,
                              body: dict = Body(default={}),
                              _auth: None = Depends(_admin_dep)):
        """Enable/disable a provider."""
        agent = _components.get("agent_loop")
        if not agent:
            raise HTTPException(503, "Agent not ready")
        enabled = body.get("enabled", True)
        try:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                ok = stack.set_provider_enabled(name, enabled)
                if ok:
                    return JSONResponse({"ok": True, "name": name, "enabled": enabled})
                raise HTTPException(404, f"Provider '{name}' not found")
            raise HTTPException(503, "SkillForge not available")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/providers/{name}/api-key")
    async def update_provider_key(request: Request, name: str,
                                  body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        """Update API key for a provider. Works even if agent not fully ready."""
        new_key = body.get("api_key", "").strip()
        if not new_key:
            raise HTTPException(400, "api_key required")
        # Try to update in-memory stack
        agent = _components.get("agent_loop")
        if agent:
            try:
                forge = getattr(agent, '_forge', None)
                if forge and hasattr(forge, '_get_stack'):
                    stack = forge._get_stack()
                    if stack:
                        stack.update_api_key(name, new_key)
            except Exception:
                pass  # In-memory update failed, but config save will handle persistence
        return JSONResponse({"ok": True, "name": name})

    @app.post("/api/v2/providers/{name}/reset")
    async def reset_provider_health(request: Request, name: str,
                                    _auth: None = Depends(_admin_dep)):
        """Reset health counters for a provider."""
        agent = _components.get("agent_loop")
        if not agent:
            raise HTTPException(503, "Agent not ready")
        try:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                ok = stack.reset_provider_health(name)
                if ok:
                    return JSONResponse({"ok": True, "name": name})
                raise HTTPException(404, f"Provider '{name}' not found")
            raise HTTPException(503, "SkillForge not available")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/providers/{name}/move")
    async def move_provider(request: Request, name: str,
                            body: dict = Body(default={}),
                            _auth: None = Depends(_admin_dep)):
        """Move a provider up or down in the fallback stack."""
        direction = body.get("direction", "").strip()
        if direction not in ("up", "down"):
            raise HTTPException(400, "direction must be 'up' or 'down'")
        agent = _components.get("agent_loop")
        if not agent:
            raise HTTPException(503, "Agent not ready")
        try:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                ok = stack.move_provider(name, direction)
                if ok:
                    return JSONResponse(stack.stats())
                raise HTTPException(400, f"Cannot move '{name}' {direction}")
            raise HTTPException(503, "SkillForge not available")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/providers/reorder")
    async def reorder_providers(request: Request,
                                body: dict = Body(default={}),
                                _auth: None = Depends(_admin_dep)):
        """Reorder the entire provider stack."""
        ordered = body.get("order", [])
        if not isinstance(ordered, list) or not ordered:
            raise HTTPException(400, "order must be a non-empty list of provider names")
        agent = _components.get("agent_loop")
        if not agent:
            raise HTTPException(503, "Agent not ready")
        try:
            forge = getattr(agent, '_forge', None)
            if forge and hasattr(forge, '_get_stack'):
                stack = forge._get_stack()
                stack.reorder(ordered)
                return JSONResponse(stack.stats())
            raise HTTPException(503, "SkillForge not available")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/config/api-key")
    async def save_api_key_to_config(request: Request,
                                     body: dict = Body(default={}),
                                     _auth: None = Depends(_admin_dep)):
        """
        Save API key to config.yaml so it persists across restarts.
        FIX v1.0: Support backup_keys for Gemini multi-key fallback.

        Body: {"provider": "gemini|mistral|cerebras|openrouter", "api_key": "...",
               "backup_keys": ["...", "..."]}  ← optional
        """
        provider = body.get("provider", "").strip()
        new_key = body.get("api_key", "").strip()
        backup_keys = body.get("backup_keys", [])
        if not provider or not new_key:
            raise HTTPException(400, "provider and api_key required")

        # Map provider id → config.yaml field name
        key_map = {
            "gemini": "gemini_api_key",
            "mistral": "mistral_api_key",
            "cerebras": "cerebras_api_key",
            "openrouter": "openrouter_api_key",
        }
        field = key_map.get(provider)
        if not field:
            raise HTTPException(400, f"Unknown provider: {provider}. Use: {list(key_map.keys())}")

        # Read and update config.yaml using absolute path
        cp = _components.get("config_path", "")
        config_path = Path(cp) if cp else Path("config.yaml")
        if not config_path.exists():
            config_path = Path(__file__).parent.parent / "config.yaml"
        if not config_path.exists():
            raise HTTPException(404, "config.yaml not found")

        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_data = yaml.safe_load(f) or {}

            if "skill_forge" not in cfg_data:
                cfg_data["skill_forge"] = {}
            cfg_data["skill_forge"][field] = new_key

            # FIX v1.0: Save backup keys for Gemini
            if provider == "gemini" and backup_keys:
                clean_backups = [k.strip() for k in backup_keys if k and k.strip()]
                if clean_backups:
                    cfg_data["skill_forge"]["gemini_backup_keys"] = clean_backups

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            _vlog("🔑", f"API key '{provider}' saved to config.yaml")

            # FIX v1.0: Hot-reload — update in-memory provider stack
            agent = _components.get("agent_loop")
            if agent:
                try:
                    forge = getattr(agent, '_forge', None)
                    if forge and hasattr(forge, '_get_stack'):
                        stack = forge._get_stack()
                        if stack:
                            # Update primary key
                            for p in stack._providers:
                                if provider == "gemini" and p.kind == "gemini":
                                    p.api_key = new_key
                                    p.enabled = True
                                    p.consecutive_fails = 0
                                    p.last_error = ""
                                elif p.name.startswith(provider) or p.kind == provider:
                                    p.api_key = new_key
                                    p.enabled = True
                                    p.consecutive_fails = 0
                                    p.last_error = ""
                            _vlog("🔄", f"Provider '{provider}' hot-reloaded in memory")
                except Exception as exc:
                    _vlog("⚠️", f"Hot-reload warning: {exc}")

            # Also write to state file
            _write_state({"keys": {provider: True}})
            return JSONResponse({"ok": True, "provider": provider, "field": field,
                                "saved": True, "backup_count": len(backup_keys)})

        except Exception as exc:
            raise HTTPException(500, f"Failed to save config: {str(exc)[:200]}")

    @app.post("/api/v2/config/shadow")
    async def update_shadow_config(request: Request, body: dict = Body(default={}), _auth: None = Depends(_admin_dep)):
        from admin.spider_hub_config import get_config
        cfg = get_config()
        if "privacy_level" in body:
            cfg.shadow_privacy_level = body["privacy_level"]
            _shadow.privacy_level = body["privacy_level"]
        return JSONResponse({"ok": True, "privacy": cfg.shadow_privacy_level})

    # ════════════════════════════════════════════════════════════
    # License API — v1.0 Commercial
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/license/status")
    async def license_status(request: Request):
        """Public: get license status (no auth required for initial setup)."""
        try:
            from core.license_manager import get_license_sync
            lm = get_license_sync()
            if lm:
                return JSONResponse(lm.stats())
            return JSONResponse({"valid": False, "tier": "free", "error": "Not initialized"})
        except Exception:
            return JSONResponse({"valid": False, "tier": "free"})

    @app.post("/api/v2/license/activate")
    async def license_activate(request: Request, body: dict = Body(default={}),
                               _auth: None = Depends(_admin_dep)):
        """Install a signed commercial license (admin only; verified offline)."""
        from core.license_manager import get_license_manager
        lm = await get_license_manager()
        key = body.get("key", "").strip()
        email = body.get("email", "").strip()
        if not key:
            raise HTTPException(400, "License key required")
        result = await lm.activate_key(key, email)
        return JSONResponse(result)

    @app.get("/api/v2/license/check-feature/{feature}")
    async def license_check_feature(request: Request, feature: str,
                                     _auth: None = Depends(_auth_dep)):
        from core.license_manager import get_license_sync
        lm = get_license_sync()
        if not lm:
            return JSONResponse({"allowed": False, "tier": "free"})
        return JSONResponse(lm.check_feature(feature))

    # ════════════════════════════════════════════════════════════
    # ReAct Controller API — v1.0
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/react/stats")
    async def react_stats(request: Request, _auth: None = Depends(_admin_dep)):
        try:
            from core.react_controller import get_react_controller
            rc = get_react_controller()
            return JSONResponse(rc.stats())
        except Exception:
            return JSONResponse({"verify_count": 0, "correct_count": 0, "catch_count": 0})

    # ════════════════════════════════════════════════════════════
    # Chrome CDP API — v1.0
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/chrome/cdp/status")
    async def cdp_status(request: Request, _auth: None = Depends(_admin_dep)):
        try:
            from automation.chrome_cdp import get_cdp
            cdp = await get_cdp()
            return JSONResponse({
                "available": await cdp.is_available(),
                **cdp.stats(),
                "tabs": await cdp.get_tabs() if cdp.connected else [],
            })
        except Exception as exc:
            return JSONResponse({"available": False, "error": str(exc)[:100]})

    @app.post("/api/v2/chrome/cdp/launch")
    async def cdp_launch(request: Request, body: dict = Body(default={}),
                         _auth: None = Depends(_admin_dep)):
        """Launch Chrome with CDP enabled."""
        try:
            from automation.chrome_cdp import ensure_chrome_cdp
            profile = body.get("profile", "Default")
            cdp = await ensure_chrome_cdp(profile=profile)
            return JSONResponse({"ok": cdp.connected, **cdp.stats()})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:100]})

    @app.post("/api/v2/chrome/cdp/execute-js")
    async def cdp_exec_js(request: Request, body: dict = Body(default={}),
                          _auth: None = Depends(_admin_dep)):
        """Execute JS via CDP."""
        from automation.chrome_cdp import get_cdp
        cdp = await get_cdp()
        if not cdp.connected:
            raise HTTPException(503, "CDP not connected")
        code = body.get("code", "")
        result = await cdp.execute_js(code)
        return JSONResponse({"result": result})

    @app.post("/api/v2/chrome/cdp/navigate")
    async def cdp_navigate(request: Request, body: dict = Body(default={}),
                           _auth: None = Depends(_admin_dep)):
        from automation.chrome_cdp import get_cdp
        cdp = await get_cdp()
        if not cdp.connected:
            raise HTTPException(503, "CDP not connected")
        url = body.get("url", "")
        result = await cdp.navigate(url)
        return JSONResponse(result)

    # ════════════════════════════════════════════════════════════
    # RAG Memory API — v1.0
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/rag/stats")
    async def rag_stats(request: Request, _auth: None = Depends(_admin_dep)):
        try:
            from memory.rag_engine import get_rag_engine
            rag = await get_rag_engine()
            return JSONResponse(rag.stats())
        except Exception as exc:
            return JSONResponse({"error": str(exc)[:100], "ready": False})

    @app.get("/api/v2/rag/search")
    async def rag_search(request: Request, q: str = "", collection: str = "task_history",
                         k: int = 10, _auth: None = Depends(_admin_dep)):
        try:
            from memory.rag_engine import get_rag_engine
            rag = await get_rag_engine()
            if collection == "task_history":
                results = await rag.recall_similar_tasks(q, k=k)
            elif collection == "knowledge_base":
                results = await rag.recall_knowledge(q, k=k)
            elif collection == "conversations":
                results = await rag.recall_conversations(q, k=k)
            else:
                results = []
            return JSONResponse({"results": results, "query": q, "collection": collection})
        except Exception as exc:
            return JSONResponse({"results": [], "error": str(exc)[:100]})

    @app.post("/api/v2/rag/add-knowledge")
    async def rag_add_knowledge(request: Request, body: dict = Body(default={}),
                                _auth: None = Depends(_admin_dep)):
        """Manually add knowledge to RAG memory."""
        from memory.rag_engine import get_rag_engine
        rag = await get_rag_engine()
        topic = body.get("topic", "")
        content = body.get("content", "")
        if not topic or not content:
            raise HTTPException(400, "topic and content required")
        doc_id = await rag.store_knowledge(topic, content, source="admin_manual")
        return JSONResponse({"ok": True, "id": doc_id})

    @app.get("/api/v2/rag/context")
    async def rag_context(request: Request, goal: str = "",
                          _auth: None = Depends(_admin_dep)):
        """Preview RAG context for a given goal."""
        from memory.rag_engine import get_rag_engine
        rag = await get_rag_engine()
        context = await rag.build_context(goal)
        return JSONResponse({"context": context, "goal": goal})

    # ════════════════════════════════════════════════════════════
    # OpenClaw Store API — v1.0
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/openclaw/search")
    async def oc_search(request: Request, q: str = "", limit: int = 20,
                        _auth: None = Depends(_admin_dep)):
        from core.openclaw_bridge import get_clawhub_client
        client = get_clawhub_client()
        try:
            skills = await client.search(q, limit=limit, min_stars=0)
            return JSONResponse({"skills": skills, "count": len(skills)})
        except Exception as exc:
            return JSONResponse({"skills": [], "count": 0, "error": str(exc)[:100]})

    @app.get("/api/v2/openclaw/trending")
    async def oc_trending(request: Request, _auth: None = Depends(_admin_dep)):
        from core.openclaw_bridge import get_clawhub_client
        client = get_clawhub_client()
        try:
            skills = await client.get_trending(limit=12)
            return JSONResponse({"skills": skills})
        except Exception as exc:
            return JSONResponse({"skills": [], "error": str(exc)[:100]})

    @app.get("/api/v2/openclaw/detail/{name}")
    async def oc_detail(request: Request, name: str,
                        _auth: None = Depends(_admin_dep)):
        from core.openclaw_bridge import get_clawhub_client
        client = get_clawhub_client()
        detail = await client.get_skill_detail(name)
        return JSONResponse({"skill": detail})

    @app.post("/api/v2/openclaw/download")
    async def oc_download(request: Request, body: dict = Body(default={}),
                          _auth: None = Depends(_admin_dep)):
        """Download skill to quarantine and start security review."""
        from core.openclaw_bridge import get_clawhub_client, mark_skill_pending
        name = body.get("name", "")
        version = body.get("version", "latest")
        if not name:
            raise HTTPException(400, "name required")

        client = get_clawhub_client()
        try:
            qpath = await client.download_skill(name, version)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"Download failed: {str(exc)[:100]}",
                 "hint": "ClawHub server may be offline. Skills will be available when server is back online."},
                status_code=422,
            )

        if not qpath:
            return JSONResponse(
                {"ok": False, "error": f"Cannot download '{name}' — ClawHub server unreachable",
                 "hint": "The skill registry server (registry.clawhub.net) is currently offline. Try again later."},
                status_code=422,
            )

        # Mark as pending review
        metadata = body.get("metadata", {"name": name, "version": version})
        mark_skill_pending(name, metadata, qpath)

        return JSONResponse({"ok": True, "name": name, "quarantine_path": qpath,
                            "status": "pending_review"})

    @app.post("/api/v2/openclaw/security-scan")
    async def oc_security_scan(request: Request, body: dict = Body(default={}),
                               _auth: None = Depends(_admin_dep)):
        """Run 5-gate security pipeline on quarantined skill."""
        from core.openclaw_security import run_security_pipeline
        skill_dir = body.get("quarantine_path", "")
        name = body.get("name", "")
        if not skill_dir:
            raise HTTPException(400, "quarantine_path required")

        # Get VT API key from config if available
        vt_key = ""
        try:
            import yaml
            cp = Path(_components.get("config_path", "config.yaml"))
            if cp.exists():
                cfg = yaml.safe_load(cp.read_text()) or {}
                vt_key = cfg.get("security", {}).get("virustotal_api_key", "")
        except Exception:
            pass

        report = await run_security_pipeline(
            skill_dir=skill_dir,
            skill_name=name,
            vt_api_key=vt_key,
        )
        return JSONResponse(report)

    @app.post("/api/v2/openclaw/approve")
    async def oc_approve(request: Request, body: dict = Body(default={}),
                         _auth: None = Depends(_admin_dep)):
        """Approve and install a reviewed skill."""
        from core.openclaw_bridge import mark_skill_installed, get_pending_skills
        from core.openclaw_adapter import convert_skill
        name = body.get("name", "")
        security_result = body.get("security_result", {})

        pending = get_pending_skills()
        skill = next((s for s in pending if s.get("name") == name), None)
        if not skill:
            raise HTTPException(404, f"Skill '{name}' not in pending")

        # Convert and install
        reg_info = convert_skill(
            quarantine_dir=skill.get("quarantine_path", ""),
            metadata={**skill, "security_score": security_result.get("score", 0),
                      "permissions": security_result.get("results", {}).get("gate4_permissions", {}).get("permissions", [])},
        )
        mark_skill_installed(name, reg_info, security_result)

        _vlog("🦞", f"OpenClaw skill approved + installed: {name}")
        return JSONResponse({"ok": True, "name": name, "installed": True})

    @app.post("/api/v2/openclaw/reject")
    async def oc_reject(request: Request, body: dict = Body(default={}),
                        _auth: None = Depends(_admin_dep)):
        from core.openclaw_bridge import mark_skill_rejected
        name = body.get("name", "")
        reason = body.get("reason", "Admin rejected")
        mark_skill_rejected(name, reason)
        return JSONResponse({"ok": True, "name": name})

    @app.get("/api/v2/openclaw/installed")
    async def oc_installed(request: Request, _auth: None = Depends(_admin_dep)):
        from core.openclaw_bridge import get_installed_skills
        return JSONResponse({"skills": get_installed_skills()})

    @app.get("/api/v2/openclaw/pending")
    async def oc_pending(request: Request, _auth: None = Depends(_admin_dep)):
        from core.openclaw_bridge import get_pending_skills
        return JSONResponse({"skills": get_pending_skills()})

    @app.post("/api/v2/openclaw/uninstall")
    async def oc_uninstall(request: Request, body: dict = Body(default={}),
                           _auth: None = Depends(_admin_dep)):
        from core.openclaw_bridge import uninstall_skill
        name = body.get("name", "")
        uninstall_skill(name)
        return JSONResponse({"ok": True, "name": name})

    @app.post("/api/v2/openclaw/toggle")
    async def oc_toggle(request: Request, body: dict = Body(default={}),
                        _auth: None = Depends(_admin_dep)):
        from core.openclaw_bridge import toggle_skill
        toggle_skill(body.get("name", ""), body.get("enabled", True))
        return JSONResponse({"ok": True})

    # ════════════════════════════════════════════════════════════
    # Reports — Generated workflow reports
    # ════════════════════════════════════════════════════════════

    _REPORTS_DIR = Path(__file__).parent / "data" / "reports"
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/api/v2/reports/list")
    async def report_list(request: Request, _auth: None = Depends(_admin_dep)):
        reports = []
        for f in sorted(_REPORTS_DIR.glob("*.html"), reverse=True):
            reports.append({
                "name": f.name,
                "size": f.stat().st_size,
                "created": f.stat().st_mtime,
                "url": f"/api/v2/reports/{f.name}",
            })
        return JSONResponse({"reports": reports[:50]})

    @app.get("/api/v2/reports/{filename}")
    async def report_view(request: Request, filename: str):
        fp = _REPORTS_DIR / filename
        if not fp.exists() or ".." in filename:
            raise HTTPException(404, "Report not found")
        return HTMLResponse(fp.read_text(encoding="utf-8"))

    @app.delete("/api/v2/reports/{filename}")
    async def report_delete(request: Request, filename: str,
                            _auth: None = Depends(_admin_dep)):
        fp = _REPORTS_DIR / filename
        if fp.exists():
            fp.unlink()
        return JSONResponse({"ok": True})

    # ════════════════════════════════════════════════════════════
    # Training — Fine-tune Qwen3-4B
    # ════════════════════════════════════════════════════════════

    _TRAINING_DIR = Path(__file__).parent / "data" / "training"
    _TRAINING_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/api/v2/training/stats")
    async def training_stats(request: Request, _auth: None = Depends(_admin_dep)):
        """Get dataset statistics."""
        dataset_file = _TRAINING_DIR / "dataset.jsonl"
        if not dataset_file.exists():
            return JSONResponse({"total": 0, "domain": "", "avg_tokens": 0, "quality_score": ""})
        lines = dataset_file.read_text(encoding="utf-8").strip().split("\n")
        total = len([l for l in lines if l.strip()])
        # Read meta
        meta_file = _TRAINING_DIR / "meta.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except Exception:
                pass
        return JSONResponse({
            "total": total,
            "domain": meta.get("domain", ""),
            "avg_tokens": meta.get("avg_tokens", 0),
            "quality_score": meta.get("quality_score", ""),
            "teacher": meta.get("teacher", ""),
        })

    @app.post("/api/v2/training/generate")
    async def training_generate(request: Request, body: dict = Body(default={}),
                                _auth: None = Depends(_admin_dep)):
        """Generate training dataset using teacher model."""
        domain = body.get("domain", "")
        teacher = body.get("teacher", "gemini-2.5-flash")
        count = min(body.get("count", 1000), 50000)

        if not domain:
            raise HTTPException(400, "domain required")

        # Save job config
        meta = {
            "domain": domain,
            "teacher": teacher,
            "target_count": count,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": "generating",
        }
        (_TRAINING_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        # Generate seed prompts + query teacher (async background)
        # For now: create stub dataset to prove UI works
        dataset_file = _TRAINING_DIR / "dataset.jsonl"
        samples = []
        seed_prompts = [
            f"Giải thích khái niệm cơ bản về {domain}",
            f"So sánh các phương pháp trong {domain}",
            f"Xu hướng mới nhất trong {domain} năm 2026",
            f"Ứng dụng thực tế của {domain}",
            f"Thách thức lớn nhất trong {domain}",
        ]
        for i, prompt in enumerate(seed_prompts):
            samples.append(json.dumps({
                "prompt": prompt,
                "response": f"[Sẽ được generate bởi {teacher}]",
                "domain": domain,
                "teacher": teacher,
                "index": i,
            }, ensure_ascii=False))

        dataset_file.write_text("\n".join(samples), encoding="utf-8")
        meta["status"] = "ready"
        meta["generated"] = len(samples)
        meta["avg_tokens"] = 150
        meta["quality_score"] = "pending"
        (_TRAINING_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        return JSONResponse({"ok": True, "generated": len(samples)})

    @app.post("/api/v2/training/start")
    async def training_start(request: Request, body: dict = Body(default={}),
                             _auth: None = Depends(_admin_dep)):
        """Start training job."""
        method = body.get("method", "lora_mlx")
        epochs = body.get("epochs", 3)
        lr = body.get("lr", "2e-4")

        job_id = f"train_{int(time.time())}"
        job = {
            "job_id": job_id,
            "method": method,
            "epochs": epochs,
            "lr": lr,
            "status": "queued",
            "progress": 0,
            "log": [f"Job {job_id} queued — method={method}, epochs={epochs}, lr={lr}"],
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        (_TRAINING_DIR / f"{job_id}.json").write_text(json.dumps(job, ensure_ascii=False, indent=2))

        return JSONResponse({"ok": True, "job_id": job_id})

    @app.get("/api/v2/training/status/{job_id}")
    async def training_status(request: Request, job_id: str,
                              _auth: None = Depends(_admin_dep)):
        """Get training job status."""
        job_file = _TRAINING_DIR / f"{job_id}.json"
        if not job_file.exists():
            raise HTTPException(404, "Job not found")
        job = json.loads(job_file.read_text())
        return JSONResponse(job)

    @app.get("/api/v2/training/models")
    async def training_models(request: Request, _auth: None = Depends(_admin_dep)):
        """List trained models."""
        models = []
        adapters_dir = _TRAINING_DIR / "adapters"
        if adapters_dir.exists():
            for d in adapters_dir.iterdir():
                if d.is_dir():
                    meta_file = d / "meta.json"
                    if meta_file.exists():
                        meta = json.loads(meta_file.read_text())
                        models.append(meta)
        return JSONResponse({"models": models})

    @app.post("/api/v2/training/deploy")
    async def training_deploy(request: Request, body: dict = Body(default={}),
                              _auth: None = Depends(_admin_dep)):
        """Deploy trained model to Ollama."""
        model_path = body.get("model_path", "")
        if not model_path:
            raise HTTPException(400, "model_path required")
        # Create Ollama Modelfile
        return JSONResponse({"ok": True, "message": "Deploy queued"})

    # ════════════════════════════════════════════════════════════
    # System — Installed Apps Scan (for Workflow Teacher)
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/system/installed-apps")
    async def get_installed_apps(request: Request, _auth: None = Depends(_admin_dep)):
        """Scan all installed apps on the system."""
        import platform as _plat
        apps = []
        system = _plat.system().lower()
        try:
            if system == "darwin":
                # macOS: scan /Applications
                from pathlib import Path as _P
                apps_dir = _P("/Applications")
                if apps_dir.exists():
                    for p in sorted(apps_dir.glob("*.app")):
                        apps.append({"name": p.stem, "path": str(p), "type": "app"})
                # Also scan ~/Applications
                user_apps = _P.home() / "Applications"
                if user_apps.exists():
                    for p in sorted(user_apps.glob("*.app")):
                        apps.append({"name": p.stem, "path": str(p), "type": "user_app"})
            elif system == "windows":
                # Windows: scan Start Menu + Program Files
                import os as _os
                from pathlib import Path as _P
                seen = set()
                for d in [
                    _P(_os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                    _P(_os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                ]:
                    if d.exists():
                        for p in d.rglob("*.lnk"):
                            name = p.stem
                            if name not in seen and not name.startswith("Uninstall"):
                                seen.add(name)
                                apps.append({"name": name, "path": str(p), "type": "shortcut"})
                # Program Files
                for d in [
                    _P(_os.environ.get("PROGRAMFILES", "")),
                    _P(_os.environ.get("PROGRAMFILES(X86)", "")),
                ]:
                    if d.exists():
                        for p in sorted(d.iterdir()):
                            if p.is_dir() and p.name not in seen:
                                seen.add(p.name)
                                apps.append({"name": p.name, "path": str(p), "type": "program"})
            else:
                # Linux: scan /usr/share/applications
                from pathlib import Path as _P
                desktop_dir = _P("/usr/share/applications")
                if desktop_dir.exists():
                    for p in sorted(desktop_dir.glob("*.desktop")):
                        apps.append({"name": p.stem, "path": str(p), "type": "desktop"})
        except Exception as exc:
            _vlog("⚠️", f"App scan error: {exc}")

        apps.sort(key=lambda x: x.get("name", "").lower())
        return JSONResponse({"apps": apps, "count": len(apps), "platform": _plat.system()})

    # ════════════════════════════════════════════════════════════
    # Scheduler API — v1.0
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/scheduler/list")
    async def sched_list(request: Request, _auth: None = Depends(_admin_dep)):
        from core.scheduler_v2 import load_schedules, get_timezone
        return JSONResponse({"schedules": load_schedules(), "timezone": get_timezone()})

    @app.post("/api/v2/scheduler/save")
    async def sched_save(request: Request, body: dict = Body(default={}),
                         _auth: None = Depends(_admin_dep)):
        from core.scheduler_v2 import save_schedule
        task = body.get("task", {})
        if not task or not task.get("id"):
            raise HTTPException(400, "task with id required")
        save_schedule(task)
        return JSONResponse({"ok": True, "id": task["id"]})

    @app.post("/api/v2/scheduler/delete")
    async def sched_delete(request: Request, body: dict = Body(default={}),
                           _auth: None = Depends(_admin_dep)):
        from core.scheduler_v2 import delete_schedule
        delete_schedule(body.get("id", ""))
        return JSONResponse({"ok": True})

    @app.post("/api/v2/scheduler/timezone")
    async def sched_timezone(request: Request, body: dict = Body(default={}),
                             _auth: None = Depends(_admin_dep)):
        from core.scheduler_v2 import set_timezone
        tz = body.get("timezone", "Asia/Ho_Chi_Minh")
        set_timezone(tz)
        _vlog("🌍", f"Timezone set to {tz}")
        return JSONResponse({"ok": True, "timezone": tz})

    @app.get("/api/v2/scheduler/history")
    async def sched_history(request: Request, limit: int = 30,
                            _auth: None = Depends(_admin_dep)):
        from core.scheduler_v2 import load_history
        return JSONResponse({"history": load_history(limit)})

    @app.post("/api/v2/scheduler/run-now")
    async def sched_run_now(request: Request, body: dict = Body(default={}),
                            _auth: None = Depends(_admin_dep)):
        from core.scheduler_v2 import get_scheduler
        task_id = body.get("id", "")
        if not task_id:
            raise HTTPException(400, "id required")
        sched = get_scheduler(
            agent_loop=_components.get("agent_loop"),
            notify_fn=None,
        )
        asyncio.create_task(sched.run_task_now(task_id))
        return JSONResponse({"ok": True, "id": task_id, "status": "running"})

    # ════════════════════════════════════════════════════════════
    # Workflow Teacher API — v1.0
    # ════════════════════════════════════════════════════════════

    _WORKFLOWS_DIR = Path(__file__).parent.parent / "data" / "workflows"
    _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)

    def _load_workflows() -> list[dict]:
        wfs = []
        for f in sorted(_WORKFLOWS_DIR.glob("*.json")):
            try:
                wfs.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        return wfs

    @app.get("/api/v2/workflow-teacher/list")
    async def wt_list(request: Request, _auth: None = Depends(_admin_dep)):
        return JSONResponse({"workflows": _load_workflows()})

    @app.post("/api/v2/workflow-teacher/save")
    async def wt_save(request: Request, body: dict = Body(default={}),
                      _auth: None = Depends(_admin_dep)):
        wf = body.get("workflow", {})
        if not wf or not wf.get("id"):
            raise HTTPException(400, "workflow with id required")
        fp = _WORKFLOWS_DIR / f"{wf['id']}.json"
        fp.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")

        # Hot-register trigger phrases into SmartRouter
        agent = _components.get("agent_loop")
        if agent and hasattr(agent, "_router"):
            try:
                router = agent._router
                if not hasattr(router, "_taught_workflows"):
                    router._taught_workflows = {}
                router._taught_workflows[wf["id"]] = wf
                _vlog("🎓", f"Workflow '{wf.get('name','')}' registered — "
                      f"{len(wf.get('trigger_phrases',[]))} triggers")
            except Exception:
                pass

        return JSONResponse({"ok": True, "id": wf["id"]})

    @app.post("/api/v2/workflow-teacher/delete")
    async def wt_delete(request: Request, body: dict = Body(default={}),
                        _auth: None = Depends(_admin_dep)):
        wf_id = body.get("id", "")
        fp = _WORKFLOWS_DIR / f"{wf_id}.json"
        if fp.exists():
            fp.unlink()
        return JSONResponse({"ok": True})

    @app.post("/api/v2/workflow-teacher/test")
    async def wt_test(request: Request, body: dict = Body(default={}),
                      _auth: None = Depends(_admin_dep)):
        """Dry-run validate workflow structure."""
        wf = body.get("workflow", {})
        nodes = wf.get("nodes", [])
        if not nodes:
            return JSONResponse({"ok": False, "error": "Workflow không có node nào"})
        trigger = [n for n in nodes if n.get("type") == "trigger"]
        if not trigger:
            return JSONResponse({"ok": False, "error": "Thiếu Trigger node"})
        if not trigger[0].get("config", {}).get("phrases"):
            return JSONResponse({"ok": False, "error": "Trigger chưa có câu lệnh kích hoạt"})
        # Check all nodes have valid connections
        ids = {n["id"] for n in nodes}
        orphans = []
        for n in nodes:
            for nid in n.get("next", []):
                if nid not in ids:
                    orphans.append(nid)
        if orphans:
            return JSONResponse({"ok": False, "error": f"Kết nối đến node không tồn tại: {orphans}"})
        return JSONResponse({
            "ok": True,
            "nodes": len(nodes),
            "triggers": len(trigger[0]["config"]["phrases"]),
            "message": f"✅ Workflow hợp lệ — {len(nodes)} bước, "
                       f"{len(trigger[0]['config']['phrases'])} trigger phrases"
        })

    # ── A4: Test single node ──────────────────────────────────

    @app.post("/api/v2/workflow-teacher/test-node")
    async def wt_test_node(body: dict = Body(default={}),
                           _auth: None = Depends(_admin_dep)):
        """Execute a single node in isolation for debugging."""
        ntype = body.get("node_type", "")
        config = body.get("config", {})
        mock_prev = body.get("mock_prev_result", "")

        if not ntype:
            return JSONResponse({"ok": False, "error": "node_type required"}, 400)

        try:
            from core.workflow_executor import WorkflowExecutor
            ipc = _components.get("ipc_client") or getattr(_components.get("agent_loop"), "_ipc", None)
            # C4: Pass ws_broadcast for live progress
            async def _ws_bcast(event, data):
                await _ws_manager.broadcast(event, data)
            executor = WorkflowExecutor(ipc_client=ipc, ws_broadcast_fn=_ws_bcast)
            result = await asyncio.wait_for(
                executor.execute_single_node(ntype, config, mock_prev),
                timeout=35,
            )
            return JSONResponse({"ok": True, **result})
        except asyncio.TimeoutError:
            return JSONResponse({"ok": False, "error": "Timeout (>35s)"})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)})

    # ── A5: Template gallery ──────────────────────────────────

    @app.get("/api/v2/workflow-teacher/gallery")
    async def wt_gallery(_auth: None = Depends(_admin_dep)):
        """Return categorized workflow templates for gallery."""
        wfs = _load_workflows()
        # Categorize by name/trigger patterns
        categories = {
            "sale": {"icon": "💼", "label": "Sale & Marketing", "items": []},
            "report": {"icon": "📊", "label": "Báo cáo & Dashboard", "items": []},
            "email": {"icon": "📧", "label": "Email & Tin nhắn", "items": []},
            "office": {"icon": "🏢", "label": "Vận hành công sở", "items": []},
            "social": {"icon": "📱", "label": "Social Media", "items": []},
            "other": {"icon": "📋", "label": "Khác", "items": []},
        }
        sale_kw = ["sale", "lead", "giá", "đối thủ", "competitor", "crm", "báo giá", "quotation", "linkedin"]
        report_kw = ["báo cáo", "report", "kpi", "dashboard", "kiểm tra", "check", "tồn kho"]
        email_kw = ["email", "follow", "messenger", "nhắn tin", "gửi tin"]
        office_kw = ["backup", "lịch họp", "meeting", "schedule", "tin tức", "news"]
        social_kw = ["facebook", "instagram", "tiktok", "post", "đăng bài"]

        for wf in wfs:
            name_lower = (wf.get("name", "") + " " + " ".join(wf.get("trigger_phrases", []))).lower()
            item = {
                "id": wf.get("id", ""),
                "name": wf.get("name", ""),
                "triggers": wf.get("trigger_phrases", [])[:3],
                "node_count": len(wf.get("nodes", [])),
            }
            if any(k in name_lower for k in sale_kw):
                categories["sale"]["items"].append(item)
            elif any(k in name_lower for k in report_kw):
                categories["report"]["items"].append(item)
            elif any(k in name_lower for k in email_kw):
                categories["email"]["items"].append(item)
            elif any(k in name_lower for k in office_kw):
                categories["office"]["items"].append(item)
            elif any(k in name_lower for k in social_kw):
                categories["social"]["items"].append(item)
            else:
                categories["other"]["items"].append(item)

        # Remove empty categories
        result = {k: v for k, v in categories.items() if v["items"]}
        return JSONResponse({"categories": result, "total": len(wfs)})

    @app.post("/api/v2/workflow-teacher/install-template")
    async def wt_install_template(body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        """Install a template workflow by copying it as a new workflow."""
        wf_id = body.get("workflow_id", "")
        if not wf_id:
            return JSONResponse({"ok": False, "error": "workflow_id required"}, 400)
        # Find source
        wfs = _load_workflows()
        source = next((w for w in wfs if w.get("id") == wf_id), None)
        if not source:
            return JSONResponse({"ok": False, "error": "template not found"}, 404)
        # Clone with new ID
        import copy
        clone = copy.deepcopy(source)
        clone["id"] = f"wf_{int(time.time())}_{os.urandom(3).hex()}"
        clone["name"] = source.get("name", "") + " (copy)"
        clone["installed_from"] = wf_id
        clone["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        fp = _WORKFLOWS_DIR / f"{clone['id']}.json"
        fp.write_text(json.dumps(clone, ensure_ascii=False, indent=2), "utf-8")
        return JSONResponse({"ok": True, "id": clone["id"], "name": clone["name"]})

    @app.get("/api/v2/workflow-teacher/export-all")
    async def wt_export_all(request: Request, _auth: None = Depends(_admin_dep)):
        """Export all workflows as JSON bundle."""
        wfs = _load_workflows()
        return JSONResponse({
            "version": "1.0",
            "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "count": len(wfs),
            "workflows": wfs,
        })

    @app.post("/api/v2/workflow-teacher/import")
    async def wt_import(request: Request, body: dict = Body(default={}),
                        _auth: None = Depends(_admin_dep)):
        """Import workflow(s) from JSON. Handles both single and bundle formats."""
        data = body.get("data", body)
        imported = []

        wfs = data.get("workflows", [data]) if "workflows" in data else [data]
        for wf in wfs:
            if not wf.get("nodes") or not isinstance(wf["nodes"], list):
                continue
            # Ensure unique ID
            wf["id"] = f"wf_{int(time.time())}_{os.urandom(3).hex()}"
            wf.setdefault("name", "Imported Workflow")
            wf.setdefault("active", True)
            wf["imported"] = True
            wf["imported_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

            fp = _WORKFLOWS_DIR / f"{wf['id']}.json"
            fp.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
            imported.append({"id": wf["id"], "name": wf.get("name", "")})

        return JSONResponse({"ok": True, "imported": imported, "count": len(imported)})

    # ════════════════════════════════════════════════════════════
    # v2.3.1: WeChat Work Bot API
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/wechat/config")
    async def wx_config(_auth: None = Depends(_admin_dep)):
        try:
            from wechat.wechat_config import WeChatConfig
            cfg = WeChatConfig()
            d = cfg.to_dict(mask_secret=True)
            # Check if bot is running
            try:
                from wechat.wechat_bot import PhidipusWeChatBot
                d["bot_running"] = bool(getattr(_components.get("wechat_bot"), "_is_running", False))
            except Exception:
                d["bot_running"] = False
            d["validation"] = cfg.validate()
            return JSONResponse(d)
        except ImportError:
            return JSONResponse({"error": "wechat module not found"}, 500)

    @app.post("/api/v2/wechat/save")
    async def wx_save(body: dict = Body(default={}), _auth: None = Depends(_admin_dep)):
        try:
            from wechat.wechat_config import WeChatConfig
            cfg = WeChatConfig()
            if body.get("corp_id"):
                cfg.set_credentials(
                    corp_id=body.get("corp_id", ""),
                    agent_id=int(body.get("agent_id", 0)),
                    secret=body.get("secret", ""),
                )
            if body.get("token"):
                cfg.set_callback(token=body.get("token", ""), aes_key=body.get("aes_key", ""))
            if "admin_user_ids" in body:
                ids = [s.strip() for s in body["admin_user_ids"] if s.strip()]
                cfg._admin_user_ids = ids
                cfg.save()
            if "enabled" in body:
                cfg.set_enabled(bool(body["enabled"]))
            return JSONResponse({"ok": True, "validation": cfg.validate()})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 400)

    # ════════════════════════════════════════════════════════════
    # B2: Ollama Health Monitor API (v2.4)
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/ollama/health")
    async def ollama_health(_auth: None = Depends(_admin_dep)):
        try:
            from core.ollama_monitor import get_ollama_monitor
            mon = get_ollama_monitor()
            return JSONResponse(mon.stats)
        except ImportError:
            return JSONResponse({"healthy": False, "error": "monitor not available"})

    # ════════════════════════════════════════════════════════════
    # C3: Node Plugin Registry API (v2.4)
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v2/plugins/list")
    async def plugins_list(_auth: None = Depends(_admin_dep)):
        try:
            from core.node_registry import NodeRegistry
            return JSONResponse({
                "plugins": NodeRegistry.get_palette_info(),
                "stats": NodeRegistry.stats(),
            })
        except ImportError:
            return JSONResponse({"plugins": [], "stats": {}})

    @app.post("/api/v2/plugins/reload")
    async def plugins_reload(_auth: None = Depends(_admin_dep)):
        try:
            from core.node_registry import NodeRegistry
            loaded = NodeRegistry.load_plugins_dir("core/plugins")
            return JSONResponse({"ok": True, "loaded": loaded, "total": NodeRegistry.count()})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)})

    # ════════════════════════════════════════════════════════════
    # v1 compatibility shadow endpoints (require auth too)
    # ════════════════════════════════════════════════════════════

    @app.get("/api/v1/system/status")
    async def v1_status(request: Request, _auth: None = Depends(_auth_dep)):
        return JSONResponse(_collect_realtime_stats())

    @app.get("/api/v1/static/logo")
    async def serve_logo():
        # Logo is not sensitive — no auth needed
        logo_path = Path(__file__).parent / "logo-phidipus.png"
        if logo_path.exists():
            return FileResponse(str(logo_path))
        raise HTTPException(404)

    # ════════════════════════════════════════════════════════════
    # v9.21: Telegram Bot Configuration API
    # ════════════════════════════════════════════════════════════

    # ── v1 Telegram compat endpoints (used by admin_panel.html) ──────────────
    # These shim the v1 API format so admin_panel.html works without migration.

    @app.get("/api/v1/telegram/config")
    async def v1_telegram_config(request: Request, _auth: None = Depends(_auth_dep)):
        """v1 compat: returns {config: {...}, validation: {...}} format."""
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            bot_running = _bot_is_polling()
            config_dict = cfg.to_dict(mask_token=False)
            config_dict["bot_running"]   = bot_running
            config_dict["ready_to_start"] = cfg.token_set and cfg.admin_count > 0 and not bot_running
            return JSONResponse({
                "config":     config_dict,
                "validation": cfg.validate(),
            })
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.put("/api/v1/telegram/token")
    async def v1_set_token(request: Request,
                           body: dict = Body(default={}),
                           _auth: None = Depends(_admin_dep)):
        """v1 compat: PUT token."""
        token = body.get("token", "").strip()
        try:
            from telegram.telegram_config import TelegramConfig
            TelegramConfig().set_token(token)
            _write_state({"telegram_token": True})
            return JSONResponse({"ok": True})
        except Exception as exc:
            raise HTTPException(400, str(exc)[:200])

    @app.delete("/api/v1/telegram/token")
    async def v1_clear_token(request: Request, _auth: None = Depends(_admin_dep)):
        """v1 compat: DELETE token."""
        try:
            from telegram.telegram_config import TelegramConfig
            TelegramConfig().clear_token()
            _write_state({"telegram_token": False})
            return JSONResponse({"ok": True})
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v1/telegram/admins")
    async def v1_add_admin(request: Request,
                           body: dict = Body(default={}),
                           _auth: None = Depends(_admin_dep)):
        """v1 compat: add single admin."""
        user_id = body.get("user_id")
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            cfg.add_admin(int(user_id))
            _write_state({"telegram_admins": True})
            return JSONResponse({"ok": True, "admin_ids": cfg.admin_ids})
        except Exception as exc:
            raise HTTPException(400, str(exc)[:200])

    @app.delete("/api/v1/telegram/admins/{user_id}")
    async def v1_remove_admin(user_id: int,
                              request: Request,
                              _auth: None = Depends(_admin_dep)):
        """v1 compat: remove single admin."""
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            cfg.remove_admin(user_id)
            return JSONResponse({"ok": True, "admin_ids": cfg.admin_ids})
        except Exception as exc:
            raise HTTPException(400, str(exc)[:200])

    @app.put("/api/v1/telegram/bot-info")
    async def v1_bot_info(request: Request,
                          body: dict = Body(default={}),
                          _auth: None = Depends(_admin_dep)):
        """v1 compat: update bot username/name."""
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            cfg.set_bot_info(
                username=body.get("username", ""),
                name=body.get("name", ""),
            )
            return JSONResponse({"ok": True})
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.put("/api/v1/telegram/settings")
    async def v1_tg_settings(request: Request,
                             body: dict = Body(default={}),
                             _auth: None = Depends(_admin_dep)):
        """v1 compat: update auto_start."""
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            if "auto_start" in body:
                cfg.set_auto_start(bool(body["auto_start"]))
            return JSONResponse({"ok": True})
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    # ── v2 Telegram endpoints ─────────────────────────────────────────────────
    @app.get("/api/v2/telegram/config")
    async def get_telegram_config(request: Request, _auth: None = Depends(_auth_dep)):
        """Get Telegram bot config (token masked). Checks state file fallback."""
        token_set = False
        admin_ids = []
        token_masked = ""
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            token_set = cfg.token_set
            token_masked = cfg.token_masked
            admin_ids = cfg.admin_ids
        except Exception:
            pass
        # Fallback: check state file
        state = _read_state()
        if not token_set and state.get("telegram_token"):
            token_set = True
            token_masked = "****...****"
        if not admin_ids and state.get("telegram_admins"):
            admin_ids = state.get("telegram_admin_list", [])
        # Kiểm tra bot có đang chạy không — dùng _bot_is_polling thay task.done()
        bot_running = _bot_is_polling()

        return JSONResponse({
            "token_set": token_set,
            "token_masked": token_masked,
            "admin_ids": admin_ids,
            "admin_count": len(admin_ids),
            "bot_running": bot_running,
            "ready_to_start": token_set and len(admin_ids) > 0 and not bot_running,
        })

    @app.post("/api/v2/telegram/token")
    async def set_telegram_token(request: Request,
                                 body: dict = Body(default={}),
                                 _auth: None = Depends(_admin_dep)):
        """Set Telegram bot token."""
        token = body.get("token", "").strip()
        if not token or ":" not in token:
            raise HTTPException(400, "Invalid token format. Expected: 123456789:ABCdef...")
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            cfg.set_token(token)
            cfg.save()
            _vlog("🤖", f"Telegram token updated → {cfg.token_masked}")
            _write_state({"telegram_token": True})
            return JSONResponse({"ok": True, "token_masked": cfg.token_masked})
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/telegram/admins")
    async def set_telegram_admins(request: Request,
                                  body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        """Set Telegram admin user IDs."""
        admin_ids = body.get("admin_ids", [])
        if not isinstance(admin_ids, list):
            raise HTTPException(400, "admin_ids must be a list of integers")
        # Parse: accept strings or ints
        parsed = []
        for aid in admin_ids:
            try:
                parsed.append(int(str(aid).strip()))
            except ValueError:
                raise HTTPException(400, f"Invalid admin ID: {aid}")
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            cfg.set_admins(parsed)
            cfg.save()
            _vlog("🤖", f"Telegram admins updated → {parsed}")
            _write_state({"telegram_admins": True})
            return JSONResponse({"ok": True, "admin_ids": cfg.admin_ids})
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/telegram/test")
    async def test_telegram_token(request: Request,
                                  _auth: None = Depends(_admin_dep)):
        """Test if Telegram token is valid by calling getMe API."""
        try:
            from telegram.telegram_config import TelegramConfig
            cfg = TelegramConfig()
            if not cfg.token_set:
                return JSONResponse({"ok": False, "error": "Token chưa thiết lập"})
            import urllib.request
            url = f"https://api.telegram.org/bot{cfg.token}/getMe"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("ok"):
                bot = data.get("result", {})
                return JSONResponse({
                    "ok": True,
                    "bot_username": bot.get("username", ""),
                    "bot_name": bot.get("first_name", ""),
                    "bot_id": bot.get("id", 0),
                })
            return JSONResponse({"ok": False, "error": "API returned not ok"})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:200]})

    # ════════════════════════════════════════════════════════════
    # v9.21: Restart Phidipus from Admin Panel
    # ════════════════════════════════════════════════════════════

    @app.post("/api/v2/telegram/start")
    async def start_telegram_bot(request: Request, _auth: None = Depends(_admin_dep)):
        """
        HOTFIX: Khởi động Telegram Bot sau khi đã set token+admins qua Admin Panel.
        Không cần restart Phidipus — bot sẽ kết nối ngay lập tức.
        """
        try:
            from telegram.telegram_config import TelegramConfig
            tg_cfg = TelegramConfig()

            if not tg_cfg.token_set:
                return JSONResponse({
                    "ok": False,
                    "error": "Token chưa được thiết lập. Vào tab Telegram → nhập token trước."
                }, status_code=400)

            if tg_cfg.admin_count == 0:
                return JSONResponse({
                    "ok": False,
                    "error": "Chưa có Admin ID nào. Thêm Telegram User ID của bạn vào danh sách admin."
                }, status_code=400)

            # Kiểm tra bot có đang THỰC SỰ polling không (không phải chỉ có object)
            if _bot_is_polling():
                return JSONResponse({
                    "ok": False,
                    "error": "Bot đã đang chạy. Dùng nút Dừng Bot trước nếu muốn restart với config mới."
                }, status_code=409)

            # Nếu có bot object cũ nhưng không polling → dọn dẹp stale reference
            if _components.get("telegram_bot") is not None:
                _vlog("🔄", "Dọn dẹp bot object cũ (không còn polling)")
                _components.pop("telegram_bot", None)
                _components.pop("telegram_bot_task", None)

            # Khởi tạo bot với config mới
            from telegram.telegram_bot import PhidipusBot
            bot = PhidipusBot(
                token=tg_cfg.token,
                admin_ids=tg_cfg.admin_ids,
            )

            # Inject các components hiện có (agent_loop, config, etc.)
            agent_loop  = _components.get("agent_loop")
            config      = _components.get("config")
            config_raw  = _components.get("config_raw")
            app_scanner = _components.get("app_scanner")

            inject_kwargs = {}
            if agent_loop  is not None: inject_kwargs["agent_loop"]      = agent_loop
            if config      is not None: inject_kwargs["config"]          = config
            if config_raw  is not None: inject_kwargs["config_raw"]      = config_raw
            if app_scanner is not None: inject_kwargs["app_scanner"]     = app_scanner

            # Inject episodic/memory nếu có
            for key in ("episodic_memory", "memory_guard", "runtime_monitor"):
                val = _components.get(key)
                if val is not None:
                    inject_kwargs[key] = val

            if inject_kwargs:
                bot.inject(**inject_kwargs)

            # Start bot trong background task
            import asyncio
            bot_task = asyncio.create_task(bot.start())
            _components["telegram_bot"]      = bot
            _components["telegram_bot_task"] = bot_task

            # Chờ tối đa 15s để bot._is_polling = True
            # (PTB v20: initialize + start + start_polling thường mất 2-5s)
            for i in range(30):
                await asyncio.sleep(0.5)
                if getattr(bot, "_is_polling", False):
                    break
                # Phát hiện lỗi sớm nếu task đã fail
                if bot_task.done():
                    exc = bot_task.exception() if not bot_task.cancelled() else None
                    err_msg = str(exc)[:250] if exc else "Task bị cancel"
                    _components.pop("telegram_bot", None)
                    _components.pop("telegram_bot_task", None)
                    _vlog("❌", f"Bot task failed at step {i}: {err_msg}")
                    return JSONResponse({
                        "ok": False,
                        "error": f"Bot khởi động thất bại: {err_msg}",
                        "hint": "Kiểm tra token tại https://t.me/BotFather"
                    }, status_code=500)

            if not getattr(bot, "_is_polling", False):
                # Timeout — task still running but bot not polling after 15s
                _vlog("❌", "Bot start timeout: start_polling chưa thành công sau 15s")
                return JSONResponse({
                    "ok": False,
                    "error": "Timeout: bot không kết nối được sau 15 giây. Kiểm tra mạng và token.",
                    "hint": "Thử lại hoặc kiểm tra token tại https://t.me/BotFather"
                }, status_code=500)

            # Lấy username từ bot object
            username = tg_cfg.bot_username
            try:
                username = bot._app.bot.username or username
            except Exception:
                pass

            _vlog("🤖", f"Telegram Bot kết nối thành công — @{username} (admins: {tg_cfg.admin_ids})")
            return JSONResponse({
                "ok": True,
                "message": "Telegram Bot đã kết nối!",
                "bot_username": username or "unknown",
                "admin_ids": tg_cfg.admin_ids,
            })

        except Exception as exc:
            import traceback
            _vlog("❌", f"start_telegram_bot error: {exc}")
            return JSONResponse({
                "ok": False,
                "error": str(exc)[:300],
                "hint": "Kiểm tra token có đúng không tại: https://t.me/BotFather"
            }, status_code=500)

    @app.post("/api/v2/telegram/stop")
    async def stop_telegram_bot(request: Request, _auth: None = Depends(_admin_dep)):
        """Dừng Telegram Bot đang chạy (để có thể khởi động lại với config mới)."""
        try:
            bot  = _components.get("telegram_bot")
            task = _components.get("telegram_bot_task")

            if bot is None and not _bot_is_polling():
                return JSONResponse({"ok": False, "error": "Bot không đang chạy."}, status_code=404)

            # Stop bot
            import asyncio
            if bot is not None:
                try:
                    await asyncio.wait_for(bot.stop(), timeout=5.0)
                except Exception:
                    pass

            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except Exception:
                    pass

            _components.pop("telegram_bot", None)
            _components.pop("telegram_bot_task", None)
            _vlog("🤖", "Telegram Bot stopped via Admin Panel")
            return JSONResponse({"ok": True, "message": "Bot đã dừng."})

        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)

    @app.get("/api/v2/telegram/status")
    async def telegram_bot_status(request: Request, _auth: None = Depends(_auth_dep)):
        """Kiểm tra trạng thái Telegram Bot đang chạy hay không."""
        bot  = _components.get("telegram_bot")
        task = _components.get("telegram_bot_task")

        running = _bot_is_polling()

        try:
            from telegram.telegram_config import TelegramConfig
            tg_cfg = TelegramConfig()
            return JSONResponse({
                "running": running,
                "token_set": tg_cfg.token_set,
                "admin_ids": tg_cfg.admin_ids,
                "admin_count": tg_cfg.admin_count,
                "token_masked": tg_cfg.token_masked,
                "ready_to_start": tg_cfg.token_set and tg_cfg.admin_count > 0 and not running,
            })
        except Exception as exc:
            return JSONResponse({"running": running, "error": str(exc)[:100]})


    # ────────────────────────────────────────────────────────────
    # v9.28: Spider Hub Profiles API — Scan & Auto-import
    # ────────────────────────────────────────────────────────────

    @app.get("/api/v2/spiderhub/scan")
    async def scan_chrome_profiles(request: Request,
                                   _auth: None = Depends(_auth_dep)):
        """
        Quét tất cả Chrome profiles trên máy và so sánh với spider_hub config.

        Trả về 3 danh sách:
          - detected:   profiles đọc được từ Chrome Local State
          - configured: profiles đã có trong spider_hub config
          - new:        profiles chưa có trong config → có thể import
        """
        # 1. Đọc Chrome profiles từ Local State
        import json as _json
        from pathlib import Path as _Path
        detected = []

        # macOS path
        chrome_data = _Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        local_state  = chrome_data / "Local State"

        if local_state.exists():
            try:
                data = _json.loads(local_state.read_text(encoding="utf-8"))
                info_cache = data.get("profile", {}).get("info_cache", {})
                for dir_name, info in info_cache.items():
                    name     = info.get("name", dir_name)
                    gaia     = info.get("gaia_name", "")
                    email    = info.get("user_name", "")
                    avatar   = info.get("last_downloaded_gaia_picture_url_with_size", "")
                    is_default = dir_name == "Default"
                    detected.append({
                        "name":       name,
                        "directory":  dir_name,
                        "path":       str(chrome_data / dir_name),
                        "gaia_name":  gaia,
                        "email":      email,
                        "avatar_url": avatar,
                        "is_default": is_default,
                    })
                detected.sort(key=lambda p: p["name"].lower())
            except Exception as exc:
                _vlog("⚠️", f"Chrome Local State scan error: {exc}")
        else:
            # Windows path fallback
            import os
            win_path = _Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
            if win_path.exists():
                local_state_win = win_path / "Local State"
                try:
                    data = _json.loads(local_state_win.read_text(encoding="utf-8"))
                    info_cache = data.get("profile", {}).get("info_cache", {})
                    for dir_name, info in info_cache.items():
                        name = info.get("name", dir_name)
                        detected.append({
                            "name": name, "directory": dir_name,
                            "path": str(win_path / dir_name),
                            "gaia_name": info.get("gaia_name", ""),
                            "email": info.get("user_name", ""),
                            "avatar_url": "",
                            "is_default": dir_name == "Default",
                        })
                    detected.sort(key=lambda p: p["name"].lower())
                except Exception:
                    pass

        # 2. Đọc profiles đã configured trong spider_hub
        cfg_raw      = _read_config_yaml()
        hive         = cfg_raw.get("spider_hub", {})
        configured   = hive.get("profiles", [])
        cfg_names    = {p.get("name", "").lower() for p in configured}

        # 3. Tìm profiles mới (chưa có trong config)
        new_profiles = [
            p for p in detected
            if p["name"].lower() not in cfg_names
        ]

        return JSONResponse({
            "detected":      detected,
            "configured":    configured,
            "new":           new_profiles,
            "total_detected": len(detected),
            "total_configured": len(configured),
            "total_new":     len(new_profiles),
            "chrome_dir":    str(chrome_data if local_state.exists() else "not_found"),
            "platform":      "mac" if local_state.exists() else "win",
        })

    @app.post("/api/v2/spiderhub/import-from-chrome")
    async def import_chrome_profiles(request: Request,
                                     body: dict = Body(default={}),
                                     _auth: None = Depends(_admin_dep)):
        """
        Import một hoặc nhiều Chrome profiles vào spider_hub config.

        Body:
          profiles: list[str]  — danh sách tên profiles cần import (từ /scan)
          active:   bool       — set active=true/false cho tất cả (default: true)
          platforms: list[str] — platforms mặc định (default: ["facebook"])
        """
        import yaml as _yaml
        profile_names = body.get("profiles", [])
        active        = bool(body.get("active", True))
        platforms     = body.get("platforms", ["facebook"])
        content_lang  = body.get("content_lang", "vi")

        if not profile_names:
            raise HTTPException(400, "profiles list is required")

        # Lấy detected profiles từ Chrome
        cfg_path = _components.get("config_path", "config.yaml")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = _yaml.safe_load(f) or {}
        except FileNotFoundError:
            raw = {}

        hive     = raw.setdefault("spider_hub", {})
        profs    = hive.setdefault("profiles", [])
        existing = {p.get("name", "").lower() for p in profs}

        # Đọc Chrome metadata
        import json as _json
        from pathlib import Path as _Path
        chrome_data = _Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        local_state = chrome_data / "Local State"
        chrome_meta = {}
        if local_state.exists():
            try:
                data = _json.loads(local_state.read_text(encoding="utf-8"))
                chrome_meta = data.get("profile", {}).get("info_cache", {})
            except Exception:
                pass

        # Build mapping name → directory
        name_to_dir = {}
        for dir_name, info in chrome_meta.items():
            pname = info.get("name", dir_name)
            name_to_dir[pname.lower()] = dir_name

        added   = []
        skipped = []
        for pname in profile_names:
            if pname.lower() in existing:
                skipped.append(pname)
                continue
            chrome_dir = name_to_dir.get(pname.lower(), "")
            new_profile = {
                "name":          pname,
                "description":   "",
                "active":        active,
                "chrome_dir":    chrome_dir,
                "platforms":     platforms,
                "facebook_url":  "https://www.facebook.com",
                "instagram_url": "https://www.instagram.com",
                "x_url":         "https://x.com",
                "content_lang":  content_lang,
                "post_delay_s":  0,
                "tags":          [],
            }
            profs.append(new_profile)
            added.append(pname)

        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                _yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)
        except Exception as exc:
            raise HTTPException(500, f"Luu config that bai: {exc}")

        _vlog("🕷️", f"SpiderHub auto-import: added {len(added)}, skipped {len(skipped)}")
        return JSONResponse({
            "ok":      True,
            "added":   added,
            "skipped": skipped,
            "total_now": len(profs),
        })

    @app.post("/api/v2/spiderhub/import-all-new")
    async def import_all_new_profiles(request: Request,
                                      body: dict = Body(default={}),
                                      _auth: None = Depends(_admin_dep)):
        """
        Shortcut: import TẤT CẢ profiles chưa có trong config (output của /scan new[]).
        Gọi /scan trước rồi gọi endpoint này với kết quả.
        """
        body["profiles"] = body.get("profiles", [])
        if not body["profiles"]:
            # Auto-fetch from scan
            import json as _json
            from pathlib import Path as _Path
            chrome_data = _Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
            local_state = chrome_data / "Local State"
            cfg_raw = _read_config_yaml()
            hive    = cfg_raw.get("spider_hub", {})
            cfg_names = {p.get("name","").lower() for p in hive.get("profiles",[])}
            if local_state.exists():
                try:
                    data      = _json.loads(local_state.read_text(encoding="utf-8"))
                    info_cache= data.get("profile",{}).get("info_cache",{})
                    body["profiles"] = [
                        info.get("name", dn)
                        for dn, info in info_cache.items()
                        if info.get("name", dn).lower() not in cfg_names
                    ]
                except Exception:
                    pass

        # Delegate to import-from-chrome logic
        req_fake = request
        return await import_chrome_profiles(req_fake, body=body, _auth=None)


    # ────────────────────────────────────────────────────────────
    # v9.28: Spider Hub Profiles API
    # ────────────────────────────────────────────────────────────

    @app.get("/api/v2/spiderhub/profiles")
    async def get_spiderhub_profiles(request: Request,
                                   _auth: None = Depends(_auth_dep)):
        """Trả về toàn bộ spider_hub config (profiles + hive_settings)."""
        try:
            cfg_raw = _read_config_yaml()
            hive = cfg_raw.get("spider_hub", {})
            hs   = hive.get("hive_settings", {})
            profs = hive.get("profiles", [])

            active_count = sum(1 for p in profs if p.get("active", True))
            return JSONResponse({
                "hive_settings": {
                    "max_parallel":         hs.get("max_parallel", 5),
                    "stagger_s":            hs.get("stagger_s", 30),
                    "default_platforms":    hs.get("default_platforms", ["facebook"]),
                    "default_content_lang": hs.get("default_content_lang", "vi"),
                },
                "profiles":      profs,
                "total":         len(profs),
                "active_count":  active_count,
            })
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/spiderhub/profiles")
    async def add_spiderhub_profile(request: Request,
                                  body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        """Thêm một profile mới vào spider_hub.profiles."""
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        try:
            import yaml
            cfg_path = _components.get("config_path", "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

            hive = raw.setdefault("spider_hub", {})
            profiles = hive.setdefault("profiles", [])

            # Reject duplicate
            if any(p.get("name", "").lower() == name.lower() for p in profiles):
                raise HTTPException(409, f"Profile '{name}' already exists")

            new_profile = {
                "name":          name,
                "description":   body.get("description", ""),
                "active":        bool(body.get("active", True)),
                "chrome_dir":    body.get("chrome_dir", ""),
                "platforms":     body.get("platforms", ["facebook"]),
                "facebook_url":  body.get("facebook_url", "https://www.facebook.com"),
                "instagram_url": body.get("instagram_url", "https://www.instagram.com"),
                "x_url":         body.get("x_url", "https://x.com"),
                "content_lang":  body.get("content_lang", "vi"),
                "post_delay_s":  float(body.get("post_delay_s", 0)),
                "tags":          body.get("tags", []),
            }
            profiles.append(new_profile)

            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)

            _vlog("🕷️", f"SpiderHub profile added: {name}")
            return JSONResponse({"ok": True, "profile": new_profile})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.put("/api/v2/spiderhub/profiles/{name}")
    async def update_spiderhub_profile(name: str,
                                     request: Request,
                                     body: dict = Body(default={}),
                                     _auth: None = Depends(_admin_dep)):
        """Cập nhật một profile (active, platforms, description, v.v.)."""
        try:
            import yaml
            cfg_path = _components.get("config_path", "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

            hive     = raw.setdefault("spider_hub", {})
            profiles = hive.setdefault("profiles", [])

            found = False
            for p in profiles:
                if p.get("name", "").lower() == name.lower():
                    # Update only provided fields
                    for key in ("description", "active", "chrome_dir", "platforms",
                                "facebook_url", "instagram_url", "x_url",
                                "content_lang", "post_delay_s", "tags"):
                        if key in body:
                            p[key] = body[key]
                    found = True
                    updated = p
                    break

            if not found:
                raise HTTPException(404, f"Profile '{name}' not found")

            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)

            _vlog("🕷️", f"SpiderHub profile updated: {name}")
            return JSONResponse({"ok": True, "profile": updated})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.delete("/api/v2/spiderhub/profiles/{name}")
    async def delete_spiderhub_profile(name: str,
                                     request: Request,
                                     _auth: None = Depends(_admin_dep)):
        """Xoá một profile khỏi spider_hub.profiles."""
        try:
            import yaml
            cfg_path = _components.get("config_path", "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

            hive     = raw.setdefault("spider_hub", {})
            profiles = hive.get("profiles", [])
            before   = len(profiles)
            hive["profiles"] = [
                p for p in profiles
                if p.get("name", "").lower() != name.lower()
            ]
            if len(hive["profiles"]) == before:
                raise HTTPException(404, f"Profile '{name}' not found")

            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)

            _vlog("🕷️", f"SpiderHub profile deleted: {name}")
            return JSONResponse({"ok": True})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])

    @app.post("/api/v2/spiderhub/settings")
    async def update_hive_settings(request: Request,
                                   body: dict = Body(default={}),
                                   _auth: None = Depends(_admin_dep)):
        """Cập nhật hive_settings (max_parallel, stagger_s, default_platforms)."""
        try:
            import yaml
            cfg_path = _components.get("config_path", "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

            hive = raw.setdefault("spider_hub", {})
            hs   = hive.setdefault("hive_settings", {})
            for key in ("max_parallel", "stagger_s", "default_platforms",
                        "default_content_lang"):
                if key in body:
                    hs[key] = body[key]

            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)

            _vlog("🕷️", f"SpiderHub settings updated: {hs}")
            return JSONResponse({"ok": True, "hive_settings": hs})
        except Exception as exc:
            raise HTTPException(500, str(exc)[:200])


    @app.post("/api/v2/system/restart")
    async def restart_phidipus(request: Request, _auth: None = Depends(_admin_dep)):
        """Restart Phidipus process. Only works from localhost."""
        if not _is_localhost(request):
            raise HTTPException(403, "Restart only from localhost")
        import subprocess, sys, os
        _vlog("🔄", "RESTART requested from Admin Panel")
        # Respond first, then restart
        async def _do_restart():
            import asyncio
            await asyncio.sleep(1)
            python = sys.executable
            script = os.path.abspath(sys.argv[0])
            args = sys.argv[1:]
            os.execv(python, [python, script] + args)
        import asyncio
        asyncio.get_event_loop().call_later(0.5, lambda: asyncio.ensure_future(_do_restart()))
        return JSONResponse({"ok": True, "message": "Restarting in 1s..."})

    # ════════════════════════════════════════════════════════════
    # v4.3: Memory Agent API (long-term memory, local SQLite)
    # ════════════════════════════════════════════════════════════
    def _mem_agent():
        from memory.memory_agent import get_memory_agent
        agent = get_memory_agent()
        if agent is None:
            raise HTTPException(503, "Memory Agent disabled (memory_agent.enabled: false)")
        return agent

    @app.get("/api/v2/memory/agent/stats")
    async def mem_agent_stats(request: Request, _auth: None = Depends(_auth_dep)):
        return JSONResponse(_mem_agent().stats())

    @app.get("/api/v2/memory/agent/search")
    async def mem_agent_search(request: Request, q: str = Query(default="", max_length=500),
                               k: int = Query(default=8, ge=1, le=50),
                               history: bool = Query(default=False),
                               _auth: None = Depends(_auth_dep)):
        hits = await _mem_agent().recall(q, k=k, include_history=history, touch=False)
        return JSONResponse({"query": q, "results": [h.to_dict() for h in hits]})

    @app.get("/api/v2/memory/agent/list")
    async def mem_agent_list(request: Request, kind: str = Query(default=""),
                             limit: int = Query(default=50, ge=1, le=500),
                             offset: int = Query(default=0, ge=0),
                             history: bool = Query(default=False),
                             _auth: None = Depends(_auth_dep)):
        agent = _mem_agent()
        items = await asyncio.to_thread(agent.store.list, kind or None, None, history, limit, offset)
        return JSONResponse({"items": [r.to_dict() for r in items],
                             "total": agent.store.count(kind or None, history)})

    @app.post("/api/v2/memory/agent/add")
    async def mem_agent_add(request: Request, body: dict = Body(default={}),
                            _auth: None = Depends(_admin_dep)):
        content = str(body.get("content", "")).strip()
        if not content:
            raise HTTPException(400, "content required")
        res = await _mem_agent().remember(content, kind=body.get("kind") or None,
                                          pinned=bool(body.get("pinned", False)), source="user")
        return JSONResponse(res)

    @app.post("/api/v2/memory/agent/forget")
    async def mem_agent_forget(request: Request, body: dict = Body(default={}),
                               _auth: None = Depends(_admin_dep)):
        target = str(body.get("target", "")).strip()
        if not target:
            raise HTTPException(400, "target (id or query) required")
        if target == "__ALL__":
            if body.get("confirm") is not True:
                raise HTTPException(400, "confirm: true required to delete every memory")
            return JSONResponse({"deleted_count": await _mem_agent().forget_all()})
        return JSONResponse(await _mem_agent().forget(target))

    @app.get("/api/v2/memory/agent/blocks")
    async def mem_agent_blocks(request: Request, _auth: None = Depends(_auth_dep)):
        return JSONResponse(_mem_agent().blocks())

    @app.post("/api/v2/memory/agent/blocks")
    async def mem_agent_set_block(request: Request, body: dict = Body(default={}),
                                  _auth: None = Depends(_admin_dep)):
        name = str(body.get("name", ""))
        if name not in ("user_profile", "preferences", "persona", "environment"):
            raise HTTPException(400, "name must be user_profile | preferences | persona | environment")
        return JSONResponse({"name": name, "content": _mem_agent().set_block(name, str(body.get("content", "")))})

    @app.post("/api/v2/memory/agent/consolidate")
    async def mem_agent_consolidate(request: Request, _auth: None = Depends(_admin_dep)):
        return JSONResponse(await _mem_agent().consolidate(force_blocks=True))

    @app.post("/api/v2/memory/agent/observe")
    async def mem_agent_observe(request: Request, body: dict = Body(default={}),
                                _auth: None = Depends(_admin_dep)):
        agent = _mem_agent()
        agent.set_observing(bool(body.get("on", True)))
        return JSONResponse({"observing": agent.observing})

    @app.get("/api/v2/memory/agent/events")
    async def mem_agent_events(request: Request, limit: int = Query(default=50, ge=1, le=500),
                               _auth: None = Depends(_auth_dep)):
        return JSONResponse({"events": _mem_agent().store.recent_events(limit)})

    @app.get("/api/v2/memory/agent/export")
    async def mem_agent_export(request: Request, _auth: None = Depends(_admin_dep)):
        data = await asyncio.to_thread(_mem_agent().store.export_all)
        return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=phidipus_memory.json"})

    # ════════════════════════════════════════════════════════════
    # v4.3: AI Lab read-only/model endpoints.  These were written after the
    # `return` of _get_fallback_html() (dead code, `app` undefined), while the
    # fallback page advertised them.  Duplicated pipeline routes were dropped
    # (the live ones are registered above).
    # ════════════════════════════════════════════════════════════
    _PROJECT_ROOT = Path(__file__).parent.parent

    @app.get("/api/v2/ailab/domains")
    async def ailab_domains(request: Request, _auth: None = Depends(_auth_dep)):
        """List domain configs from domains/*.yaml."""
        domains = []
        domains_dir = _PROJECT_ROOT / "domains"
        if domains_dir.exists():
            import yaml
            for f in sorted(domains_dir.glob("*.yaml")):
                try:
                    cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                    d = cfg.get("domain", {}) or {}
                    ds = cfg.get("dataset", {}) or {}
                    ins = cfg.get("insight_extraction", {}) or {}
                    domains.append({
                        "file": f.name,
                        "display_name": d.get("display_name", f.stem),
                        "name": d.get("name", f.stem),
                        "model_size": d.get("model_size", "1.7b"),
                        "language": d.get("language", "vi"),
                        "version": d.get("version", "1.0"),
                        "target_samples": ds.get("target_samples", 0),
                        "situation_count": len(ins.get("situation_types", []) or []),
                    })
                except Exception as exc:
                    domains.append({"file": f.name, "display_name": f.stem, "error": str(exc)[:60]})
        return JSONResponse({"domains": domains})

    def _ollama_base() -> str:
        try:
            from core.model_registry import ollama_url
            return ollama_url()
        except Exception:
            return "http://127.0.0.1:11434"

    @app.get("/api/v2/ailab/models")
    async def ailab_models(request: Request, _auth: None = Depends(_auth_dep)):
        """List Ollama models (HTTP API — no `ollama` CLI needed)."""
        def _fetch():
            with urllib.request.urlopen(f"{_ollama_base()}/api/tags", timeout=5) as r:
                return json.loads(r.read().decode())
        try:
            data = await asyncio.to_thread(_fetch)
        except Exception as exc:
            return JSONResponse({"models": [], "error": str(exc)[:80]})
        models = [{
            "name": m.get("name", ""),
            "id": (m.get("digest") or "")[:12],
            "size": f"{(m.get('size') or 0) / 1e9:.1f} GB",
            "modified": m.get("modified_at", ""),
        } for m in data.get("models", [])]
        return JSONResponse({"models": models})

    @app.post("/api/v2/ailab/models/delete")
    async def ailab_delete_model(request: Request, body: dict = Body(default={}),
                                 _auth: None = Depends(_admin_dep)):
        """Delete an Ollama model."""
        import re as _re
        name = str(body.get("name", "")).strip()
        if not name or not _re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", name):
            raise HTTPException(400, "valid model name required")
        def _delete():
            req = urllib.request.Request(
                f"{_ollama_base()}/api/delete", data=json.dumps({"model": name}).encode(),
                headers={"Content-Type": "application/json"}, method="DELETE")
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        try:
            status = await asyncio.to_thread(_delete)
            return JSONResponse({"ok": status == 200, "model": name})
        except Exception as exc:
            raise HTTPException(500, str(exc)[:100])

    @app.get("/api/v2/ailab/eval/results")
    async def ailab_eval_results(request: Request, _auth: None = Depends(_auth_dep)):
        """Latest evaluation report (searches data/, eval/, reports/ only)."""
        files: list[Path] = []
        for sub in ("data", "eval", "evaluation", "reports", "elite_pipeline"):
            base = _PROJECT_ROOT / sub
            if base.is_dir():
                files.extend(base.rglob("eval_report*.json"))
        if not files:
            return JSONResponse({"metrics": {}, "summary": "Chưa có evaluation report"})
        latest = max(files, key=lambda f: f.stat().st_mtime)
        try:
            return JSONResponse(json.loads(latest.read_text(encoding="utf-8")))
        except Exception:
            return JSONResponse({"metrics": {}, "summary": "Lỗi đọc report"})

    @app.get("/api/v2/ailab/brain/status")
    async def ailab_brain_status(request: Request, _auth: None = Depends(_auth_dep)):
        """Brain Pipeline stats of the running agent (no new model load)."""
        brain = getattr(_components.get("agent_loop"), "_brain", None)
        if brain is None:
            return JSONResponse({"model": "N/A", "loaded": False, "total": 0, "rule_hits": 0,
                                 "model_calls": 0, "overrides": 0, "rag_retries": 0})
        stats = dict(getattr(brain, "stats", {}) or {})
        stats.update(model=getattr(brain, "_model", "?"), loaded=True)
        return JSONResponse(stats)

    @app.get("/api/v2/ailab/logs")
    async def ailab_conv_logs(request: Request, _auth: None = Depends(_auth_dep)):
        """Recent entries from the Conversation Logger."""
        logs = []
        log_dir = _PROJECT_ROOT / "conversation_logs"
        if log_dir.exists():
            for f in sorted(log_dir.glob("*.jsonl"), reverse=True)[:5]:
                try:
                    for line in f.read_text(encoding="utf-8").strip().split("\n")[-20:]:
                        if line.strip():
                            logs.append(json.loads(line))
                except Exception:
                    pass
        return JSONResponse({"logs": logs[-50:]})

    # ════════════════════════════════════════════════════════════
    # SPA catch-all — MUST be LAST route (after all API routes)
    # ════════════════════════════════════════════════════════════
    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        # Same priority as serve_spa: dist/ → admin_panel.html → static/
        dist_idx = Path(__file__).parent / "dist" / "index.html"
        if dist_idx.exists():
            html = dist_idx.read_text(encoding="utf-8")
            return HTMLResponse(_inject_token(html))
        panel = Path(__file__).parent / "admin_panel.html"
        if panel.exists():
            html = panel.read_text(encoding="utf-8")
            return HTMLResponse(_inject_token(html))
        index = Path(__file__).parent / "static" / "index.html"
        if index.exists():
            html = index.read_text(encoding="utf-8")
            return HTMLResponse(_inject_token(html))
        return HTMLResponse(_get_fallback_html())

    return app


# ══════════════════════════════════════════════════════════════════
# Background helpers
# ══════════════════════════════════════════════════════════════════

async def _run_task_bg(goal: str, ws: WebSocket | None) -> None:
    """Run task in background, broadcast progress via WS.
    [C-05 FIX] Semaphore limits to 3 concurrent tasks.
    [SEC-R11 FIX] Hard queue cap: reject when pending_task_count >= _MAX_PENDING_TASKS.
    """
    global _pending_task_count

    # SEC-R11: Check hard queue cap BEFORE acquiring semaphore
    if _pending_task_count >= _MAX_PENDING_TASKS:
        err_msg = json.dumps({
            "event": "error",
            "data": {"message": f"Queue đầy ({_MAX_PENDING_TASKS} tasks). Thử lại sau."},
        })
        if ws:
            try:
                await ws.send_text(err_msg)
            except Exception:
                pass
        _vlog("🛡️", f"[SEC-R11] Task rejected — queue cap reached ({_pending_task_count}/{_MAX_PENDING_TASKS})")
        return

    _pending_task_count += 1
    # Try acquire semaphore without blocking indefinitely
    try:
        acquired = await asyncio.wait_for(_task_semaphore.acquire(), timeout=5.0)
    except asyncio.TimeoutError:
        _pending_task_count -= 1
        err_msg = json.dumps({"event": "error", "data": {"message": "Server quá tải, thử lại sau"}})
        if ws:
            try:
                await ws.send_text(err_msg)
            except Exception:
                pass
        return
    try:
        await _run_task_bg_inner(goal, ws)
    finally:
        _task_semaphore.release()
        _pending_task_count = max(0, _pending_task_count - 1)


async def _run_task_bg_inner(goal: str, ws: WebSocket | None) -> None:
    """Inner task runner (called under semaphore)."""
    agent = _components.get("agent_loop")
    if not agent:
        return

    task_id = str(uuid.uuid4())[:8]
    _vlog("🎯", f"SpiderHub God Button: {goal[:60]}")

    await _ws_manager.broadcast("task_start", {
        "task_id": task_id,
        "goal": goal,
    })

    # Record in Shadow Mode
    if _shadow.enabled:
        _shadow.start_segment(task_id, goal)

    try:
        result = await agent.run_task(goal)
        success = result.success

        if _shadow.enabled:
            _shadow.close_segment(success)

        await _ws_manager.broadcast("task_done", {
            "task_id": task_id,
            "goal": goal,
            "success": success,
            "steps": result.steps_taken,
            "error": result.error[:100] if result.error else "",
        })
    except Exception as exc:
        if _shadow.enabled:
            _shadow.close_segment(False)
        await _ws_manager.broadcast("task_error", {
            "task_id": task_id,
            "error": str(exc)[:200],
        })


# ── [M-09 FIX] Cached realtime stats (avoid blocking event loop per-tick) ───
_stats_cache: dict = {}
_stats_cache_ts: float = 0.0
_STATS_CACHE_TTL = 5.0  # seconds

def _get_cached_stats() -> dict:
    global _stats_cache, _stats_cache_ts
    now = time.time()
    if now - _stats_cache_ts > _STATS_CACHE_TTL:
        _stats_cache = _collect_realtime_stats()
        _stats_cache_ts = now
    return _stats_cache


def _collect_realtime_stats() -> dict:
    """Collect system stats for heartbeat/status."""
    agent = _components.get("agent_loop")
    stats: dict[str, Any] = {
        "ts": time.time(),
        "server": "SpiderHub v2",
        "ws_clients": _ws_manager.count,
        "shadow": {
            "enabled": _shadow.enabled,
            "privacy": _shadow.privacy_level,
            "segments": len(_shadow.segments),
        },
    }

    if agent:
        # Resource guard
        rg = getattr(agent, "_resource_guard", None)
        if rg:
            snap = rg.current
            stats["resource"] = {
                "ram_pct": round(snap.ram_used_percent, 1),
                "cpu_pct": round(snap.cpu_percent, 1),
                "disk_free_gb": round(snap.disk_free_gb, 1),
                "status": rg.stats().get("status", "unknown"),
            }

        # Skill forge
        forge = getattr(agent, "_forge", None)
        if forge:
            stats["forge"] = {
                "enabled": forge.enabled,
                "cached_skills": len(getattr(forge, "_cache", {})),
            }

        # Failure memory
        fm = getattr(agent, "_failure_memory", None)
        if fm:
            fm_stats = fm.stats()
            stats["failure_memory"] = {
                "total_records": fm_stats.get("total_records", 0),
            }

        # Workflow lib
        wf = getattr(agent, "_workflow_lib", None)
        if wf:
            wf_stats = wf.stats()
            stats["workflow"] = {
                "plans": wf_stats.get("plan_cache_entries", 0),
                "templates": wf_stats.get("workflow_templates", 0),
                "promoted": wf_stats.get("plan_cache_promoted", 0),
            }

    # ── A1 FIX: expose gemini_key_set + workflows_count for First-Run Wizard ──
    # Wizard in dist/index.html reads /api/v2/system/stats and checks
    # r.data?.gemini_key_set  — if missing → wizard always shows even with key
    cfg_raw = _components.get("config_raw", {})
    if cfg_raw:
        _gkey = cfg_raw.get("skill_forge", {}).get("gemini_api_key", "")
        stats["gemini_key_set"] = bool(_gkey and len(str(_gkey).strip()) > 8)
        stats["gemini_configured"] = stats["gemini_key_set"]  # alias
    else:
        stats["gemini_key_set"] = False
        stats["gemini_configured"] = False

    # workflows_count — direct API call to workflow-teacher list is also used by wizard,
    # but provide a fast path via stats too
    _wf_list = []
    try:
        from pathlib import Path as _Path
        import json as _json
        _wf_dir = _Path("data/workflows")
        if _wf_dir.exists():
            _wf_list = [f for f in _wf_dir.glob("*.json") if f.is_file()]
    except Exception:
        pass
    stats["workflows_count"] = len(_wf_list)

    return stats


# ══════════════════════════════════════════════════════════════════
# Fallback HTML (when Vue build not present)
# ══════════════════════════════════════════════════════════════════

def _get_fallback_html() -> str:
    """Minimal dark fallback page shown before Vue build."""
    return """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Phidipus Agents — SpiderHub</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0a0f;color:#f0f0f0;font-family:system-ui,sans-serif;
       display:flex;flex-direction:column;align-items:center;justify-content:center;
       min-height:100vh;gap:24px}
  .bee{font-size:72px;animation:float 3s ease-in-out infinite}
  @keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-16px)}}
  h1{font-size:28px;font-weight:700;color:#f5c518}
  p{color:#888;font-size:14px;text-align:center;max-width:480px;line-height:1.6}
  .badge{background:rgba(245,197,24,0.12);border:1px solid rgba(245,197,24,0.3);
         color:#f5c518;padding:6px 16px;border-radius:100px;font-size:13px}
  .api{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);
       padding:16px 24px;border-radius:12px;font-size:13px;color:#888;
       font-family:monospace;line-height:1.8}
  a{color:#00d4aa;text-decoration:none}
</style>
</head>
<body>
  <div class="bee">🕷️</div>
  <div class="badge">SpiderHub Admin Panel v9.20</div>
  <h1>Phidipus Agents đang chạy</h1>
  <p>Vue SPA chưa build. Để xem full UI:<br>
  <code>cd admin/ui && npm install && npm run build</code></p>
  <div class="api">
    API v2 available:<br>
    GET  <a href="/api/v2/system/stats">/api/v2/system/stats</a><br>
    GET  <a href="/api/v2/chrome/profiles/status">/api/v2/chrome/profiles/status</a><br>
    GET  <a href="/api/v2/shadow/state">/api/v2/shadow/state</a><br>
    GET  <a href="/api/v2/ailab/domains">/api/v2/ailab/domains</a><br>
    GET  <a href="/api/v2/ailab/models">/api/v2/ailab/models</a><br>
    GET  <a href="/api/v2/ailab/pipeline/status">/api/v2/ailab/pipeline/status</a><br>
    GET  <a href="/api/v2/ailab/brain/status">/api/v2/ailab/brain/status</a><br>
    WS   ws://127.0.0.1:8912/api/v2/ws
  </div>
</body>
</html>"""
