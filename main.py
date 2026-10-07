#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
main.py — Phidipus v2.4 Unified Entry Point
═══════════════════════════════════════════════

Boots the FULL agent: L2 daemon (IPC Server + OSController) + L1 orchestrator
(AgentLoop + LLM + Vision + Memory) + Admin Panel + Telegram Bot — all running
concurrently in a single asyncio event loop.

Usage:
    python main.py                  # Full agent + Admin Panel + Telegram Bot
    python main.py --no-telegram    # No Telegram Bot
    python main.py --demo           # Demo mode (no real agent, no IPC)
    python main.py --config path    # Custom config file

Architecture:
    ┌─────────────────── Single Process ───────────────────┐
    │  L2 Daemon:  IPCServer ← OSController ← UIAccess.   │
    │       ↕  Unix Socket / TCP localhost:19180           │
    │  L1 Orchestrator: AgentLoop ← LLM ← Vision ← Mem   │
    │       ↕  HTTP API                                    │
    │  Admin Panel:  FastAPI :8912 (injected live)         │
    │  Telegram Bot: polling (injected live)               │
    └─────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

# ── Ensure project root is in sys.path ────────────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Log: tắt JSON log trên terminal, chỉ giữ [OK]/[INFO] tiếng Việt ──
import logging
# Tắt hết JSON structured logs (quá ồn, tiếng Anh)
# Chỉ giữ WARNING+ để bắt lỗi thật sự
logging.getLogger().setLevel(logging.WARNING)
# Tắt hoàn toàn các module cực ồn
for _noisy in ("httpcore", "httpx", "httpcore.http11", "httpcore.connection",
               "telegram.ext.ExtBot", "telegram.ext.Updater", "telegram.ext",
               "PIL.PngImagePlugin", "PIL", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)


# ══════════════════════════════════════════════════════════════════
# Color helpers
# ══════════════════════════════════════════════════════════════════

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str)  -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str)  -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")
def fail(msg: str)  -> None: print(f"{_c('0;31', '[FAIL]')}  {msg}")


def show_ascii_banner() -> None:
    """Show Phidipus ASCII art in green from phidipus-ascii.txt."""
    art_path = _ROOT / "phidipus-ascii.txt"
    if art_path.exists():
        try:
            art = art_path.read_text(encoding="utf-8")
            print(f"\033[0;32m{art}\033[0m")
            return
        except Exception:
            pass
    # Fallback minimal banner
    print(_c("0;32", """
    ╔══════════════════════════════════════════╗
    ║          🕷️  P H I D I P U S            ║
    ║     Local AI • Vision Agent • macOS     ║
    ╚══════════════════════════════════════════╝
    """))


# ══════════════════════════════════════════════════════════════════
# Pre-flight checks
# ══════════════════════════════════════════════════════════════════

def check_ollama() -> bool:
    """Check Ollama and resolve every model role through core.model_registry.

    v4.3: the old check only looked for a hard-coded ``qwen3:8b`` although the
    agent uses 5 roles (reasoning / fast / coder / vision / embed) whose
    models are configurable and may be any installed equivalent.
    """
    try:
        from core import model_registry as _mr
        reg = _mr.get_registry()
        installed = reg.refresh(force=True)
        if not reg.is_reachable():
            raise ConnectionError("ollama unreachable")
        ok(f"Ollama đang chạy — {len(installed)} models")
        missing = reg.missing_roles(("reasoning", "fast", "coder", "vision", "embed"))
        for role in ("reasoning", "fast", "coder", "vision", "embed"):
            chosen = reg.get_model(role)
            if reg.is_installed(chosen):
                note = f" (thay cho {missing[role]})" if role in missing else ""
                ok(f"  {role:<9} → {chosen}{note}")
            else:
                warn(f"  {role:<9} → CHƯA CÓ — chạy: ollama pull {chosen}")
        return True
    except Exception:
        warn("Ollama chưa chạy — mở ứng dụng Ollama hoặc: ollama serve")
        return False


def check_keys(cfg_path: Path) -> bool:
    """Check if crypto keys exist."""
    keys_dir = cfg_path.parent / "keys"
    files = ["memory_hmac.key", "skill_signing.priv", "skill_signing.pub"]
    all_ok = True
    for f in files:
        p = keys_dir / f
        if p.exists():
            ok(f"  Khoá {f} — OK")
        else:
            warn(f"  Khoá {f} — THIẾU")
            all_ok = False
    return all_ok


def check_gemini_vision(cfg) -> bool:
    """
    A1 v9.24: Kiểm tra gemini_api_key cho Vision.
    Nếu trống → cảnh báo rõ ràng với hướng dẫn fix.
    """
    key = ""
    try:
        key = cfg.skill_forge.gemini_api_key
    except Exception:
        pass

    if key and len(key.strip()) > 8:
        ok("Gemini Vision API key — OK (Vision: ~1.5s/call)")
        return True

    # ── Cảnh báo nổi bật ──────────────────────────────────────
    print()
    print(f"  {_c('1;33', '╔══════════════════════════════════════════════════════╗')}")
    print(f"  {_c('1;33', '║')}  {_c('1;31', '⚠️  CẢNH BÁO: gemini_api_key chưa được cấu hình!')}  {_c('1;33', '  ║')}")
    print(f"  {_c('1;33', '║')}  Vision sẽ dùng Ollama qwen3-vl thay Gemini.           {_c('1;33', '║')}")
    print(f"  {_c('1;33', '║')}                                                         {_c('1;33', '║')}")
    print(f"  {_c('1;33', '║')}  Tác động:  Vision 15–25s/lần · RAM 91% · 10× chậm    {_c('1;33', '║')}")
    print(f"  {_c('1;33', '║')}                                                         {_c('1;33', '║')}")
    print(f"  {_c('1;33', '║')}  Để fix, thêm vào config.yaml:                         {_c('1;33', '║')}")
    print(f"  {_c('1;33', '║')}    {_c('0;36', 'skill_forge:')}                                           {_c('1;33', '║')}")
    print(f"  {_c('1;33', '║')}      {_c('0;36', 'gemini_api_key: "AIzaSy..."')}                         {_c('1;33', '║')}")
    print(f"  {_c('1;33', '║')}                                                         {_c('1;33', '║')}")
    print(f"  {_c('1;33', '║')}  Lấy key miễn phí: {_c('0;36', 'https://aistudio.google.com/apikey')}  {_c('1;33', '║')}")
    print(f"  {_c('1;33', '╚══════════════════════════════════════════════════════╝')}")
    print()
    warn("Tiếp tục chạy với Vision chậm (Ollama fallback)...")
    return False


def _unload_ollama_models(cfg) -> None:
    """Unload Ollama models from VRAM to stop fan noise after shutdown."""
    try:
        import urllib.request
        models = set()
        if cfg:
            for attr in ("reasoning_model", "coder_model", "vlm_model"):
                name = getattr(cfg.llm, attr, "")
                if name:
                    models.add(name)
        for model in models:
            try:
                data = json.dumps({"model": model, "keep_alive": 0}).encode()
                req = urllib.request.Request(
                    f"{cfg.llm.base_url}/api/generate",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp.read()
                ok(f"  Unloaded {model}")
            except Exception:
                pass
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# Main boot sequence
# ══════════════════════════════════════════════════════════════════

async def boot_full(args: argparse.Namespace) -> None:
    """Boot the full Phidipus agent with all components."""

    config_path = Path(args.config)
    if not config_path.exists():
        fail(f"Config file không tìm thấy: {config_path}")
        fail("Chạy Cai_Dat_Phidipus.command trước để tạo config.yaml")
        return

    # ── Step 1: Load config ───────────────────────────────────
    info("Đang tải cấu hình...")
    from config.config_loader import PhidipusConfig
    cfg = PhidipusConfig.from_file(str(config_path))
    try:
        from core import model_registry as _mr
        _mr.configure(cfg.raw)
    except Exception as _mr_err:
        warn(f"Model registry: {_mr_err}")
    ok(f"Config loaded — LLM: {cfg.llm.reasoning_model}, VLM: {cfg.llm.vlm_model}")

    # ── Step 2: Pre-flight checks ─────────────────────────────
    info("Kiểm tra hệ thống...")
    ollama_ok = check_ollama()
    keys_ok = check_keys(config_path)

    if not ollama_ok:
        fail("Ollama cần chạy để agent hoạt động!")
        fail("Mở ứng dụng Ollama, sau đó chạy lại main.py")
        return

    # ── A1 v9.24: Kiểm tra Gemini Vision API key ─────────────
    gemini_ok = check_gemini_vision(cfg)
    if not gemini_ok:
        warn("Vision sẽ dùng Ollama — chậm hơn 10× so với Gemini")

    # ── FIX v1.0: Kiểm tra qwen3:4b cho Intent Parser ──────
    try:
        from core.llm_intent_parser import get_intent_parser
        _parser = get_intent_parser()
        if _parser.available:
            ok(f"{_parser.model_name} — sẵn sàng (Tier 1.5 Intent Parser)")
            ok("  → Lần đầu sẽ tạo model phidipus-intent (baked prompt, nhanh 30%)")
        else:
            warn(f"{_parser.model_name} chưa có — chạy: ollama pull {_parser.model_name}")
            warn("→ Agent vẫn hoạt động, nhưng kém hiểu câu lệnh tự nhiên")
            _parser._enabled = False
    except Exception:
        warn("Intent Parser — không thể khởi tạo (bỏ qua)")

    # ── A3 v9.24: Kiểm tra cliclick (upload ảnh Facebook) ────
    import shutil as _shutil
    cliclick_ok = _shutil.which("cliclick") is not None
    if cliclick_ok:
        ok("cliclick — OK (Facebook drag & drop upload ready)")
    else:
        warn("cliclick chưa install — upload ảnh Facebook dùng JS fallback")
        warn("Để cài: brew install cliclick")

    if not keys_ok:
        warn("Thiếu khoá bảo mật — một số tính năng sẽ bị giới hạn")
        warn("Tạo khoá: PHIDIPUS_INSTALL_MODE=1 python -c \"from utils.hash_utils import generate_installation_keys; generate_installation_keys('./keys')\"")

    # ── Step 2b: VRAM management (Mac 16GB) ─────────────────────
    # Mac dùng unified memory (RAM = VRAM). 16GB chỉ đủ 1 model + OS.
    # Strategy: unload TẤT CẢ models → chỉ load reasoning model.
    # VLM sẽ load on-demand khi cần vision (Ollama tự hot-swap).
    info("Quản lý VRAM (Mac 16GB)...")
    import urllib.request

    # Step A: Unload TẤT CẢ models đang loaded trong Ollama
    try:
        req = urllib.request.Request(
            f"{cfg.llm.base_url}/api/ps",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            running = json.loads(resp.read().decode())
        loaded_models = [m.get("name", "") for m in running.get("models", [])]
        if loaded_models:
            info(f"  Đang unload {len(loaded_models)} models: {', '.join(loaded_models)}")
            for m in loaded_models:
                try:
                    data = json.dumps({"model": m, "keep_alive": 0}).encode()
                    req = urllib.request.Request(
                        f"{cfg.llm.base_url}/api/generate",
                        data=data,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        resp.read()
                except Exception:
                    pass
            ok(f"  Unloaded {len(loaded_models)} models — VRAM freed")
        else:
            ok("  Không có model nào đang loaded")
    except Exception:
        pass

    # Step B: Chỉ pre-warm reasoning model (~5GB)
    # VLM KHÔNG load — sẽ load on-demand khi cần vision
    try:
        from core.model_registry import get_model as _gm
        _warm_model = _gm("reasoning", cfg.llm.reasoning_model)
    except Exception:
        _warm_model = cfg.llm.reasoning_model
    info(f"  Nạp {_warm_model} vào VRAM...")
    try:
        data = json.dumps({
            "model": _warm_model,
            "prompt": "hi",
            "stream": False,
            "options": {"num_predict": 1},
        }).encode()
        req = urllib.request.Request(
            f"{cfg.llm.base_url}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
        ok(f"  {_warm_model} — loaded ✓")
    except Exception as exc:
        warn(f"  {_warm_model} — chưa load ({str(exc)[:60]})")
    info(f"  {cfg.llm.vlm_model} — sẽ load on-demand khi cần vision (tiết kiệm ~5GB)")

    # ── Step 2c: Scan installed apps + Chrome profiles ────────
    info("Quét ứng dụng đã cài + Chrome profiles...")
    from utils.app_scanner import AppScanner
    app_scanner = AppScanner()
    scan_result = app_scanner.scan()
    ok(f"  {scan_result['apps']} ứng dụng, {scan_result['chrome_profiles']} Chrome profiles")
    summary = app_scanner.summary()
    if summary["browsers"]:
        ok(f"  Browsers: {', '.join(summary['browsers'][:5])}")
    if summary["chrome_profiles"] > 0:
        ok(f"  Chrome profiles (top 5): {', '.join(summary['chrome_profile_names'][:5])}")

    # ── Step 2d: Start Vision Model Server (DINO + Florence) ──
    # v2.3: Grounding DINO + Florence-2 cho vision pipeline nhanh 50–80×.
    # Chỉ khởi động khi vision_backend != "vlm_only" trong config.
    _model_server = None
    _vision_backend = getattr(cfg.vision, "vision_backend", "vlm_only")
    if _vision_backend != "vlm_only":
        info("Khởi động Vision Model Server (DINO + Florence-2)...")
        try:
            from vision.model_server import get_model_server
            _model_server = await get_model_server()
            _models_dir = getattr(cfg.vision, "models_dir", "~/.phidipus/models")
            await _model_server.startup(
                models_dir=_models_dir,
                enabled=True,
                auto_download=True,
            )
            if _model_server.ready:
                ms_stats = _model_server.health()
                ok(f"Vision Model Server — DINO {'✅' if ms_stats['dino_loaded'] else '❌'} | "
                   f"Florence {'✅' if ms_stats['florence_loaded'] else '❌'}")
                ok(f"  VRAM: ~1.8GB (DINO 800MB + Florence 1GB)")
            else:
                warn("Vision Model Server — models chưa sẵn sàng")
                warn("  → Agent sẽ dùng VLM pipeline (chậm hơn)")
                _vision_backend = "vlm_only"
        except Exception as _ms_err:
            warn(f"Vision Model Server error: {_ms_err}")
            warn("  → Fallback về VLM pipeline")
            _model_server = None
            _vision_backend = "vlm_only"
    else:
        info("Vision backend: vlm_only (DINO+Florence tắt)")
        info("  → Bật: vision.vision_backend: dino_primary trong config.yaml")

    # ── Step 3: Start L2 Daemon (IPC Server + OS Controller) ──
    info("Khởi động L2 Daemon (IPC Server + OS Controller)...")
    from automation.os_controller import OSController
    from ipc.ipc_server import IPCServer
    from ipc.action_log import ActionLog

    os_controller = OSController(app_scanner=app_scanner)
    action_log = ActionLog(cfg)
    ipc_server = IPCServer(cfg, os_controller, action_log=action_log)

    await ipc_server.start()
    ok("L2 Daemon — IPC Server đang lắng nghe")

    # ── Step 4: Start L1 Orchestrator (AgentLoop) ─────────────
    info("Khởi động L1 Orchestrator (AgentLoop)...")
    from core.agent_loop import AgentLoop

    # Ensure data directories exist
    for d in ["data/skills/forge", "data/knowledge", "data/checkpoints", "data/plans", "data/memory"]:
        Path(d).mkdir(parents=True, exist_ok=True)

    agent_loop = AgentLoop(cfg, app_scanner=app_scanner)
    try:
        await agent_loop.startup()
        ok("L1 Orchestrator — AgentLoop sẵn sàng")
    except Exception as exc:
        warn(f"AgentLoop startup warning: {exc}")
        ok("L1 Orchestrator — AgentLoop sẵn sàng (với cảnh báo)")

    # v2.3: Inject ModelServer vào PerceptionPipeline
    if _model_server is not None and _vision_backend != "vlm_only":
        try:
            agent_loop._perception.inject_model_server(_model_server)
            ok(f"  ├─ Vision backend: {_vision_backend} (DINO+Florence → VLM fallback)")
        except Exception as _inj_err:
            warn(f"  ├─ Vision injection failed: {_inj_err} — using VLM only")
        # v2.3 Phase 7: Init DinoIntegration singleton for WorkflowExecutor
        try:
            from vision.dino_integration import init_dino_integration
            _dino_integ = init_dino_integration(_model_server)
            if _dino_integ.ready:
                ok(f"  ├─ DinoIntegration: S0c fast path ready (150–650ms)")
            else:
                warn(f"  ├─ DinoIntegration: models not loaded → S0c disabled")
        except Exception as _di_err:
            warn(f"  ├─ DinoIntegration init: {_di_err}")
    else:
        ok(f"  ├─ Vision backend: vlm_only (legacy pipeline)")

    # v9.20 modules status
    ok(f"  ├─ Event Bus: {agent_loop._bus.subscriber_count()} subscribers")
    ok(f"  ├─ Failure Memory: {agent_loop._failure_memory.stats()['total_records']} records")
    ok(f"  ├─ Task Decomposer: {agent_loop._decomposer.cache_stats()['templates']} templates")
    ok(f"  ├─ Checkpoint: {agent_loop._checkpoint.stats()['total_checkpoints']} saved")
    ok(f"  ├─ Skill Registry: {agent_loop._skill_db.stats()['active_skills']} skills")
    forge_status = "✓ Gemini" if agent_loop._forge.enabled else "✗ No API key"
    ok(f"  ├─ Skill Forge: {forge_status}")
    forge_stats = agent_loop._forge.stats()
    stack_info = forge_stats.get("fallback_stack", {})
    n_providers = stack_info.get("stack_size", 0)
    ok(f"  ├─ LLM Fallback Stack: {n_providers} providers (Gemini→OpenRouter→Ollama)")
    ok(f"  ├─ Goal Tracker: sẵn sàng")
    _sh_stats = agent_loop._healer.stats()
    ok(f"  ├─ Self-Healing v2: {_sh_stats['builtin_strategies']} built-in strategies, {_sh_stats['error_types_covered']} error types, {_sh_stats['total_learned_fixes']} learned fixes")
    ok(f"  ├─ Semantic Memory: {agent_loop._semantic.stats()['total_items']} items")
    # Phase 3 stats
    rg = agent_loop._resource_guard
    rg_snap = rg.current
    ok(f"  ├─ Resource Guard: RAM {rg_snap.ram_used_percent:.0f}% | CPU {rg_snap.cpu_percent:.0f}%")
    ok(f"  ├─ Wait-Until: temporal stability ✓")
    # Phase 4 stats
    wf_stats = agent_loop._workflow_lib.stats()
    ok(f"  ├─ Scheduler: max 3 concurrent tasks ✓")
    ok(f"  ├─ Workflow Library v2: {wf_stats['plan_cache_entries']} plans ({wf_stats['plan_cache_promoted']} promoted), {wf_stats['workflow_templates']} templates")
    ok(f"  ├─ Skill Registry: {agent_loop._skill_db.stats()['active_skills']} active skills (SQLite)")
    _si = agent_loop._intelligence.stats()
    ok(f"  ├─ Skill Intelligence: {'✓ active' if _si['enabled'] else '✗ disabled'} "
       f"(threshold={_si['registry_threshold']})")
    _re = agent_loop._recovery.stats()
    ok(f"  └─ Recovery Engine: {len(_re['skill_types'])} skill types, "
       f"re-plan max={_re['replanner']['max_attempts']}")

    # ── Step 4b: Start Ollama Health Monitor (B2 v2.4) ────────
    _ollama_monitor = None
    try:
        from core.ollama_monitor import get_ollama_monitor
        _ollama_monitor = get_ollama_monitor()
        if hasattr(agent_loop, '_bus'):
            _ollama_monitor.inject_event_bus(agent_loop._bus)
        await _ollama_monitor.start()
        ok(f"  ├─ Ollama Monitor: {'✅ Online' if _ollama_monitor.healthy else '❌ Offline'} "
           f"({', '.join(_ollama_monitor.models[:3]) if _ollama_monitor.models else 'no models'})")
    except Exception as exc:
        warn(f"  ├─ Ollama Monitor: {exc}")

    # ── Step 4c: Load Node Plugins (C3 v2.4) ──────────────────
    try:
        from core.node_registry import NodeRegistry
        loaded = NodeRegistry.load_plugins_dir("core/plugins")
        if loaded > 0:
            ok(f"  ├─ Node Plugins: {loaded} loaded ({', '.join(NodeRegistry.all_types())})")
        else:
            info(f"  ├─ Node Plugins: 0 (core/plugins/ empty)")
    except Exception as exc:
        warn(f"  ├─ Node Plugins: {exc}")

    # ── Step 5: Start Admin Panel ─────────────────────────────
    info("Khởi động Admin Panel...")
    # [C-16 FIX] Removed admin_server v1 from boot_full — use v2 only
    # v1 was fully initialized but never served, wasting memory and holding live refs
    from admin.admin_server_v2 import create_app_v2, inject_components_v2
    import uvicorn

    # [C-16 FIX] v1 admin_app creation removed
    admin_app_v2 = create_app_v2()
    # [M-20 FIX] v1 inject_components removed (no live refs leak)
    ok("Admin Panel — injected LIVE components (không phải demo)")
    inject_components_v2(
        admin_app_v2,
        agent_loop=agent_loop,
        config=cfg,
        config_path=str(config_path),
        app_scanner=app_scanner,
    )
    ok("SpiderHub v2 — components injected")
    # v2 is the PRIMARY app served on port 8912
    # v1 APIs available via admin_app_v2 /api/v1/* compatibility routes
    ok("SpiderHub v2 → primary at http://127.0.0.1:8912")

    # Start uvicorn as background task
    # Serve SpiderHub v2 as primary (has Vue SPA + all API v2 endpoints)
    uvi_config = uvicorn.Config(
        admin_app_v2,
        host="127.0.0.1",
        port=8912,
        log_level="warning",
        access_log=False,
    )
    uvi_server = uvicorn.Server(uvi_config)
    admin_task = asyncio.create_task(uvi_server.serve())
    ok("Admin Panel — http://127.0.0.1:8912")

    # ── Step 6: Start Telegram Bot (optional) ─────────────────
    bot_task = None
    if not args.no_telegram:
        info("Khởi động Telegram Bot...")
        try:
            from telegram.telegram_config import TelegramConfig
            tg_cfg = TelegramConfig()

            if tg_cfg.token_set and tg_cfg.admin_count > 0:
                from telegram.telegram_bot import PhidipusBot

                bot = PhidipusBot(
                    token=tg_cfg.token,
                    admin_ids=tg_cfg.admin_ids,
                )
                bot.inject(
                    agent_loop=agent_loop,
                    config=cfg,
                    config_raw=cfg.raw,
                    runtime_monitor=agent_loop._monitor,
                    episodic_memory=agent_loop._episodic,
                    memory_guard=agent_loop._guard,
                    app_scanner=app_scanner,
                )
                ok(f"Telegram Bot — injected LIVE (admins: {tg_cfg.admin_ids})")

                # ── COMMERCIAL: KnowledgeBee startup (Step 6.5) ──────────
                # Khởi động TRƯỚC khi bot bắt đầu nhận message
                info("Khởi động KnowledgeBee...")
                try:
                    from knowledge.knowledge_base import KnowledgeBase
                    from knowledge.fast_lookup import FastLookup
                    from automation.chrome_scraper import ChromeScraper
                    from ipc.ipc_client import IPCClient

                    # Thư mục knowledge: ~/Phidipus/knowledge/ (tạo nếu chưa có)
                    kb_dir = Path.home() / "Phidipus" / "knowledge"
                    kb_dir.mkdir(parents=True, exist_ok=True)

                    # FastLookup SQLite — một file DB per deployment
                    db_path = kb_dir / "products.db"
                    fast_lookup = FastLookup(db_path)

                    # KnowledgeBase — watch folder tự động ingest
                    knowledge_base = KnowledgeBase(
                        str(kb_dir),
                        company_name=getattr(cfg, "company_name", "Phidipus"),
                        auto_watch=True,
                    )
                    knowledge_base.attach_fast_lookup(fast_lookup)
                    await knowledge_base.start()

                    # ChromeScraper — dùng IPCClient để điều khiển Chrome nếu cần
                    _ipc_for_chrome = IPCClient(cfg)
                    chrome_scraper = ChromeScraper(
                        timeout_seconds=8.0,
                        max_concurrent=3,
                        ipc_client=_ipc_for_chrome,
                    )

                    # Inject tất cả vào bot → auto-wire SkillTemplateEngine
                    bot.inject(
                        knowledge_base=knowledge_base,
                        fast_lookup=fast_lookup,
                        chrome_scraper=chrome_scraper,
                    )

                    kb_stats = knowledge_base.stats()
                    db_stats = fast_lookup.stats()
                    ok(f"KnowledgeBee — {db_stats['total_products']} sản phẩm, "
                       f"{kb_stats['total_chunks']} chunks văn bản")
                    ok(f"  ├─ Folder: {kb_dir}")
                    ok(f"  ├─ Database: {db_path.name}")
                    ok(f"  └─ Auto-watch: ON (kéo file Excel/PDF vào {kb_dir})")

                except Exception as kb_exc:
                    warn(f"KnowledgeBee khởi động lỗi: {kb_exc}")
                    warn("→ Bot vẫn chạy bình thường, chỉ không có knowledge features")

                # FIX v4.3: bot.inject() registers separate TEXT and FILE
                # senders (core.notify_hub + agent_loop attributes).  The old
                # code replaced the text sender with a file sender here, so
                # Brain previews / clarify questions were sent as documents
                # (or silently dropped).
                from core import notify_hub as _nh
                if _nh.has_file_sender():
                    ok("Skill Forge ↔ Telegram — gửi text + file qua notify hub")

                async def run_bot():
                    try:
                        await bot.start()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        print(f"[WARN] Telegram Bot crashed: {e}")
                    finally:
                        try:
                            await bot.stop()
                        except Exception:
                            pass

                bot_task = asyncio.create_task(run_bot())
                # Register AFTER create_task so admin panel sees valid task reference
                try:
                    from admin.admin_server_v2 import _components as _admin_comps
                    _admin_comps["telegram_bot"]      = bot
                    _admin_comps["telegram_bot_task"] = bot_task
                except Exception:
                    pass
                ok("Telegram Bot — đang kết nối... (gõ /start trên Telegram)")
            else:
                warn("Telegram Bot — token/admin chưa thiết lập")
                warn("→ Vào http://127.0.0.1:8912 tab Telegram để cấu hình")
        except Exception as exc:
            warn(f"Telegram Bot lỗi: {exc}")
    else:
        info("Telegram Bot — bỏ qua (--no-telegram)")

    # ══════════════════════════════════════════════════════════
    # Step 7: WeChat Work Bot (v2.3.1)
    # ══════════════════════════════════════════════════════════
    _wechat_bot = None
    try:
        from wechat.wechat_config import WeChatConfig
        _wx_cfg = WeChatConfig()
        if _wx_cfg.enabled and _wx_cfg.credentials_set:
            from wechat.wechat_bot import PhidipusWeChatBot
            _wechat_bot = PhidipusWeChatBot(_wx_cfg)
            _wechat_bot.inject(agent_loop=agent_loop, config=cfg)

            async def run_wechat():
                try:
                    await _wechat_bot.start()
                except Exception as exc:
                    warn(f"WeChat Bot: {exc}")

            _wx_task = asyncio.create_task(run_wechat())
            try:
                from admin.admin_server_v2 import _components as _admin_comps
                _admin_comps["wechat_bot"] = _wechat_bot
            except Exception:
                pass
            ok("WeChat Work Bot — started (port {})".format(_wx_cfg.webhook_port))
        elif _wx_cfg.credentials_set and not _wx_cfg.enabled:
            info("WeChat Bot — disabled (bật trong wechat/wechat_config.json)")
        else:
            info("WeChat Bot — chưa cấu hình (xem wechat/wechat_config.py)")
    except ImportError:
        pass
    except Exception as exc:
        warn(f"WeChat Bot: {exc}")

    print()
    print(_c("1;33", "╔══════════════════════════════════════════════════════════════╗"))
    print(_c("1;33", "║") + "  🕷️  " + _c("1", "Phidipus Agents — AGENT THẬT đang chạy!") + "            " + _c("1;33", "║"))
    print(_c("1;33", "║") + "                                                              " + _c("1;33", "║"))
    print(_c("1;33", "║") + f"  📊  Admin Panel  →  {_c('0;36', 'http://127.0.0.1:8912')}                " + _c("1;33", "║"))
    print(_c("1;33", "║") + f"  🤖  LLM Model   →  {_c('0;36', cfg.llm.reasoning_model)}" + " " * (26 - len(cfg.llm.reasoning_model)) + _c("1;33", "║"))
    print(_c("1;33", "║") + f"  👁️   VLM Model   →  {_c('0;36', cfg.llm.vlm_model)}" + " " * (26 - len(cfg.llm.vlm_model)) + _c("1;33", "║"))
    # v2.3: Vision backend status
    _vb_label = _vision_backend.upper()
    _vb_detail = "DINO+Florence (<300ms)" if _vision_backend != "vlm_only" else "VLM legacy (1.5–25s)"
    _vb_str = f"{_vb_label}: {_vb_detail}"
    print(_c("1;33", "║") + f"  🔬  Vision      →  {_c('0;36', _vb_str[:26])}" + " " * max(0, 26 - len(_vb_str[:26])) + _c("1;33", "║"))
    # KnowledgeBee status
    kb_folder = str(Path.home() / "Phidipus" / "knowledge")
    print(_c("1;33", "║") + f"  📚  KnowledgeBee →  {_c('0;36', kb_folder[:26])}" + " " * max(0, 26 - len(kb_folder[:26])) + _c("1;33", "║"))
    print(_c("1;33", "║") + "                                                              " + _c("1;33", "║"))
    print(_c("1;33", "║") + "  💬  Telegram: /gia <mã SP> — tra giá ngay                  " + _c("1;33", "║"))
    print(_c("1;33", "║") + "  📁  Kéo Excel/PDF vào KnowledgeBee folder để cập nhật      " + _c("1;33", "║"))
    print(_c("1;33", "║") + "  🛑  Ctrl+C để dừng tất cả                                  " + _c("1;33", "║"))
    print(_c("1;33", "╚══════════════════════════════════════════════════════════════╝"))
    print()

    # ── B4 v9.25: Scheduled posting loop ────────────────────────
    async def _run_schedule_loop():
        # FIX v4.3: the workflow was called without ipc/llm/notify (it could
        # never post), and a 60s sleep could skip a whole minute.
        import json as _json
        from core import notify_hub as _nh
        sched_file = Path("data/schedules/social_posts.json")
        last_triggered: dict = {}
        while True:
            await asyncio.sleep(20)
            try:
                if not sched_file.exists():
                    continue
                schedules = _json.loads(sched_file.read_text(encoding="utf-8"))
                now = time.strftime("%H:%M")
                stamp = time.strftime("%Y-%m-%d ") + now
                for s in schedules:
                    if not s.get("active", True):
                        continue
                    sid = s.get("id", "")
                    t = s.get("time_str", "")
                    if t == now and last_triggered.get(sid) != stamp:
                        last_triggered[sid] = stamp
                        info(f"Schedule {sid} @ {now}: {s.get('goal','')[:60]}")
                        try:
                            from skills.workflow_spider_social_post import run_spider_social_post_workflow
                            _forge = getattr(agent_loop, "_forge", None)
                            result = await run_spider_social_post_workflow(
                                ipc_client=getattr(agent_loop, "_ipc", None),
                                llm_fallback=_forge._get_stack() if _forge is not None and _forge.enabled else None,
                                telegram_send_fn=_nh.send_text,
                                goal=s.get("goal", ""),
                            )
                            ok(f"Schedule {sid}: {'OK' if result.success else 'FAIL'}")
                        except Exception as _se:
                            warn(f"Schedule {sid} error: {_se}")
            except Exception:
                pass
    asyncio.create_task(_run_schedule_loop())
    ok("B4 Scheduled posting — active (interval 60s)")

    # ── FIX v1.0: Agent Work Scheduler ────────────────────────
    try:
        from core.scheduler_v2 import get_scheduler
        _agent_sched = get_scheduler(agent_loop=agent_loop)  # notify via core.notify_hub
        await _agent_sched.start()
        ok("📅 Agent Scheduler — active (check every 30s)")
    except Exception as _sched_err:
        warn(f"📅 Scheduler error: {_sched_err}")

    # ── FIX v1.0: OpenClaw Store status ──────────────────────
    try:
        from core.openclaw_bridge import get_installed_skills, get_pending_skills
        oc_installed = get_installed_skills()
        oc_pending = get_pending_skills()
        ok(f"🦞 OpenClaw Store — {len(oc_installed)} skills installed, {len(oc_pending)} pending review")
    except Exception:
        info("🦞 OpenClaw Store — ready (0 skills installed)")

    # ── FIX v1.0: RAG Memory Engine ──────────────────────────
    try:
        from memory.rag_engine import get_rag_engine
        _rag = await get_rag_engine()
        rag_stats = _rag.stats()
        total = sum(rag_stats.get("collections", {}).values())
        backend = rag_stats.get("backend", "unknown")
        ok(f"🧠 RAG Memory — {backend}, {total} records across {len(rag_stats.get('collections', {}))} collections")
    except Exception as _rag_err:
        warn(f"🧠 RAG Memory error: {_rag_err}")
        info("💡 Install ChromaDB: pip install chromadb --break-system-packages")

    # ── v4.3: Memory Agent (long-term memory, local SQLite) ──────
    _memory_agent = None
    try:
        from memory.memory_agent import get_memory_agent
        _memory_agent = get_memory_agent()
        if _memory_agent is not None:
            await _memory_agent.start()
            _ms = _memory_agent.stats()
            ok(f"🧠 Memory Agent — {_ms['total']} ký ức, {_ms['procedures']} quy trình đã học "
               f"(embed: {_ms['embed_model']}, quan sát task: {'bật' if _ms['observing'] else 'tắt'})")
        else:
            info("🧠 Memory Agent — tắt (memory_agent.enabled: false)")
    except Exception as _mem_err:
        warn(f"🧠 Memory Agent error: {_mem_err}")

    # ── FIX v1.0: Chrome CDP — direct browser control ────────
    try:
        from automation.chrome_cdp import get_cdp
        _cdp = await get_cdp()
        if _cdp.connected:
            ok(f"🌐 Chrome CDP — connected (port 9222, {'WebSocket' if _cdp._ws else 'HTTP'} mode)")
        else:
            info("🌐 Chrome CDP — not available (Chrome not running with --remote-debugging-port=9222)")
            info("   → Agent sẽ dùng IPC/AppleScript fallback (vẫn hoạt động)")
            info("   → Để bật CDP: mở Chrome với flag --remote-debugging-port=9222")
    except Exception as _cdp_err:
        info(f"🌐 Chrome CDP — skip ({_cdp_err})")

    # ── FIX v1.0: License verification ─────────────────────────
    try:
        from core.license_manager import get_license_manager
        _lm = await get_license_manager()
        lic = _lm.license
        if lic.valid:
            left = lic.days_left()
            ok(f"🔑 Giấy phép thương mại: {lic.licensee}"
               + (f" — còn {left} ngày" if left >= 0 else " — vĩnh viễn"))
        else:
            info("🔑 Giấy phép: PolyForm Noncommercial 1.0.0 — miễn phí cho cá nhân, nghiên cứu, "
                 "giáo dục, phi lợi nhuận; dùng thương mại cần giấy phép (COMMERCIAL.md)")
            if lic.error and "PUBLIC_KEYS" not in lic.error:
                warn(f"   File giấy phép không hợp lệ: {lic.error}")
    except Exception:
        info("🔑 Giấy phép: PolyForm Noncommercial 1.0.0 (xem LICENSE)")

    # ── FIX v1.0: ReAct Controller — self-verify + self-correct ──
    try:
        from core.react_controller import get_react_controller
        _react = get_react_controller()
        ok("🔄 ReAct Controller — active (verify + self-correct after every task)")
    except Exception:
        info("🔄 ReAct Controller — skip")

        # ── Tắt JSON log sau khi boot xong — chỉ giữ WARNING+ ─────
    for _name in list(logging.Logger.manager.loggerDict):
        _lgr = logging.getLogger(_name)
        _lgr.setLevel(logging.WARNING)
        for _h in _lgr.handlers:
            _h.setLevel(logging.WARNING)

    # ── Wait for shutdown signal ──────────────────────────────
    stop_event = asyncio.Event()
    _shutting_down = False

    def _signal_handler():
        nonlocal _shutting_down
        if _shutting_down:
            # Second Ctrl+C → force exit immediately
            print("\n[FORCE] Tắt ngay lập tức!")
            _unload_ollama_models(cfg)
            os._exit(0)
        _shutting_down = True
        info("Nhận tín hiệu dừng — đang tắt... (Ctrl+C lần nữa = tắt ngay)")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # ── Graceful shutdown (max 5s total) ─────────────────────
    print()
    info("Đang tắt...")

    # 1. Stop Telegram Bot (2s timeout)
    if bot_task and not bot_task.done():
        bot_task.cancel()
        try:
            await asyncio.wait_for(bot_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        ok("Telegram Bot — đã dừng")

    # 2. Stop Admin Panel (2s timeout)
    uvi_server.should_exit = True
    try:
        await asyncio.wait_for(admin_task, timeout=2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    ok("Admin Panel — đã dừng")

    # 2b. Stop Memory Agent background work
    if _memory_agent is not None:
        try:
            await asyncio.wait_for(_memory_agent.stop(), timeout=1)
        except (asyncio.TimeoutError, Exception):
            pass

    # 3. Stop Resource Guard
    try:
        await asyncio.wait_for(agent_loop._resource_guard.stop(), timeout=1)
    except (asyncio.TimeoutError, Exception):
        pass
    ok("Resource Guard — đã dừng")

    # 4. Stop IPC Server (1s timeout)
    try:
        await asyncio.wait_for(ipc_server.stop(), timeout=1)
    except (asyncio.TimeoutError, Exception):
        pass
    ok("IPC Server — đã dừng")

    # 4b. Stop Vision Model Server (DINO + Florence VRAM)
    if _model_server is not None:
        try:
            await asyncio.wait_for(_model_server.shutdown(), timeout=2)
        except (asyncio.TimeoutError, Exception):
            pass
        ok("Vision Model Server — đã dừng (DINO+Florence VRAM freed)")

    # 5. Unload VRAM
    _unload_ollama_models(cfg)
    ok("VRAM — đã giải phóng")

    ok("🕷️ Phidipus Agents đã tắt hoàn toàn.")


# ══════════════════════════════════════════════════════════════════
# Demo mode (for testing without Ollama/IPC)
# ══════════════════════════════════════════════════════════════════

async def boot_demo(args: argparse.Namespace) -> None:
    """Boot in demo mode — Admin Panel + Telegram Bot without real agent."""
    info("Chế độ DEMO — không có agent thật")

    from admin.admin_server import create_app
    import uvicorn

    admin_app = create_app()
    ok("Admin Panel (demo) — http://127.0.0.1:8912")

    uvi_config = uvicorn.Config(admin_app, host="127.0.0.1", port=8912,
                                 log_level="warning", access_log=False)
    uvi_server = uvicorn.Server(uvi_config)
    admin_task = asyncio.create_task(uvi_server.serve())

    bot_task = None
    if not args.no_telegram:
        try:
            from telegram.telegram_config import TelegramConfig
            tg_cfg = TelegramConfig()
            if tg_cfg.token_set:
                from telegram.telegram_bot import PhidipusBot
                bot = PhidipusBot(token=tg_cfg.token, admin_ids=tg_cfg.admin_ids)

                async def run_bot():
                    try:
                        await bot.start()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        print(f"[WARN] Telegram Bot (demo) crashed: {e}")
                    finally:
                        try:
                            await bot.stop()
                        except Exception:
                            pass

                bot_task = asyncio.create_task(run_bot())
                ok("Telegram Bot (demo) — đang kết nối")
        except Exception as exc:
            warn(f"Telegram Bot: {exc}")

    print()
    print(_c("1;33", "╔══════════════════════════════════════════════════════════════╗"))
    print(_c("1;33", "║") + "  🕷️  Phidipus Agents v4.3 — CHẾ ĐỘ DEMO                    " + _c("1;33", "║"))
    print(_c("1;33", "║") + f"  📊  Admin Panel  →  {_c('0;36', 'http://127.0.0.1:8912')}                " + _c("1;33", "║"))
    print(_c("1;33", "║") + "  🛑  Ctrl+C để dừng                                          " + _c("1;33", "║"))
    print(_c("1;33", "╚══════════════════════════════════════════════════════════════╝"))
    print()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: stop_event.set())
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    if bot_task and not bot_task.done():
        bot_task.cancel()
    uvi_server.should_exit = True
    ok("🕷️ Phidipus Agents demo đã tắt.")


# ══════════════════════════════════════════════════════════════════
# CLI entry point
# ══════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="🕷️ Phidipus Agents v4.3 — AI agents for macOS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ví dụ:
  python main.py                    # Chạy agent thật + Admin Panel + Telegram
  python main.py --demo             # Demo mode (không cần Ollama)
  python main.py --no-telegram      # Không khởi động Telegram Bot
  python main.py --config custom.yaml
""",
    )
    parser.add_argument("--config", default="config.yaml",
                        help="Đường dẫn file cấu hình (mặc định: config.yaml)")
    parser.add_argument("--demo", action="store_true",
                        help="Chế độ demo — không kết nối agent thật")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Không khởi động Telegram Bot")
    args = parser.parse_args()

    # v4.3: modules use paths relative to the project root (data/…, config.yaml);
    # starting from Finder / Login Items / another cwd broke them silently.
    _cfg = Path(args.config).expanduser()
    args.config = str(_cfg.resolve() if _cfg.exists() else (_ROOT / _cfg))
    os.chdir(_ROOT)

    # ── Show ASCII art in green ──
    show_ascii_banner()

    mode = "DEMO" if args.demo else "AGENT THẬT"
    print(f"  {_c('1;33', '▸')} Mode: {_c('1;32' if not args.demo else '1;33', mode)}")
    print()

    if args.demo:
        asyncio.run(boot_demo(args))
    else:
        asyncio.run(boot_full(args))


if __name__ == "__main__":
    main()
