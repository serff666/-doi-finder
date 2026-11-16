"""Terminal client: layout, input handling and command flow, against a fake engine and a tiny VT emulator."""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("DOIFINDER_DATA", tempfile.mkdtemp())

import tui  # noqa: E402
from tui_emu import Emu  # noqa: E402
from tui_fake import FakeApi  # noqa: E402

tui.USE_COLOR = False
tui.LANG = "en"


def make(cols=80, rows=24):
    em, api = Emu(rows, cols), FakeApi()
    app = tui.App(api, out=em)
    app.size = lambda: cols
    app.load_settings()
    return app, em, api


def feed(app, s):
    for k, v in tui._Parser().feed(s, final=True):
        app.on_token(k, v)
    app.draw()


def pump(app, secs):
    end = time.time() + secs
    while time.time() < end:
        app.drain()
        time.sleep(0.03)
        if app.job or app.dirty:
            app.draw()


def lines(em):
    out = em.text()
    while out and not out[-1]:
        out.pop()
    return out


def wait_idle(app, limit=6):
    end = time.time() + limit
    while app.job and time.time() < end:
        pump(app, 0.1)
    assert app.job is None, "job did not finish"


# ------------------------------------------------------------------ pure helpers
def test_tui_classify_and_selection():
    assert tui.classify_input("10.1038/nature14539\nhttps://doi.org/10.1126/science.1127647") == "download"
    assert tui.classify_input("Attention is all you need\n10.1038/nature14539") == "lookup"
    assert tui.classify_input("  \n ") == "empty"
    listing = [{"doi": "10.1/a"}, {"doi": ""}, {"doi": "10.1/c"}, {"doi": "10.1/d"}]
    assert tui.parse_selection(["1", "3-4"], listing) == (["10.1/a", "10.1/c", "10.1/d"], [])
    assert tui.parse_selection(["9", "x"], listing) == ([], ["9", "x"])
    assert tui.parse_selection(["all"], listing)[0] == ["10.1/a", "10.1/c", "10.1/d"]
    assert tui.parse_selection(["10.1000/ABC."], listing)[0] == ["10.1000/abc"]


def test_tui_argument_parsers():
    a = tui.parse_search_args(["perylene", "bisimide", "--oa", "--year", "2019-2022", "--author", "Smith"])
    assert a == {"query": "perylene bisimide", "oa_only": True, "year_from": "2019", "year_to": "2022",
                 "author": "Smith", "orcid": ""}
    j = tui.parse_journal_args(["WIREs", "Computational", "Molecular", "Science", "2011-2024", "--review"])
    assert j == {"name": "WIREs Computational Molecular Science", "year_from": 2011, "year_to": 2024, "kind": "review"}
    assert tui.parse_journal_args(["1759-0876"])["name"] == "1759-0876"
    k = tui.parse_journal_args(["2041-1480", "2015-2020"])          # ISSN first, then a year range
    assert (k["name"], k["year_from"], k["year_to"]) == ("2041-1480", 2015, 2020)


def test_tui_width_and_wrap():
    assert tui.width("查找ab") == 6
    assert tui.fit("查找论文abc", 7) == "查找论…"
    rows, r, c = tui.layout_input("abcdef", 6, 4)
    assert rows == ["abcd", "ef"] and (r, c) == (1, 2)
    rows, r, c = tui.layout_input("abcd", 4, 4)       # cursor after a full row goes to a new row
    assert (r, c) == (1, 0)


def test_tui_key_parser():
    p = tui._Parser()
    assert p.feed("\x1b[A\x1b[3~\x1bOH") == [("key", "up"), ("key", "delete"), ("key", "home")]
    assert p.feed("\x1b[200~x\ny\x1b[201~") == [("paste", "x\ny")]
    assert p.feed("abc\r\ndef") == [("paste", "abc\ndef")]          # unbracketed paste burst
    assert p.feed("\x1b", final=True) == [("key", "esc")]
    assert p.feed("a") == [("text", "a")] and p.feed("\r") == [("key", "enter")]


# ------------------------------------------------------------------ layout
def test_tui_layout_fits_narrow_terminal():
    app, em, api = make(cols=60)
    app.emit(app.welcome())
    feed(app, "\x1b[200~a\nb\nc\x1b[201~\r")
    pump(app, 0.9)
    wait_idle(app)
    ls = lines(em)
    assert all(tui.width(x) <= 60 for x in ls), [x for x in ls if tui.width(x) > 60]
    assert any("Deep learning" in x for x in ls)
    rows = [x for x in ls if "10.1021/x.1" in x]
    assert rows and "(0.71)" in rows[0], "result row must stay on one line"


def test_tui_cursor_sits_on_the_edited_cell():
    app, em, api = make()
    feed(app, "查找论文abc")
    feed(app, "\x1b[D\x1b[D")
    row = max(i for i, x in enumerate(em.screen()) if "❯" in x)
    assert em.r == row and em.c == 4 + tui.width("查找论文a")


def test_tui_long_input_wraps_inside_box():
    app, em, api = make(cols=40)
    feed(app, "x" * 100)
    sc = [x for x in em.screen() if x]
    assert sum(1 for x in sc if x.startswith("│")) >= 3
    assert all(tui.width(x) <= 40 for x in sc)


# ------------------------------------------------------------------ input handling
def test_tui_paste_placeholder_and_history():
    app, em, api = make()
    feed(app, "\x1b[200~a\nb\x1b[201~")
    assert app.buf.startswith("[Pasted") and len(app.pastes) == 1
    feed(app, "\x7f")
    assert app.buf == "" and not app.pastes
    feed(app, "/dow\t")
    assert app.buf == "/download "
    feed(app, "\x15/foo\r")
    pump(app, 0.1)
    assert any("Unknown command" in x for x in lines(em))
    feed(app, "\x1b[A")
    assert app.buf == "/foo"


def test_tui_ctrl_c_semantics():
    app, em, api = make()
    feed(app, "abc\x03")
    assert app.buf == "" and not app.quit
    feed(app, "\x03")
    assert not app.quit
    feed(app, "\x03")
    assert app.quit


# ------------------------------------------------------------------ command flow
def test_tui_lookup_then_download_default_selection():
    app, em, api = make()
    feed(app, "\x1b[200~t1\nt2\nt3\x1b[201~\r")
    wait_idle(app)
    assert app.default_dois == ["10.48550/arxiv.1706.03762"]      # uncertain/not_found excluded
    feed(app, "/download all\r")
    pump(app, 0.1)
    assert api.calls[-1][1]["dois"] == ["10.48550/arxiv.1706.03762", "10.1021/x.1"]
    wait_idle(app)


def test_tui_stop_download_with_esc():
    app, em, api = make()
    feed(app, "10.1000/a1\r")
    pump(app, 0.15)
    assert app.job and app.job["kind"] == "download"
    feed(app, "\x1b")
    wait_idle(app)
    assert any(c[0].startswith("/api/stop/") for c in api.calls)
    assert any("Download PDFs" in x for x in lines(em))


def test_tui_input_during_job_is_refused_not_lost():
    app, em, api = make()
    feed(app, "10.1000/a1\r")
    pump(app, 0.1)
    feed(app, "hello\r")
    pump(app, 0.1)
    assert any("job is running" in x for x in lines(em))
    wait_idle(app)
    assert api.calls.count(("/api/lookup", {})) == 0 and not any(c[0] == "/api/lookup" for c in api.calls)


def test_tui_search_select_retry():
    app, em, api = make()
    feed(app, "/search perylene --oa --year 2019-2022\r")
    wait_idle(app)
    body = [c for c in api.calls if c[0] == "/api/search"][0][1]
    assert body["oa_only"] and body["year_from"] == "2019" and body["query"] == "perylene"
    feed(app, "/download 2\r")
    pump(app, 0.1)
    assert api.calls[-1][1]["dois"] == ["10.1/b"]
    wait_idle(app)
    feed(app, "/retry\r")
    pump(app, 0.1)
    assert api.calls[-1][1]["dois"] == ["10.1/b"]
    wait_idle(app)


def test_tui_journal_layout_and_kind_filter():
    app, em, api = make()
    feed(app, "/journal WIREs Computational Molecular Science 2011-2013\r")
    wait_idle(app)
    h = [c for c in api.calls if c[0] == "/api/journal/harvest"][0][1]
    assert h == {"issn": "1759-0876", "year_from": 2011, "year_to": 2013}
    feed(app, "/download\r")
    pump(app, 0.1)
    d = api.calls[-1][1]
    assert len(d["dois"]) == 8 and "10.1002/wcms.4" not in d["dois"]
    assert d["layout"]["journal"] == "WIREs Computational Molecular Science" and len(d["layout"]["items"]) == 8
    wait_idle(app)
    feed(app, "/download 1-2\r")                      # a subset keeps the folder layout
    pump(app, 0.1)
    d = api.calls[-1][1]
    assert len(d["dois"]) == 2 and len(d["layout"]["items"]) == 2
    wait_idle(app)
    feed(app, "/download 10.1000/zzz\r")              # an explicit DOI is not part of the journal
    pump(app, 0.1)
    assert "layout" not in api.calls[-1][1]
    wait_idle(app)


def test_tui_settings_are_validated_and_secrets_hidden():
    app, em, api = make()
    feed(app, "/set workers 5\r")
    pump(app, 0.1)
    assert api.saved["download_workers"] == 5
    feed(app, "/set workers 99\r")
    pump(app, 0.1)
    assert api.saved["download_workers"] == 5
    feed(app, "/set scihub off\r")
    pump(app, 0.1)
    assert api.saved["scihub_enable"] is False
    feed(app, "/set core abc123\r")
    pump(app, 0.1)
    assert api.saved["core_key"] == "abc123"
    assert not any("abc123" in x for x in lines(em)) and "/set core abc123" not in app.history
