"""粘贴 BibTeX / RIS 也能直接解析。

很多人手上是 EndNote、Zotero 导出的 .bib 或 .ris，或者从期刊页面复制的引用块。
这类文本里有 DOI 就直接用，没有 DOI 就只取标题去查，省掉一次全文匹配。
"""
from __future__ import annotations

import re


def looks_bibtex(text: str) -> bool:
    return bool(re.search(r"@\w+\s*\{", text or ""))


def looks_ris(text: str) -> bool:
    return bool(re.search(r"(?m)^TY\s{1,2}-", text or ""))


def _clean(v: str) -> str:
    v = re.sub(r"[{}]", "", v or "")
    return re.sub(r"\s+", " ", v).strip().strip(",")


def parse_bibtex(text: str) -> list[dict]:
    out = []
    # 用花括号配对切块：单行、多行、嵌套大括号都能处理
    blocks = []
    for m in re.finditer(r"@(\w+)\s*\{", text or ""):
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        blocks.append(text[start:i - 1])
    for body in blocks:
        fields: dict[str, str] = {}
        for fm in re.finditer(r"(\w+)\s*=\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}|\"[^\"]*\"|[^,\n]+)", body):
            fields[fm.group(1).lower()] = _clean(fm.group(2))
        doi = fields.get("doi", "")
        title = fields.get("title", "")
        if not title and not doi:
            continue
        out.append({"title": title, "doi": doi, "year": fields.get("year", ""),
                    "journal": fields.get("journal", "") or fields.get("booktitle", "")})
    return out


def parse_ris(text: str) -> list[dict]:
    out, cur = [], {}
    for line in (text or "").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$", line.strip())
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "TY":
            cur = {}
        elif tag == "ER":
            if cur.get("title") or cur.get("doi"):
                out.append(cur)
            cur = {}
        elif tag == "TI" or tag == "T1":
            cur["title"] = val
        elif tag == "DO":
            cur["doi"] = val
        elif tag == "PY" or tag == "Y1":
            cur.setdefault("year", val[:4])
        elif tag == "JO" or tag == "JF" or tag == "T2":
            cur.setdefault("journal", val)
    if cur.get("title") or cur.get("doi"):
        out.append(cur)
    return out


def parse_structured(text: str) -> list[dict]:
    """返回结构化条目；不是 BibTeX/RIS 就返回空列表，交回原来的解析器。"""
    if looks_bibtex(text):
        return parse_bibtex(text)
    if looks_ris(text):
        return parse_ris(text)
    return []
