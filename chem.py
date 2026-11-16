"""ChemASAP feed bridge.

ChemASAP (https://api.baranlab.org) is a live list of ASAP articles from chemistry
journals. Its page is a JS app; the underlying JSON endpoints are /api/journals and
/api/feed, which this module proxies and trims.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

BASE = "https://api.baranlab.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DOIFinder/2.0"

_cache = {"at": 0.0, "feed": None, "journals": None}
TTL = 600


def _get(path: str, timeout: int = 45):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def journals() -> list[dict]:
    if _cache["journals"] and time.time() - _cache["at"] < TTL:
        return _cache["journals"]
    data = _get("/api/journals")
    out = [{"id": j.get("id"), "code": j.get("code"), "name": j.get("name"),
            "abbr": j.get("abbr"), "publisher": j.get("publisher"),
            "defaultOn": bool(j.get("defaultOn")), "discipline": j.get("discipline"),
            "group": j.get("group"), "color": j.get("color")}
           for j in data if j.get("id")]
    _cache["journals"] = out
    return out


def feed(query: str = "", journal_ids: list[str] | None = None, limit: int = 200,
         only_oa: bool = False) -> dict:
    if not _cache["feed"] or time.time() - _cache["at"] > TTL:
        data = _get("/api/feed")
        arts = []
        for a in data.get("articles", []):
            arts.append({
                "id": a.get("id"),
                "title": a.get("title") or "",
                "authors": a.get("authors") or "",
                "doi": (a.get("doi") or "").strip(),
                "journalId": a.get("journalId") or "",
                "journal": a.get("journalName") or a.get("journalAbbr") or "",
                "abbr": a.get("journalAbbr") or "",
                "publishedAt": (a.get("publishedAt") or "")[:10],
                "link": a.get("link") or "",
                "oa": bool(a.get("oa")),
                "abstract": (a.get("abstract") or "")[:600],
            })
        _cache["feed"] = {"updatedAt": data.get("updatedAt", ""), "articles": arts}
        _cache["at"] = time.time()

    arts = _cache["feed"]["articles"]
    if journal_ids:
        wanted = set(journal_ids)
        arts = [a for a in arts if a["journalId"] in wanted]
    if only_oa:
        arts = [a for a in arts if a["oa"]]
    if query:
        q = query.lower()
        arts = [a for a in arts
                if q in a["title"].lower() or q in a["authors"].lower() or q in a["journal"].lower()]
    return {"updatedAt": _cache["feed"]["updatedAt"], "total": len(arts), "articles": arts[:limit]}
