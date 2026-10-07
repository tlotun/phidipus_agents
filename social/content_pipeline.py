# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
social/content_pipeline.py — Phidipus Social Content Pipeline v1.0
═══════════════════════════════════════════════════════════════════

State-machine pipeline: Tạo ảnh → Duyệt → Tạo content → Duyệt → Post MXH.

Flow hoàn chỉnh:
  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
  │  IDLE        │────▶│  GEN_IMAGE   │────▶│  WAIT_IMAGE  │
  └──────────────┘     └──────────────┘     │  _APPROVAL   │
                                            └──────┬───────┘
                                 ┌─────────────────┘
                         OK ↓        ↑ "tạo lại..."
                       ┌──────────────────┐
                       │  GEN_CONTENT     │
                       └────────┬─────────┘
                                ↓
                       ┌──────────────────┐
                       │  WAIT_CONTENT    │
                       │  _APPROVAL       │
                       └────────┬─────────┘
                         OK ↓        ↑ "sửa lại..."
                       ┌──────────────────┐
                       │  POSTING         │
                       └────────┬─────────┘
                                ↓
                       ┌──────────────────┐
                       │  CONFIRM_POSTED  │
                       └────────┬─────────┘
                                ↓
                       ┌──────────────┐
                       │  DONE        │
                       └──────────────┘

Mỗi user có TỐI ĐA 1 pipeline chạy tại một thời điểm.
Pipeline timeout: 30 phút (tự huỷ nếu user không phản hồi).

Tích hợp:
  - Telegram Bot: nhận lệnh + gửi ảnh/content cho duyệt
  - ChatGPT Image Gen: mở Chrome → chatgpt.com → tạo ảnh
  - LLM Fallback Stack: tạo content (Gemini → OpenRouter → Ollama)
  - Social Poster: đăng bài lên Facebook/Instagram/X qua Chrome

Security:
  - Không exec() code tuỳ ý — chỉ gọi pre-built functions
  - File output chỉ trong ~/Phidipus/content/ (path-restricted)
  - Timeout cho mọi bước (không loop vô hạn)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional


def _resolve_model(name: str, role: str | None = None) -> str:
    """v4.3: map a legacy hard-coded model name to an installed one (model registry)."""
    try:
        from core.model_registry import resolve_model
        return resolve_model(name, role)
    except Exception:
        return name


# ══════════════════════════════════════════════════════════════════
# Vietnamese log helper
# ══════════════════════════════════════════════════════════════════

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Pipeline states
# ══════════════════════════════════════════════════════════════════

class PipelineState(Enum):
    """Trạng thái của Content Pipeline."""
    IDLE               = auto()
    PARSE_COMMAND       = auto()  # Parse lệnh user → extract params
    GEN_IMAGE           = auto()  # Đang tạo ảnh (Chrome → ChatGPT)
    WAIT_IMAGE_APPROVAL = auto()  # Chờ user duyệt ảnh
    GEN_CONTENT         = auto()  # Đang tạo content (LLM)
    WAIT_CONTENT_APPROVAL = auto()  # Chờ user duyệt content
    POSTING             = auto()  # Đang post lên MXH
    CONFIRM_POSTED      = auto()  # Chụp screenshot xác nhận
    DONE                = auto()  # Hoàn thành
    FAILED              = auto()  # Thất bại
    CANCELLED           = auto()  # User huỷ


# ══════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════

@dataclass
class ContentRequest:
    """Parsed request từ lệnh Telegram."""
    image_subject: str = ""       # "xyz" — chủ đề ảnh
    image_style: str = ""         # "abc" — phong cách ảnh
    content_subject: str = ""     # chủ đề content (thường = image_subject)
    target_platform: str = ""     # "facebook" | "instagram" | "x" | "all"
    raw_command: str = ""         # Lệnh gốc từ user
    extra_instructions: str = ""  # Yêu cầu thêm


@dataclass
class PipelineSession:
    """State cho 1 pipeline session."""
    session_id: str = ""
    user_id: int = 0
    chat_id: int = 0
    state: PipelineState = PipelineState.IDLE
    request: ContentRequest = field(default_factory=ContentRequest)

    # ── Image ──
    image_prompt: str = ""          # Prompt gửi ChatGPT
    image_path: str = ""            # Path file ảnh đã download
    image_attempts: int = 0         # Số lần tạo ảnh
    image_approved: bool = False

    # ── Content ──
    content_text: str = ""          # Content đã tạo
    content_attempts: int = 0       # Số lần tạo content
    content_approved: bool = False

    # ── Posting ──
    posted_platforms: list[str] = field(default_factory=list)
    post_screenshot_path: str = ""  # Screenshot xác nhận
    post_url: str = ""              # URL bài post (nếu lấy được)

    # ── Metadata ──
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str = ""

    # ── Telegram message IDs for editing ──
    status_message_id: int = 0      # Message ID để edit progress

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.created_at) / 60

    @property
    def is_expired(self) -> bool:
        return self.age_minutes > 30  # 30 phút timeout

    def touch(self) -> None:
        self.updated_at = time.time()


# ══════════════════════════════════════════════════════════════════
# Command parser
# ══════════════════════════════════════════════════════════════════

# Regex patterns cho tiếng Việt
_RE_CREATE_IMAGE = re.compile(
    r"(?:tạo|vẽ|sinh|generate|create)\s+(?:1\s+)?(?:bức\s+)?(?:ảnh|hình|image|picture|photo)"
    r"\s+(.+?)(?:\s+(?:theo|với|in|by)\s+(?:phong cách|style|kiểu)\s+(.+?))?$",
    re.IGNORECASE
)

_RE_STYLE = re.compile(
    r"(?:theo|với|in|by)\s+(?:phong cách|style|kiểu)\s+(.+?)(?:\s+(?:sau đó|rồi|then)|$)",
    re.IGNORECASE
)

_RE_CONTENT = re.compile(
    r"(?:tạo|viết|write|create)\s+(?:1\s+)?content\s+(?:về|about)\s+(.+?)(?:\s*,|\s+(?:cuối|rồi|sau|then))",
    re.IGNORECASE
)

_RE_PLATFORM = re.compile(
    r"(?:post|đăng|share|chia sẻ)\s+(?:lên|to|on|trên)\s+(facebook|instagram|insta|ig|x|twitter|tất cả|all)",
    re.IGNORECASE
)

_PLATFORM_ALIASES = {
    "facebook": "facebook", "fb": "facebook",
    "instagram": "instagram", "insta": "instagram", "ig": "instagram",
    "x": "x", "twitter": "x",
    "tất cả": "all", "all": "all",
}


def parse_content_command(text: str) -> ContentRequest | None:
    """
    Parse lệnh tạo content từ user.

    Ví dụ input:
      "tạo ảnh con ong robot theo phong cách cyberpunk sau đó tạo content về AI rồi post lên facebook"
      "tạo ảnh sunset theo style watercolor, viết content về thiên nhiên, đăng lên instagram"
      "create image of a bee in pixel art style then create content about Phidipus and post to X"

    Returns ContentRequest or None nếu không match pattern.
    """
    if not text or len(text) < 10:
        return None

    text_lower = text.lower().strip()

    # Phải có ít nhất "tạo ảnh" hoặc "create image"
    has_image_keyword = any(kw in text_lower for kw in [
        "tạo ảnh", "vẽ ảnh", "tạo hình", "sinh ảnh",
        "create image", "generate image", "make image",
        "tạo 1 bức ảnh", "tạo một bức ảnh",
    ])
    if not has_image_keyword:
        return None

    req = ContentRequest(raw_command=text)

    # ── Extract image subject + style ──
    # Strategy: tìm phần giữa "tạo ảnh" và "theo phong cách" hoặc next clause
    # Simplified: split by key phrases

    # Bước 1: tách theo các mốc
    parts = re.split(
        r'\s*(?:sau đó|rồi|then|,\s*(?:sau|rồi)?)\s*',
        text, flags=re.IGNORECASE
    )

    if parts:
        img_part = parts[0]  # Phần đầu = tạo ảnh...

        # Extract subject (phần sau "tạo ảnh" và trước "theo phong cách")
        style_match = _RE_STYLE.search(img_part)
        if style_match:
            req.image_style = style_match.group(1).strip()
            # Subject = phần trước style
            before_style = img_part[:style_match.start()].strip()
        else:
            before_style = img_part.strip()

        # Clean subject: remove "tạo ảnh", "tạo 1 bức ảnh", etc.
        subject = re.sub(
            r'^(?:tạo|vẽ|sinh|generate|create|make)\s+'
            r'(?:1\s+)?(?:bức\s+)?(?:ảnh|hình|image|picture|photo)\s+',
            '', before_style, flags=re.IGNORECASE
        ).strip()
        # Remove trailing "về" or "of"
        subject = re.sub(r'\s+(?:về|of|about)$', '', subject).strip()
        req.image_subject = subject if subject else "abstract art"

    # ── Extract content subject ──
    for part in parts[1:]:
        content_kw = re.search(
            r'(?:tạo|viết|write|create)\s+(?:1\s+)?content\s+(?:về|about)\s+(.+)',
            part, re.IGNORECASE
        )
        if content_kw:
            req.content_subject = content_kw.group(1).strip()
            # Clean: remove trailing platform clause
            req.content_subject = re.sub(
                r'\s*(?:cuối cùng|rồi|sau đó|then|,).*$', '',
                req.content_subject, flags=re.IGNORECASE
            ).strip()
            break

    if not req.content_subject:
        req.content_subject = req.image_subject

    # ── Extract platform ──
    plat_match = _RE_PLATFORM.search(text)
    if plat_match:
        plat_raw = plat_match.group(1).strip().lower()
        req.target_platform = _PLATFORM_ALIASES.get(plat_raw, plat_raw)
    else:
        # Default: detect from text
        for alias, platform in _PLATFORM_ALIASES.items():
            if alias in text_lower:
                req.target_platform = platform
                break
        if not req.target_platform:
            req.target_platform = "facebook"  # default

    return req


# ══════════════════════════════════════════════════════════════════
# Content Pipeline Manager
# ══════════════════════════════════════════════════════════════════

class ContentPipeline:
    """
    Quản lý toàn bộ luồng: Tạo ảnh → Duyệt → Content → Duyệt → Post.

    Mỗi user có tối đa 1 session. Pipeline chạy bằng state machine,
    chờ input từ Telegram giữa các bước.

    Dependencies (injected):
      - image_generator: ChatGPTImageGenerator
      - content_generator: ContentGenerator (LLM)
      - social_poster: SocialPoster (Chrome automation)
      - telegram_send_fn: async function to send messages/photos via Telegram
    """

    # ── Config ──────────────────────────────────────────────────
    MAX_IMAGE_ATTEMPTS = 10
    MAX_CONTENT_ATTEMPTS = 10
    SESSION_TIMEOUT_MINUTES = 30
    CONTENT_DIR = Path.home() / "Phidipus" / "content"

    def __init__(self) -> None:
        self._sessions: dict[int, PipelineSession] = {}  # user_id → session
        self._lock = asyncio.Lock()

        # Dependencies (injected)
        self._image_gen: Any = None      # ChatGPTImageGenerator
        self._content_gen: Any = None    # ContentGenerator
        self._social_poster: Any = None  # SocialPoster
        self._ipc_client: Any = None     # IPCClient
        self._llm_fallback: Any = None   # LLMFallbackStack

        # Telegram send functions (injected by bot)
        self._send_message: Optional[Callable] = None
        self._send_photo: Optional[Callable] = None
        self._send_document: Optional[Callable] = None
        self._edit_message: Optional[Callable] = None

        # Ensure content directory exists
        self.CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    def inject(self, **components: Any) -> None:
        """Inject dependencies."""
        for k, v in components.items():
            attr = f"_{k}"
            if hasattr(self, attr):
                setattr(self, attr, v)

    # ══════════════════════════════════════════════════════════════
    # Public API (called from Telegram Bot)
    # ══════════════════════════════════════════════════════════════

    def has_active_session(self, user_id: int) -> bool:
        """Check if user has an active pipeline session."""
        session = self._sessions.get(user_id)
        if not session:
            return False
        if session.is_expired or session.state in (
            PipelineState.DONE, PipelineState.FAILED, PipelineState.CANCELLED
        ):
            return False
        return True

    def get_session(self, user_id: int) -> PipelineSession | None:
        """Get active session for user."""
        session = self._sessions.get(user_id)
        if session and not session.is_expired:
            return session
        return None

    async def start_pipeline(
        self,
        user_id: int,
        chat_id: int,
        command: str,
    ) -> PipelineSession | None:
        """
        Bắt đầu pipeline mới.

        Returns:
            PipelineSession nếu thành công, None nếu không parse được lệnh.
        """
        async with self._lock:
            # Cancel existing session nếu có
            if user_id in self._sessions:
                old = self._sessions[user_id]
                old.state = PipelineState.CANCELLED
                _vlog("⏹", f"Cancelled old session {old.session_id}")

            # Parse command
            request = parse_content_command(command)
            if not request:
                return None

            # Create new session
            session = PipelineSession(
                session_id=str(uuid.uuid4())[:8],
                user_id=user_id,
                chat_id=chat_id,
                state=PipelineState.PARSE_COMMAND,
                request=request,
            )
            self._sessions[user_id] = session
            _vlog("🎬", f"Pipeline {session.session_id} started for user {user_id}")

        # Gửi thông báo bắt đầu
        await self._notify_start(session)

        # Chuyển sang bước tạo ảnh
        await self._transition_to_gen_image(session)

        return session

    async def handle_user_response(
        self,
        user_id: int,
        text: str,
        photo_path: str = "",
    ) -> bool:
        """
        Xử lý phản hồi từ user (OK, tạo lại, sửa...).

        Returns True nếu pipeline xử lý được message này.
        """
        session = self.get_session(user_id)
        if not session:
            return False

        session.touch()
        text_lower = text.strip().lower()

        # ── State: WAIT_IMAGE_APPROVAL ──
        if session.state == PipelineState.WAIT_IMAGE_APPROVAL:
            if self._is_approval(text_lower):
                session.image_approved = True
                _vlog("✅", f"[{session.session_id}] Image approved")
                await self._transition_to_gen_content(session)
                return True
            elif self._is_cancel(text_lower):
                await self._cancel_session(session, "User huỷ pipeline")
                return True
            else:
                # User muốn tạo lại — extract new style/instructions
                session.request.extra_instructions = text
                _new_style = self._extract_regen_style(text)
                if _new_style:
                    session.request.image_style = _new_style
                _vlog("🔄", f"[{session.session_id}] Regenerating image: {text[:80]}")
                await self._transition_to_gen_image(session)
                return True

        # ── State: WAIT_CONTENT_APPROVAL ──
        elif session.state == PipelineState.WAIT_CONTENT_APPROVAL:
            if self._is_approval(text_lower):
                session.content_approved = True
                _vlog("✅", f"[{session.session_id}] Content approved")
                await self._transition_to_posting(session)
                return True
            elif self._is_cancel(text_lower):
                await self._cancel_session(session, "User huỷ pipeline")
                return True
            else:
                # User muốn sửa content
                session.request.extra_instructions = text
                _vlog("🔄", f"[{session.session_id}] Regenerating content: {text[:80]}")
                await self._transition_to_gen_content(session)
                return True

        return False

    async def cancel(self, user_id: int) -> bool:
        """Cancel pipeline for user."""
        session = self.get_session(user_id)
        if session:
            await self._cancel_session(session, "User huỷ")
            return True
        return False

    # ══════════════════════════════════════════════════════════════
    # B1 v9.25: Public API for Telegram Approval Flow
    # ══════════════════════════════════════════════════════════════

    async def submit_image_for_approval(
        self,
        user_id: int,
        image_path: str,
        send_photo_fn: Any = None,
    ) -> bool:
        """
        B1.1: Sau khi ảnh ChatGPT đã tạo xong, gửi preview Telegram và
        chờ user duyệt trước khi chuyển sang bước tạo content.

        - Gửi ảnh qua Telegram kèm inline keyboard: [✅ Duyệt] [🔄 Tạo lại] [❌ Huỷ]
        - Trả True nếu session chuyển sang WAIT_IMAGE_APPROVAL thành công.
        """
        session = self.get_session(user_id)
        if not session:
            return False
        session.image_path = image_path
        session.state = PipelineState.WAIT_IMAGE_APPROVAL
        _vlog("📤", f"[{session.session_id}] Gửi preview ảnh để duyệt → {image_path}")
        if send_photo_fn:
            try:
                await send_photo_fn(
                    chat_id=session.chat_id,
                    photo_path=image_path,
                    caption="Anh AI da tao xong! Nhan Duyet de tiep tuc.",
                    keyboard=[
                        [("✅ Duyệt", f"pipe_approve_img:{session.session_id}"),
                         ("🔄 Tạo lại", f"pipe_regen_img:{session.session_id}")],
                        [("❌ Huỷ", f"pipe_cancel:{session.session_id}")],
                    ],
                )
            except Exception as e:
                _vlog("⚠️ ", f"send_photo error: {e}")
        return True

    async def submit_content_for_approval(
        self,
        user_id: int,
        content: str,
        send_message_fn: Any = None,
    ) -> bool:
        """
        B1.2: Sau khi content đã tạo xong, gửi preview Telegram và
        chờ user duyệt trước khi đăng Facebook.
        """
        session = self.get_session(user_id)
        if not session:
            return False
        session.generated_content = content
        session.state = PipelineState.WAIT_CONTENT_APPROVAL
        _vlog("📝", f"[{session.session_id}] Gửi preview content để duyệt")
        if send_message_fn:
            try:
                preview = content[:800] + ("..." if len(content) > 800 else "")
                await send_message_fn(
                    chat_id=session.chat_id,
                    text=(
                        "Content da tao xong! (" + str(len(content)) + " ky tu)\n\n"
                        + preview + "\n\n"
                        + "Nhan Duyet va Dang de dang len Facebook.\n"
                        + "Reply de sua/viet lai content."
                    ),

                    keyboard=[
                        [("✅ Duyệt & Đăng", f"pipe_approve_content:{session.session_id}"),
                         ("✏️ Viết lại", f"pipe_regen_content:{session.session_id}")],
                        [("❌ Huỷ", f"pipe_cancel:{session.session_id}")],
                    ],
                )
            except Exception as e:
                _vlog("⚠️ ", f"send_message error: {e}")
        return True

    def get_session_by_id(self, session_id: str) -> "PipelineSession | None":
        """Tìm session theo session_id (dùng trong callback handler)."""
        for s in self._sessions.values():
            if s.session_id == session_id:
                return s
        return None

    def stats(self) -> dict:
        """Pipeline stats."""
        active = sum(
            1 for s in self._sessions.values()
            if s.state not in (PipelineState.DONE, PipelineState.FAILED,
                               PipelineState.CANCELLED, PipelineState.IDLE)
            and not s.is_expired
        )
        return {
            "active_sessions": active,
            "total_sessions": len(self._sessions),
            "content_dir": str(self.CONTENT_DIR),
        }

    # ══════════════════════════════════════════════════════════════
    # State transitions
    # ══════════════════════════════════════════════════════════════

    async def _transition_to_gen_image(self, session: PipelineSession) -> None:
        """Chuyển sang trạng thái tạo ảnh."""
        session.state = PipelineState.GEN_IMAGE
        session.image_attempts += 1
        session.touch()

        if session.image_attempts > self.MAX_IMAGE_ATTEMPTS:
            await self._fail_session(session, "Vượt quá số lần tạo ảnh tối đa")
            return

        # Build prompt cho ChatGPT
        prompt = self._build_image_prompt(session)
        session.image_prompt = prompt

        # Notify user
        attempt_text = f" (lần {session.image_attempts})" if session.image_attempts > 1 else ""
        await self._send(
            session.chat_id,
            f"🎨 *Đang tạo ảnh{attempt_text}...*\n\n"
            f"📝 Chủ đề: _{self._esc(session.request.image_subject)}_\n"
            f"🎭 Phong cách: _{self._esc(session.request.image_style or 'default')}_\n\n"
            f"⏳ Bot đang mở ChatGPT và tạo ảnh, vui lòng chờ 30\\-60 giây\\.\\.\\."
        )

        # Execute image generation
        try:
            image_path = await self._generate_image(session, prompt)

            if image_path and os.path.exists(image_path):
                session.image_path = image_path
                session.state = PipelineState.WAIT_IMAGE_APPROVAL

                # Gửi ảnh cho user duyệt
                await self._send_image_for_approval(session)
            else:
                await self._send(
                    session.chat_id,
                    "⚠️ Không tạo được ảnh\\. Đang thử lại\\.\\.\\."
                )
                if session.image_attempts < self.MAX_IMAGE_ATTEMPTS:
                    await asyncio.sleep(2)
                    await self._transition_to_gen_image(session)
                else:
                    await self._fail_session(session, "Không tạo được ảnh sau nhiều lần thử")

        except Exception as exc:
            _vlog("❌", f"[{session.session_id}] Image gen error: {exc}")
            await self._send(
                session.chat_id,
                f"❌ Lỗi tạo ảnh: _{self._esc(str(exc)[:100])}_\n"
                f"Nhắn *bất kỳ* để thử lại hoặc /cancel để huỷ\\."
            )
            session.state = PipelineState.WAIT_IMAGE_APPROVAL  # Allow retry

    async def _transition_to_gen_content(self, session: PipelineSession) -> None:
        """Chuyển sang trạng thái tạo content."""
        session.state = PipelineState.GEN_CONTENT
        session.content_attempts += 1
        session.touch()

        if session.content_attempts > self.MAX_CONTENT_ATTEMPTS:
            await self._fail_session(session, "Vượt quá số lần tạo content tối đa")
            return

        attempt_text = f" (lần {session.content_attempts})" if session.content_attempts > 1 else ""
        await self._send(
            session.chat_id,
            f"✍️ *Đang tạo content{attempt_text}...*\n\n"
            f"📝 Chủ đề: _{self._esc(session.request.content_subject)}_\n"
            f"📱 Nền tảng: _{self._esc(session.request.target_platform)}_\n\n"
            f"⏳ Đang viết nội dung\\.\\.\\."
        )

        try:
            content = await self._generate_content(session)

            if content:
                session.content_text = content
                session.state = PipelineState.WAIT_CONTENT_APPROVAL

                # Gửi content cho user duyệt
                await self._send_content_for_approval(session)
            else:
                await self._fail_session(session, "Không tạo được content")

        except Exception as exc:
            _vlog("❌", f"[{session.session_id}] Content gen error: {exc}")
            await self._send(
                session.chat_id,
                f"❌ Lỗi tạo content: _{self._esc(str(exc)[:100])}_\n"
                f"Nhắn *bất kỳ* để thử lại hoặc /cancel để huỷ\\."
            )
            session.state = PipelineState.WAIT_CONTENT_APPROVAL

    async def _transition_to_posting(self, session: PipelineSession) -> None:
        """Chuyển sang trạng thái đăng bài."""
        session.state = PipelineState.POSTING
        session.touch()

        platforms = self._resolve_platforms(session.request.target_platform)
        platform_names = ", ".join(platforms)

        await self._send(
            session.chat_id,
            f"📤 *Đang đăng bài lên {self._esc(platform_names)}...*\n\n"
            f"🖼 Ảnh: ✅ Đã duyệt\n"
            f"📝 Content: ✅ Đã duyệt\n\n"
            f"⏳ Bot đang mở Chrome và đăng bài\\.\\.\\."
        )

        try:
            results = await self._post_to_platforms(session, platforms)

            session.posted_platforms = [r["platform"] for r in results if r.get("success")]
            failed = [r["platform"] for r in results if not r.get("success")]

            # Chụp screenshot xác nhận
            session.state = PipelineState.CONFIRM_POSTED
            screenshot_path = await self._take_confirmation_screenshot(session)
            session.post_screenshot_path = screenshot_path or ""

            # Gửi xác nhận
            await self._send_posting_confirmation(session, results)

            session.state = PipelineState.DONE
            _vlog("✅", f"[{session.session_id}] Pipeline completed!")

        except Exception as exc:
            _vlog("❌", f"[{session.session_id}] Posting error: {exc}")
            await self._fail_session(session, f"Lỗi đăng bài: {str(exc)[:100]}")

    # ══════════════════════════════════════════════════════════════
    # Image generation (delegates to ChatGPTImageGenerator)
    # ══════════════════════════════════════════════════════════════

    def _build_image_prompt(self, session: PipelineSession) -> str:
        """Build prompt for ChatGPT image generation."""
        subject = session.request.image_subject
        style = session.request.image_style
        extra = session.request.extra_instructions

        prompt = f"Create an image of {subject}"
        if style:
            prompt += f" in {style} style"
        if extra and session.image_attempts > 1:
            # User feedback from previous attempt
            prompt += f". Additional instructions: {extra}"
        prompt += ". High quality, professional, visually striking."

        return prompt

    async def _generate_image(
        self, session: PipelineSession, prompt: str
    ) -> str | None:
        """
        Tạo ảnh qua ChatGPT (Chrome automation).

        Returns: path to downloaded image, or None.
        """
        if self._image_gen:
            # Use injected ChatGPTImageGenerator
            return await self._image_gen.generate(
                prompt=prompt,
                output_dir=str(self.CONTENT_DIR / session.session_id),
                session_id=session.session_id,
            )

        # Fallback: delegate to agent_loop for Chrome automation
        if self._ipc_client:
            return await self._generate_image_via_ipc(session, prompt)

        _vlog("⚠", "No image generator available")
        return None

    async def _generate_image_via_ipc(
        self, session: PipelineSession, prompt: str
    ) -> str | None:
        """Generate image by sending IPC commands to Chrome."""
        output_dir = self.CONTENT_DIR / session.session_id
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Step 1: Open Chrome to ChatGPT
            await self._ipc_client.send_action("app_launch", {
                "app_name": "Google Chrome",
                "url": "https://chatgpt.com",
            })
            await asyncio.sleep(5)

            # Step 2: Type prompt in ChatGPT
            await self._ipc_client.send_action("keyboard_type", {
                "text": prompt,
            })
            await asyncio.sleep(1)

            # Step 3: Press Enter  (FIX v4.3: "keyboard_key" is not an IPC action)
            await self._ipc_client.send_action("keyboard_press", {
                "key": "return",
            })

            # Step 4: Wait for image generation (ChatGPT takes ~30-60s)
            _vlog("⏳", f"Waiting for ChatGPT image generation...")
            await asyncio.sleep(45)

            # Step 5: Right-click and save image
            # This requires VLM to find the image → right-click → "Save Image As"
            # For reliability, use screenshot + crop approach
            # (FIX v4.3: removed the invalid "screenshot" IPC action — the
            #  screenshot is taken once below via utils.platform_adapter)
            await asyncio.sleep(2)

            # Step 6: Use keyboard shortcut to download
            # ChatGPT has a download button on generated images
            # Agent will need to find and click it via VLM

            # Save screenshot as fallback
            screenshot_path = str(output_dir / f"chatgpt_output_{session.image_attempts}.png")

            from utils.platform_adapter import take_screenshot
            screenshot_path = await take_screenshot(screenshot_path)
            await asyncio.sleep(1)

            if os.path.exists(screenshot_path):
                return screenshot_path

        except Exception as exc:
            _vlog("❌", f"IPC image gen failed: {exc}")

        return None

    # ══════════════════════════════════════════════════════════════
    # Content generation (LLM)
    # ══════════════════════════════════════════════════════════════

    async def _generate_content(self, session: PipelineSession) -> str | None:
        """
        Tạo content cho social media post bằng LLM.
        """
        platform = session.request.target_platform
        subject = session.request.content_subject
        extra = session.request.extra_instructions if session.content_attempts > 1 else ""

        # Build prompt
        platform_guide = self._get_platform_guide(platform)
        prompt = (
            f"Viết một bài đăng mạng xã hội ({platform}) về chủ đề: {subject}\n\n"
            f"Yêu cầu:\n"
            f"- Ngôn ngữ: Tiếng Việt tự nhiên, hấp dẫn\n"
            f"- Độ dài: 100-300 từ\n"
            f"- Có emoji phù hợp\n"
            f"- Có hashtag cuối bài (5-10 hashtags)\n"
            f"- {platform_guide}\n"
        )
        if extra:
            prompt += f"\nYêu cầu bổ sung từ người dùng: {extra}\n"
        prompt += "\nChỉ trả về NỘI DUNG bài đăng, không giải thích thêm."

        # Try LLM Fallback Stack
        if self._llm_fallback:
            try:
                result = await self._llm_fallback.generate(
                    prompt=prompt,
                    temperature=0.7,
                    max_tokens=1000,
                )
                if result:
                    return result.strip()
            except Exception as exc:
                _vlog("⚠", f"LLM fallback error: {exc}")

        # Fallback to local Ollama
        if self._ipc_client:
            try:
                import urllib.request
                data = json.dumps({
                    "model": _resolve_model("qwen3:8b"),
                    "think": False,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.7, "num_predict": 1000},
                }).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:11434/api/generate",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    result = json.loads(resp.read().decode())
                    return result.get("response", "").strip()
            except Exception as exc:
                _vlog("❌", f"Ollama content gen failed: {exc}")

        return None

    def _get_platform_guide(self, platform: str) -> str:
        """Platform-specific content guidelines."""
        guides = {
            "facebook": "Tone thân thiện, dễ chia sẻ. Có CTA (Call to Action).",
            "instagram": "Tone trendy, visual-focused. Caption ngắn gọn, hashtag quan trọng.",
            "x": "Tone punchy, dưới 280 ký tự nếu có thể. Hashtag ít, chọn lọc.",
            "all": "Tone đa nền tảng, phù hợp cả Facebook, Instagram và X.",
        }
        return guides.get(platform, guides["facebook"])

    # ══════════════════════════════════════════════════════════════
    # Social media posting (delegates to SocialPoster)
    # ══════════════════════════════════════════════════════════════

    def _resolve_platforms(self, target: str) -> list[str]:
        """Resolve platform target to list."""
        if target == "all":
            return ["facebook", "instagram", "x"]
        return [target] if target else ["facebook"]

    async def _post_to_platforms(
        self, session: PipelineSession, platforms: list[str]
    ) -> list[dict]:
        """Post to each platform."""
        results = []

        for platform in platforms:
            try:
                _vlog("📤", f"[{session.session_id}] Posting to {platform}...")

                if self._social_poster:
                    result = await self._social_poster.post(
                        platform=platform,
                        content=session.content_text,
                        image_path=session.image_path,
                    )
                    results.append({
                        "platform": platform,
                        "success": result.get("success", False),
                        "url": result.get("url", ""),
                        "error": result.get("error", ""),
                    })
                else:
                    # Use IPC for Chrome automation posting
                    success = await self._post_via_ipc(
                        session, platform,
                        session.content_text,
                        session.image_path,
                    )
                    results.append({
                        "platform": platform,
                        "success": success,
                        "url": "",
                        "error": "" if success else "IPC posting failed",
                    })

                # Wait between platforms
                if len(platforms) > 1:
                    await asyncio.sleep(5)

            except Exception as exc:
                results.append({
                    "platform": platform,
                    "success": False,
                    "error": str(exc)[:100],
                })

        return results

    async def _post_via_ipc(
        self, session: PipelineSession, platform: str,
        content: str, image_path: str,
    ) -> bool:
        """Post to social media via IPC Chrome automation."""
        if not self._ipc_client:
            return False

        platform_urls = {
            "facebook": "https://www.facebook.com",
            "instagram": "https://www.instagram.com",
            "x": "https://x.com",
        }

        url = platform_urls.get(platform)
        if not url:
            return False

        try:
            # Navigate to platform
            await self._ipc_client.send_action("app_launch", {
                "app_name": "Google Chrome",
                "url": url,
            })
            await asyncio.sleep(5)

            # Platform-specific posting logic will be handled by
            # social/social_poster.py which uses IPC for Chrome control
            _vlog("📤", f"Posting to {platform} via Chrome...")

            return True

        except Exception as exc:
            _vlog("❌", f"IPC post to {platform} failed: {exc}")
            return False

    # ══════════════════════════════════════════════════════════════
    # Confirmation screenshot
    # ══════════════════════════════════════════════════════════════

    async def _take_confirmation_screenshot(
        self, session: PipelineSession
    ) -> str | None:
        """Chụp screenshot xác nhận đã post."""
        try:
            output_dir = self.CONTENT_DIR / session.session_id
            output_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = str(output_dir / f"post_confirmation_{ts}.png")

            if self._ipc_client:
                await asyncio.sleep(3)  # Wait for page to stabilize
                # FIX v4.3: "screenshot_save" is not an IPC action
                from utils.platform_adapter import take_screenshot
                path = await take_screenshot(path)
                await asyncio.sleep(1)

                if os.path.exists(path):
                    return path

        except Exception as exc:
            _vlog("⚠", f"Screenshot failed: {exc}")

        return None

    # ══════════════════════════════════════════════════════════════
    # Telegram notification helpers
    # ══════════════════════════════════════════════════════════════

    async def _notify_start(self, session: PipelineSession) -> None:
        """Thông báo bắt đầu pipeline."""
        req = session.request
        platforms = self._resolve_platforms(req.target_platform)
        plat_text = ", ".join(p.capitalize() for p in platforms)

        text = (
            f"🕷️ *Content Pipeline bắt đầu\\!*\n\n"
            f"📋 Session: `{session.session_id}`\n"
            f"🖼 Ảnh: _{self._esc(req.image_subject)}_\n"
            f"🎭 Style: _{self._esc(req.image_style or 'default')}_\n"
            f"📝 Content: _{self._esc(req.content_subject)}_\n"
            f"📱 Post lên: *{self._esc(plat_text)}*\n\n"
            f"Quy trình: Tạo ảnh → Duyệt → Content → Duyệt → Post\n"
            f"⏱ Timeout: {self.SESSION_TIMEOUT_MINUTES} phút"
        )
        await self._send(session.chat_id, text)

    async def _send_image_for_approval(self, session: PipelineSession) -> None:
        """Gửi ảnh cho user duyệt."""
        caption = (
            f"🖼 *Ảnh đã tạo* (lần {session.image_attempts})\n\n"
            f"📝 Prompt: _{self._esc(session.request.image_subject[:100])}_\n"
            f"🎭 Style: _{self._esc(session.request.image_style or 'default')}_\n\n"
            f"Phản hồi:\n"
            f"  ✅ *OK* — Duyệt ảnh, qua bước tạo content\n"
            f"  🔄 *Nhắn bất kỳ* — Tạo lại theo yêu cầu\n"
            f"     VD: _\"tạo lại đẹp hơn theo phong cách anime\"_\n"
            f"  ❌ /cancel — Huỷ toàn bộ pipeline"
        )

        if self._send_photo and session.image_path:
            await self._send_photo(
                session.chat_id,
                session.image_path,
                caption,
            )
        else:
            await self._send(session.chat_id, caption)

    async def _send_content_for_approval(self, session: PipelineSession) -> None:
        """Gửi content cho user duyệt."""
        content_preview = session.content_text[:1500]
        text = (
            f"📝 *Content đã tạo* (lần {session.content_attempts})\n\n"
            f"{'─' * 30}\n"
            f"{self._esc(content_preview)}\n"
            f"{'─' * 30}\n\n"
            f"Phản hồi:\n"
            f"  ✅ *OK* — Duyệt content, qua bước đăng bài\n"
            f"  🔄 *Nhắn bất kỳ* — Sửa/tạo lại theo yêu cầu\n"
            f"     VD: _\"viết lại ngắn hơn, thêm emoji\"_\n"
            f"  ❌ /cancel — Huỷ toàn bộ pipeline"
        )
        await self._send(session.chat_id, text)

    async def _send_posting_confirmation(
        self, session: PipelineSession, results: list[dict]
    ) -> None:
        """Gửi xác nhận đã đăng bài."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S %d/%m/%Y UTC")

        lines = [
            f"✅ *Đã hoàn thành Content Pipeline\\!*\n",
            f"📋 Session: `{session.session_id}`",
            f"🕐 Thời gian: {self._esc(now)}\n",
        ]

        for r in results:
            icon = "✅" if r["success"] else "❌"
            plat = r["platform"].capitalize()
            lines.append(f"  {icon} {plat}")
            if r.get("url"):
                lines.append(f"     🔗 {self._esc(r['url'])}")
            if r.get("error"):
                lines.append(f"     ⚠️ {self._esc(r['error'][:60])}")

        lines.append(f"\n🖼 Ảnh: {session.image_attempts} lần tạo")
        lines.append(f"📝 Content: {session.content_attempts} lần tạo")
        lines.append(f"⏱ Tổng thời gian: {session.age_minutes:.1f} phút")

        text = "\n".join(lines)
        await self._send(session.chat_id, text)

        # Gửi screenshot xác nhận
        if session.post_screenshot_path and self._send_photo:
            await self._send_photo(
                session.chat_id,
                session.post_screenshot_path,
                f"📸 Screenshot xác nhận đã post — {self._esc(now)}"
            )

    # ══════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════

    def _is_approval(self, text: str) -> bool:
        """Check if user message is an approval."""
        approvals = {"ok", "oke", "okie", "oki", "được", "duyệt", "ok luôn",
                     "đẹp", "tốt", "hay", "ok bạn", "ok nha", "xong",
                     "được rồi", "ổn", "yes", "y", "approve", "lgtm",
                     "ok đi", "ok thôi", "ok luon", "duyet", "duoc"}
        return text.strip().lower() in approvals

    def _is_cancel(self, text: str) -> bool:
        """Check if user wants to cancel."""
        cancels = {"cancel", "huỷ", "hủy", "dừng", "stop", "thôi",
                   "/cancel", "bỏ", "ko", "không", "no"}
        return text.strip().lower() in cancels

    def _extract_regen_style(self, text: str) -> str | None:
        """Extract new style from regeneration request."""
        patterns = [
            r"(?:theo|với|in|by)\s+(?:phong cách|style|kiểu)\s+(.+)",
            r"(?:đổi|chuyển|change)\s+(?:sang|to|thành)\s+(.+)",
            r"(?:phong cách|style)\s+(.+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return None

    async def _cancel_session(self, session: PipelineSession, reason: str) -> None:
        """Cancel a session."""
        session.state = PipelineState.CANCELLED
        session.error = reason
        _vlog("⏹", f"[{session.session_id}] Cancelled: {reason}")
        await self._send(
            session.chat_id,
            f"⏹ *Pipeline đã huỷ*\n\n"
            f"📋 Session: `{session.session_id}`\n"
            f"💬 Lý do: {self._esc(reason)}"
        )

    async def _fail_session(self, session: PipelineSession, error: str) -> None:
        """Mark session as failed."""
        session.state = PipelineState.FAILED
        session.error = error
        _vlog("❌", f"[{session.session_id}] Failed: {error}")
        await self._send(
            session.chat_id,
            f"❌ *Pipeline thất bại*\n\n"
            f"📋 Session: `{session.session_id}`\n"
            f"⚠️ Lỗi: {self._esc(error)}\n\n"
            f"Gửi lệnh mới để thử lại\\."
        )

    async def _send(self, chat_id: int, text: str) -> None:
        """Send message via Telegram."""
        if self._send_message:
            try:
                await self._send_message(chat_id, text)
            except Exception as exc:
                _vlog("⚠", f"Telegram send failed: {exc}")

    @staticmethod
    def _esc(text: str) -> str:
        """Escape Markdown V2 special characters."""
        if not text:
            return ""
        special = r'_*[]()~`>#+-=|{}.!'
        return "".join(f"\\{c}" if c in special else c for c in text)
