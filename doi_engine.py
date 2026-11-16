"""Core engine: parse title lists, resolve DOIs via CrossRef (OpenAlex fallback), score matches.

No third-party dependencies. All network access uses urllib with a polite User-Agent.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import validation

CROSSREF = "https://api.crossref.org/works"
OPENALEX = "https://api.openalex.org/works"

STOP = set(
    "a an the of in on for and to with by from via at as is are its their this that "
    "using use into over under between within".split()
)

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;\"'\)\]<>]+", re.I)
QUOTED_RE = re.compile(r"[\"“”„]([^\"“”„]{8,400})[\"“”„]")
YEAR_RE = re.compile(r"\((\d{4})[a-z]?\)")
NUM_PREFIX_RE = re.compile(r"^\s*(?:\[\s*\d+\s*\]|\(\s*\d+\s*\)|\d+\s*[.)\]:]|#\s*\d+)\s+")
PREFIX_NOISE_RE = re.compile(
    r"^\s*(?:comment on|reply to|erratum on|erratum:|addendum:|correction to|"
    r"communication:|letter to the editor:?)\s*", re.I
)

SELECT = "DOI,title,author,issued,container-title,type,volume,issue,page"

# A candidate has to explain this much of a reference string before we let it
# override title similarity. 0.6 keeps the "three pieces of evidence" cases and
# drops the ones where only the title lined up. The env override exists so the
# regression harness can A/B the layer without editing code.
VALIDATION_ACCEPT = float(os.environ.get("DOIFINDER_VALIDATION_ACCEPT", "0.60"))
VALIDATION_STRONG = 0.75


# ----------------------------------------------------------------------------- text utils
_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\u00ad"), " ")
_QUOTES = dict.fromkeys(map(ord, "\u2018\u2019\u201a\u201b\u2032\u02bc\u00b4"), None)


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    # Unicode punctuation must be handled before the ascii fold, otherwise
    # "Forward−Backward" collapses to "forwardbackward" and the words are lost.
    s = s.translate(_DASHES).translate(_QUOTES)
    s = s.encode("ascii", "ignore").decode()
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.lower().replace("&", " and ")
    s = s.replace("'", "").replace("`", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return [t for t in norm_text(s).split() if t not in STOP and len(t) > 1]


def similarity(a: str, b: str) -> float:
    """Blend of query coverage, candidate coverage, Jaccard and sequence ratio (0..1).

    Two coverage terms keep a short candidate ("Forward-Backward Semiclassical Dynamics")
    from beating a full-title match just because all of its words occur in the query.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    inter = len(sa & sb)
    cover_q = inter / len(sa)
    cover_c = inter / len(sb)
    jac = inter / len(sa | sb)
    na, nb = norm_text(a), norm_text(b)
    seq = SequenceMatcher(None, na, nb).ratio()
    # comparing without spaces forgives "wave functions" vs "wavefunctions"
    seq = max(seq, SequenceMatcher(None, na.replace(" ", ""), nb.replace(" ", "")).ratio())
    return round(0.30 * cover_q + 0.25 * cover_c + 0.25 * jac + 0.20 * seq, 4)


# ----------------------------------------------------------------------------- field evidence
# Title similarity alone cannot tell two records with the same title apart (a preprint and its
# journal version, "Introduction", a review series). When the input carries a year, a first
# author or a journal (BibTeX/RIS do), those fields are compared with each candidate.
#
# Rules, chosen so that a false alarm costs little and a miss is never hidden:
#   * only a clear contradiction counts against a candidate (year >= 2 apart, or the first
#     author's surname absent from every author the record lists);
#   * a missing field on either side is "unknown", never a penalty;
#   * a contradiction demotes one status level per field and never discards the record;
#   * the journal is positive-only evidence (abbreviations make a mismatch unprovable).
_YEAR_RE = re.compile(r"(?<!\d)(1[6-9]\d\d|20\d\d)(?!\d)")
_RANK_ADJ = {
    "year": {"match": 0.03, "near": 0.015, "mismatch": -0.10},
    "author": {"match": 0.03, "partial": 0.01, "mismatch": -0.10},
    "journal": {"match": 0.01},
}
_LEVELS = ("uncertain", "likely", "matched")
PROMOTE_FLOOR = 0.70      # title similarity needed before year+author may lift uncertain -> likely


def parse_year(v) -> int | None:
    """First plausible 4-digit year in `v` ("2019/2020" -> 2019); None if there is none."""
    if v is None:
        return None
    m = _YEAR_RE.search(str(v))
    return int(m.group(1)) if m else None


def _surname_key(name: str) -> str:
    """The distinctive surname token of a typed author name, "" if nothing readable.

    "Smith, Ann" / "Ann Smith" / "Smith A" / "A. Smith" -> smith;
    "{van der Berg}, Jan" -> berg;  "张伟" -> "" (nothing to compare in ASCII).
    """
    name = (name or "").strip()
    head = name.split(",", 1)[0] if "," in name else name
    toks = norm_text(head).split()
    if "," not in name:
        toks = [t for t in toks if len(t) > 1] or toks      # drop bare initials
    return toks[-1] if toks else ""


def _tok_eq(key: str, tok: str) -> bool:
    if key == tok:
        return True
    # "Mueller" ~ "Muller": forgive transliteration, but never for short names (li ~ lee)
    return len(key) >= 4 and len(tok) >= 4 and SequenceMatcher(None, key, tok).ratio() >= 0.82


def _has_name(key: str, author: str) -> bool:
    return any(_tok_eq(key, t) for t in norm_text(author).split())


def hint_matches(hint: str, authors: list[str]) -> bool:
    """Does the typed surname occur as a whole name token in any listed author?"""
    key = _surname_key(hint)
    return bool(key) and any(_has_name(key, a) for a in authors or [])


def journal_matches(typed: str, listed: str) -> bool:
    """Abbreviation-tolerant: same number of content words, each typed word a prefix of its
    counterpart ("J. Chem. Phys." ~ "The Journal of Chemical Physics"). Positive-only."""
    a = [t for t in norm_text(typed).split() if t not in STOP]
    b = [t for t in norm_text(listed).split() if t not in STOP]
    return bool(a) and len(a) == len(b) and all(y.startswith(x) for x, y in zip(a, b))


def norm_expect(expect: dict | None) -> dict:
    """Keep only the expectations that are actually usable."""
    out: dict = {}
    if not isinstance(expect, dict) or not expect:
        return out
    y = parse_year(expect.get("year"))
    if y:
        out["year"] = y
    for k in ("author", "journal"):
        v = str(expect.get(k) or "").strip()
        if re.search(r"[^\W\d_]", v):          # needs at least one letter; "123" is not a name
            out[k] = v
    return out


def field_evidence(c: dict, exp: dict) -> dict:
    """{"year": match|near|mismatch|unknown, "author": match|partial|mismatch|unknown,
    "journal": match|unknown} for the fields present in `exp`."""
    ev: dict = {}
    if "year" in exp:
        cy = parse_year(c.get("year"))
        if cy is None:
            ev["year"] = "unknown"
        else:
            d = abs(cy - exp["year"])
            ev["year"] = "match" if d == 0 else "near" if d == 1 else "mismatch"
    if "author" in exp:
        key = _surname_key(exp["author"])
        authors = [a for a in (c.get("authors") or []) if a and a.strip()]
        if not key or not authors or not norm_text(authors[0]):
            ev["author"] = "unknown"
        elif _has_name(key, authors[0]):
            ev["author"] = "match"
        elif any(_has_name(key, a) for a in authors[1:]):
            ev["author"] = "partial"
        else:
            ev["author"] = "mismatch"
    if "journal" in exp:
        ev["journal"] = "match" if journal_matches(exp["journal"], c.get("journal") or "") \
            else "unknown"
    return ev


def evidence_adjust(ev: dict) -> float:
    return sum(_RANK_ADJ.get(k, {}).get(v, 0.0) for k, v in (ev or {}).items())


def expect_key(entry: dict) -> str:
    """Everything besides the title that changes what resolve() returns for this entry.

    Used for the dedup map and the on-disk cache so that two rows with the same title but a
    different year / author / supplied DOI / reference string are never merged."""
    parts = []
    exp = norm_expect(entry)
    if exp:
        parts.append("y=%s;a=%s;j=%s" % (exp.get("year", ""), norm_text(exp.get("author", "")),
                                         norm_text(exp.get("journal", ""))))
    if entry.get("doi"):
        parts.append("d=" + str(entry["doi"]).strip().lower())
    raw = NUM_PREFIX_RE.sub("", entry.get("input") or "").strip()
    if raw and validation.looks_scoreable(raw):
        parts.append("r=" + norm_text(raw))
    return "|".join(parts)


# ----------------------------------------------------------------------------- parsing
def extract_doi(s: str) -> str | None:
    m = DOI_RE.search(s or "")
    if not m:
        return None
    d = m.group(0).rstrip(".").rstrip(")")
    return re.sub(r"\.s\d{3}$", "", d)


def looks_like_citation(line: str) -> bool:
    """A quotation only marks a reference if journal/volume/year text follows it.

    Keeps a bare title such as  Comment on “Unified framework…”  from being
    truncated to the quoted fragment.
    """
    m = QUOTED_RE.search(line)
    if m:
        tail = line[m.end():]
        return bool(YEAR_RE.search(tail)) or bool(re.search(r",\s*\S", tail))
    return bool(YEAR_RE.search(line) and line.count(",") >= 2)


def title_from_citation(line: str) -> str:
    """Extract the work title from a full reference string."""
    m = QUOTED_RE.search(line)
    if m:
        return m.group(1).strip(" .,;")
    body = DOI_RE.sub(" ", line)
    body = YEAR_RE.sub(" ", body)
    parts = [p.strip(" .;") for p in body.split(",")]
    parts = [p for p in parts if len(p.split()) >= 3]
    if not parts:
        return body.strip(" .;")
    best = max(parts, key=len)
    best = re.sub(r"\s+(?:J|Phys|Chem|Proc|Nature|Science|Mol|Annu|Faraday|Int)\.? .*$", "", best)
    return best.strip(" .;")


def parse_entries(text: str, mode: str = "auto") -> list[dict]:
    """Turn raw pasted text into entries. mode: auto | titles | citations."""
    # 粘贴 BibTeX / RIS 时直接按结构化字段取，里面有 DOI 就不必再查
    try:
        import citeparse
        struct = citeparse.parse_structured(text or "")
    except Exception:  # noqa: BLE001
        struct = []
    if struct:
        return [{"n": i + 1, "input": (e.get("title") or e.get("doi") or ""),
                 "title": e.get("title") or "", "doi": (e.get("doi") or "").lower(),
                 "numbered": False, "journal": e.get("journal") or "",
                 "year": e.get("year") or "", "author": e.get("author") or ""}
                for i, e in enumerate(struct)]

    entries: list[dict] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if re.match(r"^(#\s|//|--\s)", line):
            continue
        numbered = bool(NUM_PREFIX_RE.match(line))
        body = NUM_PREFIX_RE.sub("", line).strip()
        if not body:
            continue
        doi = extract_doi(body)
        use_citation = mode == "citations" or (mode == "auto" and looks_like_citation(body))
        title = title_from_citation(body) if use_citation else body
        if doi:
            title = re.sub(r"\s*doi:?\s*\S+$", "", title, flags=re.I).strip(" .,;")
        title = title.strip(" .,;: ")
        if not title:
            continue
        entries.append({"n": len(entries) + 1, "input": line, "title": title,
                        "doi": doi, "numbered": numbered})
    return entries


# ----------------------------------------------------------------------------- network
class Http:
    def __init__(self, mailto: str = "", timeout: int = 25):
        self.mailto = mailto
        self.timeout = timeout
        self.ua = "doi-finder/1.0" + (f" (mailto:{mailto})" if mailto else "")

    def get_json(self, url: str, tries: int = 3):
        import netpolicy
        url = netpolicy.prepare(url, self.mailto)
        last = None
        for attempt in range(tries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                           "Accept": "application/json"})
                with netpolicy.gate(url, self.mailto), \
                        urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code in (400, 404):
                    break
                if e.code in (429, 503):
                    time.sleep(netpolicy.retry_after(e, 3.0 * (attempt + 1)))
                    continue
            except Exception as e:  # noqa: BLE001
                last = type(e).__name__
            time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(last or "request failed")


def crossref_items(http: Http, query: str, rows: int = 10, author: str | None = None,
                   field: str = "bibliographic") -> list[dict]:
    params = {"rows": rows, "select": SELECT, f"query.{field}": query}
    if author:
        params["query.author"] = author
    if http.mailto:
        params["mailto"] = http.mailto
    data = http.get_json(f"{CROSSREF}?{urllib.parse.urlencode(params)}")
    return data.get("message", {}).get("items", []) or []


def openalex_items(http: Http, query: str, rows: int = 5) -> list[dict]:
    params = {"per-page": rows, "search": query,
              "select": "doi,title,publication_year,primary_location,type,authorships"}
    try:
        data = http.get_json(f"{OPENALEX}?{urllib.parse.urlencode(params)}")
    except RuntimeError as e:
        # A rejected field list must not silently switch the whole fallback off.
        if str(e) != "HTTP 400":
            raise
        params["select"] = "doi,title,publication_year,primary_location,type"
        data = http.get_json(f"{OPENALEX}?{urllib.parse.urlencode(params)}")
    out = []
    for it in data.get("results", []) or []:
        year = it.get("publication_year")
        src = ((it.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        names = [((a or {}).get("author") or {}).get("display_name") or ""
                 for a in (it.get("authorships") or [])]
        out.append({"DOI": (it.get("doi") or "").replace("https://doi.org/", ""),
                    "title": [it.get("title") or ""],
                    "issued": {"date-parts": [[year]]} if year else {},
                    "container-title": [src], "type": it.get("type"),
                    "author": [{"name": n} for n in names if n.strip()]})
    return out


def to_candidate(item: dict, source: str) -> dict | None:
    title = (item.get("title") or [""])[0] if item.get("title") else ""
    if not title:
        return None
    doi = re.sub(r"\.s\d{3}$", "", (item.get("DOI") or "").strip())
    if not doi:
        return None
    year = None
    try:
        year = item["issued"]["date-parts"][0][0]
    except Exception:  # noqa: BLE001
        pass
    authors = []
    for a in (item.get("author") or []):
        # organisations come as {"name": ...}; an empty entry must not become "first author"
        n = f"{(a.get('given') or '')} {(a.get('family') or '')}".strip() or (a.get("name") or "").strip()
        if n:
            authors.append(n)
    return {"doi": doi, "title": title, "year": year,
            "journal": (item.get("container-title") or [""])[0] if item.get("container-title") else "",
            "volume": item.get("volume") or "", "issue": item.get("issue") or "",
            "page": item.get("page") or "",
            "type": item.get("type") or "", "authors": authors[:8], "source": source, "score": 0.0}


def search_variants(title: str) -> list[str]:
    out = [title.strip()]
    stripped = PREFIX_NOISE_RE.sub("", title).strip(" .,;:\"'“”")
    if stripped and stripped != out[0]:
        out.append(stripped)
    return out


def _score_all(cands: list[dict], title: str, author_hint: str | None) -> list[dict]:
    for c in cands:
        c["score"] = similarity(title, c["title"])
        # repository/librarian-minted records usually carry no author list and are
        # almost never the version of record
        if c["source"] == "crossref" and not c["authors"]:
            c["score"] = round(max(0.0, c["score"] - 0.05), 4)
        if author_hint and c["authors"] and hint_matches(author_hint, c["authors"]):
            c["score"] = round(min(1.0, c["score"] + 0.06), 4)
    return sorted(cands, key=lambda x: x["score"], reverse=True)


def _alt(c: dict, with_evidence: bool = False) -> dict:
    a = {k: c[k] for k in ("doi", "title", "year", "journal", "score", "source")}
    a["first_author"] = (c.get("authors") or [""])[0]
    if with_evidence and c.get("ev"):
        a["evidence"] = c["ev"]
    return a


SPECIAL_KINDS = ("erratum", "correction", "addendum", "comment", "reply")


def _special_kind(query: str) -> str | None:
    q = norm_text(query)
    for kw in SPECIAL_KINDS:
        if q.startswith(kw + " ") or q.startswith(kw + ":") or f" {kw} on " in q[:60]:
            return kw
    return None


def _apply_special(query: str, pool: list[dict]) -> list[dict]:
    """Boost records that are themselves errata/comments when the query asks for one."""
    kw = _special_kind(query)
    if not kw:
        return pool
    for c in pool:
        if norm_text(c["title"]).startswith(kw):
            c["score"] = round(min(1.0, c["score"] + 0.12), 4)
    return pool


def _apply_validation(pool: list[dict], raw: str) -> dict | None:
    """Pick the candidate that explains the reference string, not just its title.

    Returns the winning candidate, or None when the input is not a full
    reference (nothing to validate against).
    """
    if not raw or not pool or not validation.looks_scoreable(raw):
        return None
    for c in pool:
        c["vscore"] = round(validation.score(c, raw), 4)
    supported = [c for c in pool if c["vscore"] >= VALIDATION_ACCEPT]
    if supported:
        return max(supported, key=lambda c: (c["vscore"], c["score"]))
    return max(pool, key=lambda c: (c["vscore"], c["score"]))


def resolve(title: str, http: Http, author_hint: str | None = None, rows: int = 10,
            provided_doi: str | None = None, use_openalex: bool = True,
            raw: str = "", expect: dict | None = None) -> dict:
    """Resolve one title to a DOI plus alternates. Never raises.

    `raw` is the original input line. When it looks like a full reference we also
    run field validation, which is far better at rejecting a plausible-looking
    wrong DOI than title similarity alone.

    `expect` ({"year", "author", "journal"}, any subset) carries what a structured
    input (BibTeX/RIS) says about the work. It re-ranks candidates and can demote a
    match whose year or first author clearly disagrees; see field_evidence().
    """
    exp = norm_expect(expect)
    res = {"doi": "", "status": "not_found", "score": 0.0, "match_title": "", "year": None,
           "journal": "", "volume": "", "page": "", "type": "", "source": "",
           "alts": [], "note": "", "provided_doi": provided_doi or ""}
    pool: list[dict] = []

    try:
        # 0. a DOI already present in the input takes precedence (after verification)
        if provided_doi:
            try:
                msg = http.get_json(f"{CROSSREF}/{urllib.parse.quote(provided_doi)}").get("message", {})
                c = to_candidate(msg, "provided")
                if c:
                    c["score"] = similarity(title, c["title"])
                    pool.append(c)
                    res["note"] = ("DOI supplied in input, title agrees" if c["score"] >= 0.85
                                   else "DOI supplied in input, but its Crossref title differs")
            except Exception:  # noqa: BLE001 - unregistered DOIs exist (arXiv, books)
                res["note"] = "DOI supplied in input (could not verify via Crossref)"

        # For a full reference, hand Crossref the whole string: query.bibliographic
        # was built for exactly that, and our own title parse is the weaker link.
        if raw and validation.looks_scoreable(raw):
            whole = raw.strip()[:400]
            variants = [whole] + [v for v in search_variants(title) if v and v != whole]
        else:
            variants = search_variants(title)
        best: dict | None = None

        def attempt(query: str, field: str, author: str | None = None):
            nonlocal best
            try:
                cands = [c for c in (to_candidate(i, "crossref")
                                     for i in crossref_items(http, query, rows, author, field)) if c]
            except RuntimeError as e:
                res["note"] = res["note"] or f"network: {e}"
                return
            got = _score_all(cands, title, author_hint)
            pool.extend(got)
            if got and (best is None or got[0]["score"] > best["score"]):
                best = got[0]

        # 1. full title, bibliographic relevance search
        attempt(variants[0], "bibliographic")

        # 2. precise title-field search (noise prefix removed, author hint if given). Also
        #    when the leader's year / first author contradicts the input: same-title records
        #    (preprint vs journal, "Introduction") may be sitting just below the first page.
        contradicted = bool(exp and best is not None and any(
            v == "mismatch" for v in field_evidence(best, exp).values()))
        if best is None or best["score"] < 0.97 or contradicted:
            attempt(variants[1] if len(variants) > 1 else variants[0], "title",
                    author_hint or _surname_key(exp.get("author", "")) or None)

        # 3. OpenAlex exact-title fallback for stubborn rows
        if use_openalex and (best is None or best["score"] < 0.90):
            try:
                cands = [c for c in (to_candidate(i, "openalex")
                                     for i in openalex_items(http, variants[-1], rows)) if c]
                got = _score_all(cands, title, author_hint)
                pool.extend(got)
                if got and (best is None or got[0]["score"] > best["score"]):
                    best = got[0]
            except RuntimeError:
                pass

        # 4. "Erratum on X" / "Comment on X" should not resolve to X itself
        pool = _apply_special(title, pool)
        for c in pool:
            c["ev"] = field_evidence(c, exp) if exp else {}

        def rank(c: dict) -> float:
            return c["score"] + evidence_adjust(c.get("ev"))

        if pool:
            best = max(pool, key=rank)
            # a DOI the user typed and whose own Crossref title agrees is the answer; a better
            # looking sibling (preprint/version of record) is offered as an alternate instead
            mine = next((c for c in pool if c["source"] == "provided"), None)
            if mine is not None and mine["score"] >= 0.85:
                best = mine

        # 4.5 full-reference input: let the bibliographic numbers decide
        weak_reference = False
        validated = _apply_validation(pool, raw)
        if validated is not None:
            if validated.get("vscore", 0.0) >= VALIDATION_ACCEPT:
                best = validated
                res["vscore"] = validated["vscore"]
                res["note"] = (res["note"] + "; " if res["note"] else "") + \
                    "validated against reference"
            else:
                # The numbers did not line up. Keep the title-based answer but
                # stop claiming certainty — Crossref's experiments show this is
                # where wrong DOIs usually slip through.
                weak_reference = True
                res["vscore"] = validated.get("vscore", 0.0)
                res["note"] = (res["note"] + "; " if res["note"] else "") + \
                    "reference numbers not corroborated"

        if provided_doi and (best is None or best["score"] < 0.85):
            res.update({"doi": provided_doi, "status": "provided",
                        "score": best["score"] if best else 0.0, "match_title": title,
                        "source": "provided"})
            return res

        if not pool or best is None:
            return res

        pool.sort(key=rank, reverse=True)
        seen = {best["doi"]}
        alts = []
        for c in pool:
            if c["doi"] in seen:
                continue
            seen.add(c["doi"])
            alts.append(_alt(c, bool(exp)))
            if len(alts) == 4:
                break

        s = best["score"]
        v = best.get("vscore")
        if v is not None and v >= VALIDATION_ACCEPT:
            # A validated match is judged on corroboration, not on how similar
            # the titles look: a reference can quote a translated or abbreviated
            # title and still point at exactly the right record.
            status = "matched" if v >= VALIDATION_STRONG else "likely"
            res["vscore"] = v
        elif s >= 0.94:
            status = "matched"
        elif s >= 0.78:
            status = "likely"
        elif s >= 0.60:
            status = "uncertain"
        else:
            # nothing close enough to claim: surface the best guess only as an alternative
            res.update({"status": "not_found", "score": round(s, 4),
                        "alts": ([_alt(best, bool(exp))] + alts)[:4],
                        "note": (res["note"] + "; " if res["note"] else "") + "no confident match"})
            return res

        if weak_reference:
            status = {"matched": "likely", "likely": "uncertain"}.get(status, status)
            res["note"] = (res["note"] + "; " if res["note"] else "") + "demoted: unverified reference"

        ev = best.get("ev") or {}
        if exp:
            bad = [k for k in ("year", "author") if ev.get(k) == "mismatch"]
            notes = []
            if "year" in bad:
                notes.append(f"year differs (input {exp['year']}, record {parse_year(best.get('year'))})")
            if "author" in bad:
                first = (best.get("authors") or [""])[0]
                notes.append(f"first author differs (input {exp['author']}, record {first})")
            if bad:
                status = _LEVELS[max(0, _LEVELS.index(status) - len(bad))]
            elif (status == "uncertain" and not weak_reference and s >= PROMOTE_FLOOR
                  and ev.get("year") == "match" and ev.get("author") == "match"):
                status = "likely"
                notes.append("year and first author agree")
            if notes:
                joined = "; ".join(notes)
                # a contradiction is what the reader must see first (the terminal client
                # shows only the first clause of a note)
                res["note"] = (joined + "; " + res["note"] if res["note"] else joined) if bad \
                    else (res["note"] + "; " if res["note"] else "") + joined
            res["evidence"] = ev

        res.update({"doi": best["doi"], "status": status, "score": round(s, 4),
                    "match_title": best["title"], "year": best["year"], "journal": best["journal"],
                    "volume": best["volume"], "issue": best.get("issue") or "",
                    "page": best["page"], "type": best["type"],
                    "authors": best.get("authors") or [],
                    "source": best["source"], "alts": alts})

        kw = _special_kind(title)
        if kw and not norm_text(best["title"]).startswith(kw):
            status = {"matched": "likely", "likely": "uncertain"}.get(status, status)
            res["note"] = (res["note"] + "; " if res["note"] else "") + \
                f"no separate {kw} record found - returned the article it refers to"
            res["status"] = status
        elif status == "uncertain":
            res["note"] = (res["note"] + "; " if res["note"] else "") + "low title similarity"
        return res
    except Exception as e:  # noqa: BLE001 - one bad row must not kill the batch
        res["status"] = "error"
        res["note"] = f"{type(e).__name__}: {e}"
        return res


# ----------------------------------------------------------------------------- batch
class Cache:
    def __init__(self, path: str | None = None):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        if path and os.path.exists(path):
            try:
                self.data = json.load(open(path, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self.data = {}

    @staticmethod
    def key(title: str, author: str | None, extra: str = "") -> str:
        # plain titles keep the historical key, so existing cache files stay valid
        k = norm_text(title) + "||" + norm_text(author or "")
        return k + "||" + extra if extra else k

    def get(self, title: str, author: str | None, extra: str = ""):
        with self.lock:
            hit = self.data.get(self.key(title, author, extra))
            return json.loads(json.dumps(hit)) if hit else None

    def put(self, title: str, author: str | None, value: dict, extra: str = ""):
        with self.lock:
            self.data[self.key(title, author, extra)] = json.loads(json.dumps(value))

    def flush(self):
        if not self.path:
            return
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)


def lookup_all(entries: list[dict], author_hint: str | None = None, rows: int = 10,
               workers: int = 5, mailto: str = "", cache: Cache | None = None,
               use_openalex: bool = True, progress=None, stop_event=None,
               dedup: bool = True, on_result=None) -> list[dict]:
    http = Http(mailto=mailto)
    results: list[dict | None] = [None] * len(entries)
    done = 0
    lock = threading.Lock()
    seen: dict[str, dict] = {}

    def stopped() -> bool:
        return bool(stop_event and stop_event.is_set())

    def work(idx: int, entry: dict):
        nonlocal done
        if stopped():
            results[idx] = {"status": "stopped", "doi": "", "score": 0.0, "alts": [], "note": "",
                            "n": entry["n"], "input": entry["input"], "title": entry["title"],
                            "provided_doi": entry.get("doi") or ""}
            with lock:
                done += 1
                if progress:
                    progress(done, len(entries))
            return

        extra = expect_key(entry)
        key = norm_text(entry["title"]) + "||" + extra
        hit = None
        if dedup:
            with lock:
                hit = seen.get(key)
        if hit is None and cache:
            hit = cache.get(entry["title"], author_hint, extra)
        if hit:
            res = dict(hit)
            res["cached"] = True
        else:
            try:
                res = resolve(entry["title"], http, author_hint, rows, entry.get("doi"),
                              use_openalex, raw=entry.get("input") or "", expect=entry)
            except Exception as e:  # noqa: BLE001 - 一条失败不能拖垮整批 485 条
                res = {"status": "error", "doi": "", "score": 0.0, "alts": [],
                       "note": f"{type(e).__name__}: {str(e)[:60]}"}
            res["cached"] = False
            # never cache a miss caused by a network error, only genuine no-matches
            if cache and res.get("status") not in ("error", "not_found"):
                cache.put(entry["title"], author_hint, res, extra)
            if dedup and res.get("status") not in ("error",):
                with lock:
                    seen[key] = res
        res.update({"n": entry["n"], "input": entry["input"], "title": entry["title"],
                    "provided_doi": entry.get("doi") or ""})
        results[idx] = res
        if on_result:
            try:
                on_result(res)
            except Exception:  # noqa: BLE001
                pass
        with lock:
            done += 1
            if progress:
                progress(done, len(entries))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, i, e) for i, e in enumerate(entries)]
        for f in futures:
            f.result()

    # Second sweep: CrossRef relevance rankings wobble under load, so anything that
    # came back empty or errored gets one calm, sequential retry.
    retry = [i for i, r in enumerate(results) if r and r.get("status") in ("not_found", "error")]
    if retry and not stopped():
        calm = Http(mailto=mailto, timeout=20)
        for i in retry[:8]:
            if stopped():
                break
            e = entries[i]
            if progress:
                progress(len(entries), len(entries))
            try:
                again = resolve(e["title"], calm, author_hint, rows, e.get("doi"), use_openalex,
                                raw=e.get("input") or "", expect=e)
            except Exception:  # noqa: BLE001
                continue
            prev = results[i] or {}
            if again.get("doi") and not prev.get("doi"):
                again.update({"n": e["n"], "input": e["input"], "title": e["title"],
                              "provided_doi": e.get("doi") or "", "cached": False, "retried": True})
                results[i] = again
                if cache and again.get("status") not in ("error", "not_found"):
                    cache.put(e["title"], author_hint, again, expect_key(e))
            time.sleep(0.7)

    if cache:
        cache.flush()
    return [r for r in results if r is not None]

