# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
core/document_generator.py — Phidipus v1.0
═══════════════════════════════════════════════════════════════════════

Office Document Generator — Creates DOCX, XLSX, HTML from workflow data.

Supported outputs:
  xlsx  — Spreadsheet with data tables + charts (openpyxl)
  docx  — Word document with headings, tables, lists (python-docx)
  html  — Styled HTML report with charts (no deps)

AI Integration:
  Uses LLM to structure raw text → tables, charts, sections.
  Model: qwen3:8b (local) → gemini-flash (cloud fallback)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


def _resolve_model(name: str, role: str | None = None) -> str:
    """v4.3: map a legacy hard-coded model name to an installed one (model registry)."""
    try:
        from core.model_registry import resolve_model
        return resolve_model(name, role)
    except Exception:
        return name

def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")

_REPORTS_DIR = Path(__file__).parent.parent / "data" / "reports"
_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════
# XLSX Generator (openpyxl)
# ══════════════════════════════════════════════════════════════

def create_xlsx(
    filename: str,
    sheets: list[dict],
    charts: list[dict] = None,
) -> str:
    """
    Create Excel workbook with multiple sheets + charts.
    
    sheets: [
      {"name": "Products", "headers": ["Name","Price","Qty"], 
       "rows": [["iPhone",999,50], ["Galaxy",799,30]]},
      {"name": "Summary", "headers": ["Metric","Value"],
       "rows": [["Total Revenue","$89,650"], ["Items","80"]]}
    ]
    
    charts: [
      {"sheet": "Products", "type": "bar", "title": "Price Comparison",
       "x_col": 0, "y_col": 1, "position": "E2"}
    ]
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.chart import BarChart, LineChart, PieChart, Reference
        from openpyxl.utils import get_column_letter
    except ImportError:
        return _create_xlsx_fallback_html(filename, sheets)

    wb = Workbook()
    
    # Styles
    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="22C55E", end_color="22C55E", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin", color="D4D4D4"),
        right=Side(style="thin", color="D4D4D4"),
        top=Side(style="thin", color="D4D4D4"),
        bottom=Side(style="thin", color="D4D4D4"),
    )
    
    for si, sheet_data in enumerate(sheets):
        if si == 0:
            ws = wb.active
            ws.title = sheet_data.get("name", "Sheet1")
        else:
            ws = wb.create_sheet(sheet_data.get("name", f"Sheet{si+1}"))
        
        headers = sheet_data.get("headers", [])
        rows = sheet_data.get("rows", [])
        
        # Headers
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=ci, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border
        
        # Data rows
        for ri, row in enumerate(rows, 2):
            for ci, val in enumerate(row, 1):
                cell = ws.cell(row=ri, column=ci, value=_smart_value(val))
                cell.border = thin_border
                if isinstance(val, (int, float)):
                    cell.alignment = Alignment(horizontal="right")
        
        # Auto-width
        for ci in range(1, len(headers) + 1):
            max_len = max(
                len(str(headers[ci-1])) if ci-1 < len(headers) else 5,
                *[len(str(r[ci-1])) for r in rows if ci-1 < len(r)],
                5
            )
            ws.column_dimensions[get_column_letter(ci)].width = min(max_len + 4, 40)
        
        # Freeze header
        ws.freeze_panes = "A2"
    
    # Charts
    for chart_cfg in (charts or []):
        sheet_name = chart_cfg.get("sheet", sheets[0]["name"] if sheets else "Sheet1")
        ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.active
        
        chart_type = chart_cfg.get("type", "bar")
        data_rows = len(ws["A"]) 
        
        if chart_type == "bar":
            chart = BarChart()
        elif chart_type == "line":
            chart = LineChart()
        elif chart_type == "pie":
            chart = PieChart()
        else:
            chart = BarChart()
        
        chart.title = chart_cfg.get("title", "Chart")
        chart.style = 10
        
        x_col = chart_cfg.get("x_col", 0) + 1
        y_col = chart_cfg.get("y_col", 1) + 1
        
        data_ref = Reference(ws, min_col=y_col, min_row=1, max_row=data_rows)
        cats_ref = Reference(ws, min_col=x_col, min_row=2, max_row=data_rows)
        
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        chart.width = 18
        chart.height = 12
        
        pos = chart_cfg.get("position", f"{get_column_letter(len(sheets[0].get('headers',[]))+2)}2")
        ws.add_chart(chart, pos)
    
    filepath = _REPORTS_DIR / filename
    wb.save(str(filepath))
    _vlog("📊", f"XLSX created: {filepath}")
    return str(filepath)


# ══════════════════════════════════════════════════════════════
# DOCX Generator (python-docx)
# ══════════════════════════════════════════════════════════════

def create_docx(
    filename: str,
    title: str = "",
    sections: list[dict] = None,
    tables: list[dict] = None,
) -> str:
    """
    Create Word document with sections and tables.
    
    sections: [
      {"heading": "Executive Summary", "level": 1, 
       "content": "This report analyzes..."},
      {"heading": "Market Analysis", "level": 2,
       "content": "The AI agent market is expected to...",
       "bullets": ["Point 1", "Point 2"]}
    ]
    
    tables: [
      {"title": "Revenue Table", "headers": ["Q","Revenue"],
       "rows": [["Q1","$100K"], ["Q2","$150K"]]}
    ]
    """
    try:
        from docx import Document
        from docx.shared import Inches, Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_TABLE_ALIGNMENT
    except ImportError:
        return _create_docx_fallback_html(filename, title, sections, tables)

    doc = Document()
    
    # Title
    if title:
        heading = doc.add_heading(title, level=0)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        doc.add_paragraph(f"Generated: {time.strftime('%Y-%m-%d %H:%M')} · Phidipus Agent OS")
        doc.add_paragraph("")
    
    # Sections
    for sec in (sections or []):
        level = sec.get("level", 1)
        heading_text = sec.get("heading", "")
        content = sec.get("content", "")
        bullets = sec.get("bullets", [])
        
        if heading_text:
            doc.add_heading(heading_text, level=min(level, 4))
        
        if content:
            doc.add_paragraph(content)
        
        for bullet in bullets:
            doc.add_paragraph(bullet, style="List Bullet")
    
    # Tables
    for tbl in (tables or []):
        if tbl.get("title"):
            doc.add_heading(tbl["title"], level=2)
        
        headers = tbl.get("headers", [])
        rows = tbl.get("rows", [])
        
        if headers:
            table = doc.add_table(rows=1 + len(rows), cols=len(headers))
            table.style = "Light Grid Accent 1"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            
            # Headers
            for ci, h in enumerate(headers):
                cell = table.rows[0].cells[ci]
                cell.text = str(h)
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.bold = True
            
            # Rows
            for ri, row in enumerate(rows):
                for ci, val in enumerate(row):
                    if ci < len(table.columns):
                        table.rows[ri + 1].cells[ci].text = str(val)
        
        doc.add_paragraph("")
    
    filepath = _REPORTS_DIR / filename
    doc.save(str(filepath))
    _vlog("📄", f"DOCX created: {filepath}")
    return str(filepath)


# ══════════════════════════════════════════════════════════════
# HTML Report with Charts (no deps)
# ══════════════════════════════════════════════════════════════

def create_html_report(
    filename: str,
    title: str = "",
    sections: list[dict] = None,
    tables: list[dict] = None,
    charts: list[dict] = None,
) -> str:
    """
    Create styled HTML report with charts (pure CSS/JS, no dependencies).
    
    charts: [
      {"title": "Revenue Growth", "type": "bar",
       "labels": ["Q1","Q2","Q3","Q4"],
       "values": [100, 150, 200, 280]},
      {"title": "Market Share", "type": "pie",
       "labels": ["Us","Competitor A","Others"],
       "values": [45, 35, 20]}
    ]
    """
    import html as html_mod
    
    body_parts = []
    
    # Title
    body_parts.append(f'<h1>{html_mod.escape(title or "Report")}</h1>')
    body_parts.append(f'<p class="meta">Generated: {time.strftime("%Y-%m-%d %H:%M")} · Phidipus Agent OS</p>')
    
    # Sections
    for sec in (sections or []):
        level = min(sec.get("level", 2), 4)
        heading = sec.get("heading", "")
        content = sec.get("content", "")
        bullets = sec.get("bullets", [])
        
        if heading:
            body_parts.append(f'<h{level}>{html_mod.escape(heading)}</h{level}>')
        if content:
            body_parts.append(f'<p>{html_mod.escape(content)}</p>')
        if bullets:
            body_parts.append('<ul>' + ''.join(f'<li>{html_mod.escape(b)}</li>' for b in bullets) + '</ul>')
    
    # Tables
    for tbl in (tables or []):
        if tbl.get("title"):
            body_parts.append(f'<h3>{html_mod.escape(tbl["title"])}</h3>')
        headers = tbl.get("headers", [])
        rows = tbl.get("rows", [])
        if headers:
            body_parts.append('<table><thead><tr>' + 
                ''.join(f'<th>{html_mod.escape(str(h))}</th>' for h in headers) +
                '</tr></thead><tbody>')
            for row in rows:
                body_parts.append('<tr>' + 
                    ''.join(f'<td>{html_mod.escape(str(v))}</td>' for v in row) +
                    '</tr>')
            body_parts.append('</tbody></table>')
    
    # Charts (CSS bar charts)
    for ci, chart in enumerate(charts or []):
        chart_title = chart.get("title", f"Chart {ci+1}")
        chart_type = chart.get("type", "bar")
        labels = chart.get("labels", [])
        values = chart.get("values", [])
        
        if not values:
            continue
        
        max_val = max(values) if values else 1
        colors = ["#22c55e", "#3b82f6", "#eab308", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#f97316"]
        
        body_parts.append(f'<div class="chart-container"><h3>{html_mod.escape(chart_title)}</h3>')
        
        if chart_type == "bar":
            body_parts.append('<div class="bar-chart">')
            for i, (label, val) in enumerate(zip(labels, values)):
                pct = (val / max_val * 100) if max_val else 0
                color = colors[i % len(colors)]
                body_parts.append(
                    f'<div class="bar-row">'
                    f'<span class="bar-label">{html_mod.escape(str(label))}</span>'
                    f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div>'
                    f'<span class="bar-value">{val:,}</span>'
                    f'</div>'
                )
            body_parts.append('</div>')
        
        elif chart_type == "pie":
            total = sum(values) or 1
            body_parts.append('<div class="pie-legend">')
            for i, (label, val) in enumerate(zip(labels, values)):
                pct = round(val / total * 100, 1)
                color = colors[i % len(colors)]
                body_parts.append(
                    f'<div class="pie-item">'
                    f'<span class="pie-dot" style="background:{color}"></span>'
                    f'{html_mod.escape(str(label))}: <b>{pct}%</b> ({val:,})'
                    f'</div>'
                )
            body_parts.append('</div>')
        
        elif chart_type == "line":
            body_parts.append('<div class="line-chart">')
            for i, (label, val) in enumerate(zip(labels, values)):
                pct = (val / max_val * 100) if max_val else 0
                body_parts.append(
                    f'<div class="line-point" style="bottom:{pct}%;left:{i/(max(len(labels)-1,1))*100}%">'
                    f'<span class="point-label">{val:,}</span></div>'
                )
            body_parts.append(
                '<div class="line-labels">' +
                ''.join(f'<span>{html_mod.escape(str(l))}</span>' for l in labels) +
                '</div></div>'
            )
        
        body_parts.append('</div>')
    
    html_content = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_mod.escape(title or "Report")}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0a0a;color:#e5e5e5;padding:24px;line-height:1.7}}
.container{{max-width:900px;margin:0 auto}}
h1{{font-size:26px;color:#22c55e;margin-bottom:4px}}
h2{{font-size:20px;color:#e5e5e5;margin:24px 0 10px;border-bottom:1px solid #262626;padding-bottom:6px}}
h3{{font-size:16px;color:#a3a3a3;margin:16px 0 8px}}
p{{margin-bottom:12px;font-size:15px}}
.meta{{font-size:12px;color:#525252;margin-bottom:24px}}
ul{{margin:8px 0 16px 20px}} li{{margin:4px 0}}
table{{width:100%;border-collapse:collapse;margin:12px 0 20px;font-size:14px}}
th{{background:#1a1a1a;color:#22c55e;text-align:left;padding:10px 14px;border:1px solid #262626;font-weight:600}}
td{{padding:8px 14px;border:1px solid #1a1a1a;color:#d4d4d4}}
tr:nth-child(even){{background:rgba(255,255,255,.02)}}
tr:hover td{{background:rgba(34,197,94,.04)}}
.chart-container{{background:#111;border:1px solid #262626;border-radius:8px;padding:16px;margin:16px 0}}
.bar-chart{{display:flex;flex-direction:column;gap:8px}}
.bar-row{{display:flex;align-items:center;gap:10px}}
.bar-label{{width:100px;font-size:13px;text-align:right;color:#a3a3a3;flex-shrink:0}}
.bar-track{{flex:1;height:24px;background:#1a1a1a;border-radius:4px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px;transition:width .5s}}
.bar-value{{width:70px;font-size:13px;font-weight:600;color:#22c55e}}
.pie-legend{{display:flex;flex-wrap:wrap;gap:12px}}
.pie-item{{display:flex;align-items:center;gap:6px;font-size:14px}}
.pie-dot{{width:12px;height:12px;border-radius:50%;flex-shrink:0}}
.line-chart{{position:relative;height:200px;border-left:1px solid #333;border-bottom:1px solid #333;margin:10px 0}}
.line-point{{position:absolute;width:10px;height:10px;background:#22c55e;border-radius:50%;transform:translate(-50%,50%)}}
.point-label{{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);font-size:10px;color:#a3a3a3;white-space:nowrap}}
.line-labels{{display:flex;justify-content:space-between;margin-top:8px;font-size:11px;color:#525252}}
.footer{{margin-top:30px;text-align:center;font-size:11px;color:#404040;border-top:1px solid #1a1a1a;padding-top:12px}}
</style></head><body><div class="container">
{"".join(body_parts)}
<div class="footer">Phidipus v1.0 · Generated {time.strftime("%Y-%m-%d %H:%M")}</div>
</div></body></html>'''
    
    filepath = _REPORTS_DIR / filename
    filepath.write_text(html_content, encoding="utf-8")
    _vlog("📄", f"HTML report: {filepath}")
    return str(filepath)


# ══════════════════════════════════════════════════════════════
# AI-powered document structuring
# ══════════════════════════════════════════════════════════════

async def ai_structure_data(raw_text: str, doc_type: str = "report", model: str = "auto") -> dict:
    """
    Use AI to structure raw text into document-ready format.
    
    Returns: {
      "title": "...",
      "sections": [...],
      "tables": [...],
      "charts": [...],
    }
    """
    prompt = f"""You are a document structuring AI. Convert the following raw data/text into a structured JSON format.

Document type: {doc_type}

Raw input:
{raw_text[:3000]}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "title": "document title",
  "sections": [
    {{"heading": "Section Title", "level": 1, "content": "paragraph text", "bullets": ["point 1", "point 2"]}}
  ],
  "tables": [
    {{"title": "Table Title", "headers": ["Col1", "Col2", "Col3"], "rows": [["val1", "val2", "val3"]]}}
  ],
  "charts": [
    {{"title": "Chart Title", "type": "bar", "labels": ["A", "B", "C"], "values": [100, 200, 150]}}
  ]
}}

Rules:
- Extract any tabular data into tables
- If numerical data exists, create appropriate charts (bar for comparison, line for trends, pie for proportions)
- Sections should have clear headings and structured content
- Keep language same as input
"""
    
    try:
        import urllib.request
        import asyncio
        
        # Try Ollama first
        payload = json.dumps({
            "model": _resolve_model("qwen3:8b" if model == "auto" else model, "reasoning"),
            "think": False,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 2000},
        }).encode()
        
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        
        def _call():
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
                return data.get("response", "")
        
        response = await asyncio.get_event_loop().run_in_executor(None, _call)
        
        # Parse JSON from response
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            result = json.loads(json_match.group())
            return result
    except Exception as exc:
        _vlog("⚠️", f"AI structuring failed: {str(exc)[:60]}")
    
    # Fallback: basic structure
    return {
        "title": "Report",
        "sections": [{"heading": "Content", "level": 1, "content": raw_text[:2000]}],
        "tables": [],
        "charts": [],
    }


# ══════════════════════════════════════════════════════════════
# Fallback: HTML when python-docx/openpyxl unavailable
# ══════════════════════════════════════════════════════════════

def _create_xlsx_fallback_html(filename: str, sheets: list[dict]) -> str:
    """Fallback: generate HTML tables when openpyxl unavailable."""
    return create_html_report(
        filename.replace(".xlsx", ".html"),
        title="Spreadsheet Report",
        tables=[{"title": s.get("name",""), "headers": s.get("headers",[]), "rows": s.get("rows",[])} for s in sheets],
    )

def _create_docx_fallback_html(filename: str, title: str, sections, tables) -> str:
    """Fallback: generate HTML when python-docx unavailable."""
    return create_html_report(
        filename.replace(".docx", ".html"),
        title=title,
        sections=sections,
        tables=tables,
    )


def _smart_value(val):
    """Convert string numbers to actual numbers for Excel."""
    if isinstance(val, str):
        # Remove currency symbols
        clean = re.sub(r'[$€£¥₫,\s]', '', val)
        try:
            if '.' in clean:
                return float(clean)
            return int(clean)
        except ValueError:
            pass
    return val
