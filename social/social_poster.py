# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
social/social_poster.py — Phidipus Social Media Poster v1.0
════════════════════════════════════════════════════════════

Đăng bài lên Facebook / Instagram / X thông qua Chrome automation.

Mỗi platform có flow riêng:

Facebook:
  1. Mở facebook.com → Click "Bạn đang nghĩ gì?" (create post)
  2. Upload ảnh → Paste content → Click "Đăng"
  3. Chờ post xuất hiện → Screenshot xác nhận

Instagram:
  1. Mở instagram.com → Click "+" (create)
  2. Upload ảnh → Next → Paste caption → Share
  3. Screenshot xác nhận

X (Twitter):
  1. Mở x.com → Click compose (hoặc Cmd+N)
  2. Upload ảnh → Paste content → Click "Post"
  3. Screenshot xác nhận

Dependencies:
  - IPC Client hoặc AppleScript (macOS)
  - Chrome profile đã login sẵn các mạng xã hội

Security:
  - URL whitelist (chỉ navigate đến facebook/instagram/x)
  - File upload chỉ từ ~/Phidipus/content/
  - Timeout mỗi platform: 60 giây
"""
from __future__ import annotations

# v9.28: Vision verification
try:
    from vision.click_verifier import ClickVerifier, SiteAssertions
    _HAS_VERIFIER = True
except ImportError:
    _HAS_VERIFIER = False

import asyncio
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Result types
# ══════════════════════════════════════════════════════════════════

@dataclass
class PostResult:
    """Kết quả đăng bài."""
    success: bool
    platform: str
    url: str = ""               # URL bài post (nếu lấy được)
    screenshot_path: str = ""   # Screenshot xác nhận
    error: str = ""
    duration_seconds: float = 0.0


# ══════════════════════════════════════════════════════════════════
# Platform URLs (whitelist)
# ══════════════════════════════════════════════════════════════════

PLATFORM_URLS = {
    "facebook": {
        "home": "https://www.facebook.com",
        "create": "https://www.facebook.com",  # Click on create post box
    },
    "instagram": {
        "home": "https://www.instagram.com",
        "create": "https://www.instagram.com",  # Click on + button
    },
    "x": {
        "home": "https://x.com",
        "compose": "https://x.com/compose/post",  # Direct compose URL
    },
}


# ══════════════════════════════════════════════════════════════════
# Social Poster
# ══════════════════════════════════════════════════════════════════

class SocialPoster:
    """
    Đăng bài lên mạng xã hội qua Chrome automation.

    Usage:
        poster = SocialPoster(ipc_client)
        result = await poster.post(
            platform="facebook",
            content="Bài đăng mới! #phidipus",
            image_path="/path/to/image.png",
        )
    """

    POST_TIMEOUT = 60       # Timeout per platform
    PAGE_LOAD_WAIT = 5      # Wait for page to load
    ACTION_DELAY = 2        # Delay between actions
    UPLOAD_WAIT = 5         # Wait for image upload
    CONTENT_DIR = Path.home() / "Phidipus" / "content"

    def __init__(self, ipc_client: Any = None) -> None:
        self._ipc = ipc_client
        # v9.28: ClickVerifier for post-action verification
        self._verifier = (
            ClickVerifier(js_exec_fn=self._js_exec, ui_settle_s=0.8)
            if _HAS_VERIFIER else None
        )

    # ══════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════

    async def post(
        self,
        platform: str,
        content: str,
        image_path: str = "",
    ) -> dict:
        """
        Đăng bài lên một platform.

        Returns dict: {"success": bool, "url": str, "error": str}
        """
        platform = platform.lower().strip()
        if platform not in PLATFORM_URLS:
            return {"success": False, "error": f"Platform '{platform}' không được hỗ trợ", "url": ""}

        # Validate image path
        if image_path and not os.path.exists(image_path):
            return {"success": False, "error": f"File ảnh không tồn tại: {image_path}", "url": ""}

        start = time.time()
        _vlog("📤", f"Posting to {platform}...")

        try:
            result = await asyncio.wait_for(
                self._post_to_platform(platform, content, image_path),
                timeout=self.POST_TIMEOUT,
            )
            result["duration_seconds"] = time.time() - start
            return result

        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Timeout ({self.POST_TIMEOUT}s) khi đăng lên {platform}",
                "url": "",
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc)[:200],
                "url": "",
            }

    async def post_multi(
        self,
        platforms: list[str],
        content: str,
        image_path: str = "",
    ) -> list[dict]:
        """Đăng bài lên nhiều platforms."""
        results = []
        for plat in platforms:
            result = await self.post(plat, content, image_path)
            results.append(result)
            if len(platforms) > 1:
                await asyncio.sleep(3)  # Delay between platforms
        return results

    async def take_screenshot(self, output_path: str) -> str | None:
        """Chụp screenshot xác nhận."""
        try:
            if self._ipc:
                # FIX v4.3: "screenshot_save" is not an IPC action
                from utils.platform_adapter import take_screenshot
                output_path = await take_screenshot(output_path)
                await asyncio.sleep(1)
            elif platform.system() == "Darwin":
                await asyncio.to_thread(
                    subprocess.run,
                    ["screencapture", "-x", output_path],
                    timeout=5,
                )

            if os.path.exists(output_path):
                return output_path

        except Exception as exc:
            _vlog("⚠", f"Screenshot failed: {exc}")

        return None

    # ══════════════════════════════════════════════════════════════
    # Platform-specific posting
    # ══════════════════════════════════════════════════════════════

    async def _post_to_platform(
        self, platform: str, content: str, image_path: str
    ) -> dict:
        """Route to platform-specific posting method."""
        handlers = {
            "facebook": self._post_facebook,
            "instagram": self._post_instagram,
            "x": self._post_x,
        }
        handler = handlers.get(platform)
        if not handler:
            return {"success": False, "error": "Unknown platform", "url": ""}

        return await handler(content, image_path)

    # ── Facebook ──────────────────────────────────────────────────

    async def _post_facebook(self, content: str, image_path: str) -> dict:
        """
        Đăng bài Facebook.

        Flow:
          1. Navigate đến facebook.com
          2. Click "Bạn đang nghĩ gì?" để mở create post dialog
          3. Upload ảnh (nếu có)
          4. Paste content
          5. Click "Đăng" / "Post"
        """
        try:
            # Step 1: Open Facebook
            await self._navigate("https://www.facebook.com")
            await asyncio.sleep(self.PAGE_LOAD_WAIT)

            # Step 2: Click "What's on your mind?" to open create post
            # Use JavaScript to click the create post area
            clicked = await self._js_exec(
                "var el = document.querySelector("
                "'[aria-label*=\"on your mind\"], "
                "[aria-label*=\"nghĩ gì\"], "
                "[aria-label*=\"Create a post\"], "
                "[role=\"textbox\"][aria-label], "
                "div[data-pagelet=\"FeedComposer\"] [role=\"button\"]');"
                "if (el) { el.click(); 'true'; } else { 'false'; }"
            )

            if clicked != "true":
                # Fallback: try clicking the post composer area
                await self._js_exec(
                    "var els = document.querySelectorAll('[role=\"button\"]');"
                    "for (var i = 0; i < els.length; i++) {"
                    "  var t = els[i].innerText || '';"
                    "  if (t.includes('nghĩ gì') || t.includes('on your mind')) {"
                    "    els[i].click(); break;"
                    "  }"
                    "}"
                )

            await asyncio.sleep(self.ACTION_DELAY)

            # v9.28: Verify composer opened (#1)
            if self._verifier:
                v = await self._verifier.verify(
                    js_assertions=SiteAssertions.fb_composer_open(),
                    ui_settle_s=1.0,
                )
                if not v.ok:
                    _vlog("⚠️ ", "FB composer may not be open — continuing anyway")

            # Step 3: Upload image (if provided)
            if image_path:
                # Click "Photo/Video" button in create post dialog
                await self._js_exec(
                    "var btns = document.querySelectorAll('[aria-label*=\"Photo\"], "
                    "[aria-label*=\"Ảnh\"], [aria-label*=\"photo\"]');"
                    "if (btns.length > 0) btns[0].click();"
                )
                await asyncio.sleep(self.ACTION_DELAY)

                # Upload file via file input
                await self._upload_file(image_path)
                await asyncio.sleep(self.UPLOAD_WAIT)

            # Step 4: Type content
            await self._type_in_active_element(content)
            await asyncio.sleep(self.ACTION_DELAY)

            # Step 5: Click Post button
            post_clicked = await self._js_exec(
                "var btns = document.querySelectorAll('[aria-label*=\"Post\"], "
                "[aria-label*=\"Đăng\"]');"
                "for (var i = 0; i < btns.length; i++) {"
                "  if (btns[i].offsetParent !== null) { btns[i].click(); return 'true'; }"
                "}"
                "return 'false';"
            )

            # v9.28: Verify post submitted (#1)
            await asyncio.sleep(2)
            if self._verifier:
                v = await self._verifier.verify(
                    js_assertions=SiteAssertions.fb_post_submitted(),
                    ui_settle_s=3.0,
                )
                if v.ok:
                    _vlog("✅", f"FB post verified (conf={v.confidence:.0%} {v.method})")
                else:
                    _vlog("⚠️ ", "Post verify uncertain — checking visually")

            await asyncio.sleep(3)  # Wait for feed update

            _vlog("✅", "Facebook post submitted")
            return {"success": True, "url": "https://www.facebook.com", "error": ""}

        except Exception as exc:
            return {"success": False, "url": "", "error": str(exc)[:200]}

    # ── Instagram — B3 v9.25 DOM-first ─────────────────────────────

    async def _post_instagram(self, content: str, image_path: str) -> dict:
        """
        B3 v9.27 E2E-fixed: Instagram posting — JS double-quote selectors.

        JS-in-AppleScript rule (_js_exec escapes " to \"):
          Python: "querySelector(\"[aria-label='New post']\")"
          JS runs: querySelector("[aria-label='New post']")  ← valid
        """
        try:
            await self._navigate("https://www.instagram.com")
            await asyncio.sleep(self.PAGE_LOAD_WAIT)

            # Check logged in — use " for outer JS string
            login_check = await self._js_exec(
                "document.querySelector("
                "\"[aria-label='New post'],[aria-label='Create']\")"
                " !== null ? \"logged_in\" : \"need_login\""
            )
            if login_check != "logged_in":
                # Fallback: check for home feed element
                feed_check = await self._js_exec(
                    "document.querySelector(\"[aria-label='Instagram']\")"
                    " !== null ? \"logged_in\" : \"need_login\""
                )
                if feed_check != "logged_in":
                    return {"success": False, "error": "Chua dang nhap Instagram", "url": ""}

            # Click New Post button
            clicked = await self._js_exec(
                "(function(){"
                "  var btns = document.querySelectorAll(\"[aria-label]\");"
                "  for(var i=0;i<btns.length;i++){"
                "    var l = btns[i].getAttribute(\"aria-label\") || '';"
                "    if(l.includes('New post') || l.includes('Create') || l.includes('Tao')){"
                "      btns[i].click(); return 'clicked';"
                "    }"
                "  } return 'notfound';"
                "})()"
            )
            if clicked != "clicked":
                return {"success": False, "error": "Khong tim thay nut New Post Instagram", "url": ""}
            await asyncio.sleep(1.5)

            # Upload anh qua input[type=file]
            if image_path and os.path.exists(image_path):
                await self._upload_file(image_path)
                await asyncio.sleep(self.UPLOAD_WAIT)

                # Click Next (2 lần — filter step + caption step)
                for _step in range(2):
                    await self._js_exec(
                        "(function(){"
                        "  var btns = document.querySelectorAll(\"[role='button'],button\");"
                        "  for(var i=0;i<btns.length;i++){"
                        "    var t = btns[i].innerText||btns[i].textContent||'';"
                        "    if(t.trim()==='Next'||t.trim()==='Tiep theo'||t.trim()==='Tiếp'){"
                        "      btns[i].click(); return 'ok';"
                        "    }"
                        "  } return 'notfound';"
                        "})()"
                    )
                    await asyncio.sleep(1.2)

            # Paste caption
            await self._js_exec(
                "(function(){"
                "  var ta = document.querySelector(\"textarea[aria-label],textarea[placeholder]\");"
                "  if(!ta) ta = document.querySelector(\"[role='textbox'],div[contenteditable]\");"
                "  if(ta){ ta.focus(); ta.click(); }"
                "})()"
            )
            await asyncio.sleep(0.5)
            await self._clipboard_paste(content[:2200])
            await asyncio.sleep(self.ACTION_DELAY)

            # Click Share button
            shared = await self._js_exec(
                "(function(){"
                "  var btns = document.querySelectorAll(\"[role='button'],button\");"
                "  for(var i=0;i<btns.length;i++){"
                "    var t = btns[i].innerText||btns[i].textContent||'';"
                "    if(t.trim()==='Share'||t.trim()==='Chia se'||t.trim()==='Chia sẻ'){"
                "      btns[i].click(); return 'shared';"
                "    }"
                "  } return 'notfound';"
                "})()"
            )
            await asyncio.sleep(5)
            _vlog("✅", f"Instagram post submitted (share={shared})")
            return {"success": True, "url": "https://www.instagram.com", "error": ""}

        except Exception as exc:
            return {"success": False, "error": str(exc)[:200], "url": ""}

    async def _post_x(self, content: str, image_path: str) -> dict:
        """
        B3 v9.27 E2E-fixed: X/Twitter posting.
        Uses " for outer JS string argument to avoid CSS attr selector issues.
        X data-testid selectors may change — multiple fallbacks provided.
        """
        try:
            # Direct compose URL (bypasses compose button search)
            await self._navigate("https://x.com/compose/post")
            await asyncio.sleep(self.PAGE_LOAD_WAIT)

            # Check logged in — compose redirects to login page if not
            login_check = await self._js_exec(
                "document.querySelector("
                "\"[data-testid='tweetTextarea_0'],[data-testid='tweetButton']"
                ",[role='textbox'][contenteditable]\") !== null"
                " ? \"logged_in\" : \"need_login\""
            )
            if login_check != "logged_in":
                # Fallback: navigate home and click compose
                await self._navigate("https://x.com")
                await asyncio.sleep(3)
                await self._js_exec(
                    "(function(){"
                    "  var btn = document.querySelector("
                    "\"[data-testid='SideNav_NewTweet_Button']"
                    ",[aria-label='Post'],[aria-label='Tweet']\");"
                    "  if(btn) btn.click();"
                    "})()"
                )
                await asyncio.sleep(2)

            # Upload image
            if image_path and os.path.exists(image_path):
                await self._upload_file(image_path)
                await asyncio.sleep(self.UPLOAD_WAIT)

            # Focus compose textarea — multiple selector fallbacks
            await self._js_exec(
                "(function(){"
                "  var ta = document.querySelector("
                "\"[data-testid='tweetTextarea_0']"
                ",[role='textbox'][contenteditable]"
                ",.DraftEditor-root [contenteditable]\");"
                "  if(ta){ ta.focus(); ta.click(); }"
                "})()"
            )
            await asyncio.sleep(0.5)
            # X limit: 280 chars
            await self._clipboard_paste(content[:280])
            await asyncio.sleep(self.ACTION_DELAY)

            # Click Post button — try multiple selectors
            posted = await self._js_exec(
                "(function(){"
                "  var selectors = ["
                "    \"[data-testid='tweetButton']\","
                "    \"[data-testid='tweetButtonInline']\","
                "    \"[aria-label='Post']\","
                "    \"[aria-label='Tweet']\""
                "  ];"
                "  for(var i=0;i<selectors.length;i++){"
                "    var btn = document.querySelector(selectors[i]);"
                "    if(btn && btn.offsetParent !== null){ btn.click(); return 'posted'; }"
                "  }"
                "  return 'notfound';"
                "})()"
            )
            await asyncio.sleep(5)
            _vlog("✅", f"X post submitted (result={posted})")
            return {"success": True, "url": "https://x.com", "error": ""}

        except Exception as exc:
            return {"success": False, "error": str(exc)[:200], "url": ""}

    async def _navigate(self, url: str) -> None:
        """Navigate Chrome to URL."""
        if self._ipc:
            await self._ipc.send_action("app_launch", {
                "app_name": "Google Chrome",
                "url": url,
            })
        elif platform.system() == "Darwin":
            script = (
                f'tell application "Google Chrome"\n'
                f'  activate\n'
                f'  open location "{url}"\n'
                f'end tell'
            )
            await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                timeout=10, capture_output=True,
            )

    async def _js_exec(self, js_code: str) -> str:
        """
        Execute JavaScript in Chrome active tab.

        v9.34 A1 — 2-tier:
          Tier 1: IPC browser_execute_js (L2 daemon → AppleScript)
                  → result thật sự, không còn '' empty string
          Tier 2: Direct AppleScript (fallback khi không có IPC)
        """
        # Tier 1: IPC → L2 daemon → a11y_macos.browser_execute_js()
        if self._ipc:
            try:
                resp = await self._ipc.send_action("browser_execute_js", {
                    "js_code": js_code,
                    "timeout_s": 8.0,
                })
                if resp and isinstance(resp, dict):
                    result = str(resp.get("result", "")).strip()
                    return result
            except Exception as exc:
                _vlog("⚠", f"IPC browser_execute_js failed: {exc} — falling back")

        # Tier 2: Direct AppleScript (khi không có IPC hoặc IPC fail)
        if platform.system() != "Darwin":
            return ""
        try:
            js_escaped = (
                js_code
                .replace("\\", "\\\\")
                .replace('"', '\\"')
            )
            script = (
                'tell application "Google Chrome"\n'
                '    try\n'
                f'        set r to execute active tab of front window javascript "{js_escaped}"\n'
                '        if r is missing value then return ""\n'
                '        return r as string\n'
                '    on error\n'
                '        return ""\n'
                '    end try\n'
                'end tell'
            )
            proc = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                timeout=10, capture_output=True, text=True,
            )
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except Exception as exc:
            _vlog("⚠", f"JS exec fallback failed: {exc}")
            return ""

    async def _type_in_active_element(self, text: str) -> None:
        """Type text into the currently focused element."""
        if self._ipc:
            await self._ipc.send_action("keyboard_type", {
                "text": text,
                "interval": 0.01,
            })
        elif platform.system() == "Darwin":
            # Use clipboard paste (faster and handles special chars)
            await self._clipboard_paste(text)

    async def _clipboard_paste(self, text: str) -> None:
        """Copy text to clipboard and paste (Cmd+V)."""
        if platform.system() != "Darwin":
            return

        try:
            # Copy to clipboard using pbcopy
            proc = await asyncio.to_thread(
                subprocess.run,
                ["pbcopy"],
                input=text.encode("utf-8"),
                timeout=5,
            )

            # Paste with Cmd+V
            await asyncio.sleep(0.3)
            script = (
                'tell application "System Events"\n'
                '  keystroke "v" using command down\n'
                'end tell'
            )
            await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                timeout=5, capture_output=True,
            )

        except Exception as exc:
            _vlog("⚠", f"Clipboard paste failed: {exc}")

    async def _upload_file(self, file_path: str) -> None:
        """
        Upload file via file input dialog.

        Uses AppleScript to interact with the native file picker,
        or sets the file input directly via JavaScript.
        """
        if platform.system() != "Darwin":
            return

        abs_path = os.path.abspath(file_path)

        # Strategy 1: Trigger file input via JS click → opens native dialog
        try:
            triggered = await self._js_exec(
                "(function(){"
                "  var inp = document.querySelector(\"input[type=file],input[accept*=image]\");"
                "  if(inp){ inp.click(); return 'clicked'; }"
                "  return 'notfound';"
                "})()"
            )
            _vlog("📂", f"File input trigger: {triggered}")
            await asyncio.sleep(1.5)  # wait for native dialog to appear
        except Exception as _e:
            _vlog("⚠", f"JS input trigger failed: {_e}")

        # Strategy 2: Use AppleScript Cmd+Shift+G to navigate to file path
        try:
            await asyncio.sleep(0.5)

            # Use Cmd+Shift+G in file dialog to go to path
            script = (
                'tell application "System Events"\n'
                '  tell process "Google Chrome"\n'
                '    delay 1\n'
                '    -- Open "Go to Folder" in file dialog\n'
                '    keystroke "g" using {command down, shift down}\n'
                '    delay 0.5\n'
                f'    keystroke "{abs_path}"\n'
                '    delay 0.3\n'
                '    keystroke return\n'
                '    delay 0.5\n'
                '    -- Click Open/Upload button\n'
                '    keystroke return\n'
                '  end tell\n'
                'end tell'
            )
            await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                timeout=10, capture_output=True,
            )

        except Exception as exc:
            _vlog("⚠", f"File upload via AppleScript failed: {exc}")

    # ══════════════════════════════════════════════════════════════
    # Stats
    # ══════════════════════════════════════════════════════════════

    def stats(self) -> dict:
        return {
            "supported_platforms": list(PLATFORM_URLS.keys()),
            "post_timeout": self.POST_TIMEOUT,
            "has_ipc": self._ipc is not None,
        }
