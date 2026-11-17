"""The app shell, end to end, in headless Chrome: sidebar, page titles, shortcuts, command palette, theme,
job monitor, drag and drop, the settings page.

The page runs in English (the language swap must not wipe the sidebar icons), against a small scripted
fake backend; then it switches to Chinese and back.  Skipped when Chrome is not installed.
Run:  python tests/web_shell_check.py
"""
import html as _h
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_path import find_chrome  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

FAKE = r"""
<script>
try { localStorage.setItem("df_lang", "en"); } catch (e) {}
window.__log = [];
window.__jobs = [];
(function () {
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o), { status: 200, headers: { "Content-Type": "application/json" } }));
  window.fetch = (url, opt) => {
    const u = String(url), body = opt && opt.body ? JSON.parse(opt.body) : null;
    window.__log.push([u, body]);
    if (u === "/api/health") return json({ ok: true, app: "doi-finder", version: "9.9", shell: "desktop", platform: "test",
                                            settings: { download_dir: "/ws" } });
    if (u === "/api/jobs") return json({ jobs: window.__jobs });
    if (u === "/api/archive") return json({ dest: "/ws/archive", items: (body.paths || []).map((p) => ({ ok: true, src: p, dest: p, size: 2048 })) });
    if (u.startsWith("/api/history")) return json({ items: [] });
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
  const until = async (f, what) => { for (let i = 0; i < 300; i++) { if (f()) return; await sleep(50); } throw new Error("timeout: " + what); };
  const out = { checks: [], err: null };
  const ok = (name, cond, extra) => out.checks.push([name, !!cond, extra === undefined ? "" : String(extra)]);
  const key = (k, extra) => document.dispatchEvent(new KeyboardEvent("keydown", Object.assign({ key: k, ctrlKey: true, bubbles: true }, extra || {})));
  const active = () => (document.querySelector(".sidebar [data-tab].active") || {}).dataset;
  const icons = () => [...document.querySelectorAll(".sidebar [data-tab]")].filter((b) => b.querySelector("svg")).length;
  try {
    await until(() => document.documentElement.lang === "en" && /9\.9/.test($("aboutVer").textContent), "boot");
    const navs = document.querySelectorAll(".sidebar [data-tab]");
    ok("sidebar lists every page and settings", navs.length === 11, navs.length);
    ok("every sidebar item keeps its icon in English", icons() === 11, icons());
    ok("labels are English", /Title → DOI/.test(navs[0].textContent) && /Settings/.test(document.querySelector('[data-tab="settings"]').textContent));
    ok("page title follows the page", $("pageTitle").textContent === "Title → DOI" && /evidence/.test($("pageSub").textContent), $("pageTitle").textContent + " | " + $("pageSub").textContent);
    ok("nothing sticks out sideways", document.documentElement.scrollWidth <= innerWidth, document.documentElement.scrollWidth + " > " + innerWidth);
    ok("about shows the desktop shell", /Desktop app/.test($("aboutShell").textContent), $("aboutShell").textContent);

    // ---- navigation and shortcuts
    document.querySelector('[data-tab="journal"]').click();
    ok("click switches page and hash", active().tab === "journal" && location.hash === "#journal" && !$("tab-journal").hidden);
    ok("page title translated", $("pageTitle").textContent === "Journal feed", $("pageTitle").textContent);
    key("2"); ok("Ctrl+2 opens DOI → PDF", active().tab === "download");
    key("0"); ok("Ctrl+0 opens preprints (page ⑩)", active().tab === "preprint");
    key("9"); ok("Ctrl+9 opens the journal library", active().tab === "journaldl");
    key(","); ok("Ctrl+, opens settings", active().tab === "settings" && !$("tab-settings").hidden);
    ok("settings page holds the global settings", ["email", "scihubEnable", "scihubMirrors", "instEnable", "instLoginUrl", "elsevierKey", "dlTemplate", "dlWorkers", "saveSettings"]
      .every((id) => $("tab-settings").contains($(id))));
    ok("download page keeps its own options", ["dlCollection", "dlStruct", "forceRedownload"].every((id) => $("tab-download").contains($(id))));
    document.querySelector('#tab-download [data-goto="settings"]').click();
    ok("'Open settings' link works", active().tab === "settings");

    // ---- command palette
    key("k"); await sleep(30);
    ok("Ctrl+K opens the palette", !$("palette").hidden && document.activeElement === $("paletteInput"));
    $("paletteInput").value = "journal lib"; $("paletteInput").dispatchEvent(new Event("input"));
    const first = $("paletteList").querySelector(".pal-item.sel");
    ok("palette ranks the best match first", first && /Journal library/.test(first.textContent), first && first.textContent.trim());
    $("paletteInput").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await sleep(30);
    ok("Enter runs it", $("palette").hidden && active().tab === "journaldl");
    key("k"); $("paletteInput").value = "zzzzqqq"; $("paletteInput").dispatchEvent(new Event("input"));
    ok("no match says so", /No matching/.test($("paletteList").textContent), $("paletteList").textContent);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    ok("Escape closes", $("palette").hidden);
    key("k"); $("paletteInput").value = "dark"; $("paletteInput").dispatchEvent(new Event("input"));
    const before = document.documentElement.dataset.theme;
    $("paletteInput").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); await sleep(30);
    ok("palette action toggles the theme", document.documentElement.dataset.theme !== before);

    // ---- theme and sidebar
    document.querySelector('[data-theme-pick="dark"]').click();
    ok("dark theme applies and is remembered", document.documentElement.dataset.theme === "dark" && localStorage.getItem("df_theme") === "dark");
    ok("dark background really changes", getComputedStyle(document.body).backgroundColor !== "rgb(244, 246, 250)", getComputedStyle(document.body).backgroundColor);
    document.querySelector('[data-theme-pick="light"]').click();
    ok("light theme", document.documentElement.dataset.theme === "light" && document.querySelector('[data-theme-pick="light"]').getAttribute("aria-pressed") === "true");
    $("navCollapse").click();
    ok("sidebar collapses to icons", document.documentElement.classList.contains("nav-mini") && localStorage.getItem("df_nav") === "mini");
    ok("collapsed items keep a tooltip", document.querySelector('[data-tab="lookup"]').title === "Title → DOI", document.querySelector('[data-tab="lookup"]').title);
    $("navCollapse").click();
    ok("and expands again", !document.documentElement.classList.contains("nav-mini"));

    // ---- workspace saves itself
    $("dlDir").value = "/new/ws"; $("dlDir").dispatchEvent(new Event("change"));
    await until(() => window.__log.some((c) => c[0] === "/api/settings" && c[1] && c[1].settings && c[1].settings.download_dir === "/new/ws"), "workspace save");
    ok("workspace change is saved on its own", true);

    // ---- job monitor
    const baseTitle = document.title;
    window.__jobs = [{ id: "A", kind: "download", state: "running", done: 1, total: 4 }];
    await until(() => !$("jobsPill").hidden, "pill shows");
    ok("status bar shows the running job", /PDF download 1\/4/.test($("jobsText").textContent), $("jobsText").textContent);
    ok("window title shows progress", /^\(25%\) /.test(document.title), document.title);
    window.__jobs = [{ id: "A", kind: "download", state: "done", done: 4, total: 4 }];
    await until(() => $("jobsPill").hidden, "pill hides");
    ok("a finished job is announced", /Finished: PDF download 4\/4/.test($("toast").textContent), $("toast").textContent);
    ok("window title restored", document.title === baseTitle, document.title + " vs " + baseTitle);

    // ---- drag and drop: text files go into the page's input box
    document.querySelector('[data-tab="lookup"]').click();
    $("titles").value = "";
    const dt = new DataTransfer();
    dt.items.add(new File(["First dropped title\nSecond dropped title"], "refs.txt", { type: "text/plain" }));
    window.dispatchEvent(new DragEvent("dragenter", { dataTransfer: dt, bubbles: true }));
    ok("dragging files shows the drop veil", !$("dropVeil").hidden && /Drop to import/.test($("dropTitle").textContent), $("dropTitle").textContent);
    window.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
    await until(() => /Second dropped title/.test($("titles").value), "text drop");
    ok("dropped text lands in the titles box", $("dropVeil").hidden && /First dropped title/.test($("titles").value));
    ok("and is counted", /2/.test($("lineCount").textContent), $("lineCount").textContent);

    // ---- drag and drop: PDFs reported by the desktop shell are archived
    document.dispatchEvent(new CustomEvent("doifinder:drop-paths", { detail: ["/x/a.pdf", "/x/notes.txt", "/x/B.PDF"] }));
    await until(() => window.__log.some((c) => c[0] === "/api/archive"), "archive call");
    const ar = window.__log.filter((c) => c[0] === "/api/archive").pop()[1];
    ok("dropped PDFs open the archive page", active().tab === "archive");
    ok("only the PDFs are archived, with their paths", JSON.stringify(ar.paths) === '["/x/a.pdf","/x/B.PDF"]', JSON.stringify(ar.paths));
    await until(() => /archive/.test($("arFeedback").textContent), "archive feedback");
    ok("archive reports the result", /2 \/ 2/.test($("arFeedback").textContent), $("arFeedback").textContent);

    // ---- page furniture: history button, collapsed download settings, idle counters, menus
    document.querySelector('[data-tab="lookup"]').click(); await sleep(30);
    ok("history button shows on a page with saved tasks", !$("histBtn").hidden && $("histPop").contains($("hist-lookup")));
    $("histBtn").click();
    ok("history opens as a popover with only this page's list", !$("histPop").hidden && !$("hist-lookup").hidden && $("hist-search").hidden);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    ok("Escape closes it", $("histPop").hidden);
    key(",");
    ok("no history button on the settings page", $("histBtn").hidden);
    document.querySelector('[data-tab="lookup"]').click();
    ok("download settings start collapsed to one line", $("lkOpts").tagName === "DETAILS" && !$("lkOpts").open, $("lkOpts").tagName);
    ok("the line says where files go and which channels run", /Will be saved to/.test($("lkWhere").textContent) && /workers 3/.test($("lkChips").textContent),
       $("lkWhere").textContent + " | " + $("lkChips").textContent);
    $("lkWorkers").value = "5"; $("lkWorkers").dispatchEvent(new Event("change", { bubbles: true })); await sleep(20);
    ok("and follows changes", /workers 5/.test($("lkChips").textContent), $("lkChips").textContent);
    ok("empty counters stay hidden", $("statTotal").closest(".summary").classList.contains("idle"));
    const menu = $("exportXlsx").closest("details.menu");
    menu.open = true; $("exportXlsx").disabled = false; $("exportXlsx").click(); await sleep(20);
    ok("a menu closes after an item is used", !menu.open);

    // ---- language round trip must not wipe icons
    $("langToggle").click(); await sleep(80);
    ok("Chinese labels", /标题 → DOI/.test(document.querySelector('[data-tab="lookup"]').textContent) && $("pageTitle").textContent === "标题 → DOI", $("pageTitle").textContent);
    ok("icons survive the switch to Chinese", icons() === 11, icons());
    $("langToggle").click(); await sleep(80);
    ok("icons survive the switch back", icons() === 11 && $("pageTitle").textContent === "Title → DOI", icons() + " " + $("pageTitle").textContent);
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
            html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
            html = html.replace("<script", FAKE + "<script", 1)
            html = html.replace("</body>", DRIVER + "</body>")
            body, ctype = html.encode("utf-8"), "text/html; charset=utf-8"
        else:
            f = os.path.join(WEB, p.lstrip("/"))
            if not os.path.isfile(f):
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
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
        r = subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--lang=en-US", f"--user-data-dir={prof}",
                            "--virtual-time-budget=60000", "--dump-dom", f"http://127.0.0.1:{srv.server_port}/"],
                           capture_output=True, text=True, timeout=150)
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
    if data["err"]:
        print("ERROR", data["err"]); bad += 1
    print("failures:", bad)
    return 1 if bad else 0


def test_web_shell():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
