# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
social/chatgpt_image_gen.py — Phidipus ChatGPT Image Generator v1.0
════════════════════════════════════════════════════════════════════

Tạo ảnh bằng ChatGPT thông qua Chrome automation (IPC).

Luồng hoạt động:
  1. Mở Chrome → chatgpt.com (profile Phidipus đã login)
  2. Gõ prompt tạo ảnh vào ô chat
  3. Nhấn Enter → chờ ChatGPT tạo ảnh (30-90 giây)
  4. Detect ảnh đã tạo xong (poll screenshot + VLM hoặc DOM check)
  5. Click nút download hoặc right-click → Save Image As
  6. Di chuyển file về thư mục dự án
  7. Return đường dẫn file ảnh

Fallback strategies:
  - Primary: Download button trên ChatGPT UI
  - Fallback 1: Right-click → Save Image As
  - Fallback 2: Screenshot full page → crop vùng ảnh
  - Fallback 3: Dùng AppleScript lấy URL ảnh → urllib download

Dependencies:
  - IPC Client (send_action to Chrome)
  - Smart Actions (keyboard shortcuts)
  - Optional: VLM (detect download button position)

Security:
  - Chỉ navigate đến chatgpt.com (URL whitelist)
  - Output chỉ trong ~/Phidipus/content/ (path-restricted)
  - Timeout 120 giây cho toàn bộ quá trình
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Result types
# ══════════════════════════════════════════════════════════════════

@dataclass
class ImageGenResult:
    """Kết quả tạo ảnh."""
    success: bool
    image_path: str = ""
    method: str = ""         # "download_button" | "save_as" | "screenshot" | "url_download"
    duration_seconds: float = 0.0
    error: str = ""
    prompt_used: str = ""


# ══════════════════════════════════════════════════════════════════
# ChatGPT Image Generator
# ══════════════════════════════════════════════════════════════════

class ChatGPTImageGenerator:
    """
    Tạo ảnh thông qua ChatGPT web UI bằng Chrome automation.

    Usage:
        gen = ChatGPTImageGenerator(ipc_client)
        path = await gen.generate(
            prompt="A cyberpunk bee in neon city",
            output_dir="~/Phidipus/content/abc123",
        )
    """

    CHATGPT_URL = "https://chatgpt.com"
    GENERATION_TIMEOUT = 60    # Max wait for image generation
    PAGE_LOAD_WAIT = 5          # Wait for page load
    POST_TYPE_WAIT = 2          # Wait after typing prompt
    POLL_INTERVAL = 5           # Seconds between completion checks
    DOWNLOAD_WAIT = 10          # Wait for download to appear

    # macOS Downloads folder
    DOWNLOADS_DIR = Path.home() / "Downloads"

    def __init__(
        self,
        ipc_client: Any = None,
        chrome_profile: str = "Default",
    ) -> None:
        self._ipc = ipc_client
        self._chrome_profile = chrome_profile

    # ══════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════

    async def generate(
        self,
        prompt: str,
        output_dir: str = "",
        session_id: str = "",
    ) -> str | None:
        """
        Tạo ảnh bằng ChatGPT — See-Think-Act pipeline v9.22.2

        Fixes:
          - Chỉ mở 1 tab (bỏ _start_new_chat riêng)
          - Timeout 60s thay vì 120s
          - Vision-based detection thay DOM poll
          - Vision click Save button thay DOM selector
        """
        start = time.time()

        if not output_dir:
            output_dir = str(Path.home() / "Phidipus" / "content")
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        _vlog("🎨", f"Starting image generation: {prompt[:80]}...")

        # Khởi tạo ChatGPTVisionHelper — tự đọc Gemini key từ config
        # Quan trọng: Gemini primary (1-3s) thay qwen3-vl (15-25s, ngốn RAM)
        vision = None
        try:
            from social.vision_actor import ChatGPTVisionHelper
            vision = ChatGPTVisionHelper(ipc_client=self._ipc)
            # ChatGPTVisionHelper.__init__ tự gọi PhidipusConfig().gemini_api_key
        except ImportError:
            _vlog("⚠️ ", "VisionActor not available — fallback to DOM polling")

        try:
            # ── B1: Force window size 1920×1080 trước khi mở ChatGPT ─────────
            # Giải pháp 1: Cưỡng bức 16:9 layout để Vision tọa độ luôn chính xác
            # Hoạt động trên 2K, 4K, ultrawide — không phụ thuộc màn hình nào
            await self._force_window_size(1920, 1080)
            await self._open_chatgpt()
            await asyncio.sleep(2)

            # ── B2: Gõ và gửi prompt ──────────────────────────────────────
            full_prompt = self._build_generation_prompt(prompt)
            await self._type_and_send(full_prompt)

            # ── B3: Chờ ảnh — Vision-first, DOM fallback ──────────────────
            _vlog("👁️ ", "Waiting for ChatGPT image (Vision + DOM, 60s timeout)...")
            image_ready = False

            if vision:
                # Tầng 1: VLM nhìn màn hình — không phụ thuộc DOM
                image_ready = await vision.wait_for_image_with_vision(
                    timeout_s=60, poll_interval=8.0
                )
                if image_ready:
                    _vlog("✅", "Vision confirmed image is ready")
            
            if not image_ready:
                # Tầng 2: DOM poll fallback (nếu VLM fail)
                _vlog("🔍", "Vision fallback → DOM polling (30s)...")
                image_ready = await self._poll_image_dom(timeout_s=30)

            if not image_ready:
                raise asyncio.TimeoutError("Image not ready after 60s + 30s fallback")

            # ── B4: Download ảnh ──────────────────────────────────────────────
            image_path = None

            # Thử 1: JS extract URL (nhanh nhất, 0 RAM)
            image_url = await self._extract_image_url()
            if image_url:
                image_path = await self._download_from_url(
                    image_url, output_dir, session_id
                )

            # Thử 2: Vision click Save/Download button
            if not image_path and vision:
                _vlog("👁️ ", "URL extract failed → Vision click Save button")
                saved = await vision.click_save_button()
                if not saved:
                    # Vision click image expand → then Save
                    await vision.click_image_to_expand()
                    await asyncio.sleep(1.0)
                    saved = await vision.click_save_button()
                if saved:
                    await asyncio.sleep(3)
                    image_path = self._find_latest_download(output_dir, session_id)
                    if image_path:
                        _vlog("✅", f"Vision Save → {image_path}")

            # Thử 3: JS click image fallback (Giải pháp 4 — sau khi Vision fail)
            if not image_path:
                _vlog("🖱️ ", "Vision failed → JS click image fallback")
                clicked = await self._js_click_image()
                if clicked:
                    await asyncio.sleep(2)
                    # Sau khi JS click expand, dùng Vision tìm Save button
                    if vision:
                        saved = await vision.click_save_button()
                        if saved:
                            await asyncio.sleep(3)
                            image_path = self._find_latest_download(output_dir, session_id)

            # Thử 4: DOM-based download
            if not image_path:
                _vlog("🔍", "Trying DOM download fallback")
                image_path = await self._download_image_dom(output_dir, session_id)

            # Thử 5: Screenshot cuối cùng
            if not image_path:
                _vlog("⚠️ ", "All download methods failed → screenshot fallback")
                image_path = await self._screenshot_fallback(output_dir, session_id)

            if image_path and os.path.exists(image_path):
                duration = time.time() - start
                _vlog("✅", f"Image saved: {image_path} ({duration:.1f}s)")
                return image_path

            _vlog("❌", "All image capture methods failed")
            return None

        except asyncio.TimeoutError:
            _vlog("❌", "Image generation timed out (90s total)")
            if vision:
                desc = await vision.describe_screen("what is visible on ChatGPT")
                _vlog("👁️ ", f"Screen state at timeout: {desc[:100]}")
            return None
        except Exception as exc:
            _vlog("❌", f"Image generation error: {exc}")
            return None

    async def _wait_for_image_observer(self, timeout_s: int = 60) -> bool:
        """
        A2 v9.24: MutationObserver — detect ảnh NGAY KHI DOM thay đổi.
        Thay poll 5s/lần → nhận event realtime → tiết kiệm 2–8 giây.

        JS inject vào ChatGPT tab: theo dõi mutations toàn DOM,
        resolve ngay khi tìm thấy <img> có naturalWidth > 200.
        """
        if platform.system() != "Darwin":
            return False
        try:
            js_observer = (
                "(function(){"
                "  return new Promise(function(resolve){"
                "    var found = false;"
                "    function checkImgs(){"
                "      var imgs = document.querySelectorAll('img');"
                "      for(var i=imgs.length-1;i>=0;i--){"
                "        var w=imgs[i].naturalWidth, h=imgs[i].naturalHeight;"
                "        var s=imgs[i].src||'';"
                "        if(w>200 && h>200 && s.startsWith('http')){"
                "          found=true; return true;"
                "        }"
                "      }"
                "      return false;"
                "    }"
                "    if(checkImgs()){resolve('found_immediately');return;}"
                "    var obs=new MutationObserver(function(){"
                "      if(!found && checkImgs()){"
                "        found=true; obs.disconnect();"
                "        resolve('found_by_observer');"
                "      }"
                "    });"
                "    obs.observe(document.body,{childList:true,subtree:true,attributes:true});"
                f"    setTimeout(function(){{obs.disconnect();resolve('timeout');}},{int(timeout_s*1000)});"
                "  });"
                "})()"
            )
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.js',
                                             delete=False, encoding='utf-8') as tf:
                tf.write(js_observer)
                tmp = tf.name

            script = (
                'tell application "Google Chrome"\n'
                f'  set r to execute active tab of front window javascript'
                f' (read POSIX file "{tmp}" as «class utf8»)\n'
                '  return r as string\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run, ["osascript", "-e", script],
                capture_output=True, text=True,
                timeout=timeout_s + 10,
            )
            import os as _os
            try:
                _os.unlink(tmp)
            except Exception:
                pass
            result = proc.stdout.strip()
            _vlog("👁️ ", f"MutationObserver result: {result}")
            return "found" in result
        except Exception as e:
            _vlog("⚠️ ", f"MutationObserver error: {e}")
            return False

    async def _poll_image_dom(self, timeout_s: int = 30) -> bool:
        """
        DOM-based image detection.
        A2 v9.24: Thử MutationObserver trước (realtime, 0 polling overhead),
        fallback về poll 5s nếu MutationObserver không hoạt động.
        """
        # Tầng 1: MutationObserver — detect ngay khi DOM thay đổi
        observer_ok = await self._wait_for_image_observer(timeout_s=min(timeout_s, 25))
        if observer_ok:
            return True
        # Tầng 2: Poll fallback nếu MutationObserver fail
        _vlog("⏳", "MutationObserver miss → poll fallback...")
        deadline = time.time() + max(timeout_s - 25, 10)
        while time.time() < deadline:
            done = await self._check_generation_complete()
            if done:
                return True
            await asyncio.sleep(5)
        return False

    async def _download_image_dom(self, output_dir: str, session_id: str) -> str | None:
        """DOM-based download — original logic."""
        return await self._download_image(output_dir, session_id)

    async def _force_window_size(self, width: int = 1920, height: int = 1080) -> None:
        """
        Resize Chrome window về kích thước cố định trước Vision.
        FIX: IPC không có set_window_size → dùng AppleScript trực tiếp.
        """
        if platform.system() != "Darwin":
            return
        try:
            script = (
                f'tell application "Google Chrome"\n'
                f'    activate\n'
                f'    set bounds of front window to {{0, 0, {width}, {height}}}\n'
                f'end tell'
            )
            await asyncio.to_thread(
                subprocess.run, ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
            _vlog("🪟", f"Chrome window → {width}×{height} (AppleScript)")
            await asyncio.sleep(0.5)
            # v9.23: Reset CẢ HAI cache sau khi resize
            import social.vision_actor as _va
            _va._screen_cache = {}
            _va.invalidate_chrome_bounds_cache()   # force re-detect Chrome bounds
        except Exception as e:
            _vlog("⚠️ ", f"Force window size: {e}")

    async def _js_click_image(self) -> bool:
        """
        Giải pháp 4: JS fallback — click ảnh trực tiếp bằng JavaScript.
        Dùng sau khi Vision click fail 2 lần liên tiếp.
        Tìm img lớn nhất trên trang (naturalWidth > 200) và dispatch click event.
        """
        if platform.system() != "Darwin":
            return False
        try:
            js = (
                "(function(){"
                "  var imgs = document.querySelectorAll('img');"
                "  var best = null, bestW = 0;"
                "  for(var i=0;i<imgs.length;i++){"
                "    var w = imgs[i].naturalWidth || imgs[i].offsetWidth;"
                "    if(w > bestW && w > 150){"
                "      bestW = w; best = imgs[i];"
                "    }"
                "  }"
                "  if(!best) return 'notfound';"
                "  best.click();"
                "  best.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));"
                "  return 'clicked:' + bestW;"
                "})()"
            )
            script = (
                'tell application "Google Chrome"\n'
                f'  set r to execute active tab of front window javascript "{js}"\n'
                '  return r as string\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run, ["osascript", "-e", script],
                capture_output=True, text=True, timeout=8,
            )
            result = proc.stdout.strip()
            ok = "clicked" in result
            _vlog("🖱️ ", f"JS click image: {result}")
            return ok
        except Exception as e:
            _vlog("⚠️ ", f"JS click image error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════
    # Chrome automation steps
    # ══════════════════════════════════════════════════════════════

    async def _open_chatgpt(self) -> None:
        """Open Chrome to ChatGPT.
        Fix: IPC app_launch schema không cho phép 'url' field.
        Tách thành 2 action: app_launch → browser_navigate.
        """
        if self._ipc:
            await self._ipc.send_action("app_launch", {
                "app_name": "Google Chrome",
            })
            await asyncio.sleep(2)
            await self._ipc.send_action("browser_navigate", {
                "url": self.CHATGPT_URL,
            })
        else:
            # Direct AppleScript fallback (macOS)
            if platform.system() == "Darwin":
                script = (
                    f'tell application "Google Chrome"\n'
                    f'  activate\n'
                    f'  set URL of active tab of front window to "{self.CHATGPT_URL}"\n'
                    f'end tell'
                )
                await asyncio.to_thread(
                    subprocess.run,
                    ["osascript", "-e", script],
                    timeout=10, capture_output=True,
                )

        await asyncio.sleep(self.PAGE_LOAD_WAIT)

    async def _start_new_chat(self) -> None:
        """
        Start fresh conversation.
        FIX: KHÔNG dùng Cmd+Shift+O — chữ 'O' rớt vào text box.
        Navigate đến URL mới → luôn fresh.
        """
        if self._ipc:
            await self._ipc.send_action("browser_navigate", {"url": self.CHATGPT_URL})
        else:
            if platform.system() == "Darwin":
                script = (
                    f'tell application "Google Chrome"\n'
                    f'  set URL of active tab of front window to "{self.CHATGPT_URL}"\n'
                    f'end tell'
                )
                await asyncio.to_thread(
                    subprocess.run, ["osascript", "-e", script],
                    timeout=5, capture_output=True,
                )
        await asyncio.sleep(3)

    async def _type_and_send(self, prompt: str) -> None:
        """
        Đưa prompt vào ChatGPT và gửi.

        FIX v9.22.1 — Nguyên tắc: KHÔNG dùng keyboard để type text.
          - keyboard_type IPC bị Telex intercept → text corrupt
          - keystroke AppleScript bị Telex intercept → text corrupt
          - GIẢI PHÁP: pbcopy + Cmd+V (clipboard hoàn toàn bypass bộ gõ)
          - Switch sang ABC trước, restore sau (try/finally)
          - Clear ô trước khi paste (execCommand delete)
          - Verify paste + verify sent
        """
        # 0. Switch sang ABC
        old_im = await self._get_input_method()
        await self._switch_to_abc()
        _vlog("⌨️ ", f"Input: '{old_im}' → ABC")

        try:
            # 1. Copy prompt vào clipboard
            await asyncio.to_thread(
                subprocess.run, ["pbcopy"],
                input=prompt.encode("utf-8"),
                capture_output=True, timeout=3,
            )
            await asyncio.sleep(0.3)

            # 2. Clear ô chat (JS)
            await self._js_clear_and_focus()
            await asyncio.sleep(0.5)

            # 3. Bring Chrome to front
            await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                capture_output=True, timeout=3,
            )
            await asyncio.sleep(0.5)

            # 4. Cmd+V paste
            await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e",
                 'tell application "System Events"\n'
                 '    keystroke "v" using command down\n'
                 'end tell'],
                capture_output=True, timeout=5,
            )
            await asyncio.sleep(1.5)

            # 5. Verify paste
            n = await self._js_input_length()
            _vlog("📋", f"Paste check: {n} chars in box")

            if n < 5:
                _vlog("⚠️ ", "Paste fail → retry Cmd+V")
                await asyncio.to_thread(
                    subprocess.run,
                    ["osascript", "-e",
                     'tell application "System Events"\n'
                     '    keystroke "v" using command down\n'
                     'end tell'],
                    capture_output=True, timeout=5,
                )
                await asyncio.sleep(1.5)
                n = await self._js_input_length()
                _vlog("📋", f"Paste retry: {n} chars")

            # 6. Send — JS click button
            await asyncio.sleep(1.5)
            sent = await self._js_click_send_button()
            _vlog("📤", f"Send button: {'clicked' if sent else 'fallback'}")

            if not sent:
                await asyncio.to_thread(
                    subprocess.run,
                    ["osascript", "-e",
                     'tell application "System Events"\n'
                     '    key code 36\n'
                     'end tell'],
                    capture_output=True, timeout=5,
                )

            # 7. Verify sent (ô phải trống)
            await asyncio.sleep(2.5)
            after = await self._js_input_length()
            _vlog("✅", f"After send: {after} chars {'(ok)' if after < 5 else '(STILL IN BOX)'}")

            if after >= 5:
                _vlog("⚠️ ", "Still in box — Enter lần 2")
                await asyncio.to_thread(
                    subprocess.run,
                    ["osascript", "-e",
                     'tell application "System Events"\n'
                     '    key code 36\n'
                     'end tell'],
                    capture_output=True, timeout=5,
                )

        finally:
            await self._restore_input_method(old_im)
            _vlog("⌨️ ", f"Input restored → '{old_im}'")

    # ── Input method helpers ──────────────────────────────────────────────

    async def _get_input_method(self) -> str:
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e",
                 'tell application "System Events"\n'
                 '    return name of current input source\n'
                 'end tell'],
                capture_output=True, text=True, timeout=5,
            )
            return proc.stdout.strip()
        except Exception:
            return ""

    async def _switch_to_abc(self) -> None:
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
        try:
            proc = await asyncio.to_thread(
                subprocess.run, ["osascript", "-e", script],
                capture_output=True, text=True, timeout=6,
            )
            _vlog("🔤", f"ABC switch: {proc.stdout.strip()}")
        except Exception:
            pass

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
        try:
            await asyncio.to_thread(
                subprocess.run, ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    # ── JS helpers ────────────────────────────────────────────────────────

    async def _run_js(self, js_code: str) -> str:
        """Run JS in Chrome front tab. Handles escaping internally."""
        if platform.system() != "Darwin":
            return ""
        try:
            # Write JS to temp file to avoid escaping issues
            import tempfile, os as _os
            with tempfile.NamedTemporaryFile(mode='w', suffix='.js',
                                             delete=False, encoding='utf-8') as f:
                f.write(js_code)
                tmp = f.name
            script = (
                'tell application "Google Chrome"\n'
                f'  set r to execute active tab of front window javascript'
                f' (read POSIX file "{tmp}" as «class utf8»)\n'
                '  return r as string\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=8,
            )
            _os.unlink(tmp)
            return proc.stdout.strip()
        except Exception:
            return ""

    async def _js_clear_and_focus(self) -> None:
        js = (
            "(function(){"
            "  var sel = '#prompt-textarea, p[data-placeholder],"
            " div[contenteditable=true].ProseMirror,"
            " [role=textbox][contenteditable=true]';"
            "  var el = document.querySelector(sel);"
            "  if(!el) return 'notfound';"
            "  el.focus(); el.click();"
            "  document.execCommand('selectAll', false, null);"
            "  document.execCommand('delete', false, null);"
            "  el.dispatchEvent(new InputEvent('input',{bubbles:true}));"
            "  return 'ok';"
            "})()"
        )
        await self._run_js(js)

    async def _js_input_length(self) -> int:
        js = (
            "(function(){"
            "  var sel = '#prompt-textarea, p[data-placeholder],"
            " div[contenteditable=true].ProseMirror,"
            " [role=textbox][contenteditable=true]';"
            "  var el = document.querySelector(sel);"
            "  if(!el) return -1;"
            "  return (el.innerText || el.textContent || '').trim().length;"
            "})()"
        )
        try:
            r = await self._run_js(js)
            return int(r) if r.lstrip('-').isdigit() else 0
        except Exception:
            return 0

    async def _js_click_send_button(self) -> bool:
        """Click ChatGPT Send button via JS. Returns True if clicked."""
        js = (
            "(function(){"
            "  var btns = ["
            "    '[data-testid=\"send-button\"]',"
            "    '[data-testid=\"composer-submit-button\"]',"
            "    'button[aria-label*=\"Send\"]',"
            "    'form button[type=submit]'"
            "  ];"
            "  for(var i=0;i<btns.length;i++){"
            "    var b=document.querySelector(btns[i]);"
            "    if(b && !b.disabled){b.click();return 'clicked';}"
            "  }"
            "  return 'notfound';"
            "})()"
        )
        r = await self._run_js(js)
        return "clicked" in r

    async def _wait_for_generation(self) -> None:
        """
        Wait for ChatGPT to finish generating the image.

        Strategy: Poll every POLL_INTERVAL seconds, check for:
          1. Download button appearing (DOM check via AppleScript)
          2. "Image generated" text in page
          3. Timeout at GENERATION_TIMEOUT seconds
        """
        deadline = time.time() + self.GENERATION_TIMEOUT
        check_count = 0

        while time.time() < deadline:
            check_count += 1
            await asyncio.sleep(self.POLL_INTERVAL)

            # Check if generation is complete
            done = await self._check_generation_complete()
            if done:
                _vlog("✅", f"Image generation complete (checked {check_count}x)")
                return

            if check_count % 4 == 0:
                _vlog("⏳", f"Still waiting... ({check_count * self.POLL_INTERVAL}s)")

        raise asyncio.TimeoutError("Image generation timed out")

    async def _check_generation_complete(self) -> bool:
        """
        Check if ChatGPT has finished generating.

        Uses AppleScript to check page DOM for completion indicators.
        """
        if platform.system() != "Darwin":
            # Non-macOS: just wait fixed time
            return False

        try:
            # Check for image element or download button via JavaScript
            js_check = (
                "document.querySelector('img[alt*=\"Generated\"]') !== null || "
                "document.querySelector('button[aria-label*=\"Download\"]') !== null || "
                "document.querySelector('[data-testid=\"image-download\"]') !== null || "
                "document.querySelectorAll('.group img').length > 0"
            )
            script = (
                'tell application "Google Chrome"\n'
                f'  set result to execute active tab of front window javascript "{js_check}"\n'
                '  return result\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                timeout=5, capture_output=True, text=True,
            )
            output = proc.stdout.strip().lower()
            return output == "true"

        except Exception:
            return False

    async def _download_image(
        self, output_dir: str, session_id: str
    ) -> str | None:
        """
        Download the generated image.

        Strategy 1: Find and click download button
        Strategy 2: Right-click → Save Image As
        Strategy 3: Extract image URL via JS → download with urllib
        """
        # Strategy 3 (most reliable): Extract URL via JavaScript
        image_url = await self._extract_image_url()
        if image_url:
            return await self._download_from_url(image_url, output_dir, session_id)

        # Strategy 1: Click download button
        clicked = await self._click_download_button()
        if clicked:
            await asyncio.sleep(self.DOWNLOAD_WAIT)
            # Find the downloaded file
            return self._find_latest_download(output_dir, session_id)

        return None

    async def _extract_image_url(self) -> str | None:
        """
        Extract image URL từ ChatGPT page via JavaScript.
        v9.23: Largest-image heuristic — bỏ CDN pattern list.
        Tìm img có diện tích naturalWidth × naturalHeight lớn nhất
        với threshold w > 200px và h > 200px.
        Không bao giờ lỗi thời dù OpenAI đổi CDN domain.
        """
        if platform.system() != "Darwin":
            return None

        try:
            js_code = (
                "(function(){"
                "  var imgs = document.querySelectorAll('img');"
                "  var best = null, bestArea = 0;"
                "  for(var i = 0; i < imgs.length; i++) {"
                "    var w = imgs[i].naturalWidth, h = imgs[i].naturalHeight;"
                "    var src = imgs[i].src || '';"
                "    if(w < 200 || h < 200) continue;"
                "    if(!src.startsWith('http')) continue;"
                "    var area = w * h;"
                "    if(area > bestArea) { bestArea = area; best = imgs[i]; }"
                "  }"
                "  return best ? best.src : '';"
                "})()"
            )
            script = (
                'tell application "Google Chrome"\n'
                f'  set imgUrl to execute active tab of front window javascript "{js_code}"\n'
                '  return imgUrl\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                timeout=5, capture_output=True, text=True,
            )
            url = proc.stdout.strip()
            if url and url.startswith("http"):
                _vlog("🔗", f"Found image URL: {url[:80]}...")
                return url

        except Exception as exc:
            _vlog("⚠", f"URL extraction failed: {exc}")

        return None

    async def _download_from_url(
        self, url: str, output_dir: str, session_id: str
    ) -> str | None:
        """Download image from URL."""
        try:
            import urllib.request

            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"chatgpt_{session_id}_{ts}.png"
            filepath = os.path.join(output_dir, filename)

            def _download():
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                })
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                with open(filepath, "wb") as f:
                    f.write(data)
                return len(data)

            size = await asyncio.to_thread(_download)
            _vlog("💾", f"Downloaded {size} bytes → {filepath}")
            return filepath

        except Exception as exc:
            _vlog("❌", f"URL download failed: {exc}")
            return None

    async def _click_download_button(self) -> bool:
        """Try to click the download button on ChatGPT."""
        if platform.system() != "Darwin":
            return False

        try:
            js_click = (
                "var btn = document.querySelector("
                "'button[aria-label*=\"Download\"], "
                "[data-testid=\"image-download\"], "
                "a[download]');"
                "if (btn) { btn.click(); true; } else { false; }"
            )
            script = (
                'tell application "Google Chrome"\n'
                f'  set result to execute active tab of front window javascript "{js_click}"\n'
                '  return result\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                timeout=5, capture_output=True, text=True,
            )
            return proc.stdout.strip().lower() == "true"

        except Exception:
            return False

    def _find_latest_download(
        self, output_dir: str, session_id: str
    ) -> str | None:
        """Find the most recently downloaded image in Downloads folder."""
        try:
            # Look for recent files in Downloads
            patterns = ["*.png", "*.jpg", "*.jpeg", "*.webp"]
            recent_files = []

            for pattern in patterns:
                for f in self.DOWNLOADS_DIR.glob(pattern):
                    stat = f.stat()
                    # Only files created in last 2 minutes
                    if time.time() - stat.st_mtime < 120:
                        recent_files.append((stat.st_mtime, f))

            if not recent_files:
                return None

            # Get most recent
            recent_files.sort(reverse=True)
            latest = recent_files[0][1]

            # Move to output_dir
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(output_dir, f"chatgpt_{session_id}_{ts}{latest.suffix}")
            shutil.move(str(latest), dest)
            _vlog("💾", f"Moved download: {latest.name} → {dest}")
            return dest

        except Exception as exc:
            _vlog("⚠", f"Find download failed: {exc}")
            return None

    async def _screenshot_fallback(
        self, output_dir: str, session_id: str
    ) -> str | None:
        """Fallback: take screenshot of the ChatGPT page."""
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(output_dir, f"chatgpt_screenshot_{session_id}_{ts}.png")

            # macOS screencapture (always — IPC screenshot_capture doesn't save to path)
            if platform.system() == "Darwin":
                await asyncio.to_thread(
                    subprocess.run,
                    ["screencapture", "-x", filepath],
                    timeout=5,
                )
            elif self._ipc:
                # IPC screenshot_capture — captures to temp, copy manually
                result = await self._ipc.send_action("screenshot_capture", {})
                await asyncio.sleep(1)

            if os.path.exists(filepath):
                return filepath

        except Exception as exc:
            _vlog("⚠", f"Screenshot fallback failed: {exc}")

        return None

    # ══════════════════════════════════════════════════════════════
    # Prompt building
    # ══════════════════════════════════════════════════════════════

    def _build_generation_prompt(self, prompt: str) -> str:
        """
        Build optimal ChatGPT prompt for image generation.
        """
        # Ensure prompt explicitly asks to generate an image
        if not any(kw in prompt.lower() for kw in ["generate", "create", "draw", "make"]):
            prompt = f"Generate an image: {prompt}"

        # Add quality instructions
        if "quality" not in prompt.lower():
            prompt += " High quality, detailed, professional."

        return prompt

    # ══════════════════════════════════════════════════════════════
    # Stats
    # ══════════════════════════════════════════════════════════════

    def stats(self) -> dict:
        return {
            "chatgpt_url": self.CHATGPT_URL,
            "chrome_profile": self._chrome_profile,
            "generation_timeout": self.GENERATION_TIMEOUT,
            "has_ipc": self._ipc is not None,
        }
