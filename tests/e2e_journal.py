"""End-to-end journal flow on a stubbed Crossref (no network needed).

harvest (real HTTP API) -> filter front matter / issue records -> download with a
journal layout -> files land in <Journal>/<Year>/Volume V, Issue I/.
Run:  python tests/e2e_journal.py
"""
import json, os, sys, tempfile, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = tempfile.mkdtemp(); OUT = tempfile.mkdtemp()
os.environ["DOIFINDER_DATA"] = DATA
os.environ["DOIFINDER_NO_BROWSER"] = "1"

JOURNAL = "WIREs Computational Molecular Science"
ISSN = "1759-0876"


def mk():
    recs = []
    for year, vol in ((2023, 13), (2024, 14)):
        for issue in range(1, 7):
            recs.append({"DOI": f"10.1111/wcms.v{vol}.{issue}", "type": "journal-issue",
                         "volume": str(vol), "issue": str(issue), "published": {"date-parts": [[year, issue * 2 - 1]]}})
            recs.append({"DOI": f"10.1002/wcms.{vol}{issue}00", "type": "journal-article", "title": ["Issue Information"],
                         "volume": str(vol), "issue": str(issue), "published": {"date-parts": [[year, issue * 2 - 1]]}})
            for n in range(1, 5):
                recs.append({"DOI": f"10.1002/wcms.{vol}{issue}{n:02d}", "type": "journal-article",
                             "title": [f"Paper {vol}.{issue}.{n}: polarizable continuum"], "container-title": [JOURNAL],
                             "author": [{"given": "Ada", "family": "Lovelace"}],
                             "volume": str(vol), "issue": str(issue), "page": f"e{n}",
                             "published": {"date-parts": [[year, issue * 2 - 1, n]]}})
    return recs


RECS = mk()
SEEN_QS = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        SEEN_QS.append(q)
        flt = dict(x.split(":", 1) for x in q.get("filter", [""])[0].split(",") if ":" in x)
        y0 = int(flt.get("from-pub-date", "0001")[:4]); y1 = int(flt.get("until-pub-date", "9999")[:4])
        rows = [r for r in RECS if y0 <= r["published"]["date-parts"][0][0] <= y1
                and ("type" not in flt or r["type"] == flt["type"])]
        off = int(q.get("offset", ["0"])[0]); n = min(int(q.get("rows", ["20"])[0]), 10)  # force paging
        body = json.dumps({"status": "ok", "message": {"total-results": len(rows), "items": rows[off:off + n]}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)


fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=fake.serve_forever, daemon=True).start()
os.environ["DOIFINDER_CROSSREF"] = f"http://127.0.0.1:{fake.server_address[1]}"

import server, downloader  # noqa: E402

server.save_settings({"download_dir": OUT})


def fake_download_one(doi, dest, email, template, index, force=False, *a, **k):
    os.makedirs(dest, exist_ok=True)
    name = doi.replace("/", "_") + ".pdf"
    open(os.path.join(dest, name), "wb").write(b"%PDF-1.4 fake")
    index[doi] = {"file": name, "title": doi}
    return {"doi": doi, "status": "downloaded", "file": name, "url": "", "source": "test", "note": "", "title": "", "year": ""}


downloader.download_one = fake_download_one
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{httpd.server_address[1]}"


def call(path, body=None):
    req = urllib.request.Request(B + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Origin": B})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def wait(jid, key="state"):
    for _ in range(200):
        j = call("/api/job/" + jid)
        if j.get("state") in ("done", "error"):
            return j
        time.sleep(0.1)
    raise SystemExit("job timed out")


job = wait(call("/api/journal/harvest", {"issn": ISSN, "year_from": 2023, "year_to": 2024, "kind": "全部类型"})["job_id"])
items = job["items"]
assert job["state"] == "done", job
assert len(items) == 48, len(items)                      # 2 years x 6 issues x 4 papers; no issue records, no front matter
assert all(i["kind"] == "review" for i in items), {i["kind"] for i in items}   # a reviews-only journal
assert not job.get("errors"), job["errors"]
assert all("select" in q for q in SEEN_QS), "harvest must request a trimmed field list"
assert not any("全部" in json.dumps(q) for q in SEEN_QS)

dois = [i["doi"] for i in items]
res = wait(call("/api/download", {"dois": dois, "dir": OUT, "workers": 3,
                                  "layout": {"journal": JOURNAL, "items": items}})["job_id"])
assert res["state"] == "done", res
got = sorted(os.path.relpath(os.path.join(d, f), OUT).replace(os.sep, "/") for d, _, fs in os.walk(OUT) for f in fs if f.endswith(".pdf"))
assert len(got) == 48, len(got)
dirs = sorted({os.path.dirname(g) for g in got})
assert dirs[0] == f"{JOURNAL}/2023/Volume 13, Issue 1", dirs[0]
assert len(dirs) == 12, dirs
print("E2E OK:", len(items), "articles ->", len(dirs), "folders; e.g.", dirs[0], "|", dirs[-1])
