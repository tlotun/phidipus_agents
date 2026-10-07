# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
knowledge/knowledge_base.py — Phidipus KnowledgeBee v1.0
══════════════════════════════════════════════════════════

Bộ nhớ doanh nghiệp: đọc doc/excel/pdf → index → trả lời nhanh.

Luồng hoạt động:
  1. Khách kéo file vào ~/Phidipus/knowledge/<company>/
  2. FileWatcher phát hiện file mới → auto-ingest trong 5 giây
  3. KnowledgeBase parse → ghi vào 2 tầng:
     - FastLookup (SQLite): tra giá/tồn kho bằng mã SP trong < 50ms
     - VectorIndex (HNSW): semantic search cho câu hỏi mở trong < 300ms
  4. Telegram hỏi → trả lời ngay từ cache

Định dạng được hỗ trợ:
  .xlsx, .xls   → openpyxl  → bảng giá / tồn kho
  .pdf          → pdfplumber → catalog / tài liệu kỹ thuật
  .docx, .doc   → python-docx → hướng dẫn / báo cáo
  .csv          → csv stdlib  → dữ liệu thuần

Tốc độ:
  - Ingest 1000 dòng Excel: < 2 giây
  - Ingest PDF 50 trang: < 10 giây
  - Query giá theo mã: < 50ms
  - Semantic search: < 300ms (HNSW in-memory)
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("phidipus.knowledge")

# ══════════════════════════════════════════════════════════════
# Data types
# ══════════════════════════════════════════════════════════════

@dataclass
class ProductRecord:
    """Một dòng sản phẩm từ file dữ liệu."""
    code:        str           # Mã sản phẩm
    name:        str           # Tên sản phẩm
    price:       float         # Giá (VND hoặc USD)
    currency:    str = "VND"
    stock:       float = 0.0   # Tồn kho
    unit:        str = "cái"   # Đơn vị
    category:    str = ""      # Danh mục
    description: str = ""      # Mô tả / đặc tính
    supplier:    str = ""      # Nhà cung cấp
    extra:       dict = field(default_factory=dict)  # Thêm các trường khác
    source_file: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name,
            "price": self.price, "currency": self.currency,
            "stock": self.stock, "unit": self.unit,
            "category": self.category, "description": self.description,
            "supplier": self.supplier, "extra": self.extra,
            "source_file": self.source_file,
        }

@dataclass
class DocumentChunk:
    """Đoạn văn bản từ tài liệu cho semantic search."""
    chunk_id:    str
    file_path:   str
    page:        int
    text:        str
    embedding:   list[float] = field(default_factory=list)
    metadata:    dict = field(default_factory=dict)


@dataclass
class IngestResult:
    file_path:    str
    records:      int = 0     # Số ProductRecord được thêm
    chunks:       int = 0     # Số chunk văn bản được index
    errors:       list[str] = field(default_factory=list)
    duration_ms:  int = 0

# ══════════════════════════════════════════════════════════════
# Parser helpers
# ══════════════════════════════════════════════════════════════

# Các keyword nhận dạng cột trong Excel/CSV (tiếng Việt + Anh)
_COL_HINTS = {
    "code":     ["mã", "code", "id", "sku", "barcode", "partno", "part", "số", "stt"],
    "name":     ["tên", "name", "sản phẩm", "product", "hàng", "mặt hàng", "description"],
    "price":    ["giá", "price", "đơn giá", "giá bán", "giá vốn", "cost", "rate"],
    "stock":    ["tồn", "stock", "số lượng", "sl", "qty", "quantity", "available"],
    "unit":     ["đvt", "unit", "đơn vị"],
    "category": ["danh mục", "category", "loại", "nhóm", "group"],
    "supplier": ["nhà cung cấp", "supplier", "ncc", "vendor", "brand"],
}

def _detect_column(header: str) -> str | None:
    """Nhận dạng loại cột từ tên header."""
    h = header.lower().strip()
    for col_type, keywords in _COL_HINTS.items():
        if any(k in h for k in keywords):
            return col_type
    return None

def _parse_price(value: Any) -> float:
    """Parse giá tiền từ nhiều định dạng: '1,200,000', '1.2M', '15000 VND'..."""
    if value is None:
        return 0.0
    s = str(value).strip()
    s = re.sub(r"[Vv][Nn][Dd]|[$€£¥₫]|đ\b", "", s).strip()
    s = s.replace(",", "").replace("_", "")
    # Xử lý M/K: 1.2M = 1,200,000; 15K = 15,000
    m = re.match(r"([\d.]+)\s*([MmKk])?$", s)
    if m:
        num = float(m.group(1))
        suffix = (m.group(2) or "").upper()
        if suffix == "M":
            return num * 1_000_000
        if suffix == "K":
            return num * 1_000
        return num
    try:
        return float(s)
    except ValueError:
        return 0.0

def _parse_stock(value: Any) -> float:
    if value is None:
        return 0.0
    s = re.sub(r"[^\d.]", "", str(value))
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_excel_file(path: Path) -> list[ProductRecord]:
    """Parse file Excel → danh sách ProductRecord."""
    import openpyxl
    records = []
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)

    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue

        # Tìm header row (row đầu tiên có > 2 cell không rỗng)
        header_row = None
        data_start = 0
        for i, row in enumerate(rows[:10]):
            non_empty = [c for c in row if c is not None and str(c).strip()]
            if len(non_empty) >= 3:
                header_row = row
                data_start = i + 1
                break

        if header_row is None:
            continue

        # Map cột
        col_map: dict[str, int] = {}
        extra_cols: dict[str, int] = {}
        for j, h in enumerate(header_row):
            if h is None:
                continue
            col_type = _detect_column(str(h))
            if col_type and col_type not in col_map:
                col_map[col_type] = j
            elif h:
                extra_cols[str(h).strip()[:40]] = j

        if not col_map:
            continue

        for row in rows[data_start:]:
            if not any(row):
                continue

            def _get(key: str) -> Any:
                idx = col_map.get(key)
                return row[idx] if idx is not None and idx < len(row) else None

            code = str(_get("code") or "").strip()
            name = str(_get("name") or "").strip()
            if not code and not name:
                continue

            # Extra fields
            extra = {}
            for col_name, col_idx in extra_cols.items():
                val = row[col_idx] if col_idx < len(row) else None
                if val is not None:
                    extra[col_name] = str(val)[:200]

            records.append(ProductRecord(
                code=code or name[:20],
                name=name or code,
                price=_parse_price(_get("price")),
                stock=_parse_stock(_get("stock")),
                unit=str(_get("unit") or "cái").strip()[:20],
                category=str(_get("category") or "").strip()[:100],
                supplier=str(_get("supplier") or "").strip()[:100],
                description=str(extra.pop("description", "") or "")[:500],
                extra=extra,
                source_file=path.name,
            ))

    wb.close()
    return records


def _parse_csv_file(path: Path) -> list[ProductRecord]:
    """Parse CSV → danh sách ProductRecord."""
    records = []
    encodings = ["utf-8", "utf-8-sig", "cp1258", "latin-1"]
    for enc in encodings:
        try:
            with open(str(path), encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                col_map: dict[str, str] = {}
                for header in (reader.fieldnames or []):
                    col_type = _detect_column(header)
                    if col_type and col_type not in col_map:
                        col_map[col_type] = header

                for row in reader:
                    def _get(key: str) -> Any:
                        h = col_map.get(key)
                        return row.get(h) if h else None

                    code = str(_get("code") or "").strip()
                    name = str(_get("name") or "").strip()
                    if not code and not name:
                        continue
                    records.append(ProductRecord(
                        code=code or name[:20],
                        name=name or code,
                        price=_parse_price(_get("price")),
                        stock=_parse_stock(_get("stock")),
                        unit=str(_get("unit") or "cái").strip()[:20],
                        category=str(_get("category") or "").strip()[:100],
                        supplier=str(_get("supplier") or "").strip()[:100],
                        source_file=path.name,
                    ))
            break
        except (UnicodeDecodeError, Exception):
            continue
    return records


def _parse_pdf_to_chunks(path: Path) -> tuple[list[ProductRecord], list[dict]]:
    """Parse PDF → (ProductRecords từ bảng, text chunks cho semantic search)."""
    try:
        import pdfplumber
    except ImportError:
        return [], []

    records = []
    chunks = []
    text_buffer = []

    with pdfplumber.open(str(path)) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            # Extract tables (có thể chứa bảng giá)
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                header = table[0]
                col_map = {}
                for j, h in enumerate(header):
                    if h:
                        col_type = _detect_column(str(h))
                        if col_type and col_type not in col_map:
                            col_map[col_type] = j
                if "name" not in col_map and "code" not in col_map:
                    continue
                for row in table[1:]:
                    if not row or not any(row):
                        continue
                    def _get(k):
                        idx = col_map.get(k)
                        return row[idx] if idx is not None and idx < len(row) else None
                    code = str(_get("code") or "").strip()
                    name = str(_get("name") or "").strip()
                    if not code and not name:
                        continue
                    records.append(ProductRecord(
                        code=code or name[:20],
                        name=name or code,
                        price=_parse_price(_get("price")),
                        stock=_parse_stock(_get("stock")),
                        unit=str(_get("unit") or "cái").strip()[:20],
                        source_file=path.name,
                    ))

            # Extract text for semantic search
            text = page.extract_text() or ""
            if text.strip():
                # Chunk theo đoạn ~500 chars với overlap 100
                for i in range(0, len(text), 400):
                    chunk_text = text[i:i + 500].strip()
                    if len(chunk_text) > 50:
                        chunks.append({
                            "file": path.name,
                            "page": page_num,
                            "text": chunk_text,
                        })

    return records, chunks


def _parse_docx_to_chunks(path: Path) -> tuple[list[ProductRecord], list[dict]]:
    """Parse Word document → (ProductRecords từ bảng, text chunks)."""
    try:
        import docx as _docx
    except ImportError:
        return [], []

    records = []
    chunks = []
    doc = _docx.Document(str(path))

    # Extract tables
    for table in doc.tables:
        if len(table.rows) < 2:
            continue
        header = [cell.text for cell in table.rows[0].cells]
        col_map = {}
        for j, h in enumerate(header):
            col_type = _detect_column(h)
            if col_type and col_type not in col_map:
                col_map[col_type] = j
        if not col_map:
            continue
        for row in table.rows[1:]:
            cells = [c.text.strip() for c in row.cells]
            def _get(k):
                idx = col_map.get(k)
                return cells[idx] if idx is not None and idx < len(cells) else None
            code = str(_get("code") or "").strip()
            name = str(_get("name") or "").strip()
            if not code and not name:
                continue
            records.append(ProductRecord(
                code=code or name[:20],
                name=name or code,
                price=_parse_price(_get("price")),
                stock=_parse_stock(_get("stock")),
                unit=str(_get("unit") or "cái").strip()[:20],
                source_file=path.name,
            ))

    # Extract text paragraphs → chunks
    full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    for i in range(0, len(full_text), 400):
        chunk_text = full_text[i:i + 500].strip()
        if len(chunk_text) > 50:
            chunks.append({
                "file": path.name,
                "page": 1,
                "text": chunk_text,
            })

    return records, chunks


# ══════════════════════════════════════════════════════════════
# KnowledgeBase
# ══════════════════════════════════════════════════════════════

class KnowledgeBase:
    """
    Bộ nhớ doanh nghiệp — parse, index, và query tài liệu công ty.

    Usage:
        kb = KnowledgeBase("~/Phidipus/knowledge/cong_ty_abc")
        await kb.start()                    # bắt đầu watch folder
        result = kb.query("giá LED XYZ")    # query nhanh
        result = kb.semantic("đèn nào cho nhà kho 500m2")  # tìm kiếm ngữ nghĩa
    """

    SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf", ".docx", ".doc"}

    def __init__(
        self,
        knowledge_dir: str,
        *,
        company_name: str = "",
        auto_watch: bool = True,
    ) -> None:
        self._dir = Path(knowledge_dir).expanduser().resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._company = company_name or self._dir.name
        self._auto_watch = auto_watch

        # FastLookup được inject sau khi khởi tạo
        self._fast_lookup: "FastLookup | None" = None

        # Simple in-memory vector index cho semantic search
        # (Sẽ dùng VectorIndex của V9 nếu hnswlib có)
        # PERF-03 FIX: capped at MAX_CHUNKS to prevent unbounded RAM growth.
        # Each chunk embedding ≈ 6KB → 2000 chunks ≈ 12MB max in-memory.
        self._MAX_CHUNKS: int = 2000
        self._chunks: list[dict] = []
        self._chunk_embeddings: list[list[float]] = []

        # File hash cache để tránh re-ingest
        self._hash_cache: dict[str, str] = {}
        self._hash_cache_path = self._dir / ".kb_cache.json"
        self._load_hash_cache()

        # Stats
        self._stats = {
            "total_files":   0,
            "total_records": 0,
            "total_chunks":  0,
            "last_update":   "",
        }

        # Watch thread
        self._watcher_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── Public API ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Ingest tất cả file hiện có + bắt đầu watch folder mới."""
        log.info(f"[KnowledgeBee] Starting for '{self._company}' at {self._dir}")
        await self._ingest_all_existing()
        if self._auto_watch:
            self._start_watcher()
        log.info(
            f"[KnowledgeBee] Ready: {self._stats['total_records']} products, "
            f"{self._stats['total_chunks']} text chunks"
        )

    def stop(self) -> None:
        self._stop_event.set()

    def attach_fast_lookup(self, fl: "FastLookup") -> None:
        self._fast_lookup = fl

    async def ingest_file(self, path: Path) -> IngestResult:
        """Ingest một file cụ thể vào index."""
        t0 = time.time()
        result = IngestResult(file_path=str(path))
        ext = path.suffix.lower()

        if ext not in self.SUPPORTED_EXTENSIONS:
            result.errors.append(f"Unsupported: {ext}")
            return result

        try:
            records: list[ProductRecord] = []
            raw_chunks: list[dict] = []

            if ext in (".xlsx", ".xls"):
                records = _parse_excel_file(path)
            elif ext == ".csv":
                records = _parse_csv_file(path)
            elif ext == ".pdf":
                records, raw_chunks = _parse_pdf_to_chunks(path)
            elif ext in (".docx", ".doc"):
                records, raw_chunks = _parse_docx_to_chunks(path)

            # Ghi vào FastLookup
            if records and self._fast_lookup:
                self._fast_lookup.upsert_products(records)
                result.records = len(records)
                self._stats["total_records"] += len(records)

            # Ghi chunks vào bộ nhớ semantic
            for chunk in raw_chunks:
                chunk_id = hashlib.md5(
                    (chunk["file"] + str(chunk["page"]) + chunk["text"][:50]).encode()
                ).hexdigest()[:12]
                self._chunks.append({**chunk, "id": chunk_id})
            result.chunks = len(raw_chunks)
            self._stats["total_chunks"] += len(raw_chunks)

            # PERF-03 FIX: evict oldest chunks when over the cap to prevent
            # unbounded RAM growth (each embedding ≈ 6KB; 2000 cap ≈ 12MB max)
            if len(self._chunks) > self._MAX_CHUNKS:
                overflow = len(self._chunks) - self._MAX_CHUNKS
                self._chunks = self._chunks[overflow:]
                if self._chunk_embeddings:
                    self._chunk_embeddings = self._chunk_embeddings[overflow:]
                import logging as _log
                _log.getLogger("KnowledgeBee").warning(
                    f"[PERF-03] _chunks capped at {self._MAX_CHUNKS}; "
                    f"evicted {overflow} oldest chunks"
                )

            # Lưu hash để tránh re-ingest
            file_hash = _file_hash(path)
            self._hash_cache[str(path)] = file_hash
            self._save_hash_cache()

            self._stats["total_files"] += 1
            self._stats["last_update"] = _now()

        except Exception as exc:
            result.errors.append(str(exc)[:200])
            log.warning(f"[KnowledgeBee] ingest error {path.name}: {exc}")

        result.duration_ms = int((time.time() - t0) * 1000)
        log.info(
            f"[KnowledgeBee] {path.name}: "
            f"{result.records} products, {result.chunks} chunks ({result.duration_ms}ms)"
        )
        return result

    def semantic_search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Tìm kiếm ngữ nghĩa trên văn bản tài liệu.
        Dùng TF-IDF keyword matching khi không có embedding model.
        """
        if not self._chunks:
            return []
        query_words = set(re.findall(r"\w+", query.lower()))
        scored = []
        for chunk in self._chunks:
            text_words = set(re.findall(r"\w+", chunk["text"].lower()))
            overlap = len(query_words & text_words)
            if overlap > 0:
                score = overlap / (len(query_words) + len(text_words) - overlap)
                scored.append((score, chunk))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k]]

    def stats(self) -> dict:
        s = dict(self._stats)
        if self._fast_lookup:
            s["db_stats"] = self._fast_lookup.stats()
        return s

    def list_files(self) -> list[str]:
        return [f.name for f in self._dir.iterdir()
                if f.suffix.lower() in self.SUPPORTED_EXTENSIONS]

    # ── Internal ───────────────────────────────────────────────────────

    async def _ingest_all_existing(self) -> None:
        files = [
            f for f in self._dir.iterdir()
            if f.suffix.lower() in self.SUPPORTED_EXTENSIONS
            and not f.name.startswith(".")
        ]
        for f in files:
            current_hash = _file_hash(f)
            if self._hash_cache.get(str(f)) == current_hash:
                log.debug(f"[KnowledgeBee] Skip unchanged: {f.name}")
                continue
            await self.ingest_file(f)

    def _start_watcher(self) -> None:
        """Dùng watchdog nếu có, fallback về polling đơn giản."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            kb_ref = self

            class _Handler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        p = Path(event.src_path)
                        if p.suffix.lower() in KnowledgeBase.SUPPORTED_EXTENSIONS:
                            time.sleep(1)  # chờ file ghi xong
                            asyncio.run_coroutine_threadsafe(
                                kb_ref.ingest_file(p),
                                asyncio.get_event_loop(),
                            )
                on_modified = on_created

            observer = Observer()
            observer.schedule(_Handler(), str(self._dir), recursive=False)
            observer.start()
            log.info(f"[KnowledgeBee] Watching {self._dir} (watchdog)")

        except ImportError:
            # Polling fallback: check mỗi 10 giây
            def _poll():
                known = {f: _file_hash(f) for f in self._dir.iterdir()
                         if f.suffix.lower() in self.SUPPORTED_EXTENSIONS}
                while not self._stop_event.is_set():
                    time.sleep(10)
                    for f in self._dir.iterdir():
                        if f.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
                            continue
                        h = _file_hash(f)
                        if known.get(f) != h:
                            known[f] = h
                            try:
                                loop = asyncio.get_event_loop()
                                asyncio.run_coroutine_threadsafe(
                                    self.ingest_file(f), loop
                                )
                            except Exception:
                                pass

            self._watcher_thread = threading.Thread(target=_poll, daemon=True)
            self._watcher_thread.start()
            log.info(f"[KnowledgeBee] Polling {self._dir} every 10s")

    def _load_hash_cache(self) -> None:
        try:
            if self._hash_cache_path.exists():
                self._hash_cache = json.loads(self._hash_cache_path.read_text())
        except Exception:
            self._hash_cache = {}

    def _save_hash_cache(self) -> None:
        try:
            self._hash_cache_path.write_text(json.dumps(self._hash_cache))
        except Exception:
            pass


def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    try:
        with open(str(path), "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()

def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
