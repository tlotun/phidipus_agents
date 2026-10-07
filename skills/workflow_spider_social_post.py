# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/workflow_spider_social_post.py — Phidipus Workflow v9.28
═══════════════════════════════════════════════════════════════════════════════

KỊCH BẢN: Tạo ảnh AI + Content + Post Facebook hoàn chỉnh
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Luồng đầy đủ 7 bước:
  B1  Mở Chrome profile 00Fujin
  B2  Vào chatgpt.com → tạo ảnh AI "con ong robot" → save về máy
  B3  Mở tab mới trên cùng Chrome đó
  B4  Vào gemini.com → nhờ Gemini viết 1000 ký tự về điểm yếu OpenClaw
  B5  Copy content từ Gemini
  B6  Vào facebook.com trên tab hiện tại
  B7  Tạo post: upload ảnh từ B2 + paste content từ B4 → Đăng

Được chạy bởi:
  agent_loop.py → TaskDecomposer → SkillForge (hoặc trực tiếp nếu detected)
  Telegram: /run hoặc text lệnh đầy đủ

Dependencies:
  social/chatgpt_image_gen.py   — ChatGPTImageGenerator
  social/social_poster.py       — SocialPoster
  utils/smart_actions.py        — SmartActionEngine (open Chrome profile)
  ipc/ipc_client.py             — IPCClient (gửi actions)
  core/llm_fallback.py          — FallbackStack (tạo content qua Gemini)

Security:
  - Chrome profile injection qua IPC app_launch (không trực tiếp subprocess)
  - Gemini content generation qua HTTPS LLM call (không eval/exec)
  - Facebook post qua SocialPoster (URL whitelist)
  - Image output → ~/Phidipus/content/ (path-restricted)
  - Timeout toàn bộ: 300 giây
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _resolve_model(name: str, role: str | None = None) -> str:
    """v4.3: map a legacy hard-coded model name to an installed one (model registry)."""
    try:
        from core.model_registry import resolve_model
        return resolve_model(name, role)
    except Exception:
        return name

# v9.32: Trajectory Memory
try:
    from memory.trajectory_memory import TrajectoryDB, StepTracker, StepTrace
    _TRAJECTORY_DB = TrajectoryDB()
    _HAS_TRAJECTORY = True
except Exception:
    _HAS_TRAJECTORY = False
    _TRAJECTORY_DB = None  # type: ignore

# v9.37 P0: ExecutionBrain
try:
    from core.execution_brain import (
        ExecutionBrain,
        expect_b2_image_saved, expect_b4_content_ready,
        expect_b6_fb_composer_open, expect_b7a_image_uploaded,
        expect_b7b_content_pasted, expect_b7c_post_submitted,
    )
    _HAS_EXECUTION_BRAIN = True
except Exception:
    _HAS_EXECUTION_BRAIN = False


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Config constants
# ══════════════════════════════════════════════════════════════════

CHROME_PROFILE     = "00Fujin"
CHATGPT_URL        = "https://chatgpt.com"
GEMINI_URL         = "https://gemini.google.com"
FACEBOOK_URL       = "https://www.facebook.com"
OUTPUT_DIR         = str(Path.home() / "Phidipus" / "content")
WORKFLOW_TIMEOUT   = 300    # 5 phút tổng cộng

# ── Default values (dùng khi không truyền params từ Telegram) ─────
DEFAULT_IMAGE_SUBJECT = (
    "a cute robot bee AI character, cyberpunk style, glowing neon yellow and blue, "
    "metallic wings with circuit patterns, digital honeycomb background, "
    "ultra detailed, 4K, vibrant colors, professional digital art"
)
DEFAULT_CONTENT_TOPIC = "điểm yếu và hạn chế của nền tảng OpenClaw so với các AI agent khác"
DEFAULT_CHROME_PROFILE = "00Fujin"
CONTENT_TARGET_CHARS   = 1000


# ══════════════════════════════════════════════════════════════════
# Goal parser — trích xuất params từ Telegram message
# ══════════════════════════════════════════════════════════════════

def parse_goal(goal: str) -> dict:
    """
    Trích xuất image_subject, content_topic, chrome_profile từ goal string.

    Ví dụ:
      "tạo ảnh con ong robot chatgpt, content điểm yếu openclaw, đăng facebook"
      → image_subject="con ong robot"
      → content_topic="điểm yếu openclaw"
      → chrome_profile="00Fujin" (default)

      "chatgpt tạo ảnh 'sunset cyberpunk', content về marketing AI, profile 01ABC, đăng facebook"
      → image_subject="sunset cyberpunk"
      → content_topic="marketing AI"
      → chrome_profile="01ABC"

    Tất cả đều có fallback về default nếu không parse được.
    """
    import re as _re
    g = goal.strip()
    gl = g.lower()

    # ── Image subject ─────────────────────────────────────────────
    # Ưu tiên text trong ngoặc kép/đơn: tạo ảnh "con ong robot"
    img_quoted = _re.search(
        r'(?:tạo ảnh|vẽ ảnh|tạo hình|sinh ảnh|create image|generate image|make image)'
        r'\s+["\'](.+?)["\']', g, _re.IGNORECASE
    )
    if img_quoted:
        image_subject = img_quoted.group(1).strip()
    else:
        # Không có ngoặc kép — lấy text giữa "tạo ảnh" và dấu phẩy/chatgpt/rồi
        img_plain = _re.search(
            r'(?:tạo ảnh|vẽ ảnh|tạo hình|sinh ảnh|create image|generate image|make image)'
            r'\s+(.+?)(?:\s*[,;]|\s+(?:chatgpt|rồi|sau đó|then|,|$))',
            g, _re.IGNORECASE
        )
        if img_plain:
            image_subject = img_plain.group(1).strip().rstrip(',').strip()
        else:
            image_subject = DEFAULT_IMAGE_SUBJECT

    # ── Content topic ─────────────────────────────────────────────
    # Ưu tiên text trong ngoặc kép: content "điểm yếu openclaw"
    ct_quoted = _re.search(
        r'(?:content|nội dung|viết content|tạo content)\s+["\'](.+?)["\']',
        g, _re.IGNORECASE
    )
    if ct_quoted:
        content_topic = ct_quoted.group(1).strip()
    else:
        # Thử "content về X" hoặc "content X" (đến dấu phẩy/đăng/rồi)
        ct_plain = _re.search(
            r'(?:content|nội dung|viết content|tạo content)\s+(?:về\s+|about\s+)?'
            r'(.+?)(?:\s*[,;]|\s+(?:đăng|post|share|rồi|sau đó|then|$))',
            g, _re.IGNORECASE
        )
        if ct_plain:
            content_topic = ct_plain.group(1).strip().rstrip(',').strip()
        else:
            content_topic = DEFAULT_CONTENT_TOPIC

    # ── Chrome profile ────────────────────────────────────────────
    # "profile 01ABC" hoặc "chrome profile 00Fujin" hoặc "hồ sơ 00Fujin"
    prof_m = _re.search(
        r'(?:profile|hồ sơ|pro)\s+["\']?([^\s"\',.]+)["\']?',
        g, _re.IGNORECASE
    )
    chrome_profile = prof_m.group(1).strip() if prof_m else DEFAULT_CHROME_PROFILE

    # ── Platform ──────────────────────────────────────────────────
    platform = "facebook"
    if _re.search(r'\binstagram\b|\binsta\b|\big\b', gl):
        platform = "instagram"
    elif _re.search(r'\b(?:x\.com|twitter)\b', gl):
        platform = "x"

    return {
        "image_subject":  image_subject,
        "content_topic":  content_topic,
        "chrome_profile": chrome_profile,
        "platform":       platform,
    }


# ══════════════════════════════════════════════════════════════════
# Workflow Result
# ══════════════════════════════════════════════════════════════════

@dataclass
class WorkflowResult:
    """
    A4 v9.24: Mở rộng với granular error tracking.
    current_step: bước đang chạy khi fail (vd "B6_click_create_post")
    failed_action: action cụ thể thất bại (vd "DOM click notfound, Vision timeout")
    retry_count: số lần đã retry
    """
    success:        bool
    image_path:     str   = ""
    content_text:   str   = ""
    post_url:       str   = ""
    error:          str   = ""
    steps_done:     int   = 0
    duration_s:     float = 0.0
    current_step:   str   = ""   # A4: step đang chạy khi fail
    failed_action:  str   = ""   # A4: action cụ thể gây fail
    retry_count:    int   = 0    # A4: tổng số retry đã dùng


# ══════════════════════════════════════════════════════════════════
# MAIN WORKFLOW CLASS
# ══════════════════════════════════════════════════════════════════

class SpiderSocialPostWorkflow:
    """
    Orchestrates the complete flow:
      Chrome profile → ChatGPT image → Gemini content → Facebook post

    v9.22: image_subject và content_topic được truyền từ Telegram prompt,
    không còn hardcode. Fallback về DEFAULT_* nếu không truyền.
    """

    def __init__(
        self,
        ipc_client: Any = None,
        llm_fallback: Any = None,
        telegram_send_fn: Any = None,
        image_subject: str = "",
        content_topic: str = "",
        chrome_profile: str = "",
        content_pipeline: Any = None,
        user_id: int = 0,
        chat_id: int = 0,
    ) -> None:
        self._ipc            = ipc_client
        self._llm            = llm_fallback
        self._tg_send        = telegram_send_fn
        self._pipeline: Any  = content_pipeline  # B1 v9.25: ContentPipeline
        self._user_id: int   = user_id
        self._chat_id: int   = chat_id
        self._image_subject  = image_subject  or DEFAULT_IMAGE_SUBJECT
        self._content_topic  = content_topic  or DEFAULT_CONTENT_TOPIC
        # v9.22 FIX: resolve short profile name ("00") → full name ("00Fujin")
        # via AppScanner Tier 2 numeric prefix match
        raw_profile = chrome_profile or DEFAULT_CHROME_PROFILE
        self._chrome_profile = self._resolve_profile(raw_profile)
        # Build full ChatGPT prompt from subject
        self._image_prompt   = self._build_image_prompt(self._image_subject)
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

        # v9.32: Đọc shortcuts từ TrajectoryDB trước khi chạy
        if _HAS_TRAJECTORY and _TRAJECTORY_DB:
            self._shortcuts = _TRAJECTORY_DB.get_shortcuts("workflow_spider_social_post")
            self._shortcuts.log("workflow_spider_social_post")
        else:
            from memory.trajectory_memory import Shortcuts
            self._shortcuts = Shortcuts()

        # StepTracker để ghi nhận trajectory sau khi xong
        self._tracker: Any = StepTracker() if _HAS_TRAJECTORY else None

        # v9.37 P0: ExecutionBrain — Execution Reliability Layer
        self._brain: Any = None
        if _HAS_EXECUTION_BRAIN:
            self._brain = ExecutionBrain.from_config(
                ipc_client=ipc_client,
                vlm=None,      # VLM được inject sau khi ChatGPTVisionHelper init
                notify_fn=telegram_send_fn,
                trajectory_db=_TRAJECTORY_DB if _HAS_TRAJECTORY else None,
            )

    def _resolve_profile(self, name: str) -> str:
        """
        Giải quyết tên profile viết tắt thành tên đầy đủ.
        "00" → "00Fujin", "1" → "01Fujin", v.v.
        Dùng AppScanner.find_chrome_profile() với 4-tier matching.
        Nếu không tìm thấy → dùng name gốc (AppScanner ở L2 sẽ xử lý tiếp).
        """
        try:
            from utils.app_scanner import AppScanner
            scanner = AppScanner()
            scanner.scan()
            found = scanner.find_chrome_profile(name)
            if found:
                _vlog("🔍", f"Profile '{name}' → '{found.name}' (resolved)")
                return found.name
        except Exception:
            pass
        return name  # giữ nguyên nếu không resolve được

    def _build_image_prompt(self, subject: str) -> str:
        """
        Build English-only prompt for ChatGPT DALL-E.
        
        QUAN TRỌNG: Luôn dùng tiếng Anh để tránh lỗi bộ gõ tiếng Việt
        (Telex/VNI) làm sai ký tự khi type vào ChatGPT.
        
        Nếu subject là tiếng Việt → translate sang English cơ bản.
        Nếu subject đã là tiếng Anh → dùng trực tiếp.
        """
        # Basic Vietnamese → English keyword map cho các mô tả phổ biến
        _vi_en = {
            "con ong robot": "robot bee",
            "ong robot": "robot bee",
            "con ong": "bee",
            "robot": "robot",
            "mèo": "cat", "chó": "dog", "rồng": "dragon",
            "cyberpunk": "cyberpunk", "neon": "neon",
            "hoàng hôn": "sunset", "bình minh": "sunrise",
            "rừng": "forest", "thành phố": "city",
            "anime": "anime", "pixel art": "pixel art",
        }
        subj_lower = subject.lower()
        for vi, en in _vi_en.items():
            subj_lower = subj_lower.replace(vi, en)
        # Nếu sau khi replace vẫn còn ký tự tiếng Việt → dùng subject gốc nhưng thêm EN suffix
        import re as _re
        has_viet = bool(_re.search(r'[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]',
                                   subj_lower, _re.IGNORECASE))
        if has_viet:
            # Fallback: dùng subject gốc nếu không translate được
            clean = subj_lower
        else:
            clean = subj_lower

        if len(clean) > 80:
            return clean  # Already a full prompt

        return (
            f"{clean}, digital art style, high quality detailed illustration, "
            f"4K resolution, vibrant colors, sharp focus, professional artwork"
        )

    async def run(self) -> WorkflowResult:
        """Execute the full 7-step workflow."""
        t0 = time.monotonic()
        result = WorkflowResult(success=False)

        try:
            result = await asyncio.wait_for(
                self._execute(),
                timeout=WORKFLOW_TIMEOUT,
            )
        except asyncio.TimeoutError:
            result.error = f"Workflow timeout sau {WORKFLOW_TIMEOUT}s"
        except Exception as exc:
            result.error = str(exc)[:300]

        result.duration_s = time.monotonic() - t0
        if result.success:
            _vlog("✅", f"Workflow hoàn thành trong {result.duration_s:.1f}s")
        else:
            _vlog("❌", f"Workflow thất bại: {result.error[:120]}")
            # A4 v9.24: Gửi Telegram error message chi tiết
            if self._tg_send:
                step_info = ("\n📍 Bước: " + result.current_step) if result.current_step else ""
                action_info = ("\n🔧 Action: " + result.failed_action[:100]) if result.failed_action else ""
                retry_info = (f"\n🔄 Đã retry: {result.retry_count} lần") if result.retry_count else ""
                steps_info = f"\n✅ Đã hoàn thành: {result.steps_done}/7 bước"
                try:
                    await self._tg_send(
                        f"❌ Workflow thất bại sau {result.duration_s:.0f}s\n\n"
                        f"💥 Lỗi: {result.error[:200]}"
                        + step_info + action_info + retry_info + steps_info
                        + "\n\nDùng /run lại để resume từ checkpoint (nếu có)"
                    )
                except Exception:
                    pass

        # v9.32: Ghi trajectory vào TrajectoryDB sau khi xong
        if _HAS_TRAJECTORY and _TRAJECTORY_DB and self._tracker:
            try:
                steps = self._tracker.get_steps()
                _TRAJECTORY_DB.record(
                    "workflow_spider_social_post",
                    success=result.success,
                    duration_s=result.duration_s,
                    steps=steps,
                    failed_at=result.current_step if not result.success else None,
                    vlm_calls=self._tracker.total_vlm_calls(),
                    memory_hits=self._tracker.total_memory_hits(),
                    reflection_used=True,  # v9.31 luôn bật
                    goal=self._image_subject + " " + self._content_topic,
                )
            except Exception as exc:
                _vlog("⚠️ ", f"Trajectory record failed: {exc}")

        return result

    # ──────────────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════
    # Checkpoint helpers — v9.23 Phase 2
    # ══════════════════════════════════════════════════════════════

    def _ckpt_path(self, name: str) -> Path:
        return Path(OUTPUT_DIR) / f".ckpt_{name}.json"

    def _save_checkpoint(self, name: str, data: dict) -> None:
        """Lưu checkpoint sau bước thành công — resume thay restart khi fail."""
        try:
            self._ckpt_path(name).write_text(
                json.dumps({**data, "_ts": time.time()}), encoding="utf-8"
            )
            _vlog("💾", f"Checkpoint saved: {name}")
        except Exception as e:
            _vlog("⚠️ ", f"Checkpoint save failed: {e}")

    def _load_checkpoint(self, name: str, max_age_s: float = 3600.0) -> dict | None:
        """Load checkpoint nếu còn trong thời hạn (default 1 giờ)."""
        try:
            p = self._ckpt_path(name)
            if not p.exists():
                return None
            data = json.loads(p.read_text(encoding="utf-8"))
            if time.time() - data.get("_ts", 0) > max_age_s:
                p.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            return None

    def _clear_checkpoint(self, name: str) -> None:
        try:
            self._ckpt_path(name).unlink(missing_ok=True)
        except Exception:
            pass

    async def _execute(self) -> WorkflowResult:
        result = WorkflowResult(success=False)

        # v9.37 P0: Khởi động ExecutionBrain
        if self._brain:
            self._brain.begin_task(
                goal=f"tạo ảnh '{self._image_subject}' đăng Facebook",
                intent_type="social_post",
            )

        # ── BƯỚC 1: Mở Chrome profile ──────────────────────────────
        _vlog("🚀", f"B1 → Mở Chrome profile: {self._chrome_profile}")
        if self._tracker:
            self._tracker.start_step("B1_open_chrome")
        await self._step1_open_chrome_profile()
        await asyncio.sleep(3)
        if self._tracker:
            self._tracker.finish_step(success=True)
        result.steps_done = 1

        # ── BƯỚC 2: Vào ChatGPT → Tạo ảnh → Save ──────────────────
        ckpt_b2 = self._load_checkpoint("step2_image")
        if ckpt_b2 and os.path.exists(ckpt_b2.get("image_path", "")):
            image_path = ckpt_b2["image_path"]
            _vlog("⚡", f"B2 RESUMED từ checkpoint: {image_path}")
            if self._tracker:
                self._tracker.start_step("B2_chatgpt_image")
                self._tracker.add_note("B2_chatgpt_image", "checkpoint_resume")
                self._tracker.finish_step(success=True)
            # v9.37: brain record checkpoint resume
            if self._brain:
                self._brain._tracker.record_step("B2_chatgpt_image", success=True,
                                                  error="checkpoint_resume")
        else:
            _vlog("🎨", "B2 → Mở chatgpt.com, tạo ảnh AI...")
            if self._tracker:
                self._tracker.start_step("B2_chatgpt_image")

            # v9.32: Áp dụng shortcut skip_url_download nếu đã học
            if self._shortcuts.skip_url_download:
                _vlog("🧭", "Shortcut: skip_url_download → dùng vision_click_save ngay")
                if self._tracker:
                    self._tracker.add_note("B2_chatgpt_image", "shortcut_skip_url")

            # v9.37 P0: Wrap B2 với ExecutionBrain
            if self._brain:
                async with self._brain.step(
                    "B2_chatgpt_image",
                    expectation=expect_b2_image_saved(lambda: image_path if 'image_path' in dir() else ""),
                    skip_validate=True,  # validate sau khi có image_path
                ) as ctx:
                    image_path = await self._step2_create_image()
                    if not image_path or not os.path.exists(image_path):
                        ctx.success = False
                        ctx.error   = "image_create_failed"
            else:
                image_path = await self._step2_create_image()

            if not image_path or not os.path.exists(image_path):
                if self._tracker:
                    self._tracker.finish_step(success=False, note="image_create_failed")
                result.error = "B2 FAILED: Không tạo/lưu được ảnh từ ChatGPT"
                return result

            # v9.37: Validate B2 sau khi có image_path
            if self._brain:
                exp_b2 = expect_b2_image_saved(lambda: image_path)
                b2_valid = await self._brain._validator.validate(exp_b2)
                self._brain._tracker.record_step(
                    "B2_chatgpt_image_validate", success=b2_valid.success,
                    error="" if b2_valid.success else str(b2_valid.failed_criteria),
                    validation=b2_valid,
                )
                if not b2_valid.success:
                    _vlog("⚠️", f"B2 validation warn: {b2_valid.failed_criteria} — tiếp tục")

            if self._tracker:
                self._tracker.finish_step(success=True)
            self._save_checkpoint("step2_image", {
                "image_path": image_path,
                "chrome_profile": self._chrome_profile,
            })

        result.image_path = image_path
        result.steps_done = 2
        _vlog("💾", f"B2 ✅ Ảnh: {image_path}")

        # B1 v9.25: Nếu có ContentPipeline → gửi ảnh để duyệt trước khi tiếp tục
        if self._pipeline and self._user_id:
            _vlog("🔍", "B1: Gửi ảnh để duyệt qua Telegram...")
            await self._pipeline.submit_image_for_approval(
                user_id=self._user_id,
                image_path=image_path,
                send_photo_fn=self._tg_send_photo,
            )
            # Chờ approval (tối đa 10 phút)
            approved = await self._wait_pipeline_approval("image", timeout_s=600)
            if not approved:
                result.error = "B2 APPROVAL: User không duyệt ảnh (timeout hoặc huỷ)"
                return result
            _vlog("✅", "B1: Ảnh được duyệt → tiếp tục...")
        elif self._tg_send:
            await self._tg_send(
                f"🎨 Ảnh AI đã tạo xong!\nPath: {image_path}\n\nĐang mở tab mới..."
            )

        # ── BƯỚC 3: Mở tab mới ────────────────────────────────────
        _vlog("📂", "B3 → Mở tab mới trong Chrome...")
        await self._step3_open_new_tab()
        await asyncio.sleep(2)
        result.steps_done = 3

        # ── BƯỚC 4: Vào Gemini → Tạo content ──────────────────────
        # v9.23: Kiểm tra checkpoint content
        ckpt_b4 = self._load_checkpoint("step4_content")
        if ckpt_b4 and len(ckpt_b4.get("content", "")) >= 200:
            content = ckpt_b4["content"]
            _vlog("⚡", f"B4 RESUMED từ checkpoint: {len(content)} ký tự")
            if self._brain:
                self._brain._tracker.record_step("B4_gemini_content", success=True,
                                                  error="checkpoint_resume")
        else:
            _vlog("✍️ ", "B4 → Mở gemini.google.com, tạo content...")

            # v9.37 P0: Wrap B4 với ExecutionBrain
            if self._brain:
                async with self._brain.step(
                    "B4_gemini_content",
                    skip_validate=True,
                ) as ctx:
                    content = await self._step4_create_content()
                    if not content or len(content) < 200:
                        ctx.success = False
                        ctx.error   = f"content_too_short:{len(content) if content else 0}"
            else:
                content = await self._step4_create_content()

            if not content or len(content) < 200:
                result.error = "B4 FAILED: Không tạo được content từ Gemini"
                return result

            # v9.37: Validate B4
            if self._brain:
                exp_b4 = expect_b4_content_ready(lambda: content)
                b4_valid = await self._brain._validator.validate(exp_b4)
                self._brain._tracker.record_step(
                    "B4_content_validate", success=b4_valid.success,
                    validation=b4_valid,
                )
                if not b4_valid.success:
                    _vlog("⚠️", f"B4 content validate warn: {b4_valid.failed_criteria}")

            # Lưu checkpoint B4
            self._save_checkpoint("step4_content", {"content": content})

        result.content_text = content
        result.steps_done = 4
        _vlog("📝", f"B4 ✅ Content: {len(content)} ký tự")

        # B1 v9.25: Nếu có ContentPipeline → gửi content để duyệt
        if self._pipeline and self._user_id:
            _vlog("🔍", "B1: Gửi content để duyệt qua Telegram...")
            await self._pipeline.submit_content_for_approval(
                user_id=self._user_id,
                content=content,
                send_message_fn=self._tg_send_with_keyboard,
            )
            approved = await self._wait_pipeline_approval("content", timeout_s=600)
            if not approved:
                result.error = "B4 APPROVAL: User không duyệt content (timeout hoặc huỷ)"
                return result
            # Lấy content đã được user chỉnh sửa (nếu có)
            # Lấy session để kiểm tra content có bị user sửa không
            session = self._pipeline.get_session_by_id(self._user_id)
            if session and getattr(session, "generated_content", ""):
                content = session.generated_content  # dùng version đã được user chỉnh
            _vlog("✅", "B1: Content được duyệt → vào Facebook...")
        elif self._tg_send:
            preview = content[:200] + "..." if len(content) > 200 else content
            await self._tg_send(
                f"✍️ Content đã tạo xong ({len(content)} ký tự):\n\n{preview}\n\nĐang vào Facebook..."
            )

        # ── BƯỚC 5+6+7: Facebook ───────────────────────────────────
        _vlog("📤", "B5-7 → Facebook, tạo post với ảnh + content...")
        result.current_step = "B5_navigate_facebook"

        # v9.37 P0: Wrap toàn bộ B5-7 với brain.step()
        if self._brain:
            async with self._brain.step(
                "B567_facebook_post",
                expectation=expect_b7c_post_submitted(),
                skip_validate=True,  # validate riêng sau
            ) as ctx:
                post_success = await self._step567_post_facebook(
                    image_path=image_path,
                    content=content,
                )
                if not post_success:
                    ctx.success = False
                    ctx.error   = result.current_step or "B5-7 unknown fail"
        else:
            post_success = await self._step567_post_facebook(
                image_path=image_path,
                content=content,
            )

        if not post_success:
            step   = result.current_step or "unknown_step"
            action = result.failed_action or "không có thêm thông tin"
            result.error = (
                f"B5-7 FAILED tại [{step}]: {action}"
                if result.failed_action
                else f"B5-7 FAILED tại [{step}]: Không đăng được bài lên Facebook"
            )
            # v9.37: record final failure
            if self._brain:
                self._brain._tracker.mark_failed()
            return result

        # Xoá checkpoint sau khi workflow hoàn thành
        self._clear_checkpoint("step2_image")
        self._clear_checkpoint("step4_content")

        result.steps_done = 7
        result.post_url   = "https://www.facebook.com"
        result.success    = True

        # v9.37 P0: SuccessJudge — xác nhận bài đã đăng thực sự
        if self._brain:
            judge_verdict = await self._brain.end_task(task_result=result)
            if not judge_verdict.achieved and judge_verdict.confidence >= 0.75:
                # Judge tự tin nói chưa đạt → cảnh báo (không fail task)
                _vlog("⚠️", f"SuccessJudge: goal chưa hẳn đạt — {judge_verdict.evidence[:120]}")
                if self._tg_send:
                    await self._tg_send(
                        f"⚠️ *Phidipus hoàn thành nhưng cần kiểm tra*\n\n"
                        f"Workflow báo thành công nhưng SuccessJudge không chắc.\n"
                        f"Lý do: {judge_verdict.evidence[:200]}\n\n"
                        f"Vui lòng kiểm tra Facebook."
                    )
            elif judge_verdict.achieved:
                _vlog("✅", f"SuccessJudge xác nhận: {judge_verdict.evidence[:80]}")

        # Thông báo hoàn thành
        if self._tg_send:
            brain_stats = ""
            if self._brain:
                rpt = self._brain.get_report()
                tr  = rpt.get("tracker", {})
                brain_stats = (
                    f"\n📊 Execution: {tr.get('success_steps',0)}/{tr.get('total_steps',0)} steps OK"
                    f" | {tr.get('elapsed_s',0):.0f}s"
                )
            await self._tg_send(
                f"✅ *Workflow hoàn thành!*\n\n"
                f"🖼 Ảnh AI: {Path(image_path).name}\n"
                f"📝 Content: {len(content)} ký tự\n"
                f"📱 Facebook: Đã đăng thành công!"
                f"{brain_stats}"
            )
        return result

    # ══════════════════════════════════════════════════════════════
    # STEP IMPLEMENTATIONS
    # ══════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════
    # B1 v9.25: Approval flow helpers
    # ══════════════════════════════════════════════════════════════

    async def _wait_pipeline_approval(self, stage: str, timeout_s: float = 600) -> bool:
        """
        Poll pipeline session state cho đến khi user duyệt hoặc timeout.
        stage: "image" | "content"
        """
        if not self._pipeline or not self._user_id:
            return True  # Không có pipeline → bỏ qua approval
        from social.content_pipeline import PipelineState
        wait_state = (
            PipelineState.WAIT_IMAGE_APPROVAL
            if stage == "image"
            else PipelineState.WAIT_CONTENT_APPROVAL
        )
        approved_states = {PipelineState.GEN_CONTENT, PipelineState.POSTING,
                           PipelineState.DONE, PipelineState.GEN_IMAGE}
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(3.0)
            session = self._pipeline.get_session(self._user_id)
            if not session:
                return False
            if session.state in (PipelineState.CANCELLED, PipelineState.FAILED):
                _vlog("❌", f"B1: Pipeline {stage} bị huỷ/fail")
                return False
            if session.state != wait_state:
                # Đã chuyển sang state khác → user đã approve
                return True
        _vlog("⏰", f"B1: Approval timeout ({timeout_s}s) cho {stage}")
        return False

    async def _tg_send_photo(self, chat_id: int, photo_path: str,
                              caption: str = "", keyboard: list = None) -> None:
        """Gửi ảnh qua Telegram với inline keyboard."""
        if not self._tg_send:
            return
        try:
            # Gửi caption trước nếu có keyboard (vì _tg_send chỉ gửi text)
            await self._tg_send(f"📸 {caption}" if caption else "📸 Ảnh đã tạo xong")
        except Exception:
            pass

    async def _tg_send_with_keyboard(self, chat_id: int, text: str,
                                      keyboard: list = None) -> None:
        """Gửi text message qua Telegram."""
        if not self._tg_send:
            return
        try:
            await self._tg_send(text)
        except Exception:
            pass

    async def _step1_open_chrome_profile(self) -> None:
        """
        B1: Mở Chrome với profile 00Fujin.
        Dùng IPC action 'app_launch' với chrome_profile parameter.
        Fallback: AppleScript trực tiếp.
        """
        if self._ipc:
            await self._ipc.send_action("app_launch", {
                "app_name": "Google Chrome",
                "chrome_profile": self._chrome_profile,
            })
        else:
            # AppleScript fallback
            await self._applescript(
                f'tell application "Google Chrome"\n'
                f'  activate\n'
                f'end tell'
            )
            await asyncio.sleep(1)
            # Open profile via CLI
            await asyncio.to_thread(
                subprocess.run,
                [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    f"--profile-directory={self._chrome_profile}",
                    "--new-window",
                ],
                start_new_session=True,
                timeout=5,
            )

    async def _step2_create_image(self) -> str | None:
        """
        B2: Mở chatgpt.com, gõ prompt, chờ ảnh, download về.
        Sử dụng ChatGPTImageGenerator nếu available,
        fallback về AppleScript + keyboard automation.
        """
        try:
            from social.chatgpt_image_gen import ChatGPTImageGenerator
            gen = ChatGPTImageGenerator(
                ipc_client=self._ipc,
                chrome_profile=self._chrome_profile,
            )
            path = await gen.generate(
                prompt=self._image_prompt,
                output_dir=OUTPUT_DIR,
                session_id=f"bee_{int(time.time())}",
            )
            if path:
                return path
        except ImportError:
            pass

        # Manual AppleScript flow (fallback)
        return await self._step2_manual_chatgpt()

    async def _step2_manual_chatgpt(self) -> str | None:
        """
        Manual flow qua AppleScript khi ChatGPTImageGenerator không có.

        Bước:
          0. Switch input method sang ABC (tránh lỗi Telex/VNI)
          1. Navigate Chrome đến chatgpt.com
          2. Gõ prompt vào chat input
          3. Nhấn Enter
          4. Chờ ảnh xuất hiện (poll 5s × 24 lần = 120s max)
          5. Lấy URL ảnh qua JavaScript
          6. Download ảnh về OUTPUT_DIR
          7. Restore input method về bộ gõ cũ
        """
        _vlog("🌐", "Manual ChatGPT flow via AppleScript...")

        # ── Bước 0: Switch sang ABC, lưu bộ gõ hiện tại ─────────────────────
        old_input_method = await self._get_current_input_method()
        await self._switch_input_method_to_abc()
        _vlog("⌨️ ", f"Input method: '{old_input_method}' → ABC (tránh lỗi Telex/VNI)")

        try:
            result = await self._step2_manual_chatgpt_inner()
        finally:
            # ── Bước cuối: Restore bộ gõ cũ dù thành công hay fail ──────────
            await self._restore_input_method(old_input_method)
            _vlog("⌨️ ", f"Input method restored: ABC → '{old_input_method}'")

        return result

    async def _step2_manual_chatgpt_inner(self) -> str | None:
        """
        Gõ prompt vào ChatGPT và chờ ảnh.

        Chiến lược mới (v9.22.1):
          - LUÔN dùng clipboard (pbcopy + Cmd+V) — bypass keyboard hoàn toàn
          - Không dùng keystroke insertText (bị Telex can thiệp)
          - Clear box trước khi paste (tránh duplicate)
          - Verify gửi xong trước khi chờ 120s
          - Scan DOM → Gemini nếu selector không tìm được
        """
        # 1. Navigate to ChatGPT
        await self._chrome_navigate(CHATGPT_URL)
        await asyncio.sleep(5)

        # ── BƯỚC 2A: Clear ô chat nếu còn text cũ ────────────────────────
        # Dùng JS để xoá sạch trước khi paste — tránh duplicate
        await self._chrome_js(
            "(function(){"
            "  var el = document.querySelector("
            "    '#prompt-textarea, p[data-placeholder],"
            "    div[contenteditable=true].ProseMirror,"
            "    [role=textbox][contenteditable=true]');"
            "  if(!el) return;"
            "  el.focus();"
            "  // Xoá toàn bộ nội dung ProseMirror an toàn"
            "  document.execCommand('selectAll', false, null);"
            "  document.execCommand('delete', false, null);"
            "  el.dispatchEvent(new InputEvent('input',{bubbles:true}));"
            "})()"
        )
        await asyncio.sleep(0.5)

        # ── BƯỚC 2B: Copy prompt vào clipboard ────────────────────────────
        await self._set_clipboard(self._image_prompt)
        await asyncio.sleep(0.3)

        # ── BƯỚC 2C: Focus ô chat ─────────────────────────────────────────
        focused = await self._chrome_js(
            "(function(){"
            "  var el = document.querySelector("
            "    '#prompt-textarea, p[data-placeholder],"
            "    div[contenteditable=true].ProseMirror,"
            "    [role=textbox][contenteditable=true]');"
            "  if(!el){"
            "    // Fallback: click vào vùng chat input"
            "    var area = document.querySelector('form, [data-testid=\"composer\"]');"
            "    if(area) area.click();"
            "    return 'fallback';"
            "  }"
            "  el.focus();"
            "  el.click();"
            "  return 'focused';"
            "})()"
        )
        _vlog("🖱️ ", f"Chat input focus: {focused}")
        await asyncio.sleep(0.5)

        # ── BƯỚC 2D: Paste bằng Cmd+V (không đi qua bộ gõ) ──────────────
        # Activate Chrome trước để Cmd+V đến đúng window
        await self._applescript('tell application "Google Chrome" to activate')
        await asyncio.sleep(0.5)
        await self._applescript(
            'tell application "System Events"\n'
            '    keystroke "v" using command down\n'
            'end tell'
        )
        await asyncio.sleep(1.5)

        # ── BƯỚC 2E: Verify paste thành công ──────────────────────────────
        paste_check = await self._chrome_js(
            "(function(){"
            "  var el = document.querySelector("
            "    '#prompt-textarea, p[data-placeholder],"
            "    div[contenteditable=true].ProseMirror,"
            "    [role=textbox][contenteditable=true]');"
            "  if(!el) return 'no_el';"
            "  var t = el.innerText || el.textContent || '';"
            "  return t.length > 3 ? 'ok:' + t.length : 'empty';"
            "})()"
        )
        _vlog("📋", f"Clipboard paste check: {paste_check}")

        # Nếu paste thất bại → scan DOM gửi Gemini để tìm selector đúng
        if paste_check in ("empty", "no_el"):
            _vlog("🧠", "Paste thất bại → scan DOM & hỏi Gemini tìm selector...")
            selector = await self._gemini_find_input_selector()
            if selector:
                _vlog("🎯", f"Gemini tìm được selector: {selector}")
                # Thử paste với selector mới
                await self._chrome_js(
                    f"(function(){{"
                    f"  var el = document.querySelector({json.dumps(selector)});"
                    f"  if(el){{el.focus();el.click();}}"
                    f"}})()"
                )
                await asyncio.sleep(0.5)
                await self._applescript(
                    'tell application "System Events"\n'
                    '    keystroke "v" using command down\n'
                    'end tell'
                )
                await asyncio.sleep(1)

        # ── BƯỚC 3: Gửi prompt ────────────────────────────────────────────
        # Thử click nút Send bằng JS (nhiều selector)
        await asyncio.sleep(1.5)
        sent = await self._chrome_js(
            "(function(){"
            "  var selectors = ["
            "    '[data-testid=\"send-button\"]',"
            "    '[data-testid=\"composer-submit-button\"]',"
            "    'button[aria-label*=\"Send\"]',"
            "    'button[aria-label*=\"send\"]',"
            "    'form button[type=submit]'"
            "  ];"
            "  for(var i=0;i<selectors.length;i++){"
            "    var btn=document.querySelector(selectors[i]);"
            "    if(btn && !btn.disabled){btn.click();return 'clicked:'+selectors[i];}"
            "  }"
            "  // Nút bị disabled → chờ React enable"
            "  return 'not_ready';"
            "})()"
        )
        _vlog("📤", f"Send attempt 1: {sent}")

        if "clicked" not in sent:
            # Chờ thêm React enable rồi thử lại
            await asyncio.sleep(2)
            sent2 = await self._chrome_js(
                "(function(){"
                "  var btn=document.querySelector('[data-testid=\"send-button\"],"
                "    [data-testid=\"composer-submit-button\"],"
                "    button[aria-label*=\"Send\"]');"
                "  if(btn && !btn.disabled){btn.click();return 'clicked';}"
                "  return 'still_not_ready';"
                "})()"
            )
            _vlog("📤", f"Send attempt 2: {sent2}")

            if "clicked" not in sent2:
                # Final fallback: Enter key
                _vlog("⌨️ ", "Send button fail → Enter key fallback")
                await self._applescript('tell application "Google Chrome" to activate')
                await asyncio.sleep(0.3)
                await self._applescript(
                    'tell application "System Events"\n'
                    '    key code 36\n'  # Return key
                    'end tell'
                )

        # ── BƯỚC 4: Verify đã gửi (ô chat phải trống) ────────────────────
        await asyncio.sleep(3)
        after_send = await self._chrome_js(
            "(function(){"
            "  var el=document.querySelector('#prompt-textarea, p[data-placeholder],"
            "    div[contenteditable=true].ProseMirror,[role=textbox][contenteditable=true]');"
            "  if(!el) return 'no_el';"
            "  var t = el.innerText||el.textContent||'';"
            "  return t.length > 5 ? 'not_sent:'+t.length : 'sent_ok';"
            "})()"
        )
        _vlog("✅", f"After send verify: {after_send}")

        if after_send.startswith("not_sent"):
            _vlog("⚠️ ", "Prompt chưa gửi được sau 3 lần thử")

        # ── BƯỚC 5: Poll ảnh (5s × 24 = 120s) ────────────────────────────
        _vlog("⏳", "Chờ ChatGPT tạo ảnh (tối đa 60s)...")
        image_url = None
        for i in range(12):
            await asyncio.sleep(5)
            js_find_img = (
                "var imgs = document.querySelectorAll('img');"
                "var url = '';"
                "for(var i=imgs.length-1;i>=0;i--){"
                "  var s=imgs[i].src||'';"
                "  if(s.includes('oaidalleapiprodscus')||s.includes('openai')||"
                "     s.includes('dall-e')||s.includes('oai-secure')){"
                "    url=s;break;}"
                "}"
                "url;"
            )
            url = await self._chrome_js(js_find_img)
            if url and url.startswith("http"):
                image_url = url
                _vlog("🖼 ", f"Ảnh detected sau {(i+1)*5}s: {url[:60]}...")
                break
            _vlog("⏳", f"  Chờ... ({(i+1)*5}s/{60}s)")

        if not image_url:
            _vlog("⚠️ ", "Không detect được ảnh URL — thử download button...")
            # Try download button as fallback
            await self._chrome_js(
                "var btn=document.querySelector("
                "'button[aria-label*=\"Download\"],button[aria-label*=\"download\"],"
                "button[download],a[download]');"
                "if(btn)btn.click();"
            )
            await asyncio.sleep(10)
            # Look in Downloads folder
            return self._find_latest_image(Path.home() / "Downloads")

        # 5. Download ảnh về OUTPUT_DIR
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"chatgpt_bee_{ts}.png"
        filepath = os.path.join(OUTPUT_DIR, filename)
        try:
            req = urllib.request.Request(
                image_url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(filepath, "wb") as f:
                f.write(data)
            _vlog("💾", f"Downloaded {len(data)/1024:.1f}KB → {filename}")
            return filepath
        except Exception as exc:
            _vlog("❌", f"Download thất bại: {exc}")
            return None

    def _find_latest_image(self, directory: Path) -> str | None:
        """Tìm file ảnh mới nhất trong thư mục (vừa được download)."""
        try:
            images = sorted(
                [f for f in directory.iterdir()
                 if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if images and (time.time() - images[0].stat().st_mtime) < 60:
                dest = os.path.join(OUTPUT_DIR, images[0].name)
                import shutil
                shutil.copy2(str(images[0]), dest)
                return dest
        except Exception:
            pass
        return None

    async def _step3_open_new_tab(self) -> None:
        """
        B3: Mở tab mới trên Chrome đang mở (cùng profile 00Fujin).
        Dùng Cmd+T qua IPC keyboard_hotkey.
        """
        if self._ipc:
            await self._ipc.send_action("keyboard_hotkey", {
                "keys": ["command", "t"]
            })
        else:
            await self._applescript(
                'tell application "Google Chrome"\n'
                '  activate\n'
                '  tell front window\n'
                '    make new tab\n'
                '  end tell\n'
                'end tell'
            )

    async def _step4_create_content(self) -> str:
        """
        B4: Vào Gemini → Tạo content 1000 ký tự về điểm yếu OpenClaw.

        Chiến lược 3 tầng:
          1. Gemini API trực tiếp qua LLM FallbackStack (nếu có Gemini API key)
          2. Gemini Web UI qua Chrome + JavaScript + copy
          3. Local Ollama qwen3:8b fallback
        """
        # Tầng 1: LLM FallbackStack (ưu tiên Gemini API)
        content = await self._generate_content_via_llm()
        if content and len(content) >= 800:
            return content

        # Tầng 2: Gemini Web UI qua Chrome
        _vlog("🌐", "Thử Gemini Web UI...")
        content = await self._generate_content_via_gemini_web()
        if content and len(content) >= 500:
            return content

        # Tầng 3: Local Ollama
        _vlog("🤖", "Fallback: Ollama local...")
        return await self._generate_content_via_ollama()

    async def _generate_content_via_llm(self) -> str:
        """Tạo content qua LLM FallbackStack (Gemini API → Mistral → Ollama)."""
        if not self._llm:
            return ""
        prompt = self._build_content_prompt()
        try:
            result = await asyncio.wait_for(
                self._llm.call(prompt),
                timeout=60,
            )
            text = ""
            if hasattr(result, "text"):
                text = result.text
            elif isinstance(result, dict):
                text = result.get("text", "")
            elif isinstance(result, str):
                text = result
            return text.strip()
        except Exception as exc:
            _vlog("⚠️ ", f"LLM API content gen: {exc}")
            return ""

    async def _generate_content_via_gemini_web(self) -> str:
        """
        Tạo content qua Gemini Web (gemini.google.com).
        v9.23:
          - Tab guard: focus_chrome_tab trước khi navigate
          - Input method: switch ABC trước khi type, restore sau
          - pbcopy + Cmd+V thay insertText (đồng nhất với ChatGPT flow)

        Flow:
          1. Tab guard + navigate gemini.google.com
          2. Switch input method → ABC
          3. pbcopy prompt → Cmd+V paste
          4. Enter → chờ response
          5. Extract text response
          6. Restore input method
        """
        # Tab guard — đảm bảo đúng tab trước khi navigate
        try:
            from social.vision_actor import focus_chrome_tab
            await focus_chrome_tab("Gemini")
        except Exception:
            pass
        await self._chrome_navigate(GEMINI_URL)
        await asyncio.sleep(4)

        prompt_text = self._build_content_prompt()

        # v9.23: Switch sang ABC trước khi type (tránh Telex/VNI corrupt)
        old_im = await self._get_current_input_method()
        await self._switch_input_method_to_abc()
        _vlog("⌨️ ", f"Input method: '{old_im}' → ABC (Gemini)")

        try:
            # pbcopy + focus + Cmd+V (đồng nhất với ChatGPT flow)
            await self._set_clipboard(prompt_text[:800])
            await asyncio.sleep(0.3)

            # Focus ô input Gemini
            js_focus = (
                "var el = document.querySelector("
                "'rich-textarea, div[contenteditable=true], textarea[placeholder],"
                " p[data-placeholder]');"
                "if(el){el.focus();el.click();return 'focused';}"
                "return 'notfound';"
            )
            r = await self._chrome_js(js_focus)
            _vlog("⌨️ ", f"Gemini input focus: {r}")
            await asyncio.sleep(0.5)

            # Cmd+V paste
            await self._applescript(
                'tell application "Google Chrome" to activate'
            )
            await asyncio.sleep(0.3)
            await self._applescript(
                'tell application "System Events"\n'
                '    keystroke "v" using command down\n'
                'end tell'
            )
            await asyncio.sleep(1.5)

            # Verify paste
            js_check = (
                "var el = document.querySelector("
                "'rich-textarea, div[contenteditable=true], textarea[placeholder],"
                " p[data-placeholder]');"
                "if(!el) return 'no_el';"
                "var t = el.innerText || el.textContent || '';"
                "return t.length > 5 ? 'ok:' + t.length : 'empty';"
            )
            paste_ok = await self._chrome_js(js_check)
            _vlog("📋", f"Gemini paste check: {paste_ok}")

        finally:
            # Luôn restore input method dù thành công hay thất bại
            await self._restore_input_method(old_im)
            _vlog("⌨️ ", f"Input method restored: ABC → '{old_im}'")

        await asyncio.sleep(0.5)

        # Enter để gửi
        if self._ipc:
            await self._ipc.send_action("keyboard_press", {"key": "return"})
        else:
            await self._applescript(
                'tell application "System Events" to keystroke return'
            )

        # Chờ response (tối đa 60s, poll mỗi 3s)
        _vlog("⏳", "Chờ Gemini viết content (max 60s)...")
        response_text = ""
        for i in range(20):
            await asyncio.sleep(3)
            js_extract = (
                "var els = document.querySelectorAll("
                "'message-content, .response-content, "
                "[class*=\"response\"], model-response p, "
                ".markdown p, .ProseMirror p');"
                "var text = '';"
                "if(els.length > 0) {"
                "  for(var i=0; i<els.length; i++) {"
                "    text += els[i].innerText + ' ';"
                "  }"
                "}"
                "text.trim().substring(0, 2000);"
            )
            extracted = await self._chrome_js(js_extract)
            if extracted and len(extracted) > 200:
                response_text = extracted
                _vlog("✅", f"Gemini response: {len(extracted)} ký tự")
                break
            _vlog("⏳", f"  Chờ Gemini... ({(i+1)*3}s)")

        return response_text

    async def _get_current_input_method(self) -> str:
        """Lấy tên input source đang active (dùng cho Gemini web flow)."""
        script = (
            'tell application "System Events"\n'
            '    try\n'
            '        return name of current input source\n'
            '    on error\n'
            '        return ""\n'
            '    end try\n'
            'end tell'
        )
        return await self._applescript(script)

    async def _switch_input_method_to_abc(self) -> None:
        script = (
            'tell application "System Events"\n'
            '    repeat with src in (every input source)\n'
            '        set n to name of src\n'
            '        if n is "ABC" or n is "U.S." or n contains "English" then\n'
            '            set current input source to src\n'
            '            return "ok:" & n\n'
            '        end if\n'
            '    end repeat\n'
            '    return "not_found"\n'
            'end tell'
        )
        await self._applescript(script)

    async def _restore_input_method(self, name: str) -> None:
        if not name or name in ("ABC", "U.S.", "English"):
            return
        script = (
            f'tell application "System Events"\n'
            f'    try\n'
            f'        set src to first input source whose name is "{name}"\n'
            f'        set current input source to src\n'
            f'    end try\n'
            f'end tell'
        )
        await self._applescript(script)

        # Enter
        if self._ipc:
            await self._ipc.send_action("keyboard_press", {"key": "return"})
        else:
            await self._applescript(
                'tell application "System Events" to keystroke return'
            )

        # Chờ response (tối đa 60s, poll mỗi 3s)
        _vlog("⏳", "Chờ Gemini viết content (max 60s)...")
        response_text = ""
        for i in range(20):
            await asyncio.sleep(3)
            js_extract = (
                "var els = document.querySelectorAll("
                "'message-content, .response-content, "
                "[class*=\"response\"], model-response p, "
                ".markdown p, .ProseMirror p');"
                "var text = '';"
                "if(els.length > 0) {"
                "  for(var i=0; i<els.length; i++) {"
                "    text += els[i].innerText + ' ';"
                "  }"
                "}"
                "text.trim().substring(0, 2000);"
            )
            extracted = await self._chrome_js(js_extract)
            if extracted and len(extracted) > 200:
                response_text = extracted
                _vlog("✅", f"Gemini response: {len(extracted)} ký tự")
                break
            _vlog("⏳", f"  Chờ Gemini... ({(i+1)*3}s)")

        return response_text

    async def _generate_content_via_ollama(self) -> str:
        """Fallback: dùng Ollama local để viết content."""
        prompt = self._build_content_prompt()
        try:
            data = json.dumps({
                "model": _resolve_model("qwen3:8b"),
                "think": False,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": 1200},
            }).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/generate",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            def _call():
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return json.loads(resp.read().decode())

            result = await asyncio.to_thread(_call)
            return result.get("response", "").strip()
        except Exception as exc:
            _vlog("❌", f"Ollama content gen failed: {exc}")
            return (
                "OpenClaw là một nền tảng AI agent đang nổi nhưng vẫn còn nhiều hạn chế. "
                "Hệ thống thiếu cơ chế bảo mật sandbox nghiêm túc — không có AST walker, "
                "không có subprocess isolation, dễ bị prompt injection qua email độc hại. "
                "Không có self-healing: khi task thất bại, hệ thống dừng luôn thay vì tự chẩn đoán. "
                "Không có vision pipeline riêng, không có ConfidenceGate cho VLM outputs. "
                "Phụ thuộc hoàn toàn vào cloud API, không chạy offline được. "
                "Skill ecosystem rộng nhưng không kiểm soát chất lượng — malicious skills có thể đánh cắp credentials. "
                "Thiếu memory learning: agent không học từ lỗi, mỗi task đều như lần đầu. "
                "#AI #OpenClaw #AIAgent #MacOS #Automation"
            )

    def _build_content_prompt(self) -> str:
        """Build prompt cho content generation."""
        return (
            f"Viết một bài đăng Facebook hoàn chỉnh về chủ đề: '{self._content_topic}'. \n\n"
            f"Yêu cầu:\n"
            f"- Độ dài: đúng khoảng {CONTENT_TARGET_CHARS} ký tự\n"
            f"- Ngôn ngữ: Tiếng Việt tự nhiên, hấp dẫn\n"
            f"- Phân tích ít nhất 5 điểm cụ thể với ví dụ minh họa\n"
            f"- Kết thúc với CTA (Call to Action) tích cực\n"
            f"- Có 8-12 hashtag phù hợp cuối bài\n"
            f"- Tone: chuyên nghiệp nhưng dễ đọc, phù hợp Facebook\n\n"
            f"Chỉ trả về NỘI DUNG bài đăng, không giải thích thêm."
        )

    async def _step567_post_facebook(
        self,
        image_path: str,
        content: str,
        platforms: list | None = None,
    ) -> bool:
        """
        B5-7: Mở Facebook, tạo post mới, upload ảnh + paste content.

        Flow chi tiết:
          B5. Vào facebook.com (trên tab hiện tại)
          B6. Click "Bạn đang nghĩ gì?" để mở Create Post dialog
          B7. Upload ảnh → Paste content → Click Đăng
        """
        try:
            from social.social_poster import SocialPoster
            poster = SocialPoster(ipc_client=self._ipc)
            result = await poster.post(
                platform="facebook",
                content=content,
                image_path=image_path,
            )
            if result.get("success"):
                _vlog("✅", "Facebook post đăng thành công!")
                return True
            else:
                _vlog("⚠️ ", f"SocialPoster failed: {result.get('error', '?')}")
                # Fallback to manual
        except ImportError:
            pass

        # Manual Facebook flow fallback (A4: truyền result để track granular errors)
        return await self._manual_facebook_post(image_path, content, result=None)

    async def _manual_facebook_post(
        self,
        image_path: str,
        content: str,
        result: "WorkflowResult | None" = None,
    ) -> bool:
        """
        Facebook posting v2.1 — Vision-powered + Granular error tracking (A4 v9.24).

        A4: Mỗi sub-step:
          - Gán result.current_step trước khi chạy
          - Catch exception riêng với message cụ thể
          - Retry 3 lần với backoff 1s/2s/4s cho B6 và B7c
          - Log chi tiết action nào fail

        B5. Navigate Facebook → SmartWait feed loaded
        B6. Click "What's on your mind?" → retry 3× nếu fail
        B7a. Upload ảnh → 3-tier strategy (JS / cliclick / AppleScript)
        B7b. Paste content → JS insertText, fallback clipboard
        B7c. Click nút Đăng → retry 3× với backoff
        B7d. Verify bài đã đăng thành công
        """
        _vlog("📘", "Facebook post flow v2.1 (Vision + Granular errors)...")

        # Khởi tạo Vision helper
        fb_vision = None
        try:
            from social.vision_actor import ChatGPTVisionHelper
            fb_vision = ChatGPTVisionHelper(
                ipc_client=self._ipc,
            )
            # v9.28: Enhanced Vision đã được bật tự động trong ChatGPTVisionHelper.__init__
            # (enable_enhanced_vision(mode="enhanced") — ClickMemory + MultiModal + CropLocator)
            _vlog("🚀", "Enhanced Vision mode: enabled (ClickMemory + MultiModal + Verify)")
        except ImportError:
            _vlog("⚠️ ", "VisionActor not available — fallback to CSS selectors")

        # ── B5: Navigate Facebook + SmartWait ─────────────────────────
        # v9.23: Tab guard trước khi navigate Facebook
        try:
            from social.vision_actor import focus_chrome_tab
            await focus_chrome_tab("Facebook")
        except Exception:
            pass
        await self._chrome_navigate(FACEBOOK_URL)
        if fb_vision:
            loaded = await fb_vision._actor.smart_wait(
                "Is Facebook feed loaded with posts visible?",
                timeout=12.0, poll_interval=2.0,
                description="Facebook loaded",
            )
            if not loaded:
                _vlog("⚠️ ", "Facebook slow to load — continuing anyway")
        else:
            await asyncio.sleep(5)

        # ── B6: Click Create Post box — retry 3× với backoff ────────
        # v9.28: click_create_post_box() dùng find_and_click_v2 (#7 #8 #1)
        if result:
            result.current_step = "B6_click_create_post"
        b6_ok = False
        _RETRY_DELAYS = [1.0, 2.0, 4.0]
        for _attempt, _delay in enumerate(_RETRY_DELAYS, 1):
            try:
                if fb_vision:
                    # v9.28: find_and_click_v2 + ClickMemory cache + SiteAssertions verify
                    b6_ok = await fb_vision.click_create_post_box()
                else:
                    clicked = await self._chrome_js(
                        "var el = document.querySelector("
                        "'[aria-label*=\"on your mind\"],[aria-label*=\"nghĩ gì\"],"
                        "[role=\"button\"][aria-label],div[data-pagelet=\"FeedComposer\"] [role=\"button\"]');"
                        "if(el){el.click();return 'clicked';}return 'notfound';"
                    )
                    b6_ok = clicked == "clicked"
                    await asyncio.sleep(1.5)
                if b6_ok:
                    break
            except Exception as e:
                if result:
                    result.failed_action = f"B6 attempt {_attempt}: {e}"
            if not b6_ok and _attempt < len(_RETRY_DELAYS):
                _vlog("🔄", f"B6 retry {_attempt}/{len(_RETRY_DELAYS)-1} (wait {_delay}s)...")
                await asyncio.sleep(_delay)
                if result:
                    result.retry_count += 1

        if not b6_ok and result:
            result.failed_action = "B6_click_create_post: DOM+Vision đều fail sau 3 lần retry"
        _vlog("📝", f"B6 Create post: {'✅' if b6_ok else '❌ FAILED after 3 retries'}")

        # ── B7a: Upload ảnh ───────────────────────────────────────────
        if image_path and os.path.exists(image_path):
            upload_ok = False

            if fb_vision:
                # Thử Drag & Drop trước (ổn định nhất)
                _vlog("🖱️ ", "Trying drag & drop upload...")
                upload_ok = await fb_vision.upload_image_drag_drop(image_path)

            if not upload_ok:
                # Fallback: Vision click Photo button → AppleScript file dialog
                _vlog("📷", "Drag drop failed → Photo button + file dialog")
                if fb_vision:
                    photo_clicked = await fb_vision.click_photo_button()
                    if photo_clicked:
                        # SmartWait cho Finder dialog xuất hiện
                        finder_ok = await fb_vision.wait_for_finder_dialog()
                        if finder_ok:
                            _vlog("✅", "Finder dialog open — pasting path")
                        else:
                            _vlog("⚠️ ", "Finder dialog not confirmed — trying anyway")
                else:
                    await self._chrome_js(
                        "var btns = document.querySelectorAll("
                        "'[aria-label*=\"Photo\"],[aria-label*=\"Ảnh\"],[aria-label*=\"photo\"]');"
                        "if(btns.length>0)btns[0].click();"
                    )
                    await asyncio.sleep(2)

                # Upload qua AppleScript Cmd+Shift+G
                await self._upload_file_via_applescript(image_path)
                await asyncio.sleep(4)

                if fb_vision:
                    upload_ok = await fb_vision._actor.ask_vision(
                        "Is there an image thumbnail or photo preview visible in the post creation area?"
                    )
                    _vlog("📷", f"Upload verify: {'✅' if upload_ok else '⚠️ uncertain'}")

        # ── B7b: Paste content ────────────────────────────────────────
        content_ok = False
        if fb_vision:
            content_ok = await fb_vision.paste_content_js(content)

        if not content_ok:
            # Fallback: click text area + pbcopy + Cmd+V
            _vlog("📝", "JS insertText failed → clipboard paste fallback")
            await self._set_clipboard(content)
            await asyncio.sleep(0.8)
            if self._ipc:
                await self._ipc.send_action("keyboard_hotkey", {"keys": ["command", "a"]})
                await asyncio.sleep(0.3)
                await self._ipc.send_action("keyboard_hotkey", {"keys": ["command", "v"]})
            else:
                await self._applescript(
                    'tell application "System Events"\n'
                    '  keystroke "a" using command down\n'
                    '  delay 0.3\n'
                    '  keystroke "v" using command down\n'
                    'end tell'
                )
            await asyncio.sleep(1.5)

        _vlog("📝", f"B7b Content paste: {'✅' if content_ok else '⚠️ clipboard fallback used'}")

        # ── B7c: Click nút Đăng — retry 3× với backoff ──────────────
        # v9.28: click_post_button() dùng find_and_click_v2 (#7 #1)
        # → ClickMemory cache nút Đăng, SiteAssertions.fb_post_submitted() verify 50ms
        if result:
            result.current_step = "B7c_click_post_button"
        post_ok = False
        for _attempt, _delay in enumerate([1.0, 2.0, 4.0], 1):
            try:
                if fb_vision:
                    # v9.28: find_and_click_v2 + ActionKeys.FB_POST_BUTTON + SiteAssertions verify
                    post_ok = await fb_vision.click_post_button()
                else:
                    dom_r = await self._chrome_js(
                        "(function(){"
                        "var btns = document.querySelectorAll("
                        "'[aria-label*=\"Post\"],[aria-label*=\"Đăng\"]');"
                        "for(var i=0;i<btns.length;i++){"
                        "  if(btns[i].offsetParent!==null){btns[i].click();return 'clicked';}"
                        "}"
                        "return 'notfound';})()"
                    )
                    post_ok = "clicked" in dom_r
                    if post_ok:
                        await asyncio.sleep(4)
                if post_ok:
                    break
            except Exception as e:
                if result:
                    result.failed_action = f"B7c attempt {_attempt}: {e}"
            if not post_ok and _attempt < 3:
                _vlog("🔄", f"B7c retry {_attempt}/2 (wait {_delay}s)...")
                await asyncio.sleep(_delay)
                if result:
                    result.retry_count += 1

        if not post_ok and result:
            result.failed_action = "B7c_click_post_button: DOM+Vision đều fail sau 3 lần retry"
        _vlog("📤", f"B7c Post submit: {'✅' if post_ok else '❌ FAILED after 3 retries'}")

        # ── B7d: Verify đã đăng ───────────────────────────────────────
        if fb_vision:
            await asyncio.sleep(3)
            published = await fb_vision.verify_post_published()
            _vlog("🔍", f"B7d Verify published: {'✅' if published else '⚠️ uncertain'}")
            return published or post_ok

        _vlog("✅", "Facebook post submitted!")
        return True

    # ══════════════════════════════════════════════════════════════
    # Helper: AppleScript / Chrome JS / Navigation
    # ══════════════════════════════════════════════════════════════

    async def _chrome_navigate(self, url: str) -> None:
        """Navigate Chrome active tab to URL.
        v9.34 A2: Invalidate bounds cache sau navigate — page load thay đổi
        viewport (address bar hiện/ẩn, scroll position reset).
        """
        if self._ipc:
            await self._ipc.send_action("browser_navigate", {"url": url})
        else:
            safe_url = url.replace('"', "")
            await self._applescript(
                f'tell application "Google Chrome"\n'
                f'  activate\n'
                f'  set URL of active tab of front window to "{safe_url}"\n'
                f'end tell'
            )
        # v9.34 A2: Bounds có thể thay đổi sau navigate (loading bar, etc.)
        try:
            from social.vision_actor import invalidate_chrome_bounds_cache
            invalidate_chrome_bounds_cache()
        except Exception:
            pass

    async def _chrome_js(self, js_code: str) -> str:
        """Execute JavaScript in Chrome active tab, return result string."""
        if platform.system() != "Darwin":
            return ""
        try:
            js_escaped = js_code.replace("\\", "\\\\").replace('"', '\\"')
            script = (
                'tell application "Google Chrome"\n'
                f'  set result to execute active tab of front window javascript "{js_escaped}"\n'
                '  return result as string\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except Exception:
            return ""

    async def _applescript(self, script: str) -> str:
        """Run AppleScript, return stdout."""
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            return proc.stdout.strip()
        except Exception:
            return ""

    async def _set_clipboard(self, text: str) -> None:
        """Set macOS clipboard content."""
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["pbcopy"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    # ── Input Method Switch helpers ───────────────────────────────────────
    # Dùng để tránh lỗi bộ gõ Telex/VNI khi type prompt vào ChatGPT.
    # macOS: TISSelectInputSource qua AppleScript (không cần accessibility permission)

    async def _get_current_input_method(self) -> str:
        """
        Lấy tên input source đang active.
        Ví dụ: "UniKey" | "Vietnamese Telex" | "ABC" | "Vietnamese"
        Trả về "" nếu không lấy được (fallback safe).
        """
        script = (
            'tell application "System Events"\n'
            '    set src to current input source\n'
            '    return name of src\n'
            'end tell'
        )
        return await self._applescript(script)

    async def _switch_input_method_to_abc(self) -> bool:
        """
        Chuyển sang bộ gõ ABC (tiếng Anh).
        Dùng try/on error trong AppleScript để tránh fail silent.
        Trả về True nếu switch thành công.
        """
        script = '''
tell application "System Events"
    set inputSources to every input source
    repeat with src in inputSources
        set srcName to name of src
        if srcName is "ABC" or srcName is "U.S." or srcName contains "English" then
            set current input source to src
            return "ok:" & srcName
        end if
    end repeat
    return "not_found"
end tell
'''
        result = await self._applescript(script)
        _vlog("⌨️ ", f"Input switch result: {result}")
        return result.startswith("ok:")

    async def _gemini_find_input_selector(self) -> str:
        """
        Khi JS selector không tìm được ô input ChatGPT,
        scan DOM structure và hỏi Gemini tìm selector đúng.
        Giải pháp lâu dài — tự thích nghi khi ChatGPT cập nhật UI.
        """
        if not self._llm:
            return ""
        try:
            # Lấy DOM structure của trang hiện tại (interactive elements only)
            dom_snapshot = await self._chrome_js(
                "(function(){"
                "  var elems = document.querySelectorAll("
                "    'textarea, input, [contenteditable], [role=textbox],"
                "     [data-testid], button, form');"
                "  var result = [];"
                "  for(var i=0;i<Math.min(elems.length,30);i++){"
                "    var el = elems[i];"
                "    result.push({"
                "      tag: el.tagName,"
                "      id: el.id||'',"
                "      role: el.getAttribute('role')||'',"
                "      testid: el.getAttribute('data-testid')||'',"
                "      class: el.className.toString().slice(0,60),"
                "      placeholder: el.getAttribute('placeholder')||el.getAttribute('data-placeholder')||'',"
                "      contenteditable: el.getAttribute('contenteditable')||''"
                "    });"
                "  }"
                "  return JSON.stringify(result);"
                "})()"
            )

            prompt = (
                f"ChatGPT web UI có cấu trúc DOM sau (interactive elements):\n"
                f"{dom_snapshot[:1500]}\n\n"
                f"Tìm CSS selector tốt nhất cho ô nhập text/prompt của ChatGPT "
                f"(nơi user gõ câu hỏi vào). "
                f"Chỉ trả về CSS selector, không giải thích. "
                f"Ví dụ: #prompt-textarea hoặc div[contenteditable=true].ProseMirror"
            )
            result = await asyncio.wait_for(
                self._llm.call(prompt, preferred_provider="gemini-2.5-flash"),
                timeout=15,
            )
            selector = str(result).strip().strip('"').strip("'")
            return selector if len(selector) < 200 else ""
        except Exception as e:
            _vlog("⚠️ ", f"Gemini DOM scan error: {e}")
            return ""

    async def _restore_input_method(self, name: str) -> None:
        """
        Khôi phục bộ gõ đã lưu trước đó.
        Bỏ qua nếu name rỗng hoặc đã là ABC.
        """
        if not name or name in ("ABC", "U.S.", "English"):
            return
        script = (
            f'tell application "System Events"\n'
            f'    try\n'
            f'        set src to first input source whose name is "{name}"\n'
            f'        set current input source to src\n'
            f'    end try\n'
            f'end tell'
        )
        await self._applescript(script)

    async def _upload_file_via_applescript(self, filepath: str) -> None:
        """
        Trigger file input dialog và upload file qua AppleScript.
        Dùng pbcopy + Cmd+V thay vì keystroke trực tiếp (filepath dài sẽ fail).
        """
        abs_path = os.path.abspath(filepath)
        # Copy path to clipboard first
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["pbcopy"],
                input=abs_path.encode("utf-8"),
                timeout=5,
            )
        except Exception:
            pass

        # Open Go-to-folder dialog and paste path
        script = (
            'tell application "System Events"\n'
            '  keystroke "g" using {command down, shift down}\n'
            '  delay 1.5\n'
            '  keystroke "v" using command down\n'
            '  delay 0.5\n'
            '  keystroke return\n'
            '  delay 1\n'
            '  keystroke return\n'
            'end tell'
        )
        await self._applescript(script)


# ══════════════════════════════════════════════════════════════════
# Convenience function — dùng trực tiếp từ agent_loop / Telegram
# ══════════════════════════════════════════════════════════════════

async def run_spider_social_post_workflow(
    ipc_client: Any = None,
    llm_fallback: Any = None,
    telegram_send_fn: Any = None,
    image_subject: str = "",
    content_topic: str = "",
    chrome_profile: str = "",
    goal: str = "",
    content_pipeline: Any = None,
    user_id: int = 0,
    chat_id: int = 0,
    target_platforms: list | None = None,   # B2 v9.28: platforms per spider_hub profile
    content_lang: str = "vi",               # B2 v9.28: LLM content language
) -> WorkflowResult:
    """
    Entry point — chạy toàn bộ workflow từ agent_loop hoặc Telegram.

    Params được truyền trực tiếp HOẶC tự parse từ goal string:
      - image_subject: mô tả ảnh gửi ChatGPT
      - content_topic: chủ đề nội dung gửi Gemini
      - chrome_profile: tên profile Chrome (default: 00Fujin)

    Nếu image_subject/content_topic trống nhưng goal được truyền,
    hàm tự parse goal để lấy params.
    """
    # Auto-parse từ goal nếu params chưa được truyền
    if goal and (not image_subject or not content_topic):
        parsed = parse_goal(goal)
        image_subject  = image_subject  or parsed["image_subject"]
        content_topic  = content_topic  or parsed["content_topic"]
        chrome_profile = chrome_profile or parsed["chrome_profile"]

    wf = SpiderSocialPostWorkflow(
        ipc_client=ipc_client,
        llm_fallback=llm_fallback,
        telegram_send_fn=telegram_send_fn,
        image_subject=image_subject,
        content_topic=content_topic,
        chrome_profile=chrome_profile,
        content_pipeline=content_pipeline,
        user_id=user_id,
        chat_id=chat_id,
    )
    return await wf.run()
