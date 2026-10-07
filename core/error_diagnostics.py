# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/error_diagnostics.py — Phidipus Error Diagnostics v1.0
═════════════════════════════════════════════════════════════

Chẩn đoán lỗi 2 tầng — tiếng Việt, tự động, không cần AI cho 80% cases.

Tầng 1 — RuleEngine (< 5ms):
  Pattern matching trên error string + context.
  Bao phủ ~80% lỗi phổ biến trong Phidipus.
  Không tốn API, không cần mạng.

Tầng 2 — AI Diagnosis (~2–5s):
  Chỉ gọi khi Tầng 1 không nhận ra lỗi.
  Dùng Ollama local qwen3:8b (không tốn API credit).
  Đọc full error + context → đưa ra gợi ý tự nhiên.

Output DiagnosisResult:
  step_name:     Tên bước bị lỗi (tiếng Việt)
  diagnosis:     Chẩn đoán ngắn gọn
  detail:        Giải thích thêm (optional)
  suggestion:    Gợi ý fix cụ thể
  can_retry:     Có nên retry không
  retry_delay_s: Chờ bao nhiêu giây trước retry
  tier:          "rule" | "ai" | "fallback"

Tích hợp:
  telegram_bot.py   — gọi sau khi task fail, gửi message có nút retry
  agent_loop.py     — inject diagnostics vào self._diagnostics
  workflow files    — truyền step_name khi raise exception

Security:
  - Không exec() code
  - Không forward error ra ngoài mạng
  - AI diagnosis chỉ gọi Ollama local (localhost:11434)
  - Timeout 15s cho AI call
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any


def _resolve_model(name: str, role: str | None = None) -> str:
    """v4.3: map a legacy hard-coded model name to an installed one (model registry)."""
    try:
        from core.model_registry import resolve_model
        return resolve_model(name, role)
    except Exception:
        return name


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;33m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════════
# Result dataclass
# ══════════════════════════════════════════════════════════════════════

@dataclass
class DiagnosisResult:
    """Kết quả chẩn đoán một lỗi."""
    step_name:     str          # "B2 — Tạo ảnh ChatGPT"
    diagnosis:     str          # "Chrome chưa login vào chatgpt.com"
    detail:        str  = ""    # Giải thích thêm
    suggestion:    str  = ""    # Gợi ý fix
    can_retry:     bool = True  # Có nên retry không
    retry_delay_s: int  = 5     # Delay trước retry (giây)
    tier:          str  = "rule"  # "rule" | "ai" | "fallback"
    error_code:    str  = ""    # Mã lỗi ngắn gọn (OLLAMA_DOWN, NO_API_KEY...)

    def format_telegram(self) -> str:
        """Format thành Markdown V2 cho Telegram."""
        def esc(t: str) -> str:
            for c in r"_*[]()~`>#+-=|{}.!":
                t = t.replace(c, f"\\{c}")
            return t

        tier_icon = {"rule": "⚡", "ai": "🤖", "fallback": "📝"}.get(self.tier, "📋")
        lines = [
            f"⚠️ *Lỗi tại {esc(self.step_name)}*",
            "",
            f"📋 *Chẩn đoán:* {esc(self.diagnosis)}",
        ]
        if self.detail:
            lines.append(f"   _{esc(self.detail)}_")
        if self.suggestion:
            lines.append(f"💊 *Gợi ý:* {esc(self.suggestion)}")
        if self.can_retry:
            lines.append(f"🔁 _Retry sau {self.retry_delay_s}s_")
        lines.append(f"\n{tier_icon} _{esc('Phân tích bởi: ' + self.tier)}_")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Tầng 1 — RULE ENGINE
# ══════════════════════════════════════════════════════════════════════

# Mỗi rule: (pattern_regex, error_code, step_name, diagnosis, detail, suggestion, can_retry, retry_delay)
_RULES: list[tuple] = [

    # ── Ollama / LLM ─────────────────────────────────────────────────
    (r"connection refused.*11434|ollama.*not.*running|cannot connect.*ollama",
     "OLLAMA_DOWN",
     "Khởi động Ollama",
     "Ollama chưa chạy",
     "Agent cần Ollama để suy luận nhưng port 11434 không phản hồi.",
     "Mở app Ollama hoặc chạy lệnh: ollama serve",
     True, 10),

    (r"model.*not found|no model named|pull.*model",
     "MODEL_MISSING",
     "Tải model Ollama",
     "Model Ollama chưa được tải về",
     "Model cần thiết chưa có trong danh sách Ollama.",
     "Chạy: ollama pull qwen3:8b  (hoặc mở Admin Panel → Ollama)",
     True, 5),

    # ── API Keys ─────────────────────────────────────────────────────
    (r"skill forge chưa cấu hình|no api key|gemini.*api.*key.*empty|chưa có.*api key",
     "NO_API_KEY",
     "Cấu hình API",
     "Chưa có API key cho Skill Forge",
     "Skill Forge cần ít nhất 1 API key (Gemini/Mistral/Cerebras) hoặc Ollama đang chạy.",
     "Vào Admin Panel → tab Providers → paste Gemini API key",
     False, 0),

    (r"429|rate.?limit|too many requests|quota.*exceed",
     "RATE_LIMIT",
     "Giới hạn API",
     "API đã đạt giới hạn quota hoặc rate limit",
     "Provider đang bị throttle — quá nhiều request trong thời gian ngắn.",
     "Chờ 60 giây rồi thử lại. Hoặc thêm API key khác vào Providers.",
     True, 60),

    (r"api.*key.*invalid|authentication.*failed|401|403.*unauthorized",
     "INVALID_KEY",
     "Xác thực API",
     "API key không hợp lệ hoặc đã hết hạn",
     "Provider từ chối API key — có thể đã bị revoke hoặc nhập sai.",
     "Kiểm tra lại API key trong Admin Panel → Providers",
     False, 0),

    # ── IPC / Daemon ─────────────────────────────────────────────────
    (r"no such file.*phidipus_ipc|ipc.*socket.*not found|daemon.*not.*start",
     "IPC_MISSING",
     "Kết nối IPC Daemon",
     "Automation daemon (L2) chưa khởi động",
     "Agent Loop (L1) không kết nối được với daemon xử lý OS actions.",
     "Khởi động lại Phidipus_Agent.command để start cả L1 và L2",
     True, 5),

    (r"address already in use.*8912|port.*8912.*occupied",
     "PORT_BUSY",
     "Khởi động Admin Panel",
     "Port 8912 đang bị chiếm bởi tiến trình cũ",
     "Phidipus version cũ có thể vẫn đang chạy ngầm.",
     "Chạy Xoa_Sach_Cai_Moi.command để kill tiến trình cũ, rồi khởi động lại",
     True, 5),

    # ── Chrome / Profile ─────────────────────────────────────────────
    (r"profile.*không tìm thấy|chrome.*profile.*not found",
     "PROFILE_MISSING",
     "Tìm Chrome Profile",
     "Chrome profile không tồn tại hoặc tên sai",
     "AppScanner không tìm thấy profile với tên đã nhập.",
     "Mở Admin Panel → xem danh sách profiles. Dùng đúng tên (vd: '00Fujin' không phải '00')",
     False, 0),

    (r"chrome.*not.*running|google chrome.*not.*open|chrome.*cần.*mở",
     "CHROME_CLOSED",
     "Khởi động Chrome",
     "Google Chrome chưa mở",
     "Workflow cần Chrome đang chạy với profile đã login.",
     "Chrome sẽ tự mở. Nếu không tự mở, mở thủ công và thử lại.",
     True, 5),

    # ── ChatGPT Image Generation ──────────────────────────────────────
    (r"image generation timed out|chatgpt.*timeout|không detect được ảnh",
     "CHATGPT_TIMEOUT",
     "B2 — Tạo ảnh ChatGPT",
     "ChatGPT mất quá 120 giây để tạo ảnh",
     "ChatGPT có thể đang bận hoặc prompt quá phức tạp.",
     "Thử lại sau ít phút. Nếu tiếp tục lỗi, mở chatgpt.com thủ công và kiểm tra.",
     True, 30),

    (r"chatgpt.*login|chatgpt.*sign in|please.*log.*in.*chatgpt",
     "CHATGPT_NOT_LOGIN",
     "B2 — Tạo ảnh ChatGPT",
     "Chrome chưa đăng nhập vào ChatGPT",
     "Profile Chrome đã mở chatgpt.com nhưng trang yêu cầu đăng nhập lại.",
     "Mở Chrome profile 00Fujin thủ công → đăng nhập ChatGPT → thử lại",
     True, 10),

    (r"download.*failed|url download failed|no result.*subprocess",
     "IMAGE_DOWNLOAD_FAIL",
     "B2 — Lưu ảnh từ ChatGPT",
     "Không download được ảnh từ ChatGPT",
     "Ảnh đã tạo xong nhưng không lấy được URL hoặc download thất bại.",
     "Thử lại. Nếu tiếp tục lỗi, kiểm tra kết nối mạng và quyền ghi ~/Phidipus/content/",
     True, 10),

    # ── Gemini Content ────────────────────────────────────────────────
    (r"gemini.*login|gemini.*sign in|gemini.*not.*logged",
     "GEMINI_NOT_LOGIN",
     "B4 — Tạo content Gemini",
     "Chrome chưa đăng nhập vào Gemini",
     "Profile Chrome mở gemini.google.com nhưng trang yêu cầu đăng nhập.",
     "Mở Chrome profile 00Fujin → vào gemini.google.com → đăng nhập Google → thử lại",
     True, 10),

    (r"gemini.*response.*empty|không lấy được.*gemini|gemini.*0 ký tự",
     "GEMINI_EMPTY",
     "B4 — Tạo content Gemini",
     "Gemini không trả về nội dung",
     "Response từ Gemini rỗng hoặc quá ngắn sau 60 giây chờ.",
     "Thử lại. Nếu tiếp tục, Phidipus sẽ tự dùng Ollama local thay thế.",
     True, 5),

    # ── Facebook ──────────────────────────────────────────────────────
    (r"facebook.*login|facebook.*not.*logged|không đăng nhập.*facebook",
     "FACEBOOK_NOT_LOGIN",
     "B5–7 — Đăng bài Facebook",
     "Chrome chưa đăng nhập vào Facebook",
     "Profile Chrome mở facebook.com nhưng trang yêu cầu đăng nhập.",
     "Mở Chrome profile 00Fujin → vào facebook.com → đăng nhập → thử lại",
     True, 10),

    (r"facebook.*post.*fail|không đăng được.*bài|post.*submit.*fail",
     "FB_POST_FAIL",
     "B7 — Đăng bài Facebook",
     "Không đăng được bài lên Facebook",
     "Có thể Facebook đã thay đổi giao diện hoặc bị rate limit đăng bài.",
     "Thử lại sau 5 phút. Nếu tiếp tục, kiểm tra Facebook xem có thông báo hạn chế không.",
     True, 300),

    # ── File / Path ───────────────────────────────────────────────────
    (r"no such file|file not found|không tồn tại.*file|path.*không tìm thấy",
     "FILE_NOT_FOUND",
     "Đọc file",
     "Không tìm thấy file cần thiết",
     "File được yêu cầu không tồn tại tại đường dẫn đã chỉ định.",
     "Kiểm tra đường dẫn file. Dùng Admin Panel → Memory để xem file đã được index chưa.",
     False, 0),

    (r"permission denied|không có quyền|access denied",
     "PERMISSION_DENIED",
     "Quyền truy cập",
     "Không có quyền đọc/ghi file hoặc thư mục",
     "Script cần quyền truy cập nhưng bị macOS chặn.",
     "Cấp quyền trong System Preferences → Privacy & Security → Files and Folders",
     False, 0),

    # ── Timeout ───────────────────────────────────────────────────────
    (r"workflow timeout.*300|task.*timeout.*300",
     "WORKFLOW_TIMEOUT",
     "Workflow tổng thể",
     "Workflow mất quá 5 phút",
     "Toàn bộ quy trình (tạo ảnh + content + đăng) vượt quá 300 giây.",
     "Kiểm tra từng bước thủ công. ChatGPT thường mất lâu nhất (30–120s).",
     True, 10),

    (r"subprocess.*timeout|skill.*timeout.*45",
     "SKILL_TIMEOUT",
     "Thực thi Skill",
     "Skill code chạy quá 45 giây và bị dừng",
     "Code được sinh ra có vòng lặp hoặc chờ quá lâu.",
     "Thử lại. Nếu tiếp tục, có thể cần prompt cụ thể hơn cho Skill Forge.",
     True, 5),

    # ── RAM / Resource ────────────────────────────────────────────────
    (r"ram.*\d+%.*quá tải|resource.*quá tải|memory.*full",
     "LOW_RAM",
     "Tài nguyên hệ thống",
     "RAM đang quá tải",
     "Máy tính không đủ RAM để chạy đồng thời Ollama + Phidipus + Chrome.",
     "Đóng bớt tab Chrome. Tắt các app không cần thiết. Thử lại sau 30 giây.",
     True, 30),

    # ── Sandbox / Security ───────────────────────────────────────────
    (r"forbidden import|forbidden call|ast.*blocked|code không an toàn",
     "SECURITY_BLOCK",
     "Kiểm tra bảo mật",
     "Skill Forge sinh ra code bị chặn bởi bộ lọc bảo mật",
     "Code chứa lệnh nguy hiểm (subprocess, os.system...) và bị AST walker từ chối.",
     "Thử diễn đạt lại yêu cầu cụ thể hơn để Skill Forge sinh code an toàn hơn.",
     True, 5),
]


class RuleEngine:
    """Tầng 1: Pattern matching < 5ms, không cần AI."""

    def diagnose(
        self,
        error: str,
        context: dict[str, Any] | None = None,
        step_name: str = "",
    ) -> DiagnosisResult | None:
        """
        Tìm rule phù hợp với error string.
        Trả về DiagnosisResult nếu match, None nếu không có rule nào phù hợp.
        """
        if not error:
            return None

        err_lower = (error + " " + str(context or "")).lower()

        for (pattern, code, default_step, diagnosis,
             detail, suggestion, can_retry, retry_delay) in _RULES:
            if re.search(pattern, err_lower, re.IGNORECASE):
                return DiagnosisResult(
                    step_name=step_name or default_step,
                    diagnosis=diagnosis,
                    detail=detail,
                    suggestion=suggestion,
                    can_retry=can_retry,
                    retry_delay_s=retry_delay,
                    tier="rule",
                    error_code=code,
                )
        return None


# ══════════════════════════════════════════════════════════════════════
# Tầng 2 — AI DIAGNOSIS (Ollama local)
# ══════════════════════════════════════════════════════════════════════

_AI_PROMPT_TEMPLATE = """Bạn là chuyên gia debug cho hệ thống Phidipus AI Agent (macOS automation).

Lỗi xảy ra tại bước: {step_name}
Thông báo lỗi: {error}
Context: {context}

Trả lời CHÍNH XÁC theo JSON (không giải thích thêm):
{{
  "diagnosis": "Chẩn đoán ngắn gọn (< 15 từ tiếng Việt)",
  "detail": "Giải thích thêm (< 30 từ, optional)",
  "suggestion": "Gợi ý fix cụ thể (< 20 từ tiếng Việt)",
  "can_retry": true hoặc false,
  "retry_delay_s": số giây nên chờ trước retry (0 nếu không retry)
}}"""


class AIDiagnostics:
    """Tầng 2: Ollama local qwen3:8b — dùng khi Rule Engine không match."""

    OLLAMA_URL    = "http://127.0.0.1:11434/api/generate"
    MODEL         = "qwen3:8b"
    TIMEOUT_S     = 15

    async def diagnose(
        self,
        error: str,
        context: dict[str, Any] | None = None,
        step_name: str = "",
    ) -> DiagnosisResult | None:
        """
        Gọi Ollama local để chẩn đoán lỗi.
        Timeout 15s — trả về None nếu Ollama không phản hồi.
        """
        prompt = _AI_PROMPT_TEMPLATE.format(
            step_name=step_name or "không xác định",
            error=error[:500],
            context=str(context or {})[:300],
        )

        data = json.dumps({
            "model": _resolve_model(self.MODEL, "reasoning"),
            "think": False,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 200,
            },
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                self.OLLAMA_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            def _call():
                with urllib.request.urlopen(req, timeout=self.TIMEOUT_S) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            resp_data = await asyncio.wait_for(
                asyncio.to_thread(_call),
                timeout=self.TIMEOUT_S + 2,
            )

            raw_text = resp_data.get("response", "").strip()
            # Strip markdown fences if present
            raw_text = re.sub(r"```[a-z]*\n?", "", raw_text).strip()

            parsed = json.loads(raw_text)
            return DiagnosisResult(
                step_name=step_name or "Không xác định",
                diagnosis=str(parsed.get("diagnosis", "Lỗi không xác định"))[:80],
                detail=str(parsed.get("detail", ""))[:120],
                suggestion=str(parsed.get("suggestion", ""))[:100],
                can_retry=bool(parsed.get("can_retry", True)),
                retry_delay_s=int(parsed.get("retry_delay_s", 10)),
                tier="ai",
                error_code="AI_DIAGNOSED",
            )

        except asyncio.TimeoutError:
            _vlog("⚠️", "AI Diagnostics timeout (15s) — dùng fallback")
            return None
        except (json.JSONDecodeError, KeyError):
            _vlog("⚠️", "AI Diagnostics parse error — dùng fallback")
            return None
        except Exception as exc:
            _vlog("⚠️", f"AI Diagnostics error: {str(exc)[:60]}")
            return None


# ══════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR — ErrorDiagnostics
# ══════════════════════════════════════════════════════════════════════

class ErrorDiagnostics:
    """
    Orchestrator 2 tầng. Entry point duy nhất cần dùng từ bên ngoài.

    Usage:
        diag = ErrorDiagnostics()

        # Trong workflow/agent khi có lỗi:
        result = await diag.diagnose(
            error="Image generation timed out",
            step_name="B2 — Tạo ảnh ChatGPT",
            context={"chrome_profile": "00Fujin", "url": "chatgpt.com"},
        )

        # result.format_telegram() → string cho Telegram bot
        # result.can_retry → True/False
        # result.retry_delay_s → giây chờ
    """

    def __init__(self) -> None:
        self._rules = RuleEngine()
        self._ai    = AIDiagnostics()

    async def diagnose(
        self,
        error: str,
        step_name: str = "",
        context: dict[str, Any] | None = None,
    ) -> DiagnosisResult:
        """
        Chẩn đoán lỗi theo 2 tầng.
        Luôn trả về DiagnosisResult — không bao giờ raise exception.
        """
        t0 = time.monotonic()

        # Tầng 1: Rule Engine (< 5ms)
        result = self._rules.diagnose(error, context, step_name)
        if result:
            elapsed = int((time.monotonic() - t0) * 1000)
            _vlog("🔍", f"[Diagnostics T1] {result.error_code} — {elapsed}ms")
            return result

        # Tầng 2: AI Diagnosis (~2–5s, Ollama local)
        _vlog("🤖", "[Diagnostics T2] Không có rule phù hợp → thử AI...")
        result = await self._ai.diagnose(error, context, step_name)
        if result:
            elapsed = int((time.monotonic() - t0) * 1000)
            _vlog("🤖", f"[Diagnostics T2] AI chẩn đoán xong — {elapsed}ms")
            return result

        # Fallback: generic message
        _vlog("📝", "[Diagnostics] Fallback generic message")
        return DiagnosisResult(
            step_name=step_name or "Không xác định",
            diagnosis="Lỗi không xác định",
            detail=f"{error[:100]}",
            suggestion="Xem log đầy đủ trong Admin Panel → tab Logs",
            can_retry=True,
            retry_delay_s=10,
            tier="fallback",
            error_code="UNKNOWN",
        )

    async def diagnose_workflow_result(
        self,
        workflow_result: Any,
        goal: str = "",
    ) -> DiagnosisResult | None:
        """
        Convenience method — nhận WorkflowResult và chẩn đoán.
        Trả về None nếu workflow thành công.
        """
        if getattr(workflow_result, "success", True):
            return None

        error    = getattr(workflow_result, "error", "") or ""
        steps    = getattr(workflow_result, "steps_done", 0)
        step_map = {
            0: "B1 — Mở Chrome Profile",
            1: "B2 — Tạo ảnh ChatGPT",
            2: "B3 — Mở tab mới",
            3: "B4 — Tạo content Gemini",
            4: "B5 — Vào Facebook",
            5: "B6-7 — Đăng bài Facebook",
        }
        step_name = step_map.get(steps, f"Bước {steps}")
        context   = {"steps_done": steps, "goal": goal[:100]}

        return await self.diagnose(error, step_name, context)


# ══════════════════════════════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════════════════════════════

_instance: ErrorDiagnostics | None = None

def get_diagnostics() -> ErrorDiagnostics:
    """Get or create the global ErrorDiagnostics instance."""
    global _instance
    if _instance is None:
        _instance = ErrorDiagnostics()
    return _instance
