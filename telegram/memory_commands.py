# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
telegram/memory_commands.py — Memory Agent commands for the Telegram bot (v4.3)
════════════════════════════════════════════════════════════════════════════════

Loaded by file path from telegram_bot.py (the local ``telegram`` package
shadows python-telegram-bot, so a normal import would fail) and injected
into PhidipusBot:

  /remember <nội dung>   /nho <nội dung>   — ghi nhớ một thông tin
  /recall <câu hỏi>                       — tìm trong bộ nhớ
  /memories [fact|preference|profile|procedure|episode]
  /forget <id | từ khoá | all>            — xoá (all cần xác nhận)
  /memoryoff  /memoryon                   — tắt / bật tự học từ tác vụ

Natural phrases in agent mode are recognised too:
  "ghi nhớ: …", "hãy nhớ rằng …", "nhớ giúp tôi là …", "remember that …"
("nhớ gửi báo cáo lúc 5h" is NOT captured — that is a task/reminder.)

Replies are plain text (no Markdown) so user content never breaks parsing.
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

_PHRASE_RE = re.compile(
    r"^(?:(?:hay\s+)?(?:ghi\s+nho|nho)\s+(?:giup\s+|gium\s+|dum\s+|ho\s+)?(?:toi\s+|minh\s+|em\s+|anh\s+)?"
    r"(?:rang|la)\s+"
    r"|ghi\s+nho\s*:\s*"
    r"|luu\s+vao\s+(?:bo\s+nho|tri\s+nho)\s*:?\s*"
    r"|remember\s+that\s+|note\s+that\s+)"
)
_KINDS = ("fact", "preference", "profile", "procedure", "episode")
_KIND_VI = {"fact": "sự kiện", "preference": "sở thích", "profile": "hồ sơ",
            "procedure": "quy trình", "episode": "tác vụ"}


def _fold_keep(text: str) -> str:
    """Accent-fold + lower-case keeping a 1:1 character alignment with NFC text."""
    out = []
    for ch in text:
        if ch in "đĐ":
            out.append("d")
            continue
        base = "".join(c for c in unicodedata.normalize("NFD", ch) if unicodedata.category(c) != "Mn")
        low = base.lower()
        out.append(low if len(low) == 1 else (ch.lower()[:1] or ch))
    return "".join(out)


def _agent():
    try:
        from memory.memory_agent import get_memory_agent
        return get_memory_agent()
    except Exception:
        return None


def _day(ts: float) -> str:
    return time.strftime("%d/%m/%Y", time.localtime(ts or time.time()))


def _format_result(res: dict[str, Any]) -> str:
    op = res.get("op")
    mid = res.get("id", "")
    tail = f"\n(id: {mid} — /forget {mid} để xoá)" if mid else ""
    red = "\n🔒 Đã ẩn thông tin nhạy cảm trong câu." if res.get("redacted") else ""
    if op == "ADD":
        return f"🧠 Đã ghi nhớ ({_KIND_VI.get(res.get('kind', ''), 'ký ức')}):\n{res.get('content', '')}{red}{tail}"
    if op == "UPDATE":
        return f"🧠 Đã cập nhật ký ức:\n{res.get('content', '')}\n(trước đó: {res.get('previous', '')}){red}{tail}"
    if op == "INVALIDATE":
        return (f"🧠 Đã ghi nhận thay đổi:\n{res.get('content', '')}\n"
                f"(thay cho: {res.get('previous', '')} — vẫn lưu trong lịch sử){red}{tail}")
    if op == "NOOP":
        return f"🧠 Mình đã nhớ điều này rồi:\n{res.get('content', '')}{tail}"
    return f"⚠️ Không lưu: {res.get('reason', 'không hợp lệ')}"


async def _reply(update, text: str, **kw) -> None:
    msg = update.effective_message
    for i in range(0, max(len(text), 1), 3900):
        await msg.reply_text(text[i:i + 3900], **kw)


# ── commands ─────────────────────────────────────────────────────────
async def _cmd_remember(self, update, ctx) -> None:
    if not await self._check_auth(update):
        return
    agent = _agent()
    if agent is None:
        await _reply(update, "🧠 Memory Agent đang tắt (memory_agent.enabled: false).")
        return
    text = " ".join(ctx.args or []).strip()
    if not text:
        await _reply(update, "Cách dùng: /remember <thông tin cần nhớ>\nVí dụ: /remember Sếp tên là Minh, thích báo cáo dạng PDF")
        return
    res = await agent.remember(text, source="user")
    await _reply(update, _format_result(res))


async def _cmd_recall(self, update, ctx) -> None:
    if not await self._check_auth(update):
        return
    agent = _agent()
    if agent is None:
        await _reply(update, "🧠 Memory Agent đang tắt.")
        return
    query = " ".join(ctx.args or []).strip()
    if not query:
        await _reply(update, "Cách dùng: /recall <câu hỏi>\nVí dụ: /recall sếp thích báo cáo kiểu gì")
        return
    hits = await agent.recall(query, k=6)
    if not hits:
        await _reply(update, "🔍 Chưa có ký ức nào liên quan.")
        return
    lines = [f"🔍 Ký ức liên quan tới «{query[:60]}»:"]
    for i, r in enumerate(hits, 1):
        lines.append(f"{i}. [{_day(r.updated_at)}] {r.content}\n   ({_KIND_VI.get(r.kind, r.kind)} · id {r.id} · điểm {r.score:.2f})")
    await _reply(update, "\n".join(lines))


async def _cmd_memories(self, update, ctx) -> None:
    if not await self._check_auth(update):
        return
    agent = _agent()
    if agent is None:
        await _reply(update, "🧠 Memory Agent đang tắt.")
        return
    kind = (ctx.args[0].lower() if ctx.args else "")
    kinds = [kind] if kind in _KINDS else ["profile", "preference", "fact"]
    items = []
    for k in kinds:
        items += agent.store.list(kind=k, limit=15)
    items.sort(key=lambda r: -r.updated_at)
    st = agent.stats()
    head = (f"🧠 BỘ NHỚ DÀI HẠN — {st['total']} ký ức "
            f"({', '.join(f'{_KIND_VI.get(k, k)}: {v}' for k, v in st['by_kind'].items()) or 'trống'})\n"
            f"Tự học từ tác vụ: {'BẬT' if st['observing'] else 'TẮT'} · lịch sử thay đổi: {st['history']}")
    if not items:
        await _reply(update, head + "\n\nChưa có ký ức. Dùng /remember <nội dung> để thêm.")
        return
    lines = [head, ""]
    for r in items[:15]:
        pin = "📌 " if r.pinned else ""
        lines.append(f"• {pin}[{_day(r.updated_at)}] {r.content}  (id {r.id})")
    blocks = agent.blocks()
    if blocks.get("user_profile"):
        lines += ["", "Hồ sơ tóm tắt:", blocks["user_profile"][:600]]
    await _reply(update, "\n".join(lines))


async def _cmd_forget(self, update, ctx) -> None:
    if not await self._check_auth(update):
        return
    agent = _agent()
    if agent is None:
        await _reply(update, "🧠 Memory Agent đang tắt.")
        return
    target = " ".join(ctx.args or []).strip()
    if not target:
        await _reply(update, "Cách dùng: /forget <id hoặc từ khoá>  ·  /forget all (xoá toàn bộ)")
        return
    if target.lower() in ("all", "tat ca", "tất cả"):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Xoá toàn bộ", callback_data="memforget:all_ok"),
            InlineKeyboardButton("❌ Huỷ", callback_data="memforget:cancel"),
        ]])
        await _reply(update, f"⚠️ Xoá TOÀN BỘ {agent.stats()['total']} ký ức? Không thể hoàn tác.",
                     reply_markup=kb)
        return
    res = await agent.forget(target)
    if not res["deleted"]:
        await _reply(update, "Không tìm thấy ký ức khớp. Xem id bằng /memories hoặc /recall.")
        return
    lines = [f"🗑 Đã xoá {len(res['deleted'])} ký ức:"] + [f"• {c}" for c in res.get("contents", [])]
    await _reply(update, "\n".join(lines))


async def _cmd_memoryoff(self, update, ctx) -> None:
    if not await self._check_auth(update):
        return
    agent = _agent()
    if agent:
        agent.set_observing(False)
    await _reply(update, "⏸ Đã TẮT tự học từ tác vụ. Ký ức cũ vẫn giữ; /remember vẫn hoạt động. Bật lại: /memoryon")


async def _cmd_memoryon(self, update, ctx) -> None:
    if not await self._check_auth(update):
        return
    agent = _agent()
    if agent:
        agent.set_observing(True)
    await _reply(update, "▶️ Đã BẬT tự học: Phidipus Agents ghi lại kết quả tác vụ và cách làm hiệu quả.")


async def _handle_memory_callback(self, query, data: str) -> bool:
    if not data.startswith("memforget:"):
        return False
    agent = _agent()
    if data == "memforget:all_ok" and agent is not None:
        n = await agent.forget_all()
        await query.message.edit_text(f"🗑 Đã xoá toàn bộ {n} ký ức.")
    else:
        await query.message.edit_text("Đã huỷ — bộ nhớ giữ nguyên.")
    return True


# ── natural phrases + chat context ───────────────────────────────────
async def _try_memory_phrase(self, update, text: str) -> bool:
    """'ghi nhớ: …' / 'hãy nhớ rằng …' → store directly. True when handled."""
    norm = unicodedata.normalize("NFC", text.strip())
    m = _PHRASE_RE.match(_fold_keep(norm))
    if not m:
        return False
    content = norm[m.end():].strip()
    if len(content) < 3:
        return False
    agent = _agent()
    if agent is None:
        return False
    res = await agent.remember(content, source="user")
    await _reply(update, _format_result(res))
    return True


async def _memory_context_for_chat(self, text: str, cloud: bool = False) -> str:
    agent = _agent()
    if agent is None:
        return ""
    try:
        return await agent.build_context(text, max_chars=1000, for_cloud=cloud)
    except Exception:
        return ""


def memory_stats_lines() -> list[str]:
    agent = _agent()
    if agent is None:
        return ["🧠 Memory Agent: tắt"]
    st = agent.stats()
    last = st.get("last_consolidation") or 0
    return [
        f"🧠 Memory Agent: {st['total']} ký ức · {st['procedures']} quy trình đã học",
        f"   tự học: {'bật' if st['observing'] else 'tắt'} · embed: {st['embed_model']} · "
        f"hợp nhất gần nhất: {_day(last) if last else 'chưa'}",
    ]


def inject_memory_commands(bot_class: type) -> None:
    bot_class._cmd_remember = _cmd_remember
    bot_class._cmd_recall = _cmd_recall
    bot_class._cmd_memories = _cmd_memories
    bot_class._cmd_forget = _cmd_forget
    bot_class._cmd_memoryoff = _cmd_memoryoff
    bot_class._cmd_memoryon = _cmd_memoryon
    bot_class._handle_memory_callback = _handle_memory_callback
    bot_class._try_memory_phrase = _try_memory_phrase
    bot_class._memory_context_for_chat = _memory_context_for_chat
    bot_class._memory_stats_lines = staticmethod(memory_stats_lines)
