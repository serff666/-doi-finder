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
    dynamic = {"editDoi", "rgStatus", "jhKind"}
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
