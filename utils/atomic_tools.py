# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/atomic_tools.py — Phidipus Atomic Tools
═══════════════════════════════════════════════

Pre-built, safe helper functions that AI-generated skills can call.
These are injected into the skill execution context so generated code
can use them without importing anything dangerous.

Categories:
  - File: find_files, read_excel, create_excel, read_text, write_text
  - Data: filter_rows, sort_data, aggregate
  - System: get_screen_info, list_apps, list_chrome_profiles
  - Output: send_telegram_file, send_telegram_message
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


# ══════════════════════════════════════════════════════════════
# File Operations
# ══════════════════════════════════════════════════════════════

def find_files(
    query: str,
    search_dirs: list[str] | None = None,
    extensions: list[str] | None = None,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """
    Find files by name (fuzzy match).

    Args:
        query: Search term (e.g. "hàng tồn kho")
        search_dirs: Directories to search (default: ~/Documents, ~/Desktop, ~/Downloads)
        extensions: File extensions to filter (e.g. [".xlsx", ".xls", ".csv"])
        max_results: Maximum number of results

    Returns:
        List of {"name": str, "path": str, "size_kb": int, "modified": str}
    """
    if search_dirs is None:
        home = str(Path.home())
        search_dirs = [
            os.path.join(home, "Documents"),
            os.path.join(home, "Desktop"),
            os.path.join(home, "Downloads"),
        ]

    if extensions is None:
        extensions = [".xlsx", ".xls", ".csv", ".txt", ".pdf", ".docx", ".json"]

    query_lower = query.lower()
    results = []

    for search_dir in search_dirs:
        if not os.path.exists(search_dir):
            continue
        try:
            for root, dirs, files in os.walk(search_dir):
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for f in files:
                    if f.startswith('.'):
                        continue
                    name_lower = f.lower()
                    ext = os.path.splitext(f)[1].lower()

                    if ext not in extensions:
                        continue

                    if query_lower in name_lower:
                        full_path = os.path.join(root, f)
                        try:
                            stat = os.stat(full_path)
                            results.append({
                                "name": f,
                                "path": full_path,
                                "size_kb": int(stat.st_size / 1024),
                                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            })
                        except OSError:
                            pass

                    if len(results) >= max_results:
                        break
        except PermissionError:
            continue

    results.sort(key=lambda x: x["modified"], reverse=True)
    return results[:max_results]


def read_excel(path: str, sheet_name: str | None = None) -> dict[str, Any]:
    """
    Read an Excel file and return structured data.

    Returns:
        {
            "headers": ["col1", "col2", ...],
            "rows": [[val1, val2, ...], ...],
            "row_count": int,
            "sheet_name": str,
        }
    """
    try:
        import openpyxl
    except ImportError:
        return {"error": "openpyxl chưa cài. Chạy: pip install openpyxl"}

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active

        rows = []
        headers = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            str_row = [str(cell) if cell is not None else "" for cell in row]
            if i == 0:
                headers = str_row
            else:
                rows.append(str_row)

        wb.close()
        return {
            "headers": headers,
            "rows": rows,
            "row_count": len(rows),
            "sheet_name": ws.title,
        }
    except Exception as e:
        return {"error": str(e)}


def create_excel(
    headers: list[str],
    rows: list[list],
    output_path: str,
    sheet_name: str = "Sheet1",
) -> dict[str, Any]:
    """
    Create a new Excel file.

    Returns:
        {"success": True, "path": str, "rows": int} or {"error": str}
    """
    try:
        import openpyxl
    except ImportError:
        return {"error": "openpyxl chưa cài. Chạy: pip install openpyxl"}

    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name

        # Write headers with bold
        from openpyxl.styles import Font
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)

        # Write data
        for row_idx, row_data in enumerate(rows, 2):
            for col_idx, value in enumerate(row_data, 1):
                ws.cell(row=row_idx, column=col_idx, value=value)

        # Auto-width columns
        for col in ws.columns:
            max_length = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_length + 2, 50)

        wb.save(output_path)
        return {"success": True, "path": output_path, "rows": len(rows)}
    except Exception as e:
        return {"error": str(e)}


def read_csv(path: str, encoding: str = "utf-8") -> dict[str, Any]:
    """Read a CSV file and return structured data."""
    try:
        with open(path, "r", encoding=encoding) as f:
            reader = csv.reader(f)
            headers = next(reader, [])
            rows = [row for row in reader]
        return {"headers": headers, "rows": rows, "row_count": len(rows)}
    except Exception as e:
        return {"error": str(e)}


def read_text(path: str, encoding: str = "utf-8") -> str:
    """Read a text file."""
    try:
        return Path(path).read_text(encoding=encoding)
    except Exception as e:
        return f"[ERROR] {e}"


def write_text(path: str, content: str, encoding: str = "utf-8") -> dict:
    """Write text to a file."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding=encoding)
        return {"success": True, "path": path}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
# Data Processing
# ══════════════════════════════════════════════════════════════

def filter_rows(
    rows: list[list],
    headers: list[str],
    keyword: str,
    columns: list[str] | None = None,
) -> list[list]:
    """
    Filter rows by keyword (fuzzy match, case-insensitive).

    Args:
        rows: Data rows
        headers: Column headers
        keyword: Search keyword (e.g. "bóng đèn")
        columns: Specific columns to search (default: all)

    Returns:
        Filtered rows
    """
    kw = keyword.lower()
    col_indices = list(range(len(headers)))

    if columns:
        col_indices = [i for i, h in enumerate(headers) if h.lower() in [c.lower() for c in columns]]

    result = []
    for row in rows:
        for idx in col_indices:
            if idx < len(row) and kw in str(row[idx]).lower():
                result.append(row)
                break

    return result


def aggregate(
    rows: list[list],
    headers: list[str],
    value_column: str,
    operation: str = "sum",
) -> Any:
    """
    Aggregate a column (sum, avg, count, min, max).
    """
    col_idx = None
    for i, h in enumerate(headers):
        if h.lower() == value_column.lower():
            col_idx = i
            break

    if col_idx is None:
        return None

    values = []
    for row in rows:
        if col_idx < len(row):
            try:
                val = float(str(row[col_idx]).replace(",", "").replace(".", "").strip())
                values.append(val)
            except (ValueError, TypeError):
                pass

    if not values:
        return None

    ops = {
        "sum": sum(values),
        "avg": sum(values) / len(values),
        "count": len(values),
        "min": min(values),
        "max": max(values),
    }
    return ops.get(operation, sum(values))


# ══════════════════════════════════════════════════════════════
# System Info
# ══════════════════════════════════════════════════════════════

def get_screen_info() -> dict:
    """Get screen resolution."""
    try:
        import subprocess
        out = subprocess.check_output(
            ["system_profiler", "SPDisplaysDataType"], timeout=5
        ).decode()
        m = re.search(r'(\d{3,5})\s*x\s*(\d{3,5})', out)
        if m:
            return {"width": int(m.group(1)), "height": int(m.group(2))}
    except Exception:
        pass
    return {"width": 3440, "height": 1440}  # Fallback


def get_dir_tree(path: str, max_depth: int = 3, max_files: int = 100) -> str:
    """Get directory tree as string (for Gemini context)."""
    lines = []
    count = 0
    root_path = Path(path)

    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith('.')]
        depth = len(Path(root).relative_to(root_path).parts)
        if depth >= max_depth:
            dirs.clear()
            continue

        indent = "  " * depth
        lines.append(f"{indent}{os.path.basename(root)}/")

        for f in sorted(files)[:20]:
            if f.startswith('.'):
                continue
            lines.append(f"{indent}  {f}")
            count += 1
            if count >= max_files:
                lines.append(f"{indent}  ... ({len(files) - 20} more)")
                break

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# v9.22: New Atomic Tools — web_fetch, read_pdf, get_clipboard,
#         get_recent_files
# ══════════════════════════════════════════════════════════════

def web_fetch(url: str, timeout: int = 10) -> dict:
    """
    Fetch text content from a URL (GET only, no auth).
    Returns {success, text, status_code} or {error}.

    Security: only http/https, no file:// or other schemes.
    Path restriction: caller must provide public URL.
    """
    import urllib.request as _ur
    import urllib.error as _ue

    # Only allow http/https — block file://, ftp://, etc.
    if not url.strip().lower().startswith(("http://", "https://")):
        return {"error": "Chỉ hỗ trợ http:// hoặc https://"}

    try:
        req = _ur.Request(
            url[:2000],
            headers={"User-Agent": "Phidipus/9.22 (macOS)"},
            method="GET",
        )
        with _ur.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(512 * 1024)  # max 512KB
            encoding = resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(encoding, errors="replace")
            return {
                "success": True,
                "text": text,
                "status_code": resp.status,
                "url": url,
            }
    except _ue.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "status_code": e.code}
    except Exception as e:
        return {"error": str(e)[:200]}


def read_pdf(path: str) -> dict:
    """
    Extract text from a PDF file.
    Returns {text, pages, success} or {error}.
    Requires PyPDF2 or pypdf (pip install pypdf).
    """
    if not os.path.exists(path):
        return {"error": f"File không tồn tại: {path}"}

    # Try pypdf first (newer), then PyPDF2
    try:
        try:
            from pypdf import PdfReader as _PR
        except ImportError:
            from PyPDF2 import PdfReader as _PR  # type: ignore

        reader = _PR(path)
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")

        full_text = "\n\n".join(texts)
        return {
            "success": True,
            "text": full_text[:50000],  # max 50K chars
            "pages": len(reader.pages),
            "path": path,
        }
    except ImportError:
        # Fallback: try pdfplumber
        try:
            import pdfplumber as _pl  # type: ignore
            with _pl.open(path) as pdf:
                texts = [p.extract_text() or "" for p in pdf.pages]
                return {
                    "success": True,
                    "text": "\n\n".join(texts)[:50000],
                    "pages": len(pdf.pages),
                    "path": path,
                }
        except ImportError:
            return {"error": "Cần cài: pip install pypdf  (hoặc pdfplumber)"}
    except Exception as e:
        return {"error": str(e)[:200]}


def get_clipboard() -> str:
    """
    Get current clipboard text content (macOS only via pbpaste).
    Returns clipboard string, or empty string if unavailable.
    """
    try:
        import subprocess as _sp
        result = _sp.run(
            ["pbpaste"],
            capture_output=True, text=True, timeout=3
        )
        return result.stdout[:5000] if result.returncode == 0 else ""
    except Exception:
        return ""


def get_recent_files(n: int = 10, extensions: list | None = None) -> list:
    """
    Get list of recently modified files across Desktop/Documents/Downloads.
    Returns list of {name, path, size_kb, modified} sorted newest first.

    Args:
        n: max number of files to return
        extensions: filter by extension e.g. [".xlsx", ".pdf"]
    """
    import os as _os
    from pathlib import Path as _P
    from datetime import datetime as _dt

    home = _P.home()
    search_dirs = [
        str(home / "Desktop"),
        str(home / "Documents"),
        str(home / "Downloads"),
    ]
    if extensions is None:
        extensions = [".xlsx", ".xls", ".csv", ".pdf", ".docx", ".txt", ".json", ".py"]

    results = []
    for d in search_dirs:
        if not _os.path.exists(d):
            continue
        try:
            for root, dirs, files in _os.walk(d):
                dirs[:] = [x for x in dirs if not x.startswith('.')][:5]  # shallow
                for f in files:
                    if f.startswith('.'):
                        continue
                    ext = _os.path.splitext(f)[1].lower()
                    if ext not in extensions:
                        continue
                    fp = _os.path.join(root, f)
                    try:
                        stat = _os.stat(fp)
                        results.append({
                            "name": f,
                            "path": fp,
                            "size_kb": int(stat.st_size / 1024),
                            "modified": _dt.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            "mtime": stat.st_mtime,
                        })
                    except OSError:
                        pass
        except PermissionError:
            continue

    # Sort by modified time, newest first
    results.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    # Remove internal mtime field
    for r in results:
        r.pop("mtime", None)
    return results[:n]


# ══════════════════════════════════════════════════════════════
# Tool Registry — list of all available tools for Gemini prompt
# ══════════════════════════════════════════════════════════════

TOOL_DESCRIPTIONS = """
Available Python functions (already imported, call directly):

FILE OPERATIONS:
  find_files(query, search_dirs=None, extensions=None) → list of {name, path, size_kb, modified}
  read_excel(path, sheet_name=None) → {headers, rows, row_count} or {error}
  create_excel(headers, rows, output_path, sheet_name="Sheet1") → {success, path, rows}
  read_csv(path) → {headers, rows, row_count}
  read_text(path) → str
  write_text(path, content) → {success, path}
  read_pdf(path) → {text, pages, success} or {error}
  get_recent_files(n=10, extensions=None) → list of {name, path, size_kb, modified}

WEB & CLIPBOARD:
  web_fetch(url, timeout=10) → {success, text, status_code} or {error}
  get_clipboard() → str  (macOS clipboard content, max 5000 chars)

DATA PROCESSING:
  filter_rows(rows, headers, keyword, columns=None) → filtered rows
  aggregate(rows, headers, value_column, "sum"|"avg"|"count"|"min"|"max") → number

SYSTEM:
  get_screen_info() → {width, height}
  get_dir_tree(path, max_depth=3) → str

BROWSER AUTOMATION (Chrome trên macOS — gọi trực tiếp, không cần import):
  browser_open_profile(profile_name, url="") → bool  # Mở Chrome profile (vd: "00fujin", "01")
  browser_navigate(url) → bool                        # Điều hướng tab hiện tại đến URL
  browser_new_tab(url="") → bool                      # Mở tab mới (tuỳ chọn URL)
  browser_activate_tab(url_fragment) → bool           # Focus tab có URL chứa fragment
  browser_get_url() → str                             # Lấy URL tab đang active
  browser_js(js_code, timeout=10) → str               # Chạy JavaScript, trả về string
  browser_type(text) → bool                           # Paste text vào element đang focus
  browser_click_text(label) → bool                    # Click button/link có text chứa label
  browser_wait(seconds) → None                        # Đợi N giây (tối đa 120s)
  browser_save_image(output_path, img_url="") → bool  # Download/save ảnh
  browser_screenshot(output_path) → bool              # Chụp màn hình → save file
  browser_close_windows(n=0) → bool                   # Đóng N cửa sổ Chrome (0=tất cả)
  clipboard_set(text) → bool                          # Copy text vào clipboard
  clipboard_get() → str                               # Lấy text từ clipboard

BROWSER AUTOMATION RULES:
  - Luôn dùng browser_wait(3) sau browser_navigate() để đợi trang load
  - Luôn dùng browser_wait(30-45) sau khi gửi prompt cho AI (ChatGPT/Gemini)
  - Dùng browser_js() để tương tác DOM khi không có nút rõ ràng
  - KHÔNG dùng import os, subprocess, sys — đã có sẵn qua browser_* tools

OUTPUT (set by runtime):
  result_files = []      # Append file paths để gửi về user
  result_message = ""    # Tin nhắn kết quả (dùng tiếng Việt)

NOTES:
  - All file paths are absolute
  - Excel requires openpyxl (already installed)
  - PDF requires: pip install pypdf
  - web_fetch: chỉ GET public URLs, max 512KB
  - Output files to /tmp/phidipus_output/
  - Browser tools chỉ hoạt động trên macOS với Chrome đang chạy
"""
