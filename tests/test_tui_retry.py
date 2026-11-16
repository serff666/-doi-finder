"""Terminal client: what happens when something fails, and how the person gets going again.

Failure is injected into the fake engine: dropped connections, jobs that end in error, rows that
come back unresolved. The point of every test is the same: the session survives, the message says
what happened in plain words, and one key (Ctrl+R) or /retry redoes exactly what failed.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import tui  # noqa: E402
from test_tui import feed, lines, make, pump, wait_idle  # noqa: E402

CTRL_R = "\x12"


def txt(em):
    return "\n".join(lines(em))


def row(inp, status="matched", doi="", **kw):
    return dict({"input": inp, "status": status, "doi": doi, "score": 0.99 if doi else 0, "year": 2020}, **kw)


def plan(results_by_call):
    """lookup_fn that answers the n-th call with the n-th list of rows."""
    state = {"n": 0}

    def fn(text):
        i = min(state["n"], len(results_by_call) - 1)
        state["n"] += 1
        res = results_by_call[i](text) if callable(results_by_call[i]) else results_by_call[i]
        return {"state": "done", "results": res, "done": len(res), "total": len(res)}
    return fn


# ------------------------------------------------------------------ pure helpers
def test_ctrl_r_is_a_key():
    assert tui._Parser().feed("\x12", final=True) == [("key", "ctrl-r")]


def test_friendly_messages():
    assert tui.friendly(TimeoutError("timed out")).startswith("Network problem or timeout")
    assert tui.friendly(RuntimeError("[Errno 101] Network is unreachable")).startswith("Network problem")
    assert tui.friendly(RuntimeError("HTTP 500 boom")) == "HTTP 500 boom"
    assert tui.friendly(RuntimeError("SSL: CERTIFICATE_VERIFY_FAILED")).startswith("Network problem")
    assert tui.friendly(RuntimeError("classless addressing failed")) == "classless addressing failed"   # 'ssl' inside a word
    assert tui.friendly(ValueError()) == "ValueError"


def test_normalize_doi():
    n = tui.normalize_doi
    assert n("10.1021/JACS.1c00001") == "10.1021/jacs.1c00001"
    assert n("https://doi.org/10.1021/abc.1.") == "10.1021/abc.1"
    assert n("http://dx.doi.org/10.1021/abc") == "10.1021/abc"
    assert n("doi: 10.1021/abc") == "10.1021/abc"
    for bad in ("", "abc", "10.12/x", "10.1021/", "10.1021 /x", "1", None):
        assert n(bad) == "", bad


# ------------------------------------------------------------------ a failed request must not end the session
def test_lookup_request_failure_keeps_session_and_offers_retry():
    app, em, api = make(rows=70)
    api.fail_next["/api/lookup"] = 1
    feed(app, "Attention is all you need\r")
    pump(app, 0.2)
    t = txt(em)
    assert "Error" in t and "Network problem or timeout" in t and "Ctrl+R or /retry" in t, t
    assert app.job is None and not app.quit
    assert any("Ctrl+R retries: look up again" in x for x in lines(em)[-4:]), lines(em)[-5:]
    feed(app, CTRL_R)
    wait_idle(app)
    assert [c[1]["text"] for c in api.calls if c[0] == "/api/lookup"] == ["Attention is all you need"] * 2
    assert "Title → DOI" in txt(em) and len(app.last_results) == 3


def test_typing_hides_the_retry_hint_but_keeps_the_retry():
    app, em, api = make(rows=70)
    api.fail_next["/api/lookup"] = 1
    feed(app, "x\r")
    pump(app, 0.2)
    feed(app, "hello")
    assert "Ctrl+R retries" not in lines(em)[-1]
    assert "lookup" in app.retries


def test_download_job_error_offers_retry_and_resends_the_same_dois():
    app, em, api = make(rows=70)
    api.job_error["/api/download"] = "HTTP 500 boom"
    feed(app, "10.1000/a1\r")
    wait_idle(app)
    assert "boom" in txt(em) and "Ctrl+R" in txt(em)
    api.job_error.clear()
    feed(app, CTRL_R)
    pump(app, 0.1)
    assert api.calls[-1] == ("/api/download", {"dois": ["10.1000/a1"]})
    wait_idle(app)
    assert not app.retries


def test_command_failure_is_retryable_but_settings_errors_are_not():
    app, em, api = make(rows=70)
    api.fail_next["/api/search"] = 1
    feed(app, "/search perylene\r")
    wait_idle(app)
    assert "Ctrl+R or /retry" in txt(em) and "search" in app.retries
    feed(app, CTRL_R)
    wait_idle(app)
    assert [c[0] for c in api.calls].count("/api/search") == 2 and "Perylene bisimide" in txt(em)
    assert not app.retries
    # a failing /set is reported without a retry offer
    api.fail_next["/api/settings"] = 1
    feed(app, "/set workers 4\r")
    pump(app, 0.1)
    assert not app.retries


def test_harvest_job_error_is_retryable():
    app, em, api = make(rows=70)
    api.job_error["/api/journal/harvest"] = "HTTP 502"
    feed(app, "/journal 1759-0876\r")
    wait_idle(app)
    assert "HTTP 502" in txt(em) and "journal" in app.retries
    api.job_error.clear()
    feed(app, CTRL_R)
    wait_idle(app)
    assert "Paper 0" in txt(em) and not app.retries


def test_journal_name_resolution_failure_is_retryable():
    app, em, api = make(rows=70)
    api.fail_next["/api/journal/resolve"] = 1
    feed(app, "/journal WIREs Computational Molecular Science\r")
    wait_idle(app)
    assert "journal" in app.retries
    feed(app, CTRL_R)
    wait_idle(app)
    wait_idle(app)
    assert "Paper 0" in txt(em)


# ------------------------------------------------------------------ lookup: only the failed rows are redone
def test_partial_lookup_retry_sends_only_failed_rows_and_keeps_numbering():
    app, em, api = make(rows=80)
    api.lookup_fn = plan([
        [row("Good paper one", "matched", "10.1/g"), row("flaky one", "error", note="network: HTTP 503"),
         row("missing one", "not_found")],
        lambda text: [row("flaky one", "matched", "10.9/f"), row("missing one", "not_found")],
    ])
    feed(app, "\x1b[200~Good paper one\nflaky one\nmissing one\x1b[201~\r")
    wait_idle(app)
    t = txt(em)
    assert "does not mean the paper is missing" in t, t
    assert app.unresolved() == [1, 2]
    assert any("Ctrl+R retries: look up the 2 ✗ rows again" in x for x in lines(em)[-4:])
    feed(app, CTRL_R)
    pump(app, 0.1)
    assert api.calls[-1][1]["text"] == "flaky one\nmissing one"
    wait_idle(app)
    assert [r["doi"] for r in app.last_results] == ["10.1/g", "10.9/f", ""]
    assert app.unresolved() == [2]
    assert app.retries["lookup"]["label"].startswith("look up the 1 ✗")
    assert "retry found 1" in txt(em)
    assert app.default_dois == ["10.1/g", "10.9/f"]
    assert any("2  ✓" in x and "flaky one" in x for x in lines(em)), "row 2 keeps its number"


def test_bibtex_retry_resends_the_whole_text_so_year_and_author_survive():
    bib = ("@article{k1, author = {Smith, Ann}, title = {A title that is long}, year = {2019}}\n"
           "@article{k2, author = {Doe, Jo}, title = {Another long title here}, year = {2020}}")
    app, em, api = make(rows=80)
    api.lookup_fn = plan([[row("A title that is long", "matched", "10.1/a"),
                           row("Another long title here", "error")],
                          [row("A title that is long", "matched", "10.1/a"),
                           row("Another long title here", "matched", "10.1/b")]])
    feed(app, "\x1b[200~" + bib + "\x1b[201~\r")
    wait_idle(app)
    feed(app, CTRL_R)
    pump(app, 0.1)
    assert api.calls[-1][1]["text"] == bib
    wait_idle(app)
    assert not app.unresolved() and "lookup" not in app.retries


def test_row_count_mismatch_never_splices_results_into_the_wrong_rows():
    app, em, api = make(rows=80)
    api.lookup_fn = plan([
        [row("one", "matched", "10.1/1"), row("two", "error"), row("three", "matched", "10.1/3")],
        [row("two", "matched", "10.1/2"), row("surplus", "matched", "10.1/9")],      # wrong shape
    ])
    feed(app, "\x1b[200~one\ntwo\nthree\x1b[201~\r")
    wait_idle(app)
    before = [r["doi"] for r in app.last_results]
    feed(app, CTRL_R)
    wait_idle(app)
    assert [r["doi"] for r in app.last_results] == before
    assert "Row count mismatch" in txt(em) and "lookup" in app.retries


def test_stopping_a_lookup_offers_to_resume_the_rest():
    app, em, api = make(rows=70)
    api.lookup_fn = plan([[row("one", "matched", "10.1/1"), row("two", "stopped")],
                          lambda text: [row("two", "matched", "10.1/2")]])
    feed(app, "\x1b[200~one\ntwo\x1b[201~\r")
    pump(app, 0.05)
    feed(app, "\x1b")
    wait_idle(app)
    assert app.unresolved() == [1] and "partial" in txt(em)
    feed(app, CTRL_R)
    pump(app, 0.1)
    assert api.calls[-1][1]["text"] == "two"
    wait_idle(app)
    assert [r["doi"] for r in app.last_results] == ["10.1/1", "10.1/2"]


# ------------------------------------------------------------------ several things to redo
def test_download_misses_and_lookup_misses_are_kept_apart():
    app, em, api = make(rows=90)
    api.lookup_fn = plan([[row("A", "matched", "10.1/a"), row("B", "matched", "10.1/b"), row("C", "error")],
                          lambda text: [row("C", "matched", "10.1/c")]])
    feed(app, "\x1b[200~A\nB\nC\x1b[201~\r")
    wait_idle(app)
    feed(app, "/download\r")
    wait_idle(app)
    assert set(app.retries) == {"lookup", "download"}
    assert "Ctrl+R retries: download 1 again" in lines(em)[-1] and "(+1" in lines(em)[-1], lines(em)[-1]
    feed(app, "/retry list\r")
    pump(app, 0.1)
    t = txt(em)
    assert "lookup" in t and "download" in t and "Can be retried" in t
    feed(app, "/retry lookup\r")
    pump(app, 0.1)
    assert api.calls[-1][0] == "/api/lookup" and api.calls[-1][1]["text"] == "C"
    wait_idle(app)
    assert list(app.retries) == ["download"]
    feed(app, "/retry nothing\r")
    pump(app, 0.1)
    assert "Nothing to retry under that name" in txt(em)


def test_ctrl_r_with_nothing_to_retry_and_during_a_job():
    app, em, api = make(rows=70)
    feed(app, CTRL_R)
    pump(app, 0.05)
    assert "Nothing to retry" in txt(em)
    feed(app, "10.1000/a1\r")
    pump(app, 0.1)
    feed(app, CTRL_R)
    pump(app, 0.05)
    assert "A job is running" in txt(em)
    wait_idle(app)


def test_stopped_download_is_offered_as_resume():
    app, em, api = make(rows=70)
    feed(app, "\x1b[200~10.1000/a1\n10.1000/a2\x1b[201~\r")
    pump(app, 0.15)
    feed(app, "\x1b")
    wait_idle(app)
    assert app.last_failed == ["10.1000/a2"], app.last_failed
    assert "resume the remaining 1" in app.retries["download"]["label"]
    assert "stopped" in txt(em).lower() or "–" in txt(em)


# ------------------------------------------------------------------ the loop itself must not die
def test_a_key_that_raises_does_not_end_the_session():
    app, em, api = make(rows=40)
    real = app.on_token

    def boom(kind, val):
        if (kind, val) == ("key", "ctrl-k"):
            raise ValueError("boom")
        return real(kind, val)
    app.on_token = boom

    class Keys:
        def __init__(self):
            self.batches = [[("key", "ctrl-k")], [("key", "ctrl-d")]]

        def read(self, timeout):
            return self.batches.pop(0) if self.batches else [("key", "ctrl-d")]
    app.run(Keys())
    assert "boom" in txt(em) and app.quit


def test_garbage_from_the_engine_ends_the_job_instead_of_hanging():
    app, em, api = make(rows=60)
    api.get_hook = lambda path: []                  # not a dict: .get() raises inside the poll thread
    feed(app, "10.1000/a1\r")
    wait_idle(app, limit=4)
    assert "Error" in txt(em) and app.job is None


# ------------------------------------------------------------------ /fix
FIX_ROWS = [
    row("Some uncertain paper", "uncertain", "10.1/x", score=0.71, alts=[
        {"doi": "10.1/alt1", "title": "Alt one", "year": 2018, "journal": "J A", "score": 0.69,
         "source": "crossref", "first_author": "Ann Smith"},
        {"doi": "10.1/ALT2", "title": "Alt two", "year": 2017, "journal": "J B", "score": 0.5,
         "source": "openalex", "first_author": "Bo Chen"}]),
    row("Missing paper", "not_found"),
]


def fix_app():
    app, em, api = make(rows=90)
    api.lookup_fn = plan([[dict(r) for r in FIX_ROWS]])
    feed(app, "\x1b[200~Some uncertain paper\nMissing paper\x1b[201~\r")
    wait_idle(app)
    return app, em, api


def test_fix_refuses_without_a_lookup_list_or_with_bad_numbers():
    app, em, api = make(rows=60)
    feed(app, "/fix 1\r")
    pump(app, 0.05)
    assert "/fix works on a Title → DOI list" in txt(em)
    feed(app, "/search perylene\r")
    wait_idle(app)
    feed(app, "/fix 1\r")
    pump(app, 0.05)
    assert txt(em).count("/fix works on a Title → DOI list") == 2
    app, em, api = fix_app()
    for bad in ("/fix\r", "/fix x\r", "/fix 0\r", "/fix 3\r"):
        feed(app, bad)
        pump(app, 0.05)
    assert txt(em).count("Usage: /fix N") == 4


def test_fix_lists_alternatives_with_first_author():
    app, em, api = fix_app()
    feed(app, "/fix 1\r")
    pump(app, 0.05)
    t = txt(em)
    assert "Row 1" in t and "Alt one" in t and "Ann Smith" in t and "Bo Chen" in t and "10.1/alt1" in t
    feed(app, "/fix 2\r")
    pump(app, 0.05)
    assert "No alternatives on record." in txt(em) and "no DOI" in txt(em)


def test_fix_picks_an_alternative_and_normalises_a_typed_doi():
    app, em, api = fix_app()
    assert app.default_dois == []                    # uncertain and not_found are not defaults
    feed(app, "/fix 1 2\r")
    pump(app, 0.05)
    assert app.last_results[0]["doi"] == "10.1/alt2" and app.last_results[0]["status"] == "provided"
    assert app.last_results[0]["year"] == 2017 and app.listing[0]["status"] == "provided"
    assert app.default_dois == ["10.1/alt2"]
    assert app.unresolved() == [1] and "lookup" in app.retries
    feed(app, "/fix 2 https://doi.org/10.5555/ABC.1.\r")
    pump(app, 0.05)
    assert app.last_results[1]["doi"] == "10.5555/abc.1" and app.default_dois == ["10.1/alt2", "10.5555/abc.1"]
    assert "lookup" not in app.retries                 # nothing left to retry
    assert "was not checked online" in txt(em)


def test_fix_rejects_bad_input_and_changes_nothing():
    app, em, api = fix_app()
    before = [dict(r) for r in app.last_results]
    for cmd in ("/fix 1 notadoi", "/fix 1 5", "/fix 2 10.12/x"):
        feed(app, cmd + "\r")
        pump(app, 0.05)
    assert [r["doi"] for r in app.last_results] == [r["doi"] for r in before]
    assert "That is not a DOI" in txt(em) and "There is no alternative 5" in txt(em)


def test_fix_can_clear_a_row_and_the_download_skips_it():
    app, em, api = fix_app()
    feed(app, "/fix 1 2\r")
    feed(app, "/fix 1 -\r")
    pump(app, 0.05)
    assert app.last_results[0]["doi"] == "" and app.last_results[0]["status"] == "not_found"
    assert app.default_dois == [] and "Cleared" in txt(em)
    assert app.unresolved() == [0, 1]


def test_a_fixed_row_is_not_retried_again():
    app, em, api = make(rows=80)
    api.lookup_fn = plan([[row("one", "error"), row("two", "error")],
                          lambda text: [row("two", "matched", "10.1/2")]])
    feed(app, "\x1b[200~one\ntwo\x1b[201~\r")
    wait_idle(app)
    feed(app, "/fix 1 10.7777/hand\r")
    pump(app, 0.05)
    feed(app, CTRL_R)
    pump(app, 0.1)
    assert api.calls[-1][1]["text"] == "two"           # row 1 was set by hand, so only row 2 goes again
    wait_idle(app)
    assert [r["doi"] for r in app.last_results] == ["10.7777/hand", "10.1/2"]


# ------------------------------------------------------------------ stale retries must not linger
def test_a_new_action_retires_the_old_retry_of_its_kind():
    app, em, api = make(rows=90)
    # download: a2 misses, then a fresh download of one DOI succeeds -> nothing left to redo
    feed(app, "\x1b[200~10.1000/a1\n10.1000/a2\x1b[201~\r")
    wait_idle(app)
    assert "download" in app.retries
    feed(app, "10.1000/a3\r")
    wait_idle(app)
    assert "download" not in app.retries
    # search: a failed search, then a successful one
    api.fail_next["/api/search"] = 1
    feed(app, "/search first\r")
    wait_idle(app)
    assert "search" in app.retries
    feed(app, "/search second\r")
    wait_idle(app)
    assert "search" not in app.retries
    # lookup: unresolved rows, then a new list that resolves completely
    api.lookup_fn = plan([[row("A", "error")], [row("B", "matched", "10.1/b")]])
    feed(app, "\x1b[200~A\nB\x1b[201~\r")
    wait_idle(app)
    assert "lookup" in app.retries
    feed(app, "\x1b[200~new list\nanother\x1b[201~\r")
    wait_idle(app)
    assert "lookup" not in app.retries
    # journal: failed harvest, then a good one
    api.job_error["/api/journal/harvest"] = "HTTP 502"
    feed(app, "/journal 1759-0876\r")
    wait_idle(app)
    assert "journal" in app.retries
    api.job_error.clear()
    feed(app, "/journal 1759-0876\r")
    wait_idle(app)
    assert "journal" not in app.retries


def test_wrap_plain_and_blocks():
    import tui
    for w in (10, 20, 40):
        for s in ("  hello world " * 6, "长长的中文提示" * 9, "x" * 90, "a b  c"):
            out = tui.wrap_plain(s, w)
            assert out and all(tui.width(l) <= max(8, w) for l in out), (w, s, out)
            assert "".join(l.strip() for l in out).replace(" ", "") == s.replace(" ", ""), (w, s)
    assert tui.wrap_plain("  a b", 40) == ["  a b"]
    assert tui.wrap_plain("", 10) == [""]
    # a long error never overflows the terminal and keeps its whole text
    app, em, api = make(cols=40, rows=30)
    api.fail_next["/api/search"] = 1
    feed(app, "/search x\r"); wait_idle(app)
    ls = lines(em)
    assert all(tui.width(l) <= 40 for l in ls), ls
    assert "Ctrl+R or /retry to retry: search" in " ".join(l.strip() for l in ls)
