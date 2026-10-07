# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
automation/chrome_scraper.py — Phidipus ChromeScraper v1.0
═══════════════════════════════════════════════════════════

Mở Chrome, search sản phẩm, extract giá từ nhiều trang web.
Dùng IPC (không trực tiếp gọi pyautogui) → an toàn.

Tính năng:
  - search_prices(query)  → giá từ Shopee, Lazada, Google Shopping, web thường
  - search_products(query) → danh sách sản phẩm + link + giá
  - screenshot()          → chụp kết quả để đính kèm Telegram

Chiến lược scraping (không cần Selenium/Playwright):
  1. Google Shopping → URL trực tiếp, parse từ redirect
  2. Requests + BeautifulSoup nếu available (offline)
  3. Chrome screenshot + VLM parse nếu cần (heavy, optional)

Default: dùng Python requests qua IPC subprocess — nhanh nhất,
không cần Chrome khởi động cho mỗi query đơn giản.
Chrome chỉ mở khi cần screenshot hoặc JavaScript-heavy sites.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


# ══════════════════════════════════════════════════════════════
# Data types
# ══════════════════════════════════════════════════════════════

@dataclass
class WebPrice:
    """Giá từ một nguồn web."""
    site:        str
    title:       str
    price:       float
    price_text:  str
    currency:    str = "VND"
    url:         str = ""
    in_stock:    bool = True
    rating:      str = ""
    source_type: str = "scrape"   # scrape | api | cache

@dataclass
class WebSearchResult:
    """Kết quả tìm kiếm web."""
    title:      str
    url:        str
    price_text: str = ""
    price:      float = 0.0
    snippet:    str = ""
    site:       str = ""


# ══════════════════════════════════════════════════════════════
# Chrome Scraper
# ══════════════════════════════════════════════════════════════

class ChromeScraper:
    """
    Web price scraper không cần Selenium.
    Dùng urllib (stdlib) để fetch trang, parse HTML bằng regex + simple parser.

    Khi cần JavaScript (Shopee/Lazada SPA):
    → Dùng IPC để gửi lệnh cho Chrome profile đang chạy
    → Fallback về Google Cache nếu không mở được Chrome

    Usage:
        scraper = ChromeScraper()
        prices = await scraper.search_prices("đèn LED XYZ 12W")
        # → [WebPrice(site="Google Shopping", price=85000, ...), ...]
    """

    # Sites được hỗ trợ và search URL template
    _SEARCH_TEMPLATES = {
        "Google Shopping": "https://www.google.com/search?q={q}+giá+mua&tbm=shop",
        "Shopee":          "https://shopee.vn/search?keyword={q}",
        "Lazada":          "https://www.lazada.vn/catalog/?q={q}",
        "Tiki":            "https://tiki.vn/search?q={q}",
    }

    _USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        max_concurrent: int = 3,
        ipc_client: Any = None,   # inject IPCClient nếu cần mở Chrome thật
    ) -> None:
        self._timeout = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._ipc = ipc_client
        self._cache: dict[str, tuple[float, list]] = {}  # query → (ts, results)
        self._cache_ttl = 300  # 5 phút

    # ── Public API ─────────────────────────────────────────────────────

    async def search_prices(
        self, query: str, max_sites: int = 4
    ) -> list[WebPrice]:
        """
        Tìm giá sản phẩm từ nhiều nguồn web.
        Trả về danh sách WebPrice, sắp xếp theo giá tăng dần.
        """
        # Check cache
        cache_key = f"prices:{query.lower()[:80]}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached

        tasks = [
            self._fetch_google_shopping(query),
            self._fetch_site_price("Tiki", f"https://tiki.vn/search?q={urllib.parse.quote(query)}", query),
        ]
        results_nested = await asyncio.gather(*tasks, return_exceptions=True)
        prices: list[WebPrice] = []
        for r in results_nested:
            if isinstance(r, list):
                prices.extend(r)

        # Deduplicate và sort
        prices = _dedup_prices(prices)
        prices.sort(key=lambda p: p.price if p.price > 0 else float("inf"))

        self._set_cache(cache_key, prices)
        return prices[:max_sites * 2]

    async def search_products(
        self, query: str, max_results: int = 5
    ) -> list[WebSearchResult]:
        """
        Tìm kiếm sản phẩm trên web (general search).
        Trả về danh sách link + title + snippet.
        """
        cache_key = f"products:{query.lower()[:80]}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached

        results = await self._fetch_google_general(query, max_results)
        self._set_cache(cache_key, results)
        return results

    async def open_url_in_chrome(self, url: str) -> bool:
        """Mở URL trong Chrome qua IPC (không block)."""
        if not self._ipc:
            return False
        try:
            from ipc.action_schema import make_request
            msg = make_request("browser_navigate", {"url": url})
            resp = await self._ipc.send_action(msg)
            return resp.success if resp else False
        except Exception:
            return False

    # ── Scrapers ────────────────────────────────────────────────────────

    async def _fetch_google_shopping(self, query: str) -> list[WebPrice]:
        """Parse Google Shopping results."""
        url = f"https://www.google.com/search?q={urllib.parse.quote(query + ' giá mua')}&tbm=shop&hl=vi"
        html = await self._fetch_html(url)
        if not html:
            return []

        prices = []
        # Extract tên + giá từ Google Shopping HTML
        # Pattern: tên sản phẩm và giá trong các tag kết quả shopping
        items = re.findall(
            r'class="[^"]*sh-dgr__grid-result[^"]*"[^>]*>(.*?)</div>\s*</div>',
            html, re.DOTALL
        )
        if not items:
            # Fallback pattern cho Google Shopping structure khác
            items = re.findall(r'"name":"([^"]{5,100})","price":(\d+)', html)
            for name, price_str in items[:5]:
                prices.append(WebPrice(
                    site="Google Shopping",
                    title=_clean_text(name),
                    price=float(price_str),
                    price_text=_fmt_price_vi(float(price_str)),
                    url=f"https://www.google.com/search?q={urllib.parse.quote(query)}",
                ))
            return prices

        for item in items[:5]:
            name_m = re.search(r'<h3[^>]*>([^<]+)</h3>', item)
            price_m = re.search(r'([\d.,]+)\s*(₫|đ|VND|vnd)', item)
            link_m = re.search(r'href="(/shopping/product/[^"]+)"', item)
            if name_m and price_m:
                price_val = _parse_price_text(price_m.group(0))
                prices.append(WebPrice(
                    site="Google Shopping",
                    title=_clean_text(name_m.group(1)),
                    price=price_val,
                    price_text=price_m.group(0).strip()[:30],
                    url="https://www.google.com" + (link_m.group(1) if link_m else ""),
                ))

        return prices

    async def _fetch_site_price(
        self, site_name: str, url: str, query: str
    ) -> list[WebPrice]:
        """Generic scraper cho các site thương mại điện tử."""
        html = await self._fetch_html(url)
        if not html:
            return []

        prices = []
        # Extract giá từ schema.org JSON-LD (chuẩn nhất, nhiều site dùng)
        ld_blocks = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
        for block in ld_blocks[:5]:
            try:
                data = json.loads(block)
                if isinstance(data, list):
                    data = data[0]
                if data.get("@type") in ("Product", "ItemList"):
                    offers = data.get("offers") or data.get("Offers") or {}
                    if isinstance(offers, dict):
                        offers = [offers]
                    for offer in (offers if isinstance(offers, list) else []):
                        price_val = float(offer.get("price", 0))
                        if price_val > 0:
                            prices.append(WebPrice(
                                site=site_name,
                                title=_clean_text(data.get("name", query)[:80]),
                                price=price_val,
                                price_text=_fmt_price_vi(price_val),
                                url=url,
                                source_type="schema.org",
                            ))
            except Exception:
                pass

        if prices:
            return prices

        # Fallback: regex tìm pattern giá tiền trong HTML
        price_patterns = [
            r'class="[^"]*price[^"]*"[^>]*>([\d.,]+\s*[₫đ])',
            r'"price":\s*"?([\d.,]+)"?',
            r'([\d.,]+)\s*₫',
        ]
        for pat in price_patterns:
            matches = re.findall(pat, html, re.IGNORECASE)
            for m in matches[:3]:
                price_val = _parse_price_text(m)
                if price_val > 1000:  # filter noise
                    prices.append(WebPrice(
                        site=site_name,
                        title=query[:60],
                        price=price_val,
                        price_text=_fmt_price_vi(price_val),
                        url=url,
                    ))
            if prices:
                break

        return prices[:3]

    async def _fetch_google_general(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        """General Google search results."""
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=vi&num={max_results + 5}"
        html = await self._fetch_html(url)
        if not html:
            return []

        results = []
        # Parse organic search results
        blocks = re.findall(
            r'<div class="[^"]*g[^"]*"[^>]*>.*?<a href="(/url\?q=[^"&]+|https?://[^"]+)"[^>]*>'
            r'<h3[^>]*>([^<]+)</h3>',
            html, re.DOTALL
        )
        for url_raw, title in blocks[:max_results]:
            actual_url = url_raw
            if url_raw.startswith("/url?q="):
                actual_url = urllib.parse.unquote(url_raw[7:].split("&")[0])

            # Extract snippet
            site = urllib.parse.urlparse(actual_url).netloc.replace("www.", "")

            # Find price in title
            price_m = re.search(r'([\d.,]+)\s*(₫|đ|VND)', title)
            price = _parse_price_text(price_m.group(0)) if price_m else 0.0

            results.append(WebSearchResult(
                title=_clean_text(title),
                url=actual_url,
                price=price,
                price_text=_fmt_price_vi(price) if price > 0 else "",
                site=site,
            ))

        return results

    async def _fetch_html(self, url: str) -> str:
        """Fetch HTML từ URL, timeout, với User-Agent giả lập browser."""
        async with self._semaphore:
            loop = asyncio.get_event_loop()

            def _do_fetch():
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent":      self._USER_AGENT,
                        "Accept":          "text/html,application/xhtml+xml,*/*",
                        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
                        "Accept-Encoding": "identity",
                    }
                )
                try:
                    with urllib.request.urlopen(req, timeout=int(self._timeout)) as resp:
                        content_type = resp.headers.get("Content-Type", "")
                        charset = "utf-8"
                        if "charset=" in content_type:
                            charset = content_type.split("charset=")[-1].split(";")[0].strip()
                        return resp.read().decode(charset, errors="replace")
                except Exception:
                    return ""

            try:
                html = await asyncio.wait_for(
                    loop.run_in_executor(None, _do_fetch),
                    timeout=self._timeout,
                )
                return html
            except asyncio.TimeoutError:
                return ""

    # ── Cache ──────────────────────────────────────────────────────────

    def _get_cache(self, key: str) -> list | None:
        entry = self._cache.get(key)
        if entry and (time.time() - entry[0]) < self._cache_ttl:
            return entry[1]
        return None

    def _set_cache(self, key: str, data: list) -> None:
        if len(self._cache) > 200:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[key] = (time.time(), data)


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _parse_price_text(text: str) -> float:
    s = re.sub(r"[^\d.,]", "", text).replace(",", "")
    try:
        v = float(s)
        return v if v > 100 else 0.0
    except ValueError:
        return 0.0

def _fmt_price_vi(price: float) -> str:
    if price <= 0:
        return "Liên hệ"
    if price >= 1_000_000:
        return f"{price/1_000_000:.1f}M ₫"
    if price >= 1_000:
        return f"{price/1_000:.0f}K ₫"
    return f"{price:.0f} ₫"

def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]

def _dedup_prices(prices: list[WebPrice]) -> list[WebPrice]:
    seen: set[tuple] = set()
    out = []
    for p in prices:
        key = (p.site, round(p.price / 1000))  # group by site + rounded price
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out
