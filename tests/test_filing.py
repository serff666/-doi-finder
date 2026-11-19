"""The one filing rule: <workspace>/<collection>/<sub>/file.pdf.  Network is faked."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filing  # noqa: E402


def test_collection_names_are_safe_single_folders():
    assert filing.collection_name("") == "未分类" and filing.collection_name("   ") == "未分类"
    assert filing.collection_name("Zhang, Xinglong") == "Zhang, Xinglong"
    assert "/" not in filing.collection_name("J. Chem. Phys./Part A") and ":" not in filing.collection_name("a:b")
    assert filing.collection_name("CON") == "_CON"
    assert not filing.collection_name("name. ").endswith(" ") and not filing.collection_name("name. ").endswith(".")
    assert len(filing.collection_name("x" * 300)) <= 80
    assert filing.collection_name("../../etc") .count("/") == 0 and ".." not in filing.collection_name("../../etc").split(os.sep)


def test_sub_for_structures():
    m = {"year": "2023-05-01", "journal": "J. Am. Chem. Soc.", "volume": "145", "issue": "3"}
    assert filing.sub_for("flat", m) == ""
    assert filing.sub_for("year", m) == "2023"
    assert filing.sub_for("journal", m) == "J. Am. Chem. Soc"
    assert filing.sub_for("journal_year", m) == "J. Am. Chem. Soc/2023"
    assert filing.sub_for("volume_issue", m) == "2023/Volume 145, Issue 3"
    assert filing.sub_for("volume_issue", {"year": 2020}) == "2020/No volume-issue info"
    # missing metadata never makes a path component empty
    assert filing.sub_for("year", {}) == "年份未知" and filing.sub_for("journal", {}) == "期刊未知"
    assert filing.sub_for("journal_year", None) == "期刊未知/年份未知"
    assert filing.sub_for("nonsense", m) == ""


def test_destination_and_inside():
    ws = tempfile.mkdtemp()
    assert filing.destination(ws, "") == ws                                   # old requests: no collection level
    assert filing.destination(ws, "Zhang") == os.path.join(ws, "Zhang")
    assert filing.inside(os.path.join(ws, "Zhang", "a.pdf"), os.path.join(ws, "Zhang"))
    assert not filing.inside(os.path.join(ws, "Other", "a.pdf"), os.path.join(ws, "Zhang"))
    assert not filing.inside(os.path.join(ws, "Zhang2", "a.pdf"), os.path.join(ws, "Zhang"))


# ------------------------------------------------------------------ the downloader follows the rule
def _fakes():
    import browser
    import downloader
    import sources
    undo = []
    for o, n in ((sources, "work_meta"), (sources, "resolve_oa"), (sources, "published_version"),
                 (downloader, "fetch_pdf"), (browser, "find_browser"), (downloader, "INST_MIN_INTERVAL")):
        undo.append((o, n, getattr(o, n)))
    metas = {"10.1021/a.1": {"title": "Alpha paper", "year": "2023", "first_author": "Ann Smith", "journal": "JACS"},
             "10.1021/b.2": {"title": "Beta paper", "year": "2021", "first_author": "Bo Chen", "journal": "Nature"},
             "10.1021/c.3": {"title": "Gamma paper", "year": "", "first_author": "", "journal": ""}}
    fetched = []
    sources.work_meta = lambda doi, email="": dict(metas[doi])
    sources.resolve_oa = lambda doi, email="", diag=None: [{"url": "https://oa.example/" + doi.replace("/", "_") + ".pdf", "source": "OA"}]
    sources.published_version = lambda doi, title="", mailto="": ""

    def fetch(url):
        fetched.append(url)
        return (b"%PDF-1.4 x" + url.encode(), "", url) if url.startswith("https://oa.example/") else (None, "HTTP 404", url)
    downloader.fetch_pdf = fetch
    browser.find_browser = lambda: None
    downloader.INST_MIN_INTERVAL = 0
    return undo, fetched


def _undo(undo):
    for o, n, v in reversed(undo):
        setattr(o, n, v)


def _many(dois, ws, collection, structure, **kw):
    import downloader
    return downloader.download_many(dois, filing.destination(ws, collection), "", "{year}_{first_author}_{title}",
                                    scihub_conf={"enabled": False}, root=ws, structure=structure, **kw)


def test_year_structure_files_under_collection_and_year_and_indexes_at_the_workspace():
    undo, fetched = _fakes()
    try:
        ws = tempfile.mkdtemp()
        r = _many(["10.1021/a.1", "10.1021/b.2", "10.1021/c.3"], ws, "Smith, Ann", "year")
        assert [x["status"] for x in r] == ["downloaded"] * 3, r
        assert r[0]["file"].startswith("2023/") and r[1]["file"].startswith("2021/") and r[2]["file"].startswith("年份未知/"), [x["file"] for x in r]
        for x in r:
            assert os.path.isfile(os.path.join(ws, "Smith, Ann", *x["file"].split("/"))), x
        idx = json.load(open(os.path.join(ws, ".doi-index.json"), encoding="utf-8"))
        assert idx["10.1021/a.1"]["file"].startswith("Smith, Ann/2023/") and idx["10.1021/a.1"]["meta"]["year"] == "2023"
        assert not os.path.exists(os.path.join(ws, "Smith, Ann", ".doi-index.json"))     # one index, at the root
        n = len(fetched)
        again = _many(["10.1021/a.1"], ws, "Smith, Ann", "year")
        assert again[0]["status"] == "exists" and len(fetched) == n          # nothing fetched the second time
    finally:
        _undo(undo)


def test_a_paper_filed_under_another_collection_is_copied_not_downloaded_again():
    undo, fetched = _fakes()
    try:
        ws = tempfile.mkdtemp()
        _many(["10.1021/a.1"], ws, "JACS", "journal_year")
        n = len(fetched)
        r = _many(["10.1021/a.1"], ws, "Smith, Ann", "year")[0]
        assert r["status"] == "exists" and "复制" in r["note"] and len(fetched) == n, r
        assert r["file"].startswith("2023/") and os.path.isfile(os.path.join(ws, "Smith, Ann", *r["file"].split("/")))
        # both copies are still there
        assert os.listdir(os.path.join(ws, "JACS", "JACS", "2023")) and os.listdir(os.path.join(ws, "Smith, Ann", "2023"))
        # asking again for the second collection does not copy or fetch again
        r2 = _many(["10.1021/a.1"], ws, "Smith, Ann", "year")[0]
        assert r2["status"] == "exists" and len(fetched) == n
    finally:
        _undo(undo)


def test_journal_library_folders_map_wins_and_is_relative_to_the_collection():
    undo, fetched = _fakes()
    try:
        ws = tempfile.mkdtemp()
        r = _many(["10.1021/a.1"], ws, "JACS", "flat", folders={"10.1021/a.1": filing.sub_for("volume_issue", {"year": 2023, "volume": "145", "issue": "3"})})[0]
        assert r["file"].startswith("2023/Volume 145, Issue 3/"), r
        assert os.path.isfile(os.path.join(ws, "JACS", *r["file"].split("/")))
    finally:
        _undo(undo)


def test_older_per_folder_indexes_are_still_recognised():
    undo, fetched = _fakes()
    try:
        ws = tempfile.mkdtemp()
        old = os.path.join(ws, "JACS", "2023", "Volume 1, Issue 1")          # laid out by an older version
        os.makedirs(old)
        open(os.path.join(old, "Smith_2023_Alpha paper.pdf"), "wb").write(b"%PDF-1.4 old")
        json.dump({"10.1021/a.1": {"file": "Smith_2023_Alpha paper.pdf", "title": "Alpha paper"}},
                  open(os.path.join(old, ".doi-index.json"), "w", encoding="utf-8"))
        r = _many(["10.1021/a.1"], ws, "JACS", "flat")[0]
        assert r["status"] == "exists" and not fetched, (r, fetched)
        assert r["file"] == "2023/Volume 1, Issue 1/Smith_2023_Alpha paper.pdf", r
    finally:
        _undo(undo)


def test_no_collection_keeps_the_old_flat_behaviour():
    undo, fetched = _fakes()
    try:
        ws = tempfile.mkdtemp()
        import downloader
        r = downloader.download_many(["10.1021/a.1"], ws, "", "{title}", scihub_conf={"enabled": False})[0]
        assert r["status"] == "downloaded" and "/" not in r["file"] and os.path.isfile(os.path.join(ws, r["file"]))
    finally:
        _undo(undo)


# ------------------------------------------------------------------ server job paths and the lab page
def test_server_job_paths():
    os.environ.setdefault("DOIFINDER_DATA", tempfile.mkdtemp())
    os.environ["DOIFINDER_NO_BROWSER"] = "1"
    import server
    ws = tempfile.mkdtemp()
    assert server.job_paths({"workspace": ws, "collection": "Zhang", "structure": "year"}) == (ws, os.path.join(ws, "Zhang"), "year")
    assert server.job_paths({"dir": ws}) == (ws, ws, "flat")                                 # old request
    assert server.job_paths({"workspace": ws, "collection": "x", "structure": "bogus"})[2] == "flat"
    assert server.job_paths({"workspace": ws, "collection": "../../x"})[1].startswith(ws)    # cannot climb out


def test_lab_pdfs_follow_the_same_structure():
    import labpage
    orig = labpage._fetch_pdf
    labpage._fetch_pdf = lambda url: b"%PDF-1.4 x"
    try:
        items = [{"title": "T one", "pdf": "https://x/a.pdf", "year": "2024"}, {"title": "T two", "pdf": "https://x/b.pdf", "year": ""}]
        d1 = tempfile.mkdtemp()
        r = labpage.download_pdfs(items, d1, structure="year")
        assert sorted(os.listdir(d1)) == ["2024", "年份未知"], os.listdir(d1)
        d2 = tempfile.mkdtemp()
        labpage.download_pdfs(items, d2, structure="flat")
        assert all(f.endswith(".pdf") for f in os.listdir(d2)) and len(os.listdir(d2)) == 2
        assert all(x["status"] == "downloaded" for x in r)
    finally:
        labpage._fetch_pdf = orig


def test_find_year_ignores_digits_inside_longer_numbers():
    # the DOI tail of acs.joc.4c02061 holds "2061"; it is not a year
    assert filing.find_year("acs.joc.4c02061") == ""
    assert filing.find_year("acs.joc.4c02061_Tan_2025") == "2025"
    assert filing.find_year("J. Org. Chem. 2025, 90, 2052–2061", last=True) == "2025"
    assert filing.find_year("Copyright 2019 ... 2061 pages 12345678") == "2019"
    assert filing.find_year("") == "" and filing.find_year(None) == ""


def test_archive_year_from_doi_like_file_name(tmp_path):
    import labpage
    assert labpage.parse_block("Tan J. Unraveling the Mechanism. J. Org. Chem. 2025, 90, 2052–2061")["year"] == "2025"


FIRST_PAGE_JOC = """The Journal of Organic Chemistry pubs.acs.org/joc Article
Unraveling the Mechanism and Influence of Auxiliary Ligands on the Isomerization of Neutral [P,O]-Chelated Nickel Complexes for Olefin Polymerization
Jeremy Tan, Jingyi Liu, and Xinglong Zhang
Cite This: J. Org. Chem. 2025, 90, 2052−2061 Read Online
Downloaded via 101.127.180.194 on February 25, 2025 at 04:24:37 (UTC).
Further advancement in the field came in 2000, when Grubbs and coworkers reported the use of
neutral salicylaldimine nickel catalysts. Shortly after, Drent and coworkers in 2002 showed that
palladium catalysts ... accounted for 34% of the entire plastic market in 2017."""


def test_year_from_first_page_prefers_the_citation_line():
    # the body names 2000, 2002 and 2017; the paper is from 2025
    assert filing.year_from_first_page(FIRST_PAGE_JOC) == "2025"


def test_year_from_first_page_other_sources_and_fallback():
    assert filing.year_from_first_page("Chem. Sci., 2024, 15, 100\nin 2030 and 2031") == "2024"
    assert filing.year_from_first_page("Foo\nPublished: 3 March 2023\nafter 2001 work") == "2023"
    assert filing.year_from_first_page("Foo\n© 2022 The Authors\nsince 1999") == "2022"
    assert filing.year_from_first_page("no marker but 2018 and 2021 appear") == "2021"
    assert filing.year_from_first_page("") == "" and filing.year_from_first_page("page 12345") == ""
