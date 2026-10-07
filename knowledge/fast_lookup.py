# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
knowledge/fast_lookup.py — Phidipus FastLookup v1.0
═════════════════════════════════════════════════════

Tra cứu nhanh giá / tồn kho / thông số sản phẩm bằng SQLite.
Tốc độ: < 50ms cho mọi query.

Các cách tra cứu:
  1. Mã chính xác:  lookup("LED-XYZ-001")
  2. Tên gần đúng:  search("đèn led xyz")   → fuzzy match
  3. Danh mục:      by_category("đèn led")
  4. Khoảng giá:    by_price_range(0, 500000)
  5. Tồn kho thấp:  low_stock(threshold=10)
  6. Full-text:     fulltext("bóng đèn tiết kiệm điện")

Tất cả query đều dùng FTS5 (Full-Text Search) của SQLite — không cần
thư viện ngoài, hoạt động offline.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .knowledge_base import ProductRecord


# ══════════════════════════════════════════════════════════════
# Result types
# ══════════════════════════════════════════════════════════════

@dataclass
class LookupResult:
    """Kết quả tra cứu một sản phẩm."""
    code:        str
    name:        str
    price:       float
    currency:    str
    stock:       float
    unit:        str
    category:    str
    description: str
    supplier:    str
    extra:       dict
    source_file: str
    match_score: float = 1.0   # 1.0 = exact match, < 1.0 = fuzzy

    def format_price(self) -> str:
        if self.price <= 0:
            return "Liên hệ"
        p = self.price
        if self.currency == "VND":
            if p >= 1_000_000:
                return f"{p/1_000_000:.1f}M đ"
            if p >= 1_000:
                return f"{p/1_000:.0f}K đ"
            return f"{p:.0f} đ"
        return f"{p:.2f} {self.currency}"

    def format_stock(self) -> str:
        if self.stock <= 0:
            return "Hết hàng"
        if self.stock < 10:
            return f"Còn {self.stock:.0f} {self.unit} (ít)"
        return f"Còn {self.stock:.0f} {self.unit}"

    def to_telegram(self) -> str:
        """Format đẹp cho Telegram reply."""
        lines = [
            f"📦 *{_esc(self.name)}*",
            f"🔖 Mã: `{_esc(self.code)}`",
            f"💰 Giá: *{_esc(self.format_price())}*",
            f"📊 Tồn: {_esc(self.format_stock())}",
        ]
        if self.category:
            lines.append(f"🗂 Danh mục: {_esc(self.category)}")
        if self.supplier:
            lines.append(f"🏭 Nhà CC: {_esc(self.supplier)}")
        if self.description:
            lines.append(f"📝 {_esc(self.description[:200])}")
        if self.extra:
            for k, v in list(self.extra.items())[:3]:
                lines.append(f"  • {_esc(k)}: {_esc(str(v))}")
        lines.append(f"_Nguồn: {_esc(self.source_file)}_")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name,
            "price": self.price, "currency": self.currency,
            "price_formatted": self.format_price(),
            "stock": self.stock, "unit": self.unit,
            "stock_formatted": self.format_stock(),
            "category": self.category, "description": self.description,
            "supplier": self.supplier, "extra": self.extra,
            "source_file": self.source_file,
        }


# ══════════════════════════════════════════════════════════════
# FastLookup
# ══════════════════════════════════════════════════════════════

class FastLookup:
    """
    SQLite-backed product catalog với full-text search.

    Tạo một instance duy nhất per company, chia sẻ qua dependency injection.

    Usage:
        fl = FastLookup("~/Phidipus/knowledge/cong_ty_abc/products.db")
        results = fl.search("đèn led xyz 12w")
        # → [LookupResult(name="Đèn LED XYZ-001 12W", price=85000, ...)]
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ── Setup ──────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=10,
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    code        TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    name_norm   TEXT NOT NULL,
                    price       REAL DEFAULT 0,
                    currency    TEXT DEFAULT 'VND',
                    stock       REAL DEFAULT 0,
                    unit        TEXT DEFAULT 'cái',
                    category    TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    supplier    TEXT DEFAULT '',
                    extra       TEXT DEFAULT '{}',
                    source_file TEXT DEFAULT '',
                    updated_at  TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (code, source_file)
                );

                CREATE INDEX IF NOT EXISTS idx_products_name_norm
                    ON products(name_norm);
                CREATE INDEX IF NOT EXISTS idx_products_category
                    ON products(category);
                CREATE INDEX IF NOT EXISTS idx_products_price
                    ON products(price);
                CREATE INDEX IF NOT EXISTS idx_products_stock
                    ON products(stock);

                CREATE VIRTUAL TABLE IF NOT EXISTS products_fts
                    USING fts5(
                        code, name, category, description, supplier,
                        content='products',
                        content_rowid='rowid'
                    );

                CREATE TRIGGER IF NOT EXISTS products_fts_insert
                AFTER INSERT ON products BEGIN
                    INSERT INTO products_fts(rowid, code, name, category, description, supplier)
                    VALUES (new.rowid, new.code, new.name, new.category, new.description, new.supplier);
                END;

                CREATE TRIGGER IF NOT EXISTS products_fts_delete
                AFTER DELETE ON products BEGIN
                    INSERT INTO products_fts(products_fts, rowid, code, name, category, description, supplier)
                    VALUES ('delete', old.rowid, old.code, old.name, old.category, old.description, old.supplier);
                END;

                CREATE TRIGGER IF NOT EXISTS products_fts_update
                AFTER UPDATE ON products BEGIN
                    INSERT INTO products_fts(products_fts, rowid, code, name, category, description, supplier)
                    VALUES ('delete', old.rowid, old.code, old.name, old.category, old.description, old.supplier);
                    INSERT INTO products_fts(rowid, code, name, category, description, supplier)
                    VALUES (new.rowid, new.code, new.name, new.category, new.description, new.supplier);
                END;
            """)
            conn.commit()

    # ── Write ──────────────────────────────────────────────────────────

    def upsert_products(self, records: list[ProductRecord]) -> int:
        """Thêm hoặc cập nhật danh sách sản phẩm. Trả về số record đã ghi."""
        if not records:
            return 0
        with self._lock:
            conn = self._get_conn()
            count = 0
            for r in records:
                try:
                    conn.execute("""
                        INSERT INTO products
                            (code, name, name_norm, price, currency, stock, unit,
                             category, description, supplier, extra, source_file, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                        ON CONFLICT(code, source_file) DO UPDATE SET
                            name=excluded.name, name_norm=excluded.name_norm,
                            price=excluded.price, stock=excluded.stock,
                            unit=excluded.unit, category=excluded.category,
                            description=excluded.description, supplier=excluded.supplier,
                            extra=excluded.extra, updated_at=excluded.updated_at
                    """, (
                        r.code, r.name, _normalize(r.name),
                        r.price, r.currency, r.stock, r.unit,
                        r.category, r.description, r.supplier,
                        json.dumps(r.extra, ensure_ascii=False),
                        r.source_file,
                    ))
                    count += 1
                except Exception as e:
                    pass  # skip bad records, continue
            conn.commit()
            return count

    def delete_by_file(self, source_file: str) -> int:
        """Xóa toàn bộ products từ một file (khi file bị xóa/thay thế)."""
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute("DELETE FROM products WHERE source_file=?", (source_file,))
            conn.commit()
            return cur.rowcount

    # ── Read — Exact & Fast ─────────────────────────────────────────────

    def lookup_by_code(self, code: str) -> LookupResult | None:
        """Tra cứu chính xác theo mã sản phẩm (< 10ms)."""
        code_norm = code.strip().upper()
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM products WHERE upper(code)=? LIMIT 1",
                (code_norm,)
            ).fetchone()
        return _row_to_result(row, 1.0) if row else None

    def search(self, query: str, limit: int = 5) -> list[LookupResult]:
        """
        Tìm kiếm thông minh theo tên/mã/danh mục (< 50ms).

        Chiến lược theo độ ưu tiên:
          1. Exact code match
          2. FTS5 full-text search
          3. Fuzzy name match (difflib)
        """
        query = query.strip()
        if not query:
            return []

        results: list[LookupResult] = []

        # 1. Exact code
        exact = self.lookup_by_code(query)
        if exact:
            results.append(exact)

        # 2. FTS5
        fts_results = self._fts_search(query, limit=limit * 2)
        seen_codes = {r.code for r in results}
        for r in fts_results:
            if r.code not in seen_codes:
                results.append(r)
                seen_codes.add(r.code)

        # 3. Fuzzy nếu chưa đủ kết quả
        if len(results) < limit:
            fuzzy = self._fuzzy_search(query, limit=limit)
            for r in fuzzy:
                if r.code not in seen_codes:
                    results.append(r)
                    seen_codes.add(r.code)

        return results[:limit]

    def by_category(self, category: str, limit: int = 20) -> list[LookupResult]:
        """Lấy sản phẩm theo danh mục."""
        cat_norm = _normalize(category)
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT * FROM products
                   WHERE name_norm LIKE ? OR category LIKE ?
                   ORDER BY name LIMIT ?""",
                (f"%{cat_norm}%", f"%{category}%", limit)
            ).fetchall()
        return [_row_to_result(r) for r in rows]

    def by_price_range(
        self, min_price: float, max_price: float, limit: int = 20
    ) -> list[LookupResult]:
        """Lấy sản phẩm trong khoảng giá."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT * FROM products
                   WHERE price BETWEEN ? AND ?
                   ORDER BY price LIMIT ?""",
                (min_price, max_price, limit)
            ).fetchall()
        return [_row_to_result(r) for r in rows]

    def low_stock(self, threshold: float = 10, limit: int = 50) -> list[LookupResult]:
        """Sản phẩm tồn kho thấp — dùng cho cảnh báo hàng sắp hết."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT * FROM products
                   WHERE stock > 0 AND stock <= ?
                   ORDER BY stock LIMIT ?""",
                (threshold, limit)
            ).fetchall()
        return [_row_to_result(r) for r in rows]

    def out_of_stock(self) -> list[LookupResult]:
        """Sản phẩm hết hàng (stock = 0)."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM products WHERE stock <= 0 ORDER BY name LIMIT 200"
            ).fetchall()
        return [_row_to_result(r) for r in rows]

    def all_products(self, limit: int = 1000) -> list[LookupResult]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM products ORDER BY category, name LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_result(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            conn = self._get_conn()
            total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            out = conn.execute("SELECT COUNT(*) FROM products WHERE stock<=0").fetchone()[0]
            cats = conn.execute(
                "SELECT category, COUNT(*) as n FROM products GROUP BY category ORDER BY n DESC LIMIT 10"
            ).fetchall()
        return {
            "total_products": total,
            "out_of_stock":   out,
            "in_stock":       total - out,
            "top_categories": [{"category": r[0], "count": r[1]} for r in cats],
        }

    # ── Internal search helpers ─────────────────────────────────────────

    def _fts_search(self, query: str, limit: int = 10) -> list[LookupResult]:
        """FTS5 full-text search."""
        # Build FTS query: escape special chars
        fts_query = re.sub(r'[^\w\s]', ' ', query).strip()
        if not fts_query:
            return []
        # Thêm wildcard cho từng từ
        terms = fts_query.split()
        fts_query = " OR ".join(f'"{t}"*' for t in terms if t)

        try:
            with self._lock:
                conn = self._get_conn()
                rows = conn.execute(
                    """SELECT p.*, rank as score
                       FROM products_fts
                       JOIN products p ON products_fts.rowid = p.rowid
                       WHERE products_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (fts_query, limit)
                ).fetchall()
            return [_row_to_result(r) for r in rows]
        except Exception:
            return []

    def _fuzzy_search(self, query: str, limit: int = 5) -> list[LookupResult]:
        """Fuzzy name matching dùng difflib (stdlib, no deps)."""
        qnorm = _normalize(query)
        with self._lock:
            conn = self._get_conn()
            # Lấy tất cả name_norm để so sánh (chỉ 1 column, nhanh)
            rows = conn.execute(
                "SELECT code, name, name_norm, rowid FROM products"
            ).fetchall()

        names = [r["name_norm"] for r in rows]
        matches = difflib.get_close_matches(qnorm, names, n=limit, cutoff=0.4)
        results = []
        for match in matches:
            for row in rows:
                if row["name_norm"] == match:
                    score = difflib.SequenceMatcher(None, qnorm, match).ratio()
                    with self._lock:
                        full = self._get_conn().execute(
                            "SELECT * FROM products WHERE rowid=?", (row["rowid"],)
                        ).fetchone()
                    if full:
                        results.append(_row_to_result(full, score))
                    break
        return results

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _normalize(text: str) -> str:
    """Chuẩn hóa text: bỏ dấu, lowercase, loại ký tự đặc biệt."""
    text = text.lower().strip()
    # Bỏ dấu tiếng Việt
    nfkd = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Giữ chữ số, chữ cái, khoảng trắng
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _row_to_result(row: sqlite3.Row | Any, score: float = 0.9) -> LookupResult:
    """Convert SQLite Row → LookupResult."""
    try:
        extra = json.loads(row["extra"] or "{}")
    except Exception:
        extra = {}
    return LookupResult(
        code=row["code"] or "",
        name=row["name"] or "",
        price=row["price"] or 0.0,
        currency=row["currency"] or "VND",
        stock=row["stock"] or 0.0,
        unit=row["unit"] or "cái",
        category=row["category"] or "",
        description=row["description"] or "",
        supplier=row["supplier"] or "",
        extra=extra,
        source_file=row["source_file"] or "",
        match_score=score,
    )

def _esc(text: str) -> str:
    """Escape Markdown V2 cho Telegram."""
    for c in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(c, f"\\{c}")
    return text
