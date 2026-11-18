"""Regression tests for bugs found while taking over the project."""
import os
import re
import sys
import tempfile
import threading
import json
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DOIFINDER_DATA", tempfile.mkdtemp())


def test_every_element_js_needs_exists_in_html():
    """A removed input that app.js still reads crashed lookup, download and settings."""
    js = open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'\$\("([A-Za-z0-9_\-]+)"\)', js))
    # rendered dynamically, or always guarded by an `if ($("x"))` check
    dynamic = {"editDoi", "rgStatus", "jhKind", "langToggle"}   # langToggle: made by i18n.js
    assert sorted(u for u in used if u not in ids and u not in dynamic) == []


def test_junk_filter():
    import journals
    for t in ("Issue Information", "Front Cover", "Correction to: X", "Author Index", "Table of Contents"):
        assert journals.is_junk(t), t
    for t in ("Index theorems in physics", "Corrections in quantum chemistry", "Machine learning potentials"):
        assert not journals.is_junk(t), t


def test_harvest_paging_filters_and_sanitises_type(monkeypatch=None):
    import journals
    seen_urls = []

    def fake_get(url, rows=3):
        seen_urls.append(url)
        if "rows=1&" in url or "sort=published" in url:
            return {"message": {"items": [{"DOI": "10.1/a", "title": ["t"], "published": {"date-parts": [[2020]]}}]}}
        items = [
            {"DOI": "10.1/a", "title": ["A real paper"], "volume": "1", "issue": "2",
             "container-title": ["J"], "published": {"date-parts": [[2020, 1, 5]]}},
            {"DOI": "10.1/b", "title": ["Issue Information"], "published": {"date-parts": [[2020, 1, 5]]}},
        ]
        return {"message": {"items": items, "total-results": 2}}

    orig = journals._get
    journals._get = fake_get
    try:
        errs = []
        got = journals.harvest("1759-0876", year_from=2020, year_to=2020, kind="全部类型", errors=errs)
    finally:
        journals._get = orig
    assert [g["doi"] for g in got] == ["10.1/a"]          # front matter dropped
    assert not errs
    assert all("type%3A%3F" not in u and "全部" not in u for u in seen_urls)   # no label in the URL


def test_path_allowed_is_case_and_slash_insensitive():
    import server
    base = tempfile.mkdtemp()
    server.save_settings({"download_dir": base})
    h = server.Handler.__new__(server.Handler)
    assert h._path_allowed(os.path.join(base, "sub", "x.pdf"))
    assert h._path_allowed(base + os.sep)
    assert not h._path_allowed(os.path.dirname(base))
    assert not h._path_allowed(os.path.join(base, "..", "etc"))


def test_platform_util_helpers():
    import platform_util
    assert platform_util.data_dir().endswith("DOIFinder")
    assert platform_util.curl_bin() in ("curl", "curl.exe")
    if not platform_util.IS_WIN:
        assert platform_util.popen_kwargs() == {}
    assert platform_util.is_edge("/usr/bin/microsoft-edge") and not platform_util.is_edge("/usr/bin/chromium")


def test_api_jobs_answers_get():
    import server
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for path in ("/api/jobs", "/api/health"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                assert r.status == 200
                data = json.loads(r.read())
        assert "ok" in data
    finally:
        httpd.shutdown()


def test_journal_tab_has_type_filter():
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()
    assert 'id="jdKind"' in html and "selectByKind" in js


def test_harvest_rejects_journal_title_instead_of_issn():
    """The page once sent a journal *title* as the ISSN, which ended in 'HTTP 404' for year 1900."""
    import server
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(base + "/api/journal/harvest",
                                     data=json.dumps({"issn": "Wiley Interdisciplinary Reviews"}).encode(),
                                     headers={"Content-Type": "application/json", "Origin": base})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "ISSN" in json.loads(e.read())["error"]
    finally:
        httpd.shutdown()


def test_harvest_unknown_journal_reports_clear_error():
    import journals
    orig = journals._get

    def boom(url, rows=3):
        raise RuntimeError("HTTP 404")
    journals._get = boom
    try:
        errs = []
        assert journals.harvest("0000-0000", errors=errs) == []
        assert errs and "查不到这本期刊" in errs[0]
    finally:
        journals._get = orig


def test_core_needs_key_and_exact_doi():
    import netpolicy
    import sources
    calls = []
    orig = sources.get_json

    def fake(url, mailto="", timeout=15, tries=3, headers=None):
        calls.append((url, headers))
        return {"results": [
            {"doi": "10.1/other", "downloadUrl": "https://x.org/wrong.pdf"},
            {"doi": "10.1002/wcms.1704", "downloadUrl": "https://core.ac.uk/download/1.pdf",
             "sourceFulltextUrls": ["https://repo.org/a.pdf", "https://repo.org/a.html"]}]}
    sources.get_json = fake
    try:
        netpolicy.set_core_key("")
        assert sources.core_candidates("10.1002/wcms.1704") == [] and not calls     # no key: no request
        netpolicy.set_core_key("k")
        urls = [c["url"] for c in sources.core_candidates("10.1002/wcms.1704")]
        assert urls == ["https://core.ac.uk/download/1.pdf", "https://repo.org/a.pdf"]
        assert calls[0][1]["Authorization"] == "Bearer k"
    finally:
        sources.get_json = orig
        netpolicy.set_core_key("")


def test_internet_archive_prefers_webarchive_pdfs():
    import sources
    orig = sources.get_json
    sources.get_json = lambda url, **k: {"files": [
        {"mimetype": "text/html", "urls": [{"rel": "webarchive", "url": "https://web.archive.org/web/1/h.html"}]},
        {"mimetype": "application/pdf", "urls": [
            {"rel": "repository", "url": "https://repo.org/p.pdf"},
            {"rel": "publisher", "url": "https://pub.org/p.pdf"},
            {"rel": "webarchive", "url": "https://web.archive.org/web/2/https://repo.org/p.pdf"}]}]}
    try:
        got = [c["url"] for c in sources.internet_archive_candidates("10.1/x")]
        assert got == ["https://web.archive.org/web/2/https://repo.org/p.pdf", "https://repo.org/p.pdf"]
    finally:
        sources.get_json = orig


def test_core_key_is_masked_in_public_settings():
    import server
    server.save_settings({"core_key": "abc123"})
    assert server.public_settings()["core_key"] == "********"
    server.save_settings({"core_key": ""})


def _journal_csv(header, rows):
    return header + "\n" + "\n".join(rows) + "\n"


def test_journalinfo_parses_english_and_chinese_headers():
    import journalinfo
    journalinfo.configure(tempfile.mkdtemp())
    en = _journal_csv("id,name,issn,eissn,publisher,index,cas_zone_2025,jcr_quartile_2025,impact_factor_2024,"
                      "review_sample_count,review_avg_days,review_median_days,review_min_days,review_max_days,updated",
                      ["1,Test J,1759-0876,1759-0884,Wiley,SCIE,2区,Q1,16.1,8,112.5,98,40,200,2026-11-14"])
    zh = _journal_csv("编号,期刊名称,ISSN,eISSN,出版社,收录,中科院分区,JCR分区,影响因子,样本数,平均审稿天数,中位审稿天数,最短天数,最长天数,更新日期",
                      ["1,测试刊,0001-0002,,某社,SSCI,3区,Q2,2.5,5,60,55,20,90,2026-11-14"])
    for text, issn, avg in ((en, "1759-0884", 112.5), (zh, "0001-0002", 60.0)):
        with open(journalinfo.path(), "w", encoding="utf-8") as fh:
            fh.write(text)
        journalinfo._CACHE["mtime"] = 0.0
        rec = journalinfo.lookup(issn)
        assert rec and rec["avg_days"] == avg and rec["quartile"] in ("Q1", "Q2")
    assert journalinfo.lookup("9999-9999") is None


def test_journalinfo_update_rejects_incomplete_or_unrecognised_data():
    import io
    import journalinfo
    journalinfo.configure(tempfile.mkdtemp())

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    bad = lambda req, timeout=0: R(b"foo,bar\n1,2\n")                                   # noqa: E731
    try:
        journalinfo.update(opener=bad)
        raise AssertionError("expected failure")
    except RuntimeError as e:
        assert "下载失败" in str(e)
    assert not os.path.exists(journalinfo.path())            # a bad download never replaces anything
    rows = [f"{i},J{i},{1000 + i:04d}-{i:04d},,P,SCIE,2区,Q1,1.0,3,50,45,20,80,2026-11-14" for i in range(150)]
    good = _journal_csv("id,name,issn,eissn,publisher,index,cas_zone,jcr_quartile,impact_factor,sample_count,avg_days,median_days,min_days,max_days,updated", rows)
    st = journalinfo.update(opener=lambda req, timeout=0: R(good.encode()))
    assert st["loaded"] and st["count"] >= 100


def test_journalinfo_endpoints_and_page_elements():
    import server
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/journalinfo?issn=0000-0000", timeout=5) as r:
            d = json.loads(r.read())
        assert d["info"] is None and "attribution" in d["status"]
    finally:
        httpd.shutdown()
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    assert 'id="jdInfo"' in html and 'id="coreKey"' in html

def test_scihub_verifies_tls_by_default():
    import ssl
    import scihub
    import server
    assert server.scihub_conf({})["insecure"] is False
    strict = scihub._Session(insecure=False)
    loose = scihub._Session(insecure=True)
    ctx = lambda s: next(h for h in s.opener.handlers if hasattr(h, "_context"))._context  # noqa: E731
    assert ctx(strict).verify_mode == ssl.CERT_REQUIRED and ctx(strict).check_hostname
    assert ctx(loose).verify_mode == ssl.CERT_NONE


# ----------------------------------------------------------------- download logic
def _stub_download_env(monkey, *, oa=(), fetch=None, published=""):
    import browser
    import downloader
    import sources
    monkey.append((sources, "work_meta", sources.work_meta))
    monkey.append((sources, "resolve_oa", sources.resolve_oa))
    monkey.append((sources, "published_version", sources.published_version))
    monkey.append((downloader, "fetch_pdf", downloader.fetch_pdf))
    monkey.append((browser, "find_browser", browser.find_browser))
    sources.work_meta = lambda doi, email="": {"title": "A Paper", "year": "2020", "first_author": "Smith", "journal": "J"}
    sources.resolve_oa = lambda doi, email="", diag=None: list(oa)
    sources.published_version = lambda doi, title="", mailto="": published
    downloader.fetch_pdf = fetch or (lambda url: (None, "HTTP 404", ""))
    browser.find_browser = lambda: None


def _restore(monkey):
    for obj, name, val in reversed(monkey):
        setattr(obj, name, val)


def test_download_reports_no_open_access_record_and_key_hints():
    import downloader
    undo = []
    _stub_download_env(undo)
    try:
        d = tempfile.mkdtemp()
        rec = downloader.download_one("10.1016/j.x.2020.1", d, "", "{title}", {}, scihub_conf={"enabled": False})
        # the DOI landing page is always tried, but "no OA record" must still be detected and reported
        assert rec["status"] == "no_oa", rec
        assert "没有开放获取记录" in rec["note"]
        assert "elsevier_key" in rec["note"]            # the fixable hint used to be silently dropped
        rec = downloader.download_one("10.1002/wcms.1704", d, "", "{title}", {}, scihub_conf={"enabled": False})
        assert "wiley_token" in rec["note"]
    finally:
        _restore(undo)


def test_preprint_swapped_for_published_version_is_indexed_under_both_dois():
    import downloader
    undo = []
    _stub_download_env(undo, oa=[{"url": "https://x.org/a.pdf", "source": "T"}],
                       fetch=lambda url: (b"%PDF-1.4 data", "", url), published="10.1002/published.1")
    try:
        d = tempfile.mkdtemp()
        idx = {}
        rec = downloader.download_one("10.26434/chemrxiv-2020-abc", d, "", "{title}", idx, scihub_conf={"enabled": False})
        assert rec["status"] == "downloaded", rec
        assert "10.26434/chemrxiv-2020-abc" in idx and "10.1002/published.1" in idx
        again = downloader.download_one("10.26434/chemrxiv-2020-abc", d, "", "{title}", idx, scihub_conf={"enabled": False})
        assert again["status"] == "exists"             # was re-downloaded every time before
    finally:
        _restore(undo)


def test_filenames_are_creatable_everywhere():
    import downloader
    assert downloader.sanitize("CON") == "_CON" and downloader.sanitize("nul.txt").startswith("_")
    assert downloader.sanitize("Console") == "Console"
    base = downloader._fit_name(tempfile.mkdtemp(), "量子化学" * 80, ".pdf")
    assert len((base + ".pdf").encode("utf-8")) <= 255
    # two writers with the same title never overwrite each other
    d = tempfile.mkdtemp()
    p1, n1 = downloader._save(d, "Same Title", b"%PDF-1 one", False)
    p2, n2 = downloader._save(d, "Same Title", b"%PDF-1 two", False)
    assert n1 == "Same Title.pdf" and n2 == "Same Title (2).pdf"
    assert open(p1, "rb").read().endswith(b"one") and open(p2, "rb").read().endswith(b"two")


# ----------------------------------------------------------------- harvest logic
def test_harvest_year_folder_follows_the_issue_and_sorts_naturally():
    import journals
    items = []
    for vol in ("9", "10"):
        items.append({"DOI": f"10.1/v{vol}", "title": [f"Paper v{vol}"], "volume": vol, "issue": "1",
                      "container-title": ["J"], "type": "journal-article",
                      "published": {"date-parts": [[2023, 12, 20]]},
                      "published-online": {"date-parts": [[2023, 12, 20]]},
                      "published-print": {"date-parts": [[2024, 1]]}})
    items.append({"DOI": "10.1/noissue", "title": ["Online first"], "type": "journal-article",
                  "published": {"date-parts": [[2023, 11, 2]]}, "published-online": {"date-parts": [[2023, 11, 2]]}})
    orig = journals._get
    journals._get = lambda url, rows=3: {"message": {"items": items, "total-results": len(items)}}
    try:
        got = journals.harvest("1759-0876", year_from=2023, year_to=2023, kind="")
    finally:
        journals._get = orig
    by = {g["doi"]: g for g in got}
    assert by["10.1/v9"]["year"] == "2024"          # filed with its issue, not the December online date
    assert by["10.1/noissue"]["year"] == "2023"     # no issue yet: online date
    assert [g["volume"] for g in got if g["volume"]] == ["9", "10"]    # 9 before 10, not "10" < "9"


def test_pause_time_is_not_counted_against_the_per_paper_deadline():
    import server
    import time
    job = {"cancel": threading.Event(), "paused": False}
    gate = server._PauseGate(job)
    assert gate.paused_seconds() == 0.0
    job["paused"], job["paused_at"] = True, time.monotonic() - 5     # paused 5 s ago
    assert 4.9 < gate.paused_seconds() < 6
    job["paused_total"], job["paused"], job["paused_at"] = 7.0, False, 0.0    # resumed: 7 s banked
    assert gate.paused_seconds() == 7.0


def test_deadline_ignores_paused_time_in_download_one():
    import downloader
    undo = []
    calls = {"n": 0}

    class Stop:
        def is_set(self): return False
        def paused_seconds(self):
            calls["n"] += 1
            return 0.0 if calls["n"] == 1 else 1000.0     # "paused" for 1000 s while working
    _stub_download_env(undo)
    old = downloader.DEADLINE_PER_DOI
    downloader.DEADLINE_PER_DOI = 0      # any real elapsed time would trip this, unless pause time is excluded
    try:
        rec = downloader.download_one("10.1002/wcms.1704", tempfile.mkdtemp(), "", "{title}", {},
                                      scihub_conf={"enabled": False}, stop=Stop())
        assert "超过" not in rec["note"], rec["note"]      # not aborted as "too slow"
    finally:
        downloader.DEADLINE_PER_DOI = old
        _restore(undo)


def test_open_folder_allows_job_dirs_only():
    import server
    base = tempfile.mkdtemp()
    other = tempfile.mkdtemp()
    server.save_settings({"download_dir": base})
    h = server.Handler.__new__(server.Handler)
    server._JOB_DIRS.clear()
    assert not h._openable(other)                       # arbitrary folder: refused
    server._register_job_dir(other)
    assert h._openable(other) and h._openable(os.path.join(other, "2024", "v1"))
    assert not h._openable(os.path.dirname(other))      # the parent is still refused
    assert h._openable(base)
    server._JOB_DIRS.clear()
