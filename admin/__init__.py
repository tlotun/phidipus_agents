# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
admin — Phidipus v1.0 Admin Panel

Provides a FastAPI-powered admin interface with a single-file HTML frontend.
Binds to localhost:8912 by default.

Usage:
    from admin.admin_server import create_app, inject_components
    app = create_app()
    inject_components(app, agent_loop=loop, config=cfg, ...)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8912)
"""
