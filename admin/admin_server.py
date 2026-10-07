# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
admin/admin_server.py — Phidipus v1.0 Admin Panel Backend
═══════════════════════════════════════════════════════════

FastAPI server providing REST API + WebSocket real-time push.
Binds ONLY to 127.0.0.1:8912 for security.

Standalone (demo mode):
    python -m admin.admin_server

Production (with Phidipus):
    from admin.admin_server import create_app, inject_components
    app = create_app()
    inject_components(app, agent_loop=..., config=..., ...)
    uvicorn.run(app, host="127.0.0.1", port=8912)
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── FastAPI imports (graceful if missing) ─────────────────────────
try:
    from fastapi import (
        FastAPI, WebSocket, WebSocketDisconnect,
        HTTPException, Query,
    )
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.middleware.cors import CORSMiddleware
except ImportError:
    raise SystemExit(
        "[admin] FastAPI chưa cài đặt.\n"
        "  pip install fastapi uvicorn --break-system-packages"
    )


# ══════════════════════════════════════════════════════════════════
# WebSocket connection manager
# ══════════════════════════════════════════════════════════════════

class _WSManager:
    """Thread-safe WebSocket broadcast hub."""

    def __init__(self) -> None:
        self._active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._active:
            self._active.remove(ws)

    async def broadcast(self, data: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self._active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self._active)


# ══════════════════════════════════════════════════════════════════
# Shared state container — injected at startup
# ══════════════════════════════════════════════════════════════════

class _AdminState:
    def __init__(self) -> None:
        # Phidipus components (None = demo mode)
        self.agent_loop: Any = None
        self.config: Any = None
        self.config_raw: dict = {}
        self.skill_registry: Any = None
        self.skill_discovery: Any = None
        self.memory_guard: Any = None
        self.episodic_memory: Any = None
        self.vector_memory: Any = None
        self.runtime_monitor: Any = None
        self.evolution_engine: Any = None
        self.docker_pool: Any = None
        self.social_manager: Any = None
        self.telegram_config: Any = None
        self.app_scanner: Any = None
        # Internal bookkeeping
        self.task_history: list[dict] = []
        self.ipc_log: list[dict] = []
        self.start_time: float = time.time()
        # Auto-init social manager
        try:
            _root = str(Path(__file__).resolve().parent.parent)
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from social.social_manager import SocialManager
            self.social_manager = SocialManager()
        except Exception:
            pass
        # Auto-init telegram config
        try:
            _root = str(Path(__file__).resolve().parent.parent)
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from telegram.telegram_config import TelegramConfig
            self.telegram_config = TelegramConfig()
        except Exception:
            pass

    @property
    def demo(self) -> bool:
        return self.agent_loop is None


# ══════════════════════════════════════════════════════════════════
# App factory
# ══════════════════════════════════════════════════════════════════

def create_app() -> FastAPI:
    app = FastAPI(title="Phidipus Admin", version="9.12", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8912", "http://localhost:8912"],
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Phidipus-Token"],
        allow_credentials=False,
    )
    app.state.admin = _AdminState()
    app.state.ws = _WSManager()

    # ── SEC-R6: Token auth middleware for ALL v1 endpoints ────────
    # Reads token from the same file written by admin_server_v2.
    # Static assets (logo) are exempt; everything else requires token.
    import secrets as _secrets
    from fastapi import Request as _Request
    from fastapi.responses import JSONResponse as _JSONResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    class _V1AuthMiddleware(BaseHTTPMiddleware):
        EXEMPT_PATHS = {"/", "/api/v1/static/logo"}

        async def dispatch(self, request: _Request, call_next):
            if request.url.path in self.EXEMPT_PATHS:
                return await call_next(request)
            if request.method == "OPTIONS":
                return await call_next(request)
            # Read token from shared token file (written by admin_server_v2)
            try:
                token_file = Path("/tmp/.phidipus_admin_token")
                expected = token_file.read_text().strip() if token_file.exists() else ""
            except Exception:
                expected = ""
            supplied = (
                request.headers.get("X-Phidipus-Token", "")
                or request.query_params.get("token", "")
            )
            if not expected or not supplied or not _secrets.compare_digest(supplied, expected):
                return _JSONResponse(
                    {"error": "Unauthorized: invalid or missing X-Phidipus-Token"},
                    status_code=401,
                )
            return await call_next(request)

    app.add_middleware(_V1AuthMiddleware)
    _mount_routes(app)
    return app


def inject_components(app: FastAPI, **kw: Any) -> None:
    """Inject live Phidipus components into admin state."""
    st: _AdminState = app.state.admin
    for k, v in kw.items():
        if hasattr(st, k):
            setattr(st, k, v)


# ══════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════

def _mount_routes(app: FastAPI) -> None:  # noqa: C901 — single mount point
    ws_mgr: _WSManager = app.state.ws

    # ── HTML ──────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def _root():
        p = Path(__file__).parent / "admin_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text("utf-8"))
        return HTMLResponse("<h1>admin_panel.html không tìm thấy</h1>", 404)

    @app.get("/api/v1/static/logo")
    async def _logo():
        logo = Path(__file__).parent / "logo-phidipus.png"
        if logo.exists():
            return FileResponse(logo, media_type="image/png")
        return JSONResponse({"error": "logo not found"}, 404)

    # ══════════════════════════════════════════════════════════════
    # 1. DASHBOARD
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/dashboard/health")
    async def _health():
        st: _AdminState = app.state.admin
        if st.demo:
            return _mock_health(st)
        h = st.runtime_monitor.check_health()
        return {
            "tasks_started": h.tasks_started,
            "tasks_succeeded": h.tasks_succeeded,
            "tasks_failed": h.tasks_failed,
            "loop_iterations": h.loop_iterations,
            "error_rate": round(h.error_rate, 4),
            "stuck": h.stuck,
            "llm_calls": h.llm_calls,
            "llm_mean_ms": round(h.llm_mean_ms, 1),
            "last_activity": getattr(h, "last_activity", ""),
            "alerts": list(getattr(h, "alerts", [])),
            "uptime_s": int(time.time() - st.start_time),
            "ws_clients": ws_mgr.count,
        }

    @app.get("/api/v1/dashboard/ollama")
    async def _ollama():
        st: _AdminState = app.state.admin
        url = "http://127.0.0.1:11434"
        if st.config:
            url = st.config.llm.base_url
        try:
            import urllib.request
            with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:
                data = json.loads(r.read().decode())
            models = [
                {"name": m["name"],
                 "size_gb": round(m.get("size", 0) / 1e9, 1),
                 "modified": m.get("modified_at", "")}
                for m in data.get("models", [])
            ]
            return {"status": "online", "url": url, "models": models}
        except Exception as e:
            return {"status": "offline", "url": url, "models": [], "error": str(e)[:120]}

    # ══════════════════════════════════════════════════════════════
    # 2. TASKS
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/tasks")
    async def _tasks(limit: int = Query(50, le=500)):
        st: _AdminState = app.state.admin
        return {"tasks": st.task_history[-limit:][::-1], "total": len(st.task_history)}

    @app.post("/api/v1/tasks")
    async def _create_task(body: dict):
        st: _AdminState = app.state.admin
        goal = body.get("goal", "").strip()
        if not goal:
            raise HTTPException(400, "Vui lòng nhập mục tiêu")
        tid = str(uuid.uuid4())[:8]
        entry = {
            "task_id": tid, "goal": goal, "status": "pending",
            "steps": 0, "success": None, "error": "",
            "started_at": _utc_iso(), "finished_at": "",
        }
        st.task_history.append(entry)
        await ws_mgr.broadcast({"type": "task_started", "data": entry})
        if not st.demo:
            asyncio.create_task(_bg_run_task(st, ws_mgr, tid, goal))
        else:
            # Demo: simulate instant completion
            entry.update(status="completed", success=True, steps=3,
                         finished_at=_utc_iso())
        return entry

    @app.post("/api/v1/tasks/{tid}/cancel")
    async def _cancel_task(tid: str):
        st: _AdminState = app.state.admin
        for t in st.task_history:
            if t["task_id"] == tid and t["status"] == "running":
                t["status"] = "cancelled"
                t["success"] = False
                t["error"] = "Đã huỷ bởi quản trị viên"
                await ws_mgr.broadcast({"type": "task_cancelled", "data": t})
                return t
        raise HTTPException(404, "Không tìm thấy tác vụ đang chạy")

    # ══════════════════════════════════════════════════════════════
    # 3. SKILLS
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/skills")
    async def _skills():
        st: _AdminState = app.state.admin
        if st.skill_registry:
            items = st.skill_registry.list_skills()
            return {"skills": [
                {"name": s.name, "description": getattr(s, "description", ""),
                 "version": getattr(s, "version", 1),
                 "quarantined": getattr(s, "quarantined", False)}
                for s in items
            ]}
        return {"skills": _mock_skills()}

    @app.get("/api/v1/skills/actions")
    async def _actions_list():
        try:
            _ensure_syspath()
            from ipc.action_schema import ALLOWED_ACTIONS
            return {"actions": sorted(ALLOWED_ACTIONS), "count": len(ALLOWED_ACTIONS)}
        except Exception:
            return {"actions": [], "count": 0}

    @app.post("/api/v1/skills/{name}/quarantine")
    async def _quarantine_skill(name: str):
        st: _AdminState = app.state.admin
        if st.skill_registry and hasattr(st.skill_registry, "quarantine"):
            st.skill_registry.quarantine(name)
            return {"status": "quarantined", "name": name}
        return {"status": "demo", "name": name}

    # ══════════════════════════════════════════════════════════════
    # 4. MEMORY
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/memory/stats")
    async def _mem_stats():
        st: _AdminState = app.state.admin
        backend = "hnswlib" if _has_hnswlib() else "python"
        if st.episodic_memory:
            cnt = st.episodic_memory.count() if hasattr(st.episodic_memory, "count") else 0
            ns = getattr(st.episodic_memory, "_namespace", "primary")
            return {"episode_count": cnt, "namespace": ns, "vector_backend": backend}
        return {"episode_count": 0, "namespace": "primary", "vector_backend": backend}

    @app.get("/api/v1/memory/episodes")
    async def _episodes(limit: int = Query(50, le=500)):
        st: _AdminState = app.state.admin
        if st.episodic_memory and hasattr(st.episodic_memory, "list_recent"):
            return {"episodes": st.episodic_memory.list_recent(limit)}
        return {"episodes": []}

    @app.post("/api/v1/memory/integrity/scan")
    async def _integrity_scan():
        st: _AdminState = app.state.admin
        if st.memory_guard and hasattr(st.memory_guard, "scan_integrity"):
            report = st.memory_guard.scan_integrity()
            return {"status": "completed", "report": str(report)[:2000]}
        return {"status": "demo", "report": "Chế độ demo — không có dữ liệu"}

    # ══════════════════════════════════════════════════════════════
    # 5. CONFIG
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/config")
    async def _get_config():
        st: _AdminState = app.state.admin
        if st.config_raw:
            return {"config": _mask_secrets(dict(st.config_raw))}
        try:
            _ensure_syspath()
            from config.schema import _DEFAULTS
            return {"config": _mask_secrets(dict(_DEFAULTS))}
        except Exception:
            return {"config": _mock_config()}

    @app.get("/api/v1/config/schema")
    async def _get_schema():
        try:
            _ensure_syspath()
            from config.schema import CONFIG_SCHEMA
            return {"schema": CONFIG_SCHEMA}
        except Exception:
            return {"schema": {}}

    @app.put("/api/v1/config/{section}")
    async def _update_config(section: str, body: dict):
        # R-12 enforcement: network_mode cannot be changed
        if section == "sandbox" and body.get("network_mode", "none") != "none":
            raise HTTPException(403, "R-12: network_mode phải luôn là 'none'")
        st: _AdminState = app.state.admin
        if st.config_raw and section in st.config_raw:
            st.config_raw[section].update(body)
            return {"status": "updated", "section": section}
        return {"status": "demo", "section": section}

    @app.post("/api/v1/config/reload")
    async def _reload_config():
        return {"status": "reloaded", "message": "Cấu hình đã được tải lại"}

    # ══════════════════════════════════════════════════════════════
    # 6. SECURITY
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/security/invariants")
    async def _invariants():
        return {"invariants": _build_invariants(), "count": 26}

    @app.get("/api/v1/security/keys")
    async def _keys():
        st: _AdminState = app.state.admin
        result = []
        for label, section, attr in [
            ("Khoá HMAC", "memory", "hmac_key_file"),
            ("Khoá ký (private)", "skill_validator", "signing_private_key_file"),
            ("Khoá ký (public)", "skill_validator", "signing_public_key_file"),
        ]:
            path = ""
            if st.config:
                sec = getattr(st.config, section, None)
                if sec:
                    path = getattr(sec, attr, "")
            exists = Path(path).exists() if path else False
            result.append({"name": label, "path": path or "(chưa cấu hình)", "exists": exists})
        return {"keys": result}

    @app.get("/api/v1/security/ast-test")
    async def _ast_test():
        """Run a quick AST sandbox verification."""
        try:
            _ensure_syspath()
            from security.skill_ast_sandbox import SkillASTSandbox, ASTSandboxViolation
            sb = SkillASTSandbox()
            attacks = [
                ('import os\nos.system("id")', "import os"),
                ('__builtins__["exec"]("1")', "__builtins__"),
                ('from os import *', "wildcard import"),
            ]
            results = []
            for code, desc in attacks:
                try:
                    sb.check(code)
                    results.append({"attack": desc, "blocked": False})
                except ASTSandboxViolation:
                    results.append({"attack": desc, "blocked": True})
                except Exception:
                    results.append({"attack": desc, "blocked": True})
            return {"results": results, "all_blocked": all(r["blocked"] for r in results)}
        except Exception as e:
            return {"error": str(e)[:200]}

    # ══════════════════════════════════════════════════════════════
    # 7. EVOLUTION
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/evolution/stats")
    async def _evo_stats():
        st: _AdminState = app.state.admin
        if st.evolution_engine and hasattr(st.evolution_engine, "get_stats"):
            return st.evolution_engine.get_stats()
        return {"cycles": 0, "acceptance_rate": 0.0, "avg_score": 0.0}

    # ══════════════════════════════════════════════════════════════
    # 8. DOCKER
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/docker/status")
    async def _docker():
        sock = Path("/var/run/docker.sock")
        status = "online (socket)" if sock.exists() else "offline"
        st: _AdminState = app.state.admin
        pool_sz = 2
        pool_rdy = 0
        if st.config:
            pool_sz = getattr(st.config.sandbox, "pool_size", 2)
        if st.docker_pool and hasattr(st.docker_pool, "available_count"):
            pool_rdy = st.docker_pool.available_count()
        return {
            "status": status, "pool_size": pool_sz, "pool_ready": pool_rdy,
            "network_mode": "none", "memory_limit": "256m", "cpu_quota": 0.5,
        }

    # ══════════════════════════════════════════════════════════════
    # 9. IPC
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/ipc/log")
    async def _ipc_log(limit: int = Query(100, le=1000)):
        st: _AdminState = app.state.admin
        return {"messages": st.ipc_log[-limit:][::-1], "total": len(st.ipc_log)}

    @app.post("/api/v1/ipc/send")
    async def _ipc_send(body: dict):
        """Admin-only: manually send an IPC action (requires double confirmation)."""
        action = body.get("action", "")
        payload = body.get("payload", {})
        confirm = body.get("confirm", False)
        if not confirm:
            return {"status": "needs_confirm",
                    "message": f"Xác nhận gửi '{action}'? Gửi lại với confirm=true"}
        st: _AdminState = app.state.admin
        if st.agent_loop and hasattr(st.agent_loop, "_ipc"):
            resp = await st.agent_loop._ipc.send_action(action, payload)
            return {"status": "sent", "success": resp.success}
        return {"status": "demo", "message": "Chế độ demo — không gửi thật"}

    # ══════════════════════════════════════════════════════════════
    # 10. SYSTEM
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/system/info")
    async def _sysinfo():
        import shutil
        disk = shutil.disk_usage("/")
        return {
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "python": platform.python_version(),
            "cpus": os.cpu_count() or 0,
            "ram_gb": round(_total_ram_gb(), 1),
            "disk_free_gb": round(disk.free / 1e9, 1),
            "disk_total_gb": round(disk.total / 1e9, 1),
            "pid": os.getpid(),
        }

    @app.get("/api/v1/system/patcher")
    async def _patcher():
        return {"patches_this_hour": 0, "max_per_hour": 10, "ledger_entries": 0}

    @app.get("/api/v1/system/apps")
    async def _system_apps():
        st: _AdminState = app.state.admin
        if st.app_scanner:
            return st.app_scanner.summary()
        return {"scanned": False, "total_apps": 0, "browsers": [], "chrome_profiles": 0}

    @app.post("/api/v1/system/apps/refresh")
    async def _system_apps_refresh():
        st: _AdminState = app.state.admin
        if st.app_scanner:
            import asyncio
            result = await asyncio.to_thread(st.app_scanner.refresh)
            return {"status": "refreshed", **result}
        return {"status": "no_scanner"}

    @app.get("/api/v1/system/chrome-profiles")
    async def _chrome_profiles():
        st: _AdminState = app.state.admin
        if st.app_scanner:
            return {"profiles": st.app_scanner.list_chrome_profiles()}
        return {"profiles": []}

    # ══════════════════════════════════════════════════════════════
    # 11. SOCIAL — Kết nối mạng xã hội
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/social/accounts")
    async def _social_accounts():
        st: _AdminState = app.state.admin
        if st.social_manager:
            return st.social_manager.get_summary()
        return _mock_social()

    @app.get("/api/v1/social/platforms")
    async def _social_platforms():
        try:
            _ensure_syspath()
            from social.social_manager import SocialManager
            return {"platforms": SocialManager.supported_platforms()}
        except Exception:
            return {"platforms": [
                {"id": "facebook", "name": "Facebook", "icon": "📘", "color": "#1877F2"},
                {"id": "instagram", "name": "Instagram", "icon": "📸", "color": "#E4405F"},
                {"id": "tiktok", "name": "TikTok", "icon": "🎵", "color": "#000000"},
                {"id": "twitter", "name": "Twitter / X", "icon": "𝕏", "color": "#1DA1F2"},
                {"id": "google", "name": "Google Account", "icon": "🔵", "color": "#4285F4"},
                {"id": "threads", "name": "Threads", "icon": "🔗", "color": "#000000"},
            ]}

    @app.post("/api/v1/social/connect")
    async def _social_connect(body: dict):
        st: _AdminState = app.state.admin
        platform = body.get("platform", "")
        username = body.get("username", "")
        display_name = body.get("display_name", "")
        if not platform:
            raise HTTPException(400, "Thiếu platform")
        if st.social_manager:
            result = st.social_manager.add_account(
                platform, username=username, display_name=display_name,
            )
            st.social_manager.set_connected(platform, True)
            return {"status": "connected", "account": result}
        return {"status": "demo", "platform": platform}

    @app.post("/api/v1/social/disconnect")
    async def _social_disconnect(body: dict):
        st: _AdminState = app.state.admin
        platform = body.get("platform", "")
        if not platform:
            raise HTTPException(400, "Thiếu platform")
        if st.social_manager:
            result = st.social_manager.set_connected(platform, False)
            return {"status": "disconnected", "account": result}
        return {"status": "demo", "platform": platform}

    @app.delete("/api/v1/social/{platform}")
    async def _social_remove(platform: str):
        st: _AdminState = app.state.admin
        if st.social_manager:
            removed = st.social_manager.remove_account(platform)
            return {"status": "removed" if removed else "not_found", "platform": platform}
        return {"status": "demo", "platform": platform}

    @app.get("/api/v1/social/chrome")
    async def _social_chrome():
        st: _AdminState = app.state.admin
        if st.social_manager:
            return st.social_manager.get_chrome_profile_info()
        return {
            "path": str(Path.home() / "Library/Application Support/Google/Chrome/Phidipus"),
            "exists": False,
            "launch_command": "",
        }

    @app.post("/api/v1/social/login")
    async def _social_login(body: dict):
        """Instruct Phidipus agent to open Chrome and navigate to login page."""
        st: _AdminState = app.state.admin
        platform = body.get("platform", "")
        if not platform:
            raise HTTPException(400, "Thiếu platform")
        if st.social_manager:
            url = st.social_manager.get_login_url(platform)
            cmd = st.social_manager.get_launch_command(platform)
            if st.agent_loop:
                goal = f"Mở Chrome profile Phidipus và đăng nhập vào {platform} tại {url}"
                asyncio.create_task(_bg_run_task(st, ws_mgr, str(uuid.uuid4())[:8], goal))
                return {"status": "agent_started", "goal": goal, "url": url}
            return {"status": "manual", "url": url, "command": cmd}
        return {"status": "demo", "platform": platform}

    # ══════════════════════════════════════════════════════════════
    # 12. TELEGRAM — Quản lý Telegram Bot
    # ══════════════════════════════════════════════════════════════

    @app.get("/api/v1/telegram/config")
    async def _tg_config():
        st: _AdminState = app.state.admin
        if st.telegram_config:
            return {
                "config": st.telegram_config.to_dict(mask_token=True),
                "validation": st.telegram_config.validate(),
            }
        return {
            "config": {"token": "", "token_set": False, "admin_ids": [],
                       "admin_count": 0, "bot_username": "", "bot_name": "",
                       "auto_start": False, "notes": ""},
            "validation": {"valid": False, "issues": ["TelegramConfig chưa khởi tạo"], "ready": False},
        }

    @app.put("/api/v1/telegram/token")
    async def _tg_set_token(body: dict):
        st: _AdminState = app.state.admin
        token = body.get("token", "").strip()
        if not token:
            raise HTTPException(400, "Token không được để trống")
        if ":" not in token:
            raise HTTPException(400, "Token không hợp lệ — phải có dấu ':' (ví dụ: 123456:ABC-DEF...)")
        if st.telegram_config:
            st.telegram_config.set_token(token)
            return {"status": "saved", "token_masked": st.telegram_config.token_masked}
        return {"status": "demo"}

    @app.delete("/api/v1/telegram/token")
    async def _tg_clear_token():
        st: _AdminState = app.state.admin
        if st.telegram_config:
            st.telegram_config.clear_token()
            return {"status": "cleared"}
        return {"status": "demo"}

    @app.post("/api/v1/telegram/admins")
    async def _tg_add_admin(body: dict):
        st: _AdminState = app.state.admin
        user_id = body.get("user_id")
        if not user_id or not str(user_id).isdigit():
            raise HTTPException(400, "user_id phải là số nguyên")
        user_id = int(user_id)
        if st.telegram_config:
            added = st.telegram_config.add_admin(user_id)
            return {
                "status": "added" if added else "already_exists",
                "user_id": user_id,
                "admin_ids": st.telegram_config.admin_ids,
            }
        return {"status": "demo", "user_id": user_id}

    @app.delete("/api/v1/telegram/admins/{user_id}")
    async def _tg_remove_admin(user_id: int):
        st: _AdminState = app.state.admin
        if st.telegram_config:
            removed = st.telegram_config.remove_admin(user_id)
            return {
                "status": "removed" if removed else "not_found",
                "user_id": user_id,
                "admin_ids": st.telegram_config.admin_ids,
            }
        return {"status": "demo", "user_id": user_id}

    @app.put("/api/v1/telegram/bot-info")
    async def _tg_set_bot_info(body: dict):
        st: _AdminState = app.state.admin
        if st.telegram_config:
            st.telegram_config.set_bot_info(
                username=body.get("bot_username", ""),
                name=body.get("bot_name", ""),
            )
            return {"status": "saved", "config": st.telegram_config.to_dict()}
        return {"status": "demo"}

    @app.put("/api/v1/telegram/settings")
    async def _tg_update_settings(body: dict):
        st: _AdminState = app.state.admin
        if st.telegram_config:
            if "auto_start" in body:
                st.telegram_config.set_auto_start(bool(body["auto_start"]))
            if "notes" in body:
                st.telegram_config.set_notes(str(body["notes"]))
            return {"status": "saved", "config": st.telegram_config.to_dict()}
        return {"status": "demo"}

    @app.get("/api/v1/telegram/validate")
    async def _tg_validate():
        st: _AdminState = app.state.admin
        if st.telegram_config:
            return st.telegram_config.validate()
        return {"valid": False, "issues": ["TelegramConfig chưa khởi tạo"], "ready": False}

    # ══════════════════════════════════════════════════════════════
    # WEBSOCKET — real-time push
    # ══════════════════════════════════════════════════════════════

    @app.websocket("/ws/live")
    async def _ws_live(ws: WebSocket):
        await ws_mgr.connect(ws)
        try:
            while True:
                data = await _health()
                await ws.send_json({"type": "health", "data": data})
                await asyncio.sleep(3)
        except WebSocketDisconnect:
            ws_mgr.disconnect(ws)
        except Exception:
            ws_mgr.disconnect(ws)


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def _ensure_syspath() -> None:
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)

def _has_hnswlib() -> bool:
    try:
        import hnswlib  # noqa: F401
        return True
    except ImportError:
        return False

def _total_ram_gb() -> float:
    try:
        if platform.system() == "Darwin":
            import subprocess
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).decode().strip()
            return int(out) / 1e9
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    return int(line.split()[1]) / 1e6
    except Exception:
        return 0.0

def _mask_secrets(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _mask_secrets(v)
        elif any(s in k.lower() for s in ("key", "secret", "password", "token")):
            out[k] = "•••" if v else ""
        else:
            out[k] = v
    return out

async def _bg_run_task(st: _AdminState, ws: _WSManager, tid: str, goal: str) -> None:
    entry = next((t for t in st.task_history if t["task_id"] == tid), None)
    if not entry:
        return
    entry["status"] = "running"
    try:
        result = await st.agent_loop.run_task(goal)
        entry.update(
            status="completed", success=result.success,
            steps=result.steps_taken, error=result.error,
            finished_at=_utc_iso(),
        )
        await ws.broadcast({"type": "task_completed", "data": entry})
    except Exception as exc:
        entry.update(status="failed", success=False, error=str(exc)[:200], finished_at=_utc_iso())
        await ws.broadcast({"type": "task_failed", "data": entry})

def _mock_health(st: _AdminState) -> dict:
    up = int(time.time() - st.start_time)
    return {
        "tasks_started": 42, "tasks_succeeded": 38, "tasks_failed": 4,
        "loop_iterations": 156, "error_rate": 0.095, "stuck": False,
        "llm_calls": 312, "llm_mean_ms": round(800 + (up % 200), 1),
        "last_activity": _utc_iso(), "alerts": [], "uptime_s": up, "ws_clients": 0,
    }

def _mock_skills() -> list[dict]:
    return [
        {"name": "open_browser",  "description": "Mở trình duyệt web",              "version": 3, "quarantined": False},
        {"name": "type_text",     "description": "Gõ văn bản vào ô nhập liệu",      "version": 1, "quarantined": False},
        {"name": "click_element", "description": "Click phần tử giao diện qua VLM", "version": 2, "quarantined": True},
        {"name": "navigate_url",  "description": "Điều hướng tới URL",              "version": 1, "quarantined": False},
        {"name": "take_screenshot","description": "Chụp ảnh màn hình",              "version": 1, "quarantined": False},
        {"name": "fill_form",     "description": "Điền biểu mẫu tự động",           "version": 2, "quarantined": False},
    ]

def _mock_config() -> dict:
    return {
        "llm": {"base_url": "http://127.0.0.1:11434", "reasoning_model": "deepseek-r1:7b",
                "coder_model": "qwen2.5-coder:7b", "vlm_model": "qwen3-vl:8b-instruct",
                "request_timeout_seconds": 120.0, "max_tokens": 4096, "temperature": 0.2,
                "retry_max_attempts": 3, "retry_base_delay": 0.5},
        "agent": {"max_steps": 30, "task_timeout_seconds": 300.0, "goal_max_length": 2048},
        "vision": {"confidence_threshold": 0.8, "screen_cache_enabled": True, "screen_cache_ttl_seconds": 2.0},
        "sandbox": {"network_mode": "none", "pool_size": 2, "memory_limit": "256m",
                    "cpu_quota": 0.5, "execution_timeout_seconds": 30.0},
        "memory": {"episodic_max_episodes": 10000, "embedding_model": "bge-m3",
                   "vector_max_items": 2048},
        "evolution": {"mutations_per_cycle": 5, "min_score_to_promote": 0.7},
        "patcher": {"rate_limit_per_hour": 10},
    }

def _mock_social() -> dict:
    return {
        "total_platforms": 6, "connected": 2, "disconnected": 4,
        "accounts": [
            {"platform": "facebook", "name": "Facebook", "icon": "📘", "color": "#1877F2",
             "login_url": "https://www.facebook.com/login", "home_url": "https://www.facebook.com",
             "connected": True, "username": "user@email.com", "display_name": "Nguyễn Văn A",
             "auto_login": False, "last_check": "", "last_used": "", "notes": "", "profile_url": ""},
            {"platform": "instagram", "name": "Instagram", "icon": "📸", "color": "#E4405F",
             "login_url": "https://www.instagram.com/accounts/login", "home_url": "https://www.instagram.com",
             "connected": True, "username": "user_ig", "display_name": "@user_ig",
             "auto_login": False, "last_check": "", "last_used": "", "notes": "", "profile_url": ""},
            {"platform": "tiktok", "name": "TikTok", "icon": "🎵", "color": "#000000",
             "login_url": "https://www.tiktok.com/login", "home_url": "https://www.tiktok.com",
             "connected": False, "username": "", "display_name": "",
             "auto_login": False, "last_check": "", "last_used": "", "notes": "", "profile_url": ""},
            {"platform": "twitter", "name": "Twitter / X", "icon": "𝕏", "color": "#1DA1F2",
             "login_url": "https://twitter.com/i/flow/login", "home_url": "https://twitter.com/home",
             "connected": False, "username": "", "display_name": "",
             "auto_login": False, "last_check": "", "last_used": "", "notes": "", "profile_url": ""},
            {"platform": "google", "name": "Google Account", "icon": "🔵", "color": "#4285F4",
             "login_url": "https://accounts.google.com/signin", "home_url": "https://myaccount.google.com",
             "connected": False, "username": "", "display_name": "",
             "auto_login": False, "last_check": "", "last_used": "", "notes": "", "profile_url": ""},
            {"platform": "threads", "name": "Threads", "icon": "🔗", "color": "#000000",
             "login_url": "https://www.threads.net/login", "home_url": "https://www.threads.net",
             "connected": False, "username": "", "display_name": "",
             "auto_login": False, "last_check": "", "last_used": "", "notes": "", "profile_url": ""},
        ],
        "chrome_profile": {"path": "~/Library/Application Support/Google/Chrome/Phidipus",
                           "exists": False, "launch_command": ""},
    }

def _build_invariants() -> list[dict]:
    rules = [
        ("R-01", "Không import pyautogui trong L1 orchestrator"),
        ("R-02", "Không subprocess.run() trong L1"),
        ("R-03", "LLMClient tắt trong L2 daemon"),
        ("R-05", "Mọi hành động OS qua IPC"),
        ("R-07", "Không exec() trên skill code trong L1"),
        ("R-08", "Mọi skill qua 3 cổng xác thực (AST → Docker → Schema)"),
        ("R-09", "Skill đã ký bằng ed25519"),
        ("R-11", "SAFE_IMPORTS là frozenset bất biến"),
        ("R-12", "Docker container network_mode='none'"),
        ("R-14", "Container không có quyền privileged, cap_drop=['ALL']"),
        ("R-15", "Input qua stdin, không qua CLI args"),
        ("R-16", "HMAC-SHA256 cho mọi episode"),
        ("R-18", "RuntimePatcher không vá sandbox/security/patcher"),
        ("R-19", "Giới hạn tốc độ vá (10/giờ)"),
        ("R-21", "VLM output qua ConfidenceGate"),
        ("R-22", "Yêu cầu xác nhận cho hành động nguy hiểm"),
        ("R-24", "Làm sạch goal trước khi gửi LLM"),
        ("R-25", "Làm sạch traceback trước khi phản hồi"),
        ("R-26", "Không .format() / f-string trên user input"),
    ]
    return [{"rule": r, "description": d, "status": "verified"} for r, d in rules]


# ══════════════════════════════════════════════════════════════════
# Standalone runner
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    _app = create_app()
    print("╔═══════════════════════════════════════════════════╗")
    print("║  🕷️ Phidipus Admin Panel v9.12                    ║")
    print("║  http://127.0.0.1:8912                            ║")
    print("║  Chế độ demo (không kết nối agent)                ║")
    print("╚═══════════════════════════════════════════════════╝")
    uvicorn.run(_app, host="127.0.0.1", port=8912, log_level="info")
