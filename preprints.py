"""Find the ChemRxiv preprint of a paper.

A preprint and its journal version often carry a different title and always a different DOI, so
title search alone is not enough.  Evidence, strongest first:

  1. the journal record itself lists the preprint (Crossref ``relation.has-preprint``), or the
     preprint lists its published version (``is-preprint-of`` / ChemRxiv ``vor``)  -> "exact"
  2. title similarity, confirmed by first author and year                           -> "likely"
  3. a similar title or a matching author without enough other evidence             -> "possible"

Three independent sources are asked (ChemRxiv's own search API, Crossref restricted to the ChemRxiv
DOI prefix, and the relation links); one of them failing never hides what the others found.
Only "exact" and "likely" are ever used automatically; "possible" is shown to the user to decide.

Every network call goes through an object with ``get_json(url)`` (doi_engine.Http), so tests fake it.
"""
from __future__ import annotations

import re
import urllib.parse

import doi_engine as de

PREFIX = "10.26434"                 # ChemRxiv
ENGAGE = "https://chemrxiv.org/engage/chemrxiv/public-api/v1/"
CROSSREF = "https://api.crossref.org/works"
LEVELS = ("possible", "likely", "exact")
AUTO_LEVELS = ("likely", "exact")
_PDF_RE = re.compile(r"https?://[^\s\"']*chemrxiv[^\s\"']*\.pdf", re.I)


def norm_doi(v) -> str:
    s = str(v or "").strip().lower()
    s = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", s)
    s = re.sub(r"^doi:\s*", "", s)
    return s.strip().strip(".,;")


def is_chemrxiv(doi: str) -> bool:
    return norm_doi(doi).startswith(PREFIX + "/")


# ----------------------------------------------------------------------------- parsing
def _year(v) -> int | None:
    if isinstance(v, dict):                       # Crossref {"date-parts": [[2021, 3, 1]]}
        parts = v.get("date-parts") or [[None]]
        try:
            return de.parse_year(parts[0][0])
        except (IndexError, TypeError):
            return None
    return de.parse_year(v)


def _walk_pdf(obj, depth: int = 0) -> str:
    """First ChemRxiv .pdf URL anywhere in an Engage item (the key it sits under has changed
    over time, the URL shape has not)."""
    if depth > 6:
        return ""
    if isinstance(obj, str):
        m = _PDF_RE.search(obj)
        return m.group(0) if m else ""
    if isinstance(obj, dict):
        for v in obj.values():
            r = _walk_pdf(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_pdf(v, depth + 1)
            if r:
                return r
    return ""


def _engage_authors(item: dict) -> list[str]:
    out = []
    for a in item.get("authors") or []:
        if isinstance(a, str):
            name = a
        elif isinstance(a, dict):
            name = " ".join(x for x in (a.get("firstName") or a.get("given"), a.get("lastName") or a.get("family"))
                            if x) or str(a.get("name") or "")
        else:
            continue
        if name.strip():
            out.append(name.strip())
    return out


def from_engage(hit: dict) -> dict | None:
    item = hit.get("item") if isinstance(hit, dict) and isinstance(hit.get("item"), dict) else hit
    if not isinstance(item, dict):
        return None
    doi = norm_doi(item.get("doi"))
    title = " ".join(str(item.get("title") or "").split())
    if not doi or not title:
        return None
    vor = item.get("vor") if isinstance(item.get("vor"), dict) else {}
    return {"doi": doi, "title": title, "authors": _engage_authors(item),
            "year": _year(item.get("publishedDate") or item.get("statusDate")),
            "pdf_url": _walk_pdf(item), "source": "ChemRxiv",
            "published_doi": norm_doi(vor.get("vorDoi")), "relations": set()}


def from_crossref(it: dict) -> dict | None:
    doi = norm_doi(it.get("DOI"))
    title = " ".join(str((it.get("title") or [""])[0] or "").split())
    if not doi or not title:
        return None
    authors = []
    for a in it.get("author") or []:
        n = " ".join(x for x in (a.get("given"), a.get("family")) if x) or str(a.get("name") or "")
        if n.strip():
            authors.append(n.strip())
    rel = it.get("relation") or {}
    pub = [norm_doi(r.get("id")) for r in rel.get("is-preprint-of") or []
           if isinstance(r, dict) and r.get("id-type", "doi") == "doi"]
    return {"doi": doi, "title": title, "authors": authors,
            "year": _year(it.get("issued") or it.get("created")),
            "pdf_url": "", "source": "Crossref", "published_doi": pub[0] if pub else "",
            "relations": set()}


# ----------------------------------------------------------------------------- sources
def engage_search(http, query: str, limit: int = 10) -> list[dict]:
    q = " ".join((query or "").split())[:300]
    if not q:
        return []
    url = ENGAGE + "items?" + urllib.parse.urlencode({"term": q, "limit": limit})
    data = http.get_json(url, 2)
    hits = data.get("itemHits") if isinstance(data, dict) else None
    out = []
    for h in hits or []:
        c = from_engage(h)
        if c:
            out.append(c)
    return out


def crossref_search(http, query: str, rows: int = 10) -> list[dict]:
    q = " ".join((query or "").split())[:300]
    if not q:
        return []
    params = {"filter": "prefix:" + PREFIX, "query.title": q, "rows": rows,
              "select": "DOI,title,author,issued,created,relation"}
    if getattr(http, "mailto", ""):
        params["mailto"] = http.mailto
    data = http.get_json(CROSSREF + "?" + urllib.parse.urlencode(params))
    out = []
    for it in ((data or {}).get("message") or {}).get("items") or []:
        c = from_crossref(it)
        if c and is_chemrxiv(c["doi"]):
            out.append(c)
    return out


def crossref_work(http, doi: str) -> dict | None:
    data = http.get_json(CROSSREF + "/" + urllib.parse.quote(norm_doi(doi), safe="/()"))
    return (data or {}).get("message") or None


def linked_preprints(work: dict) -> list[str]:
    """ChemRxiv DOIs a journal record declares as its preprint."""
    rel = (work or {}).get("relation") or {}
    out = []
    for r in rel.get("has-preprint") or []:
        if isinstance(r, dict) and r.get("id-type", "doi") == "doi" and is_chemrxiv(r.get("id")):
            out.append(norm_doi(r["id"]))
    return out


# ----------------------------------------------------------------------------- grading
def _grade(c: dict, title: str, first_author: str, authors: list[str], year, exact: set[str]) -> None:
    """Fill c["level"], c["score"], c["why"] (a sentence the user can read)."""
    sim = de.similarity(title, c["title"]) if title else 0.0
    c["score"] = round(sim, 3)
    if c["doi"] in exact:
        c["level"], c["why"] = "exact", "期刊记录登记了这篇预印本（DOI 关联，不看标题）"
        return
    if c.get("published_doi") and c["published_doi"] == norm_doi(c.get("_for", "")):
        c["level"], c["why"] = "exact", "预印本登记的正式版 DOI 就是这篇"
        return

    ev = de.field_evidence(c, de.norm_expect({"author": first_author, "year": year}))
    y, a = ev.get("year", "unknown"), ev.get("author", "unknown")
    # a preprint comes out before the paper: up to ~3 years earlier, at most a year later
    py, cy = de.parse_year(year), c.get("year")
    if py and cy:
        y = "match" if 0 <= py - cy <= 3 else "near" if cy - py == 1 else "mismatch"
    # whole author list overlap (any 2 shared surnames) is as good as a first-author match
    shared = 0
    for name in authors or []:
        k = de._surname_key(name)
        if k and any(de._has_name(k, x) for x in c.get("authors") or []):
            shared += 1
    if shared >= 2 and a != "match":
        a = "match"
    notes = []
    if a == "match":
        notes.append("作者吻合")
    if y == "match":
        notes.append("年份吻合")
    if a == "mismatch":
        notes.append("第一作者对不上")
    if y == "mismatch":
        notes.append("年份对不上")

    strong = a == "match" and y in ("match", "near")
    if sim >= 0.88 and a != "mismatch" and y != "mismatch":
        level = "likely"
    elif sim >= 0.55 and strong:
        level = "likely"              # the title was reworded; author + year carry it
    elif sim >= 0.55 or (a == "match" and sim >= 0.40):
        level = "possible"
    else:
        level = ""
    c["level"] = level
    c["why"] = ("标题相似度 %d%%" % round(sim * 100)) + ("，" + "，".join(notes) if notes else "")


def find(title: str, doi: str = "", first_author: str = "", authors: list[str] | None = None,
         year=None, http=None, limit: int = 8) -> dict:
    """{"candidates": [...best first...], "notes": [...], "error": "" | message}.

    ``error`` is set only when every source failed, so "nothing found" and "could not ask" are
    never confused (the first is an answer, the second deserves a retry)."""
    http = http or de.Http()
    title = " ".join((title or "").split())
    doi = norm_doi(doi)
    notes: list[str] = []
    asked = failed = 0
    cands: dict[str, dict] = {}
    exact: set[str] = set()

    def add(c: dict | None) -> None:
        if not c or c["doi"] == doi:
            return
        old = cands.get(c["doi"])
        if not old:
            cands[c["doi"]] = c
            return
        for k, v in c.items():                       # merge: keep whatever either source knows
            if k == "relations":
                continue
            if v and not old.get(k):
                old[k] = v

    # 1. relation links from the journal record (needs its DOI)
    if doi and not is_chemrxiv(doi):
        asked += 1
        try:
            work = crossref_work(http, doi)
            for d in linked_preprints(work):
                exact.add(d)
            if work and not title:
                title = " ".join(str((work.get("title") or [""])[0]).split())
        except Exception as e:  # noqa: BLE001
            failed += 1
            notes.append("Crossref 关联记录没取到：" + str(e))
    # 2. ChemRxiv's own search
    if title:
        asked += 1
        try:
            for c in engage_search(http, title, limit):
                add(c)
        except Exception as e:  # noqa: BLE001
            failed += 1
            notes.append("ChemRxiv 官方接口没连上（" + str(e) + "；常见原因是被 Cloudflare 拦截），已改用 Crossref")
        # 3. Crossref restricted to ChemRxiv
        asked += 1
        try:
            for c in crossref_search(http, title, limit):
                add(c)
        except Exception as e:  # noqa: BLE001
            failed += 1
            notes.append("Crossref 检索没连上：" + str(e))
    # declared preprints must appear even if no search surfaced them
    for d in sorted(exact):
        if d not in cands:
            try:
                c = from_crossref(crossref_work(http, d) or {})
                add(c)
            except Exception as e:  # noqa: BLE001
                notes.append(f"预印本 {d} 的详情没取到：{e}")
                cands.setdefault(d, {"doi": d, "title": "", "authors": [], "year": None, "pdf_url": "",
                                     "source": "Crossref", "published_doi": doi, "relations": set()})

    for c in cands.values():
        c["_for"] = doi
        _grade(c, title, first_author, authors or [], year, exact)
        c.pop("_for", None)
        c.pop("relations", None)
        c["url"] = "https://doi.org/" + c["doi"]
    ranked = [c for c in cands.values() if c.get("level")]
    ranked.sort(key=lambda c: (-LEVELS.index(c["level"]), -c["score"]))
    err = ""
    if asked and failed >= asked:
        err = "；".join(notes) or "所有来源都没连上"
    return {"candidates": ranked[:limit], "notes": notes, "error": err}


def best_auto(found: dict) -> dict | None:
    """The one candidate that may be used without asking: exact, else the single best 'likely'."""
    cs = [c for c in found.get("candidates", []) if c.get("level") in AUTO_LEVELS]
    if not cs:
        return None
    top = cs[0]
    # two different "likely" preprints for one paper: do not guess between them
    rivals = [c for c in cs[1:] if c["level"] == top["level"] and top["level"] != "exact"
              and c["score"] >= top["score"] - 0.03]
    return None if rivals else top
