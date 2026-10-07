# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
telegram/b1b2b4_commands.py — Phidipus v1.25
B1: /post  — Social post with Telegram approval flow
B2: /postall — Multi-profile parallel posting
B4: /schedule, /schedules, /unschedule — Scheduled posting

These methods are injected into PhidipusBot at startup.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


# ══════════════════════════════════════════════════════════════════
# B1: /post — Approval Flow
# ══════════════════════════════════════════════════════════════════

def _llm_stack(self) -> Any:
    """FIX v4.3: the social workflow calls ``llm.call(prompt)`` which only the
    Skill-Forge FallbackStack provides — ``agent_loop._llm`` is an LLMClient
    (``complete``/``chat``), so content generation always failed."""
    forge = getattr(self._agent_loop, "_forge", None)
    try:
        if forge is not None and forge.enabled:
            return forge._get_stack()
    except Exception:
        pass
    return None


async def _cmd_post(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """/post [goal] — Tao anh AI + content + dang Facebook voi approval."""
    if not await self._check_auth(update):
        return
    msg = update.effective_message
    goal = " ".join(ctx.args) if ctx.args else ""
    if not goal:
        await msg.reply_text(
            "Dung: /post <mo ta>\n\n"
            "Vi du:\n"
            "/post tao anh con ong robot chatgpt content diem yeu openclaw\n\n"
            "Bot se:\n"
            "1. Tao anh AI tren ChatGPT\n"
            "2. Gui preview de ban duyet\n"
            "3. Tao content Gemini\n"
            "4. Gui content de ban duyet\n"
            "5. Dang Facebook sau khi ban xac nhan"
        )
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await msg.reply_text("Bat dau workflow voi approval...\nGoal: " + goal[:100])

    # Khoi tao ContentPipeline
    if not self._content_pipeline:
        try:
            from social.content_pipeline import ContentPipeline
            self._content_pipeline = ContentPipeline()
        except Exception as e:
            await msg.reply_text("Loi khoi tao pipeline: " + str(e))
            return

    try:
        await self._content_pipeline.start_pipeline(
            user_id=user_id,
            chat_id=chat_id,
            command=goal,
        )
    except Exception:
        pass

    from skills.workflow_spider_social_post import run_spider_social_post_workflow

    async def _send(text: str):
        try:
            await msg.reply_text(str(text)[:4000])
        except Exception:
            pass

    result = await run_spider_social_post_workflow(
        ipc_client=getattr(self._agent_loop, "_ipc", None),
        llm_fallback=_llm_stack(self),
        telegram_send_fn=_send,
        goal=goal,
        content_pipeline=self._content_pipeline,
        user_id=user_id,
        chat_id=chat_id,
    )

    if result.success:
        img_name = Path(result.image_path).name if result.image_path else "-"
        await msg.reply_text(
            "Workflow hoan thanh!\n"
            "Anh: " + img_name + "\n"
            "Content: " + str(len(result.content_text)) + " ky tu\n"
            "Thoi gian: " + str(int(result.duration_s)) + "s"
        )
    else:
        await msg.reply_text("That bai: " + result.error[:200])


async def _handle_pipeline_callback(self, query: Any, data: str) -> bool:
    """Xu ly callback tu approval inline keyboard. Tra True neu da handle."""
    if not self._content_pipeline:
        return False

    user_id = query.from_user.id
    parts = data.split(":", 1)
    action = parts[0]
    session_id = parts[1] if len(parts) > 1 else ""

    session = None
    if session_id and hasattr(self._content_pipeline, "get_session_by_id"):
        session = self._content_pipeline.get_session_by_id(session_id)
    if not session:
        return False

    if action == "pipe_approve_img":
        await self._content_pipeline.handle_user_response(user_id, "ok")
        await query.answer("Anh da duyet!")
        await query.message.reply_text("Anh da duyet. Dang tao content...")
        return True
    elif action == "pipe_regen_img":
        await self._content_pipeline.handle_user_response(user_id, "tao lai")
        await query.answer("Dang tao anh moi...")
        await query.message.reply_text("Dang tao lai anh...")
        return True
    elif action == "pipe_approve_content":
        await self._content_pipeline.handle_user_response(user_id, "ok")
        await query.answer("Content da duyet!")
        await query.message.reply_text("Content da duyet. Dang dang Facebook...")
        return True
    elif action == "pipe_regen_content":
        await self._content_pipeline.handle_user_response(user_id, "viet lai")
        await query.answer("Dang viet lai...")
        await query.message.reply_text("Dang tao lai content...")
        return True
    elif action == "pipe_cancel":
        await self._content_pipeline.cancel(user_id)
        await query.answer("Da huy")
        await query.message.reply_text("Workflow da huy.")
        return True
    return False


# ══════════════════════════════════════════════════════════════════
# B2: /postall — Multi-profile parallel
# ══════════════════════════════════════════════════════════════════

async def _cmd_postall(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """/postall <goal> — Dang len tat ca Chrome profiles song song."""
    if not await self._check_auth(update):
        return
    msg = update.effective_message
    goal = " ".join(ctx.args) if ctx.args else ""
    if not goal:
        await msg.reply_text(
            "Dung: /postall <goal>\n"
            "Dang bai len TAT CA Chrome profiles active song song.\n\n"
            "Vi du: /postall tao anh ong robot dang facebook\n\n"
            "Dung /profiles de xem danh sach accounts."
        )
        return

    profiles = await _get_active_profiles(self)
    if not profiles:
        await msg.reply_text(
            "Chua co profile nao active.\n\n"
            "Them vao config.yaml:\n"
            "spider_hub:\n"
            "  hive_settings:\n"
            "    default_platforms: [facebook]\n"
            "  profiles:\n"
            "    - name: 00Fujin\n"
            "      active: true\n"
            "      platforms: [facebook, instagram]\n\n"
            "Sau do restart Phidipus Agents."
        )
        return

    hive_cfg = await _get_hive_settings(self)
    stagger_s    = float(hive_cfg.get("stagger_s", 30))
    max_parallel = int(hive_cfg.get("max_parallel", 5))

    # Cap profiles to max_parallel
    active_profiles = profiles[:max_parallel]
    skipped = len(profiles) - len(active_profiles)

    summary_lines = [
        f"Bat dau /postall — {len(active_profiles)} profiles",
        f"Stagger: {stagger_s:.0f}s | Max parallel: {max_parallel}",
    ]
    if skipped:
        summary_lines.append(f"Bo qua: {skipped} profiles (vuot max_parallel)")
    for i, p in enumerate(active_profiles):
        plat = ", ".join(p["platforms"])
        eta  = int(i * stagger_s)
        summary_lines.append(f"  [{i+1}] {p['name']} → {plat} (bat dau sau {eta}s)")
    await msg.reply_text("\n".join(summary_lines))

    from skills.workflow_spider_social_post import run_spider_social_post_workflow

    async def _run_profile(profile: dict, stagger: float) -> tuple[str, Any]:
        """Chay workflow cho 1 profile."""
        # Stagger delay: profile i bat dau sau i * stagger_s
        base_delay = stagger + profile.get("post_delay_s", 0)
        if base_delay > 0:
            await asyncio.sleep(base_delay)

        profile_name = profile["name"]
        platforms    = profile.get("platforms", ["facebook"])

        async def _send(text: str) -> None:
            try:
                tag = f"[{profile_name}]"
                await msg.reply_text(f"{tag} {str(text)[:3500]}")
            except Exception:
                pass

        # Thong bao bat dau
        await _send(f"Bat dau workflow → {', '.join(platforms)}...")

        try:
            r = await run_spider_social_post_workflow(
                ipc_client=getattr(self._agent_loop, "_ipc", None),
                llm_fallback=_llm_stack(self),
                telegram_send_fn=_send,
                goal=goal,
                chrome_profile=profile_name,
                # Platforms from spider_hub profile config
                target_platforms=platforms,
                content_lang=profile.get("content_lang", "vi"),
            )
            return profile_name, r
        except Exception as exc:
            return profile_name, exc

    # Chay tat ca profiles song song voi stagger
    tasks = [
        _run_profile(p, i * stagger_s)
        for i, p in enumerate(active_profiles)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Tong hop ket qua
    ok_list, fail_list = [], []
    for item in results:
        if isinstance(item, Exception):
            fail_list.append(f"  Error: {str(item)[:80]}")
            continue
        pname, r = item
        if isinstance(r, Exception):
            fail_list.append(f"  [{pname}] Exception: {str(r)[:80]}")
        elif hasattr(r, "success") and r.success:
            ok_list.append(f"  OK [{pname}] ({int(getattr(r,'duration_s',0))}s)")
        else:
            err = getattr(r, "error", str(r))[:80] if hasattr(r, "error") else str(r)[:80]
            fail_list.append(f"  FAIL [{pname}]: {err}")

    lines = [f"Ket qua /postall ({len(ok_list)}/{len(active_profiles)} thanh cong):"]
    lines.extend(ok_list)
    lines.extend(fail_list)
    await msg.reply_text("\n".join(lines))


async def _cmd_profiles(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """/profiles — Hien thi danh sach Spider Hub profiles va trang thai."""
    if not await self._check_auth(update):
        return
    msg = update.effective_message

    try:
        cfg = getattr(self, "_config", None)
        hive_cfg = await _get_hive_settings(self)

        # All profiles (active + inactive)
        all_profiles: list[dict] = []
        cfg_obj = getattr(self, "_config", None)
        if cfg_obj is not None and hasattr(cfg_obj, "spider_hub"):
            all_profiles = [p.to_dict() for p in cfg_obj.spider_hub.profiles]
        else:
            raw = getattr(self, "_config_raw", None) or {}
            all_profiles = raw.get("spider_hub", {}).get("profiles", [])

        if not all_profiles:
            await msg.reply_text(
                "Chua co profile nao.\n\n"
                "Them vao config.yaml:\n"
                "spider_hub:\n"
                "  profiles:\n"
                "    - name: 00Fujin\n"
                "      active: true\n"
                "      platforms: [facebook]\n\n"
                "Xem config.example.yaml de biet them."
            )
            return

        lines = [
            f"Spider Hub — {len(all_profiles)} profiles",
            f"Max parallel: {hive_cfg.get('max_parallel', 5)} | Stagger: {hive_cfg.get('stagger_s', 30):.0f}s",
            f"Default platforms: {', '.join(hive_cfg.get('default_platforms', ['facebook']))}",
            "",
        ]
        active_count = 0
        for p in all_profiles:
            name   = p.get("name", "?")
            active = p.get("active", True)
            plat   = ", ".join(p.get("platforms", []) or hive_cfg.get("default_platforms", ["facebook"]))
            desc   = p.get("description", "")
            lang   = p.get("content_lang", "vi")
            status = "ON " if active else "OFF"
            if active:
                active_count += 1
            line = f"  [{status}] {name}"
            if desc:
                line += f" — {desc}"
            line += f"\n         Platforms: {plat} | Lang: {lang}"
            lines.append(line)

        lines.append("")
        lines.append(f"Active: {active_count}/{len(all_profiles)} | /postall chay {active_count} profiles")
        await msg.reply_text("\n".join(lines))

    except Exception as exc:
        await msg.reply_text(f"Loi doc profiles: {exc}")


async def _get_active_profiles(self) -> list[dict]:
    """
    Tra ve danh sach profiles active tu spider_hub config.

    Moi item la dict day du:
        {name, description, active, chrome_dir, platforms,
         facebook_url, instagram_url, x_url, content_lang,
         post_delay_s, tags}

    Fallback: neu khong co spider_hub config, tra ve 1 profile mac dinh
    lay tu CHROME_PROFILE constant trong workflow.
    """
    try:
        # Thu tu config_loader truoc (typed API)
        cfg = getattr(self, "_config", None)
        if cfg is not None and hasattr(cfg, "spider_hub"):
            return [p.to_dict() for p in cfg.spider_hub.active_profiles]
        # Fallback: doc raw config_raw dict
        raw = getattr(self, "_config_raw", None) or {}
        hive = raw.get("spider_hub", {})
        profiles = hive.get("profiles", [])
        hs = hive.get("hive_settings", {})
        default_platforms = hs.get("default_platforms", ["facebook"])
        active = []
        for p in profiles:
            if not p.get("active", True):
                continue
            active.append({
                "name":          p.get("name", ""),
                "description":   p.get("description", ""),
                "active":        True,
                "chrome_dir":    p.get("chrome_dir", "") or p.get("name", ""),
                "platforms":     p.get("platforms", default_platforms),
                "facebook_url":  p.get("facebook_url", "https://www.facebook.com"),
                "instagram_url": p.get("instagram_url", "https://www.instagram.com"),
                "x_url":         p.get("x_url", "https://x.com"),
                "content_lang":  p.get("content_lang", "vi"),
                "post_delay_s":  float(p.get("post_delay_s", 0)),
                "tags":          p.get("tags", []),
            })
        return active
    except Exception:
        return []


async def _get_hive_settings(self) -> dict:
    """Tra ve hive_settings tu config."""
    try:
        cfg = getattr(self, "_config", None)
        if cfg is not None and hasattr(cfg, "spider_hub"):
            return cfg.spider_hub.to_dict()["hive_settings"]
        raw = getattr(self, "_config_raw", None) or {}
        return raw.get("spider_hub", {}).get("hive_settings", {})
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════
# B4: /schedule, /schedules, /unschedule
# ══════════════════════════════════════════════════════════════════

async def _cmd_schedule(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """/schedule <goal> <HH:MM> — Dat lich dang bai hang ngay."""
    if not await self._check_auth(update):
        return
    msg = update.effective_message
    args = list(ctx.args or [])
    if len(args) < 2:
        await msg.reply_text(
            "Dung: /schedule <goal> <HH:MM>\n\n"
            "Vi du: /schedule tao anh ong robot dang facebook 08:00\n\n"
            "Bot se tu dang bai luc 08:00 moi ngay."
        )
        return

    time_str = args[-1]
    goal = " ".join(args[:-1])

    # Validate HH:MM
    import re as _re
    if not _re.match(r"^[0-9]{1,2}:[0-9]{2}$", time_str):
        await msg.reply_text("Dinh dang gio sai: '" + time_str + "'. Dung HH:MM (vd: 08:00)")
        return

    schedule = await _save_social_schedule(self, goal, time_str, update.effective_user.id)
    await msg.reply_text(
        "Da dat lich!\n\n"
        "Goal: " + goal[:100] + "\n"
        "Gio: " + time_str + " (moi ngay)\n"
        "ID: " + schedule["id"] + "\n\n"
        "Dung /schedules de xem danh sach."
    )


async def _cmd_schedules(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """/schedules — Xem danh sach lich dang bai."""
    if not await self._check_auth(update):
        return
    msg = update.effective_message
    schedules = await _load_social_schedules(self)
    if not schedules:
        await msg.reply_text("Chua co lich nao. Dung /schedule de them.")
        return
    lines = ["Lich dang bai tu dong:", ""]
    for s in schedules:
        st = "ON " if s.get("active", True) else "OFF "
        lines.append(st + "[" + s["id"] + "] " + s["time_str"] + " hang ngay")
        lines.append("  " + s["goal"][:60])
    lines.append("")
    lines.append("Dung /unschedule <ID> de xoa.")
    await msg.reply_text("\n".join(lines))


async def _cmd_unschedule(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """/unschedule <ID> — Xoa lich dang bai."""
    if not await self._check_auth(update):
        return
    msg = update.effective_message
    sched_id = ctx.args[0] if ctx.args else ""
    if not sched_id:
        await msg.reply_text("Dung: /unschedule <ID>")
        return
    schedules = await _load_social_schedules(self)
    original_len = len(schedules)
    schedules = [s for s in schedules if s.get("id") != sched_id]
    if len(schedules) < original_len:
        await _write_social_schedules(self, schedules)
        await msg.reply_text("Da xoa lich [" + sched_id + "]")
    else:
        await msg.reply_text("Khong tim thay lich voi ID: " + sched_id)


async def _save_social_schedule(self, goal: str, time_str: str, user_id: int) -> dict:
    sched_dir = Path("data/schedules")
    sched_dir.mkdir(parents=True, exist_ok=True)
    schedules = await _load_social_schedules(self)
    new_sched = {
        "id": uuid.uuid4().hex[:8],
        "goal": goal,
        "time_str": time_str,
        "user_id": user_id,
        "active": True,
        "created_at": time.time(),
    }
    schedules.append(new_sched)
    await _write_social_schedules(self, schedules)
    return new_sched


async def _load_social_schedules(self) -> list:
    sched_file = Path("data/schedules/social_posts.json")
    if not sched_file.exists():
        return []
    try:
        return json.loads(sched_file.read_text(encoding="utf-8"))
    except Exception:
        return []


async def _write_social_schedules(self, schedules: list) -> None:
    sched_file = Path("data/schedules/social_posts.json")
    sched_file.parent.mkdir(parents=True, exist_ok=True)
    sched_file.write_text(
        json.dumps(schedules, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ══════════════════════════════════════════════════════════════════
# Inject vào PhidipusBot class
# ══════════════════════════════════════════════════════════════════

def inject_b1b2b4_commands(bot_class: type) -> None:
    """Monkey-patch B1/B2/B4 command methods vao PhidipusBot."""
    bot_class._cmd_post               = _cmd_post
    bot_class._cmd_postall            = _cmd_postall
    bot_class._cmd_schedule           = _cmd_schedule
    bot_class._cmd_schedules          = _cmd_schedules
    bot_class._cmd_unschedule         = _cmd_unschedule
    bot_class._handle_pipeline_callback = _handle_pipeline_callback
    bot_class._get_active_profiles    = _get_active_profiles
    bot_class._save_social_schedule   = _save_social_schedule
    bot_class._load_social_schedules  = _load_social_schedules
    bot_class._write_social_schedules = _write_social_schedules
    bot_class._cmd_profiles           = _cmd_profiles
    bot_class._get_hive_settings      = _get_hive_settings
