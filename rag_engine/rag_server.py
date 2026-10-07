#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
rag_engine/rag_server.py — Phidipus AI Forge E6
═══════════════════════════════════════════════════════════════════

RAG Server: FastAPI inference endpoint cho domain chatbot.

Architecture:
  USER QUERY
    ↓
  [Situation Classifier]  — nhận diện situation type
    ↓
  [RAG Engine]            — vector search trong Framework Store
    ↓
  [Context Builder]       — build augmented prompt
    ↓
  [Qwen3-1.7B inference]  — generate response via Ollama
    ↓
  [Hallucinate Guard]     — 2-tier check
    ↓
  [Conversation Logger]   — ghi log cho feedback loop
    ↓
  OUTPUT

Cài đặt:
  pip install fastapi uvicorn

Chạy:
  uvicorn rag_engine.rag_server:app --host 0.0.0.0 --port 8000
  
  # Với domain config:
  DOMAIN_CONFIG=domains/ban_hang.yaml uvicorn rag_engine.rag_server:app --port 8000
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

import yaml


# ══════════════════════════════════════════════════════════════════
# Config — load từ env var hoặc default
# ══════════════════════════════════════════════════════════════════

DOMAIN_CONFIG_PATH = os.environ.get("DOMAIN_CONFIG", "domains/ban_hang.yaml")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_TEMP = float(os.environ.get("LLM_TEMPERATURE", "0.3"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "512"))


def load_domain_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"[WARN] Domain config không tồn tại: {path} — dùng config rỗng")
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ══════════════════════════════════════════════════════════════════
# Situation Classifier (lightweight keyword matching)
# ══════════════════════════════════════════════════════════════════

SITUATION_KEYWORDS: dict[str, list[str]] = {
    "price_objection":       ["giá cao", "đắt", "không có ngân sách", "chỗ khác rẻ hơn", "giảm giá"],
    "customer_silent":       ["im lặng", "không phản hồi", "không trả lời", "mất hứng"],
    "competitor_comparison": ["bên X", "đối thủ", "cạnh tranh", "so sánh", "tốt hơn không"],
    "need_to_think":         ["suy nghĩ", "cân nhắc", "để anh xem", "chưa quyết định"],
    "ask_family":            ["hỏi vợ", "hỏi chồng", "gia đình", "tham khảo"],
    "first_contact":         ["giới thiệu", "lần đầu", "muốn biết", "tư vấn"],
    "follow_up":             ["hôm trước", "lần trước", "đã nói", "follow up"],
    "complaint_handling":    ["phàn nàn", "khiếu nại", "không hài lòng", "tệ quá"],
    "upsell_opportunity":    ["thêm", "nâng cấp", "package cao hơn", "nhiều hơn"],
    "symptom_inquiry":       ["triệu chứng", "đau", "bệnh", "sốt", "mệt"],
    "stress_anxiety":        ["lo lắng", "stress", "căng thẳng", "áp lực", "lo âu"],
    "crisis_detection":      ["không muốn sống", "tự làm hại", "muốn chết", "vô vọng", "hết hi vọng"],
}


def classify_situation(user_input: str) -> str:
    """Simple keyword-based situation classifier."""
    user_lower = user_input.lower()
    scores: dict[str, int] = {}

    for situation, keywords in SITUATION_KEYWORDS.items():
        for kw in keywords:
            if kw in user_lower:
                scores[situation] = scores.get(situation, 0) + 1

    if not scores:
        return "general_inquiry"
    return max(scores, key=lambda k: scores[k])


# ══════════════════════════════════════════════════════════════════
# Context Builder
# ══════════════════════════════════════════════════════════════════

def build_rag_context(
    situation_type: str,
    domain_config: dict,
    top_k: int = 2,
) -> str:
    """Build RAG context từ framework store."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from rag_engine.framework_store import get_relevant_frameworks, format_framework_for_rag
        frameworks = get_relevant_frameworks(situation_type, domain_config, top_k=top_k)
        if not frameworks:
            return ""
        parts = [format_framework_for_rag(fw) for fw in frameworks]
        return "\n\n---\n\n".join(parts)
    except ImportError:
        return ""


def build_augmented_system_prompt(
    domain_config: dict,
    rag_context: str,
) -> str:
    """Build final system prompt = persona + refusal rules + RAG context."""
    dataset_cfg = domain_config.get("dataset", {})
    persona = dataset_cfg.get("persona", "Bạn là trợ lý AI hữu ích.")
    refusal_rules = dataset_cfg.get("refusal_rules", [])

    parts = [persona]

    if refusal_rules:
        rules_str = "\n".join(f"- {r}" for r in refusal_rules)
        parts.append(f"\nQUY TẮC BẮT BUỘC:\n{rules_str}")

    if rag_context:
        parts.append(f"\n\nCHIẾN LƯỢC THAM KHẢO:\n{rag_context}")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════
# Ollama inference
# ══════════════════════════════════════════════════════════════════

def ollama_chat(model: str, system: str, user: str, temperature: float = 0.3) -> Optional[str]:
    """Gọi Ollama /api/chat."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": MAX_TOKENS,
        },
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            return data.get("message", {}).get("content", "")
    except Exception as e:
        print(f"[ERROR] Ollama: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════════════

if _HAS_FASTAPI:
    app = FastAPI(
        title="Phidipus AI Forge — RAG Server",
        description="Domain chatbot inference với RAG + Hallucinate Guard",
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Load domain config on startup
    _domain_config: dict = {}
    _guard = None
    _logger = None

    @app.on_event("startup")
    async def startup():
        global _domain_config, _guard, _logger
        _domain_config = load_domain_config(DOMAIN_CONFIG_PATH)
        domain_name = _domain_config.get("domain", {}).get("name", "unknown")
        print(f"[OK] Domain loaded: {domain_name}")
        print(f"[OK] Model: {_domain_config.get('ollama', {}).get('model_name', 'N/A')}")

        # Init guard
        try:
            from scripts.hallucinate_guard import HallucinateGuard
            _guard = HallucinateGuard(_domain_config, use_tier2=False)  # Tier1 only for speed
            print("[OK] Hallucinate Guard ready")
        except ImportError:
            print("[WARN] Hallucinate Guard not available")

        # Init logger
        try:
            from rag_engine.conversation_logger import ConversationLogger
            _logger = ConversationLogger(domain=domain_name)
            print("[OK] Conversation Logger ready")
        except ImportError:
            print("[WARN] Conversation Logger not available")

    # ── Request/Response models ────────────────────────────────────

    class ChatRequest(BaseModel):
        message:     str
        session_id:  Optional[str] = None
        temperature: Optional[float] = None

    class ChatResponse(BaseModel):
        response:           str
        situation_detected: str
        framework_used:     str
        hallucinate_blocked:bool
        latency_ms:         int
        entry_id:           Optional[str] = None

    class FeedbackRequest(BaseModel):
        entry_id: str
        feedback: str   # "good" | "bad"

    # ── Endpoints ──────────────────────────────────────────────────

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        t0 = time.time()

        if not req.message.strip():
            raise HTTPException(status_code=400, detail="message is empty")

        model_name = _domain_config.get("ollama", {}).get("model_name", "sales-assistant-1.7b")
        rag_cfg = _domain_config.get("rag", {})
        top_k = rag_cfg.get("top_k", 2)
        temp = req.temperature or DEFAULT_TEMP

        # 1. Classify situation
        situation = classify_situation(req.message)

        # 2. Get RAG context
        rag_context = build_rag_context(situation, _domain_config, top_k=top_k)
        framework_used = ""
        if rag_context:
            # Extract framework name from context
            import re
            m = re.search(r"\[FRAMEWORK: ([^\]]+)\]", rag_context)
            framework_used = m.group(1) if m else ""

        # 3. Build augmented system prompt
        system_prompt = build_augmented_system_prompt(_domain_config, rag_context)

        # 4. LLM inference
        response = ollama_chat(model_name, system_prompt, req.message, temperature=temp)

        if response is None:
            response = "Xin lỗi, hệ thống đang gặp sự cố. Vui lòng thử lại sau."
            hallu_blocked = False
        else:
            # 5. Hallucinate check
            hallu_blocked = False
            if _guard:
                guard_result = _guard.check(response)
                hallu_blocked = guard_result.blocked
                if hallu_blocked:
                    response = _guard.get_fallback_response(situation)

        latency_ms = int((time.time() - t0) * 1000)

        # 6. Log conversation
        entry_id = None
        if _logger:
            entry_id = _logger.log(
                user_input=req.message,
                response=response,
                situation_detected=situation,
                framework_used=framework_used,
                hallucinate_check={"blocked": hallu_blocked},
                metadata={"session_id": req.session_id, "latency_ms": latency_ms},
            )

        return ChatResponse(
            response=response,
            situation_detected=situation,
            framework_used=framework_used,
            hallucinate_blocked=hallu_blocked,
            latency_ms=latency_ms,
            entry_id=entry_id,
        )

    @app.post("/feedback")
    async def feedback(req: FeedbackRequest):
        if _logger:
            _logger.update_feedback(req.entry_id, req.feedback)
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        domain_name = _domain_config.get("domain", {}).get("name", "N/A")
        model_name = _domain_config.get("ollama", {}).get("model_name", "N/A")
        return {
            "status": "ok",
            "domain": domain_name,
            "model": model_name,
            "ollama_host": OLLAMA_HOST,
        }

    @app.get("/stats")
    async def stats():
        if not _logger:
            return {"error": "logger not available"}
        return _logger.get_stats(days=7)

else:
    print("[WARN] FastAPI không có — cài: pip install fastapi uvicorn")
    print("       Sau đó chạy: uvicorn rag_engine.rag_server:app --port 8000")


# ══════════════════════════════════════════════════════════════════
# Standalone test (không cần FastAPI)
# ══════════════════════════════════════════════════════════════════

def test_pipeline(domain_config_path: str, query: str):
    """Quick test pipeline không cần FastAPI."""
    domain_cfg = load_domain_config(domain_config_path)
    model_name = domain_cfg.get("ollama", {}).get("model_name", "sales-assistant-1.7b")

    print(f"\n{'='*55}")
    print(f"Domain: {domain_cfg.get('domain', {}).get('name', '?')}")
    print(f"Model:  {model_name}")
    print(f"Query:  {query}")
    print(f"{'='*55}")

    situation = classify_situation(query)
    print(f"Situation: {situation}")

    rag_context = build_rag_context(situation, domain_cfg, top_k=2)
    if rag_context:
        print(f"RAG: {rag_context[:100]}...")

    system = build_augmented_system_prompt(domain_cfg, rag_context)
    print(f"\nGenerating response...")
    t0 = time.time()
    response = ollama_chat(model_name, system, query)
    latency = int((time.time()-t0)*1000)

    if response:
        print(f"\nResponse ({latency}ms):\n{response}")
    else:
        print("ERROR: Ollama không phản hồi. Kiểm tra: ollama serve")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default=DOMAIN_CONFIG_PATH)
    parser.add_argument("--query",  default="Khách hỏi giá ngay câu đầu, tôi xử lý thế nào?")
    parser.add_argument("--serve",  action="store_true", help="Chạy FastAPI server")
    args = parser.parse_args()

    if args.serve:
        if _HAS_FASTAPI:
            import uvicorn
            print(f"Starting RAG Server: http://0.0.0.0:8000")
            print(f"Domain config: {args.domain}")
            os.environ["DOMAIN_CONFIG"] = args.domain
            uvicorn.run("rag_engine.rag_server:app", host="0.0.0.0", port=8000, reload=False)
        else:
            print("Cần cài: pip install fastapi uvicorn")
    else:
        test_pipeline(args.domain, args.query)
