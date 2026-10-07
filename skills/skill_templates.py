# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
skills/skill_templates.py — Phidipus SkillTemplates v1.1  (v9.22)
══════════════════════════════════════════════════════════════════

10 template skill pre-built bao phủ 90% use case doanh nghiệp.
LLM chỉ cần phân loại intent + extract params → chạy template đúng.

v9.22 — Template Feedback Loop:
  Mỗi lần template chạy thành công → SemanticMemory.extract_from_task()
  Mỗi lần template thất bại       → FailureMemory.record_failure()
  Templates không còn là "vùng chết" trong learning loop.

Templates:
  1.  product_lookup          — tra giá/tồn kho theo mã hoặc tên
  2.  price_compare_web       — so sánh giá nội bộ vs thị trường
  3.  inventory_report        — báo cáo tồn kho (tổng/danh mục/hết hàng)
  4.  product_search_web      — tìm sản phẩm trên web
  5.  low_stock_alert         — cảnh báo hàng sắp hết
  6.  document_search         — tìm kiếm ngữ nghĩa trong tài liệu
  7.  price_range_filter      — lọc sản phẩm theo khoảng giá
  8.  category_browse         — xem sản phẩm theo danh mục
  9.  quick_answer            — trả lời câu hỏi từ tài liệu (RAG)
  10. batch_update_notice     — thông báo cập nhật bảng giá mới
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


def _resolve_model(name: str, role: str | None = None) -> str:
    """v4.3: map a legacy hard-coded model name to an installed one (model registry)."""
    try:
        from core.model_registry import resolve_model
        return resolve_model(name, role)
    except Exception:
        return name

# ══════════════════════════════════════════════════════════════
# Data types
# ══════════════════════════════════════════════════════════════

@dataclass
class SkillResult:
    """Kết quả thực thi một skill template."""
    success:       bool
    text:          str = ""        # Text trả lời cho Telegram
    data:          dict = field(default_factory=dict)  # Data structured
    files:         list[str] = field(default_factory=list)  # File đính kèm
    next_skills:   list[str] = field(default_factory=list)  # Skill tiếp theo cần chạy
    error:         str = ""

@dataclass
class ParsedIntent:
    """Kết quả LLM phân loại intent từ Telegram message."""
    template_name: str           # Tên template cần dùng
    params:        dict          # Parameters extracted
    confidence:    float = 1.0
    raw_message:   str = ""


# ══════════════════════════════════════════════════════════════
# Intent Parser (LLM-based)
# ══════════════════════════════════════════════════════════════

_INTENT_PROMPT = """Bạn là AI phân loại yêu cầu của người dùng cho hệ thống quản lý hàng hóa.

Phân tích message và trả về JSON với template phù hợp nhất.

TEMPLATES CÓ SẴN:
- product_lookup: tra cứu giá, tồn kho, thông tin sản phẩm
- price_compare_web: so sánh giá nội bộ với thị trường internet
- inventory_report: báo cáo tồn kho tổng hợp
- product_search_web: tìm sản phẩm mới trên internet
- low_stock_alert: xem sản phẩm sắp hết hàng
- document_search: tìm trong tài liệu PDF/Word
- price_range_filter: lọc sản phẩm theo giá
- category_browse: xem danh sách sản phẩm theo loại
- quick_answer: trả lời câu hỏi từ tài liệu công ty
- batch_update_notice: thông báo về cập nhật bảng giá

MESSAGE: "{message}"

Trả về JSON DUY NHẤT (không giải thích):
{{"template": "tên_template", "params": {{"key": "value"}}, "confidence": 0.95}}

Params thường dùng:
- product_code, product_name, query: từ khóa tìm kiếm
- category: danh mục hàng hóa  
- min_price, max_price: khoảng giá (số nguyên VND)
- limit: số kết quả tối đa
- compare_web: true nếu cần so sánh internet
"""

async def parse_intent(
    message: str,
    ollama_url: str = "http://127.0.0.1:11434",
    model: str = "qwen3:8b",
) -> ParsedIntent:
    """
    Dùng LLM local (qwen3:8b) phân loại intent từ Telegram message.
    Timeout 5s, fallback về product_lookup nếu timeout.
    """
    import urllib.request
    import json as _json

    prompt = _INTENT_PROMPT.format(message=message[:300])
    data = _json.dumps({
        "model": _resolve_model(model, "reasoning"),
        "think": False,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 120, "temperature": 0},
    }).encode()

    try:
        req = urllib.request.Request(
            f"{ollama_url}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        loop = asyncio.get_event_loop()
        def _call():
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read().decode()

        raw = await asyncio.wait_for(loop.run_in_executor(None, _call), timeout=5.0)
        resp_data = _json.loads(raw)
        response_text = resp_data.get("response", "").strip()

        # Extract JSON từ response
        match = re.search(r'\{[^{}]+\}', response_text, re.DOTALL)
        if match:
            parsed = _json.loads(match.group())
            return ParsedIntent(
                template_name=parsed.get("template", "product_lookup"),
                params=parsed.get("params", {}),
                confidence=float(parsed.get("confidence", 0.8)),
                raw_message=message,
            )
    except Exception:
        pass

    # Fallback: keyword-based classification
    return _keyword_classify(message)


def _keyword_classify(message: str) -> ParsedIntent:
    """Phân loại bằng keyword khi LLM không có — đủ cho 80% cases."""
    m = message.lower()

    # So sánh thị trường
    if any(k in m for k in ["so sánh", "compare", "thị trường", "market", "internet"]):
        query = _extract_product_query(message)
        return ParsedIntent("price_compare_web", {"query": query}, 0.8, message)

    # Tồn kho / hết hàng
    if any(k in m for k in ["tồn kho", "còn hàng", "hết hàng", "stock", "inventory"]):
        if any(k in m for k in ["báo cáo", "report", "tổng", "summary"]):
            return ParsedIntent("inventory_report", {}, 0.9, message)
        if any(k in m for k in ["sắp hết", "cạn", "ít", "low"]):
            return ParsedIntent("low_stock_alert", {"threshold": 10}, 0.9, message)
        return ParsedIntent("inventory_report", {}, 0.8, message)

    # Tìm theo giá
    price_match = re.search(r"(\d[\d.,]*)\s*[Kk]?\s*[-–]\s*(\d[\d.,]*)\s*[Kk]?", m)
    if price_match or any(k in m for k in ["giá dưới", "dưới", "từ", "khoảng giá"]):
        return ParsedIntent("price_range_filter", _extract_price_range(message), 0.8, message)

    # Tìm trong tài liệu
    if any(k in m for k in ["tài liệu", "catalog", "spec", "thông số kỹ thuật", "hướng dẫn"]):
        return ParsedIntent("document_search", {"query": message[:200]}, 0.8, message)

    # Danh mục
    if any(k in m for k in ["danh sách", "list", "tất cả", "loại", "nhóm"]):
        cat = _extract_category(message)
        return ParsedIntent("category_browse", {"category": cat}, 0.8, message)

    # Tìm web
    if any(k in m for k in ["tìm trên", "search", "website", "online", "mua ở đâu"]):
        return ParsedIntent("product_search_web", {"query": _extract_product_query(message)}, 0.8, message)

    # Default: tra cứu sản phẩm
    query = _extract_product_query(message)
    return ParsedIntent("product_lookup", {"query": query}, 0.7, message)


def _extract_product_query(message: str) -> str:
    """Extract tên/mã sản phẩm từ message."""
    # Loại bỏ các từ lệnh phổ biến
    noise = r"\b(tìm|kiếm|tra|cứu|xem|hỏi|giá|tồn|kho|hàng|cho|tôi|biết|bao nhiêu|là)\b"
    q = re.sub(noise, "", message, flags=re.IGNORECASE).strip()
    q = re.sub(r"\s+", " ", q).strip()
    return q[:150] if q else message[:150]

def _extract_price_range(message: str) -> dict:
    nums = re.findall(r"(\d[\d.,]*)\s*([Mm]|[Kk])?", message)
    prices = []
    for n, suffix in nums:
        val = float(n.replace(",", ""))
        if suffix.upper() == "M":
            val *= 1_000_000
        elif suffix.upper() == "K":
            val *= 1_000
        prices.append(val)
    if len(prices) >= 2:
        return {"min_price": min(prices), "max_price": max(prices)}
    if len(prices) == 1:
        return {"min_price": 0, "max_price": prices[0]}
    return {"min_price": 0, "max_price": 10_000_000}

def _extract_category(message: str) -> str:
    cat_keywords = ["đèn", "led", "bóng", "dây", "cáp", "công tắc", "ổ cắm", "máy", "thiết bị"]
    for kw in cat_keywords:
        if kw in message.lower():
            return kw
    return ""


# ══════════════════════════════════════════════════════════════
# Template Executor
# ══════════════════════════════════════════════════════════════

class SkillTemplateEngine:
    """
    Thực thi skill templates với các components được inject.

    v9.22: Template Feedback Loop — mỗi lần chạy thành công feed vào
    SemanticMemory, mỗi lần thất bại feed vào FailureMemory.
    Đây là cách templates đóng góp vào vòng học của Phidipus.

    Usage:
        engine = SkillTemplateEngine()
        engine.inject(fast_lookup=fl, knowledge_base=kb, chrome_scraper=cs,
                      semantic_memory=sm, failure_memory=fm)
        result = await engine.run("price_compare_web", {"query": "đèn LED XYZ"})
    """

    def __init__(self) -> None:
        self._components: dict[str, Any] = {}
        # v9.22: Memory references (injected, optional)
        self._semantic: Any = None
        self._failure: Any  = None

    def inject(self, **components: Any) -> None:
        self._components.update(components)
        # v9.22: Extract memory references for feedback loop
        if "semantic_memory" in components:
            self._semantic = components["semantic_memory"]
        if "failure_memory" in components:
            self._failure = components["failure_memory"]

    async def run_from_message(self, message: str, ollama_url: str = "http://127.0.0.1:11434") -> SkillResult:
        """Entry point chính: nhận Telegram message → trả kết quả."""
        intent = await parse_intent(message, ollama_url=ollama_url)
        return await self.run(intent.template_name, intent.params, original_query=message)

    async def run(self, template_name: str, params: dict, original_query: str = "") -> SkillResult:
        """Thực thi một template cụ thể với params."""
        handler = _TEMPLATE_REGISTRY.get(template_name)
        if not handler:
            return SkillResult(False, error=f"Template '{template_name}' không tồn tại")
        try:
            result = await handler(self._components, params, original_query)
            # v9.22: Template Feedback Loop
            await self._record_outcome(template_name, params, original_query, result)
            return result
        except Exception as exc:
            err_result = SkillResult(False, error=str(exc)[:200])
            # v9.22: Record exception to failure memory
            self._record_failure(template_name, original_query, str(exc))
            return err_result

    # ── v9.22: Feedback Loop helpers ─────────────────────────────────

    async def _record_outcome(
        self,
        template_name: str,
        params: dict,
        query: str,
        result: "SkillResult",
    ) -> None:
        """
        v9.22: Feed template execution outcome into memory systems.

        Success → SemanticMemory.extract_from_task()
                  Learns: file locations, column names, product patterns
        Failure → FailureMemory.record_failure()
                  Learns: error patterns, fix strategies for next time
        """
        if result.success and self._semantic:
            try:
                # Build a pseudo step-list from template result for SemanticMemory
                steps = [
                    {
                        "action": template_name,
                        "description": f"Template {template_name}: {query[:60]}",
                        "result": result.data or result.text[:200],
                    }
                ]
                # If result contains file paths, add as file_op step
                for fpath in result.files:
                    steps.append({
                        "action": "file_output",
                        "description": "output file",
                        "result": {"path": fpath, "headers": []},
                    })
                learned = self._semantic.extract_from_task(query, steps, success=True)
                if learned:
                    pass  # _vlog handled inside extract_from_task
            except Exception:
                pass

        if not result.success and self._failure:
            self._record_failure(template_name, query, result.error)

    def _record_failure(self, template_name: str, query: str, error: str) -> None:
        """Feed failure into FailureMemory for future self-healing."""
        if not self._failure:
            return
        try:
            self._failure.record_failure(
                error_type=f"Template_{template_name}",
                error_message=error[:200],
                context={"template": template_name, "query": query[:100]},
                goal=query,
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# Template Implementations
# ══════════════════════════════════════════════════════════════

async def _template_product_lookup(
    components: dict, params: dict, query: str
) -> SkillResult:
    """
    Template 1: Tra cứu sản phẩm theo mã hoặc tên.
    Input:  {"query": "LED XYZ 12W"} hoặc {"product_code": "LED-XYZ-001"}
    Output: Thông tin giá/tồn kho/mô tả
    """
    fl = components.get("fast_lookup")
    kb = components.get("knowledge_base")

    search_q = params.get("query") or params.get("product_code") or params.get("product_name") or query
    if not search_q:
        return SkillResult(False, error="Cần nhập tên hoặc mã sản phẩm")

    if not fl:
        return SkillResult(False, error="Knowledge base chưa được khởi tạo")

    results = fl.search(search_q, limit=5)

    if not results:
        # Thử semantic search
        if kb:
            chunks = kb.semantic_search(search_q, top_k=3)
            if chunks:
                text = (
                    f"⚠️ Không tìm thấy sản phẩm khớp *{_esc(search_q[:50])}* trong bảng giá\\.\n\n"
                    f"📚 *Thông tin liên quan từ tài liệu:*\n"
                )
                for c in chunks[:2]:
                    text += f"\n_{_esc(c['text'][:200])}_\n"
                return SkillResult(True, text=text, data={"type": "document_fallback"})
        return SkillResult(True, text=f"Không tìm thấy sản phẩm: *{_esc(search_q[:50])}*")

    if len(results) == 1:
        r = results[0]
        return SkillResult(
            True,
            text=r.to_telegram(),
            data={"products": [r.to_dict()], "count": 1},
        )

    # Nhiều kết quả → hiển thị danh sách ngắn
    lines = [f"🔍 Tìm thấy *{len(results)}* sản phẩm cho _{_esc(search_q[:50])}_:\n"]
    for i, r in enumerate(results, 1):
        stock_icon = "✅" if r.stock > 0 else "❌"
        lines.append(
            f"{i}\\. {stock_icon} `{_esc(r.code)}` — *{_esc(r.name[:40])}*\n"
            f"   💰 {_esc(r.format_price())} | 📦 {_esc(r.format_stock())}"
        )
    lines.append("\n_Gõ mã sản phẩm để xem chi tiết_")

    return SkillResult(
        True,
        text="\n".join(lines),
        data={"products": [r.to_dict() for r in results], "count": len(results)},
        next_skills=["price_compare_web"] if any(r.price > 0 for r in results) else [],
    )


async def _template_price_compare_web(
    components: dict, params: dict, query: str
) -> SkillResult:
    """
    Template 2: So sánh giá nội bộ vs thị trường internet.
    Input:  {"query": "đèn LED XYZ 12W"}
    Output: Bảng so sánh giá từ nhiều nguồn
    """
    fl = components.get("fast_lookup")
    chrome = components.get("chrome_scraper")
    search_q = params.get("query") or query

    lines = [f"📊 *So sánh giá: {_esc(search_q[:50])}*\n"]

    # Giá nội bộ
    internal_price = 0.0
    if fl:
        internal_results = fl.search(search_q, limit=3)
        if internal_results:
            lines.append("🏢 *Giá nội bộ:*")
            for r in internal_results[:3]:
                lines.append(f"  • `{_esc(r.code)}` — *{_esc(r.format_price())}*")
                if r.stock > 0:
                    lines.append(f"    Tồn: {_esc(r.format_stock())}")
            internal_price = internal_results[0].price
            lines.append("")

    # Giá thị trường (Chrome scraper)
    if chrome:
        lines.append("🌐 *Giá thị trường:*")
        try:
            web_prices = await chrome.search_prices(search_q, max_sites=4)
            if web_prices:
                for item in web_prices[:5]:
                    lines.append(
                        f"  • [{_esc(item['site'])}]({item['url']}) — *{_esc(item['price_text'])}*"
                    )
                # Tính giá trung bình
                valid_prices = [p["price"] for p in web_prices if p.get("price", 0) > 0]
                if valid_prices:
                    avg = sum(valid_prices) / len(valid_prices)
                    min_p = min(valid_prices)
                    max_p = max(valid_prices)
                    lines.append(
                        f"\n📈 *Thị trường:* "
                        f"thấp nhất {_fmt_price(min_p)} — "
                        f"cao nhất {_fmt_price(max_p)} — "
                        f"trung bình {_fmt_price(avg)}"
                    )
                    if internal_price > 0:
                        diff_pct = ((internal_price - avg) / avg) * 100
                        icon = "⬇️" if diff_pct < 0 else "⬆️"
                        lines.append(
                            f"{icon} Giá nội bộ "
                            f"{'thấp hơn' if diff_pct < 0 else 'cao hơn'} "
                            f"thị trường *{abs(diff_pct):.1f}%*"
                        )
            else:
                lines.append("  _Không tìm thấy giá trên internet_")
        except Exception as e:
            lines.append(f"  ⚠️ Lỗi tìm web: {_esc(str(e)[:60])}")
    else:
        lines.append("💡 _Để so sánh thị trường, cần bật Chrome automation_")

    return SkillResult(
        True,
        text="\n".join(lines),
        data={"query": search_q, "internal_price": internal_price},
    )


async def _template_inventory_report(
    components: dict, params: dict, query: str
) -> SkillResult:
    """Template 3: Báo cáo tồn kho tổng hợp."""
    fl = components.get("fast_lookup")
    if not fl:
        return SkillResult(False, error="Knowledge base chưa khởi tạo")

    stats = fl.stats()
    lines = [
        "📋 *BÁO CÁO TỒN KHO*\n",
        f"📦 Tổng sản phẩm: *{stats['total_products']}*",
        f"✅ Còn hàng: *{stats['in_stock']}*",
        f"❌ Hết hàng: *{stats['out_of_stock']}*",
        "",
        "🗂 *Theo danh mục:*",
    ]
    for cat in stats["top_categories"][:8]:
        lines.append(f"  • {_esc(cat['category'] or 'Khác')}: *{cat['count']}* SP")

    # Hàng sắp hết
    low = fl.low_stock(threshold=params.get("threshold", 10))
    if low:
        lines.append(f"\n⚠️ *Sắp hết hàng \\({len(low)} SP\\):*")
        for r in low[:5]:
            lines.append(f"  • `{_esc(r.code)}` {_esc(r.name[:30])} — còn *{r.stock:.0f}* {_esc(r.unit)}")
        if len(low) > 5:
            lines.append(f"  _\\.\\.\\.và {len(low)-5} sản phẩm khác_")

    return SkillResult(True, text="\n".join(lines), data=stats)


async def _template_low_stock_alert(
    components: dict, params: dict, query: str
) -> SkillResult:
    """Template 5: Cảnh báo sản phẩm sắp hết hàng."""
    fl = components.get("fast_lookup")
    if not fl:
        return SkillResult(False, error="Knowledge base chưa khởi tạo")

    threshold = float(params.get("threshold", 10))
    low = fl.low_stock(threshold=threshold)
    out = fl.out_of_stock()

    lines = [f"⚠️ *CẢNH BÁO TỒN KHO*\n"]
    if out:
        lines.append(f"🔴 *Hết hàng: {len(out)} sản phẩm*")
        for r in out[:10]:
            lines.append(f"  ❌ `{_esc(r.code)}` — {_esc(r.name[:40])}")
        if len(out) > 10:
            lines.append(f"  _\\.\\.\\.và {len(out)-10} SP khác_")
        lines.append("")

    if low:
        lines.append(f"🟡 *Sắp hết \\(≤{threshold:.0f}\\): {len(low)} sản phẩm*")
        for r in low[:10]:
            lines.append(
                f"  ⚠️ `{_esc(r.code)}` — {_esc(r.name[:30])} "
                f"\\(còn *{r.stock:.0f}* {_esc(r.unit)}\\)"
            )

    if not out and not low:
        lines.append("✅ Tất cả sản phẩm còn hàng đủ")

    return SkillResult(True, text="\n".join(lines),
                       data={"out_of_stock": len(out), "low_stock": len(low)})


async def _template_document_search(
    components: dict, params: dict, query: str
) -> SkillResult:
    """Template 6: Tìm kiếm ngữ nghĩa trong tài liệu."""
    kb = components.get("knowledge_base")
    search_q = params.get("query") or query

    if not kb:
        return SkillResult(False, error="Knowledge base chưa khởi tạo")

    chunks = kb.semantic_search(search_q, top_k=5)
    if not chunks:
        return SkillResult(True, text=f"Không tìm thấy thông tin về: *{_esc(search_q[:50])}*")

    lines = [f"📚 *Kết quả tìm trong tài liệu:*\n_{_esc(search_q[:60])}_\n"]
    for i, c in enumerate(chunks[:3], 1):
        lines.append(f"*{i}\\. {_esc(c.get('file', ''))}* \\(trang {c.get('page', 1)}\\):")
        lines.append(f"_{_esc(c['text'][:250])}_\n")

    return SkillResult(True, text="\n".join(lines),
                       data={"chunks": chunks[:5], "query": search_q})


async def _template_price_range_filter(
    components: dict, params: dict, query: str
) -> SkillResult:
    """Template 7: Lọc sản phẩm theo khoảng giá."""
    fl = components.get("fast_lookup")
    if not fl:
        return SkillResult(False, error="Knowledge base chưa khởi tạo")

    min_p = float(params.get("min_price", 0))
    max_p = float(params.get("max_price", 10_000_000))
    results = fl.by_price_range(min_p, max_p, limit=20)

    lines = [
        f"💰 *Sản phẩm giá {_fmt_price(min_p)} – {_fmt_price(max_p)}*\n"
        f"Tìm thấy *{len(results)}* sản phẩm\n"
    ]
    for r in results[:15]:
        stock_icon = "✅" if r.stock > 0 else "❌"
        lines.append(
            f"{stock_icon} `{_esc(r.code)}` *{_fmt_price(r.price)}*\n"
            f"   {_esc(r.name[:50])}"
        )

    return SkillResult(True, text="\n".join(lines),
                       data={"products": [r.to_dict() for r in results]})


async def _template_category_browse(
    components: dict, params: dict, query: str
) -> SkillResult:
    """Template 8: Xem sản phẩm theo danh mục."""
    fl = components.get("fast_lookup")
    if not fl:
        return SkillResult(False, error="Knowledge base chưa khởi tạo")

    category = params.get("category") or query
    results = fl.by_category(category, limit=20)

    lines = [f"🗂 *Danh mục: {_esc(category[:50])}* \\({len(results)} sản phẩm\\)\n"]
    for r in results[:15]:
        stock_icon = "✅" if r.stock > 0 else "❌"
        lines.append(
            f"{stock_icon} `{_esc(r.code)}` — *{_esc(r.name[:40])}*\n"
            f"   💰 {_esc(r.format_price())} | {_esc(r.format_stock())}"
        )

    return SkillResult(True, text="\n".join(lines),
                       data={"products": [r.to_dict() for r in results]})


async def _template_quick_answer(
    components: dict, params: dict, query: str
) -> SkillResult:
    """Template 9: Trả lời câu hỏi từ tài liệu công ty (RAG)."""
    kb = components.get("knowledge_base")
    fl = components.get("fast_lookup")
    search_q = params.get("query") or query

    context_parts = []

    # Lấy context từ structured data
    if fl:
        products = fl.search(search_q, limit=3)
        if products:
            context_parts.append("Sản phẩm liên quan:\n" +
                "\n".join(f"- {r.name}: {r.format_price()}" for r in products))

    # Lấy context từ document chunks
    if kb:
        chunks = kb.semantic_search(search_q, top_k=3)
        if chunks:
            context_parts.append("Tài liệu:\n" +
                "\n".join(c["text"][:300] for c in chunks))

    if not context_parts:
        return SkillResult(True,
            text=f"Không tìm thấy thông tin về: *{_esc(search_q[:50])}*")

    # Tổng hợp câu trả lời đơn giản (không gọi LLM để nhanh)
    lines = [f"💡 *Thông tin về: {_esc(search_q[:60])}*\n"]
    for part in context_parts:
        lines.append(f"_{_esc(part[:400])}_\n")

    return SkillResult(True, text="\n".join(lines),
                       data={"context": context_parts, "query": search_q})


async def _template_product_search_web(
    components: dict, params: dict, query: str
) -> SkillResult:
    """Template 4: Tìm sản phẩm trên internet."""
    chrome = components.get("chrome_scraper")
    search_q = params.get("query") or query

    if not chrome:
        return SkillResult(True,
            text=f"💡 Cần bật Chrome automation để tìm web\\.\n"
                 f"Query: _{_esc(search_q[:80])}_")

    try:
        results = await chrome.search_products(search_q, max_results=5)
        lines = [f"🌐 *Kết quả web: {_esc(search_q[:50])}*\n"]
        for r in results[:5]:
            lines.append(
                f"• [{_esc(r.get('title','')[:50])}]({r.get('url','')}) "
                f"— {_esc(r.get('price_text','Không rõ giá'))}"
            )
        return SkillResult(True, text="\n".join(lines), data={"web_results": results})
    except Exception as e:
        return SkillResult(False, error=str(e)[:100])


async def _template_batch_update_notice(
    components: dict, params: dict, query: str
) -> SkillResult:
    """Template 10: Thông báo cập nhật bảng giá."""
    fl = components.get("fast_lookup")
    kb = components.get("knowledge_base")
    if not fl:
        return SkillResult(False, error="Knowledge base chưa khởi tạo")

    stats = fl.stats()
    kb_stats = kb.stats() if kb else {}
    last_update = kb_stats.get("last_update", "Không rõ")

    text = (
        f"🔄 *CẬP NHẬT DỮ LIỆU*\n\n"
        f"📅 Lần cuối: _{_esc(str(last_update)[:30])}_\n"
        f"📦 Tổng sản phẩm: *{stats['total_products']}*\n"
        f"📁 Files đã xử lý: *{kb_stats.get('total_files', 0)}*\n\n"
        f"_Kéo file Excel/PDF mới vào folder để cập nhật tự động_"
    )
    return SkillResult(True, text=text, data={"stats": stats})


# ══════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════

_TEMPLATE_REGISTRY: dict[str, Callable] = {
    "product_lookup":      _template_product_lookup,
    "price_compare_web":   _template_price_compare_web,
    "inventory_report":    _template_inventory_report,
    "product_search_web":  _template_product_search_web,
    "low_stock_alert":     _template_low_stock_alert,
    "document_search":     _template_document_search,
    "price_range_filter":  _template_price_range_filter,
    "category_browse":     _template_category_browse,
    "quick_answer":        _template_quick_answer,
    "batch_update_notice": _template_batch_update_notice,
}

def list_templates() -> list[str]:
    return list(_TEMPLATE_REGISTRY.keys())


# ── Helpers ────────────────────────────────────────────────────────────

def _fmt_price(price: float) -> str:
    if price <= 0:
        return "0đ"
    if price >= 1_000_000:
        return f"{price/1_000_000:.1f}M đ"
    if price >= 1_000:
        return f"{price/1_000:.0f}K đ"
    return f"{price:.0f} đ"

def _esc(text: str) -> str:
    for c in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(c, f"\\{c}")
    return text
