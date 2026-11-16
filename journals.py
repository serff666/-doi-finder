"""Journal issue monitoring: "what did this journal publish recently?"

The key design choice: new-article discovery goes through Crossref, not through
each publisher's own site. One interface covers ACS, Elsevier, Wiley, Springer,
Nature, RSC and the rest, with no API key, no browser and no Cloudflare. The
publisher-specific part only matters later, when a PDF has to be fetched — and
that is already handled by downloader.py.

Presets are stored as journal *names* and resolved to ISSNs through Crossref the
first time they are used. Hard-coding ISSNs would be faster but a single digit
off would silently point at a different journal.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CR = os.environ.get("DOIFINDER_CROSSREF") or "https://api.crossref.org"
UA = "doi-finder/2.0 (journal monitor)"
TIMEOUT = 25

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL = 600  # Crossref etiquette: don't re-ask for the same list within 10 min

# Curated starting points. Names, not ISSNs — see the module docstring.
PRESETS = [
    "Journal of the American Chemical Society",
    "ACS Nano",
    "Organic Letters",
    "Chemical Reviews",
    "Journal of Chemical Physics",
    "Chemical Physics Letters",
    "Journal of Computational Chemistry",
    "Angewandte Chemie International Edition",
    "Nature",
    "Nature Chemistry",
    "Nature Materials",
    "Science",
    "Physical Review Letters",
    "Chemical Science",
    "Physical Chemistry Chemical Physics",
]


def _cached(key: str):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    return None


def _store(key: str, value) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)


def _log(msg: str) -> None:
    """抓取过程的诊断日志。出问题时能看见 Crossref 到底收到了什么 URL。"""
    try:
        import server
        server.log("[harvest] " + msg)
    except Exception:  # noqa: BLE001
        pass


def _get(url: str, rows: int = 3):
    import netpolicy
    url = netpolicy.prepare(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = ""
    for attempt in range(rows):
        try:
            with netpolicy.gate(url), urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 404):
                break
            time.sleep(netpolicy.retry_after(e, 1.2 * (attempt + 1)) if e.code in (429, 503) else 1.2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(last or "request failed")


def _metric(msg: dict, key: str) -> int:
    """Crossref reports counts as an object of counters, not a single number."""
    counts = msg.get(key)
    if isinstance(counts, dict):
        for name in ("total-dois", "current-dois", "backfile-dois"):
            if name in counts:
                try:
                    return int(counts[name] or 0)
                except (TypeError, ValueError):
                    return 0
        return 0
    try:
        return int(counts or 0)
    except (TypeError, ValueError):
        return 0


def search(query: str, rows: int = 12) -> list[dict]:
    """Find journals by name. Returns ISSN-carrying records, most relevant first."""
    q = (query or "").strip()
    if not q:
        return []
    key = f"jsearch:{q.lower()}:{rows}"
    hit = _cached(key)
    if hit is not None:
        return hit
    url = f"{CR}/journals?" + urllib.parse.urlencode({"query": q, "rows": rows})
    data = _get(url)
    out = []
    for it in (data.get("message", {}).get("items") or []):
        issns = it.get("ISSN") or []
        if not issns:
            continue
        out.append({"issn": issns[0], "issn_alt": issns[1:] or [],
                    "title": (it.get("title") or "").strip() or issns[0],
                    "publisher": it.get("publisher") or "",
                    "works": _metric(it, "counts")})
    _store(key, out)
    return out


def _simplify(name: str) -> list[str]:
    """把刊名逐步简化：原名 → 去标点 → 去前缀词（WIREs/The/Journal of…）→ 前三个词。"""
    out = []
    base = " ".join((name or "").split())
    if base:
        out.append(base)
    no_punct = re.sub(r"[^\w\s\-]+", " ", base).strip()
    if no_punct and no_punct != base:
        out.append(no_punct)
    words = [w for w in no_punct.split() if w]
    for drop in (("wires", "the", "a", "an"), ("journal", "of", "and", "for")):
        kept = [w for w in words if w.lower() not in drop]
        if kept and " ".join(kept) not in out:
            out.append(" ".join(kept))
    if len(words) > 3:
        out.append(" ".join(words[:3]))
    return out


def resolve(name: str) -> dict | None:
    """Best journal record for a preset name."""
    issn_like = re.fullmatch(r"\s*(\d{4}-?\d{3}[\dXx])\s*", name or "")
    if issn_like:
        got = _get(f"{CR}/journals/{urllib.parse.quote(issn_like.group(1), safe='')}")
        msg = got.get("message") or {}
        if msg.get("title"):
            return {"issn": msg.get("ISSN", [""])[0] if msg.get("ISSN") else "",
                    "title": msg["title"], "publisher": msg.get("publisher") or ""}
    cands = []
    for attempt in _simplify(name):
        cands = search(attempt, rows=5)
        if cands:
            break
    if not cands:
        return None
    norm = " ".join(name.lower().split())
    for c in cands:
        if " ".join(c["title"].lower().split()) == norm:
            return c
    return cands[0]


def _issue_year(item: dict) -> str:
    """Year of the printed issue (what the volume/issue numbers belong to)."""
    for field in ("published-print", "issued"):
        parts = ((item.get(field) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            return str(parts[0])
    return ""


def _natural(s: str):
    """Sort key so that volume 9 comes before volume 10 (and issue "S1" after 12)."""
    return [(0, int(x)) if x.isdigit() else (1, x.lower()) for x in re.findall(r"\d+|\D+", s or "")]


def _date_of(item: dict) -> str:
    """Return the date a reader cares about: when it appeared online.

    Crossref's plain `published` is often the *print issue* date, which can sit
    months in the future (a December issue assigned in November, or a 12-31
    placeholder). Showing that makes the feed look broken, so prefer
    `published-online` and only fall back to the others when it is missing or
    itself in the future.
    """
    today = datetime.date.today().isoformat()

    def read(field: str) -> str:
        parts = ((item.get(field) or {}).get("date-parts") or [[None]])[0]
        if not parts or not parts[0]:
            return ""
        return "-".join(f"{p:02d}" if i else f"{p}" for i, p in enumerate(parts) if p)

    online = read("published-online")
    if online and online <= today:
        return online
    # 取所有字段里"不晚于今天"的最早日期；实在没有未来日期时才用它
    seen = [d for d in (online, read("published"), read("issued"),
                        read("published-print"), read("created")) if d]
    past = [d for d in seen if d <= today]
    if past:
        return min(past)
    return min(seen) if seen else ""


def latest(issn: str, rows: int = 40, since: str = "", kind: str = "") -> list[dict]:
    """Most recent works in a journal.

    `since` is an ISO date (YYYY-MM-DD); Crossref filters server-side so a daily
    refresh costs one small response rather than a full listing.
    """
    issn = (issn or "").strip()
    if not issn:
        return []

    # Crossref 的 type 只认 "journal-article" 这类 ASCII 值。前端传过来的
    # "全部类型"、"综述" 这类界面选项一律当默认类型，否则 URL 里会带中文 → HTTP 400。
    if kind and not re.fullmatch(r"[a-z][a-z0-9-]*", kind):
        kind = "journal-article"
    key = f"latest:{issn}:{rows}:{since}:{kind}"
    hit = _cached(key)
    if hit is not None:
        return hit
    params = {"rows": min(max(rows, 1), 200), "sort": "published", "order": "desc"}
    filters = []
    if since:
        filters.append(f"from-pub-date:{since}")
    if kind == "journal-article":
        filters.append("type:journal-article")
    if filters:
        params["filter"] = ",".join(filters)
    url = f"{CR}/journals/{urllib.parse.quote(issn, safe='')}/works?" + urllib.parse.urlencode(params)
    data = _get(url)
    out = []
    for it in (data.get("message", {}).get("items") or []):
        doi = (it.get("DOI") or "").lower()
        title = " ".join((it.get("title") or [""])[0].split())
        if not doi or not title:
            continue
        authors = []
        for a in (it.get("author") or [])[:6]:
            nm = f"{a.get('given') or ''} {a.get('family') or ''}".strip()
            if nm:
                authors.append(nm)
        out.append({"doi": doi, "title": title, "authors": authors,
                    "date": _date_of(it), "type": it.get("type") or "",
                    "journal": (it.get("container-title") or [""])[0]})
    _store(key, out)
    return out


def total(issn: str) -> int:
    try:
        data = _get(f"{CR}/journals/{urllib.parse.quote(issn, safe='')}/works?rows=0")
        return int(data.get("message", {}).get("total-results") or 0)
    except Exception:  # noqa: BLE001
        return 0



_NON_RESEARCH = re.compile(
    r"^\s*(correction|corrigendum|erratum|errata|retraction|expression of concern|"
    r"editorial|issue information|front cover|back cover|cover picture|masthead|"
    r"table of contents|contents|index|author index|subject index|"
    r"publisher'?s note|addendum|comment on|reply to|response to)\b", re.I)
_REVIEW_TITLE = re.compile(
    r"\b(review|overview|perspective|perspectives|progress in|advances in|"
    r"state[- ]of[- ]the[- ]art|recent (advances|developments|progress)|"
    r"a (brief|short|critical) (review|survey)|tutorial|primer|roadmap|"
    r"opportunities and challenges|past, present)\b", re.I)
# journals that publish (almost) only reviews; anchored so "Physical Review B" is not matched
_REVIEW_JOURNAL = re.compile(
    r"^(wires\b|wiley interdisciplinary reviews|annual reviews?\b|annual review of|"
    r"chemical reviews|chemical society reviews|nature reviews|reviews (in|of)\b|"
    r"progress in\b|advances in\b|trends in\b|current opinion|critical reviews|"
    r"accounts of chemical research|reports on progress|physics reports)", re.I)


# Front matter that is never a paper worth downloading: issue information, covers,
# mastheads, tables of contents, indexes, and errata. Dropped during harvest, which
# also stops files like "Issue Information (5).pdf" from appearing in the output.
_JUNK = re.compile(
    r"^\s*(issue information|front cover|back cover|inside (front|back) cover|cover (picture|image|story)|"
    r"masthead|editorial board|table of contents|contents\s*(:|$)|index\s*$|author index|subject index|"
    r"keyword index|erratum|errata|corrigendum|correction( to)?\b|"
    r"publisher'?s note|retraction( note| notice)?)(?=\W|$)", re.I)


_CONTAINER_TYPES = {"journal-issue", "journal-volume", "journal", "book-series", "book-set"}


def is_junk(title: str) -> bool:
    return bool(_JUNK.match(title or ""))


def article_kind(title: str, journal: str = "", ctype: str = "journal-article") -> str:
    """Best-effort label: review | research | non-research.

    Crossref's `type` cannot tell a review from a research paper (both are
    "journal-article"), so this uses the journal's nature and the title. It is a
    heuristic, not a guarantee; the UI says so.
    """
    t = (title or "").strip()
    if ctype and ctype not in ("journal-article", ""):
        return "non-research"
    if not t or _NON_RESEARCH.match(t):
        return "non-research"
    if _REVIEW_JOURNAL.match((journal or "").strip()) or _REVIEW_TITLE.search(t):
        return "review"
    return "research"



def _earliest_year(issn: str) -> int:
    """这本刊最早一篇的年份；查不到就返回 0。"""
    try:
        url = (f"{CR}/journals/{urllib.parse.quote(issn, safe='')}/works?"
               + urllib.parse.urlencode({"rows": 1, "sort": "published", "order": "asc"}))
        data = _get(url, rows=2)
        items = (data.get("message") or {}).get("items") or []
        if items:
            y = _date_of(items[0])[:4]
            if y.isdigit():
                return int(y)
    except Exception:  # noqa: BLE001
        pass
    return 0


def harvest(issn: str, year_from: int = 0, year_to: int = 0,
            on_page=None, stop=None, kind: str = "journal-article",
            limit: int = 20000, errors: list | None = None) -> list[dict]:
    """把一本刊某段年份的全部文献抓下来。

    Crossref 的 cursor 深翻页在这个端点上会给 400，offset 又只到一万条，所以按年切片：
    一年之内的文献数很少超过一万，逐年 offset 翻页既不会漏也不会被上限挡住。
    """
    issn = (issn or "").strip()
    if not issn:
        return []

    # Crossref 的 type 只认 ASCII 值。中文选项（"全部类型"）在拼接 URL 时会被
    # 编码成 "????"，服务端只能返回 400。所以这里只放行已知类型，其余一律不加过滤。
    ALLOWED = {"journal-article", "proceedings-article", "book-chapter",
               "posted-content", "report", "dissertation", "reference-entry",
               "monograph", "edited-book", "book", "component", "dataset",
               "peer-review", "standard", "other"}
    if (kind or "").strip().lower() not in ALLOWED:
        kind = ""

    today_year = datetime.date.today().year
    # 不填起始年就从刊的第一篇算起：先问一次 Crossref 最早的年份，
    # 否则 1900 起步要白跑一百多次空请求，既慢又容易被限流。
    if not year_from:
        year_from = _earliest_year(issn)
        if not year_from:
            # Distinguish "unknown journal" from "no dates": without this the loop starts at 1900
            # and every year fails with an unhelpful HTTP 404.
            try:
                _get(f"{CR}/journals/{urllib.parse.quote(issn, safe='')}", rows=2)
            except Exception as e:  # noqa: BLE001
                if errors is not None:
                    errors.append(f"Crossref 查不到这本期刊（ISSN {issn}）：{e}")
                return []
            year_from = 1900
    first = int(year_from)
    last = int(year_to) if year_to else today_year

    out: list[dict] = []
    seen: set[str] = set()
    for year in range(first, last + 1):
        if stop is not None and stop.is_set():
            break
        offset = 0
        while True:
            if stop is not None and stop.is_set():
                break
            params = {
                "rows": 1000,
                "offset": offset,
                "sort": "published",
                "order": "asc",
                # Without select, Crossref returns full metadata (references, abstracts…)
                # for 1000 items per page: megabytes per request and the main cause of slow harvests.
                "select": "DOI,title,author,volume,issue,page,type,container-title,"
                          "published,published-print,published-online,issued,created",
                "filter": ",".join(x for x in [
                    f"type:{kind}" if kind else "",
                    f"from-pub-date:{year}-01-01",
                    f"until-pub-date:{year}-12-31",
                ] if x),
            }
            url = (f"{CR}/journals/{urllib.parse.quote(issn, safe='')}/works?"
                   + urllib.parse.urlencode(params))
            data = None
            last_err = ""
            for attempt in range(3):
                try:
                    data = _get(url, rows=3)
                    break
                except Exception as e:  # noqa: BLE001 - 退避后重试，别一失败就整批作废
                    last_err = f"{type(e).__name__}: {e}"
                    time.sleep(2 + 3 * attempt)
            if data is None:
                _log(f"FAIL year={year} offset={offset} url={url} err={last_err}")
                if errors is not None:
                    errors.append(f"{year} 年翻页失败：{last_err}")
                break
            msg = data.get("message") or {}
            items = msg.get("items") or []
            if not items:
                break
            for it in items:
                doi = (it.get("DOI") or "").lower()
                if not doi or doi in seen:
                    continue
                seen.add(doi)
                title = " ".join((it.get("title") or [""])[0].split())
                # "journal-issue"/"journal-volume" records (DOI like 10.1111/wcms.v14.4) have no
                # title and are not papers; they appear when no type filter is applied.
                if (it.get("type") or "journal-article") in _CONTAINER_TYPES or not title:
                    continue
                if is_junk(title):
                    continue
                authors = []
                for a in (it.get("author") or [])[:6]:
                    nm = f"{a.get('given') or ''} {a.get('family') or ''}".strip()
                    if nm:
                        authors.append(nm)
                date = _date_of(it)
                vol, iss = str(it.get("volume") or ""), str(it.get("issue") or "")
                # Folder year: a paper in "Volume 14, Issue 1" belongs under the year of that issue even
                # if it appeared online in December of the year before; otherwise use the online date.
                fyear = (_issue_year(it) if (vol or iss) else "") or (date or "")[:4] or str(year)
                out.append({
                    "doi": doi, "title": title, "authors": authors,
                    "date": date, "year": fyear,
                    "volume": vol,
                    "issue": iss,
                    "page": str(it.get("page") or ""),
                    "journal": (it.get("container-title") or [""])[0],
                    "type": it.get("type") or "",
                    "kind": article_kind(title, (it.get("container-title") or [""])[0], it.get("type") or ""),
                })
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
            if on_page:
                try:
                    on_page(len(out), int(msg.get("total-results") or 0), year, out)
                except Exception:  # noqa: BLE001
                    pass
            offset += len(items)
            if offset >= int(msg.get("total-results") or 0):
                break
        if len(out) >= limit:
            break

    out.sort(key=lambda x: (x.get("year") or "", _natural(x.get("volume")),
                            _natural(x.get("issue")), x.get("doi") or ""))
    return out
