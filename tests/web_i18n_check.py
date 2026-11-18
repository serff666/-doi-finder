"""English UI, end to end, in headless Chrome against the scripted fake backend of web_retry_check.

The language is set to English before the page loads, every page and every running state is
visited (lookup, download, journal library running / paused / finished, preprints, the per-page
download panels), and after each step the whole document is scanned for Chinese left in text
nodes, placeholder / title / aria-label attributes and the page title. Then the language is
switched to Chinese and back to check that nothing is stuck in the wrong language.

Skipped when Chrome is not installed.  Run:  python tests/web_i18n_check.py
"""
import json, os, re, shutil, sys, tempfile, threading, html as _h
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import web_retry_check as base  # noqa: E402  (its FAKE backend and fixtures are reused)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_path import find_chrome, run_chrome  # noqa: E402

PRE = '<script>try { localStorage.setItem("df_lang", "en"); } catch (e) {}</script>'

DRIVER = r"""
<script>
(async () => {
  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (f, what) => { for (let i = 0; i < 200; i++) { if (f()) return; await sleep(50); } throw new Error("timeout: " + what); };
  const out = { checks: [], left: [], err: null };
  const ok = (name, cond, extra) => out.checks.push([name, !!cond, extra === undefined ? "" : String(extra)]);
  const CJK = /[\u4e00-\u9fff]/;
  const KEEP = new Set(["中文"]);            // the language switch itself
  async function scan(stage) {
    await sleep(40);                         // let the MutationObserver translate what just appeared
    const found = [];
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const p = n.parentElement;
      if (!p || /^(SCRIPT|STYLE)$/.test(p.tagName)) continue;
      const t = n.nodeValue.replace(/\s+/g, " ").trim();
      if (CJK.test(t) && !KEEP.has(t)) found.push("text: " + t);
    }
    document.querySelectorAll("[placeholder],[title],[aria-label]").forEach((el) => {
      for (const a of ["placeholder", "title", "aria-label"]) {
        const v = el.getAttribute(a);
        if (v && CJK.test(v)) found.push(a + " of #" + (el.id || el.tagName) + ": " + v);
      }
    });
    if (CJK.test(document.title)) found.push("page title: " + document.title);
    found.forEach((f) => out.left.push(stage + " | " + f));
  }
  const tab = (name) => { const b = document.querySelector('[data-tab="' + name + '"]'); b.click(); return b; };
  try {
    await sleep(500);
    ok("page is in English", document.documentElement.lang === "en" && $("start").textContent === "Start batch lookup", $("start").textContent);
    await scan("boot");
    for (const b of [...document.querySelectorAll("[data-tab]")]) { b.click(); await sleep(60); await scan("tab " + b.dataset.tab); }

    // ---- lookup + retry
    tab("lookup");
    $("titles").value = "Alpha paper\nBeta paper\nGamma paper\nDelta paper";
    $("titles").dispatchEvent(new Event("input", { bubbles: true }));
    await scan("lookup typed");
    $("start").click();
    await until(() => /Lookup finished/.test($("runStatus").textContent), "first lookup");
    await scan("lookup done");
    $("retryLookup").click();
    await until(() => /Retry finished/.test($("runStatus").textContent), "lookup retry");
    await scan("lookup retried");
    ok("retry count has its number", /\d/.test($("retryLookup").textContent), $("retryLookup").textContent);

    // ---- DOI -> PDF
    tab("download");
    $("dois").value = "10.1000/a\n10.1000/b\n10.1000/c";
    $("dois").dispatchEvent(new Event("input", { bubbles: true }));
    $("startDownload").click();
    await until(() => /^Finished/.test($("dlStatus").textContent), "download 1");
    await scan("download done");
    $("retryDownload").click();
    await until(() => /Retry finished/.test($("dlStatus").textContent), "download retry");
    await scan("download retried");
    ok("workers option keeps its number", [...$("lkWorkers").options].every((o) => /^workers \d$/.test(o.textContent)), [...$("lkWorkers").options].map((o) => o.textContent).join("|"));

    // ---- journal library: running, paused, finished, retried
    const jdTab = [...document.querySelectorAll("[data-tab]")].find((b) => /Journal library/.test(b.textContent));
    ok("journal library tab is named in English", !!jdTab); jdTab.click();
    $("jdIssn").value = "1759-0876";
    $("jdRun").click();
    await until(() => /Fetch finished/.test($("jdRunTitle").textContent), "harvest");
    await scan("harvest done");
    $("jdDownload").click();
    await until(() => !$("jdPause").disabled && /Processed 1/.test($("jdProg").textContent), "running tick");
    await scan("journal running");
    $("jdPause").click();
    await until(() => /Paused/.test($("jdRunTitle").textContent), "paused");
    await scan("journal paused");
    $("jdPause").click();
    await until(() => !/Paused/.test($("jdRunTitle").textContent), "resumed");
    window.__jd.finish = true;
    await until(() => /Download finished/.test($("jdRunTitle").textContent), "journal finished");
    await scan("journal finished");
    $("jdOnlyBad").click(); await scan("journal all rows"); $("jdOnlyBad").click();
    $("jdRetry").click();
    await until(() => /Download finished/.test($("jdRunTitle").textContent) && /2 succeeded|✓/.test($("jdRunTitle").textContent), "journal retry");
    await scan("journal retried");

    // ---- per-page download panels
    window.__inpage = true;
    tab("lookup");
    await until(() => !$("toDownload").disabled, "toDownload");
    $("lkDir").value = "D:\\papers"; $("lkDir").dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(40);
    ok("collection suggestion in English", $("lkColl").placeholder === "auto: Unsorted", $("lkColl").placeholder);
    ok("layout suggestion in English", $("lkStruct").options[0].textContent === "auto (flat)", $("lkStruct").options[0].textContent);
    ok("preview in English", $("lkWhere").textContent === "Will be saved to: D:\\papers\\Unsorted\\file.pdf", $("lkWhere").textContent);
    ok("folder placeholder in English", /^Workspace \(empty = default: /.test($("lkDir").placeholder), $("lkDir").placeholder);
    $("lkStruct").value = "year"; $("lkStruct").dispatchEvent(new Event("change", { bubbles: true })); await sleep(40);
    ok("preview with a layout", $("lkWhere").textContent === "Will be saved to: D:\\papers\\Unsorted\\2025\\file.pdf", $("lkWhere").textContent);
    $("qAuthor").value = "Zhang Xinglong"; $("qAuthor").dispatchEvent(new Event("input", { bubbles: true })); await sleep(40);
    ok("author folder", $("srColl").placeholder === "auto: Zhang Xinglong" && $("srStruct").options[0].textContent === "auto (by year)", $("srColl").placeholder + " | " + $("srStruct").options[0].textContent);
    $("qAuthor").value = ""; $("q").value = ""; $("q").dispatchEvent(new Event("input", { bubbles: true })); await sleep(40);
    ok("topic folder name in English", $("srColl").placeholder === "auto: Topic search", $("srColl").placeholder);
    ok("labs and preprints folder names", $("lbColl").placeholder === "auto: Group website" && $("ppColl").placeholder === "auto: Preprints", $("lbColl").placeholder + " | " + $("ppColl").placeholder);
    ok("journal library layout", $("jdStruct").options[0].textContent === "auto (by year, then volume/issue)", $("jdStruct").options[0].textContent);
    await scan("download panels");

    // ---- preprints
    tab("preprint");
    $("ppInput").value = "Alpha paper\nBeta paper\nGamma paper";
    $("ppFind").click();
    await until(() => /failed/.test($("ppRunTitle").textContent), "preprint find");
    await scan("preprints found");
    $("ppRetryFind").click();
    await until(() => /Search done/.test($("ppRunTitle").textContent), "preprint retry");
    await scan("preprints retried");

    // ---- Chinese and back: nothing may stay in the wrong language
    $("langToggle").click(); await sleep(150);
    ok("switch to Chinese: static text", $("start").textContent === "开始批量检索", $("start").textContent);
    ok("switch to Chinese: computed text", $("lkColl").placeholder === "自动：未分类" && /^将保存到：D:\\papers\\未分类\\2025\\文件\.pdf$/.test($("lkWhere").textContent), $("lkColl").placeholder + " | " + $("lkWhere").textContent);
    ok("switch to Chinese: layout label", $("lkStruct").options[0].textContent === "自动（不分层）" || $("lkStruct").options[0].textContent.startsWith("自动（"), $("lkStruct").options[0].textContent);
    ok("switch to Chinese: workers option", $("lkWorkers").options[1].textContent === "并发 3", $("lkWorkers").options[1].textContent);
    ok("switch to Chinese: page title", /文献检索/.test(document.title), document.title);
    ok("switch to Chinese: running status", /重试完成/.test($("dlStatus").textContent), $("dlStatus").textContent);
    const english = [...document.querySelectorAll("button,label,summary,h1,h2,h3,th")].filter((e) => /^[A-Z][a-z]+( [a-z]+)+$/.test((e.textContent || "").trim())).map((e) => e.textContent.trim());
    ok("switch to Chinese: no English UI sentences left behind", english.length === 0, english.slice(0, 5).join(" | "));
    $("langToggle").click(); await sleep(150);
    ok("and back to English", $("start").textContent === "Start batch lookup" && $("lkColl").placeholder === "auto: Unsorted", $("start").textContent);
    await scan("after round trip");
  } catch (e) { out.err = String(e && e.stack || e); }
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
            html = open(os.path.join(base.WEB, "index.html"), encoding="utf-8").read()
            html = html.replace("<script", PRE + base.FAKE + "<script", 1)
            html = html.replace("</body>", DRIVER + "</body>")
            body, ctype = html.encode("utf-8"), "text/html; charset=utf-8"
        else:
            f = os.path.join(base.WEB, p.lstrip("/"))
            if not os.path.isfile(f):
                self.send_response(404); self.end_headers(); return
            body = open(f, "rb").read()
            ctype = "text/css" if f.endswith(".css") else "application/javascript" if f.endswith(".js") else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


def run() -> int:
    chrome = find_chrome()
    if not chrome:
        print("SKIP: no Chrome"); return 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prof = tempfile.mkdtemp()
    try:
        r = run_chrome([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--lang=en-US",
                            "--virtual-time-budget=90000", "--dump-dom", f"http://127.0.0.1:{srv.server_port}/"], timeout=150)
    finally:
        srv.shutdown(); shutil.rmtree(prof, ignore_errors=True)
    m = re.search(r'<pre id="result">(.*?)</pre>', r.stdout, re.S)
    if not m:
        print("no result from page", r.stderr[-500:]); return 1
    data = json.loads(_h.unescape(m.group(1)))
    bad = 0
    for name, good, extra in data["checks"]:
        print(("ok   " if good else "FAIL ") + name + ("" if good or not extra else "  -> " + extra))
        bad += not good
    seen = set()
    for line in data["left"]:
        stage, _, what = line.partition(" | ")
        if what in seen:
            continue
        seen.add(what)
        print("LEFT " + line); bad += 1
    if data["err"]:
        print("ERROR", data["err"]); bad += 1
    print("failures:", bad)
    return 1 if bad else 0


def test_web_i18n():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
