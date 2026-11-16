"""Open-access resolution and topic search.

Everything here uses public, legal metadata services: Unpaywall, OpenAlex,
arXiv and Europe PMC. The optional Sci-Hub / institutional-browser fallbacks live in scihub.py,
carsi.py and browser.py and are driven by downloader.py.
"""
from __future__ import annotations

import platform_util
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA_BASE = "DOIFinder/2.0 (+https://localhost)"


def _ua(mailto: str = "") -> str:
    return UA_BASE + (f" mailto:{mailto}" if mailto else "")


def get_json(url: str, mailto: str = "", timeout: int = 15, tries: int = 3, headers: dict | None = None):
    import netpolicy
    url = netpolicy.prepare(url, mailto)
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _ua(mailto), "Accept": "application/json", **(headers or {})})
            with netpolicy.gate(url, mailto), urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break
            if e.code == 429:  # polite-pool throttle: wait longer than the default backoff
                time.sleep(netpolicy.retry_after(e, 5.0 * (attempt + 1)))
                continue
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(last or "request failed")


def _norm_doi(doi: str) -> str:
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (doi or "").strip(), flags=re.I)


# --------------------------------------------------------------------------- OA candidates
def _loc(url: str, source: str, version: str = "", license_: str = "", host: str = "") -> dict | None:
    if not url or not str(url).startswith("http"):
        return None
    return {"url": url, "source": source, "version": version or "",
            "license": license_ or "", "host": host or ""}


def arxiv_candidate(doi: str) -> list[dict]:
    m = re.match(r"10\.48550/arxiv\.(.+)$", doi, re.I)
    if not m:
        return []
    ident = m.group(1)
    return [_loc(f"https://arxiv.org/pdf/{ident}", "arXiv", "preprint")]


def unpaywall_candidates(doi: str, email: str) -> list[dict]:
    if not email:
        return []
    data = get_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={urllib.parse.quote(email)}",
                    email)
    out = []
    for loc in [data.get("best_oa_location")] + list(data.get("oa_locations") or []):
        if not loc:
            continue
        url = loc.get("url_for_pdf") or loc.get("url")
        out.append(_loc(url, "Unpaywall", loc.get("version") or "",
                        loc.get("license") or "", loc.get("host_type") or ""))
    return [x for x in out if x]


def openalex_candidates(doi: str, mailto: str) -> list[dict]:
    """Every OA location OpenAlex knows, published version first."""
    data = get_json("https://api.openalex.org/works/https://doi.org/" + urllib.parse.quote(doi)
                    + "?select=best_oa_location,locations", mailto, timeout=15, tries=2)
    rank = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
    locs = [x for x in ([data.get("best_oa_location")] + list(data.get("locations") or []))
            if x and x.get("is_oa")]
    locs.sort(key=lambda x: rank.get(x.get("version"), 3))
    out = []
    for loc in locs:
        if loc.get("pdf_url"):
            src = ((loc.get("source") or {}).get("display_name") or "OpenAlex")
            out.append(_loc(loc["pdf_url"], "OpenAlex", loc.get("version") or "",
                            loc.get("license") or "", src))
    for loc in locs:
        if not loc.get("pdf_url") and loc.get("landing_page_url"):
            src = ((loc.get("source") or {}).get("display_name") or "OpenAlex")
            out.append(_loc(loc["landing_page_url"], "OpenAlex·落地页", loc.get("version") or "",
                            loc.get("license") or "", src))
    return [x for x in out if x]



def _epmc_pmcid(doi: str) -> str:
    q = urllib.parse.quote(f'DOI:"{doi}"')
    try:
        meta = get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                        f"?query={q}&format=json&resultType=core&pageSize=1", timeout=15)
    except Exception:  # noqa: BLE001
        return ""
    results = ((meta or {}).get("resultList") or {}).get("result") or []
    if not results:
        return ""
    rec = results[0]
    pmcid = rec.get("pmcid") or ""
    if not pmcid and (rec.get("source") or "").upper() == "PMC" and rec.get("id"):
        pmcid = "PMC" + str(rec["id"])
    return pmcid


def europepmc_repo_file(doi: str) -> tuple[bytes | None, str]:
    """Europe PMC 前端的文件清单：非 OA 记录也能拿到 PDF 或 JATS 全文。

    这条不是官方 REST API，是它自己阅读器用的内部接口，但确实能返回订阅刊全文的
    PDF/XML（很多期刊按公共获取政策存了稿）。拿不到就返回空，不重试、不纠缠。
    """

    pmcid = _epmc_pmcid(doi)
    if not pmcid:
        return None, "Europe PMC 没有这篇的记录"
    try:
        meta = get_json("https://europepmc.org/api/fulltextRepo"
                        f"?pmcId={pmcid}&type=METADATA", timeout=20)
    except Exception:  # noqa: BLE001
        return None, "清单取不到"
    files = (meta or {}).get("files") or []
    pdf = [f for f in files if (f.get("mimeType") or "") == "application/pdf"
           and (f.get("type") or "") != "supplement"]
    xmls = [f for f in files if (f.get("mimeType") or "") == "application/xml"]
    for f in (pdf[:1] + xmls[:1]):
        url = f.get("url") or ""
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DOIFinder/2.1"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
        except Exception:  # noqa: BLE001
            continue
        if not data:
            continue
        if data[:4] == b"%PDF":
            return data, f"Europe PMC 仓储 PDF（{pmcid}）"
        if b"<article" in data[:4000]:
            return data, f"Europe PMC JATS 全文（{pmcid}）"
    return None, "清单里没有可下的文件"


def europepmc_fulltext_xml(doi: str) -> bytes | None:
    """Europe PMC 的 JATS 全文。

    很多订阅期刊按公共获取政策把全文存进了 PMC，PDF 不让下，但 XML 全文可以拿。
    没有 PDF 不等于没有全文，这份 XML 就是正文本身。
    """
    import urllib.parse as _up
    import urllib.request as _rq

    q = _up.quote(f'DOI:"{doi}"')
    try:
        meta = get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                        f"?query={q}&format=json&resultType=core&pageSize=1", timeout=15)
    except Exception:  # noqa: BLE001
        return None
    results = ((meta or {}).get("resultList") or {}).get("result") or []
    if not results:
        return None
    rec = results[0]
    pmcid = rec.get("pmcid") or ""
    if not pmcid and rec.get("id") and (rec.get("source") or "").upper() == "PMC":
        pmcid = "PMC" + str(rec["id"])
    if not pmcid:
        return None
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    try:
        req = _rq.Request(url, headers={"User-Agent": "DOIFinder/2.1"})
        with _rq.urlopen(req, timeout=25) as r:
            data = r.read()
    except Exception:  # noqa: BLE001
        return None
    return data if b"<article" in data[:4000] else None


def europepmc_candidates(doi: str) -> list[dict]:
    """Europe PMC: PMC-rendered PDF first (steadier than getPdf), then listed OA full texts."""
    q = urllib.parse.quote(f'DOI:"{doi}"')
    data = get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                    f"?query={q}&format=json&resultType=core&pageSize=1", timeout=15, tries=2)
    out = []
    for res in data.get("resultList", {}).get("result", []) or []:
        if _norm_doi(res.get("doi") or "") not in ("", _norm_doi(doi)):
            continue
        pmcid = res.get("pmcid")
        if pmcid and res.get("inEPMC") != "N":
            out.append(_loc(f"https://europepmc.org/articles/{pmcid}?pdf=render",
                            "Europe PMC", "published", "", "PMC"))
        for item in (res.get("fullTextUrlList") or {}).get("fullTextUrl", []) or []:
            if (item.get("documentStyle") or "").lower() != "pdf":
                continue
            if item.get("availabilityCode") in ("OA", "F") and item.get("url"):
                out.append(_loc(item.get("url"), "Europe PMC", "", "", item.get("site") or ""))
    return [x for x in out if x]



def semanticscholar_candidates(doi: str) -> list[dict]:
    """Semantic Scholar aggregates repository copies that Unpaywall sometimes misses."""
    data = get_json("https://api.semanticscholar.org/graph/v1/paper/DOI:"
                    + urllib.parse.quote(doi) + "?fields=openAccessPdf,title", timeout=15, tries=2)
    pdf = (data.get("openAccessPdf") or {}).get("url")
    return [_loc(pdf, "Semantic Scholar", "repository")] if pdf else []


def pmc_candidates(doi: str, mailto: str = "") -> list[dict]:
    """PubMed Central copy, via the NCBI id converter."""
    data = get_json("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?format=json&ids="
                    + urllib.parse.quote(doi), mailto, timeout=15, tries=2)
    out = []
    for rec in data.get("records", []) or []:
        pmcid = rec.get("pmcid")
        if pmcid:
            out.append(_loc(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/",
                            "PubMed Central", "published"))
    return [x for x in out if x]


def osti_candidates(doi: str) -> list[dict]:
    """US DOE OSTI accepted manuscripts — useful for computational chemistry/physics."""
    data = get_json("https://www.osti.gov/api/v1/records?doi=" + urllib.parse.quote(doi),
                    timeout=8, tries=1)
    out = []
    for rec in data if isinstance(data, list) else []:
        if _norm_doi(rec.get("doi") or "") not in ("", _norm_doi(doi)):
            continue
        for link in rec.get("links") or []:
            if link.get("rel") == "fulltext" and link.get("href"):
                out.append(_loc(link["href"], "OSTI", "accepted"))
        if rec.get("osti_id") and not out:
            out.append(_loc(f"https://www.osti.gov/servlets/purl/{rec['osti_id']}", "OSTI", "accepted"))
    return [x for x in out if x]


def zenodo_candidates(doi: str) -> list[dict]:
    q = urllib.parse.quote(f'doi:"{doi}" OR related.identifier:"{doi}"')
    data = get_json(f"https://zenodo.org/api/records?q={q}&size=5&sort=bestmatch",
                    timeout=12, tries=1)
    out = []
    for hit in ((data.get("hits") or {}).get("hits") or []):
        ids = {_norm_doi(hit.get("doi")).lower()}
        for rel in (hit.get("metadata") or {}).get("related_identifiers") or []:
            ids.add(_norm_doi(rel.get("identifier")).lower())
        if _norm_doi(doi).lower() not in ids:
            continue
        for f in hit.get("files") or []:
            if (f.get("key") or "").lower().endswith(".pdf") and (f.get("links") or {}).get("self"):
                out.append(_loc(f["links"]["self"], "Zenodo", "repository"))
    return [x for x in out if x]


def hal_candidates(doi: str) -> list[dict]:
    """HAL, the French open archive."""
    q = urllib.parse.quote(f'doiId_s:"{doi}"')
    data = get_json("https://api.archives-ouvertes.fr/search/?q=" + q
                    + "&fl=fileMain_s,uri_s&wt=json&rows=3", timeout=12, tries=1)
    out = []
    for doc in ((data.get("response") or {}).get("docs") or []):
        if doc.get("fileMain_s"):
            out.append(_loc(doc["fileMain_s"], "HAL", "published"))
    return [x for x in out if x]


def core_candidates(doi: str) -> list[dict]:
    """CORE (core.ac.uk): the largest aggregator of repository full texts. Needs a free API key."""
    import netpolicy
    key = netpolicy.core_key()
    if not key:
        return []
    q = urllib.parse.quote(f'doi:"{_norm_doi(doi)}"')
    data = get_json(f"https://api.core.ac.uk/v3/search/works?q={q}&limit=3", timeout=15, tries=1,
                    headers={"Authorization": f"Bearer {key}"})
    want = _norm_doi(doi).lower()
    out = []
    for item in data.get("results") or []:
        got = {_norm_doi(item.get("doi")).lower()}
        for ident in item.get("identifiers") or []:
            if isinstance(ident, str) and ident.lower().startswith("doi:"):
                got.add(_norm_doi(ident[4:]).lower())
        if want not in got:
            continue            # CORE's text search is fuzzy: only accept an exact DOI match
        out.append(_loc(item.get("downloadUrl"), "CORE", "repository"))
        for u in item.get("sourceFulltextUrls") or []:
            if str(u).lower().endswith(".pdf"):
                out.append(_loc(u, "CORE", "repository"))
    return [x for x in out if x]


def internet_archive_candidates(doi: str) -> list[dict]:
    """Internet Archive Scholar (fatcat catalogue): archived copies of open-access PDFs."""
    d = urllib.parse.quote(_norm_doi(doi), safe="")
    data = get_json(f"https://api.fatcat.wiki/v0/release/lookup?doi={d}&expand=files&hide=abstracts,refs",
                    timeout=15, tries=1)
    out, rank = [], {"webarchive": 0, "archive": 1, "repository": 2}
    for f in data.get("files") or []:
        if (f.get("mimetype") or "application/pdf") != "application/pdf":
            continue
        urls = [u for u in (f.get("urls") or []) if u.get("url") and u.get("rel") in rank]
        urls.sort(key=lambda u: rank[u["rel"]])
        for u in urls[:2]:
            out.append(_loc(u["url"], "Internet Archive", "archived"))
    return [x for x in out if x]


def doaj_candidates(doi: str) -> list[dict]:
    data = get_json("https://doaj.org/api/v3/search/articles/"
                    + urllib.parse.quote(_norm_doi(doi), safe="") + "?pageSize=3", timeout=12, tries=1)
    out = []
    for item in data.get("results") or []:
        for link in (item.get("bibjson") or {}).get("link") or []:
            url = link.get("url")
            if not url:
                continue
            if link.get("type") in ("fulltext", "pdf") or url.lower().endswith(".pdf"):
                out.append(_loc(url, "DOAJ", "published", "", "DOAJ"))
    return [x for x in out if x]


def crossref_oa_links(doi: str, mailto: str = "") -> list[dict]:
    """Crossref full-text links, but only when the record carries an open licence."""
    data = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}", mailto, tries=1)
    msg = data.get("message") or {}
    lic = [(x.get("URL") or "").lower() for x in msg.get("license") or []]
    if not any(("creativecommons" in u or "open-access" in u or "openaccess" in u) for u in lic):
        return []
    out = []
    for link in msg.get("link") or []:
        url = link.get("URL")
        ctype = (link.get("content-type") or "").lower()
        if url and ("pdf" in ctype or url.lower().split("?")[0].endswith(".pdf")):
            out.append(_loc(url, "Crossref", "published"))
    return [x for x in out if x]


def biorxiv_candidates(doi: str) -> list[dict]:
    doi = _norm_doi(doi)
    if not doi.startswith("10.1101/"):
        return []
    return [_loc(f"https://www.biorxiv.org/content/{doi}.full.pdf", "bioRxiv", "preprint"),
            _loc(f"https://www.medrxiv.org/content/{doi}.full.pdf", "medRxiv", "preprint")]


def elife_candidate(doi: str) -> list[dict]:
    m = re.match(r"10\.7554/elife\.(\d+)", _norm_doi(doi), re.I)
    if not m:
        return []
    return [_loc(f"https://elifesciences.org/articles/{m.group(1)}/pdf", "eLife", "published")]


_PLOS_JOURNALS = {
    "pone": "plosone", "pmed": "plosmedicine", "pbio": "plosbiology", "pgen": "plosgenetics",
    "pcbi": "ploscompbiol", "ppat": "plospathogens", "pntd": "plosntds", "pdig": "plosdigitalhealth",
    "pclm": "plosclimate", "pstr": "plossustainability", "pwat": "ploswater", "pmen": "plosmentalhealth",
    "pgph": "plosglobalpublichealth",
}
_APS_CODES = {
    "PhysRevLett.": "prl", "PhysRevB.": "prb", "PhysRevA.": "pra", "PhysRevC.": "prc",
    "PhysRevD.": "prd", "PhysRevE.": "pre", "PhysRevResearch.": "prresearch",
    "PhysRevMaterials.": "prmaterials", "PhysRevApplied.": "prapplied",
    "PhysRevFluids.": "prfluids", "PhysRevAccelBeams.": "prab", "PhysRevSTPER.": "prstper",
    "RevModPhys.": "rmp", "PRXQuantum.": "prxquantum", "PRXEnergy.": "prxenergy",
    "PhysRevX": "prx", "PhysRev.": "pr",
}


def publisher_direct(doi: str) -> list[dict]:
    """Publisher PDF URLs that follow a fixed DOI→URL pattern.

    Ported from scansci-pdf's per-publisher strategy table (templates verified
    against live publisher URLs in 2026). Free ones are attempted first; the
    paywalled ones simply fail and cost one request.
    """
    doi = _norm_doi(doi)
    out: list[dict] = []

    def add(url: str, name: str, version: str = "published") -> None:
        loc = _loc(url, name, version)
        if loc:
            out.append(loc)

    if doi.startswith("10.3389/"):
        add(f"https://www.frontiersin.org/articles/{doi}/pdf", "Frontiers")
    if doi.startswith(("10.1186/", "10.1007/", "10.1057/")):
        add(f"https://link.springer.com/content/pdf/{urllib.parse.quote(doi, safe='/')}.pdf", "Springer")
    if doi.startswith("10.1371/"):
        m = re.match(r"10\.1371/journal\.([a-z0-9]+)\.", doi, re.I)
        journal = _PLOS_JOURNALS.get((m.group(1) if m else "pone").lower(), "plosone")
        add(f"https://journals.plos.org/{journal}/article/file?id={doi}&type=printable", "PLOS")
    if doi.startswith("10.1002/"):
        add(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}", "Wiley")
    if doi.startswith("10.1021/"):
        add(f"https://pubs.acs.org/doi/pdf/{doi}?ref=article_openPDF", "ACS")
        add(f"https://pubs.acs.org/doi/epdf/{doi}", "ACS")
    if doi.startswith("10.1063/"):
        add(f"https://pubs.aip.org/doi/epdf/{doi}", "AIP")
        add(f"https://pubs.aip.org/doi/pdf/{doi}", "AIP")
    if doi.startswith("10.1088/"):
        add(f"https://iopscience.iop.org/article/{doi}/pdf", "IOP")
    if doi.startswith("10.1080/"):
        add(f"https://www.tandfonline.com/doi/pdf/{doi}", "Taylor & Francis")
    if doi.startswith("10.1177/"):
        add(f"https://journals.sagepub.com/doi/pdf/{doi}?download=true", "SAGE")
    if doi.startswith("10.1038/"):
        add(f"https://www.nature.com/articles/{doi.split('/', 1)[-1]}.pdf", "Nature")
    if doi.startswith(("10.1126/", "10.1073/")):
        add(f"https://www.science.org/doi/epdf/{doi}" if doi.startswith("10.1126/")
            else f"https://www.pnas.org/doi/epdf/{doi}", "Science" if doi.startswith("10.1126/") else "PNAS")
    if doi.startswith("10.1103/"):
        for prefix, code in _APS_CODES.items():
            if doi[len("10.1103/"):].startswith(prefix):
                add(f"https://journals.aps.org/{code}/pdf/{doi}", "APS")
                break
    if doi.startswith("10.1142/"):
        add(f"https://www.worldscientific.com/doi/pdf/{doi}", "World Scientific")
    if doi.startswith("10.1364/"):
        add(f"https://opg.optica.org/viewmedia.cfm?r=1&uri={doi}", "Optica")
    m = re.match(r"10\.5194/([a-z0-9]+)-(\d+)-(.+)-(\d{4})$", doi, re.I)
    if m:  # Copernicus: journal-volume-page-year is fully encoded in the DOI
        journal, vol, page, year = m.groups()
        suffix = doi.split("/", 1)[-1]
        add(f"https://{journal}.copernicus.org/articles/{vol}/{page}/{year}/{suffix}.pdf",
            "Copernicus")
    if doi.startswith("10.3390/"):
        # the ISSN/volume/issue path is not derivable from the DOI; the landing page is
        add(f"https://www.mdpi.com/{doi.split('/', 1)[-1]}/pdf", "MDPI")
    return out


def elsevier_candidates(doi: str, mailto: str = "") -> list[dict]:
    """Elsevier has no DOI→PDF template: Crossref carries the PII, which does."""
    doi = _norm_doi(doi)
    if not doi.startswith(("10.1016/", "10.1053/", "10.1054/")):
        return []
    try:
        data = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}",
                        mailto, timeout=12, tries=1)
    except Exception:  # noqa: BLE001
        return []
    msg = data.get("message") or {}
    pii = ""
    for alt in msg.get("alternative-id") or []:
        cand = re.sub(r"[^A-Za-z0-9]", "", str(alt)).upper()
        if re.match(r"^[A-Z0-9]{4,}$", cand) and any(ch.isdigit() for ch in cand):
            pii = cand
            break
    if not pii:
        return []
    journal = ((msg.get("container-title") or [""])[0] or "").lower()
    if journal.startswith("cell"):
        return [_loc(f"https://www.cell.com/action/showPdf?pii={pii}", "Cell Press")]
    return [_loc(f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft"
                 "?isDTMRedir=true&download=true", "Elsevier")]


def ieee_candidates(doi: str, timeout: int = 15) -> list[dict]:
    """IEEE needs the arnumber from the article URL, not from the DOI."""
    doi = _norm_doi(doi)
    if not doi.startswith("10.1109/"):
        return []
    try:
        req = urllib.request.Request(f"https://doi.org/{doi}", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl() or ""
    except Exception:  # noqa: BLE001
        return []
    m = re.search(r"/document/(\d+)", final)
    if not m:
        return []
    return [_loc("https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&isnumber=&arnumber="
                 + m.group(1), "IEEE")]




def arxiv_by_title(title: str, doi: str = "", min_similarity: float = 0.90) -> list[dict]:
    """找同一篇在 arXiv 上的预印本。

    最可靠的办法不是比标题，而是看 arXiv 条目里登记的期刊 DOI（arxiv:doi 字段）。
    作者投稿时通常会把正式版 DOI 挂上，这是精确匹配，不会认错。
    查不到 DOI 字段时，才退回高阈值（0.90）的标题比对。
    """
    import difflib
    import urllib.parse as _up
    import urllib.request as _rq
    import xml.etree.ElementTree as _ET

    q = " ".join((title or "").split())
    want = (doi or "").lower().strip()
    if len(q) < 12 and not want:
        return []
    stop = {"the", "and", "for", "with", "from", "that", "this", "using", "via", "into",
            "their", "between", "under", "over", "its", "our", "based", "new", "toward"}
    words = [w.strip(",.:;()[]") for w in q.split()]
    key = [w for w in words if len(w) > 3 and w.lower() not in stop]
    if not key:
        return []
    url = ("http://export.arxiv.org/api/query?search_query=ti:"
           + _up.quote(f'"{key[0]} {key[1]}"' if len(key) > 1 else f'"{key[0]}"')
           + "&start=0&max_results=20")
    try:
        req = _rq.Request(url, headers={"User-Agent": "DOIFinder/2.1"})
        with _rq.urlopen(req, timeout=20) as r:
            root = _ET.fromstring(r.read())
    except Exception:  # noqa: BLE001
        return []
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    out = []
    for e in root.findall("a:entry", ns):
        aid = (e.findtext("a:id", "", ns) or "").rstrip("/").split("/")[-1]
        if not aid:
            continue
        linked = (e.findtext("x:doi", "", ns) or "").lower().strip()
        t = " ".join((e.findtext("a:title", "", ns) or "").split())
        if want and linked and linked == want:
            out.append({"source": "arXiv（同一篇，DOI 对应）",
                        "url": f"https://arxiv.org/pdf/{aid}"})
            continue
        if not want and t:
            sim = difflib.SequenceMatcher(None, q.lower(), t.lower()).ratio()
            if sim >= min_similarity:
                out.append({"source": "arXiv（标题一致）",
                            "url": f"https://arxiv.org/pdf/{aid}"})
    return out[:2]


PREPRINT_PREFIXES = ("10.2139", "10.21203", "10.20944", "10.26434", "10.48550", "10.1101")


def published_version(doi: str, title: str = "", mailto: str = "") -> str:
    """预印本 DOI → 正式发表版本的 DOI。

    SSRN、Research Square 这些预印本经常不是读者要的那篇：正式版在出版社那边，
    而且可能是开放获取（Elsevier 的 open archive 就是）。用标题回 Crossref 查一遍，
    挑一个非预印本前缀、标题最像的。
    """
    import difflib
    import json as _json
    import urllib.parse as _up
    import urllib.request as _rq

    doi = (doi or "").lower()
    if not doi.startswith(PREPRINT_PREFIXES):
        return ""
    q = (title or "").strip()
    if len(q) < 15:
        return ""
    url = ("https://api.crossref.org/works?rows=8&select=DOI,title,type&query.bibliographic="
           + _up.quote(q[:300]))
    if mailto:
        url += "&mailto=" + _up.quote(mailto)
    try:
        req = _rq.Request(url, headers={"User-Agent": "DOIFinder/2.1 (mailto:%s)" % (mailto or "")})
        with _rq.urlopen(req, timeout=20) as r:
            items = (_json.loads(r.read().decode("utf-8", "replace")).get("message") or {}).get("items") or []
    except Exception:  # noqa: BLE001
        return ""
    best, best_sim = "", 0.0
    for it in items:
        d = (it.get("DOI") or "").lower()
        if not d or d == doi or d.startswith(PREPRINT_PREFIXES):
            continue
        t = " ".join((it.get("title") or [""])[0].split()).lower()
        sim = difflib.SequenceMatcher(None, q.lower(), t).ratio()
        if sim > best_sim:
            best, best_sim = d, sim
    return best if best_sim >= 0.90 else ""


def s2_pdf_url(doi: str, timeout: int = 20) -> list[dict]:
    """Semantic Scholar 的开放 PDF 直链。它镜像了很多出版社的 PDF，且不挡爬虫。

    单独放在这里做一次带重试的补查：并行查询里它常常 15 秒不回就被丢掉了，
    而出版社自己的链接（MDPI 尤其明显）对爬虫时好时坏。
    """
    import json as _json
    import time as _time
    import urllib.request as _rq
    url = (f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
           "?fields=openAccessPdf,title,externalIds")
    for i in range(3):
        try:
            req = _rq.Request(url, headers={"User-Agent": "DOIFinder/2.1"})
            with _rq.urlopen(req, timeout=timeout) as r:
                d = _json.loads(r.read().decode("utf-8", "replace"))
            pdf = ((d.get("openAccessPdf") or {}).get("url") or "").strip()
            if pdf:
                return [{"source": "Semantic Scholar", "url": pdf}]
            return []
        except Exception:  # noqa: BLE001
            _time.sleep(1.5 * (i + 1))
    return []


# ---------------------------------------------------------------- OA 结果缓存
_OA_CACHE: dict | None = None
_OA_TTL = 24 * 3600
_OA_TTL_EMPTY = 6 * 3600
_OA_LOCK = threading.RLock()
_OA_LAST_SAVE = 0.0


def _oa_cache_path() -> str:
    base = os.environ.get("DOIFINDER_DATA") or platform_util.data_dir()
    return os.path.join(base, "oa_cache.json")


def _oa_cache_load() -> dict:
    import json as _json
    global _OA_CACHE
    if _OA_CACHE is not None:
        return _OA_CACHE
    try:
        with open(_oa_cache_path(), encoding="utf-8") as f:
            _OA_CACHE = _json.load(f)
    except Exception:  # noqa: BLE001
        _OA_CACHE = {}
    return _OA_CACHE


def _oa_cache_save(force: bool = False) -> None:
    """Write the cache; debounced to once per 5 s so a big batch isn't O(n^2) I/O."""
    import json as _json
    global _OA_LAST_SAVE
    now = time.time()
    if not force and now - _OA_LAST_SAVE < 5:
        return
    with _OA_LOCK:
        _OA_LAST_SAVE = now
        data = dict(_oa_cache_load())
    try:
        path = _oa_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


import atexit as _atexit
_atexit.register(lambda: _OA_CACHE is not None and _oa_cache_save(force=True))


def resolve_oa(doi: str, email: str = "", diag: list | None = None) -> list[dict]:
    """Return ordered, de-duplicated open-access candidates for a DOI.

    Deterministic URLs (arXiv/bioRxiv/eLife) are known without a request; the
    metadata services are queried in parallel with one shared deadline, and the
    results are still concatenated in a fixed priority order. A dead or slow
    source therefore costs latency only, never the whole lookup.
    """
    doi = _norm_doi(doi)
    cache = _oa_cache_load()
    hit = cache.get(doi)
    if hit:
        age = time.time() - float(hit.get("at") or 0)
        ttl = _OA_TTL if hit.get("cands") else _OA_TTL_EMPTY
        if age < ttl:
            if diag is not None:
                diag.append("开放获取:缓存命中")
            return list(hit.get("cands") or [])
    cands: list[dict] = []
    for fn in (lambda: arxiv_candidate(doi), lambda: biorxiv_candidates(doi),
               lambda: elife_candidate(doi)):
        try:
            cands.extend(fn())
        except Exception:  # noqa: BLE001
            continue

    tasks = [
        ("OpenAlex", lambda: openalex_candidates(doi, email)),
        ("Unpaywall", lambda: unpaywall_candidates(doi, email)),
        ("Europe PMC", lambda: europepmc_candidates(doi)),
        ("Semantic Scholar", lambda: semanticscholar_candidates(doi)),
        ("PubMed Central", lambda: pmc_candidates(doi, email)),
        ("OSTI", lambda: osti_candidates(doi)),
        ("Zenodo", lambda: zenodo_candidates(doi)),
        ("HAL", lambda: hal_candidates(doi)),
        ("Crossref", lambda: crossref_oa_links(doi, email)),
        ("DOAJ", lambda: doaj_candidates(doi)),
        ("CORE", lambda: core_candidates(doi)),
        ("Internet Archive", lambda: internet_archive_candidates(doi)),
        ("Elsevier", lambda: elsevier_candidates(doi, email)),
        ("IEEE", lambda: ieee_candidates(doi)),
    ]
    results = _run_parallel(tasks, deadline=25.0)
    for i, (label, _fn) in enumerate(tasks):
        got = results.get(i)
        if got is None:
            if diag is not None:
                diag.append(f"{label}:超时")
            continue
        if not got and diag is not None:
            diag.append(f"{label}:无")
        cands.extend(got)

    # 出版社页面常挡爬虫（MDPI 就时好时坏），把直链镜像提到前面
    def _rank(c: dict) -> int:
        u = (c.get("url") or "").lower()
        if any(h in u for h in ("pdfs.semanticscholar.org", "arxiv.org", "biorxiv.org",
                                "europepmc.org", "ncbi.nlm.nih.gov")):
            return 0
        if u.endswith(".pdf") or "/pdf" in u:
            return 1
        return 2

    cands.sort(key=_rank)

    # 一个直链镜像都没有时，单独再问一次 Semantic Scholar
    if not any("semanticscholar.org" in (c.get("url") or "") for c in cands):
        try:
            extra = s2_pdf_url(doi)
        except Exception:  # noqa: BLE001
            extra = []
        if extra:
            cands = extra + cands
            if diag is not None:
                diag.append("Semantic Scholar:补查命中")

    cands.extend(publisher_direct(doi))
    # publisher landing pages last: they are HTML, but carry the PDF link
    try:
        cands.extend(landing_candidates(doi, email))
    except Exception:  # noqa: BLE001
        pass

    seen, out = set(), []
    for c in cands:
        key = c["url"].split("?")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    try:
        with _OA_LOCK:
            cache[doi] = {"at": time.time(), "cands": out[:20]}
            if len(cache) > 4000:
                for k in sorted(cache, key=lambda k: cache[k].get("at") or 0)[:1000]:
                    cache.pop(k, None)
        _oa_cache_save()
    except Exception:  # noqa: BLE001
        pass

    return out


def _run_parallel(tasks: list, deadline: float = 35.0) -> dict:
    """Run (label, callable) tasks on daemon threads; return {index: result}.

    Threads that overrun the deadline are abandoned, not killed — every call has
    its own socket timeout, so they die on their own without blocking exit.
    """
    import threading

    results: dict[int, list] = {}
    lock = threading.Lock()

    def run(i: int, fn) -> None:
        try:
            value = fn()
        except Exception:  # noqa: BLE001
            return
        with lock:
            results[i] = value

    threads = []
    for i, (_label, fn) in enumerate(tasks):
        t = threading.Thread(target=run, args=(i, fn), daemon=True)
        t.start()
        threads.append(t)
    end = time.time() + deadline
    for t in threads:
        t.join(max(0.0, end - time.time()))
    return dict(results)


def pmc_pow_pdf(doi: str, email: str = "", timeout: int = 40) -> bytes | None:
    """PMC full text behind a SHA-256 proof-of-work cookie (same approach as the author's AuthorSciHub tool).

    PubMed Central now wraps the PDF endpoint in a PoW gate; solving it takes a
    few milliseconds, so biomedical papers stay reachable without a browser.
    """
    import hashlib
    import http.cookiejar

    doi = _norm_doi(doi)
    pmcid = ""
    try:
        data = get_json("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?format=json&ids="
                        + urllib.parse.quote(doi), email, timeout=12, tries=1)
        for rec in data.get("records", []) or []:
            if rec.get("pmcid"):
                pmcid = rec["pmcid"]
                break
    except Exception:  # noqa: BLE001
        return None
    if not pmcid:
        try:
            s2 = get_json("https://api.semanticscholar.org/graph/v1/paper/DOI:"
                          + urllib.parse.quote(doi) + "?fields=externalIds", timeout=12, tries=1)
            pmcid = ((s2.get("externalIds") or {}).get("PubMedCentral") or "")
            pmcid = f"PMC{pmcid}" if pmcid and not str(pmcid).startswith("PMC") else pmcid
        except Exception:  # noqa: BLE001
            return None
    if not pmcid:
        return None

    url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": _ua(email), "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as r:
            body = r.read()
        if body[:5].startswith(b"%PDF"):
            return body
        text = body.decode("utf-8", "replace")
        m_ch = re.search(r'POW_CHALLENGE\s*=\s*"([^"]+)"', text)
        if not m_ch:
            return None
        m_df = re.search(r'POW_DIFFICULTY\s*=\s*"(\d+)"', text)
        difficulty = int(m_df.group(1)) if m_df else 4
        target = "0" * difficulty
        challenge = m_ch.group(1)
        nonce = 0
        while nonce < 20_000_000:
            if hashlib.sha256((challenge + str(nonce)).encode()).hexdigest().startswith(target):
                break
            nonce += 1
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name="cloudpmc-viewer-pow", value=f"{challenge},{nonce}",
            port=None, port_specified=False, domain=".ncbi.nlm.nih.gov", domain_specified=True,
            domain_initial_dot=True, path="/", path_specified=True, secure=True,
            expires=None, discard=False, comment=None, comment_url=None, rest={}))
        req2 = urllib.request.Request(url, headers=headers)
        with opener.open(req2, timeout=timeout) as r2:
            body2 = r2.read()
        return body2 if body2[:5].startswith(b"%PDF") else None
    except Exception:  # noqa: BLE001
        return None



def landing_candidates(doi: str, mailto: str = "") -> list[dict]:
    """The publisher article page (from CrossRef) and the DOI resolver itself."""
    out = []
    try:
        meta = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}", mailto, tries=1)
        url = (meta.get("message") or {}).get("URL")
        if url:
            out.append(_loc(url, "publisher", "", "", "landing"))
    except Exception:  # noqa: BLE001
        pass
    out.append(_loc(f"https://doi.org/{doi}", "doi.org", "", "", "resolver"))
    return [x for x in out if x]


# --------------------------------------------------------------------------- HTML → PDF
_META_PDF = re.compile(
    r"<meta\b[^>]*(?:name|property|itemprop)\s*=\s*['\"][^'\"]*pdf_url['\"][^>]*>", re.I)
_META_CONTENT = re.compile(r"content\s*=\s*['\"]([^'\"]+)", re.I)
# supporting information only — "article.pdf"/"main.pdf" must survive
_SUPPLEMENT = re.compile(
    r"(?:^|[/_.-])(?:supp|suppl|supplement|supplementary|supporting|appendix|"
    r"mmc\d*|moesm\d*|cms|attachment|suppl_file|suppdata|pb-assets)(?:[/_.-]|$)|"
    r"(?:^|[/_.-])s\d{1,3}(?:[/_.-]|$)", re.I)
_MAIN_PDF = re.compile(r"(?:^|/)(?:main|mainext|article)\.pdf$", re.I)
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = lambda name: re.compile(name + r"\s*=\s*['\"]([^'\"]+)", re.I)  # noqa: E731
_IFRAME = re.compile(
    r"<(?:iframe|embed|object)\b[^>]*(?:src|data|href)\s*=\s*['\"]([^'\"]+)", re.I)
_ANCHOR = re.compile(r"<a\b[^>]*href\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.{0,160}?)</a>", re.I | re.S)
# pdf.js viewers (Wiley/OUP/T&F) keep the real file only in this call
_PDFJS = re.compile(r"PDFViewerApplicationOptions\s*\.\s*set\s*\(\s*['\"]defaultUrl['\"]\s*,\s*"
                    r"['\"]([^'\"]+)", re.I)
_QUERY_PDF = re.compile(r"[?&](?:file|pdf|src|url|download)=([^&\"'>]+)", re.I)


def pdf_links_from_html(page_url: str, body: bytes, limit: int = 6) -> list[str]:
    """Extract article-specific PDF links from a landing page, in priority order.

    Signals, in order: any meta tag whose name ends in pdf_url, <link
    type=application/pdf>, pdf.js defaultUrl, pdf iframes/objects, hrefs shaped
    like a PDF, then anchors whose href or label says pdf. Supporting-information
    links are dropped so the article itself wins.
    """
    import html as _html
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")

    def absolute(href: str) -> str:
        href = _html.unescape(href).replace("\\/", "/").strip()
        if href.startswith("//"):
            href = "https:" + href
        return urllib.parse.urljoin(page_url, href)

    found: list[str] = []

    for tag in _META_PDF.findall(text):
        m = _META_CONTENT.search(tag)
        if m:
            found.append(absolute(m.group(1)))

    for tag in _LINK_TAG.findall(text):
        rel = _ATTR("rel").search(tag)
        typ = _ATTR("type").search(tag)
        href = _ATTR("href").search(tag)
        if not href:
            continue
        is_pdf_type = bool(typ and "application/pdf" in typ.group(1).lower())
        is_alternate = bool(rel and "alternate" in rel.group(1).lower())
        if is_pdf_type or (is_alternate and is_pdf_type):
            found.append(absolute(href.group(1)))

    m = _PDFJS.search(text)
    if m:
        found.append(absolute(m.group(1)))

    for m in _IFRAME.finditer(text):
        src = absolute(m.group(1))
        if "pdf" in src.lower():
            found.append(src)

    # J-STAGE serves PDFs under .../_pdf ; CiNii and repositories often link .../...pdf
    for m in re.finditer(r"href\s*=\s*['\"]([^'\"]*(?:_pdf|/pdf/|\.pdf)[^'\"]*)", text, re.I):
        found.append(absolute(m.group(1)))

    for m in _ANCHOR.finditer(text):
        href, label = m.group(1), m.group(2)
        if "pdf" in href.lower() or "pdf" in re.sub(r"<[^>]+>", " ", label).lower():
            url = absolute(href)
            if not url.lower().startswith(("javascript:", "mailto:")):
                found.append(url)

    # a viewer URL often hides the file behind ?file=/pdf=/?url=
    for u in list(found):
        q = _QUERY_PDF.search(u)
        if q:
            inner = absolute(urllib.parse.unquote(q.group(1)))
            if "pdf" in inner.lower():
                found.insert(0, inner)

    seen, out = set(), []
    for u in found:
        if not u.startswith("http") or u in seen:
            continue
        if u.lower().endswith((".css", ".js", ".png", ".jpg", ".gif")):
            continue
        path = urllib.parse.urlparse(u).path
        if _SUPPLEMENT.search(path) and not _MAIN_PDF.search(path):
            continue  # supporting information is not the article
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- metadata
def crossref_meta(doi: str, mailto: str = "") -> dict:
    doi = _norm_doi(doi)
    data = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}", mailto, tries=2)
    m = data.get("message", {})
    authors = m.get("author") or []
    first = ""
    if authors:
        a = authors[0]
        first = a.get("family") or a.get("name") or ""
    year = ""
    for key in ("published-print", "published-online", "issued", "created"):
        try:
            year = str(m[key]["date-parts"][0][0])
            break
        except Exception:  # noqa: BLE001
            continue
    return {"doi": doi, "title": (m.get("title") or [""])[0], "year": year,
            "first_author": first,
            "journal": (m.get("container-title") or [""])[0],
            "volume": m.get("volume") or "", "page": m.get("page") or ""}


def openalex_meta(doi: str, mailto: str = "") -> dict:
    """Fallback metadata for records CrossRef does not carry (arXiv preprints, books)."""
    doi = _norm_doi(doi)
    data = get_json("https://api.openalex.org/works/https://doi.org/" + urllib.parse.quote(doi),
                    mailto, tries=2)
    auths = data.get("authorships") or []
    first = ""
    if auths:
        name = ((auths[0].get("author") or {}).get("display_name") or "")
        first = name.split()[-1] if name else ""
    loc = data.get("primary_location") or {}
    return {"doi": doi, "title": data.get("title") or "",
            "year": str(data.get("publication_year") or ""),
            "first_author": first,
            "journal": ((loc.get("source") or {}) or {}).get("display_name") or "",
            "volume": "", "page": ""}


_META_CACHE: dict[str, dict] = {}
_META_CACHE_PATH = ""
_META_TTL = 30 * 86400
_META_LOCK = threading.Lock()


def configure_meta_cache(path: str, ttl: int = 30 * 86400) -> None:
    """Remember DOI→metadata on disk: re-running a batch must not re-hit Crossref."""
    global _META_CACHE_PATH, _META_TTL
    _META_CACHE_PATH = path or ""
    _META_TTL = ttl
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        with _META_LOCK:
            _META_CACHE.update(data)
    except Exception:  # noqa: BLE001
        pass


def flush_meta_cache() -> None:
    if not _META_CACHE_PATH:
        return
    try:
        os.makedirs(os.path.dirname(_META_CACHE_PATH), exist_ok=True)
        tmp = _META_CACHE_PATH + ".tmp"
        with _META_LOCK:
            payload = dict(_META_CACHE)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, _META_CACHE_PATH)
    except Exception:  # noqa: BLE001
        pass


def work_meta(doi: str, mailto: str = "") -> dict:
    """CrossRef first, then OpenAlex; whichever answers with a title wins.

    Successful lookups are cached on disk so a repeated batch is instant and
    does not burn Crossref/OpenAlex quota.
    """
    doi = _norm_doi(doi)
    now = time.time()
    with _META_LOCK:
        hit = _META_CACHE.get(doi.lower())
    if hit and hit.get("meta") and now - hit.get("ts", 0) < _META_TTL:
        return dict(hit["meta"])

    meta = {}
    try:
        meta = crossref_meta(doi, mailto)
    except Exception:  # noqa: BLE001
        meta = {"doi": doi, "title": "", "year": "", "first_author": "", "journal": ""}
    if not meta.get("title"):
        try:
            alt = openalex_meta(doi, mailto)
            for k, v in alt.items():
                if v and not meta.get(k):
                    meta[k] = v
        except Exception:  # noqa: BLE001
            pass
    if meta.get("title"):
        with _META_LOCK:
            _META_CACHE[doi.lower()] = {"ts": now, "meta": meta}
    return meta


# --------------------------------------------------------------------------- topic search
def resolve_author(name: str, mailto: str = "") -> str | None:
    data = get_json("https://api.openalex.org/authors?per-page=1&search="
                    + urllib.parse.quote(name), mailto, tries=2)
    for a in data.get("results", []) or []:
        return (a.get("id") or "").rsplit("/", 1)[-1]
    return None


# --------------------------------------------------------------------------- ORCID
def orcid_search(name: str, rows: int = 10, mailto: str = "") -> list[dict]:
    """Name → candidate ORCID iDs.

    OpenAlex first (it carries ORCID iDs plus a work count, which makes picking the
    right person easy), then the ORCID registry as a fallback — a record whose
    family name is private only shows up in the registry's own index.
    """
    name = (name or "").strip()
    if not name:
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def add(orcid: str, display: str, institution: str = "", works: int = 0, src: str = "") -> None:
        orcid = re.sub(r"^https?://orcid\.org/", "", (orcid or "").strip())
        if not re.match(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", orcid, re.I) or orcid in seen:
            return
        seen.add(orcid)
        out.append({"orcid": orcid, "name": display, "institution": institution,
                    "works": works, "source": src})

    try:
        data = get_json("https://api.openalex.org/authors?per-page=8&search="
                        + urllib.parse.quote(name), mailto, timeout=12, tries=1)
        for a in data.get("results") or []:
            inst = ""
            for key in ("last_known_institutions", "last_known_institution", "affiliations"):
                val = a.get(key)
                if isinstance(val, list) and val:
                    inst = (val[0] or {}).get("display_name") or (val[0] or {}).get("institution", {}).get("display_name", "")
                    break
                if isinstance(val, dict):
                    inst = val.get("display_name") or ""
                    break
            add(a.get("orcid") or "", a.get("display_name") or "",
                inst, int(a.get("works_count") or 0), "OpenAlex")
    except Exception:  # noqa: BLE001 - OpenAlex throttles; the registry still answers
        pass

    try:
        data = get_json("https://pub.orcid.org/v3.0/expanded-search/?q="
                        + urllib.parse.quote(name) + f"&rows={max(1, min(rows, 25))}",
                        timeout=12, tries=1)
        for rec in data.get("expanded-result") or []:
            add(rec.get("orcid-id") or "",
                " ".join(x for x in (rec.get("given-names"), rec.get("family-names")) if x),
                ", ".join((rec.get("institution-name") or [])[:2]), 0, "ORCID")
    except Exception:  # noqa: BLE001
        pass

    # ORCID's free-text search is fuzzy: it happily ignores the given name. Query the
    # surname alone and keep only records whose given name actually matches.
    parts = name.split()
    if len(parts) >= 2:
        given = parts[0].lower()
        family = parts[-1]
        try:
            data = get_json("https://pub.orcid.org/v3.0/expanded-search/?q=family-name:"
                            + urllib.parse.quote(family) + "&rows=50", timeout=12, tries=1)
            for rec in data.get("expanded-result") or []:
                g = (rec.get("given-names") or "").lower()
                if not g:
                    continue
                if not (g.startswith(given) or given.startswith(g.split()[0])):
                    continue
                add(rec.get("orcid-id") or "",
                    " ".join(x for x in (rec.get("given-names"), rec.get("family-names")) if x),
                    ", ".join((rec.get("institution-name") or [])[:2]), 0, "ORCID")
        except Exception:  # noqa: BLE001
            pass

    return out[:max(1, rows)]




def orcid_public_works(orcid: str, timeout: int = 30) -> list[dict]:
    """直接用 ORCID 官方公开接口取作品，绕开 OpenAlex 的限流。

    这正是作者自己在 ORCID 上登记的那份清单，条数与网页一致。
    返回结构与 orcid_works 相同，调用方不用区分。
    """
    orcid = re.sub(r"^https?://orcid\.org/", "", (orcid or "").strip())
    url = f"https://pub.orcid.org/v3.0/{orcid}/works"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "DOIFinder/2.1 (local desktop app)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for grp in data.get("group") or []:
        summaries = grp.get("work-summary") or []
        if not summaries:
            continue
        s = summaries[0]
        title = ((s.get("title") or {}).get("title") or {}).get("value") or ""
        year = str(((s.get("publication-date") or {}).get("year") or {}).get("value") or "")
        doi = ""
        for eid in ((s.get("external-ids") or {}).get("external-id") or []):
            if (eid.get("external-id-type") or "").lower() == "doi":
                doi = (eid.get("external-id-value") or "").lower()
                break
        out.append({
            "doi": doi, "title": title, "year": year, "authors": "",
            "venue": (s.get("journal-title") or {}).get("value") or "",
            "url": f"https://orcid.org/{orcid}", "oa": "", "pdf_url": "",
            "cited": 0, "source": "ORCID",
        })
    return out


def orcid_works(orcid: str, limit: int = 50, mailto: str = "",
                year_from: str = "", year_to: str = "", oa_only: bool = False) -> list[dict]:
    """Works of one ORCID iD, resolved through OpenAlex's author.orcid filter."""
    orcid = re.sub(r"^https?://orcid\.org/", "", (orcid or "").strip())
    if not re.match(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", orcid, re.I):
        raise RuntimeError("ORCID iD 格式不正确（应为 0000-0000-0000-0000）")
    params = {
        "filter": f"author.orcid:{orcid}",
        "per-page": str(min(max(limit, 1), 200)),
        "select": "id,doi,title,publication_year,authorships,primary_location,open_access,cited_by_count,type",
        "sort": "publication_year:desc",
    }
    filters = [f"author.orcid:{orcid}"]
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if oa_only:
        filters.append("is_oa:true")
    params["filter"] = ",".join(filters)
    # OpenAlex 单页最多 200 条。要拿全就得翻页，否则 230 篇的作者只能看到 200 篇。
    results: list[dict] = []
    cursor = "*"
    page = 0
    while cursor and page < 12:
        page += 1
        params["per-page"] = str(min(max(limit - len(results), 1), 200))
        params["cursor"] = cursor
        try:
            data = get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params), mailto)
        except Exception:  # noqa: BLE001 - OpenAlex throttles; Crossref and the registry still answer
            break
        chunk = data.get("results") or []
        if not chunk and page > 1:
            # 第二页开始空返回，多半是被限流，歇一下重试一次再放弃
            time.sleep(1.2)
            try:
                data = get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params), mailto)
                chunk = data.get("results") or []
            except Exception:  # noqa: BLE001
                break
        results.extend(chunk)
        cursor = ((data.get("meta") or {}).get("next_cursor") or "")
        if len(results) >= limit or not chunk:
            break
        time.sleep(0.35)  # 连发两页容易触发 429
    data = {"results": results[:limit]}
    out = []
    for w in data.get("results", []) or []:
        auths = [((a.get("author") or {}).get("display_name") or "")
                 for a in (w.get("authorships") or [])[:6]]
        loc = w.get("primary_location") or {}
        oa = w.get("open_access") or {}
        best = (w.get("best_oa_location") if "best_oa_location" in w else None) or {}
        out.append({
            "doi": _norm_doi(w.get("doi") or ""),
            "title": w.get("title") or "",
            "year": w.get("publication_year") or "",
            "journal": ((loc.get("source") or {}) or {}).get("display_name") or "",
            "authors": auths,
            "cited": w.get("cited_by_count") or 0,
            "oa": bool(oa.get("is_oa")),
            "oa_url": oa.get("oa_url") or best.get("pdf_url") or best.get("landing_page_url") or "",
            "type": w.get("type") or "",
        })
    if len(out) < limit:
        # Crossref also indexes ORCIDs and has no daily quota: fill the gaps and
        # rescue the case where OpenAlex is throttled or has no author link.
        try:
            extra = crossref_orcid_works(orcid, limit, mailto, year_from, year_to)
        except Exception:  # noqa: BLE001
            extra = []
        have = {(_norm_doi(r.get("doi") or "")).lower() for r in out}
        for row in extra:
            key = (row.get("doi") or "").lower()
            if key and key not in have:
                have.add(key)
                out.append(row)
            if len(out) >= limit:
                break
    if not out and not oa_only:
        out = _orcid_works_direct(orcid, limit, year_from, year_to)
    return out[:limit]


def crossref_orcid_works(orcid: str, limit: int = 50, mailto: str = "",
                         year_from: str = "", year_to: str = "") -> list[dict]:
    """Works linked to an ORCID iD in Crossref — the fallback when OpenAlex throttles."""
    orcid = re.sub(r"^https?://orcid\.org/", "", (orcid or "").strip())
    params = {
        "filter": f"orcid:{orcid}",
        "rows": str(min(max(limit, 1), 200)),
        "select": "DOI,title,author,issued,container-title,is-referenced-by-count,type",
        "sort": "published",
        "order": "desc",
    }
    filters = [f"orcid:{orcid}"]
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")
    params["filter"] = ",".join(filters)
    data = get_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params),
                    mailto, timeout=15, tries=2)
    out = []
    for it in (data.get("message") or {}).get("items") or []:
        doi = _norm_doi(it.get("DOI") or "")
        if not doi or (it.get("type") or "") == "component" or re.search(r"\.s\d+$", doi):
            continue
        parts = ((it.get("issued") or {}).get("date-parts") or [[]])[0]
        auths = []
        for a in (it.get("author") or [])[:6]:
            name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name") or ""
            if name:
                auths.append(name)
        out.append({
            "doi": doi,
            "title": _norm_ws((it.get("title") or [""])[0]) if it.get("title") else "",
            "year": parts[0] if parts else "",
            "journal": _norm_ws((it.get("container-title") or [""])[0]) if it.get("container-title") else "",
            "authors": auths,
            "cited": it.get("is-referenced-by-count") or 0,
            "oa": False,
            "oa_url": "",
            "type": it.get("type") or "",
        })
    return out
_BAIDU_CLEAN = re.compile(r"[\u0001-\u0008\ufffd]|<em>|</em>|查看全部>>|&nbsp;", re.I)


def _norm_ws(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _clean_text(v) -> str:
    if not isinstance(v, str):
        return ""
    return re.sub(r"\s+", " ", _BAIDU_CLEAN.sub(" ", v)).strip()


def _deep_get(obj, keys: tuple, depth: int = 0):
    """Depth-first search for the first scalar under any of `keys`."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and not isinstance(obj[k], (dict, list)) and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            got = _deep_get(v, keys, depth + 1)
            if got not in (None, ""):
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _deep_get(v, keys, depth + 1)
            if got not in (None, ""):
                return got
    return None


def _path_get(obj, path: list):
    """Walk an exact key path; returns None as soon as a step is missing."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and key < len(cur):
            cur = cur[key]
        else:
            return None
        if cur is None:
            return None
    return cur


def _join_scalar(v) -> str:
    if isinstance(v, list):
        return ", ".join(_clean_text(x if not isinstance(x, dict) else x.get("value", "")) for x in v)
    if isinstance(v, dict):
        return _clean_text(v.get("value") or v.get("name") or "")
    return _clean_text(v)


def baidu_paper(paper_id: str) -> dict:
    """Baidu Scholar's detail endpoint — the one Chinese source that answers anonymously.

    Baidu's *search* API rejects anything without a Chrome TLS fingerprint, but
    /scholarai/paper/detail/info returns plain JSON (verified 2026-11). It is an
    undocumented endpoint, so everything here is defensive.
    """
    paper_id = (paper_id or "").strip()
    if not paper_id:
        raise RuntimeError("缺少 paperId")
    url = ("https://xueshu.baidu.com/scholarai/paper/detail/info?paperId="
           + urllib.parse.quote(paper_id, safe=""))
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"https://xueshu.baidu.com/usercenter/paper/show?paperid={paper_id}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"百度学术不可用：{type(e).__name__}")
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        raise RuntimeError("百度学术返回的不是 JSON（可能需要验证码）")

    code = _deep_get(data, ("code",))
    msg = _deep_get(data, ("msg", "message", "error_msg"))
    if isinstance(code, int) and code not in (0, 200):
        raise RuntimeError(f"百度学术：{_clean_text(msg) or '论文不存在或已下线'}（code {code}）")

    doi = _clean_text(_deep_get(data, ("doi", "DOI")))
    doi = _norm_doi(doi)
    title = _clean_text(_deep_get(data, ("title", "paper_title", "paperTitle")))
    journal = (_join_scalar(_path_get(data, ["data", "publish_info", "journal", "name"]))
               or _clean_text(_deep_get(data, ("journal_name", "journalName"))))
    year = _clean_text(_deep_get(data, ("year", "publish_year", "publishYear")))
    volume = _clean_text(_deep_get(data, ("volume",)))
    issue = _clean_text(_deep_get(data, ("issue",)))
    pages = _clean_text(_deep_get(data, ("page", "pages")))
    issn = (_join_scalar(_path_get(data, ["data", "publish_info", "journal", "issn"]))
            or _join_scalar(_deep_get(data, ("issn",))))
    cn = _clean_text(_deep_get(data, ("cn", "cn_number", "cnNumber")))
    abstract = _clean_text(_deep_get(data, ("abstract", "summary")))
    if not (doi or title):
        raise RuntimeError("百度学术没返回标题或 DOI")
    return {"paper_id": paper_id, "doi": doi, "title": title, "journal": journal,
            "year": year, "volume": volume, "issue": issue, "pages": pages,
            "issn": issn, "cn": cn, "abstract": abstract[:1200],
            "url": f"https://xueshu.baidu.com/usercenter/paper/show?paperid={paper_id}"}


def baidu_paper_id(text: str) -> str:
    """Pull the paperid out of a pasted Baidu Scholar link (or accept a raw id)."""
    text = (text or "").strip()
    m = re.search(r"paperid=([0-9a-zA-Z]+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{16,64}", text):
        return text
    return ""


def _orcid_works_direct(orcid: str, limit: int = 50, year_from: str = "",                        year_to: str = "") -> list[dict]:
    """Fallback: the ORCID registry's own works list (no citation counts, no OA flags)."""
    data = get_json(f"https://pub.orcid.org/v3.0/{orcid}/works", timeout=15, tries=2)
    out = []
    for group in data.get("group") or []:
        summary = (group.get("work-summary") or [{}])[0]
        title = ((summary.get("title") or {}).get("title") or {}).get("value") or ""
        year = str((((summary.get("publication-date") or {}).get("year") or {}).get("value")) or "")
        if year_from and year and year < year_from:
            continue
        if year_to and year and year > year_to:
            continue
        doi = ""
        for ext in (summary.get("external-ids") or {}).get("external-id") or []:
            if (ext.get("external-id-type") or "").lower() == "doi":
                doi = _norm_doi(ext.get("external-id-value") or "")
                break
        if not doi:
            continue  # without a DOI the download step cannot use it
        out.append({"doi": doi, "title": title, "year": year,
                    "journal": (summary.get("journal-title") or {}).get("value") or "",
                    "authors": [], "cited": 0, "oa": False, "oa_url": "", "type": "article"})
        if len(out) >= limit:
            break
    return out


def crossref_search(query: str = "", email: str = "", year_from: str = "", year_to: str = "",
                    author: str = "", limit: int = 40) -> list[dict]:
    """Crossref search — the fallback when OpenAlex throttles (HTTP 429).

    Crossref has no daily quota for polite use and returns DOI/title/year/journal/
    authors/citation count, which is everything the results table shows.
    """
    params = {
        "rows": str(min(max(limit, 1), 100)),
        "select": "DOI,title,author,issued,container-title,is-referenced-by-count,type",
        "sort": "relevance",
        # without the type filter Crossref puts supporting-information
        # components (*.s001) at the top of the relevance list
        "filter": "type:journal-article,type:posted-content",
    }
    if query:
        params["query.bibliographic"] = query
    if author:
        params["query.author"] = author
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")
    if filters:
        params["filter"] += "," + ",".join(filters)
    data = get_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params),
                    email, timeout=15, tries=2)
    out = []
    for it in (data.get("message") or {}).get("items") or []:
        doi = _norm_doi(it.get("DOI") or "")
        if (it.get("type") or "") == "component" or re.search(r"\.s\d+$", doi):
            continue  # supporting-information components are not the article
        title = (it.get("title") or [""])[0] if it.get("title") else ""
        year = ""
        parts = ((it.get("issued") or {}).get("date-parts") or [[]])[0]
        if parts:
            year = parts[0]
        auths = []
        for a in (it.get("author") or [])[:6]:
            name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name") or ""
            if name:
                auths.append(name)
        out.append({
            "doi": doi,
            "title": _norm_ws(title),
            "year": year,
            "journal": _norm_ws((it.get("container-title") or [""])[0]) if it.get("container-title") else "",
            "authors": auths,
            "cited": it.get("is-referenced-by-count") or 0,
            "oa": False,
            "oa_url": "",
            "type": it.get("type") or "",
        })
    return out[:max(1, limit)]


def search_works(query: str, email: str = "", year_from: str = "", year_to: str = "",
                 oa_only: bool = False, author: str = "", limit: int = 40,
                 sort: str = "relevance_score:desc", diag: dict | None = None) -> list[dict]:
    if diag is not None:
        diag.setdefault("source", "OpenAlex")
    params = {
        "search": query,
        "per-page": str(min(max(limit, 1), 100)),
        "select": "id,doi,title,publication_year,authorships,primary_location,"
                  "open_access,cited_by_count,type",
        "sort": sort,
    }
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if oa_only:
        filters.append("is_oa:true")
    if author:
        try:
            aid = resolve_author(author, email)
        except Exception:  # noqa: BLE001 - throttled author lookup must not kill the search
            aid = None
        if aid:
            filters.append(f"author.id:{aid}")
    if filters:
        params["filter"] = ",".join(filters)

    try:
        data = get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params), email)
    except Exception as e:  # noqa: BLE001
        if diag is not None:
            diag["source"] = "Crossref"
            diag["note"] = f"OpenAlex 不可用（{str(e)[:30]}），已改用 Crossref 结果"
        return crossref_search(query, email, year_from, year_to, author, limit)

    out = []
    for w in data.get("results", []) or []:
        auths = [((a.get("author") or {}).get("display_name") or "")
                 for a in (w.get("authorships") or [])[:6]]
        loc = w.get("primary_location") or {}
        oa = w.get("open_access") or {}
        best = (w.get("best_oa_location") if "best_oa_location" in w else None) or {}
        out.append({
            "doi": _norm_doi(w.get("doi") or ""),
            "title": w.get("title") or "",
            "year": w.get("publication_year") or "",
            "journal": ((loc.get("source") or {}) or {}).get("display_name") or "",
            "authors": auths,
            "cited": w.get("cited_by_count") or 0,
            "oa": bool(oa.get("is_oa")),
            "oa_url": oa.get("oa_url") or best.get("pdf_url") or best.get("landing_page_url") or "",
            "type": w.get("type") or "",
        })
    return out
