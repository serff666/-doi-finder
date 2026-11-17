"""Desktop shell (desktop.py): dialogs, window state, close guard, single instance, and the server hooks.

pywebview is replaced by a fake so this runs anywhere (no GUI needed)."""
import base64
import json
import os
import re
import sys
import threading
import types
import urllib.request

import pytest

import desktop
import platform_util
import server


# ---- fakes ---------------------------------------------------------------------------------------
class FakeDialog:
    OPEN, FOLDER, SAVE = 10, 20, 30


class FakeWindow:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []
        self.width, self.height, self.x, self.y = 1300, 850, 40, 60
        self.confirm = True
        self.destroyed = False
        self.log = []

    def create_file_dialog(self, kind, **kw):
        self.calls.append((kind, kw))
        return self.answers.pop(0) if self.answers else None

    def create_confirmation_dialog(self, title, message):
        self.log.append(("confirm", message))
        if isinstance(self.confirm, Exception):
            raise self.confirm
        return self.confirm

    def destroy(self):
        self.destroyed = True

    def restore(self):
        self.log.append("restore")

    def show(self):
        self.log.append("show")

    def evaluate_js(self, js):
        self.log.append(("js", js))

    @property
    def on_top(self):
        return False

    @on_top.setter
    def on_top(self, v):
        self.log.append(("on_top", v))


def make_shell(answers=None):
    sh = desktop.Shell(types.SimpleNamespace(FileDialog=FakeDialog))
    sh.window = FakeWindow(answers)
    return sh


# ---- file type filters ---------------------------------------------------------------------------
# the exact pattern pywebview validates against (webview.util.parse_file_type)
PYWEBVIEW_FILTER = re.compile(r'^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*(?:\.(?:\w+|\*))*)*)\)$')


def test_file_filter_is_what_pywebview_accepts():
    got = desktop.file_filter((("PDF / CAJ", "*.pdf;*.caj"), ("PDF", "*.pdf"), ("选择 PDF", "*.pdf *.PDF")))
    assert got == ["PDF CAJ (*.pdf;*.caj)", "PDF (*.pdf)", "选择 PDF (*.pdf;*.PDF)", "All files (*.*)"]
    assert all(PYWEBVIEW_FILTER.match(f) for f in got)


def test_file_filter_drops_patterns_it_cannot_express():
    assert desktop.file_filter((("Weird", "a?b; [x]"), ("!!!", "*.txt"))) == ["Files (*.txt)", "All files (*.*)"]
    assert desktop.file_filter(()) == ["All files (*.*)"]


def test_safe_filename():
    assert desktop.safe_filename('../../evil:"name".csv') == "evil_name_.csv"
    assert desktop.safe_filename("   ") == "export"
    assert desktop.safe_filename("DOI检索结果_2026.xlsx") == "DOI检索结果_2026.xlsx"


# ---- window geometry -----------------------------------------------------------------------------
def test_geometry_round_trip(tmp_path):
    p = str(tmp_path / "w.json")
    w = FakeWindow()
    desktop.save_geometry(w, p)
    assert desktop.load_geometry(p) == {"width": 1300, "height": 850, "x": 40, "y": 60}


def test_geometry_is_sanitised(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"width": 300, "height": 99999, "x": -32000, "y": -32000}))
    g = desktop.load_geometry(str(p))
    assert (g["width"], g["height"]) == desktop.DEFAULT_SIZE and g["x"] is None and g["y"] is None
    p.write_text("not json")
    assert desktop.load_geometry(str(p))["width"] == desktop.DEFAULT_SIZE[0]
    assert desktop.load_geometry(str(tmp_path / "missing.json"))["x"] is None


def test_minimised_window_does_not_overwrite_the_record(tmp_path):
    p = str(tmp_path / "w.json")
    desktop.save_geometry(FakeWindow(), p)
    w = FakeWindow(); w.x = w.y = -32000
    desktop.save_geometry(w, p)
    assert desktop.load_geometry(p)["x"] == 40


# ---- pickers -------------------------------------------------------------------------------------
def test_folder_picker_uses_the_window(tmp_path):
    sh = make_shell([[str(tmp_path)]])
    assert sh.pick("folder", "t", str(tmp_path)) == [str(tmp_path)]
    kind, kw = sh.window.calls[0]
    assert kind == FakeDialog.FOLDER and kw["directory"] == str(tmp_path)


def test_folder_picker_ignores_a_missing_start_folder():
    sh = make_shell([None])
    assert sh.pick("folder", "t", "/no/such/dir") == []
    assert sh.window.calls[0][1]["directory"] == ""


def test_file_picker_multi_and_filters():
    sh = make_shell([("a.pdf", "b.pdf"), "c.pdf"])
    assert sh.pick("file", "t", patterns=(("PDF", "*.pdf"),), multi=True) == ["a.pdf", "b.pdf"]
    kind, kw = sh.window.calls[0]
    assert kind == FakeDialog.OPEN and kw["allow_multiple"] is True and kw["file_types"] == ("PDF (*.pdf)", "All files (*.*)")
    assert sh.pick("file", "t") == ["c.pdf"]               # a bare string answer is a list of one


def test_platform_util_routes_dialogs_through_the_shell():
    seen = []
    platform_util.set_picker(lambda kind, title, initial, patterns, multi: seen.append((kind, multi)) or ["/p/x.pdf"])
    try:
        assert platform_util.pick_folder("t", "/i") == "/p/x.pdf"
        assert platform_util.pick_files("t", (("PDF", "*.pdf"),), multi=True) == ["/p/x.pdf"]
        assert seen == [("folder", False), ("file", True)]
    finally:
        platform_util.set_picker(None)
    assert platform_util._PICKER is None


# ---- save as, reveal -----------------------------------------------------------------------------
def test_save_file_writes_through_the_native_dialog(tmp_path, monkeypatch):
    target = tmp_path / "out.csv"
    sh = make_shell([str(target)])
    monkeypatch.setattr(server, "load_settings", lambda: {"download_dir": str(tmp_path)})
    api = desktop.Api(sh)
    r = api.save_file("结果.csv", base64.b64encode("a,b\n1,2".encode()).decode())
    assert r == {"path": str(target)} and target.read_text() == "a,b\n1,2"
    kind, kw = sh.window.calls[0]
    assert kind == FakeDialog.SAVE and kw["save_filename"] == "结果.csv" and kw["directory"] == str(tmp_path)
    opened = []
    monkeypatch.setattr(platform_util, "open_path", opened.append)
    assert api.reveal(str(target)) == {"ok": True} and opened == [str(tmp_path)]
    assert "error" in api.reveal(str(tmp_path / "other.txt"))   # only files this window saved


def test_save_cancelled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "load_settings", lambda: {"download_dir": ""})
    api = desktop.Api(make_shell([None]))
    assert api.save_file("x.csv", base64.b64encode(b"x").decode()) == {"path": ""}
    assert list(tmp_path.iterdir()) == []


def test_api_exposes_only_what_the_page_needs():
    public = sorted(n for n in dir(desktop.Api) if not n.startswith("_"))
    assert public == ["info", "quit", "reveal", "save_file"]


# ---- close guard, focus, drop --------------------------------------------------------------------
def test_close_without_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_state_path", lambda: str(tmp_path / "w.json"))
    monkeypatch.setattr(server, "running_jobs", lambda: [])
    sh = make_shell()
    assert sh.on_closing() is True and not sh.window.log
    assert os.path.isfile(tmp_path / "w.json")              # geometry saved on the way out


def test_close_with_running_jobs_asks(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_state_path", lambda: str(tmp_path / "w.json"))
    monkeypatch.setattr(server, "running_jobs", lambda: [{"id": "a"}, {"id": "b"}])
    sh = make_shell()
    sh.window.confirm = False
    assert sh.on_closing() is False
    assert "2" in sh.window.log[0][1]
    sh.window.confirm = RuntimeError("no dialog")
    assert sh.on_closing() is True                           # never trap the user in the window


def test_running_jobs_counts_running_and_paused(monkeypatch):
    monkeypatch.setattr(server, "JOBS", {"a": {"state": "running", "kind": "download", "done": 1, "total": 3},
                                         "b": {"state": "paused", "kind": "harvest"}, "c": {"state": "done"}})
    assert [j["id"] for j in server.running_jobs()] == ["a", "b"]


def test_focus_brings_the_window_forward():
    sh = make_shell()
    sh.focus()
    assert sh.window.log == ["restore", "show", ("on_top", True), ("on_top", False)]


def test_drop_reports_real_paths_to_the_page():
    sh = make_shell()
    sh.on_drop({"dataTransfer": {"files": [{"name": "a.pdf", "pywebviewFullPath": "C:\\p\\a.pdf"}, {"name": "x"}]}})
    js = sh.window.log[0][1]
    assert "doifinder:drop-paths" in js and json.dumps(["C:\\p\\a.pdf"]) in js
    sh.window.log.clear()
    sh.on_drop({"dataTransfer": {"files": []}})
    assert sh.window.log == []


# ---- server hooks over HTTP ----------------------------------------------------------------------
@pytest.fixture()
def live(monkeypatch):
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr(server, "APP_HOOKS", {})
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()


def _post(url, path):
    req = urllib.request.Request(url + path, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def test_health_reports_the_shell(live, monkeypatch):
    with urllib.request.urlopen(live + "api/health", timeout=5) as r:
        assert json.loads(r.read().decode())["shell"] == "browser"
    monkeypatch.setattr(server, "SHELL", "desktop")
    with urllib.request.urlopen(live + "api/health", timeout=5) as r:
        assert json.loads(r.read().decode())["shell"] == "desktop"


def test_focus_endpoint(live):
    assert _post(live, "api/app/focus") == {"ok": False}            # no window registered
    hits = []
    server.APP_HOOKS["focus"] = lambda: hits.append(1)
    assert _post(live, "api/app/focus") == {"ok": True} and hits == [1]
    assert desktop._focus_existing(live) is True
    server.APP_HOOKS["focus"] = lambda: 1 / 0
    assert _post(live, "api/app/focus")["ok"] is False


def test_quit_closes_the_window_when_there_is_one(live, monkeypatch):
    done = threading.Event()
    server.APP_HOOKS["quit"] = done.set
    monkeypatch.setattr(server, "shutdown", lambda: pytest.fail("server must not be stopped directly"))
    assert _post(live, "api/quit") == {"ok": True}
    assert done.wait(3)


# ---- start-up --------------------------------------------------------------------------------------
def test_without_pywebview_it_falls_back_to_the_browser_window(monkeypatch):
    called = []
    monkeypatch.setitem(sys.modules, "webview", None)       # "import webview" raises ImportError
    monkeypatch.delenv("DOIFINDER_NO_BROWSER", raising=False)
    monkeypatch.setattr(sys, "argv", ["desktop.py"])
    monkeypatch.setattr(server, "main", lambda: called.append(list(sys.argv)))
    desktop.main()
    assert called == [["desktop.py", "--open"]]              # ... and server.main opens the browser window


def test_headless_runs_only_the_server(monkeypatch):
    called = []
    monkeypatch.setenv("DOIFINDER_NO_BROWSER", "1")
    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(create_window=lambda *a, **k: pytest.fail("no window")))
    monkeypatch.setattr(server, "main", lambda: called.append(1))
    desktop.main()
    assert called == [1]


def test_second_launch_focuses_the_running_window(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace())
    monkeypatch.delenv("DOIFINDER_PORT", raising=False)
    monkeypatch.delenv("DOIFINDER_NO_BROWSER", raising=False)
    monkeypatch.setattr(server, "already_running", lambda: "http://127.0.0.1:8765/")
    seen = []
    monkeypatch.setattr(desktop, "_focus_existing", lambda url: seen.append(url) or True)
    monkeypatch.setattr(server, "show_ui", lambda url: pytest.fail("should focus, not open another window"))
    monkeypatch.setattr(server, "start", lambda port=0: pytest.fail("must not start a second server"))
    desktop.main()
    assert seen == ["http://127.0.0.1:8765/"]


def test_window_is_wired_up(monkeypatch, tmp_path):
    """main() with a fake pywebview: window options, hooks, picker, and clean-up after the window closes."""
    created, started = {}, {}

    class Ev(list):
        def __iadd__(self, fn):
            self.append(fn); return self

    class Win(FakeWindow):
        def __init__(self):
            super().__init__()
            self.events = types.SimpleNamespace(closing=Ev(), closed=Ev(), loaded=Ev())

    def create_window(title, url, **kw):
        created.update(kw, title=title, url=url)
        created["win"] = Win()
        return created["win"]

    def start(**kw):
        started.update(kw)
        assert platform_util._PICKER is not None and set(server.APP_HOOKS) == {"focus", "quit"}

    fake = types.SimpleNamespace(create_window=create_window, start=start, settings={}, FileDialog=FakeDialog)
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setenv("DOIFINDER_PORT", "1")
    monkeypatch.delenv("DOIFINDER_NO_BROWSER", raising=False)
    monkeypatch.setattr(server, "APP_HOOKS", {})
    monkeypatch.setattr(server, "DATA", str(tmp_path))
    monkeypatch.setattr(server, "start", lambda port=0: "http://127.0.0.1:1/")
    monkeypatch.setattr(server, "serve", lambda: None)
    stops = []
    monkeypatch.setattr(server, "shutdown", lambda: stops.append(1))
    try:
        desktop.main()
    finally:
        platform_util.set_picker(None)
        server.SHELL = "browser"
    assert created["url"] == "http://127.0.0.1:1/" and created["text_select"] is True and created["min_size"] == desktop.MIN_SIZE
    assert isinstance(created["js_api"], desktop.Api)
    w = created["win"]
    assert len(w.events.closing) == len(w.events.closed) == len(w.events.loaded) == 1
    assert started["private_mode"] is False and started["storage_path"] == os.path.join(str(tmp_path), "webview")
    assert fake.settings["ALLOW_DOWNLOADS"] is True
    assert stops == [1]                                      # the server stops when the window is gone
