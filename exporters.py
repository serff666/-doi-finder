"""Export lookup results to XLSX / Markdown / HTML / CSV / plain DOI list."""
from __future__ import annotations

import csv
import html as htmllib
import io
from datetime import datetime

STATUS_LABEL = {
    "matched": "matched",
    "likely": "likely",
    "uncertain": "check",
    "provided": "supplied",
    "not_found": "not found",
    "error": "error",
}

STATUS_COLOR = {
    "matched": "22A06B",
    "likely": "2F6FEB",
    "uncertain": "B7791F",
    "provided": "6B46C1",
    "not_found": "C53030",
    "error": "718096",
}


def _rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: r.get("n", 0))


def summary(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status", "error")] = counts.get(r.get("status", "error"), 0) + 1
    parts = [f"{len(rows)} entries"]
    for k in ("matched", "likely", "uncertain", "provided", "not_found", "error"):
        if counts.get(k):
            parts.append(f"{counts[k]} {STATUS_LABEL[k]}")
    return " · ".join(parts)


def link(doi: str) -> str:
    return f"https://doi.org/{doi}" if doi else ""


# ----------------------------------------------------------------------------- markdown
def to_markdown(rows: list[dict]) -> str:
    rows = _rows(rows)
    out = ["# DOI lookup result", "", f"_{summary(rows)} — generated {datetime.now():%Y-%m-%d %H:%M}_", ""]
    out.append("| # | Title | DOI | Status | Journal | Year |")
    out.append("|---:|---|---|---|---|---:|")
    for r in rows:
        title = (r.get("title") or "").replace("|", "\\|")
        journal = (r.get("journal") or "").replace("|", "\\|")
        doi = r.get("doi") or ""
        doi_cell = f"[{doi}](https://doi.org/{doi})" if doi else "—"
        out.append(
            f"| {r.get('n','')} | {title} | {doi_cell} | {STATUS_LABEL.get(r.get('status',''), r.get('status',''))} "
            f"| {journal} | {r.get('year') or ''} |"
        )
    out.append("")
    out.append("## DOI list")
    out.append("")
    out.append("```")
    out.extend([r.get("doi") or "N/A" for r in rows])
    out.append("```")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------- plain text
def to_txt(rows: list[dict]) -> str:
    return "\n".join([(r.get("doi") or "N/A") for r in _rows(rows)]) + "\n"


def _safe(v):
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a text cell)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


# ----------------------------------------------------------------------------- csv
def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "Title", "DOI", "Status", "Score", "Journal", "Year", "Volume", "Page", "URL", "Note"])
    for r in _rows(rows):
        w.writerow([
            r.get("n", ""), _safe(r.get("title", "")), r.get("doi", ""),
            STATUS_LABEL.get(r.get("status", ""), r.get("status", "")),
            r.get("score", ""), _safe(r.get("journal", "")), r.get("year", ""),
            r.get("volume", ""), r.get("page", ""),
            link(r.get("doi", "")), _safe(r.get("note", "")),
        ])
    return buf.getvalue()


# ----------------------------------------------------------------------------- html
def to_html(rows: list[dict], title: str = "DOI lookup result") -> str:
    rows = _rows(rows)
    body_rows = []
    for r in rows:
        st = r.get("status", "error")
        color = STATUS_COLOR.get(st, "718096")
        doi = r.get("doi") or ""
        doi_cell = (f'<a href="https://doi.org/{htmllib.escape(doi)}" target="_blank" rel="noopener">'
                    f'{htmllib.escape(doi)}</a>') if doi else '<span class="muted">—</span>'
        score = r.get("score")
        score_txt = f"{score:.3f}" if isinstance(score, (int, float)) and score else ""
        body_rows.append(
            "<tr>"
            f'<td class="num">{r.get("n","")}</td>'
            f'<td class="title">{htmllib.escape(r.get("title",""))}'
            f'<div class="jt">{htmllib.escape(r.get("journal") or "")}'
            f'{" " + str(r.get("year")) if r.get("year") else ""}</div></td>'
            f"<td>{doi_cell}</td>"
            f'<td><span class="chip" style="--c:#{color}">{STATUS_LABEL.get(st, st)}</span></td>'
            f'<td class="num">{score_txt}</td>'
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{htmllib.escape(title)}</title>
<style>
:root{{--ink:#1a202c;--muted:#718096;--line:#e2e8f0;--bg:#fff}}
*{{box-sizing:border-box}}
body{{margin:0;padding:40px;font:15px/1.55 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans SC",sans-serif;
color:var(--ink);background:var(--bg);max-width:1100px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 6px}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;
letter-spacing:.04em;border-bottom:2px solid var(--line);padding:8px 10px}}
td{{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;color:var(--muted);width:56px}}
td.title{{max-width:520px}}
.jt{{color:var(--muted);font-size:12px;margin-top:3px}}
a{{color:#2f6feb;text-decoration:none}} a:hover{{text-decoration:underline}}
.chip{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:600;
color:var(--c);background:color-mix(in srgb,var(--c) 12%,transparent);white-space:nowrap}}
.muted{{color:var(--muted)}}
tr:hover td{{background:#f7fafc}}
</style></head>
<body>
<h1>{htmllib.escape(title)}</h1>
<div class="sub">{htmllib.escape(summary(rows))} · generated {datetime.now():%Y-%m-%d %H:%M}</div>
<table><thead><tr><th>#</th><th>Title</th><th>DOI</th><th>Status</th><th>Score</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody></table>
</body></html>
"""


# ----------------------------------------------------------------------------- xlsx
def to_xlsx(rows: list[dict], title: str = "DOIs") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = _rows(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "DOIs"

    headers = ["#", "Title", "DOI", "Status", "Score", "Journal", "Year", "Volume", "Page", "Link", "Note"]
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="1F2937")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 22

    for r in rows:
        doi = r.get("doi") or ""
        ws.append([r.get("n", ""), _safe(r.get("title", "")), doi,
                   STATUS_LABEL.get(r.get("status", ""), r.get("status", "")),
                   r.get("score", ""), _safe(r.get("journal", "")), r.get("year", ""),
                   r.get("volume", ""), r.get("page", ""), link(doi), _safe(r.get("note", ""))])
        row = ws.max_row
        if doi:
            c = ws.cell(row=row, column=3)
            c.hyperlink = f"https://doi.org/{doi}"
            c.font = Font(color="2F6FEB", underline="single", size=10)
        ws.cell(row=row, column=10).hyperlink = f"https://doi.org/{doi}" if doi else None
        st = r.get("status", "error")
        ws.cell(row=row, column=4).font = Font(bold=True, color=STATUS_COLOR.get(st, "718096"), size=10)
        for col in range(1, len(headers) + 1):
            ws.cell(row=row, column=col).alignment = Alignment(vertical="top", wrap_text=col == 2)

    widths = {"A": 5, "B": 78, "C": 34, "D": 11, "E": 7, "F": 30, "G": 7, "H": 8, "I": 10, "J": 40, "K": 26}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"

    summary_ws = wb.create_sheet("Summary")
    summary_ws.append(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M")])
    summary_ws.append(["Entries", len(rows)])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status", "error")] = counts.get(r.get("status", "error"), 0) + 1
    summary_ws.append([])
    summary_ws.append(["Status", "Count"])
    for k, v in counts.items():
        summary_ws.append([STATUS_LABEL.get(k, k), v])
    summary_ws.column_dimensions["A"].width = 18
    summary_ws.column_dimensions["B"].width = 26

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


EXPORTERS = {
    "xlsx": (to_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "md": (to_markdown, "text/markdown; charset=utf-8", "md"),
    "html": (to_html, "text/html; charset=utf-8", "html"),
    "csv": (to_csv, "text/csv; charset=utf-8", "csv"),
    "txt": (to_txt, "text/plain; charset=utf-8", "txt"),
}
