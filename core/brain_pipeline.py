# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/brain_pipeline.py — Phidipus Brain Pipeline (P3 Last Resort Classifier)
═══════════════════════════════════════════════════════════════════════════════

Tích hợp vào agent_loop.py giữa SmartAction (P1-3) và SkillForge (P4).

Architecture:
  User Input
     ↓
  [P0: RULE LAYER]     — regex/keyword → 0ms, 100% cho known patterns
     ↓ (no match)
  [P1: MODEL]          — phidipus-brain-v6 via Ollama API
     ↓
  [P2: OVERRIDE]       — sửa known model mistakes dựa trên response
     ↓
  [P3: VALIDATOR]      — verify JSON structure, node types, WF names
     ↓ (invalid)
  [P4: RAG RETRY]      — few-shot examples → retry
     ↓
  BrainResult

Usage trong agent_loop.py:
  from core.brain_pipeline import BrainPipeline

  brain = BrainPipeline(model="phidipus-brain-v6")
  result = await brain.process("loop không có max_items, có vấn đề?")
  # result.action = "suggest_fix"
  # result.response = {"action":"suggest_fix","diagnosis":"...","fixes":[...]}
  # result.source = "rule" | "model" | "override" | "rag"
"""

import asyncio
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════
_DEFAULT_MODEL = "phidipus-brain-v6"
_TIMEOUT = 60
_VRAM_ENABLED = True                 # Tắt nếu RAM >32GB (không cần swap)

# ── MLX_LM Backend (for phidipus-brain-v7, Qwen3.5-4B) ──────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MLX_MODEL_PATH = "models/Qwen3.5-4B-mlx-4bit"   # MLX format model
_MLX_ADAPTER_PATH = "adapters_v7"                   # LoRA adapter
_MLX_MAX_TOKENS = 1024     # [FIX v4.2] đủ token sinh workflow JSON đầy đủ
_MLX_IDLE_UNLOAD_S = 600   # v4.3: free ~2.5 GB unified memory after 10 idle minutes
_V7_MODEL_NAME = "phidipus-brain-v7"


def _brain_cfg() -> dict:
    """config.yaml → brain: {model_path, adapter_path, idle_unload_s, max_tokens}."""
    try:
        from core.model_registry import config_section
        return config_section("brain")
    except Exception:
        return {}


def _ollama_base() -> str:
    try:
        from core.model_registry import ollama_url
        return ollama_url()
    except Exception:
        return "http://127.0.0.1:11434"


def _worker_model() -> str:
    """The Ollama model that is swapped out while the v6 Brain runs."""
    try:
        from core.model_registry import get_model
        return get_model("reasoning", "qwen3:8b")
    except Exception:
        return "qwen3:8b"


# Full system prompt (baked into Ollama Modelfile for v6,
# but needed explicitly for mlx_lm v7)
_V7_SYSTEM_PROMPT = """Bạn là Phidipus Brain — Workflow Compiler cho Phidipus Agent trên macOS.

NHIỆM VỤ: Nhận lệnh user → phân tích → trả về JSON hợp lệ.

NODE TYPES (25 loại):
chrome_navigate, chrome_get_text, wait, vision_read, vision_click,
type_text, ai_process, notify_telegram, file_write, file_read,
file_copy, terminal_run, screenshot, generate_report, create_document,
scheduler, loop, condition, store_variable, get_variable,
http_request, email_send, email_read, db_query, ocr_read

WORKFLOWS CÓ SẴN (chỉ 15, không hơn):
bao_cao_doanh_so, theo_doi_gia_doi_thu, email_follow_up, kpi_dashboard,
dang_bai_linkedin, tim_lead_google, check_email, tao_bao_gia,
dat_lich_hop, backup_file, kiem_tra_ton_kho, cham_soc_messenger,
tong_hop_tin_tuc, xuat_crm, nghien_cuu_doi_thu

BẮT BUỘC:
- LUÔN trả về JSON. KHÔNG trả text mô tả. KHÔNG markdown.
- action phải là 1 trong 8 loại dưới đây.

ACTION TYPES & RANH GIỚI CHÍNH XÁC:

run_workflow:
  → Dùng KHI: lệnh khớp CHÍNH XÁC 1 trong 15 WF có sẵn ở trên
  → KHÔNG dùng khi: task có chi tiết bổ sung (so sánh nhiều nguồn, filter, platform mới...)
  → Output: {"action":"run_workflow","workflow_name":"<tên WF>"}

create_workflow:
  → Dùng KHI: task MỚI không match CHÍNH XÁC bất kỳ WF nào trong 15 WF
  → Bao gồm: task quen nhưng có chi tiết KHÁC (3 sàn cụ thể, đối tác cụ thể, platform mới...)
  → Output: {"action":"create_workflow","workflow":{"nodes":[...]}}

compose_workflows:
  → Dùng KHI: ghép ≥2 WF có sẵn trong danh sách 15 WF, tối đa 3 WF
  → Output: {"action":"compose_workflows","sub_tasks":[...],"workflow":{"nodes":[...]}}

refuse:
  → Dùng KHI: BẤT HỢP PHÁP / PHÁ HOẠI / XÂM PHẠM quyền riêng tư người khác
  → Gồm: hack, exploit, DDoS, keylogger, đọc tin nhắn người khác bí mật, xóa system
  → Output: {"action":"refuse","reason":"...","category":"..."}

warn:
  → Dùng KHI: task HỢP LỆ về mặt pháp lý nhưng có RỦI RO KỸ THUẬT hoặc HIỆU SUẤT
  → Gồm: đăng quá nhiều, workflow quá lớn, rate limit, financial risk
  → KHÔNG dùng khi task đang bị hỏng (→ dùng suggest_fix)
  → Output: {"action":"warn","message":"...","risks":[...],"can_proceed":true/false}

suggest_fix:
  → Dùng KHI: có thứ ĐANG HỎNG / KHÔNG HOẠT ĐỘNG / THIẾU CONFIG / SAI THỨ TỰ
  → Trigger words: "không gửi được", "bị lỗi", "fail", "thiếu config", "không hoạt động",
    "có vấn đề gì không?", "đúng không?", "trả về rỗng", "không nhận diện được"
  → Phân biệt với warn: suggest_fix = đang broken NOW. warn = có thể fail TƯƠNG LAI
  → Output: {"action":"suggest_fix","diagnosis":"...","fixes":[...],"fixed_workflow":{...}}

clarify:
  → Dùng KHI: input THIẾU THÔNG TIN CỐT LÕI để thực hiện (không biết làm GÌ)
  → Ví dụ: "Đăng bài" (không biết platform), "Gửi email" (không biết cho ai)
  → KHÔNG dùng cho câu hỏi kiến thức — câu hỏi về hệ thống → answer_knowledge
  → Output: {"action":"clarify","questions":["..."]}

answer_knowledge:
  → Dùng KHI: câu hỏi về hệ thống Phidipus, node types, workflows, config
  → Trigger: "X là gì?", "X hoạt động thế nào?", "X cần config gì?",
    "có bao nhiêu X?", "sự khác nhau giữa X và Y?"
  → LUÔN trả lời ngay, KHÔNG hỏi lại clarify
  → Output: {"action":"answer_knowledge","answer":"...","details":{...}}"""

VALID_ACTIONS = {
    "run_workflow", "create_workflow", "compose_workflows",
    "refuse", "warn", "suggest_fix", "clarify", "answer_knowledge",
}

VALID_WFS = {
    "bao_cao_doanh_so", "theo_doi_gia_doi_thu", "email_follow_up",
    "kpi_dashboard", "dang_bai_linkedin", "tim_lead_google",
    "check_email", "tao_bao_gia", "dat_lich_hop", "backup_file",
    "kiem_tra_ton_kho", "cham_soc_messenger", "tong_hop_tin_tuc",
    "xuat_crm", "nghien_cuu_doi_thu",
}

VALID_NODES = {
    "chrome_navigate", "chrome_get_text", "wait", "vision_read",
    "vision_click", "type_text", "ai_process", "notify_telegram",
    "file_write", "file_read", "file_copy", "terminal_run",
    "screenshot", "generate_report", "create_document", "scheduler",
    "loop", "condition", "store_variable", "get_variable",
    "http_request", "email_send", "email_read", "db_query", "ocr_read",
}


# ══════════════════════════════════════════════════════════════════
# RESULT DATACLASS
# ══════════════════════════════════════════════════════════════════
@dataclass
class BrainResult:
    """Result from Brain Pipeline."""
    action: str = "clarify"
    response: dict = field(default_factory=dict)
    source: str = ""           # "rule" | "model" | "override" | "rag"
    confidence: float = 1.0
    latency_ms: int = 0
    valid: bool = True
    issues: list = field(default_factory=list)

    @property
    def handled(self) -> bool:
        return self.action in VALID_ACTIONS

    @property
    def is_executable(self) -> bool:
        """True if result contains a workflow ready to execute."""
        return self.action in ("run_workflow", "create_workflow", "compose_workflows")

    @property
    def workflow_name(self) -> Optional[str]:
        if self.action == "run_workflow":
            return self.response.get("workflow_name")
        return None

    @property
    def workflow(self) -> Optional[dict]:
        if self.action in ("create_workflow", "compose_workflows"):
            return self.response.get("workflow")
        return None


def _vlog(icon: str, msg: str):
    print(f"  {icon} [Brain] {msg}")


# ══════════════════════════════════════════════════════════════════
# RULE LAYER
# ══════════════════════════════════════════════════════════════════

def _word_boundary_match(keyword: str, text: str) -> bool:
    """
    [FIX v4.1] Kiểm tra keyword xuất hiện như một từ ĐỘC LẬP trong text.

    Dùng regex thay vì 'kw in text' thuần để tránh false-positive tiếng Việt.
    Tiếng Việt dùng space làm ranh giới từ → kiểm tra space/punctuation quanh kw.

    Ví dụ false-positive cũ (đã fix):
      "ảo" in "tham khảo"  → True  (SAI — "ảo" là substring của "khảo")
      "lén" in "điện mặt"  → True  (SAI — "lén" là substring của "điện")
      "hack" in "hackathon" → True (SAI — không phải từ độc lập)
    """
    pattern = (
        r'(?:^|[\s,.:;!?"\'()\[\]/\\-])'
        + re.escape(keyword)
        + r'(?:$|[\s,.:;!?"\'()\[\]/\\-])'
    )
    return bool(re.search(pattern, text, re.IGNORECASE))


def _rule_classify(user_input: str) -> Optional[str]:
    """
    Keyword/regex classification. Returns action or None.
    Catches cases the model consistently gets wrong.

    [FIX v4.1] Tất cả refuse_kw dùng _word_boundary_match() thay vì 'kw in u'
    để tránh false-positive. Lỗi cũ: "tham khảo" → refuse vì "ảo" ⊂ "khảo".
    """
    u = user_input.lower().strip()

    # ── REFUSE ──
    # Lưu ý: đã bỏ khoảng trắng trailing "hack " → dùng word boundary thay thế
    refuse_kw = [
        "giả mạo", "fake", "phishing", "keylogger",
        "sql injection", "xss", "ddos", "brute force", "bypass",
        "exploit", "clone website",
    ]
    # [FIX v4.2] "không biết" xoá khỏi refuse_kw — quá chung, gây false refuse
    # [FIX v4.2] "hack", "lén", "bí mật", "ảo" cần context check:
    #   - "hack" trong "hackathon" → OK. "hack server" → refuse
    #   - "bí mật" trong "bí mật kinh doanh" → OK. "đọc bí mật người khác" → refuse
    #   - "lén" trong "lén đọc email" → refuse. "điện mặt trời" → OK (substring, đã fix)
    if _word_boundary_match("hack", u) and not any(w in u for w in ["hackathon", "hackers news", "hackernews"]):
        return "refuse"
    if _word_boundary_match("lén", u) and any(w in u for w in ["lén đọc", "lén xem", "lén theo dõi", "lén cài"]):
        return "refuse"
    if _word_boundary_match("bí mật", u) and any(w in u for w in ["người khác", "đối thủ", "lén", "trộm", "đánh cắp"]):
        return "refuse"
    if _word_boundary_match("ảo", u) and any(w in u for w in ["tài khoản", "account", "profile", "số điện thoại", "danh tính"]):
        return "refuse"
    if "spam" in u and any(w in u for w in ["comment", "bài viết", "post", "group"]):
        return "refuse"
    # "ảo"/"fake"/"bot" trong context follow/like — dùng word boundary cho "ảo"
    if ("follow" in u or "like" in u) and (
        _word_boundary_match("ảo", u) or "fake" in u or "bot" in u
    ):
        return "refuse"
    # [FIX] Dùng _word_boundary_match thay vì 'kw in u'
    for kw in refuse_kw:
        if _word_boundary_match(kw, u):
            return "refuse"

    # ── SUGGEST_FIX ──
    fix_re = [
        r"workflow.*đúng không", r"đúng không\??$",
        r"có vấn đề", r"vấn đề.*không",
        r"thiếu.*config", r"thiếu gì", r"cần sửa", r"sai ở đâu",
        r"không nhất quán", r"không hoạt động", r"không gửi được",
        r"trả.*rỗng", r"không trả về",
        r"bị lỗi", r"báo lỗi", r"exit code",
        r"không trigger", r"bị stuck", r"bị dừng", r"không chạy được",
        r"có nên tối ưu", r"có nên chia", r"có cần chia",
        r"\d+ nodes.*có nên", r"\d+ nodes.*có xử lý",
    ]
    for pat in fix_re:
        if re.search(pat, u):
            return "suggest_fix"

    # ── CLARIFY ──
    clarify_exact = {
        "tạo document", "tạo file", "tạo doc", "download",
        "report", "báo cáo", "phân tích", "kiểm tra",
        "gửi thông báo", "thông báo", "gửi tin", "post bài",
        "chạy", "xem", "lưu", "làm", "ok chạy đi",
        "làm cái gì đó", "chạy script", "tạo báo cáo",
        "tạo workflow mới",
    }
    word_count = len(u.split())
    safe_short = {
        "check email", "check inbox", "backup file", "stock check",
        "check kho", "find leads", "competitor analysis",
    }
    if u in clarify_exact or (word_count <= 2 and u not in safe_short):
        return "clarify"

    # ── CREATE: email + specific sender/filter ──
    if ("email" in u or "mail" in u) and any(kw in u for kw in [
        "từ ", "from ", "@", "corp", "đối tác", "sếp", "ceo",
        "team dev", "nhà cung", "invoice", "filter", "lọc",
    ]):
        return "create_workflow"

    # ── CREATE: download ảnh/file ──
    if any(kw in u for kw in ["download ảnh", "tải ảnh", "download sản phẩm"]):
        return "create_workflow"

    # ── WARN: N×M pipeline ──
    if re.search(r"\d+\s*leads.*\d+.*báo giá", u) or re.search(r"\d+.*×\s*\d+", u):
        return "warn"

    # ── RUN: "viết/soạn/post LinkedIn" ──
    if "linkedin" in u and any(kw in u for kw in [
        "viết", "soạn", "post", "đăng", "write", "content",
    ]):
        return "run_workflow"

    return None


# ══════════════════════════════════════════════════════════════════
# LAZY VRAM MANAGER (Ollama v6 only)
# Unload the worker LLM before loading the Brain, reload lazily after.
# Peak RAM = 1 model at a time → M1 16GB OK.
# ══════════════════════════════════════════════════════════════════

def _ollama_loaded_models() -> list:
    """Models currently loaded in Ollama RAM."""
    try:
        with urllib.request.urlopen(f"{_ollama_base()}/api/ps", timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


def _ollama_unload(model: str) -> bool:
    """Unload *model* from Ollama memory (keep_alive=0)."""
    try:
        payload = json.dumps({"model": model, "keep_alive": 0, "prompt": "", "stream": False}).encode("utf-8")
        req = urllib.request.Request(
            f"{_ollama_base()}/api/generate", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        _vlog("⚠️", f"VRAM unload {model}: {e}")
        return False


def _vram_swap_to_brain(brain_model: str = _DEFAULT_MODEL) -> None:
    if not _VRAM_ENABLED:
        return
    loaded = _ollama_loaded_models()
    worker = _worker_model()
    if worker in loaded and brain_model not in loaded:
        _vlog("🔄", f"VRAM: unload {worker} → load {brain_model}")
        _ollama_unload(worker)


def _vram_swap_to_worker(brain_model: str = _DEFAULT_MODEL) -> None:
    if not _VRAM_ENABLED:
        return
    if brain_model in _ollama_loaded_models():
        _ollama_unload(brain_model)


# ══════════════════════════════════════════════════════════════════
# OLLAMA API (v6)
# ══════════════════════════════════════════════════════════════════
def _call_ollama_sync(prompt: str, model: str = _DEFAULT_MODEL, temperature: float = 0.2) -> str:
    """Synchronous Ollama call. Returns "" on failure (caller falls through)."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_ollama_base()}/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")).get("response", "")
    except Exception as e:
        _vlog("⚠️", f"Ollama error: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════
# MLX BACKEND (v7) — v4.3
# ══════════════════════════════════════════════════════════════════
# Before v4.3 every Brain call spawned `python3 -m mlx_lm generate`:
#   * bare `python3` (system interpreter, often without mlx_lm),
#   * a hand-built ChatML prompt that the CLI wrapped in the chat template
#     AGAIN (double template → off-distribution input for the LoRA),
#   * the 4B model was re-loaded from disk on every call (5–10 s),
#   * temperature was ignored.
# Now the model is loaded once in-process, generation is serialised on ONE
# dedicated thread (MLX streams are thread-bound) and the weights are freed
# after an idle period.  The prompt is rendered exactly like the training
# data: system + user, generation prompt with an empty <think></think> block.

def _resolve(path: str) -> Path:
    p = Path(os.path.expanduser(path))
    return p if p.is_absolute() else _PROJECT_ROOT / p


class _MLXBrainBackend:
    def __init__(self) -> None:
        cfg = _brain_cfg()
        self.model_path = _resolve(os.environ.get("PHIDIPUS_BRAIN_MODEL_PATH")
                                   or cfg.get("model_path") or _MLX_MODEL_PATH)
        self.adapter_path = _resolve(os.environ.get("PHIDIPUS_BRAIN_ADAPTER_PATH")
                                     or cfg.get("adapter_path") or _MLX_ADAPTER_PATH)
        self.idle_unload_s = float(cfg.get("idle_unload_s", _MLX_IDLE_UNLOAD_S))
        self.max_tokens = int(cfg.get("max_tokens", _MLX_MAX_TOKENS))
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="brain-mlx")
        self._model = None
        self._tokenizer = None
        self._last_used = 0.0
        self._timer: Optional[threading.Timer] = None
        self._inprocess_ok: Optional[bool] = None

    # ── availability ─────────────────────────────────────────────
    def files_present(self) -> bool:
        return (self.model_path / "config.json").exists()

    # ── in-process path (runs on the dedicated thread) ───────────
    def _load(self) -> None:
        if self._model is not None:
            return
        from mlx_lm import load  # imported on this thread on purpose
        adapter = str(self.adapter_path) if (self.adapter_path / "adapters.safetensors").exists() else None
        t0 = time.time()
        self._model, self._tokenizer = load(str(self.model_path), adapter_path=adapter)
        _vlog("🧠", f"Brain v7 loaded in-process ({time.time() - t0:.1f}s, adapter={'yes' if adapter else 'no'})")

    def _generate_on_thread(self, user_input: str, temperature: float) -> str:
        self._load()
        from mlx_lm import generate
        from mlx_lm.sample_utils import make_sampler
        messages = [{"role": "system", "content": _V7_SYSTEM_PROMPT},
                    {"role": "user", "content": user_input}]
        try:
            prompt = self._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        except TypeError:
            prompt = self._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
        tokens = self._tokenizer.encode(prompt, add_special_tokens=False)
        text = generate(self._model, self._tokenizer, prompt=tokens,
                        max_tokens=self.max_tokens, sampler=make_sampler(temp=max(0.0, temperature)),
                        verbose=False)
        self._last_used = time.time()
        return text

    def _unload_on_thread(self) -> None:
        if self._model is None:
            return
        if time.time() - self._last_used < self.idle_unload_s - 1:
            return  # used again meanwhile
        self._model = None
        self._tokenizer = None
        try:
            import gc
            import mlx.core as mx
            gc.collect()
            mx.clear_cache()
        except Exception:
            pass
        _vlog("💤", "Brain v7 unloaded (idle)")

    def _schedule_unload(self) -> None:
        if self.idle_unload_s <= 0:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.idle_unload_s,
                                      lambda: self._executor.submit(self._unload_on_thread))
        self._timer.daemon = True
        self._timer.start()

    # ── subprocess fallback (mlx_lm importable only by the interpreter) ──
    def _generate_subprocess(self, user_input: str, temperature: float) -> str:
        cmd = [sys.executable, "-m", "mlx_lm", "generate", "--model", str(self.model_path)]
        if (self.adapter_path / "adapters.safetensors").exists():
            cmd += ["--adapter-path", str(self.adapter_path)]
        cmd += [
            "--system-prompt", _V7_SYSTEM_PROMPT,
            "--prompt", user_input,
            "--max-tokens", str(self.max_tokens),
            "--temp", str(max(0.0, temperature)),
            "--chat-template-config", json.dumps({"enable_thinking": False}),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=str(_PROJECT_ROOT))
        except subprocess.TimeoutExpired:
            _vlog("⚠️", "mlx_lm subprocess timeout (180s)")
            return ""
        if res.returncode != 0:
            _vlog("⚠️", f"mlx_lm subprocess failed: {res.stderr.strip()[-200:]}")
            return ""
        out, inside, lines = res.stdout, False, []
        for line in out.split("\n"):
            if line.startswith("=========="):
                inside = not inside
                continue
            if inside:
                lines.append(line)
        return "\n".join(lines) or out

    # ── public ───────────────────────────────────────────────────
    def generate(self, user_input: str, temperature: float = 0.2) -> str:
        """Returns the raw model text, or "" when the backend is unavailable."""
        if not self.files_present():
            _vlog("⚠️", f"Brain v7 model not found: {self.model_path}")
            return ""
        if self._inprocess_ok is not False:
            try:
                fut = self._executor.submit(self._generate_on_thread, user_input, temperature)
                text = fut.result(timeout=240)
                self._inprocess_ok = True
                self._schedule_unload()
                return text
            except ImportError as exc:
                _vlog("⚠️", f"mlx_lm not importable in-process ({exc}) → subprocess")
                self._inprocess_ok = False
            except concurrent.futures.TimeoutError:
                _vlog("⚠️", "Brain v7 generation timeout (240s)")
                return ""
            except Exception as exc:
                _vlog("⚠️", f"Brain v7 in-process error: {str(exc)[:160]}")
                return ""
        return self._generate_subprocess(user_input, temperature)

    def unload_now(self) -> None:
        self._last_used = 0.0
        self._executor.submit(self._unload_on_thread)


_MLX_BACKEND: Optional[_MLXBrainBackend] = None
_MLX_BACKEND_LOCK = threading.Lock()


def _mlx_backend() -> _MLXBrainBackend:
    global _MLX_BACKEND
    with _MLX_BACKEND_LOCK:
        if _MLX_BACKEND is None:
            _MLX_BACKEND = _MLXBrainBackend()
        return _MLX_BACKEND


def _call_mlx_lm_sync(user_input: str, model_path: str = "", adapter_path: str = "",
                      temperature: float = 0.2) -> str:
    """Backward-compatible wrapper (paths now come from config / defaults)."""
    return _mlx_backend().generate(user_input, temperature=temperature)


def _parse_response(raw: str) -> dict:
    """Extract the JSON object from a model response ({} when there is none)."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL)
    text = text.split("</think>")[-1].strip()   # generation may start inside the think block
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # First balanced JSON object (string-aware)
    depth, start, in_str, esc = 0, -1, False, False
    for i, c in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    start = -1

    m = re.search(r'"action"\s*:\s*"(\w+)"', text)
    if m:
        return {"action": m.group(1)}
    return {}


# ══════════════════════════════════════════════════════════════════
# OVERRIDE LAYER
# ══════════════════════════════════════════════════════════════════
def _override(user_input: str, result: dict) -> dict:
    """Post-model correction for known failure patterns."""
    u = user_input.lower()
    action = result.get("action", "")

    # warn → suggest_fix: model diagnosed correctly but chose wrong action
    if action == "warn":
        msg = result.get("message", "").lower()
        if any(kw in msg for kw in [
            "thứ tự không đúng", "thứ tự sai", "phải ở cuối", "phải ở đầu",
            "chạy vô hạn", "vô hạn", "infinite loop",
            "không nhất quán", "thiếu",
        ]):
            return {
                "action": "suggest_fix",
                "diagnosis": result.get("message", ""),
                "fixes": result.get("risks", []),
            }

    # warn → refuse: fake engagement
    if action == "warn" and any(kw in u for kw in [
        "spam comment", "follow ảo", "like ảo", "fake", "ảo",
    ]):
        return {
            "action": "refuse",
            "reason": result.get("message", "Vi phạm ToS"),
            "category": "platform_manipulation",
        }

    # run check_email → create (specific sender)
    if (action == "run_workflow"
            and result.get("workflow_name") == "check_email"
            and any(kw in u for kw in ["từ ", "from ", "@", "corp", "đối tác"])):
        return None  # Signal: needs regeneration

    # answer_knowledge → suggest_fix (optimization question)
    if action == "answer_knowledge" and any(kw in u for kw in [
        "tối ưu", "có nên", "nên chia", "quá nhiều node",
    ]):
        return {
            "action": "suggest_fix",
            "diagnosis": result.get("answer", ""),
            "fixes": ["Xem chi tiết trong diagnosis"],
        }

    return result


# ══════════════════════════════════════════════════════════════════
# VALIDATOR
# ══════════════════════════════════════════════════════════════════
def _validate(result: dict) -> tuple:
    """Returns (is_valid, issues_list)."""
    action = result.get("action", "")
    issues = []

    if action not in VALID_ACTIONS:
        issues.append(f"Invalid action: {action}")

    if action == "run_workflow":
        wf = result.get("workflow_name", "")
        if wf not in VALID_WFS:
            issues.append(f"Invalid workflow: {wf}")

    if action in ("create_workflow", "compose_workflows"):
        wf = result.get("workflow", {})
        if isinstance(wf, dict):
            nodes = wf.get("nodes", [])
            if not nodes:
                issues.append("No nodes found")
            for nd in nodes:
                if isinstance(nd, dict):
                    nt = nd.get("type", "")
                    if nt and nt not in VALID_NODES:
                        issues.append(f"Invalid node: {nt}")
        else:
            issues.append("workflow not dict")

    return len(issues) == 0, issues


# ══════════════════════════════════════════════════════════════════
# RAG FEW-SHOT
# ══════════════════════════════════════════════════════════════════
_RAG_EXAMPLES = {
    "suggest_fix": [
        "loop không có max_items, có vấn đề?",
        "Workflow notify→ai→chrome. Đúng không?",
        "ai_process không nhất quán",
        "vision_click thiếu target",
        "25 nodes có nên tối ưu?",
    ],
    "refuse": [
        "Spam comment đối thủ", "Follow ảo Instagram",
        "SQL injection database", "Đọc tin nhắn Zalo lén",
    ],
    "warn": [
        "Scheduler mỗi 5 phút 24/7", "Loop 10000 items",
        "Auto retweet 50/ngày", "Ghép 5 workflows",
    ],
    "create_workflow": [
        "Đọc email từ ABC Corp", "Download ảnh sản phẩm",
        "Check giá iPhone Shopee vs Tiki",
    ],
}


def _rag_hint(user_input: str) -> str:
    """Few-shot hint appended to the user message for a retry."""
    words_u = set(user_input.lower().split())
    scored = []
    for action, examples in _RAG_EXAMPLES.items():
        for ex in examples:
            overlap = len(set(ex.lower().split()) & words_u)
            if overlap:
                scored.append((overlap, ex, action))
    scored.sort(reverse=True)
    lines = [f'- "{ex}" → {act}' for _, ex, act in scored[:3]]
    examples = ("\nVí dụ đã phân loại đúng:\n" + "\n".join(lines)) if lines else ""
    return ("\n\n(Yêu cầu có chi tiết riêng — trả JSON create_workflow với workflow.nodes "
            "dùng đúng NODE TYPES." + examples + ")")


_REFUSE_REASON = ("Yêu cầu có dấu hiệu vi phạm pháp luật, quyền riêng tư hoặc điều khoản "
                  "của nền tảng nên Phidipus không thực hiện.")


def _clarify_questions(user_input: str) -> list:
    u = user_input.strip()
    return [
        f"Bạn muốn làm cụ thể việc gì với “{u[:60]}”?",
        "Dùng ứng dụng / trang web / nguồn dữ liệu nào?",
        "Kết quả cần gửi về đâu (Telegram, file, email…)?",
    ]


# ══════════════════════════════════════════════════════════════════
# MAIN PIPELINE CLASS
# ══════════════════════════════════════════════════════════════════
class BrainPipeline:
    """
    Phidipus Brain — Smart Pipeline.

    Usage:
        brain = BrainPipeline(model="phidipus-brain-v7")
        result = await brain.process("loop không có max_items?")

    v4.3: when the backend is unavailable or returns garbage the result action
    is "unavailable" (agent_loop falls through to Skill Intelligence / Forge)
    instead of the old silent "clarify" dead-end.
    """

    def __init__(self, model: str = _DEFAULT_MODEL, verbose: bool = True):
        self._model = model
        self._verbose = verbose
        self._stats = {
            "rule_hits": 0, "model_calls": 0,
            "overrides": 0, "rag_retries": 0,
            "unavailable": 0, "total": 0,
        }

    @property
    def is_mlx(self) -> bool:
        return self._model == _V7_MODEL_NAME or "v7" in self._model

    @property
    def available(self) -> bool:
        if self.is_mlx:
            return _mlx_backend().files_present()
        return True

    async def process(self, user_input: str) -> BrainResult:
        """Main entry point — runs the blocking pipeline in a worker thread."""
        return await asyncio.to_thread(self._process_sync, user_input)

    def _call_model(self, user_input: str, temperature: float = 0.2) -> str:
        """Dispatch to mlx (v7) or Ollama (v6). Returns "" on failure."""
        self._stats["model_calls"] += 1
        if self.is_mlx:
            return _mlx_backend().generate(user_input, temperature=temperature)
        return _call_ollama_sync(user_input, model=self._model, temperature=temperature)

    def process_sync(self, user_input: str) -> BrainResult:
        """Synchronous entry point."""
        return self._process_sync(user_input)

    def unload(self) -> None:
        if self.is_mlx and _MLX_BACKEND is not None:
            _MLX_BACKEND.unload_now()

    def _process_sync(self, user_input: str) -> BrainResult:
        """Swap VRAM to the Brain (Ollama v6 only) → run → swap back."""
        t0 = time.time()
        if not self.is_mlx:
            _vram_swap_to_brain(self._model)
        try:
            return self._process_inner(user_input, t0)
        finally:
            if not self.is_mlx:
                _vram_swap_to_worker(self._model)

    # ── helpers ───────────────────────────────────────────────────
    def _ms(self, t0: float) -> int:
        return int((time.time() - t0) * 1000)

    def _unavailable(self, t0: float, why: str) -> BrainResult:
        self._stats["unavailable"] += 1
        if self._verbose:
            _vlog("⚠️", f"Brain unavailable: {why}")
        return BrainResult(action="unavailable", response={}, source="error",
                           latency_ms=self._ms(t0), valid=False, issues=[why])

    def _retry_workflow(self, user_input: str) -> Optional[dict]:
        """P4: regenerate a create/compose workflow with a few-shot hint."""
        hint = _rag_hint(user_input)
        for temp in (0.4, 0.7):
            self._stats["rag_retries"] += 1
            raw = self._call_model(user_input + hint, temperature=temp)
            if not raw:
                return None
            cand = _parse_response(raw)
            ok, _ = _validate(cand)
            if ok and cand.get("action") in ("create_workflow", "compose_workflows"):
                return cand
        return None

    def _process_inner(self, user_input: str, t0: float) -> BrainResult:
        self._stats["total"] += 1

        # ── P0: Rule Layer — no model call when the rule already decides ──
        rule_action = _rule_classify(user_input)
        if rule_action:
            self._stats["rule_hits"] += 1
            if self._verbose:
                _vlog("📏", f"Rule → {rule_action}")
            if rule_action == "refuse":
                return BrainResult(action="refuse", source="rule", latency_ms=self._ms(t0),
                                   response={"action": "refuse", "reason": _REFUSE_REASON,
                                             "category": "policy"})
            if rule_action == "clarify":
                return BrainResult(action="clarify", source="rule", latency_ms=self._ms(t0),
                                   response={"action": "clarify",
                                             "questions": _clarify_questions(user_input)})
            if rule_action == "run_workflow" and "linkedin" in user_input.lower():
                return BrainResult(action="run_workflow", source="rule", latency_ms=self._ms(t0),
                                   response={"action": "run_workflow",
                                             "workflow_name": "dang_bai_linkedin"})

            # suggest_fix / create_workflow / warn → the model writes the content
            raw = self._call_model(user_input)
            if not raw:
                return self._unavailable(t0, "backend returned nothing")
            resp = _parse_response(raw)
            if rule_action == "suggest_fix" and resp.get("action") == "warn":
                resp = {"action": "suggest_fix", "diagnosis": resp.get("message", ""),
                        "fixes": resp.get("risks", [])}
            resp["action"] = rule_action
            valid, issues = _validate(resp)
            source = "rule"
            if rule_action == "create_workflow" and not valid:
                retry = self._retry_workflow(user_input)
                if retry:
                    resp, valid, issues, source = retry, True, [], "rag"
            return BrainResult(action=resp.get("action", rule_action), response=resp, source=source,
                               latency_ms=self._ms(t0), valid=valid, issues=issues)

        # ── P1: Model ──
        raw = self._call_model(user_input)
        if not raw:
            return self._unavailable(t0, "backend returned nothing")
        result = _parse_response(raw)
        action = result.get("action", "")
        if action not in VALID_ACTIONS:
            return self._unavailable(t0, f"unparsable output: {raw[:80]!r}")
        source = "model"
        if self._verbose:
            _vlog("🧠", f"Model → {action}")

        # ── P2: Override ──
        overridden = _override(user_input, result)
        if overridden is None:
            retry = self._retry_workflow(user_input)
            result = retry or {"action": "create_workflow"}
            source = "rag"
        elif overridden.get("action") != action:
            self._stats["overrides"] += 1
            if self._verbose:
                _vlog("🔄", f"Override {action} → {overridden['action']}")
            result = overridden
            source = "override"
        action = result.get("action", "clarify")

        # ── P3: Validate (+ P4 retry for workflows) ──
        is_valid, issues = _validate(result)
        if not is_valid and action in ("create_workflow", "compose_workflows"):
            if self._verbose:
                _vlog("⚠️", f"Invalid: {issues}")
            retry = self._retry_workflow(user_input)
            if retry:
                result, is_valid, issues, source = retry, True, [], "rag"
        if not is_valid and action == "run_workflow":
            # user-taught workflows are valid targets too
            try:
                from core.workflow_nodes_ext import resolve_workflow
                if resolve_workflow(result.get("workflow_name", "")):
                    is_valid, issues = True, []
            except Exception:
                pass

        return BrainResult(
            action=result.get("action", "clarify"),
            response=result,
            source=source,
            latency_ms=self._ms(t0),
            valid=is_valid,
            issues=issues if not is_valid else [],
        )

    @property
    def stats(self) -> dict:
        return self._stats.copy()


# ══════════════════════════════════════════════════════════════════
# STANDALONE CLI
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 -m core.brain_pipeline 'your command'")
        print("  python3 -m core.brain_pipeline --test ood_test_set.jsonl")
        sys.exit(1)

    brain = BrainPipeline()

    if sys.argv[1] == "--test":
        test_file = sys.argv[2] if len(sys.argv) > 2 else "training_data_v9/ood_test_set.jsonl"
        tests = [json.loads(l) for l in open(test_file)]

        correct = 0
        for i, test in enumerate(tests):
            user = test["messages"][1]["content"]
            exp = json.loads(test["messages"][2]["content"]).get("action", "")
            result = brain.process_sync(user)
            ok = result.action == exp
            if ok:
                correct += 1
            print(f"[{i+1}/{len(tests)}] {'✅' if ok else '❌'} exp={exp:20s} got={result.action:20s} src={result.source:8s} | {user[:50]}")

        print(f"\nAccuracy: {100*correct//len(tests)}% ({correct}/{len(tests)})")
        print(f"Stats: {brain.stats}")
    else:
        user = " ".join(sys.argv[1:])
        result = brain.process_sync(user)
        print(json.dumps(result.response, ensure_ascii=False, indent=2))
        print(f"\n[{result.source}] {result.action} ({result.latency_ms}ms)")
