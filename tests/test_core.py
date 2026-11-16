import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DOIFINDER_DATA"] = tempfile.mkdtemp()
import server, exporters


def test_download_workers_not_forced_up():
    assert server.save_settings({"download_workers": 3})["download_workers"] == 3
    assert server.save_settings({"download_workers": 99})["download_workers"] == 5


def test_secrets_masked_and_preserved():
    server.save_settings({"elsevier_key": "SECRET"})
    assert server.public_settings()["elsevier_key"] == "********"
    server.save_settings({"elsevier_key": "********"})
    assert server.load_settings()["elsevier_key"] == "SECRET"


def test_csv_formula_injection():
    out = exporters.to_csv([{"n": 1, "title": "=1+1", "doi": "10.1/x", "status": "found"}])
    assert "'=1+1" in out


def test_markdown_pipe_escape():
    md = exporters.to_markdown([{"n": 1, "title": "a|b", "journal": "J|x", "doi": "10.1/x", "status": "found"}])
    assert "a\\|b" in md and "J\\|x" in md


def test_journal_folder_layout():
    import downloader
    assert downloader.journal_folder("J. Chem. Phys.", 2024, "160", "3") == "J. Chem. Phys/2024/Volume 160, Issue 3"
    assert downloader.journal_folder("A/B", "2020", "", "") .endswith("/2020/No volume-issue info")
    assert ".." not in downloader.journal_folder("..", "2020", "1", "")
    assert downloader.journal_folder("J", "", "5", "").endswith("Unknown year/Volume 5")


def test_article_kind():
    import journals
    W = "WIREs Computational Molecular Science"
    assert journals.article_kind("Machine learning potentials", W) == "review"
    assert journals.article_kind("Correction to: X", W) == "non-research"
    assert journals.article_kind("Issue Information", "J. Chem. Phys.") == "non-research"
    assert journals.article_kind("A review of coupled cluster", "J. Chem. Phys.") == "review"
    assert journals.article_kind("Path integral dynamics", "Physical Review B") == "research"


def test_netpolicy_prepare_and_gate():
    import netpolicy, time
    netpolicy.set_openalex_key("K")
    assert netpolicy.prepare("https://api.openalex.org/works?x=1").endswith("&api_key=K")
    assert "api_key" not in netpolicy.prepare("https://api.crossref.org/works?x=1")
    assert "mailto=a%40b.c" in netpolicy.prepare("https://api.crossref.org/works?x=1", "a@b.c")
    netpolicy.set_openalex_key("")
    assert netpolicy.prepare("https://api.openalex.org/works") == "https://api.openalex.org/works"
    # polite list queries: ~3/s -> 4 calls take at least ~1 s
    t0 = time.monotonic()
    for _ in range(4):
        with netpolicy.gate("https://api.crossref.org/works?query=x", "a@b.c"):
            pass
    assert time.monotonic() - t0 >= 0.9


def test_stop_and_pause_inside_download():
    import threading, time, tempfile, downloader

    class Stop:  # same contract as server._PauseGate
        def __init__(self): self.stopped = False; self.paused = False
        def is_set(self):
            while self.paused and not self.stopped:
                time.sleep(0.02)
            return self.stopped

    d = tempfile.mkdtemp()
    s = Stop(); s.stopped = True
    rec = downloader.download_one("10.1000/xyz", d, "", "{title}", {}, stop=s)
    assert rec["status"] == "stopped"

    # pause: the call must block until resumed, then continue and stop
    s = Stop(); s.paused = True
    out = {}
    t = threading.Thread(target=lambda: out.update(r=downloader.download_one("10.1000/xyz", d, "", "{title}", {}, stop=s)))
    t.start(); time.sleep(0.4)
    assert t.is_alive(), "should be held while paused"
    s.stopped = True; t.join(5)
    assert not t.is_alive() and out["r"]["status"] == "stopped"


def test_scihub_total_budget(monkeypatch=None):
    import time, scihub

    class FakeClient:
        def _ordered_mirrors(self): return ["https://m%d.example" % i for i in range(20)]
        def _resolve_on_mirror(self, url, doi):
            time.sleep(0.3); raise scihub.SciHubError("slow")
        def _bump(self, *a): pass
        def resolve_scinet_pdf_url(self, doi): return ""

    orig = scihub.client
    scihub.client = lambda *a, **k: FakeClient()
    try:
        t0 = time.monotonic()
        data, err, *_ = scihub.fetch("10.1/x", mirrors=["https://a.example"], discover=False, budget=1.0)
        assert data is None and time.monotonic() - t0 < 3, "20 mirrors x 0.3 s must be cut off at the budget"
    finally:
        scihub.client = orig
