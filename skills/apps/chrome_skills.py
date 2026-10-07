# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/apps/chrome_skills.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

P2 Option 3: Chrome General Skills

Thực hiện mọi tác vụ web trong Chrome — không chỉ ChatGPT/Facebook/Gemini.
Kết hợp CDP (Chrome DevTools Protocol) qua browser_execute_js với
VisionActor.find_and_click_v2 cho các trang phức tạp.

Actions:
  navigate(url)                        → bool
  search_google(query)                 → list[SearchResult]
  get_page_text(url)                   → str
  get_page_title()                     → str
  get_current_url()                    → str
  fill_form(selector, value)           → bool
  click_element(selector_or_label)     → bool
  download_file(url, dest)             → str local_path
  take_screenshot(path)                → str path
  extract_links()                      → list[dict]
  get_selected_text()                  → str
  scroll_page(direction, amount)       → bool
  wait_for_element(selector, timeout)  → bool
  run_js(code)                         → str result
  close_tab()                          → bool
  open_new_tab(url)                    → bool
  get_cookies()                        → list[dict]
  read_page_table()                    → list[list]

Entry: run_chrome_task(goal, ipc, notify_fn) → ChromeResult
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;34m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Result types
# ══════════════════════════════════════════════════════════════════

@dataclass
class ChromeResult:
    success:    bool
    action:     str  = ""
    output:     Any  = None
    error:      str  = ""
    url:        str  = ""
    duration_ms:int  = 0

    @property
    def summary(self) -> str:
        if self.success:
            if isinstance(self.output, str):
                return f"✅ {self.action}: {self.output[:80]}"
            if isinstance(self.output, list):
                return f"✅ {self.action}: {len(self.output)} items"
            return f"✅ {self.action}"
        return f"❌ {self.action}: {self.error[:80]}"


@dataclass
class SearchResult:
    title:   str = ""
    url:     str = ""
    snippet: str = ""


# ══════════════════════════════════════════════════════════════════
# JS helpers
# ══════════════════════════════════════════════════════════════════

async def _js(ipc_client, code: str, timeout: float = 8.0) -> str:
    """Execute JS in Chrome active tab. CDP first, IPC/AppleScript fallback."""
    # ── FIX v1.0: Try CDP first (cross-platform, faster) ────
    try:
        from automation.chrome_cdp import get_cdp_sync
        cdp = get_cdp_sync()
        if cdp and cdp.connected:
            result = await cdp.execute_js(code, timeout=timeout)
            if result:
                return result
    except Exception:
        pass

    # ── Fallback: IPC → daemon → AppleScript ──────────────────
    if not ipc_client:
        return ""
    try:
        resp = await ipc_client.send_action("browser_execute_js", {
            "js_code": code,
            "timeout_s": timeout,
        })
        if resp is None:
            return ""
        # FIX v4.3: IPC now reports the real success flag; a failed JS call
        # must not return its error text as if it were the page value.
        if hasattr(resp, "success") and not resp.success:
            return ""
        if hasattr(resp, "result"):
            result = resp.result
        elif isinstance(resp, dict):
            if not resp.get("success", True):
                return ""
            result = resp.get("result", resp.get("data", ""))
        else:
            result = str(resp)
        return str(result).strip() if result is not None else ""
    except Exception:
        return ""


async def _navigate(ipc_client, url: str) -> bool:
    """Navigate Chrome to URL. CDP first, IPC fallback."""
    # ── FIX v1.0: Try CDP first ──────────────────────────────
    try:
        from automation.chrome_cdp import get_cdp_sync
        cdp = get_cdp_sync()
        if cdp and cdp.connected:
            result = await cdp.navigate(url)
            if result.get("success", True):
                return True
    except Exception:
        pass

    # ── Fallback: IPC → daemon ────────────────────────────────
    if not ipc_client:
        return False
    try:
        resp = await ipc_client.send_action("browser_navigate", {"url": url})
        return getattr(resp, "success", False)
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════
# ChromeSkills
# ══════════════════════════════════════════════════════════════════

class ChromeSkills:
    """
    Tất cả thao tác Chrome/web trên macOS.
    Dùng browser_execute_js (đã build trong A1) làm backbone.
    """

    def __init__(self, ipc_client: Any = None) -> None:
        self._ipc = ipc_client

    # ── Navigation ───────────────────────────────────────────────

    async def navigate(self, url: str) -> ChromeResult:
        """Navigate đến URL."""
        t0 = time.time()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        ok = await _navigate(self._ipc, url)
        if ok:
            await asyncio.sleep(2.0)  # wait for page load
        _vlog("🌐", f"navigate: {url} {'✅' if ok else '❌'}")
        return ChromeResult(ok, "navigate", output=url, url=url,
                            duration_ms=int((time.time()-t0)*1000))

    async def get_current_url(self) -> ChromeResult:
        """Lấy URL trang hiện tại."""
        t0 = time.time()
        url = await _js(self._ipc, "window.location.href")
        return ChromeResult(bool(url), "get_current_url", output=url, url=url,
                            duration_ms=int((time.time()-t0)*1000))

    async def get_page_title(self) -> ChromeResult:
        """Lấy tiêu đề trang."""
        t0 = time.time()
        title = await _js(self._ipc, "document.title")
        return ChromeResult(bool(title), "get_page_title", output=title,
                            duration_ms=int((time.time()-t0)*1000))

    async def open_new_tab(self, url: str = "") -> ChromeResult:
        """Mở tab mới."""
        t0 = time.time()
        script = (
            'tell application "Google Chrome"\n'
            '  tell front window\n'
            '    set newTab to make new tab\n'
            f'    if "{url}" is not "" then set URL of newTab to "{url}"\n'
            '  end tell\n'
            '  activate\n'
            'end tell'
        )
        try:
            proc = await asyncio.to_thread(
                subprocess.run, ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5.0,
            )
            ok = proc.returncode == 0
            if ok and url:
                await asyncio.sleep(2.0)
            return ChromeResult(ok, "open_new_tab", output=url,
                                duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return ChromeResult(False, "open_new_tab", error=str(exc))

    async def close_tab(self) -> ChromeResult:
        """Đóng tab hiện tại."""
        t0 = time.time()
        result = await _js(self._ipc, "window.close()")
        return ChromeResult(True, "close_tab", duration_ms=int((time.time()-t0)*1000))

    # ── Search ───────────────────────────────────────────────────

    async def search_google(
        self,
        query: str,
        num_results: int = 5,
    ) -> ChromeResult:
        """
        Search Google và trả về results với title, url, snippet.
        Dùng Google Search JSON API public endpoint.
        """
        t0 = time.time()
        try:
            encoded = query.replace(" ", "+").replace('"', "%22")
            await _navigate(self._ipc, f"https://www.google.com/search?q={encoded}&num={num_results}")
            await asyncio.sleep(2.5)

            # Extract results via JS
            js = """
            (() => {
                const results = [];
                const cards = document.querySelectorAll('div.g, div[data-sokoban-feature]');
                let count = 0;
                for (const card of cards) {
                    if (count >= """ + str(num_results) + """) break;
                    const a = card.querySelector('a[href^="http"]');
                    const h3 = card.querySelector('h3');
                    const snip = card.querySelector('.VwiC3b, .s3v9rd, span[data-content]');
                    if (a && h3) {
                        results.push({
                            title: h3.innerText.trim(),
                            url: a.href,
                            snippet: snip ? snip.innerText.trim().substring(0, 200) : ''
                        });
                        count++;
                    }
                }
                return JSON.stringify(results);
            })()
            """
            raw = await _js(self._ipc, js)
            items = json.loads(raw) if raw and raw.startswith("[") else []
            results = [SearchResult(**item) for item in items]
            ms = int((time.time()-t0)*1000)
            _vlog("🔍", f"search_google '{query}': {len(results)} results ({ms}ms)")
            return ChromeResult(True, "search_google",
                                output=results, url=f"https://google.com/search?q={encoded}",
                                duration_ms=ms)
        except Exception as exc:
            return ChromeResult(False, "search_google", error=str(exc),
                                duration_ms=int((time.time()-t0)*1000))

    # ── Content extraction ───────────────────────────────────────

    async def get_page_text(
        self,
        url: str = "",
        max_chars: int = 3000,
    ) -> ChromeResult:
        """
        Lấy text content của trang (hoặc navigate đến URL rồi lấy).
        """
        t0 = time.time()
        if url:
            nav_result = await self.navigate(url)
            if not nav_result.success:
                return ChromeResult(False, "get_page_text", error=f"Navigate failed: {url}")

        js = f"""
        (() => {{
            // Remove script, style, nav elements
            const clone = document.body.cloneNode(true);
            ['script','style','nav','header','footer'].forEach(tag => {{
                clone.querySelectorAll(tag).forEach(el => el.remove());
            }});
            const text = clone.innerText || clone.textContent || '';
            return text.replace(/\\s+/g, ' ').trim().substring(0, {max_chars});
        }})()
        """
        text = await _js(self._ipc, js)
        current_url = await _js(self._ipc, "window.location.href")
        _vlog("📄", f"get_page_text: {len(text)} chars")
        return ChromeResult(bool(text), "get_page_text", output=text, url=current_url,
                            duration_ms=int((time.time()-t0)*1000))

    async def extract_links(self, filter_domain: str = "") -> ChromeResult:
        """Lấy tất cả links trong trang."""
        t0 = time.time()
        domain_filter = f'&& a.href.includes("{filter_domain}")' if filter_domain else ""
        js = f"""
        (() => {{
            const links = [];
            document.querySelectorAll('a[href^="http"]{domain_filter}').forEach(a => {{
                if (links.length < 50) links.push({{text: a.innerText.trim().substring(0,80), url: a.href}});
            }});
            return JSON.stringify(links);
        }})()
        """
        raw = await _js(self._ipc, js)
        items = json.loads(raw) if raw and raw.startswith("[") else []
        return ChromeResult(True, "extract_links", output=items,
                            duration_ms=int((time.time()-t0)*1000))

    async def read_page_table(self, table_index: int = 0) -> ChromeResult:
        """Đọc nội dung bảng (table) trong trang."""
        t0 = time.time()
        js = f"""
        (() => {{
            const tables = document.querySelectorAll('table');
            if (tables.length <= {table_index}) return '[]';
            const table = tables[{table_index}];
            const rows = [];
            table.querySelectorAll('tr').forEach(tr => {{
                const cells = [];
                tr.querySelectorAll('td,th').forEach(td => cells.push(td.innerText.trim()));
                if (cells.length) rows.push(cells);
            }});
            return JSON.stringify(rows);
        }})()
        """
        raw = await _js(self._ipc, js)
        rows = json.loads(raw) if raw and raw.startswith("[") else []
        _vlog("📊", f"read_table: {len(rows)} rows")
        return ChromeResult(bool(rows), "read_page_table", output=rows,
                            duration_ms=int((time.time()-t0)*1000))

    async def get_selected_text(self) -> ChromeResult:
        """Lấy text đang được select."""
        t0 = time.time()
        text = await _js(self._ipc, "window.getSelection().toString()")
        return ChromeResult(bool(text), "get_selected_text", output=text,
                            duration_ms=int((time.time()-t0)*1000))

    # ── Interaction ──────────────────────────────────────────────

    async def fill_form(
        self,
        selector: str,
        value: str,
        clear_first: bool = True,
    ) -> ChromeResult:
        """
        Điền giá trị vào form field.

        Examples:
            fill_form('input[name="email"]', "user@example.com")
            fill_form('#search', "machine learning")
        """
        t0 = time.time()
        clear_js = f"document.querySelector('{selector}').value = '';" if clear_first else ""
        js = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            if (!el) return 'not_found';
            el.focus();
            {clear_js}
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            return 'ok';
        }})()
        """
        result = await _js(self._ipc, js)
        ok = result == "ok"
        _vlog("✏️", f"fill_form '{selector}': {'✅' if ok else '❌ not_found'}")
        return ChromeResult(ok, "fill_form", output=value,
                            error="" if ok else f"Element not found: {selector}",
                            duration_ms=int((time.time()-t0)*1000))

    async def click_element(
        self,
        selector_or_label: str,
        by_text: bool = False,
    ) -> ChromeResult:
        """
        Click element theo CSS selector hoặc text label.

        Examples:
            click_element('button[type="submit"]')
            click_element("Submit", by_text=True)
            click_element("Search", by_text=True)
        """
        t0 = time.time()
        if by_text or not (selector_or_label.startswith(("#", ".", "[", "button",
                                                          "input", "a", "div"))):
            # CRIT-03 FIX: encode text as JSON to properly escape Unicode Vietnamese characters
            # (e.g. "Đăng ký", "Thêm vào giỏ"). Old approach using .replace('"', '\\"') failed
            # because JS template-literal injection left multi-byte UTF-8 unescaped.
            # Also added: null-check before click (return 'not_found' → success=False instead of
            # silent miss), and auto-wait + retry if element not found on first attempt.
            import json as _json_mod
            text_json = _json_mod.dumps(selector_or_label.lower())  # e.g. "đăng ký"
            js = f"""
            (() => {{
                const text = {text_json};
                const candidates = [...document.querySelectorAll(
                    'button,a,input[type="submit"],input[type="button"],[role="button"]'
                )];
                // Also search shadow DOM roots one level deep
                document.querySelectorAll('*').forEach(host => {{
                    if (host.shadowRoot) {{
                        host.shadowRoot.querySelectorAll(
                            'button,a,input[type="submit"],input[type="button"],[role="button"]'
                        ).forEach(el => candidates.push(el));
                    }}
                }});
                const el = candidates.find(e =>
                    (e.innerText || '').toLowerCase().includes(text) ||
                    (e.value || '').toLowerCase().includes(text) ||
                    (e.getAttribute('aria-label') || '').toLowerCase().includes(text)
                );
                if (!el) return 'not_found';
                el.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
                el.click();
                return 'ok';
            }})()
            """
        else:
            escaped = selector_or_label.replace("'", "\\'")
            js = f"""
            (() => {{
                const el = document.querySelector('{escaped}');
                if (!el) return 'not_found';
                el.click();
                return 'ok';
            }})()
            """

        result = await _js(self._ipc, js)
        # CRIT-03 FIX: if element not found on SPA (React/Vue), wait 2s for render then retry once
        if result == "not_found":
            await asyncio.sleep(2.0)
            result = await _js(self._ipc, js)
        ok = result == "ok"
        _vlog("🖱️", f"click_element '{selector_or_label}': {'✅' if ok else '❌'}")
        return ChromeResult(ok, "click_element",
                            error="" if ok else f"Element not found: {selector_or_label}",
                            duration_ms=int((time.time()-t0)*1000))

    async def scroll_page(
        self,
        direction: str = "down",
        amount: int = 500,
    ) -> ChromeResult:
        """Scroll trang."""
        t0 = time.time()
        dy = amount if direction == "down" else -amount
        await _js(self._ipc, f"window.scrollBy(0, {dy})")
        return ChromeResult(True, "scroll_page",
                            duration_ms=int((time.time()-t0)*1000))

    async def wait_for_element(
        self,
        selector: str,
        timeout: float = 10.0,
    ) -> ChromeResult:
        """Chờ element xuất hiện trong DOM."""
        t0 = time.time()
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = await _js(self._ipc,
                               f"document.querySelector('{selector}') !== null ? 'found' : 'not_found'")
            if result == "found":
                ms = int((time.time()-t0)*1000)
                return ChromeResult(True, "wait_for_element", output=selector, duration_ms=ms)
            await asyncio.sleep(0.5)
        return ChromeResult(False, "wait_for_element",
                            error=f"Timeout {timeout}s: {selector}",
                            duration_ms=int((time.time()-t0)*1000))

    # ── Screenshot / Download ────────────────────────────────────

    async def take_screenshot(self, output_path: str = "") -> ChromeResult:
        """Chụp screenshot trang hiện tại."""
        t0 = time.time()
        if not output_path:
            ts = int(time.time())
            output_path = str(Path.home() / "Desktop" / f"screenshot_{ts}.png")
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["screencapture", "-x", output_path],
                capture_output=True, timeout=5.0,
            )
            ok = proc.returncode == 0 and os.path.exists(output_path)
            _vlog("📸", f"screenshot: {output_path} {'✅' if ok else '❌'}")
            return ChromeResult(ok, "take_screenshot", output=output_path,
                                duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return ChromeResult(False, "take_screenshot", error=str(exc))

    async def run_js(self, code: str) -> ChromeResult:
        """Thực thi JS tùy ý trong trang (dùng thận trọng)."""
        t0 = time.time()
        result = await _js(self._ipc, code)
        return ChromeResult(True, "run_js", output=result,
                            duration_ms=int((time.time()-t0)*1000))


# ══════════════════════════════════════════════════════════════════
# Entry point cho SmartRouter
# ══════════════════════════════════════════════════════════════════

_CHROME = None  # Lazy init (cần ipc_client)


async def run_chrome_task(
    goal: str,
    ipc_client: Any = None,
    notify_fn: Any = None,
) -> ChromeResult:
    """
    Entry point cho SmartRouter Fast Lane (web_task intent).
    Parse goal → gọi đúng ChromeSkills method.

    Examples handled:
        "mở google.com"
        "tìm kiếm thông tin về machine learning trên google"
        "lấy nội dung trang web https://example.com"
        "chụp screenshot trang hiện tại"
        "cuộn xuống dưới"
        "lấy tiêu đề trang"
        "click nút Submit"
        "đọc bảng dữ liệu trong trang"
    """
    skills = ChromeSkills(ipc_client=ipc_client)
    gl     = goal.lower()
    url    = _extract_url(goal)

    # ══════════════════════════════════════════════════════════════
    # FIX v1.0: Smart routing — detect intent by COMBINATION of signals
    # instead of first-keyword-wins. Priority:
    #   1. URL + content verbs → get_page_text (most specific)
    #   2. URL + navigate verbs → navigate
    #   3. No URL + search verbs → search_google
    #   4. Specific UI actions (click, scroll, screenshot)
    #   5. Fallback: URL → navigate, no URL → search
    # ══════════════════════════════════════════════════════════════

    _CONTENT_VERBS = ("nội dung", "lấy nội dung", "đọc nội dung", "xem nội dung",
                      "lấy text", "đọc trang", "read page", "get content", "get text",
                      "extract text", "extract content", "đọc bài", "xem bài",
                      "lấy bài viết", "content of", "text of")
    _NAV_VERBS     = ("mở", "vào", "navigate", "go to", "open", "truy cập", "đến")
    _SEARCH_VERBS  = ("tìm kiếm", "search", "google", "tìm trên", "tra cứu", "look up")

    has_content_verb = any(k in gl for k in _CONTENT_VERBS)
    has_nav_verb     = any(k in gl for k in _NAV_VERBS)
    has_search_verb  = any(k in gl for k in _SEARCH_VERBS)

    # ── 1. Content extraction (URL + content verb) ────────────────
    if has_content_verb or (url and any(k in gl for k in ("nội dung", "content", "text", "đọc", "lấy", "read", "get", "xem"))):
        result = await skills.get_page_text(url)

    # ── 2. Navigate (URL + nav verb, or just URL without content) ─
    elif url and (has_nav_verb or not has_search_verb):
        result = await skills.navigate(url)

    # ── 3. Search (search verb without URL) ───────────────────────
    elif has_search_verb:
        query = _extract_search_query(goal)
        result = await skills.search_google(query)

    # ── 4. Screenshot ─────────────────────────────────────────────
    elif any(k in gl for k in ("screenshot", "chụp màn hình", "chụp trang", "screen capture")):
        result = await skills.take_screenshot()

    # ── 5. Scroll ─────────────────────────────────────────────────
    elif any(k in gl for k in ("scroll", "cuộn")):
        direction = "up" if any(k in gl for k in ("lên", "up", "trên")) else "down"
        result = await skills.scroll_page(direction)

    # ── 6. Click ──────────────────────────────────────────────────
    elif any(k in gl for k in ("click", "bấm", "nhấn")):
        label = _extract_element_label(goal)
        result = await skills.click_element(label, by_text=True)

    # ── 7. Table ──────────────────────────────────────────────────
    elif any(k in gl for k in ("bảng", "table", "dữ liệu bảng")):
        result = await skills.read_page_table()

    # ── 8. Links ──────────────────────────────────────────────────
    elif any(k in gl for k in ("link", "liên kết", "extract links", "lấy link")):
        result = await skills.extract_links()

    # ── 9. Page title ─────────────────────────────────────────────
    elif any(k in gl for k in ("tiêu đề", "title", "tên trang")):
        result = await skills.get_page_title()

    # ── 10. Current URL ───────────────────────────────────────────
    elif any(k in gl for k in ("url hiện tại", "current url", "đang ở đâu", "đang ở trang")):
        result = await skills.get_current_url()

    # ── 11. New tab ───────────────────────────────────────────────
    elif any(k in gl for k in ("tab mới", "new tab", "mở tab")):
        url2 = url or ""
        result = await skills.open_new_tab(url2)

    # ── Fallback ──────────────────────────────────────────────────
    else:
        # FIX v1.0: Try qwen3:4b for ambiguous web tasks
        _llm_parsed = False
        try:
            from core.llm_intent_parser import get_intent_parser
            parser = get_intent_parser()
            gi = await asyncio.wait_for(parser.parse(goal), timeout=3.0)
            if gi and gi.confidence >= 0.5:
                if gi.action == "get_content" and (gi.url or url):
                    result = await skills.get_page_text(gi.url or url)
                    _llm_parsed = True
                elif gi.action == "search_web" and gi.query:
                    result = await skills.search_google(gi.query)
                    _llm_parsed = True
                elif gi.action == "navigate" and (gi.url or url):
                    result = await skills.navigate(gi.url or url)
                    _llm_parsed = True
        except Exception:
            pass

        if not _llm_parsed:
            if url:
                result = await skills.navigate(url)
            else:
                result = await skills.search_google(goal)

    # Notify Telegram
    if notify_fn:
        try:
            if result.success:
                output = result.output
                action = result.action

                if action == "search_google" and isinstance(output, list):
                    # Search results — format as list
                    lines = []
                    for r in output[:5]:
                        t = getattr(r, "title", str(r)) if not isinstance(r, dict) else r.get("title", "")
                        u = getattr(r, "url", "") if not isinstance(r, dict) else r.get("url", "")
                        s = getattr(r, "snippet", "") if not isinstance(r, dict) else r.get("snippet", "")
                        lines.append(f"*{t}*\n{u}\n_{s[:80]}_")
                    msg = f"🔍 *Kết quả tìm kiếm:*\n\n" + "\n\n".join(lines)

                elif action == "get_page_text" and isinstance(output, str) and len(output) > 20:
                    # FIX v1.0: Nội dung trang web — escape Markdown, truncate
                    url_info = f"🌐 *{result.url}*\n\n" if result.url else ""
                    # Escape Markdown special chars trong page text
                    safe_text = output[:1800].replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
                    msg = f"📄 *Nội dung trang web:*\n{url_info}{safe_text}"
                    if len(output) > 1800:
                        msg += f"\n\n_...({len(output)} ký tự, đã cắt gọn)_"

                elif isinstance(output, str) and len(output) > 20:
                    msg = f"✅ *{action}*\n\n{output[:1500]}"
                elif isinstance(output, list):
                    msg = f"✅ *{action}*: {len(output)} items"
                else:
                    msg = f"✅ *{action}*"

                # Telegram message limit 4096 chars
                if len(msg) > 4000:
                    msg = msg[:3950] + "\n\n_...(đã cắt gọn)_"

                await notify_fn(msg)
            else:
                await notify_fn(f"❌ *{result.action}* thất bại: {result.error[:200]}")
        except Exception:
            pass

    _vlog("🌐", f"ChromeTask: {result.summary}")
    return result


# ── Parsing helpers ───────────────────────────────────────────────

def _extract_url(text: str) -> str:
    """Tìm URL trong text."""
    m = re.search(r'https?://[^\s"\']+', text)
    if m:
        return m.group()
    # Tìm domain-like string
    m = re.search(r'\b([\w-]+\.(com|net|org|io|vn|co|dev|app)[\w/.-]*)\b', text)
    if m:
        return "https://" + m.group()
    return ""

def _extract_search_query(text: str) -> str:
    """Lấy query từ câu tìm kiếm."""
    text = re.sub(r'(tìm kiếm|search|google|tìm)\s+', "", text.lower()).strip()
    text = re.sub(r'\s+(trên google|on google|bằng google)', "", text).strip()
    return text[:200] or text

def _extract_element_label(text: str) -> str:
    """Lấy label element từ câu click."""
    m = re.search(r'(?:click|bấm|nhấn)\s+(?:nút\s+)?["\']?([^"\']+)["\']?', text, re.I)
    return m.group(1).strip()[:50] if m else text[:50]
