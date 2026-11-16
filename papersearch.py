"""多源文献检索：把 PaperSearch 的检索能力用纯 stdlib 重写一遍。

选这三个源先做，因为它们不需要 key、覆盖最广：
  · OpenAlex    开放学术图谱，约 2.5 亿条，带引用数与被引关系
  · arXiv       预印本，物理化学材料计算机都在这里
  · Semantic Scholar  带摘要与引用上下文

返回统一结构，字段固定，前端和后面的 DOI 解析、下载、翻译都能直接吃。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "DOIFinder/1.0 (local desktop app)"


def _get(url: str, timeout: int = 25, attempts: int = 3) -> bytes:
    """带退避的 GET。OpenAlex 和 Semantic Scholar 对共享 IP 限流很凶，必须重试。"""
    import netpolicy
    url = netpolicy.prepare(url)
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Accept": "application/json, application/atom+xml, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last if last else RuntimeError("请求失败")


def _record(source: str, title: str, doi: str = "", year: str = "", authors: str = "",
            venue: str = "", cited: int = 0, url: str = "", abstract: str = "",
            oa_pdf: str = "") -> dict:
    return {"source": source, "title": (title or "").strip(), "doi": (doi or "").lower(),
            "year": str(year or ""), "authors": authors, "venue": venue,
            "cited": int(cited or 0), "url": url, "abstract": abstract, "oa_pdf": oa_pdf}


# ---------------------------------------------------------------- OpenAlex
def search_openalex(query: str, rows: int = 20) -> list[dict]:
    url = ("https://api.openalex.org/works?search=" + urllib.parse.quote(query)
           + f"&per-page={min(rows, 50)}")
    data = json.loads(_get(url))
    out = []
    for w in data.get("results", []):
        loc = (w.get("best_oa_location") or {})
        out.append(_record(
            "openalex", w.get("title") or "", (w.get("doi") or "").replace("https://doi.org/", ""),
            w.get("publication_year") or "",
            ", ".join(a["author"]["display_name"] for a in (w.get("authorships") or [])[:6]),
            ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
            w.get("cited_by_count") or 0, w.get("id") or "", "",
            loc.get("pdf_url") or ""))
    return out


# ---------------------------------------------------------------- arXiv
_ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}


def search_arxiv(query: str, rows: int = 20) -> list[dict]:
    url = ("http://export.arxiv.org/api/query?search_query=all:"
           + urllib.parse.quote(query) + f"&start=0&max_results={min(rows, 50)}")
    root = ET.fromstring(_get(url))
    out = []
    for e in root.findall("a:entry", _ARXIV_NS):
        title = " ".join((e.findtext("a:title", "", _ARXIV_NS) or "").split())
        summ = " ".join((e.findtext("a:summary", "", _ARXIV_NS) or "").split())
        pub = e.findtext("a:published", "", _ARXIV_NS) or ""
        link = e.findtext("a:id", "", _ARXIV_NS) or ""
        authors = ", ".join(a.findtext("a:name", "", _ARXIV_NS) or ""
                            for a in e.findall("a:author", _ARXIV_NS))
        out.append(_record("arxiv", title, "", pub[:4], authors, "arXiv", 0, link, summ, ""))
    return out


# ---------------------------------------------------------------- Semantic Scholar
def search_s2(query: str, rows: int = 20, pause: float = 1.0) -> list[dict]:
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?query="
           + urllib.parse.quote(query)
           + f"&limit={min(rows, 100)}&fields=title,year,authors,venue,citationCount,"
             "externalIds,abstract,openAccessPdf,url")
    for attempt in range(3):
        try:
            data = json.loads(_get(url))
            break
        except Exception:  # noqa: BLE001 - 匿名额度很紧，退避重试
            time.sleep(pause * (attempt + 1))
    else:
        return []
    out = []
    for p in data.get("data", []):
        ids = p.get("externalIds") or {}
        pdf = (p.get("openAccessPdf") or {}).get("url") or ""
        out.append(_record(
            "s2", p.get("title") or "", ids.get("DOI") or "", p.get("year") or "",
            ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:6]),
            p.get("venue") or "", p.get("citationCount") or 0,
            p.get("url") or "", (p.get("abstract") or "")[:600], pdf))
    return out


# ---------------------------------------------------------------- 合并
def _key(r: dict) -> str:
    if r["doi"]:
        return "doi:" + r["doi"]
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (r["title"] or "").lower())
    return "t:" + t[:80]


def search_all(query: str, rows: int = 15, sources: tuple[str, ...] = ("openalex", "arxiv", "s2")) -> list[dict]:
    """三源并查，按 DOI 或标题去重，保留被引数最高的那条，来源合并成列表。"""
    merged: dict[str, dict] = {}
    for name in sources:
        try:
            got = {"openalex": search_openalex, "arxiv": search_arxiv,
                   "s2": search_s2}[name](query, rows)
        except Exception:  # noqa: BLE001 - 单源失败不影响整体
            got = []
        for r in got:
            k = _key(r)
            old = merged.get(k)
            if old is None:
                r["sources"] = [r["source"]]
                merged[k] = r
            else:
                old["sources"].append(r["source"])
                for f in ("doi", "abstract", "oa_pdf", "venue", "url"):
                    if not old.get(f) and r.get(f):
                        old[f] = r[f]
                old["cited"] = max(old["cited"], r["cited"])
    return sorted(merged.values(), key=lambda x: (-x["cited"], x["year"]), reverse=False)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "quantum dynamics path integral"
    for i, r in enumerate(search_all(q, 8), 1):
        print(f"{i}. [{r['year']}] {r['title'][:80]}")
        print(f"   {r['venue'][:40]} | 被引 {r['cited']} | {','.join(r['sources'])} | {r['doi'] or r['url']}")


# ---------------------------------------------------------------- PubMed
def search_pubmed(query: str, rows: int = 20) -> list[dict]:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    ids_url = (base + "esearch.fcgi?db=pubmed&retmode=json&retmax=" + str(min(rows, 50))
               + "&term=" + urllib.parse.quote(query))
    ids = (json.loads(_get(ids_url)).get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    time.sleep(0.35)  # eutils 限速：每秒三次
    sum_url = (base + "esummary.fcgi?db=pubmed&retmode=json&id=" + ",".join(ids))
    data = json.loads(_get(sum_url)).get("result") or {}
    out = []
    for pid in ids:
        d = data.get(pid) or {}
        doi = ""
        for a in d.get("articleids") or []:
            if a.get("idtype") == "doi":
                doi = a.get("value") or ""
        out.append(_record("pubmed", d.get("title") or "", doi,
                           (d.get("pubdate") or "")[:4],
                           ", ".join(x.get("name", "") for x in (d.get("authors") or [])[:6]),
                           d.get("fulljournalname") or d.get("source") or "",
                           0, f"https://pubmed.ncbi.nlm.nih.gov/{pid}/", "", ""))
    return out


# ---------------------------------------------------------------- Europe PMC
def search_europepmc(query: str, rows: int = 20) -> list[dict]:
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?format=json&pageSize="
           + str(min(rows, 100)) + "&query=" + urllib.parse.quote(query))
    data = json.loads(_get(url))
    out = []
    for r in ((data.get("resultList") or {}).get("result") or []):
        out.append(_record("europepmc", r.get("title") or "", r.get("doi") or "",
                           r.get("pubYear") or "", r.get("authorString") or "",
                           r.get("journalTitle") or "", r.get("citedByCount") or 0,
                           f"https://europepmc.org/article/{r.get('source','MED')}/{r.get('id','')}",
                           (r.get("abstractText") or "")[:600],
                           "" if not r.get("isOpenAccess") == "Y" else
                           f"https://europepmc.org/articles/{r.get('pmcid','')}?pdf=render"))
    return out


# ---------------------------------------------------------------- DBLP
def search_dblp(query: str, rows: int = 20) -> list[dict]:
    url = ("https://dblp.org/search/publ/api?format=json&h=" + str(min(rows, 100))
           + "&q=" + urllib.parse.quote(query))
    try:
        data = json.loads(_get(url))
    except Exception:  # noqa: BLE001 - dblp 限流时返回 HTML 而不是 JSON
        return []
    hits = ((data.get("result") or {}).get("hits") or {}).get("hit") or []
    out = []
    for h in hits:
        i = h.get("info") or {}
        au = i.get("authors") or {}
        names = au.get("author") if isinstance(au, dict) else []
        if isinstance(names, dict):
            names = [names]
        out.append(_record("dblp", i.get("title") or "", i.get("doi") or "",
                           i.get("year") or "",
                           ", ".join(n.get("text", "") for n in (names or [])[:6]),
                           i.get("venue") or "", 0, i.get("url") or "",
                           i.get("ee") or "", ""))
    return out
