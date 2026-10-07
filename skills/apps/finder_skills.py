# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/apps/finder_skills.py — Phidipus v1.38
═══════════════════════════════════════════════════════════════════════

P2 Option A: macOS Finder Skill

Thực hiện mọi thao tác file/folder trên macOS bằng:
  Tier 1: AppleScript (Finder native API)
  Tier 2: subprocess shell (cp, mv, rm, find, ls)
  Tier 3: Python pathlib (pure Python, always works)

KHÔNG cần VLM, KHÔNG cần click, KHÔNG cần nhìn màn hình.
Kết quả trả về trong < 2 giây cho hầu hết thao tác.

Actions:
  find_files(path, pattern)          → list[str] paths tìm được
  copy_files(sources, destination)   → dict kết quả
  move_files(sources, destination)   → dict kết quả
  delete_files(paths, trash=True)    → dict kết quả (default: Trash)
  create_folder(path)                → bool
  rename_file(src, new_name)         → str new_path
  get_info(path)                     → dict {size, created, modified}
  open_in_finder(path)               → bool
  compress(sources, output)          → str zip_path
  list_folder(path, filter)          → list[dict]
  search_spotlight(query)            → list[str] paths
  get_downloads()                    → list[str] recent downloads

Entry point cho SmartRouter Fast Lane:
  run_finder_task(goal, ipc_client, notify_fn) → FinderResult
"""
from __future__ import annotations

import asyncio
import glob
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

_log = get_logger(__name__, process="orchestrator")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;36m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════════
# Result type
# ══════════════════════════════════════════════════════════════════

@dataclass
class FinderResult:
    success:    bool
    action:     str  = ""
    output:     Any  = None   # list, dict, str tùy action
    error:      str  = ""
    duration_ms:int  = 0

    @property
    def summary(self) -> str:
        if self.success:
            if isinstance(self.output, list):
                return f"✅ {self.action}: {len(self.output)} items"
            return f"✅ {self.action}: {str(self.output)[:80]}"
        return f"❌ {self.action}: {self.error[:80]}"


# ══════════════════════════════════════════════════════════════════
# Helper: run AppleScript
# ══════════════════════════════════════════════════════════════════

async def _osa(script: str, timeout: float = 10.0) -> str:
    """Chạy AppleScript, trả về stdout string."""
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.stdout.strip()
    except Exception as exc:
        return f"error:{exc}"


def _expand(path: str) -> str:
    """Expand ~ và variables, trả về absolute path."""
    return str(Path(path).expanduser().resolve())


def _safe_path(path: str) -> str:
    """Escape path cho AppleScript."""
    return path.replace('"', '\\"')


# ══════════════════════════════════════════════════════════════════
# FinderSkills
# ══════════════════════════════════════════════════════════════════

class FinderSkills:
    """
    Tất cả thao tác Finder / File system trên macOS.
    Mỗi method là async, trả về FinderResult.
    """

    # ── Tìm kiếm ────────────────────────────────────────────────

    async def find_files(
        self,
        path: str,
        pattern: str = "*",
        recursive: bool = True,
        max_results: int = 200,
    ) -> FinderResult:
        """
        Tìm files trong folder theo pattern.
        Pattern: "*.pdf", "*.jpg", "report*", "**/*.py"

        Examples:
            find_files("~/Downloads", "*.pdf")
            find_files("~/Documents", "**/*.xlsx", recursive=True)
        """
        t0 = time.time()
        try:
            base = Path(_expand(path))
            if not base.exists():
                return FinderResult(False, "find_files", error=f"Path không tồn tại: {path}")

            if recursive and "**" not in pattern:
                pattern = f"**/{pattern}"

            matches = list(base.glob(pattern) if recursive
                           else base.glob(pattern.replace("**/", "")))
            matches = [str(m) for m in matches
                       if m.is_file()][:max_results]

            ms = int((time.time() - t0) * 1000)
            _vlog("🔍", f"FinderSkills.find_files: {len(matches)} files trong '{path}' "
                  f"pattern='{pattern}' ({ms}ms)")
            return FinderResult(True, "find_files", output=matches, duration_ms=ms)

        except Exception as exc:
            return FinderResult(False, "find_files", error=str(exc))

    async def search_spotlight(
        self,
        query: str,
        max_results: int = 20,
    ) -> FinderResult:
        """
        Tìm kiếm toàn hệ thống qua Spotlight (mdfind).
        Nhanh hơn glob, tìm theo nội dung và tên file.

        Examples:
            search_spotlight("báo cáo tháng 3")
            search_spotlight("kind:pdf created:today")
        """
        t0 = time.time()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["mdfind", "-limit", str(max_results), query],
                capture_output=True, text=True, timeout=10.0,
            )
            results = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
            ms = int((time.time() - t0) * 1000)
            _vlog("🔦", f"Spotlight '{query}': {len(results)} results ({ms}ms)")
            return FinderResult(True, "search_spotlight", output=results, duration_ms=ms)
        except Exception as exc:
            return FinderResult(False, "search_spotlight", error=str(exc))

    # ── Copy / Move ──────────────────────────────────────────────

    async def copy_files(
        self,
        sources: list[str] | str,
        destination: str,
        overwrite: bool = False,
    ) -> FinderResult:
        """
        Copy files/folders vào destination.
        destination tự động tạo nếu chưa có.

        Examples:
            copy_files("~/Downloads/report.pdf", "~/Desktop/Backup/")
            copy_files(["~/a.pdf", "~/b.pdf"], "~/Backup/")
        """
        t0 = time.time()
        if isinstance(sources, str):
            sources = [sources]

        dest = Path(_expand(destination))
        copied, failed = [], []

        try:
            dest.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return FinderResult(False, "copy_files",
                                error=f"Không tạo được destination '{destination}': {exc}")

        for src in sources:
            src_path = Path(_expand(src))
            if not src_path.exists():
                failed.append({"src": src, "error": "không tồn tại"})
                continue
            dst_path = dest / src_path.name
            try:
                if dst_path.exists() and not overwrite:
                    stem, suffix = dst_path.stem, dst_path.suffix
                    dst_path = dest / f"{stem}_copy{suffix}"
                if src_path.is_dir():
                    shutil.copytree(str(src_path), str(dst_path))
                else:
                    shutil.copy2(str(src_path), str(dst_path))
                copied.append(str(dst_path))
            except Exception as exc:
                failed.append({"src": src, "error": str(exc)})

        ms = int((time.time() - t0) * 1000)
        success = len(copied) > 0
        _vlog("📋", f"copy_files: {len(copied)} OK, {len(failed)} fail ({ms}ms)")
        return FinderResult(
            success=success, action="copy_files",
            output={"copied": copied, "failed": failed, "destination": str(dest)},
            error="" if success else f"{len(failed)} files fail",
            duration_ms=ms,
        )

    async def move_files(
        self,
        sources: list[str] | str,
        destination: str,
    ) -> FinderResult:
        """
        Di chuyển files/folders vào destination.

        Examples:
            move_files("~/Downloads/old_report.pdf", "~/Archive/2025/")
        """
        t0 = time.time()
        if isinstance(sources, str):
            sources = [sources]

        dest = Path(_expand(destination))
        moved, failed = [], []

        try:
            dest.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return FinderResult(False, "move_files",
                                error=f"Không tạo được destination: {exc}")

        for src in sources:
            src_path = Path(_expand(src))
            if not src_path.exists():
                failed.append({"src": src, "error": "không tồn tại"})
                continue
            try:
                dst_path = dest / src_path.name
                shutil.move(str(src_path), str(dst_path))
                moved.append(str(dst_path))
            except Exception as exc:
                failed.append({"src": src, "error": str(exc)})

        ms = int((time.time() - t0) * 1000)
        success = len(moved) > 0
        _vlog("🚚", f"move_files: {len(moved)} OK, {len(failed)} fail ({ms}ms)")
        return FinderResult(
            success=success, action="move_files",
            output={"moved": moved, "failed": failed},
            error="" if success else f"{len(failed)} files fail",
            duration_ms=ms,
        )

    async def delete_files(
        self,
        paths: list[str] | str,
        use_trash: bool = True,
    ) -> FinderResult:
        """
        Xóa files — mặc định đưa vào Trash (an toàn).
        use_trash=False: xóa vĩnh viễn (nguy hiểm, cần confirm).

        Examples:
            delete_files("~/Downloads/temp.zip")
            delete_files(["~/a.tmp", "~/b.tmp"], use_trash=True)
        """
        t0 = time.time()
        if isinstance(paths, str):
            paths = [paths]

        deleted, failed = [], []

        for path in paths:
            expanded = _expand(path)
            p = Path(expanded)
            if not p.exists():
                failed.append({"path": path, "error": "không tồn tại"})
                continue
            try:
                if use_trash:
                    # Dùng AppleScript để đưa vào Trash (có thể recover)
                    safe = _safe_path(expanded)
                    script = (
                        f'tell application "Finder"\n'
                        f'  move POSIX file "{safe}" to trash\n'
                        f'end tell'
                    )
                    await _osa(script, timeout=5.0)
                else:
                    if p.is_dir():
                        shutil.rmtree(str(p))
                    else:
                        p.unlink()
                deleted.append(expanded)
            except Exception as exc:
                failed.append({"path": path, "error": str(exc)})

        ms = int((time.time() - t0) * 1000)
        success = len(deleted) > 0
        method = "→ Trash" if use_trash else "→ Permanent"
        _vlog("🗑️", f"delete_files {method}: {len(deleted)} OK, {len(failed)} fail ({ms}ms)")
        return FinderResult(
            success=success, action="delete_files",
            output={"deleted": deleted, "failed": failed, "trash": use_trash},
            duration_ms=ms,
        )

    # ── Folder operations ────────────────────────────────────────

    async def create_folder(self, path: str) -> FinderResult:
        """Tạo folder (bao gồm parent folders)."""
        t0 = time.time()
        try:
            p = Path(_expand(path))
            p.mkdir(parents=True, exist_ok=True)
            _vlog("📁", f"create_folder: {p}")
            return FinderResult(True, "create_folder", output=str(p),
                                duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return FinderResult(False, "create_folder", error=str(exc))

    async def rename_file(self, src: str, new_name: str) -> FinderResult:
        """Đổi tên file/folder."""
        t0 = time.time()
        try:
            src_path = Path(_expand(src))
            if not src_path.exists():
                return FinderResult(False, "rename_file", error=f"Không tìm thấy: {src}")
            dst_path = src_path.parent / new_name
            src_path.rename(dst_path)
            _vlog("✏️", f"rename: {src_path.name} → {new_name}")
            return FinderResult(True, "rename_file", output=str(dst_path),
                                duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return FinderResult(False, "rename_file", error=str(exc))

    async def list_folder(
        self,
        path: str = "~/Desktop",
        show_hidden: bool = False,
        sort_by: str = "name",  # "name" | "size" | "modified"
    ) -> FinderResult:
        """
        List nội dung thư mục với metadata.

        Returns list of {name, type, size_mb, modified}
        """
        t0 = time.time()
        try:
            base = Path(_expand(path))
            if not base.exists():
                return FinderResult(False, "list_folder", error=f"Không tồn tại: {path}")

            items = []
            for item in base.iterdir():
                if not show_hidden and item.name.startswith("."):
                    continue
                try:
                    stat = item.stat()
                    items.append({
                        "name":     item.name,
                        "type":     "folder" if item.is_dir() else "file",
                        "ext":      item.suffix.lower() if item.is_file() else "",
                        "size_mb":  round(stat.st_size / 1024 / 1024, 2),
                        "modified": int(stat.st_mtime),
                        "path":     str(item),
                    })
                except Exception:
                    pass

            # Sort
            key_map = {"name": "name", "size": "size_mb", "modified": "modified"}
            items.sort(key=lambda x: x.get(key_map.get(sort_by, "name"), ""))

            ms = int((time.time()-t0)*1000)
            _vlog("📂", f"list_folder '{path}': {len(items)} items ({ms}ms)")
            return FinderResult(True, "list_folder", output=items, duration_ms=ms)
        except Exception as exc:
            return FinderResult(False, "list_folder", error=str(exc))

    # ── Info / Open ──────────────────────────────────────────────

    async def get_info(self, path: str) -> FinderResult:
        """Lấy thông tin file: size, dates, kind."""
        t0 = time.time()
        try:
            p = Path(_expand(path))
            if not p.exists():
                return FinderResult(False, "get_info", error=f"Không tồn tại: {path}")
            stat = p.stat()
            info = {
                "name":     p.name,
                "path":     str(p),
                "type":     "folder" if p.is_dir() else "file",
                "size_bytes": stat.st_size,
                "size_mb":  round(stat.st_size / 1024 / 1024, 3),
                "modified": int(stat.st_mtime),
                "created":  int(stat.st_ctime),
                "ext":      p.suffix.lower(),
                "exists":   True,
            }
            return FinderResult(True, "get_info", output=info,
                                duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return FinderResult(False, "get_info", error=str(exc))

    async def open_in_finder(self, path: str) -> FinderResult:
        """Mở file/folder trong Finder (hiện thị cho user)."""
        t0 = time.time()
        try:
            expanded = _expand(path)
            safe = _safe_path(expanded)
            if Path(expanded).is_dir():
                script = f'tell application "Finder"\n  open folder POSIX file "{safe}"\n  activate\nend tell'
            else:
                script = f'tell application "Finder"\n  reveal POSIX file "{safe}"\n  activate\nend tell'
            await _osa(script, timeout=5.0)
            _vlog("📂", f"Opened in Finder: {path}")
            return FinderResult(True, "open_in_finder", output=expanded,
                                duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return FinderResult(False, "open_in_finder", error=str(exc))

    async def open_file(self, path: str) -> FinderResult:
        """Mở file với default app (open command)."""
        t0 = time.time()
        try:
            expanded = _expand(path)
            proc = await asyncio.to_thread(
                subprocess.run, ["open", expanded],
                capture_output=True, timeout=10.0,
            )
            success = proc.returncode == 0
            return FinderResult(success, "open_file", output=expanded,
                                error=proc.stderr.decode()[:100] if not success else "",
                                duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return FinderResult(False, "open_file", error=str(exc))

    # ── Compress ─────────────────────────────────────────────────

    async def compress(
        self,
        sources: list[str] | str,
        output_path: str = "",
    ) -> FinderResult:
        """
        Nén files/folders thành zip.

        Examples:
            compress(["~/Downloads/report.pdf"], "~/Desktop/report.zip")
            compress("~/Documents/Project/", "~/Desktop/Project_backup.zip")
        """
        t0 = time.time()
        if isinstance(sources, str):
            sources = [sources]
        if not output_path:
            first = Path(_expand(sources[0]))
            output_path = str(first.parent / f"{first.stem}.zip")

        try:
            import zipfile
            out = Path(_expand(output_path))
            with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as zf:
                for src in sources:
                    src_path = Path(_expand(src))
                    if src_path.is_dir():
                        for f in src_path.rglob("*"):
                            if f.is_file():
                                zf.write(str(f), f.relative_to(src_path.parent))
                    elif src_path.is_file():
                        zf.write(str(src_path), src_path.name)
            ms = int((time.time()-t0)*1000)
            size_mb = round(out.stat().st_size / 1024 / 1024, 2)
            _vlog("🗜️", f"compress: {out.name} ({size_mb}MB, {ms}ms)")
            return FinderResult(True, "compress", output=str(out), duration_ms=ms)
        except Exception as exc:
            return FinderResult(False, "compress", error=str(exc))

    # ── Clipboard ────────────────────────────────────────────────

    async def copy_path_to_clipboard(self, path: str) -> FinderResult:
        """Copy đường dẫn file vào clipboard."""
        t0 = time.time()
        try:
            expanded = _expand(path)
            proc = await asyncio.to_thread(
                subprocess.run, ["pbcopy"],
                input=expanded.encode(), capture_output=True, timeout=3.0,
            )
            return FinderResult(proc.returncode == 0, "copy_to_clipboard",
                                output=expanded, duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return FinderResult(False, "copy_to_clipboard", error=str(exc))

    async def get_downloads(
        self,
        days: int = 7,
        extensions: list[str] | None = None,
    ) -> FinderResult:
        """
        Lấy danh sách files download gần đây.

        Examples:
            get_downloads(days=1)       # hôm nay
            get_downloads(days=7, extensions=[".pdf", ".docx"])
        """
        t0 = time.time()
        try:
            downloads = Path.home() / "Downloads"
            cutoff    = time.time() - (days * 86400)
            items     = []

            for item in downloads.iterdir():
                if item.name.startswith("."):
                    continue
                try:
                    stat = item.stat()
                    if stat.st_mtime < cutoff:
                        continue
                    if extensions and item.suffix.lower() not in extensions:
                        continue
                    items.append({
                        "name":     item.name,
                        "path":     str(item),
                        "size_mb":  round(stat.st_size / 1024 / 1024, 2),
                        "modified": int(stat.st_mtime),
                    })
                except Exception:
                    pass

            items.sort(key=lambda x: x["modified"], reverse=True)
            _vlog("📥", f"get_downloads: {len(items)} files trong {days} ngày")
            return FinderResult(True, "get_downloads", output=items,
                                duration_ms=int((time.time()-t0)*1000))
        except Exception as exc:
            return FinderResult(False, "get_downloads", error=str(exc))


# ══════════════════════════════════════════════════════════════════
# Smart Goal Decomposer — FIX v1.0
# ══════════════════════════════════════════════════════════════════
# Thay vì dùng keyword match cứng (if "tìm" → find, elif "list" → list),
# decomposer tách goal thành structured intent TRƯỚC rồi mới routing.
#
# VÍ DỤ vấn đề cũ:
#   "liệt kê các file pdf trong downloads"
#     → "downloads" match trước → get_downloads() → 27 items (SAI)
#   "tìm file PDF trong downloads"
#     → "tìm" match → find_files() → 5 PDF files (ĐÚNG)
#
# VÍ DỤ sau fix:
#   Cả 2 câu → decompose: action=find, file_filter=*.pdf, location=~/Downloads
#   → find_files("~/Downloads", "*.pdf") → 5 PDF files (ĐÚNG cả 2)
#
# QUY TẮC VÀNG:
#   Nếu câu lệnh nhắc đến FILE TYPE cụ thể (pdf, jpg, xlsx...)
#   → LUÔN là find/filter, bất kể động từ là gì.
# ══════════════════════════════════════════════════════════════════

import re as _re

# Mapping động từ → action (Vietnamese + English, synonyms)
_ACTION_VERBS: dict[str, list[str]] = {
    "find":     ["tìm", "find", "search", "tìm kiếm", "kiếm", "lọc", "filter", "locate"],
    "list":     ["list", "liệt kê", "danh sách", "xem", "ls", "dir", "show", "hiển thị",
                 "cho xem", "xem danh sách", "đưa ra", "kể ra"],
    "copy":     ["copy", "sao chép", "chép", "cp", "sao", "clone", "duplicate"],
    "move":     ["move", "di chuyển", "dời", "mv", "chuyển", "bỏ vào", "đưa vào"],
    "delete":   ["delete", "xóa", "xoá", "remove", "dọn", "rm", "bỏ", "hủy"],
    "create":   ["tạo", "create", "mkdir", "new folder", "tạo mới", "tạo thư mục"],
    "compress": ["nén", "compress", "zip", "archive", "đóng gói"],
    "open":     ["mở", "open", "reveal", "show in finder"],
    "info":     ["info", "thông tin", "size", "dung lượng", "chi tiết", "detail", "xem info"],
    "rename":   ["rename", "đổi tên", "đặt tên lại"],
    "search_spotlight": ["spotlight", "toàn hệ thống", "everywhere", "toàn bộ máy"],
    "downloads": ["download gần đây", "tải về gần đây", "file mới tải",
                   "mới download", "recent downloads", "vừa tải"],
}

# File extensions cho detect
_EXT_PATTERN = (
    r"(?<!\w)"
    r"(pdf|docx?|xlsx?|xls|pptx?|ppt|png|jpe?g|gif|webp|svg|bmp|tiff?"
    r"|zip|tar|gz|rar|7z"
    r"|py|js|ts|jsx|tsx|vue|html?|css|json|xml|yaml|yml|md|txt|csv|log"
    r"|mp[34]|mov|avi|mkv|wav|flac|aac"
    r"|dmg|pkg|app|exe|sh|command)"
    r"(?!\w)"
)

# Folder aliases
_FOLDER_ALIASES = {
    "desktop":   "~/Desktop",
    "downloads": "~/Downloads",
    "download":  "~/Downloads",    # singular
    "tải về":    "~/Downloads",
    "documents": "~/Documents",
    "document":  "~/Documents",    # singular
    "tài liệu":  "~/Documents",
    "pictures":  "~/Pictures",
    "picture":   "~/Pictures",     # singular
    "ảnh":       "~/Pictures",
    "hình":      "~/Pictures",
    "movies":    "~/Movies",
    "movie":     "~/Movies",       # singular
    "music":     "~/Music",
    "nhạc":      "~/Music",
    "home":      "~",
}


@dataclass
class _FinderIntent:
    """Structured intent parsed from natural language goal."""
    action:       str = "unknown"   # find|list|copy|move|delete|create|compress|open|info|rename|search_spotlight|downloads
    file_filter:  str = "*"         # "*.pdf" | "*.jpg" | "*"
    has_file_type:bool = False      # True nếu user nhắc đến file type cụ thể
    location:     str = ""          # "~/Downloads" etc.
    destination:  str = ""          # for copy/move
    query:        str = ""          # for search/spotlight
    raw_goal:     str = ""


def _decompose_finder_goal(goal: str) -> _FinderIntent:
    """
    Tách goal thành structured intent.

    Priority:
      1. Detect file type/extension → has_file_type=True
      2. Detect location (folder)
      3. Detect action verb → map to canonical action
      4. Apply golden rule: has_file_type + non-transfer action → find
    """
    intent = _FinderIntent(raw_goal=goal)
    gl = goal.lower()

    # ── 1. Detect file extension/type ─────────────────────────────
    m_ext = _re.search(r"\*?\." + _EXT_PATTERN, goal, _re.I)
    if not m_ext:
        m_ext = _re.search(_EXT_PATTERN, goal, _re.I)
    if m_ext:
        ext = m_ext.group(1).lower()
        intent.file_filter = f"*.{ext}"
        intent.has_file_type = True

    # Detect "file ảnh" / "file hình" / "file nhạc" → extension groups
    if not intent.has_file_type:
        # Specific app names first (excel→xlsx, word→docx, powerpoint→pptx)
        _app_ext_map = {
            r"\bexcel\b":       "*.xlsx",
            r"\bword\b":        "*.docx",
            r"\bpowerpoint\b":  "*.pptx",
            r"\bppt\b":         "*.pptx",
        }
        for pat, filt in _app_ext_map.items():
            if _re.search(pat, gl):
                intent.file_filter = filt
                intent.has_file_type = True
                break

    if not intent.has_file_type:
        _type_groups = {
            r"\b(?:ảnh|hình ảnh|photos?|images?)\b": "*.{jpg,jpeg,png,gif,webp}",
            r"\b(?:video|phim|clip)\b":               "*.{mp4,mov,avi,mkv}",
            r"\b(?:nhạc|music|audio|bài hát)\b":      "*.{mp3,wav,flac,aac}",
            r"\b(?:tài liệu|documents?)\b":           "*.{pdf,docx,xlsx,pptx,txt}",
        }
        for pat, filt in _type_groups.items():
            if _re.search(pat, gl):
                intent.file_filter = filt
                intent.has_file_type = True
                break

    # ── 2. Detect location (folder) ───────────────────────────────
    # Absolute path first
    m_abs = _re.search(r"(~/[^\s\"',]+)", goal)
    if m_abs:
        intent.location = m_abs.group(1).rstrip("/,.")

    # Relative folder name
    if not intent.location:
        for alias, expanded in _FOLDER_ALIASES.items():
            if _re.search(r"\b" + _re.escape(alias) + r"\b", gl):
                # Check if there's a subfolder
                m_sub = _re.search(
                    _re.escape(alias) + r"[/\\]([^\s\"',]+)",
                    goal, _re.I
                )
                if m_sub:
                    intent.location = f"{expanded}/{m_sub.group(1).rstrip('/,.')}"
                else:
                    intent.location = expanded
                break

    # ── 3. Detect destination (for copy/move) ─────────────────────
    # "vào/into/to <folder>"
    m_dst = _re.search(
        r"(?:vào|into|\bto\b)\s+(~/[^\s]+|\b(?:Desktop|Downloads|Documents|Pictures)(?:/[^\s]*)?)",
        goal, _re.I
    )
    if m_dst:
        dst = m_dst.group(1)
        if not dst.startswith("~/"):
            dst = f"~/{dst}"
        intent.destination = dst.rstrip("/,.")

    # ── 4. Detect action verb ─────────────────────────────────────
    # Check special composite keywords FIRST (higher priority)
    _SPECIAL_ACTIONS = [
        ("search_spotlight", ["spotlight", "toàn hệ thống", "everywhere", "toàn bộ máy",
                              "tìm kiếm spotlight", "search spotlight"]),
        ("downloads",        ["download gần đây", "tải về gần đây", "file mới tải",
                              "mới download", "recent downloads", "vừa tải"]),
    ]
    for action, triggers in _SPECIAL_ACTIONS:
        if any(t in gl for t in triggers):
            intent.action = action
            break

    if intent.action == "unknown":
        best_action = "unknown"
        best_pos = len(gl) + 1  # position in text (earlier = stronger)

        for action, verbs in _ACTION_VERBS.items():
            for verb in verbs:
                idx = gl.find(verb)
                if idx != -1 and idx < best_pos:
                    # Check word boundary
                    before_ok = (idx == 0 or not gl[idx-1].isalpha())
                    after_idx = idx + len(verb)
                    after_ok = (after_idx >= len(gl) or not gl[after_idx].isalpha())
                    if before_ok and after_ok:
                        best_action = action
                        best_pos = idx

        intent.action = best_action

    # ── 5. GOLDEN RULE: file type + non-transfer verb → find ──────
    # "liệt kê PDF" = "tìm PDF" = "xem PDF" = find_files
    if intent.has_file_type and intent.action in ("list", "downloads", "unknown", "info", "open"):
        intent.action = "find"

    # ── 6. Default location ───────────────────────────────────────
    if not intent.location:
        if intent.action == "downloads":
            intent.location = "~/Downloads"
        else:
            intent.location = "~/Desktop"

    # ── 7. Fallback action ────────────────────────────────────────
    if intent.action == "unknown":
        if intent.destination:
            intent.action = "copy"  # có destination → likely copy/move
        elif intent.has_file_type:
            intent.action = "find"
        elif "spotlight" in gl or "toàn" in gl:
            intent.action = "search_spotlight"
        else:
            intent.action = "list"  # default: list folder

    intent.query = goal  # for spotlight fallback

    return intent


_FINDER = FinderSkills()


async def run_finder_task(
    goal: str,
    ipc_client: Any = None,
    notify_fn: Any = None,
) -> FinderResult:
    """
    Entry point cho SmartRouter Fast Lane.
    FIX v1.0: Smart Goal Decomposer — structured intent routing.

    Mọi cách diễn đạt cùng ý đều cho cùng kết quả:
        "liệt kê file pdf trong downloads"  → find_files(~/Downloads, *.pdf)
        "tìm file PDF trong downloads"      → find_files(~/Downloads, *.pdf)
        "xem các file pdf ở downloads"      → find_files(~/Downloads, *.pdf)
        "cho xem pdf downloads"             → find_files(~/Downloads, *.pdf)
        "list file trong desktop"           → list_folder(~/Desktop)
        "copy pdf downloads vào desktop"    → copy_files(~/Downloads/**/*.pdf, ~/Desktop)
    """
    # ── Decompose goal → structured intent ────────────────────────
    intent = _decompose_finder_goal(goal)

    # ── FIX v1.0: qwen3:4b fallback khi decomposer → unknown ───
    if intent.action == "unknown" or (intent.action == "list" and not intent.has_file_type and "?" in goal):
        try:
            from core.llm_intent_parser import get_intent_parser, intent_to_finder_path, intent_to_file_filter
            parser = get_intent_parser()
            gi = await asyncio.wait_for(parser.parse(goal), timeout=3.0)
            if gi and gi.action != "unknown" and gi.confidence >= 0.4:
                # Override decomposer result with LLM's understanding
                _GEMMA_ACTION_MAP = {
                    "find": "find", "list": "list", "copy": "copy", "move": "move",
                    "delete": "delete", "create_folder": "create", "compress": "compress",
                    "rename": "rename", "open_file": "open",
                }
                mapped = _GEMMA_ACTION_MAP.get(gi.action, intent.action)
                if mapped != intent.action or gi.file_type:
                    intent.action = mapped
                    if gi.file_type:
                        intent.file_filter = intent_to_file_filter(gi.file_type)
                        intent.has_file_type = True
                    if gi.location:
                        intent.location = intent_to_finder_path(gi.location)
                    if gi.destination:
                        intent.destination = intent_to_finder_path(gi.destination)
                    _vlog("🧠", f"Qwen3 override: action={intent.action} "
                          f"filter={intent.file_filter} loc={intent.location}")
        except Exception:
            pass  # qwen3 unavailable → use decomposer result

    _vlog("🧩", f"FinderIntent: action={intent.action} filter={intent.file_filter} "
          f"loc={intent.location} dst={intent.destination} has_type={intent.has_file_type}")

    result: FinderResult

    # ── Route by structured action ────────────────────────────────

    if intent.action == "find":
        path, pattern = intent.location, intent.file_filter
        if pattern == "*":
            # No extension from decomposer → try _parse_find for legacy compat
            path, pattern = _parse_find(goal)
        result = await _FINDER.find_files(path, pattern)

    elif intent.action == "copy":
        src, dst = _parse_src_dst(goal)
        if src and dst:
            import glob as _glob
            if any(c in src for c in "*?["):
                matches = _glob.glob(str(Path(src).expanduser()), recursive=True)
                if matches:
                    result = await _FINDER.copy_files(matches, dst)
                else:
                    result = FinderResult(False, "copy_files", error=f"Không tìm thấy file khớp: {src}")
            else:
                result = await _FINDER.copy_files(src, dst)
        else:
            result = FinderResult(False, "copy_files", error="Không xác định được nguồn/đích")

    elif intent.action == "move":
        src, dst = _parse_src_dst(goal)
        if src and dst:
            result = await _FINDER.move_files(src, dst)
        else:
            result = FinderResult(False, "move_files", error="Không xác định được nguồn/đích")

    elif intent.action == "delete":
        targets, use_trash = _parse_delete_targets(goal)
        result = await _FINDER.delete_files(targets, use_trash=use_trash)

    elif intent.action == "create":
        if any(k in goal.lower() for k in ("thư mục", "folder", "mkdir", "directory")):
            path = _extract_path(goal) or intent.location + "/NewFolder"
            result = await _FINDER.create_folder(path)
        else:
            path = _extract_path(goal) or intent.location + "/NewFolder"
            result = await _FINDER.create_folder(path)

    elif intent.action == "compress":
        src, dst = _parse_src_dst(goal)
        if not dst:
            dst = ""
        result = await _FINDER.compress(src or intent.location, dst)

    elif intent.action == "open":
        path = _extract_path(goal) or intent.location
        gl = goal.lower()
        if any(k in gl for k in ("finder", "thư mục", "folder")):
            result = await _FINDER.open_in_finder(path)
        else:
            result = await _FINDER.open_file(path)

    elif intent.action == "list":
        path = intent.location or _extract_path(goal) or "~/Desktop"
        result = await _FINDER.list_folder(path)

    elif intent.action == "info":
        path = _extract_path(goal) or intent.location
        result = await _FINDER.get_info(path)

    elif intent.action == "rename":
        # Basic rename — extract old + new name
        path = _extract_path(goal)
        result = FinderResult(False, "rename", error="Rename chưa implement đầy đủ — dùng terminal: mv old new")

    elif intent.action == "search_spotlight":
        query = _extract_query(goal)
        result = await _FINDER.search_spotlight(query)

    elif intent.action == "downloads":
        result = await _FINDER.get_downloads()

    else:
        # Absolute fallback: Spotlight search
        result = await _FINDER.search_spotlight(goal)

    # Notify qua Telegram
    if notify_fn and result.success:
        try:
            output = result.output
            action = result.action

            # ── list_folder ────────────────────────────────────────
            if action == "list_folder" and isinstance(output, list):
                folders = [x for x in output if isinstance(x, dict) and x.get("type") == "folder"]
                files   = [x for x in output if isinstance(x, dict) and x.get("type") == "file"]
                lines = [f"📂 *Thư mục Desktop* — {len(output)} mục:\n"]
                for x in folders[:5]:
                    lines.append(f"  📁 {x.get('name', '')}")
                for x in files[:8]:
                    name = x.get("name", "")
                    size = x.get("size_mb", 0)
                    lines.append(f"  📄 {name} _{size:.1f}MB_")
                if len(output) > 13:
                    lines.append(f"  _...và {len(output)-13} mục khác_")
                msg = "\n".join(lines)

            # ── find_files ─────────────────────────────────────────
            elif action == "find_files" and isinstance(output, list):
                if output:
                    lines = [f"🔍 *Tìm thấy {len(output)} file:*\n"]
                    for x in output[:8]:
                        name = x.get("name", str(x)) if isinstance(x, dict) else str(x)
                        lines.append(f"  📄 {name}")
                    if len(output) > 8:
                        lines.append(f"  _...và {len(output)-8} files khác_")
                    msg = "\n".join(lines)
                else:
                    msg = "🔍 Không tìm thấy file nào khớp."

            # ── copy_files / move_files ────────────────────────────
            elif action in ("copy_files", "move_files") and isinstance(output, dict):
                verb = "Đã sao chép" if action == "copy_files" else "Đã di chuyển"
                ok_list = output.get("copied", output.get("moved", []))
                dst = output.get("destination", "")
                lines = [f"✅ *{verb} {len(ok_list)} file*"]
                if dst:
                    lines.append(f"📁 Đến: `{dst}`")
                for f in ok_list[:5]:
                    import os
                    lines.append(f"  • {os.path.basename(f)}")
                if len(ok_list) > 5:
                    lines.append(f"  _...và {len(ok_list)-5} files khác_")
                msg = "\n".join(lines)

            # ── create_folder ──────────────────────────────────────
            elif action == "create_folder":
                import os
                folder_name = os.path.basename(str(output))
                msg = f"✅ Đã tạo thư mục *{folder_name}*\n📁 `{output}`"

            # ── delete_files ───────────────────────────────────────
            elif action == "delete_files" and isinstance(output, dict):
                n = len(output.get("deleted", []))
                msg = f"🗑️ Đã xóa *{n} mục* vào Trash"

            # ── search_spotlight ───────────────────────────────────
            elif action == "search_spotlight" and isinstance(output, list):
                lines = [f"🔦 *Spotlight tìm thấy {len(output)} kết quả:*\n"]
                for x in output[:6]:
                    name = x.get("name", str(x)) if isinstance(x, dict) else str(x)
                    lines.append(f"  • {name}")
                msg = "\n".join(lines)

            # ── fallback ───────────────────────────────────────────
            elif isinstance(output, list):
                msg = f"✅ *{action}*: {len(output)} items hoàn thành"
            elif isinstance(output, dict):
                ok_items = output.get("copied", output.get("moved", output.get("deleted", [])))
                msg = f"✅ *{action}*: {len(ok_items)} items hoàn thành"
            else:
                msg = f"✅ *{action}*: {str(output)[:200]}"

            await notify_fn(msg)
        except Exception:
            pass
    elif notify_fn and not result.success:
        try:
            await notify_fn(f"❌ *{result.action}* thất bại: {result.error[:200]}")
        except Exception:
            pass

    _vlog("🗂️", f"FinderTask: {result.summary}")
    return result


# ── Simple parsers (không cần LLM) ───────────────────────────────

def _extract_path(text: str) -> str:
    """Tìm path-like string trong text. FIX v1.0: expand relative paths."""
    import re
    m = re.search(r'(~/[^\s"\']*|(?<![\w/])/[^\s"\']{2,})', text)
    if m:
        return m.group(1)
    known = re.search(r'\b(Desktop|Downloads|Documents|Pictures|Movies|Music)(/[^\s"\']*)?' , text)
    if known:
        base = known.group(1)
        rest = known.group(2) or ""
        return f"~/{base}{rest}"
    return ""

def _extract_query(text: str) -> str:
    """Lấy phần query sau động từ tìm kiếm."""
    import re
    m = re.sub(r'(tìm|search|find|kiếm)\s+', "", text.lower()).strip()
    return m[:100]

def _parse_src_dst(text: str) -> tuple:
    """Tìm src và dst. FIX v1.0: split trên vào/into, expand relative paths."""
    import re

    FOLDER_PAT = r"(?:Desktop|Downloads|Documents|Pictures|Movies|Music)"
    EXT_PAT = r"(?:png|jpg|jpeg|pdf|docx?|xlsx?|zip|mp4|mp3|txt|csv|py|js)"

    def _expand(p: str) -> str:
        p = p.strip().rstrip(",.")
        if not p:
            return p
        if p.startswith("~/") or p.startswith("/"):
            return p
        if re.match(FOLDER_PAT, p, re.I):
            return f"~/{p}"
        return p

    def _first_path(s: str) -> str:
        """Wildcard check TRƯỚC folder để tránh match nhầm."""
        # 1. Wildcard: "tất cả PNG", "all PDF"
        m = re.search(r"(?:tất cả|all)\s+(\w+)", s, re.I)
        if m:
            return m.group(1).lower().rstrip("s")
        # 2. Bare ext: "copy PDF vào..." → "pdf"
        m = re.match(r"\s*(?:copy|sao chép|chép|move|di chuyển)?\s*(" + EXT_PAT + r")\b", s, re.I)
        if m:
            return m.group(1).lower()
        # 3. ~/path hoặc /absolute
        m = re.search(r"(~/[\S]+|(?<![\w/])/[\S]{2,})", s)
        if m:
            return m.group(1).rstrip(",.")  # giữ trailing /
        # 4. Relative folder: Desktop/X
        m = re.search(r"\b(" + FOLDER_PAT + r")((?:/[\S]*)?)", s, re.I)
        if m:
            return _expand(m.group(1) + m.group(2))
        return ""

    # Tách left/right theo "vào" / "into" / " to "
    split = re.split(r"\s+(?:vào|into|\bto\b)\s+", text, maxsplit=1, flags=re.I)

    if len(split) == 2:
        left, right = split
        dst = _expand(_first_path(right))
        src_raw = _first_path(left)

        # Nếu src_raw là extension ("png", "pdf") → tìm folder trong left
        if src_raw and not src_raw.startswith("~") and not src_raw.startswith("/"):
            ext = src_raw
            m2 = re.search(r"\b(" + FOLDER_PAT + r")((?:/[\S]*)?)", left, re.I)
            folder = _expand(m2.group(1) + m2.group(2)) if m2 else "~/Desktop"
            src = f"{folder}/**/*.{ext}"
        else:
            src = _expand(src_raw)

        if src and dst:
            return src, dst

    # Fallback: collect tất cả paths
    t = text.lower()
    paths = []
    for m in re.finditer(r"(~/[\S]+|\b(?:Desktop|Downloads|Documents|Pictures|Movies|Music)(?:/[\S]*)?)", text, re.I):
        p = _expand(m.group(0))
        if p and p not in paths:
            paths.append(p)

    if len(paths) >= 2:
        return paths[0], paths[-1]
    if len(paths) == 1:
        if "desktop" in t:
            return paths[0], "~/Desktop/"
        if "downloads" in t:
            return paths[0], "~/Downloads/"
        if "documents" in t:
            return paths[0], "~/Documents/"

    return ("", "")

def _parse_delete_targets(text: str) -> tuple:
    """Tìm targets và xác định có dùng Trash không."""
    import re
    paths = re.findall(r'([~/][^\s"\']+)', text)
    t = text.lower()
    use_trash = "vĩnh viễn" not in t and "permanent" not in t
    if not paths:
        # Tìm theo pattern
        if "*.tmp" in t or ".tmp" in t:
            paths = list(glob.glob(str(Path.home() / "Downloads" / "*.tmp")))
    return (paths or ["~/Downloads/"], use_trash)

def _parse_find(text: str) -> tuple:
    """Parse path và pattern từ câu find. FIX v1.0: extract folder + ext đúng."""
    import re

    FOLDER_PAT = r"(?:Desktop|Downloads|Documents|Pictures|Movies|Music)"
    EXT_LIST = r"(pdf|docx?|xlsx?|pptx?|png|jpg|jpeg|gif|zip|tar|py|js|ts|txt|csv|md|json|mp4|mp3|mov)"

    # Tìm extension — *.pdf hoặc bare keyword "PDF", "png"
    m_ext = re.search(r"\*?\." + EXT_LIST, text, re.I)
    if not m_ext:
        m_ext = re.search(r"(?<!\w)" + EXT_LIST + r"(?!\w)", text, re.I)
    pattern = f"*.{m_ext.group(1).lower()}" if m_ext else "*"

    # Tìm base folder — ~/path trước, sau đó relative folder name
    m_abs = re.search(r"(~/[^\s\"',]+)", text)
    if m_abs:
        base = m_abs.group(1).rstrip("/,.")
        return (base, pattern)

    m_rel = re.search(r"(?<!\w)(" + FOLDER_PAT + r")(/[^\s\"',]*)?(?!\w)", text, re.I)
    if m_rel:
        rest = m_rel.group(2) or ""
        base = f"~/{m_rel.group(1)}{rest}"
        return (base, pattern)

    # Fallback: Desktop
    return ("~/Desktop", pattern)
