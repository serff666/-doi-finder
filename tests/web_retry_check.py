"""Drives the real web UI in headless Chrome against a scripted fake backend and checks the
"retry the failed ones" buttons (lookup and download), the row splicing, and poll reconnects.

Skipped when Chrome is not installed.  Run:  python tests/web_retry_check.py
"""
import json, os, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

FAKE = r"""
<script>
window.__log = [];
(function () {
  const calls = { job: {} };
  const row = (n, input, status, doi) => ({ n, input, title: input, status, doi: doi || "", score: 1, note: status === "error" ? "network" : "" });
  const jobs = {
    L1: { state: "done", done: 4, total: 4, summary: "x", results: [
      row(1, "Alpha paper", "matched", "10.1000/alpha"), row(2, "Beta paper", "error"),
      row(3, "Gamma paper", "not_found"), row(4, "Delta paper", "error")] },
    L2: { state: "done", done: 2, total: 2, results: [row(1, "Beta paper", "matched", "10.1000/beta"), row(2, "Delta paper", "error")] },
    L3: { state: "done", done: 1, total: 1, results: [row(1, "Delta paper", "matched", "10.1000/delta")] },
    D1: { state: "done", done: 3, total: 3, summary: "s", dest: "/d", results: [
      { doi: "10.1000/a", title: "A", status: "downloaded", file: "a.pdf" }, { doi: "10.1000/b", title: "B", status: "failed", note: "boom" },
      { doi: "10.1000/c", title: "C", status: "no_oa" }] },
    D2: { state: "done", done: 1, total: 1, summary: "s2", dest: "/d", results: [{ doi: "10.1000/b", title: "B", status: "downloaded", file: "b.pdf" }] },
  };
  const order = { lookup: ["L1", "L2", "L3"], download: ["D1", "D2"] };
  const used = { lookup: 0, download: 0 };
  let flaky = 0;
  const json = (o, ok = true, status = 200) => Promise.resolve(new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json" } }));
  window.fetch = (url, opt) => {
    const u = String(url), body = opt && opt.body ? JSON.parse(opt.body) : null;
    window.__log.push([u, body]);
    if (u === "/api/lookup" || u === "/api/download") {
      const k = u.slice(5), id = order[k][used[k]++];
      return json({ job_id: id });
    }
    let m = u.match(/^\/api\/job\/(\w+)/);
    if (m) {
      if (m[1] === "L2" && flaky < 2) { flaky++; return Promise.reject(new TypeError("Failed to fetch")); }   // two dropped polls
      return json(jobs[m[1]]);
    }
    return json({});
  };
})();
</script>
"""

DRIVER = r"""
<script>
(async () => {
  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (f, what) => { for (let i = 0; i < 200; i++) { if (f()) return; await sleep(50); } throw new Error("timeout: " + what); };
  const out = { checks: [], err: null };
  const ok = (name, cond, extra) => out.checks.push([name, !!cond, extra === undefined ? "" : String(extra)]);
  try {
    await sleep(300);
    $("titles").value = "Alpha paper\nBeta paper\nGamma paper\nDelta paper";
    $("titles").dispatchEvent(new Event("input"));
    ok("retry hidden before any run", $("retryLookup").hidden);
    $("start").click();
    await until(() => $("runStatus").textContent.startsWith("检索结束"), "first lookup");
    ok("retry visible, counts 2 failed", !$("retryLookup").hidden && $("retryLookup").textContent.includes("2"), $("retryLookup").textContent);
    $("retryLookup").click();
    await until(() => /重试完成/.test($("runStatus").textContent), "retry 1");
    const calls = window.__log.filter((c) => c[0] === "/api/lookup");
    ok("retry sends only failed lines", calls[1][1].text === "Beta paper\nDelta paper", JSON.stringify(calls[1][1].text));
    ok("retry sends dedup=false", calls[1][1].dedup === false);
    ok("polls reconnect after drops", /重试完成/.test($("runStatus").textContent), $("runStatus").textContent);
    const rowsTxt = () => [...document.querySelectorAll("#lookupRoot tbody tr")].map((r) => r.textContent.replace(/\s+/g, " "));
    let t = rowsTxt();
    ok("Beta spliced in place", /Beta paper.*10\.1000\/beta/.test(t[1]), t[1]);
    ok("Gamma untouched (not_found stays)", /Gamma paper/.test(t[2]) && !/10\.1000/.test(t[2]), t[2]);
    ok("Delta still failed, still 1 retry", $("retryLookup").textContent.includes("1"), $("retryLookup").textContent);
    $("retryLookup").click();
    await until(() => $("retryLookup").hidden, "retry 2 done");
    t = rowsTxt();
    ok("Delta fixed", /Delta paper.*10\.1000\/delta/.test(t[3]), t[3]);
    ok("row numbers kept", t.map((x) => x.trim().split(" ")[0]).join(",") === "1,2,3,4", t.join("|"));
    $("exportCsv").click();
    await until(() => window.__log.some((c) => c[0] === "/api/export/csv"), "export");
    const ex = window.__log.find((c) => c[0] === "/api/export/csv")[1].results;
    ok("export keeps original row numbers and DOIs", ex.map((r) => r.n).join(",") === "1,2,3,4" && ex[1].doi === "10.1000/beta" && ex[3].doi === "10.1000/delta", JSON.stringify(ex.map((r) => [r.n, r.doi])));
    ok("all-clear message", /全部查完/.test($("runStatus").textContent), $("runStatus").textContent);

    document.querySelector('[data-tab="download"]').click();
    $("dois").value = "10.1000/a\n10.1000/b\n10.1000/c";
    $("dois").dispatchEvent(new Event("input"));
    $("startDownload").click();
    await until(() => /^完成/.test($("dlStatus").textContent), "download 1");
    ok("download retry visible for the 1 failed", !$("retryDownload").hidden && $("retryDownload").textContent.includes("1"), $("retryDownload").textContent);
    $("retryDownload").click();
    await until(() => /重试完成/.test($("dlStatus").textContent), "download retry");
    const dcalls = window.__log.filter((c) => c[0] === "/api/download");
    ok("download retry sends only the failed DOI", JSON.stringify(dcalls[1][1].dois) === '["10.1000/b"]', JSON.stringify(dcalls[1][1].dois));
    const dt = [...document.querySelectorAll("#dlRoot tbody tr")].map((r) => r.textContent.replace(/\s+/g, " "));
    ok("download rows keep order, b now downloaded", dt.length === 3 && /10\.1000\/b.*(已下载|Downloaded)/.test(dt[1]) && /10\.1000\/a.*(已下载|Downloaded)/.test(dt[0]) && /10\.1000\/c.*(无开放获取|No open access)/.test(dt[2]), dt.join("|"));
    ok("download retry hidden when nothing failed", $("retryDownload").hidden);
  } catch (e) { out.err = String(e && e.message || e) + " | dlStatus=" + $("dlStatus").textContent + " | dlFeedback=" + $("dlFeedback").textContent + " | log=" + JSON.stringify(window.__log.slice(-4)); }
  const pre = document.createElement("pre"); pre.id = "result"; pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
            html = html.replace("<script", FAKE + "<script", 1) if "<script" in html else FAKE + html
            html = html.replace("</body>", DRIVER + "</body>")
            body, ctype = html.encode("utf-8"), "text/html; charset=utf-8"
        else:
            f = os.path.join(WEB, p.lstrip("/"))
            if not os.path.isfile(f):
                self.send_response(404); self.end_headers(); return
            body = open(f, "rb").read()
            ctype = "text/css" if f.endswith(".css") else "application/javascript" if f.endswith(".js") else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


def run() -> int:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        print("SKIP: no Chrome"); return 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prof = tempfile.mkdtemp()
    try:
        r = subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--lang=zh-CN", f"--user-data-dir={prof}",
                            "--virtual-time-budget=60000", "--dump-dom", f"http://127.0.0.1:{srv.server_port}/"],
                           capture_output=True, text=True, timeout=120)
    finally:
        srv.shutdown(); shutil.rmtree(prof, ignore_errors=True)
    import re, html as _h
    m = re.search(r'<pre id="result">(.*?)</pre>', r.stdout, re.S)
    if not m:
        print("no result from page", r.stderr[-500:]); return 1
    data = json.loads(_h.unescape(m.group(1)))
    bad = 0
    for name, good, extra in data["checks"]:
        print(("ok   " if good else "FAIL ") + name + ("" if good or not extra else "  -> " + extra))
        bad += not good
    if data["err"]:
        print("ERROR", data["err"]); bad += 1
    print("failures:", bad)
    return 1 if bad else 0


def test_web_retry():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
