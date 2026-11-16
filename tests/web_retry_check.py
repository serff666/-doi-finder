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
  const it = (n, doi) => ({ doi, title: "Paper " + n, year: 2024, volume: "14", issue: String(n), kind: "research" });
  jobs.H1 = { state: "done", done: 3, total: 3, errors: [], items: [it(1, "10.1000/j1"), it(2, "10.1000/j2"), it(3, "10.1000/j3")] };
  window.__jd = { paused: false, finish: null };       // the test flips these
  jobs.J1 = { get state() { return window.__jd.finish ? "done" : "running"; }, get paused() { return window.__jd.paused; },
    get done() { return window.__jd.finish ? 3 : 1; }, total: 3, current: "10.1000/j2", summary: "s", dest: "/d",
    get results() { return window.__jd.finish ? [
      { doi: "10.1000/j1", status: "downloaded", file: "a.pdf" }, { doi: "10.1000/j2", status: "failed", note: "boom" },
      { doi: "10.1000/j3", status: "no_oa" }] : [{ doi: "10.1000/j1", status: "downloaded", file: "a.pdf" }]; } };
  jobs.J2 = { state: "done", done: 1, total: 1, dest: "/d", results: [{ doi: "10.1000/j2", status: "downloaded", file: "b.pdf" }] };
  jobs.J3 = { get state() { return window.__jd.stopped ? "stopped" : "running"; }, cancelled: false, done: 0, total: 3, results: [], dest: "/d" };
  const cand = (doi, title, level) => ({ doi, title, year: "2023", authors: ["A. Author"], level, why: "why-" + level, url: "https://doi.org/" + doi });
  const prow = (n, input, status, cands, auto, err) => ({ n, input, doi: "", title: input, first_author: "", year: "", status, candidates: cands || [], auto: auto || "", notes: [], error: err || "" });
  jobs.P1 = { state: "done", done: 3, total: 3, results: [
    prow(1, "Alpha paper", "found", [cand("10.26434/chemrxiv-1", "Alpha preprint", "exact"), cand("10.26434/chemrxiv-2", "Alpha-like", "possible")], "10.26434/chemrxiv-1"),
    prow(2, "Beta paper", "error", [], "", "net down"), prow(3, "Gamma paper", "error", [], "", "timeout")] };
  jobs.P2 = { state: "done", done: 2, total: 2, results: [prow(1, "Beta paper", "found", [cand("10.26434/chemrxiv-3", "Beta preprint", "likely")], "10.26434/chemrxiv-3"), prow(2, "Gamma paper", "none")] };
  jobs.PD = { state: "done", done: 3, total: 3, dest: "/d", results: ["10.26434/chemrxiv-1", "10.26434/chemrxiv-2", "10.26434/chemrxiv-3"].map((d) => ({ doi: d, status: "downloaded", file: "p.pdf" })) };
  jobs.K1 = { state: "done", done: 3, total: 3, dest: "/d", results: ["10.1000/alpha", "10.1000/beta", "10.1000/delta"].map((d) => ({ doi: d, status: "downloaded", file: "k.pdf" })) };
  const pord = ["P1", "P2"]; let pused = 0, pdl = 0;
  const order = { lookup: ["L1", "L2", "L3"], download: ["D1", "D2", "J1", "J2", "J3"] };
  const used = { lookup: 0, download: 0 };
  let flaky = 0;
  const json = (o, ok = true, status = 200) => Promise.resolve(new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json" } }));
  window.fetch = (url, opt) => {
    const u = String(url), body = opt && opt.body ? JSON.parse(opt.body) : null;
    window.__log.push([u, body]);
    if (u === "/api/journal/harvest") return json({ job_id: "H1" });
    let pm = u.match(/^\/api\/(pause|resume)\/J1/);
    if (pm) { window.__jd.paused = pm[1] === "pause"; return json({ ok: true, paused: window.__jd.paused }); }
    if (u === "/api/stop/J3") { window.__jd.stoppedSoon = true; return json({ ok: true }); }
    if (u === "/api/preprint/find") return json({ job_id: pord[pused++] });
    if (u === "/api/download" && window.__inpage) { return json({ job_id: pdl++ === 0 ? "K1" : "PD" }); }
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

    // ---- journal library: harvest, then download with a visible progress panel
    const tabBtn = [...document.querySelectorAll("[data-tab]")].find((b) => /期刊文库|Journal library/i.test(b.textContent));
    ok("journal library tab exists", !!tabBtn); tabBtn.click();
    $("jdIssn").value = "1759-0876";
    $("jdRun").click();
    await until(() => /抓取完成/.test($("jdRunTitle").textContent), "harvest done");
    ok("harvest panel says complete with count", /共 3 篇/.test($("jdRunTitle").textContent), $("jdRunTitle").textContent);
    ok("stop hidden after harvest", $("jdStop").hidden);
    $("jdDownload").click();
    await until(() => /正在下载/.test($("jdRunTitle").textContent), "dl started");
    ok("panel visible with pause and stop", !$("jdPanel").hidden && !$("jdPause").hidden && !$("jdStop").hidden);
    await until(() => !$("jdPause").disabled && /已处理 1/.test($("jdProg").textContent), "first progress tick");
    ok("progress text shows counts", /已处理 1 \/ 3/.test($("jdProg").textContent) && /成功 1/.test($("jdProg").textContent), $("jdProg").textContent);
    ok("current item shown", /10\.1000\/j2/.test($("jdCurrent").textContent), $("jdCurrent").textContent);
    ok("download button locked while running", $("jdDownload").disabled);
    $("jdPause").click();
    await until(() => /已暂停/.test($("jdRunTitle").textContent), "paused");
    ok("pause sent to the right job", window.__log.some((c) => c[0] === "/api/pause/J1"));
    ok("pause button now says resume", /继续|Resume/i.test($("jdPause").textContent), $("jdPause").textContent);
    $("jdPause").click();
    await until(() => !/已暂停/.test($("jdRunTitle").textContent), "resumed");
    ok("resume sent", window.__log.some((c) => c[0] === "/api/resume/J1"));
    window.__jd.finish = true;
    await until(() => /下载结束/.test($("jdRunTitle").textContent), "dl finished with failures");
    ok("finish banner counts the failure", /还有 1 篇没成功/.test($("jdRunTitle").textContent), $("jdRunTitle").textContent);
    ok("panel turns red when something failed", $("jdPanel").classList.contains("bad") && !$("jdPanel").classList.contains("done"));
    ok("final counts", /成功 1/.test($("jdProg").textContent) && /失败 1/.test($("jdProg").textContent) && /已处理 3 \/ 3/.test($("jdProg").textContent), $("jdProg").textContent);
    ok("detail defaults to unfetched only", /10\.1000\/j2/.test($("jdResults").textContent) && /10\.1000\/j3/.test($("jdResults").textContent) && !/10\.1000\/j1/.test($("jdResults").textContent), $("jdResults").textContent);
    ok("detail summary counts", /未获取 2 \/ 共 3/.test($("jdResSum").textContent), $("jdResSum").textContent);
    window.__blobs = []; const _cb = URL.createObjectURL; URL.createObjectURL = (b) => { window.__blobs.push(b); return _cb.call(URL, b); };
    $("jdExport").click();
    await until(() => window.__blobs.length, "export blob");
    const csv = await window.__blobs[0].text();
    ok("export lists only unfetched with header", /DOI,标题,状态/.test(csv) && /10\.1000\/j2/.test(csv) && /10\.1000\/j3/.test(csv) && !/10\.1000\/j1/.test(csv), csv);
    $("jdOnlyBad").click();
    ok("unticking shows everything", /10\.1000\/j1/.test($("jdResults").textContent));
    $("jdOnlyBad").click();
    ok("buttons reset, retry offered for 1", $("jdPause").hidden && $("jdStop").hidden && !$("jdRetry").hidden && $("jdRetry").textContent.includes("1") && !$("jdDownload").disabled, $("jdRetry").textContent);
    $("jdRetry").click();
    await until(() => /下载完成/.test($("jdRunTitle").textContent), "retry done");
    const jcalls = window.__log.filter((c) => c[0] === "/api/download");
    ok("journal retry sends only the failed DOI with its layout", JSON.stringify(jcalls[3][1].dois) === '["10.1000/j2"]' && jcalls[3][1].layout.items.length === 1 && jcalls[3][1].layout.items[0].issue === "2", JSON.stringify(jcalls[3][1]));
    ok("panel turns green when all done", $("jdPanel").classList.contains("done") && !$("jdPanel").classList.contains("bad"));
    ok("all-clear banner", /成功 2/.test($("jdRunTitle").textContent) && /无开放获取/.test($("jdRunTitle").textContent) && $("jdRetry").hidden, $("jdRunTitle").textContent);
    $("jdDownload").click();
    await until(() => /正在下载/.test($("jdRunTitle").textContent), "second run");
    await until(() => !$("jdStop").disabled, "stop enabled");
    $("jdStop").click();
    await until(() => window.__log.some((c) => c[0] === "/api/stop/J3"), "stop sent");
    ok("stop acknowledged on screen", /正在停止/.test($("jdRunTitle").textContent), $("jdRunTitle").textContent);
    window.__jd.stopped = true;
    await until(() => /已停止/.test($("jdRunTitle").textContent) && !$("jdRetry").hidden, "stopped");
    ok("stopped run offers retry for the rest", $("jdRetry").textContent.includes("3") || $("jdRetry").textContent.includes("2") || $("jdRetry").textContent.includes("1"), $("jdRetry").textContent);

    // ---- lookup page: download right there, no jump to the DOI -> PDF tab
    window.__inpage = true;
    document.querySelector('[data-tab="lookup"]').click();
    await until(() => !$("toDownload").disabled, "toDownload enabled");
    const tabBefore = document.querySelector("[data-tab].active, [data-tab][aria-selected='true']");
    ok("every page has its own settings before any download", !!$("lkDir") && !!$("ppDir") && !!$("jdDir") && !!$("sr" + "Dir"));
    ok("channels default from the global ones", $("lkSci").checked === $("scihubEnable").checked && $("lkInst").checked === $("instEnable").checked);
    $("lkDir").value = "/tmp/lk"; $("lkDir").dispatchEvent(new Event("change"));
    $("lkSci").checked = false; $("lkSci").dispatchEvent(new Event("change"));
    $("lkInst").checked = true; $("lkInst").dispatchEvent(new Event("change"));
    $("lkWorkers").value = "5"; $("lkWorkers").dispatchEvent(new Event("change"));
    ok("other pages untouched by lk settings", $("ppDir").value === "" && $("ppSci").checked === $("scihubEnable").checked && $("ppInst").checked === $("instEnable").checked && $("ppWorkers").value === $("dlWorkers").value);
    $("scihubEnable").checked = false; $("instEnable").checked = !$("instEnable").checked;
    document.dispatchEvent(new Event("doifinder:settings"));
    ok("untouched pages follow a changed default, touched ones do not", $("ppSci").checked === false && $("ppInst").checked === $("instEnable").checked && $("lkSci").checked === false && $("lkInst").checked === true && $("lkDir").value === "/tmp/lk");
    $("scihubEnable").checked = true; $("instEnable").checked = !$("instEnable").checked;
    document.dispatchEvent(new Event("doifinder:settings"));
    ok("default restored", $("ppSci").checked === true);
    $("toDownload").click();
    await until(() => !!$("lkPanel") && /下载完成/.test($("lkRunTitle").textContent), "in-page download");
    ok("lookup download stays on the lookup page", document.getElementById("tab-lookup") && !document.getElementById("tab-lookup").hidden && document.getElementById("tab-download").hidden);
    const lkBody = window.__log.filter((c) => c[0] === "/api/download").slice(-1)[0][1];
    ok("lookup download uses its own folder and channels", lkBody.dir === "/tmp/lk" && lkBody.scihub === false && lkBody.inst === true && lkBody.workers === 5, JSON.stringify(lkBody));
    ok("in-page download sent the matched DOIs", window.__log.filter((c) => c[0] === "/api/download").slice(-1)[0][1].dois.includes("10.1000/alpha"), JSON.stringify(window.__log.filter((c) => c[0] === "/api/download").slice(-1)));

    // ---- preprint page
    document.querySelector('[data-tab="preprint"]').click();
    ok("preprint page shown", !document.getElementById("tab-preprint").hidden);
    ok("find with empty input refuses", ($("ppFind").click(), /至少一行/.test($("ppFeedback").textContent)));
    $("ppInput").value = "Alpha paper\nBeta paper\nGamma paper";
    $("ppFind").click();
    await until(() => /没查成/.test($("ppRunTitle").textContent), "preprint find done");
    ok("find posts the typed lines", window.__log.filter((c) => c[0] === "/api/preprint/find")[0][1].text === "Alpha paper\nBeta paper\nGamma paper");
    ok("panel turns red with failures", $("ppPanel").classList.contains("bad"));
    ok("exact candidate preselected, possible not", document.querySelector('[data-pp="10.26434/chemrxiv-1"]').checked && !document.querySelector('[data-pp="10.26434/chemrxiv-2"]').checked);
    ok("levels and reasons shown", /why-exact/.test($("ppRoot").textContent) && /why-possible/.test($("ppRoot").textContent));
    ok("failed rows say the query failed, not 'none'", $("ppRoot").textContent.split("查询没成功").length === 3 && !/没有找到预印本/.test($("ppRoot").textContent));
    ok("retry offered for 2", !$("ppRetryFind").hidden && $("ppRetryFind").textContent.includes("2"), $("ppRetryFind").textContent);
    ok("download enabled with 1 selected", !$("ppDownload").disabled && /已选 1/.test($("ppCount").textContent), $("ppCount").textContent);
    $("ppRetryFind").click();
    await until(() => /查找完成/.test($("ppRunTitle").textContent) && $("ppRetryFind").hidden, "retry done");
    ok("retry sends only the failed line", window.__log.filter((c) => c[0] === "/api/preprint/find")[1][1].text === "Beta paper\nGamma paper");
    ok("retried row spliced in place, order kept", [...document.querySelectorAll(".pp-row b")].map((b) => b.textContent).join("|") === "Alpha paper|Beta paper|Gamma paper" && /Beta preprint/.test($("ppRoot").textContent) && /没有找到预印本/.test(document.querySelectorAll(".pp-row")[2].textContent) && !/查询没成功/.test($("ppRoot").textContent));
    ok("retry preselects the new auto pick", document.querySelector('[data-pp="10.26434/chemrxiv-3"]').checked && /已选 2/.test($("ppCount").textContent), $("ppCount").textContent);
    document.querySelector('[data-pp="10.26434/chemrxiv-2"]').click();
    ok("manual tick adds possible one", /已选 3/.test($("ppCount").textContent));
    $("ppPickGood").click();
    ok("pick good resets to the confident ones", /已选 2/.test($("ppCount").textContent) && !document.querySelector('[data-pp="10.26434/chemrxiv-2"]').checked);
    $("ppNone").click();
    ok("none clears and disables download", $("ppDownload").disabled);
    $("ppPickGood").click();
    document.querySelector('[data-pp="10.26434/chemrxiv-2"]').click();
    $("dlDir").value = "/global";
    $("ppDownload").click();
    await until(() => /下载完成/.test($("ppRunTitle").textContent), "preprint download");
    const pc = window.__log.filter((c) => c[0] === "/api/download").slice(-1)[0][1];
    ok("preprint download sends the selected preprint DOIs", JSON.stringify(pc.dois.slice().sort()) === '["10.26434/chemrxiv-1","10.26434/chemrxiv-2","10.26434/chemrxiv-3"]', JSON.stringify(pc.dois));
    ok("untouched folder falls back to the default one, channels stay default", pc.dir === "/global" && pc.scihub === $("scihubEnable").checked && pc.inst === $("instEnable").checked, JSON.stringify(pc));
    ok("pp panel all fetched says so", /全部已获取/.test($("ppResults").textContent), $("ppResults").textContent);
    ok("lk settings kept independently", $("lkDir").value === "/tmp/lk" && $("lkSci").checked === false);
    $("ppDir").value = "/tmp/pp"; $("ppDir").dispatchEvent(new Event("change"));
    ok("preprint page stays put", !document.getElementById("tab-preprint").hidden);

    // ---- from the DOI -> PDF page: unfetched DOIs go to the preprint page, which only fills the box
    document.querySelector('[data-tab="download"]').click();
    ok("missing-to-preprint enabled (rows have failures)", !$("missingToPreprint").disabled);
    $("missingToPreprint").click();
    ok("preprint input filled with the failed DOI", /10\.1000\/c/.test($("ppInput").value) || /10\.1000\/b/.test($("ppInput").value), $("ppInput").value);
    ok("lands on preprint page without searching", !document.getElementById("tab-preprint").hidden && window.__log.filter((c) => c[0] === "/api/preprint/find").length === 2);
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
