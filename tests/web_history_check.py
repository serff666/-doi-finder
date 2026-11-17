"""Real server + real UI in headless Chrome: a task saved on disk comes back on its page after a restart.

Seeds history records through the API, opens the UI, and checks that each page shows its history entry and
re-displays the saved rows.  Skipped when Chrome is not installed.  Run:  python tests/web_history_check.py
"""
import html as _h
import json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_path import find_chrome, run_chrome  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN_ZH = "df_lang"


def post(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def get(base, path):
    return json.load(urllib.request.urlopen(base + path, timeout=10))


def run() -> int:
    chrome = find_chrome()
    if not chrome:
        print("SKIP: no Chrome"); return 0
    data = tempfile.mkdtemp(); prof = tempfile.mkdtemp()
    port = 18000 + os.getpid() % 1000
    env = dict(os.environ, DOIFINDER_DATA=data, DOIFINDER_PORT=str(port), DOIFINDER_NO_BROWSER="1")
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    bad = 0
    try:
        for _ in range(50):
            try:
                get(base, "/api/history?kind=lab"); break
            except Exception:
                time.sleep(0.2)
        rows = [{"title": f"Paper {i}", "doi": f"10.1000/x{i}", "year": 2020 + i} for i in range(3)]
        a = post(base, "/api/history/save", {"kind": "lab", "id": None, "title": "lab page · 3 篇", "summary": "共 3 条",
                                             "payload": {"rows": rows, "sel": [0, 1], "url": "https://example.edu/pubs"}})
        post(base, "/api/history/save", {"kind": "search", "id": None, "title": "perylene · 3 篇", "summary": "共 3 条",
                                         "payload": {"rows": [{"title": "S1", "doi": "10.1/s1"}], "sel": [], "q": "perylene"}})
        post(base, "/api/history/save", {"kind": "rg", "id": None, "title": "old", "summary": "", "payload": {"rows": [{"title": "R1", "url": "u"}], "sel": []}})
        ok = a.get("id") and len(get(base, "/api/history?kind=lab")["items"]) == 1
        print(("ok   " if ok else "FAIL ") + "API saves and lists a record"); bad += 0 if ok else 1

        r = run_chrome([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--lang=zh-CN",
                            "--virtual-time-budget=20000", "--dump-dom", base + "/#lab"], timeout=90)
        dom = r.stdout
        res = {
            "hist": {k: len(re.findall(r'<div class="hist-row', (re.search(r'id="hist-%s".*?</details>' % k, dom, re.S) or [""])[0]))
                     for k in ("lab", "search", "rg", "lookup")},
            "dom": ("Paper 1" in dom, "S1" in dom, 'value="https://example.edu/pubs"' in dom, 'value="perylene"' in dom),
        }
        checks = [
            ("lab page lists its saved task", res["hist"]["lab"] == 1),
            ("search page lists its saved task", res["hist"]["search"] == 1),
            ("rg page lists its saved task", res["hist"]["rg"] == 1),
            ("a page without history shows none", res["hist"]["lookup"] == 0),
            ("newest task reopens by itself: lab rows", res["dom"][0]),
            ("newest task reopens by itself: search rows", res["dom"][1]),
        ]
        for name, good in checks:
            print(("ok   " if good else "FAIL ") + name + ("" if good else "  -> " + json.dumps(res)))
            bad += 0 if good else 1
        d = post(base, "/api/history/delete", {"kind": "rg", "id": get(base, "/api/history?kind=rg")["items"][0]["id"]})
        ok = get(base, "/api/history?kind=rg")["items"] == []
        print(("ok   " if ok else "FAIL ") + "delete removes the record"); bad += 0 if ok else 1
    finally:
        srv.terminate(); shutil.rmtree(prof, ignore_errors=True); shutil.rmtree(data, ignore_errors=True)
    return 1 if bad else 0


def test_web_history():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
