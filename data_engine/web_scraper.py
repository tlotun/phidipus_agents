#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
data_engine/web_scraper.py — Phidipus AI Forge E1
═══════════════════════════════════════════════════════════════════

Web scraper for collecting training data from blogs, forums, and websites.
Uses requests + BeautifulSoup (no Scrapy dependency for simplicity).

Priority: LOW (YouTube + HuggingFace trước, web scraping last resort)

Usage:
  python data_engine/web_scraper.py --urls urls.txt --output raw_corpus/web/
  python data_engine/web_scraper.py --url "https://example.com/blog" --output raw_corpus/web/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg: str)   -> None: print(f"{_c('0;32', '[  OK]')}  {msg}")
def info(msg: str) -> None: print(f"{_c('0;36', '[INFO]')}  {msg}")
def warn(msg: str) -> None: print(f"{_c('1;33', '[WARN]')}  {msg}")
def fail(msg: str) -> None: print(f"{_c('0;31', '[FAIL]')}  {msg}")


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
}

SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mp3", ".wav", ".avi", ".mov",
    ".pdf", ".zip", ".rar", ".tar", ".gz",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
}


def can_fetch(url: str, user_agent: str = "*") -> bool:
    """Check robots.txt (basic compliance)."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        resp = requests.get(robots_url, headers=DEFAULT_HEADERS, timeout=5)
        if resp.status_code != 200:
            return True
        text = resp.text.lower()
        if "disallow: /" in text and "user-agent: *" in text:
            return False
        return True
    except Exception:
        return True


def extract_text(html: str, url: str = "") -> dict:
    """Extract clean text from HTML."""
    if not _HAS_BS4:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return {"text": text, "title": "", "method": "regex"}

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "noscript", "iframe", "form"]):
        tag.decompose()

    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)

    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)

    article = soup.find("article") or soup.find("main") or soup.find("div", class_=re.compile(r"content|post|article|entry", re.I))
    if article:
        text = article.get_text(separator="\n", strip=True)
    else:
        body = soup.find("body")
        text = body.get_text(separator="\n", strip=True) if body else soup.get_text(separator="\n", strip=True)

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    lines = [line for line in lines if len(line) > 20]
    clean_text = "\n".join(lines)

    return {
        "text": clean_text,
        "title": title,
        "method": "bs4",
    }


def scrape_url(
    url: str,
    delay: float = 1.0,
    timeout: int = 15,
    min_length: int = 200,
) -> dict | None:
    """Scrape a single URL and return extracted content."""
    if not _HAS_REQUESTS:
        fail("requests not installed: pip install requests")
        return None

    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext in SKIP_EXTENSIONS:
        return None

    try:
        time.sleep(delay)
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()

        if "text/html" not in resp.headers.get("Content-Type", ""):
            return None

        resp.encoding = resp.apparent_encoding or "utf-8"
        extracted = extract_text(resp.text, url)

        if len(extracted["text"]) < min_length:
            return None

        return {
            "url": url,
            "title": extracted["title"],
            "text": extracted["text"],
            "length": len(extracted["text"]),
            "method": extracted["method"],
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    except requests.RequestException as exc:
        warn(f"  Error scraping {url}: {exc}")
        return None


def scrape_batch(
    urls: list[str],
    output_dir: str = "raw_corpus/web",
    delay: float = 1.5,
    respect_robots: bool = True,
    min_length: int = 200,
) -> dict:
    """Scrape multiple URLs and save to output directory."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    stats = {"total": len(urls), "scraped": 0, "skipped": 0, "errors": 0, "total_chars": 0}

    for i, url in enumerate(urls):
        info(f"[{i+1}/{len(urls)}] {url[:80]}")

        if respect_robots and not can_fetch(url):
            warn(f"  Blocked by robots.txt")
            stats["skipped"] += 1
            continue

        result = scrape_url(url, delay=delay, min_length=min_length)
        if not result:
            stats["errors"] += 1
            continue

        url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
        safe_title = re.sub(r"[^\w\s-]", "", result["title"])[:50].strip().replace(" ", "_") or url_hash
        filename = f"{safe_title}_{url_hash}.txt"
        filepath = Path(output_dir) / filename
        filepath.write_text(result["text"], encoding="utf-8")

        meta_path = Path(output_dir) / f"{safe_title}_{url_hash}.meta.json"
        meta = {k: v for k, v in result.items() if k != "text"}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        stats["scraped"] += 1
        stats["total_chars"] += result["length"]
        ok(f"  {result['title'][:50]} ({result['length']} chars)")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Web scraper for AI Forge E1")
    parser.add_argument("--url", help="Single URL to scrape")
    parser.add_argument("--urls", help="File with URLs (one per line)")
    parser.add_argument("--output", "-o", default="raw_corpus/web/",
                        help="Output directory")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Delay between requests (seconds)")
    parser.add_argument("--min-length", type=int, default=200,
                        help="Minimum text length to keep")
    parser.add_argument("--no-robots", action="store_true",
                        help="Ignore robots.txt (not recommended)")
    args = parser.parse_args()

    urls = []
    if args.url:
        urls.append(args.url)
    if args.urls:
        urls_file = Path(args.urls)
        if urls_file.exists():
            urls.extend([
                line.strip() for line in urls_file.read_text(encoding="utf-8").split("\n")
                if line.strip() and not line.startswith("#")
            ])

    if not urls:
        fail("No URLs provided. Use --url or --urls")
        return

    info(f"Scraping {len(urls)} URLs → {args.output}")
    stats = scrape_batch(
        urls,
        output_dir=args.output,
        delay=args.delay,
        respect_robots=not args.no_robots,
        min_length=args.min_length,
    )

    print()
    ok(f"Done: {stats['scraped']}/{stats['total']} scraped, "
       f"{stats['skipped']} skipped, {stats['errors']} errors, "
       f"{stats['total_chars']:,} chars total")


if __name__ == "__main__":
    main()
