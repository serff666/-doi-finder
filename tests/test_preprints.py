"""ChemRxiv preprint finder and its use as the last download step. Every network call is faked."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import doi_engine as de  # noqa: E402
import preprints as pp  # noqa: E402

JOURNAL = "10.1021/jacs.2c01234"
JT = "Efficient conformer generation with machine learned force fields"


def cx(doi, title, year, authors, relation=None):
    it = {"DOI": doi, "title": [title], "issued": {"date-parts": [[year]]},
          "author": [dict(zip(("given", "family"), a.rsplit(" ", 1))) for a in authors]}
    if relation:
        it["relation"] = relation
    return it


def eng(doi, title, year, authors, pdf=None, vor=None):
    item = {"id": "abc123", "doi": doi, "title": title, "publishedDate": f"{year}-03-01T00:00:00Z",
            "authors": [dict(zip(("firstName", "lastName"), a.rsplit(" ", 1))) for a in authors]}
    if pdf:
        item["asset"] = {"original": {"url": pdf}}
    if vor:
        item["vor"] = {"vorDoi": vor}
    return {"item": item}


class Net:
    """engage: list of hits or an Exception; search: crossref items; works: {doi: message}."""
    mailto = ""

    def __init__(self, engage=(), search=(), works=None, fail_search=False):
        self.engage, self.search, self.works = engage, list(search), works or {}
        self.fail_search = fail_search
        self.urls = []

    def get_json(self, url, tries=3):
        self.urls.append(url)
        if "chemrxiv.org/engage" in url:
            if isinstance(self.engage, Exception):
                raise self.engage
            return {"itemHits": list(self.engage), "totalCount": len(self.engage)}
        if "/works?" in url:
            if self.fail_search:
                raise RuntimeError("HTTP 503")
            assert "prefix%3A10.26434" in url, url      # searches must stay inside ChemRxiv
            return {"message": {"items": self.search}}
        if "/works/" in url:
            import urllib.parse
            d = urllib.parse.unquote(url.split("/works/", 1)[1]).lower()
            if d in self.works:
                return {"message": self.works[d]}
            raise RuntimeError("HTTP 404")
        raise AssertionError(url)


def run(net, title=JT, doi="", fa="Ann Smith", year=2023, authors=None):
    return pp.find(title, doi=doi, first_author=fa, authors=authors, year=year, http=net)


# ------------------------------------------------------------------ finding
def test_exact_through_the_journal_records_own_link_even_when_title_and_authors_differ():
    pre = "10.26434/chemrxiv-2022-abcde"
    net = Net(works={JOURNAL: {"DOI": JOURNAL, "title": [JT],
                               "relation": {"has-preprint": [{"id-type": "doi", "id": pre.upper(), "asserted-by": "object"}]}},
                     pre: cx(pre, "A completely different working title", 2022, ["Bo Chen"])})
    r = run(net, doi=JOURNAL, fa="Ann Smith")
    c = r["candidates"][0]
    assert c["doi"] == pre and c["level"] == "exact" and "关联" in c["why"], r
    assert pp.best_auto(r)["doi"] == pre


def test_reworded_title_is_likely_when_author_and_year_agree():
    t2 = "Machine learned force fields for efficient generation of conformers"
    net = Net(search=[cx("10.26434/chemrxiv-2022-x1", t2, 2022, ["Ann Smith", "Bo Chen"])])
    c = run(net)["candidates"][0]
    assert c["level"] == "likely" and 0.55 <= c["score"] < 0.88, c
    assert "作者吻合" in c["why"] and "年份吻合" in c["why"]


def test_reworded_title_without_author_agreement_is_only_possible_and_never_automatic():
    t2 = "Machine learned force fields for efficient generation of conformers"
    net = Net(search=[cx("10.26434/chemrxiv-2022-x1", t2, 2022, ["Dan Wu"])])
    r = run(net)
    assert r["candidates"][0]["level"] == "possible" and pp.best_auto(r) is None


def test_identical_title_but_other_author_or_wrong_year_is_not_trusted():
    net = Net(search=[cx("10.26434/chemrxiv-2022-x1", JT, 2022, ["Dan Wu"])])
    c = run(net)["candidates"][0]
    assert c["level"] == "possible" and "第一作者对不上" in c["why"]
    net = Net(search=[cx("10.26434/chemrxiv-2015-x2", JT, 2015, ["Ann Smith"])])     # 8 years before
    c = run(net, year=2023)["candidates"][0]
    assert c["level"] == "possible" and "年份对不上" in c["why"]
    # an identical title with nothing contradicting it is accepted
    net = Net(search=[cx("10.26434/chemrxiv-2022-x3", JT, 2022, ["Ann Smith"])])
    assert run(net)["candidates"][0]["level"] == "likely"
    net = Net(search=[cx("10.26434/chemrxiv-2022-x4", JT, 2022, ["Ann Smith"])])
    assert run(net, fa="", year=None)["candidates"][0]["level"] == "likely"


def test_unrelated_records_are_dropped():
    net = Net(search=[cx("10.26434/chemrxiv-2022-x1", "Synthesis of perylene diimide dyes", 2022, ["Ann Smith"])])
    assert run(net)["candidates"] == []


def test_author_list_overlap_can_stand_in_for_first_author_order():
    t2 = "Machine learned force fields for efficient generation of conformers"
    net = Net(search=[cx("10.26434/chemrxiv-2022-x1", t2, 2022, ["Bo Chen", "Cy Diaz", "Ann Smith"])])
    assert run(net, fa="Smith", authors=["Ann Smith", "Cy Diaz"])["candidates"][0]["level"] == "likely"
    assert run(net, fa="Smith", authors=None)["candidates"][0]["level"] == "possible"
    # a single shared name, not in first position, is not enough
    assert run(net, fa="Smith", authors=["Ann Smith", "Zed Quux"])["candidates"][0]["level"] == "possible"


def test_engage_item_shapes_pdf_link_and_published_version():
    pdf = "https://chemrxiv.org/engage/api-gateway/chemrxiv/assets/orp/resource/item/abc123/original/paper.pdf"
    net = Net(engage=[eng("10.26434/chemrxiv-2022-e1", JT, 2022, ["Ann Smith"], pdf=pdf, vor=JOURNAL.upper())])
    r = run(net, doi=JOURNAL)
    c = r["candidates"][0]
    assert c["pdf_url"] == pdf and c["published_doi"] == JOURNAL and c["level"] == "exact", c
    # same preprint reported by both sources is one candidate that keeps the PDF link
    net = Net(engage=[eng("10.26434/chemrxiv-2022-e1", JT, 2022, ["Ann Smith"], pdf=pdf)],
              search=[cx("10.26434/chemrxiv-2022-e1", JT, 2022, ["Ann Smith"])])
    r = run(net)
    assert len(r["candidates"]) == 1 and r["candidates"][0]["pdf_url"] == pdf


def test_malformed_engage_payloads_never_raise():
    for bad in ([{"item": None}, {"item": {"doi": "", "title": "x"}}, "str", 5, {"item": {"title": "t"}}],):
        r = run(Net(engage=bad))
        assert r["candidates"] == [] and r["error"] == ""


def test_one_source_down_is_noted_but_not_an_error_and_all_down_is():
    net = Net(engage=RuntimeError("HTTP 403"), search=[cx("10.26434/chemrxiv-2022-x3", JT, 2022, ["Ann Smith"])])
    r = run(net)
    assert r["candidates"] and r["error"] == "" and any("Cloudflare" in n for n in r["notes"]), r
    net = Net(engage=RuntimeError("HTTP 403"), fail_search=True)
    r = run(net)
    assert r["candidates"] == [] and r["error"], r
    assert pp.best_auto(r) is None
    # asked and answered "nothing" is not an error
    assert run(Net()) == {"candidates": [], "notes": [], "error": ""}


def test_two_equally_good_likely_preprints_are_not_guessed_between():
    net = Net(search=[cx("10.26434/chemrxiv-2022-a", JT, 2022, ["Ann Smith"]),
                      cx("10.26434/chemrxiv-2022-b", JT, 2022, ["Ann Smith"])])
    r = run(net)
    assert len(r["candidates"]) == 2 and pp.best_auto(r) is None
    # but an exact link wins over a look-alike
    pre = "10.26434/chemrxiv-2022-a"
    net.works = {JOURNAL: {"relation": {"has-preprint": [{"id-type": "doi", "id": pre}]}}}
    assert pp.best_auto(run(net, doi=JOURNAL))["doi"] == pre


def test_helpers():
    assert pp.norm_doi(" https://doi.org/10.26434/ABC. ") == "10.26434/abc"
    assert pp.is_chemrxiv("DOI:10.26434/x") and not pp.is_chemrxiv("10.1021/x")
    assert pp._year({"date-parts": [[2021, 3]]}) == 2021 and pp._year("2020-05-01") == 2020 and pp._year(None) is None
    assert pp._year({"date-parts": []}) is None


# ------------------------------------------------------------------ as the last download step
def _env(undo, net, fetch, journal_fetch_ok=False):
    import browser
    import downloader
    import sources
    for o, n in ((sources, "work_meta"), (sources, "resolve_oa"), (sources, "published_version"),
                 (downloader, "fetch_pdf"), (browser, "find_browser"), (de, "Http")):
        undo.append((o, n, getattr(o, n)))
    sources.work_meta = lambda doi, email="": {"title": JT, "year": "2023", "first_author": "Ann Smith", "journal": "JACS"}
    sources.resolve_oa = lambda doi, email="", diag=None: []
    sources.published_version = lambda doi, title="", mailto="": ""
    downloader.fetch_pdf = fetch
    browser.find_browser = lambda: None
    de.Http = lambda *a, **k: net


def _undo(undo):
    for o, n, v in reversed(undo):
        setattr(o, n, v)


def _fetch_only(pdf_urls):
    return lambda url: ((b"%PDF-1.4 preprint", "", url) if any(url.startswith(u) for u in pdf_urls)
                        else (None, "HTTP 403", url))


def test_download_falls_back_to_the_preprint_and_says_so():
    import downloader
    pdf = "https://chemrxiv.org/engage/api-gateway/chemrxiv/assets/orp/resource/item/abc123/original/paper.pdf"
    net = Net(engage=[eng("10.26434/chemrxiv-2022-e1", "Machine learned force fields for efficient generation of conformers", 2022, ["Ann Smith"], pdf=pdf)])
    undo = []
    _env(undo, net, _fetch_only([pdf]))
    try:
        d, idx = tempfile.mkdtemp(), {}
        rec = downloader.download_one(JOURNAL, d, "", "{title}", idx, scihub_conf={"enabled": False})
        assert rec["status"] == "downloaded", rec
        assert "ChemRxiv 预印本 10.26434/chemrxiv-2022-e1" in rec["source"] and "作者吻合" in rec["source"], rec
        assert JOURNAL in idx          # a re-run recognises the file under the DOI the user asked for
        assert downloader.download_one(JOURNAL, d, "", "{title}", idx, scihub_conf={"enabled": False})["status"] == "exists"
    finally:
        _undo(undo)


def test_download_without_a_confident_preprint_reports_why():
    import downloader
    cases = [
        (Net(), "没有找到同一篇的预印本"),
        (Net(search=[cx("10.26434/chemrxiv-2022-x1", "Machine learned force fields for efficient generation of conformers", 2022, ["Dan Wu"])]), "把握不够"),
        (Net(engage=RuntimeError("HTTP 403"), fail_search=True), "没连上，可重试"),
    ]
    for net, text in cases:
        undo = []
        _env(undo, net, _fetch_only([]))
        try:
            rec = downloader.download_one(JOURNAL, tempfile.mkdtemp(), "", "{title}", {}, scihub_conf={"enabled": False})
            assert rec["status"] != "downloaded" and text in rec["note"], (text, rec)
        finally:
            _undo(undo)


def test_a_preprint_doi_is_not_searched_for_its_own_preprint():
    import downloader
    net = Net()
    undo = []
    _env(undo, net, _fetch_only([]))
    try:
        downloader.download_one("10.26434/chemrxiv-2022-zz", tempfile.mkdtemp(), "", "{title}", {}, scihub_conf={"enabled": False})
        assert not any("chemrxiv.org/engage" in u for u in net.urls), net.urls
    finally:
        _undo(undo)


def test_pdf_hint_from_the_finder_is_tried_first():
    import downloader
    import sources
    undo = [(sources, "work_meta", sources.work_meta), (sources, "resolve_oa", sources.resolve_oa),
            (sources, "published_version", sources.published_version), (downloader, "fetch_pdf", downloader.fetch_pdf)]
    sources.work_meta = lambda doi, email="": {"title": "Preprint", "year": "2022", "first_author": "", "journal": ""}
    sources.resolve_oa = lambda doi, email="", diag=None: []
    sources.published_version = lambda doi, title="", mailto="": ""
    seen = []

    def fetch(url):
        seen.append(url)
        return (b"%PDF-1.4 x", "", url) if "assets/orp" in url else (None, "HTTP 403", url)
    downloader.fetch_pdf = fetch
    try:
        downloader.register_pdf_hint("10.26434/CHEMRXIV-2022-H", "https://chemrxiv.org/engage/api-gateway/chemrxiv/assets/orp/x.pdf")
        rec = downloader.download_one("10.26434/chemrxiv-2022-h", tempfile.mkdtemp(), "", "{title}", {}, scihub_conf={"enabled": False})
        assert rec["status"] == "downloaded" and "assets/orp" in seen[0], (rec, seen)
    finally:
        _undo(undo)


# ------------------------------------------------------------------ the institutional browser is never skipped
def test_institutional_browser_is_opened_every_time_and_oa_still_tried_after_it_fails():
    import browser
    import downloader
    import sources
    undo = [(browser, "find_browser", browser.find_browser), (browser, "fetch_pdf", browser.fetch_pdf),
            (sources, "work_meta", sources.work_meta), (sources, "resolve_oa", sources.resolve_oa),
            (sources, "published_version", sources.published_version), (downloader, "fetch_pdf", downloader.fetch_pdf),
            (downloader, "INST_MIN_INTERVAL", downloader.INST_MIN_INTERVAL), (de, "Http", de.Http)]
    calls = []
    browser.find_browser = lambda: "chrome"
    browser.fetch_pdf = lambda doi, profile, timeout=75.0, login_url="": (calls.append(doi) or (None, "没拿到", ""))
    sources.work_meta = lambda doi, email="": {"title": "T", "year": "2025", "first_author": "", "journal": "J"}
    sources.resolve_oa = lambda doi, email="", diag=None: [{"url": "https://oa.example/a.pdf", "source": "OA"}]
    sources.published_version = lambda doi, title="", mailto="": ""
    downloader.fetch_pdf = lambda url: ((b"%PDF-1.4 oa", "", url) if url.endswith("a.pdf") else (None, "HTTP 404", url))
    downloader.INST_MIN_INTERVAL = 0
    de.Http = lambda *a, **k: Net()
    try:
        d = tempfile.mkdtemp()
        recs = [downloader.download_one(f"10.1021/jacs.25{n}", d, "", "{title}", {},
                                        scihub_conf={"enabled": False}, inst_conf={"enabled": True, "profile": "/p", "timeout": 1})
                for n in range(6)]
        # same publisher, five failures in a row: the browser is still opened for the sixth (it used to be sidelined)
        assert [c for c in calls if c.startswith("10.1021/jacs.250")] and len(calls) >= 6 * 2, calls
        # a new subscription-journal paper whose browser step fails still gets its open-access copy
        assert all(r["status"] == "downloaded" and r["source"] == "OA" for r in recs), recs
    finally:
        _undo(undo)


# ------------------------------------------------------------------ 2025 on: a certain preprint before the institutional browser
PRE = "10.26434/chemrxiv-2025-abcde"
REL_NET = lambda: Net(works={  # noqa: E731
    "10.1021/jacs.5c01234": {"DOI": "10.1021/jacs.5c01234", "title": [JT],
                             "relation": {"has-preprint": [{"id-type": "doi", "id": PRE}]}},
    PRE: cx(PRE, "Another working title", 2025, ["Bo Chen"])})


def _early(year, net, inst_extra=None, fetch_ok=True, doi="10.1021/jacs.5c01234"):
    import browser
    import downloader
    import sources
    undo = [(browser, "find_browser", browser.find_browser), (browser, "fetch_pdf", browser.fetch_pdf),
            (sources, "work_meta", sources.work_meta), (sources, "resolve_oa", sources.resolve_oa),
            (sources, "published_version", sources.published_version), (downloader, "fetch_pdf", downloader.fetch_pdf),
            (downloader, "INST_MIN_INTERVAL", downloader.INST_MIN_INTERVAL), (de, "Http", de.Http)]
    calls, fetched = [], []
    browser.find_browser = lambda: "chrome"
    browser.fetch_pdf = lambda d, profile, timeout=75.0, login_url="": (calls.append(d) or (None, "没拿到", ""))
    sources.work_meta = lambda d, email="": {"title": JT, "year": str(year), "first_author": "Ann Smith", "journal": "JACS"}
    sources.resolve_oa = lambda d, email="", diag=None: []
    sources.published_version = lambda d, title="", mailto="": ""

    def fetch(url):
        fetched.append(url)
        return (b"%PDF-1.4 pre", "", url) if fetch_ok and "10.26434" in url else (None, "HTTP 403", url)
    downloader.fetch_pdf = fetch
    downloader.INST_MIN_INTERVAL = 0
    de.Http = lambda *a, **k: net
    try:
        inst = {"enabled": True, "profile": "/p", "timeout": 1}
        inst.update(inst_extra or {})
        rec = downloader.download_one(doi, tempfile.mkdtemp(), "", "{title}", {}, scihub_conf={"enabled": False}, inst_conf=inst)
    finally:
        _undo(undo)
    return rec, calls, fetched


def test_recent_year_boundary():
    import downloader
    assert [downloader.recent_year(y) for y in (2024, "2024-12-31", 2025, "2025", 2026, "", None, "x")] == \
        [False, False, True, True, True, False, False, False]


def test_2025_paper_uses_the_certain_preprint_before_the_browser_and_says_so():
    rec, calls, _ = _early(2025, REL_NET())
    assert rec["status"] == "downloaded" and calls == [], (rec, calls)          # the browser was never opened
    assert rec["source"].startswith("ChemRxiv 预印本") and "预印本，可能与正式版有出入" in rec["note"], rec


def test_older_paper_still_goes_to_the_browser_first():
    rec, calls, fetched = _early(2024, REL_NET())
    assert calls and not any(PRE in u for u in fetched[:1]), (calls, fetched)   # browser first; preprint only as last resort
    assert rec["status"] == "downloaded"                                         # ...which still rescues it


def test_switch_off_keeps_the_old_order():
    rec, calls, _ = _early(2025, REL_NET(), {"preprint_early": False})
    assert calls, calls                                                          # browser tried before the last-resort preprint


def test_a_guessed_preprint_is_not_used_early():
    t2 = "Machine learned force fields for efficient generation of conformers"
    net = Net(search=[cx("10.26434/chemrxiv-2025-x1", t2, 2025, ["Ann Smith"])])
    rec, calls, _ = _early(2025, net)
    assert calls, calls                                                          # only "likely": browser first


def test_failed_early_preprint_does_not_block_the_browser_and_is_not_fetched_twice():
    net = REL_NET()
    rec, calls, fetched = _early(2025, net, fetch_ok=False)
    assert sum("/works/10.1021" in u for u in net.urls) == 1, net.urls         # the finder asked once, not twice
    assert calls, calls                                                          # browser still tried afterwards
    assert sum(PRE in u for u in fetched) == 1, fetched                          # one failed fetch, not two
    assert rec["status"] != "downloaded"


# ------------------------------------------------------------------ the server job behind the page
def _server():
    os.environ.setdefault("DOIFINDER_DATA", tempfile.mkdtemp())
    os.environ["DOIFINDER_NO_BROWSER"] = "1"
    import server
    return server


def test_preprint_one_handles_title_citation_doi_only_and_failures():
    server = _server()
    import sources
    old = sources.work_meta
    sources.work_meta = lambda doi, email="": ({"title": JT, "year": "2023", "first_author": "Ann Smith"}
                                              if doi == JOURNAL else (_ for _ in ()).throw(RuntimeError("down")))
    t2 = "Machine learned force fields for efficient generation of conformers"
    net = Net(search=[cx("10.26434/chemrxiv-2022-x1", t2, 2022, ["Ann Smith"])])
    try:
        # a bare DOI: the title, author and year come from the journal record
        r = server.preprint_one({"n": 1, "input": JOURNAL, "title": JOURNAL, "doi": JOURNAL}, net)
        assert r["status"] == "found" and r["auto"] == "10.26434/chemrxiv-2022-x1" and r["title"] == JT, r
        assert r["first_author"] == "Ann Smith" and r["year"] == "2023"
        # a title with nothing to confirm it: found, but not chosen automatically unless confident
        r = server.preprint_one({"n": 2, "input": t2, "title": t2, "doi": ""}, net)
        assert r["status"] == "found" and r["candidates"]
        # nothing exists
        r = server.preprint_one({"n": 3, "input": "x", "title": "Totally unrelated words about volcanoes", "doi": ""}, Net())
        assert r["status"] == "none" and r["candidates"] == [] and r["error"] == ""
        # a DOI whose record cannot be fetched: an error that says what to do, never "none"
        r = server.preprint_one({"n": 4, "input": "10.1000/zz9999", "title": "10.1000/zz9999", "doi": "10.1000/zz9999"}, Net())
        assert r["status"] == "error" and "标题" in r["error"], r
        # every source down: error (retryable), not "none"
        r = server.preprint_one({"n": 5, "input": JT, "title": JT, "doi": ""}, Net(engage=RuntimeError("HTTP 403"), fail_search=True))
        assert r["status"] == "error" and r["error"]
    finally:
        sources.work_meta = old


def test_preprint_job_runs_in_order_reports_progress_and_registers_pdf_links():
    server = _server()
    import threading
    import time
    import downloader
    pdf = "https://chemrxiv.org/engage/api-gateway/chemrxiv/assets/orp/resource/item/abc123/original/p.pdf"
    net = Net(engage=[eng("10.26434/chemrxiv-2022-e9", JT, 2022, ["Ann Smith"], pdf=pdf)])
    server.Http = lambda *a, **k: net
    jid = "t" + str(time.time()).replace(".", "")
    entries = [{"n": i + 1, "input": t, "title": t, "doi": ""} for i, t in
               enumerate([JT, "Totally unrelated words about volcanoes", JT])]
    server.JOBS[jid] = {"created": time.time(), "state": "running", "kind": "preprint", "done": 0,
                        "total": 3, "results": [], "cancel": threading.Event()}
    server.run_preprint_job(jid, entries, {"email": ""})
    job = server.JOBS[jid]
    assert job["state"] == "done" and job["done"] == 3, job
    assert [r["n"] for r in job["results"]] == [1, 2, 3]                 # input order, not completion order
    assert job["results"][0]["status"] == "found" and job["results"][1]["status"] == "none"
    assert downloader._PDF_HINTS.get("10.26434/chemrxiv-2022-e9") == pdf
    # a stop leaves the rest marked "stopped" so the page can offer to continue
    jid2 = jid + "b"
    ev = threading.Event()
    ev.set()
    server.JOBS[jid2] = {"created": time.time(), "state": "running", "kind": "preprint", "done": 0,
                         "total": 3, "results": [], "cancel": ev}
    server.run_preprint_job(jid2, entries, {"email": ""})
    assert server.JOBS[jid2]["state"] == "stopped"
    assert {r["status"] for r in server.JOBS[jid2]["results"]} == {"stopped"}
