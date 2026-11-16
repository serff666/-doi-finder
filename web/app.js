/* DOI Finder 2.0 — 前端逻辑（原生 JS，无构建步骤） */
// 任何脚本错误都写进状态栏：界面没反应时，至少能看到原因
window.addEventListener("error", (e) => {
  const el = document.getElementById("statusText");
  if (el) el.textContent = `脚本错误：${e.message || e.type} @ ${(e.filename || "").split("/").pop()}:${e.lineno || ""}`;
});
window.addEventListener("unhandledrejection", (e) => {
  const el = document.getElementById("statusText");
  if (el) el.textContent = "请求失败：" + ((e.reason && e.reason.message) || e.reason);
});
(() => {

  // 重新加载界面：带上时间戳绕过缓存，等价于 Ctrl+F5，但不用记快捷键
  const reloadBtn = () => {
    const b = document.getElementById("reloadApp");
    if (!b) return;
    b.addEventListener("click", () => {
      b.disabled = true;
      b.textContent = "刷新中…";
      const url = location.pathname + "?t=" + Date.now();
      location.replace(url);
    });
  };
  "use strict";


  const $ = (id) => document.getElementById(id);
  const DEMO = new URLSearchParams(location.search).get("demo") === "1";

  /* ------------------------------------------------------------------ helpers */
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let toastTimer;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("on"), 2400);
  }
  function feedback(id, msg, isErr = false) {
    const el = $(id);
    el.textContent = msg || "";
    el.className = "feedback" + (msg ? " show" : "") + (isErr ? " err" : "");
  }
  function stamp() {
    const d = new Date(), p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`;
  }
  function doiUrl(doi) { return "https://doi.org/" + encodeURI(doi); }
  function validDoi(v) {
    let t = String(v || "").trim();
    // arXiv 链接或编号先转成 arXiv 的 DOI，不然手填 arXiv 永远过不了校验
    const ax = t.match(/arxiv\.org\/(?:abs|pdf)\/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)/i)
      || t.match(/arxiv\s*:\s*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)/i)
      || t.match(/^([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)$/);
    if (ax) return "10.48550/arXiv." + ax[1].replace(/\.pdf$/i, "");
    t = t.replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "")
      .replace(/^doi\s*:\s*/i, "").replace(/\s+/g, "");
    return /^10\.\d{4,9}\/\S+$/i.test(t) ? t : "";
  }
  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    const res = await fetch(path, opt);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  function saveBlob(blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); toast("已复制"); }
    catch {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove(); toast("已复制");
    }
  }

  /* ------------------------------------------------------------------ tabs */
  const state = { tab: "lookup", lookupRows: [], downloadRows: [], searchRows: [], chemRows: [],
                  selected: { search: new Set(), chem: new Set() }, mode: "auto", job: null,
                  baiduDoi: "", editRow: null, myOrcid: "", recentOrcids: [] };

  function showTab(name) {
    state.tab = name;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tabpanel").forEach((p) => { p.hidden = p.id !== "tab-" + name; });
  }
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  /* ================================================================ 1. titles → DOI */
  const NUM = /^\s*(?:\[\s*\d+\s*\]|\(\s*\d+\s*\)|\d+\s*[.)\]:]|#\s*\d+)\s+/;
  function parseTitles(text) {
    return String(text).split(/\r?\n/).map((line) => {
      const s = line.trim();
      if (!s || /^(#\s|\/\/|--\s)/.test(s)) return "";
      return s.replace(NUM, "").trim();
    }).filter(Boolean);
  }
  function countInput() {
    $("lineCount").textContent = `当前 ${parseTitles($("titles").value).length} 篇文献`;
  }

  const STATUS = {
    matched: "高可信", likely: "可能匹配", uncertain: "需核对", provided: "已提供",
    not_found: "未找到", error: "检索失败", edited: "人工确认", stopped: "未检索",
  };
  const PILL = {
    matched: "success", likely: "success", provided: "success", edited: "success",
    uncertain: "review", not_found: "pending", error: "error", stopped: "pending",
  };
  const SAMPLE = "";

  document.querySelectorAll("#modeSeg button").forEach((b) => b.addEventListener("click", () => {
    state.mode = b.dataset.mode;
    document.querySelectorAll("#modeSeg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  }));

  $("titles").addEventListener("input", countInput);
  $("fillExample").addEventListener("click", () => { $("titles").value = SAMPLE; countInput(); });
  $("importFile").addEventListener("click", () => $("filePicker").click());
  $("filePicker").addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) { $("titles").value = await f.text(); countInput(); toast(`已导入 ${f.name}`); }
    e.target.value = "";
  });
  $("clearInput").addEventListener("click", () => { $("titles").value = ""; countInput(); feedback("feedback", ""); });

  function lookupStats() {
    const rows = state.lookupRows;
    const done = rows.filter((r) => !["pending", "stopped"].includes(r.status)).length;
    const found = rows.filter((r) => r.doi && ["matched", "likely", "provided", "edited"].includes(r.status)).length;
    const review = rows.filter((r) => r.status === "uncertain").length;
    $("statTotal").textContent = rows.length;
    $("statFound").textContent = found;
    $("statReview").textContent = review;
    $("statDone").textContent = done;
    $("progressBar").style.width = (rows.length ? Math.round(100 * done / rows.length) : 0) + "%";
    const has = rows.length > 0;
    ["exportXlsx", "exportMd", "exportHtml", "exportCsv", "exportTxt", "copyDois", "toDownload"]
      .forEach((id) => { $(id).disabled = !has; });
  }

  function lookupRowHtml(r, i) {
    const st = r.status || "not_found";
    const score = typeof r.score === "number" && r.score ? `${Math.round(r.score * 100)}%` : "";
    const meta = [r.year, r.journal].filter(Boolean).join(" · ");
    // 没找到可靠匹配时，别把相似度很低的候选摆出来当答案，读者会误以为那是这篇
    const weak = (r.status === "not_found") || (typeof r.score === "number" && r.score > 0 && r.score < 0.55);
    const matchTitle = weak ? "" : (r.match_title || (r.alts && r.alts[0] ? r.alts[0].title : ""));
    const editing = state.editRow === i;
    const alts = r.alts || [];
    const editPanel = editing ? `<tr class="edit-row"><td colspan="6">
        ${alts.length ? `<div class="edit-alts">${alts.map((a, k) => `
          <button class="alt-card" type="button" data-pick="${k}">
            <span class="alt-title">${esc(a.title || "")}</span>
            <span class="alt-meta">${esc([a.first_author, a.year, a.journal, a.doi].filter(Boolean).join(" · "))}</span>
            <span class="alt-score">${typeof a.score === "number" ? Math.round(a.score * 100) + "%" : ""} ${esc(a.source || "")}</span>
          </button>`).join("")}</div>` : `<div class="aux" style="margin-bottom:8px">这条没有候选记录，直接填 DOI。</div>`}
        <div class="edit-line">
          <input class="smallinput" id="editDoi" value="${esc(r.doi || "")}" placeholder="10.xxxx/xxxxx（留空表示 N/A）">
          <button class="btn sm primary" id="editSave" type="button">保存</button>
          <button class="btn sm" id="editCancel" type="button">取消</button>
        </div>
        <div class="subnote">点上面的候选卡片会填入它的 DOI；保存后状态变成「已改写」，导出和下载都用新 DOI。</div>
      </td></tr>` : "";
    return `<tr class="${editing ? "editing" : ""}">
      <td class="num">${i + 1}</td>
      <td><div class="title-main">${esc(r.title)}</div></td>
      <td><div class="match-title">${esc(matchTitle || (weak ? "无可靠匹配" : "—"))}</div><div class="aux">${esc(weak ? "" : meta)}</div></td>
      <td>${r.doi ? `<a class="doi-link" href="${esc(doiUrl(r.doi))}" target="_blank" rel="noopener noreferrer">${esc(r.doi)}</a>` : '<span class="dash">—</span>'}
          <div class="aux">${esc([r.source, r.note].filter(Boolean).join(" · "))}</div></td>
      <td><span class="pill ${PILL[st] || "pending"}">${esc(STATUS[st] || st)}</span>
          <div class="aux">${score ? score + " 匹配度" : ""}</div>
          ${r.title_mismatch ? '<div class="aux" style="color:#b45309;margin-top:3px">⚠ 标题与输入不符</div>' : ""}</td>
      <td><button class="mini-btn" data-edit="${i}" type="button">${editing ? "收起" : "修改"}</button>
        <button class="btn sm" type="button" data-del-lookup="${i}" title="从列表移除">删除</button></td>
    </tr>${editPanel}`;
  }

  function renderLookup() {
    lookupStats();
    const root = $("lookupRoot");
    if (!state.lookupRows.length) {
      root.innerHTML = `<div class="empty"><div class="empty-symbol">⌕</div>
        <strong>检索结果会显示在这里</strong>
        <p>输入一组标题后点击「开始批量检索」。</p></div>`;
      return;
    }
    root.innerHTML = `<div class="results-scroll"><table class="results-table">
      <thead><tr><th>#</th><th>输入标题</th><th>数据库匹配标题</th><th>DOI</th><th>状态</th><th></th></tr></thead>
      <tbody>${state.lookupRows.map(lookupRowHtml).join("")}</tbody></table></div>`;
  }

  $("lookupRoot").addEventListener("click", (e) => {
    const editBtn = e.target.closest("[data-edit]");
    if (editBtn) {
      const i = Number(editBtn.dataset.edit);
      state.editRow = state.editRow === i ? null : i;
      renderLookup();
      const box = $("editDoi");
      if (box) { box.focus(); box.select(); }
      return;
    }
    const pick = e.target.closest("[data-pick]");
    if (pick) {
      const i = state.editRow;
      const row = state.lookupRows[i];
      const alt = row && row.alts && row.alts[Number(pick.dataset.pick)];
      const box = $("editDoi");
      if (alt && box) { box.value = alt.doi || ""; box.focus(); }
      return;
    }
    if (e.target.id === "editCancel") { state.editRow = null; renderLookup(); return; }
    if (e.target.id === "editSave") {
      const i = state.editRow;
      const row = state.lookupRows[i];
      if (!row) return;
      const box = $("editDoi");
      const raw = (box ? box.value : "").trim();
      const value = raw ? validDoi(raw) : "";
      if (raw && !value) return toast("DOI 或 arXiv 链接格式不正确");
      row.doi = value;
      row.status = value ? "edited" : "not_found";
      row.source = value ? "手动" : "";
      row.score = 0;  // the old similarity score belongs to the old DOI
      row.title_mismatch = false;
      state.editRow = null;
      if (!value) {
        row.note = "标记为无 DOI";
        row.match_title = "";
        renderLookup();
        return toast("已标记为无 DOI");
      }
      row.note = "已填写，正在按 DOI 取元数据…";
      renderLookup();
      toast("已改写为 " + value + "，正在核对标题");
      api("/api/meta", { doi: value }).then((m) => {
        if (!m || m.error) {
          row.note = "手动填写（元数据没取到）";
        } else {
          if (m.title) row.match_title = m.title;
          if (m.year) row.year = m.year;
          if (m.journal) row.journal = m.journal;
          if (m.first_author) row.first_author = m.first_author;
          row.note = "手动填写 · 已按 DOI 更新元数据";
          row.title_mismatch = m.title ? titleDice(row.title, m.title) < 0.6 : false;
        }
        renderLookup();
      }).catch(() => {
        row.note = "手动填写（元数据没取到）";
        renderLookup();
      });
    }
  });

  function titleDice(a, b) {
    const STOP = new Set(["and", "the", "for", "with", "from", "into", "over", "under", "between",
                          "using", "via", "its", "their", "this", "that", "are", "was", "were"]);
    const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, " ").trim();
    const words = (s) => new Set(norm(s).split(/\s+/).filter((w) => w.length > 2 && !STOP.has(w)));
    const A = words(a), B = words(b);
    if (!A.size || !B.size) return 1;
    let hit = 0;
    A.forEach((w) => { if (B.has(w)) hit += 1; });
    return (2 * hit) / (A.size + B.size);
  }

  $("start").addEventListener("click", async () => {
    const titles = parseTitles($("titles").value);
    if (!titles.length) { feedback("feedback", "请先输入至少一篇文献标题。", true); $("titles").focus(); return; }
    $("start").disabled = true; $("stop").disabled = false; $("titles").disabled = true;
    $("runStatus").textContent = "正在检索…"; $("dot").className = "green-dot busy";
    state.lookupRows = [];
    renderLookup();
    const lookupIdle = () => {
      $("start").disabled = false; $("stop").disabled = true; $("titles").disabled = false;
      $("dot").className = "green-dot";
    };
    try {
      const job = await api("/api/lookup", {
        text: $("titles").value, mode: state.mode,
        author: $("author").value.trim(), workers: Number($("workers").value),
        openalex: $("openalex").checked, dedup: $("dedup").checked, mailto: $("email").value.trim(),
      });
      pollJob(job.job_id, (d) => {
        $("runStatus").textContent = `正在检索… ${d.done} / ${d.total}`;
        $("progressBar").style.width = (d.total ? Math.round(100 * d.done / d.total) : 0) + "%";
      }, (d) => {
        lookupIdle();
        state.lookupRows = d.results || [];
        state.editRow = null;
        renderLookup();
        $("runStatus").textContent = d.state === "stopped"
          ? `已停止，完成 ${state.lookupRows.length} 篇`
          : `检索结束 · ${state.lookupRows.length} 篇 · ${d.summary || ""}`;
      });
    } catch (e) {
      feedback("feedback", "检索失败：" + e.message, true);
      lookupIdle();
    }
  });

  $("stop").addEventListener("click", async () => {
    if (!state.job) return;
    $("stop").disabled = true;
    $("runStatus").textContent = "正在停止…（当前这一条查完就停）";
    try { await api(`/api/stop/${state.job}`, {}); } catch { /* ignore */ }
  });

  function lookupPayload() {
    return state.lookupRows.map((r) => ({
      n: r.n, title: r.title, doi: r.doi || "", status: r.status, score: r.score,
      year: r.year, journal: r.journal, volume: r.volume, page: r.page,
      match_title: r.match_title, note: r.note, source: r.source,
    }));
  }
  async function exportAs(fmt) {
    if (!state.lookupRows.length) return toast("还没有结果可导出");
    try {
      const res = await fetch(`/api/export/${fmt}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ results: lookupPayload(), basename: `DOI检索结果_${stamp()}` }),
      });
      if (!res.ok) throw new Error("导出失败");
      const cd = res.headers.get("Content-Disposition") || "";
      const m = /filename="([^"]+)"/.exec(cd);
      saveBlob(await res.blob(), m ? m[1] : `dois.${fmt}`);
      toast(`已导出 ${fmt.toUpperCase()}`);
    } catch (e) { toast("导出失败：" + e.message); }
  }

  // 参考文献表：按 GB/T 7714-2015 的期刊格式生成
  // 作者. 题名[J]. 刊名, 年, 卷(期): 页码. DOI.
  // 我们手里通常只有题名、刊名、年份和 DOI，缺的部分不硬编，直接省略
  function fmtAuthors(list) {
    const a = (list || []).filter(Boolean);
    if (!a.length) return "";
    // GB/T 7714：三位以内全列，超过三位取前三加“等”
    return (a.length > 3 ? a.slice(0, 3).join(", ") + ", 等" : a.join(", ")) + ". ";
  }

  function gbt7714(rows) {
    return rows.map((r, i) => {
      const title = (r.title || r.match_title || "").replace(/\.$/, "");
      const journal = r.journal || "";
      const year = r.year || "";
      const doi = r.doi || "";
      const vol = r.volume || "";
      const iss = r.issue || "";
      const page = r.page || "";
      let s = `${i + 1}. ${fmtAuthors(r.authors)}${title}[J]`;
      if (journal) s += `. ${journal}`;
      if (year) s += `, ${year}`;
      if (vol) s += `, ${vol}`;
      if (iss) s += `(${iss})`;
      if (page) s += `: ${page}`;
      if (!/[.!?]$/.test(s)) s += ".";
      if (doi) s += ` DOI: ${doi}.`;
      return s;
    }).join("\n");
  }

  function acsStyle(rows) {
    return rows.map((r) => {
      const title = (r.title || r.match_title || "").replace(/\.$/, "");
      const journal = r.journal || "";
      const year = r.year || "";
      const doi = r.doi || "";
      let s = `${title}. ${journal}`.trim();
      if (year) s += ` ${year}`;
      s = s.replace(/\s+$/, "") + ".";
      if (doi) s += ` DOI: ${doi}.`;
      return s;
    }).join("\n");
  }

  function apaStyle(rows) {
    return rows.map((r) => {
      const a = (r.authors || []).filter(Boolean);
      let who = "";
      if (a.length === 1) who = a[0] + ". ";
      else if (a.length === 2) who = `${a[0]}, & ${a[1]}. `;
      else if (a.length > 2) who = a.slice(0, -1).join(", ") + `, & ${a[a.length - 1]}. `;
      const year = r.year ? `(${r.year}). ` : "(n.d.). ";
      const title = (r.title || r.match_title || "").replace(/\.$/, "") + ". ";
      let src = "";
      if (r.journal) {
        src = r.journal;
        if (r.volume) src += `, ${r.volume}`;
        if (r.issue) src += `(${r.issue})`;
        if (r.page) src += `, ${r.page}`;
        src += ". ";
      }
      const doi = r.doi ? `https://doi.org/${r.doi}` : "";
      return (who + year + title + src + doi).trim();
    }).join("\n");
  }

  function exportCitations() {
    const rows = state.lookupRows.filter((r) => r.doi || r.title);
    if (!rows.length) return toast("没有可导出的条目");
    const fmt = ($("citeStyle") && $("citeStyle").value) || "gbt";
    const text = fmt === "apa" ? apaStyle(rows) : fmt === "acs" ? acsStyle(rows) : gbt7714(rows);
    saveBlob(new Blob([text], { type: "text/plain;charset=utf-8" }),
             fmt === "gbt" ? "references-gbt7714.txt" : `references-${fmt}.txt`);
    toast(`已导出 ${rows.length} 条（${fmt.toUpperCase()}）`);
  }
  [["exportXlsx", "xlsx"], ["exportMd", "md"], ["exportHtml", "html"], ["exportCsv", "csv"], ["exportTxt", "txt"]]
    .forEach(([id, fmt]) => $(id).addEventListener("click", () => exportAs(fmt)));

  if ($("exportCite")) $("exportCite").addEventListener("click", exportCitations);

  $("copyDois").addEventListener("click", () => copyText(state.lookupRows.map((r) => r.doi || "N/A").join("\n")));
  $("toDownload").addEventListener("click", () => {
    const dois = state.lookupRows.filter((r) => r.doi).map((r) => r.doi);
    if (!dois.length) return toast("没有可下载的 DOI");
    addDois(dois);
    showTab("download");
  });

  /* ================================================================ 2. DOI → PDF */
  function parseDois(text) {
    return String(text).split(/[\r\n,;\s]+/).map((s) => s.trim())
      .filter(Boolean).map(validDoi).filter(Boolean);
  }
  function countDois() {
    const n = parseDois($("dois").value).length;
    $("doiCount").textContent = `当前 ${n} 个 DOI`;
    const badge = $("dlBadge");
    badge.hidden = n === 0;
    badge.textContent = n;
  }
  function addDois(list) {
    const have = parseDois($("dois").value);
    const seen = new Set(have.map((d) => d.toLowerCase()));
    const add = list.map(validDoi).filter((d) => d && !seen.has(d.toLowerCase()));
    if (!add.length) { toast("这些 DOI 已经在列表里了"); return 0; }
    $("dois").value = have.concat(add).join("\n");
    countDois();
    toast(`已加入 ${add.length} 个 DOI`);
    return add.length;
  }
  $("dois").addEventListener("input", countDois);
  $("clearDois").addEventListener("click", () => { $("dois").value = ""; countDois(); });
  $("fromLookup").addEventListener("click", () => {
    const dois = state.lookupRows.filter((r) => r.doi).map((r) => r.doi);
    if (!dois.length) return toast("检索结果里还没有 DOI");
    addDois(dois);
  });
  $("importDoiFile").addEventListener("click", () => $("doiFilePicker").click());
  $("doiFilePicker").addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) addDois(parseDois(await f.text()));
    e.target.value = "";
  });

  const DL_STATUS = {
    downloaded: ["已下载", "ok"], exists: ["已存在", "info"], no_oa: ["无开放获取", "warn"],
    blocked: ["需订阅 / 验证码", "warn"],
    failed: ["失败", "bad"], invalid: ["DOI 无效", "bad"], stopped: ["已停止", "pending"],
    pending: ["等待中", "pending"],
  };

  function downloadStats() {
    const rows = state.downloadRows;
    const c = (s) => rows.filter((r) => r.status === s).length;
    $("dlTotal").textContent = rows.length || parseDois($("dois").value).length;
    $("dlDone").textContent = c("downloaded") + c("exists");
    $("dlNoOa").textContent = c("no_oa");
    $("dlFail").textContent = c("failed") + c("invalid") + c("blocked");
    $("openDest").disabled = !rows.length;
    $("openBlocked").disabled = !rows.length;
    $("exportDlCsv").disabled = !rows.length;
    $("exportMissing").disabled = !rows.length;
    $("exportTask").disabled = !rows.length;
  }

  function renderDownloads() {
    if ($("onlyMissing")) $("onlyMissing").onchange = renderDownloads;
    downloadStats();
    const root = $("dlRoot");
    if (!state.downloadRows.length) {
      root.innerHTML = `<div class="empty"><div class="empty-symbol">⇩</div>
        <strong>还没有下载任务</strong>
        <p>粘贴 DOI 或从其他标签页带过来，选择下载目录后点「开始下载」。</p></div>`;
      return;
    }
    const onlyMissing = $("onlyMissing") && $("onlyMissing").checked;
    const rows = state.downloadRows
      .map((r, i) => ({ r, i }))
      .filter(({ r }) => !onlyMissing || !["downloaded", "exists"].includes(r.status))
      .map(({ r, i }) => {
      const st = DL_STATUS[r.status] || [r.status, "pending"];
      return `<tr>
        <td>${r.doi ? `<a class="doi-link" href="${esc(doiUrl(r.doi))}" target="_blank" rel="noopener noreferrer">${esc(r.doi)}</a>` : "—"}
            <div class="aux">${esc((r.title || "").slice(0, 90))}</div></td>
        <td><span class="pill ${st[1]}">${esc(st[0])}</span></td>
        <td><div class="dl-file">${esc(r.file || "")}</div></td>
        <td>${esc(r.source || "")}<div class="aux">${esc(r.note || "")}</div></td>
        <td>${r.url ? `<a class="doi-link" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">来源</a>` : ""}
            ${["downloaded", "exists"].includes(r.status)
              ? `
              <button class="mini-btn" data-attach="${i}" style="margin-left:6px">重新归档</button>
              <button class="mini-btn" data-del-dl="${i}" style="margin-left:6px">删除</button>`
              : `<button class="mini-btn" data-open="${esc(r.url || doiUrl(r.doi))}" style="margin-left:6px">浏览器打开</button>
               <button class="mini-btn" data-carsi="${i}" style="margin-left:6px">机构登录</button>
               <button class="mini-btn" data-attach="${i}" style="margin-left:6px">归档</button>
               <button class="mini-btn" data-del-dl="${i}" style="margin-left:6px">删除</button>`}</td>
      </tr>`;
    }).join("");
    root.innerHTML = `<div class="results-scroll"><table class="results-table">
      <thead><tr><th>DOI</th><th>状态</th><th>文件</th><th>来源 / 说明</th><th>链接</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }


  // 检索/下载结果里删除单条，不动磁盘上已下载的文件
  document.addEventListener("click", (e) => {
    const lk = e.target.closest("[data-del-lookup]");
    if (lk && !lk.disabled) {  // del-lookup handler
      const i = Number(lk.dataset.delLookup);
      if (Number.isInteger(i) && state.lookupRows[i]) {
        state.lookupRows.splice(i, 1);
        if (state.editRow === i) state.editRow = null;
        renderLookup();
        toast("已从检索结果移除");
      }
      return;
    }
    const dl = e.target.closest("[data-del-dl]");
    if (dl && !dl.disabled) {
      const i = Number(dl.dataset.delDl);
      if (Number.isInteger(i) && state.downloadRows[i]) {
        state.downloadRows.splice(i, 1);
        renderDownloads();
        toast("已从下载结果移除");
      }
    }
  });

  $("startDownload").addEventListener("click", async () => {
    const dois = parseDois($("dois").value);
    if (!dois.length) { feedback("dlFeedback", "请先粘贴至少一个 DOI。", true); return; }
    $("startDownload").disabled = true; $("stopDownload").disabled = false;
    $("pauseDownload").disabled = false; $("pauseDownload").textContent = "暂停";
    $("dlStatus").textContent = "正在解析开放获取来源…";
    $("dot").className = "green-dot busy";
    // 先把待办行铺出来，别让表格空着：后端要过几秒才吐第一行结果
    const seed = dois.map((doi) => ({ doi, title: "", status: "pending", file: "", source: "", note: "" }));
    state.downloadRows = seed;
    renderDownloads();
    const downloadIdle = () => {
      $("startDownload").disabled = false; $("stopDownload").disabled = true;
      $("pauseDownload").disabled = true; $("pauseDownload").textContent = "暂停";
      $("dot").className = "green-dot";
    };
    // 后端结果覆盖对应行，没轮到的保持“等待中”
    const mergeRows = (results) => {
      const got = new Map((results || []).map((r) => [String(r.doi).toLowerCase(), r]));
      return seed.map((s) => got.get(s.doi.toLowerCase()) || s);
    };
    try {
      const layout = state.pendingLayout || undefined;
      state.pendingLayout = null;
      const job = await api("/api/download", {
        layout,
        dois, dir: $("dlDir").value.trim(), email: $("email").value.trim(),
        template: $("dlTemplate").value.trim(), force: $("forceRedownload").checked,
        workers: Number($("dlWorkers").value),
        scihub: $("scihubEnable").checked,
        mirrors: $("scihubMirrors").value.trim(),
        inst: $("instEnable").checked,
        login_url: $("instLoginUrl").value.trim(),
      });
      pollJob(job.job_id, (d) => {
        state.downloadRows = mergeRows(d.results);
        renderDownloads();
        $("dlStatus").textContent = `正在下载… ${d.done} / ${d.total}` + (d.current ? ` · ${d.current}` : "");
        $("dlBar").style.width = (d.total ? Math.round(100 * d.done / d.total) : 0) + "%";
      }, (d) => {
        downloadIdle();
        state.downloadRows = d.state === "error" ? mergeRows(d.results) : (d.results || []);
        renderDownloads();
        $("dlStatus").textContent = d.state === "error" ? "任务失败：" + d.error
          : `完成 · ${d.summary || ""} · ${d.dest || ""}`;
      });
    } catch (e) {
      feedback("dlFeedback", "下载失败：" + e.message, true);
      downloadIdle();
    }
  });
  $("exportMissing").addEventListener("click", () => {
    const rows = state.downloadRows.filter((r) => !["downloaded", "exists", "stopped"].includes(r.status));
    if (!rows.length) return toast("没有未获取的条目");
    const head = ["DOI", "标题", "状态", "说明", "链接"];
    const lines = [head.join(",")].concat(rows.map((r) => [
      r.doi, r.title, (DL_STATUS[r.status] || [r.status])[0], r.note, r.url || doiUrl(r.doi),
    ].map((v) => `"${String(v || "").replace(/"/g, '""')}"`).join(",")));
    saveBlob(new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" }),
      `未获取清单_${stamp()}.csv`);
    toast(`已导出 ${rows.length} 条未获取记录`);
  });

  $("exportTask").addEventListener("click", () => {
    if (!state.downloadRows.length) return toast("没有可保存的任务");
    const payload = {
      version: 1, kind: "doi-finder-task", at: Math.floor(Date.now() / 1000),
      dest: $("dlDir").value.trim(), template: $("dlTemplate").value.trim(),
      summary: $("dlStatus").textContent, results: state.downloadRows,
    };
    saveBlob(new Blob([JSON.stringify(payload, null, 1)], { type: "application/json" }),
      `DOI任务_${stamp()}.json`);
    toast(`已保存 ${state.downloadRows.length} 条任务记录`);
  });
  const DL_STATUS_FROM_LABEL = {
    "已下载": "downloaded", "已存在": "exists", "无开放获取": "no_oa",
    "需订阅 / 验证码": "blocked", "需订阅": "blocked", "需要订阅": "blocked",
    "失败": "failed", "DOI 无效": "invalid", "已停止": "stopped",
  };

  function parseCsv(text) {
    const rows = [];
    let row = [], field = "", quoted = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (quoted) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i++; } else { quoted = false; }
        } else field += c;
      } else if (c === '"') quoted = true;
      else if (c === ",") { row.push(field); field = ""; }
      else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
      else if (c !== "\r") field += c;
    }
    if (field.length || row.length) { row.push(field); rows.push(row); }
    return rows.filter((r) => r.some((x) => String(x).trim() !== ""));
  }

  function rowsFromCsv(text) {
    const table = parseCsv(text.replace(/^\ufeff/, ""));
    if (table.length < 2) return [];
    const head = table[0].map((h) => h.trim());
    const idx = (name) => head.findIndex((h) => h === name);
    const iDoi = idx("DOI");
    if (iDoi < 0) return [];
    const iStatus = idx("状态"), iFile = idx("文件"), iSrc = idx("来源"),
      iNote = idx("说明"), iUrl = idx("链接"), iTitle = idx("标题");
    const out = [];
    for (const cells of table.slice(1)) {
      const get = (i) => (i >= 0 && i < cells.length ? String(cells[i]).trim() : "");
      const doi = get(iDoi).replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "");
      if (!doi) continue;
      const label = get(iStatus);
      out.push({
        doi: validDoi(doi) || doi,
        status: DL_STATUS_FROM_LABEL[label] || (label ? "failed" : "no_oa"),
        file: get(iFile),
        source: get(iSrc),
        note: get(iNote),
        url: get(iUrl),
        title: get(iTitle) || "",
      });
    }
    return out;
  }

  $("importTask").addEventListener("click", () => $("taskPicker").click());
  $("taskPicker").addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!f) return;
    const text = await f.text();
    const looksJson = /^\s*[{[]/.test(text);
    try {
      let rows = [];
      let meta = {};
      if (looksJson) {
        const data = JSON.parse(text);
        rows = Array.isArray(data.results) ? data.results : (Array.isArray(data) ? data : []);
        meta = data;
      } else {
        rows = rowsFromCsv(text);
      }
      if (!rows.length) return toast("这个文件里没有可识别的记录");
      state.downloadRows = rows;
      if (meta.dest) $("dlDir").value = meta.dest;
      if (meta.template) $("dlTemplate").value = meta.template;
      renderDownloads();
      const when = meta.at
        ? new Date(meta.at * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
      $("dlStatus").textContent =
        `已导入${looksJson ? "任务" : " CSV"}${when ? "（" + when + "）" : ""} · ${rows.length} 条` +
        (meta.summary ? ` · ${meta.summary}` : "") + "　未获取的可以继续点「归档」";
      toast(`已导入 ${rows.length} 条，未获取的可以继续点「归档」`);
    } catch (err) {
      toast("导入失败：" + err.message);
    }
  });

  $("pauseDownload").addEventListener("click", async () => {
    if (!state.job) return;
    const btn = $("pauseDownload");
    const paused = btn.textContent === "继续";
    try {
      await api(`/api/${paused ? "resume" : "pause"}/${state.job}`, {});
      btn.textContent = paused ? "暂停" : "继续";
      $("dlStatus").textContent = paused ? "已继续" : "暂停中…（当前这一步结束后停住，点「继续」恢复）";
    } catch (e) {
      toast("操作失败：" + e.message);
    }
  });

  $("stopDownload").addEventListener("click", async () => {
    if (!state.job) return;
    $("stopDownload").disabled = true;
    $("pauseDownload").disabled = true;
    $("dlStatus").textContent = "正在停止…（当前这一步结束后立即停，最长等一个网络超时）";
    try { await api(`/api/stop/${state.job}`, {}); } catch { /* ignore */ }
  });
  $("dlRoot").addEventListener("click", async (e) => {
    const attach = e.target.closest("[data-attach]");
    if (attach) {
      const idx = Number(attach.dataset.attach);
      const row = state.downloadRows[idx];
      if (!row) return toast("这一行已经不存在了，请重新运行任务");
      if (!row.doi) return toast("这一行没有 DOI，无法归档");
      attach.disabled = true;
      toast("在弹出的窗口里选择你已下载的 PDF…");
      try {
        const d = await api("/api/attach", {
          doi: row.doi, dir: $("dlDir").value.trim(), template: $("dlTemplate").value.trim(),
        });
        if (d.error) { attach.disabled = false; return toast("归档失败：" + d.error); }
        Object.assign(row, d);
        renderDownloads();
        toast("已归档：" + d.file);
      } catch (err) {
        attach.disabled = false;
        toast("归档失败：" + err.message);
      }
      return;
    }
    const btn = e.target.closest("[data-open]");
    if (!btn) return;
    api("/api/open-url", { url: btn.dataset.open })
      .then(() => toast("已在浏览器打开，浏览器里可以直接下载"))
      .catch((err) => toast(err.message));
  });
  $("openBlocked").addEventListener("click", async () => {
    const rows = state.downloadRows.filter((r) => !["downloaded", "exists"].includes(r.status));
    if (!rows.length) return toast("没有待处理的条目");
    if (rows.length > 12 && !confirm(`将打开 ${rows.length} 个标签页，继续？`)) return;
    for (const r of rows.slice(0, 12)) {
      try { await api("/api/open-url", { url: r.url || doiUrl(r.doi) }); } catch { /* ignore */ }
      await new Promise((res) => setTimeout(res, 400));
    }
    toast(`已打开 ${Math.min(rows.length, 12)} 个链接，浏览器里另存即可`);
  });
  $("openDest").addEventListener("click", () => api("/api/open-folder", { path: $("dlDir").value.trim() })
    .catch((e) => toast(e.message)));
  $("exportDlCsv").addEventListener("click", () => {
    const head = ["DOI", "状态", "文件", "来源", "说明", "链接"];
    const lines = [head.join(",")].concat(state.downloadRows.map((r) => [
      r.doi, (DL_STATUS[r.status] || [r.status])[0], r.file, r.source, r.note, r.url,
    ].map((v) => `"${String(v || "").replace(/"/g, '""')}"`).join(",")));
    saveBlob(new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" }),
      `下载结果_${stamp()}.csv`);
  });

  /* ================================================================ 3. topic search */
  $("runSearch").addEventListener("click", async () => {
    const q = $("q").value.trim();
    const orcid = $("qOrcid").value.trim();
    if (!q && !$("qAuthor").value.trim() && !orcid) {
      feedback("searchFeedback", "请输入主题关键词、作者或 ORCID。", true); return;
    }
    $("runSearch").disabled = true;
    feedback("searchFeedback", "检索中…");
    try {
      const d = await api("/api/search", {
        query: q, author: $("qAuthor").value.trim(), orcid,
        year_from: $("yFrom").value.trim(), year_to: $("yTo").value.trim(),
        oa_only: $("qOa").checked, limit: orcid ? 1000 : 50,
      });
      state.searchRows = d.works || [];
      state.selected.search.clear();
      if ($("searchSourceTag") && d.source) $("searchSourceTag").textContent = d.source;
      feedback("searchFeedback", d.note || "");
      $("searchCount").textContent = `找到 ${d.count} 条` + (d.source ? ` · ${d.source}` : "");
      if (orcid) rememberOrcid(orcid);
      renderSearch();
    } catch (e) {
      feedback("searchFeedback", e.message, true);
    } finally { $("runSearch").disabled = false; }
  });

  const ORCID_RE = /^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$/i;

  function renderOrcidBar() {
    const bar = $("orcidBar");
    if (!bar) return;
    const items = [];
    if (state.myOrcid) items.push({ v: state.myOrcid, label: "★ " + state.myOrcid });
    state.recentOrcids
      .filter((v) => v !== state.myOrcid)
      .slice(0, 5)
      .forEach((v) => items.push({ v, label: v }));
    bar.innerHTML = items.map((it) =>
      `<button class="chip" type="button" data-orcid-fill="${esc(it.v)}" title="点击填入">${esc(it.label)}</button>`
    ).join("");
  }

  function rememberOrcid(v) {
    if (!ORCID_RE.test(v)) return;
    state.recentOrcids = [v, ...state.recentOrcids.filter((x) => x !== v)].slice(0, 8);
    renderOrcidBar();
    api("/api/settings", { save: true, settings: { recent_orcids: state.recentOrcids.join("\n") } })
      .catch(() => { /* keep the session list even if saving fails */ });
  }

  async function saveMyOrcid() {    const v = $("qOrcid").value.trim();
    if (!ORCID_RE.test(v)) return toast("先填一个格式正确的 ORCID（0000-0000-0000-0000）");
    try {
      await api("/api/settings", { save: true, settings: { my_orcid: v } });
      state.myOrcid = v;
      renderOrcidBar();
      toast("已保存为我的 ORCID：" + v);
    } catch (e) { toast("保存失败：" + e.message); }
  }
  $("saveMyOrcid").addEventListener("click", saveMyOrcid);
  $("orcidBar").addEventListener("click", (e) => {
    const b = e.target.closest("[data-orcid-fill]");
    if (!b) return;
    $("qOrcid").value = b.dataset.orcidFill;
    $("qOrcid").focus();
  });

  $("findOrcid").addEventListener("click", async () => {
    const name = $("qAuthor").value.trim();
    if (name.length < 3) return toast("先在「作者」里填一个名字");
    $("findOrcid").disabled = true;
    try {
      const d = await api("/api/orcid/search", { name });
      const list = d.candidates || [];
      if (!list.length) { $("orcidCandidates").innerHTML = ""; return toast("没有找到匹配的 ORCID"); }
      $("orcidCandidates").innerHTML = list.map((c) => `
        <button class="chip" type="button" data-orcid="${esc(c.orcid)}" title="${esc(c.institution || "")}">
          ${esc(c.name || "")} · ${esc(c.orcid)}${c.institution ? ` · ${esc(c.institution)}` : ""}
        </button>`).join("");
    } catch (e) { toast("查询失败：" + e.message); }
    finally { $("findOrcid").disabled = false; }
  });
  $("orcidCandidates").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-orcid]");
    if (!btn) return;
    $("qOrcid").value = btn.dataset.orcid;
    toast("已填入 ORCID，点「检索」即可列出其全部论文");
  });

  function renderSearch() {    const rows = state.searchRows;
    $("searchAll").disabled = $("searchNone").disabled = $("searchToDownload").disabled = !rows.length;
    const root = $("searchRoot");
    if (!rows.length) {
      root.innerHTML = `<div class="empty"><div class="empty-symbol">⌕</div>
        <strong>没有结果</strong><p>换个关键词，或放宽年份 / OA 限制。</p></div>`;
      return;
    }
    root.innerHTML = `<div class="results-scroll"><table class="results-table">
      <thead><tr><th class="checkcell"></th><th>标题</th><th>期刊 / 年份</th><th>作者</th><th>被引</th><th>OA</th></tr></thead>
      <tbody>${rows.map((w, i) => `<tr>
        <td class="checkcell"><input type="checkbox" data-sel="search" data-i="${i}" ${state.selected.search.has(i) ? "checked" : ""}></td>
        <td><div class="rowtitle">${esc(w.title)}</div>
            <div class="rowsub">${w.doi ? `<a class="doi-link" href="${esc(doiUrl(w.doi))}" target="_blank" rel="noopener noreferrer">${esc(w.doi)}</a>` : "无 DOI"}</div></td>
        <td>${esc(w.journal || "")}<div class="aux">${esc(String(w.year || ""))}</div></td>
        <td style="max-width:220px">${esc((w.authors || []).join(", "))}</td>
        <td>${w.cited || 0}</td>
        <td>${w.oa ? '<span class="pill ok">OA</span>' : '<span class="pill pending">—</span>'}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  $("searchRoot").addEventListener("change", (e) => {
    const cb = e.target.closest('[data-sel="search"]');
    if (!cb) return;
    const i = Number(cb.dataset.i);
    if (cb.checked) state.selected.search.add(i); else state.selected.search.delete(i);
  });
  $("searchAll").addEventListener("click", () => {
    state.searchRows.forEach((_, i) => state.selected.search.add(i));
    renderSearch();
  });
  $("searchNone").addEventListener("click", () => { state.selected.search.clear(); renderSearch(); });
  $("searchToDownload").addEventListener("click", () => {
    const dois = [...state.selected.search].map((i) => state.searchRows[i] && state.searchRows[i].doi).filter(Boolean);
    if (!dois.length) return toast("选中项里没有 DOI");
    addDois(dois);
    showTab("download");
  });

  /* ================================================================ 4. ChemASAP */
  $("chemLoad").addEventListener("click", loadChem);
  $("chemQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") loadChem(); });

  async function loadChem() {
    $("chemLoad").disabled = true;
    feedback("chemFeedback", "正在从 ChemASAP 拉取最新文章…");
    try {
      const params = new URLSearchParams({
        q: $("chemQuery").value.trim(), limit: "300", oa: $("chemOa").checked ? "1" : "0",
      });
      const d = await api("/api/chem/feed?" + params.toString());
      state.chemRows = d.articles || [];
      state.selected.chem.clear();
      feedback("chemFeedback", "");
      $("chemCount").textContent = `共 ${d.total} 条 · 更新于 ${(d.updatedAt || "").replace("T", " ").slice(0, 16)}`;
      renderChem();
    } catch (e) {
      feedback("chemFeedback", e.message, true);
    } finally { $("chemLoad").disabled = false; }
  }

  function renderChem() {
    const rows = state.chemRows;
    $("chemToDownload").disabled = !rows.length;
    const root = $("chemRoot");
    if (!rows.length) {
      root.innerHTML = `<div class="empty"><div class="empty-symbol">⚗</div>
        <strong>没有匹配的文章</strong><p>清空筛选条件再刷新试试。</p></div>`;
      return;
    }
    root.innerHTML = `<div class="results-scroll"><table class="results-table">
      <thead><tr><th class="checkcell"></th><th>标题</th><th>期刊 / 日期</th><th>作者</th><th>DOI</th></tr></thead>
      <tbody>${rows.map((a, i) => `<tr>
        <td class="checkcell"><input type="checkbox" data-sel="chem" data-i="${i}" ${state.selected.chem.has(i) ? "checked" : ""}></td>
        <td><div class="rowtitle">${esc(a.title)}</div>
            ${a.oa ? '<span class="pill ok" style="margin-top:6px">OA</span>' : ""}
            <div class="chem-abstract">${esc((a.abstract || "").slice(0, 220))}${(a.abstract || "").length > 220 ? "…" : ""}</div></td>
        <td>${esc(a.journal || "")}<div class="aux">${esc(a.publishedAt || "")}</div></td>
        <td style="max-width:200px">${esc(a.authors || "")}</td>
        <td>${a.doi ? `<a class="doi-link" href="${esc(doiUrl(a.doi))}" target="_blank" rel="noopener noreferrer">${esc(a.doi)}</a>` : "—"}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  $("chemRoot").addEventListener("change", (e) => {
    const cb = e.target.closest('[data-sel="chem"]');
    if (!cb) return;
    const i = Number(cb.dataset.i);
    if (cb.checked) state.selected.chem.add(i); else state.selected.chem.delete(i);
  });
  $("chemToDownload").addEventListener("click", () => {
    const dois = [...state.selected.chem].map((i) => state.chemRows[i] && state.chemRows[i].doi).filter(Boolean);
    if (!dois.length) return toast("选中项里没有 DOI");
    addDois(dois);
    showTab("download");
  });

  /* ================================================================ settings + boot */
  $("baiduLookup").addEventListener("click", async () => {
    const text = $("baiduInput").value.trim();
    if (!text) return toast("先粘贴百度学术的论文链接");
    $("baiduLookup").disabled = true;
    $("baiduResult").textContent = "查询中…";
    try {
      const d = await api("/api/baidu/lookup", { text });
      const bits = [d.title, [d.journal, d.year, d.volume ? `第 ${d.volume} 卷` : "", d.issue ? `第 ${d.issue} 期` : ""].filter(Boolean).join(" · "), d.doi ? "DOI " + d.doi : "没有 DOI"];
      $("baiduResult").innerHTML = `<b>${esc(bits[0])}</b><br>${esc(bits.slice(1).join(" · "))}` +
        (d.cn ? `<br>CN ${esc(d.cn)}${d.issn ? " · ISSN " + esc(d.issn) : ""}` : "");
      state.baiduDoi = d.doi || "";
      $("baiduToDownload").hidden = !d.doi;
    } catch (e) {
      $("baiduResult").textContent = "反查失败：" + e.message;
      $("baiduToDownload").hidden = true;
    } finally { $("baiduLookup").disabled = false; }
  });
  $("baiduToDownload").addEventListener("click", () => {
    if (!state.baiduDoi) return;
    addDois([state.baiduDoi]);
    showTab("download");
  });

  async function loadSettings() {
    try {
      const d = await api("/api/health");
      const s = d.settings || {};
      $("dlDir").value = s.download_dir || "";
      $("email").value = s.email || "";
      $("dlTemplate").value = s.template || "";
      $("scihubEnable").checked = s.scihub_enable !== false;
      $("scihubInsecure").checked = !!s.scihub_insecure_tls;
      $("dlWorkers").value = String(s.download_workers || 3);
      $("scihubMirrors").value = s.scihub_mirrors || "";
      $("instEnable").checked = s.inst_enable === true;
      $("instFirst").checked = s.inst_first === true;
      $("instLoginUrl").value = s.inst_login_url || "";
      if ($("elsevierKey")) $("elsevierKey").value = s.elsevier_key || "";
      if ($("elsevierToken")) $("elsevierToken").value = s.elsevier_token || "";
      if ($("wileyToken")) $("wileyToken").value = s.wiley_token || "";
      if ($("openalexKey")) $("openalexKey").value = s.openalex_key || "";
      if ($("coreKey")) $("coreKey").value = s.core_key || "";
      if ($("rgBrowser")) $("rgBrowser").value = (s.browser || "auto");
      state.myOrcid = s.my_orcid || "";
      $("arFileTpl").value = s.archive_file_template || "{first_author}_{year}_{title}";
      $("arFolderTpl").value = s.archive_folder_template || "{year}";
      state.recentOrcids = String(s.recent_orcids || "").split("\n").map((v) => v.trim()).filter(Boolean);
      renderOrcidBar();
      if (d.version) $("verText").textContent = d.version + (d.build ? ` · ${d.build}` : "");
      refreshInstStatus();
      if (d.frozen) $("quit").hidden = false;
    } catch { /* ignore */ }
  }
  $("saveSettings").addEventListener("click", async () => {
    try {
      await api("/api/settings", {
        save: true,
        settings: { download_dir: $("dlDir").value.trim(), email: $("email").value.trim(),
                    template: $("dlTemplate").value.trim(),
                    scihub_enable: $("scihubEnable").checked,
                    scihub_insecure_tls: $("scihubInsecure").checked,
                    download_workers: Number($("dlWorkers").value),
                    scihub_mirrors: $("scihubMirrors").value.trim(),
                    inst_enable: $("instEnable").checked,
                    inst_first: $("instFirst").checked,
                    inst_login_url: $("instLoginUrl").value.trim(),
                    elsevier_key: $("elsevierKey") ? $("elsevierKey").value.trim() : "",
                    elsevier_token: $("elsevierToken") ? $("elsevierToken").value.trim() : "",
                    wiley_token: $("wileyToken") ? $("wileyToken").value.trim() : "",
                    openalex_key: $("openalexKey") ? $("openalexKey").value.trim() : "",
                    core_key: $("coreKey") ? $("coreKey").value.trim() : "" },
      });
      toast("设置已保存");
    } catch (e) { toast("保存失败：" + e.message); }
  });

  async function refreshInstStatus() {
    try {
      const d = await api("/api/inst/status", {});
      const bits = [];
      bits.push(d.browser_name ? `${d.browser_name} 已就绪` : "没有找到 Chrome / Edge");
      bits.push(d.logged_in ? "配置目录已有登录数据" : "还没有登录过");
      $("instStatus").textContent = bits.join(" · ");
    } catch (e) { $("instStatus").textContent = "检测失败：" + e.message; }
  }
  if ($("rgBrowser")) {
    $("rgBrowser").addEventListener("change", async () => {
      const sel = $("rgBrowser");
      try {
        await api("/api/settings", { save: true, settings: { browser: sel.value } });
        const label = sel.selectedOptions[0] ? sel.selectedOptions[0].textContent : sel.value;
        toast("浏览器已切换：" + label);
        refreshInstStatus();
        if ($("rgStatus")) $("rgStatus").textContent = "已切换浏览器，首次使用请点「打开并登录」过验证";
      } catch (e) { toast("保存失败：" + e.message); }
    });
  }
  $("instLogin").addEventListener("click", async () => {
    const url = $("instLoginUrl").value.trim();
    if (!url) return toast("先填写学校图书馆 / EZproxy 登录地址");
    try {
      const d = await api("/api/inst/login", { url });
      if (!d.ok) return toast("打开失败：" + (d.error || ""));
      toast("在弹出的 Chrome 窗口里完成登录（含验证码/双因素），登录后回到这里即可");
      setTimeout(refreshInstStatus, 4000);
    } catch (e) { toast("打开失败：" + e.message); }
  });
  $("instClose").addEventListener("click", async () => {
    try { await api("/api/inst/close", {}); toast("浏览器已关闭（登录状态保留）"); }
    catch (e) { toast(e.message); }
    refreshInstStatus();
  });
  $("testMirrors").addEventListener("click", async () => {
    const btn = $("testMirrors");
    btn.disabled = true;
    $("mirrorStatus").textContent = "正在检测…";
    try {
      const d = await api("/api/scihub/test", { mirrors: $("scihubMirrors").value.trim() });
      const rows = d.mirrors || [];
      const good = rows.filter((r) => r.ok).length;
      $("mirrorStatus").textContent = `可用 ${good}/${rows.length}：` +
        rows.map((r) => `${r.mirror.replace(/^https?:\/\//, "")} ${r.ok ? r.status + " " + r.ms + "ms" : "×"}`).join(" · ");
    } catch (e) {
      $("mirrorStatus").textContent = "检测失败：" + e.message;
    } finally {
      btn.disabled = false;
    }
  });
  $("pickDir").addEventListener("click", async () => {
    try {
      const d = await api("/api/pick-folder", { initial: $("dlDir").value.trim() });
      if (d.path) { $("dlDir").value = d.path; toast("已选择：" + d.path); }
    } catch (e) { toast(e.message); }
  });
  $("openDir").addEventListener("click", () => api("/api/open-folder", { path: $("dlDir").value.trim() })
    .catch((e) => toast(e.message)));
  $("quit").addEventListener("click", async () => {
    try { await api("/api/quit", {}); } catch { /* ignore */ }
    $("statusText").textContent = "已退出，可以关闭此页面";
    $("dot").className = "green-dot down";
  });

  function pollJob(jobId, onTick, onDone) {
    state.job = jobId;
    const tick = async () => {
      try {
        const d = await api(`/api/job/${jobId}`);
        onTick(d);
        if (d.state === "running") { setTimeout(tick, 500); return; }
        onDone(d);
      } catch (e) {
        onDone({ state: "error", error: e.message, results: [], done: 0, total: 0 });
      }
    };
    setTimeout(tick, 400);
  }

  async function restoreLast() {
    const fmt = (ts) => {
      try { return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }); }
      catch { return ""; }
    };
    try {
      const lk = await api("/api/last/lookup");
      if (lk && Array.isArray(lk.results) && lk.results.length && !state.lookupRows.length) {
        state.lookupRows = lk.results;
        renderLookup();
        $("runStatus").textContent =
          `已恢复上次检索（${fmt(lk.at)}）· ${lk.results.length} 篇 · ${lk.summary || ""}`;
      }
    } catch { /* nothing saved yet */ }
    try {
      const dl = await api("/api/last/download");
      if (dl && Array.isArray(dl.results) && dl.results.length && !state.downloadRows.length) {
        state.downloadRows = dl.results;
        if (dl.dest) $("dlDir").value = dl.dest;
        renderDownloads();
        $("dlStatus").textContent =
          `已恢复上次下载（${fmt(dl.at)}）· ${dl.summary || ""}　未获取的可以继续点「归档」`;
      }
    } catch { /* nothing saved yet */ }
  }

  /* ================================================================ 5. 期刊追新 */  state.journals = [];
  state.journalItems = [];
  state.journalCurrent = "";
  state.selected.journal = new Set();

  function renderJournals() {
    $("jCount").textContent = `${state.journals.length} 个期刊`;
    const root = $("jList");
    if (!state.journals.length) {
      root.innerHTML = `<div class="empty" style="padding:26px 14px">
        <div class="empty-symbol">≣</div><strong>还没有关注期刊</strong>
        <p>用上面的搜索框添加，或点「载入常用期刊」。</p></div>`;
      return;
    }
    root.innerHTML = state.journals.map((j) => `
      <div class="journal-item ${state.journalCurrent === j.issn ? "active" : ""}" data-issn="${esc(j.issn)}">
        <div style="flex:1;min-width:0">
          <div class="jname">${esc(j.title)}</div>
          <div class="jmeta">${esc(j.publisher || "")} · ${esc(j.issn)}</div>
        </div>
        <button class="jdel" data-del="${esc(j.issn)}" title="移除">✕</button>
      </div>`).join("");
  }

  async function saveJournalSubs() {
    try {
      await api("/api/journal/subs", { subs: state.journals });
    } catch (e) { toast("保存期刊列表失败：" + e.message); }
  }

  function addJournal(rec) {
    if (!rec || !rec.issn) return false;
    if (state.journals.some((j) => j.issn === rec.issn)) return false;
    state.journals.push({ issn: rec.issn, title: rec.title, publisher: rec.publisher || "", seen: "" });
    return true;
  }

  const KIND_LABEL = { review: "综述", research: "研究论文", "non-research": "更正/社论/目录" };
  function kindOk(it) {
    const sel = $("jhKind") ? $("jhKind").value : "all";
    const k = it.kind || "research";
    if (sel === "research") return k === "research";
    if (sel === "review") return k === "review";
    if (sel === "noncorr") return k !== "non-research";
    return true;
  }

  function renderJournalItems() {
    const sub = state.journals.find((j) => j.issn === state.journalCurrent);
    const seen = sub ? (sub.seen || "") : "";
    const onlyNew = $("jOnlyNew").checked;
    const items = state.journalItems.filter((it) => (!onlyNew || (it.date || "") > seen) && kindOk(it));
    $("jToDownload").disabled = !items.length;
    $("jMarkRead").disabled = !state.journalItems.length;
    if (!items.length) {
      $("jItems").innerHTML = `<div class="empty"><div class="empty-symbol">✓</div>
        <strong>${onlyNew ? "没有未读文章" : "这个期刊没有返回条目"}</strong>
        <p>${onlyNew ? "取消勾选「只看未读」可以看到全部最近文章。" : "Crossref 里可能还没有收录。"}</p></div>`;
      return;
    }
    $("jItems").innerHTML = `<div class="results-scroll"><table class="results-table">
      <thead><tr><th class="checkcell"></th><th style="width:92px">日期</th><th>标题</th><th>作者</th></tr></thead>
      <tbody>${items.map((w, i) => `<tr>
        <td class="checkcell"><input type="checkbox" data-jsel="${i}" ${state.selected.journal.has(i) ? "checked" : ""}></td>
        <td><span class="aux">${esc(w.date || "")}</span>${w.date > seen ? ' <span class="pill ok">新</span>' : ""}</td>
        <td><div class="rowtitle">${esc(w.title)}${w.kind ? ` <span class="pill info">${esc(KIND_LABEL[w.kind] || w.kind)}</span>` : ""}</div>
            <div class="rowsub"><a class="doi-link" href="${esc(doiUrl(w.doi))}" target="_blank" rel="noopener noreferrer">${esc(w.doi)}</a></div></td>
        <td style="max-width:240px">${esc((w.authors || []).join(", "))}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  async function loadJournal(issn) {
    const sub = state.journals.find((j) => j.issn === issn);
    state.journalCurrent = issn;
    state.selected.journal.clear();
    renderJournals();
    $("jTitle").textContent = sub ? sub.title : issn;
    $("jItems").innerHTML = `<div class="empty"><div class="empty-symbol">…</div><strong>正在拉取最新文章</strong></div>`;
    try {
      const d = await api(`/api/journal/latest?issn=${encodeURIComponent(issn)}&rows=40`);
      state.journalItems = d.items || [];
      renderJournalItems();
      feedback("jFeedback", "");
    } catch (e) {
      $("jItems").innerHTML = `<div class="empty"><div class="empty-symbol">!</div>
        <strong>拉取失败</strong><p>${esc(e.message)}</p></div>`;
    }
  }

  $("jList").addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      const issn = del.dataset.del;
      state.journals = state.journals.filter((j) => j.issn !== issn);
      if (state.journalCurrent === issn) {
        state.journalCurrent = "";
        state.journalItems = [];
        $("jTitle").textContent = "选择左侧期刊";
        $("jItems").innerHTML = `<div class="empty"><div class="empty-symbol">▤</div><strong>选一个期刊看最新文章</strong></div>`;
        $("jToDownload").disabled = true;
      }
      renderJournals();
      saveJournalSubs();
      return;
    }
    const row = e.target.closest("[data-issn]");
    if (row) loadJournal(row.dataset.issn);
  });

  $("jFind").addEventListener("click", doFindJournal);
  $("jq").addEventListener("keydown", (e) => { if (e.key === "Enter") doFindJournal(); });

  async function doFindJournal() {
    const q = $("jq").value.trim();
    if (!q) return;
    feedback("jFeedback", "正在 Crossref 里查找期刊…");
    try {
      const d = await api("/api/journal/search?q=" + encodeURIComponent(q));
      const rows = d.results || [];
      if (!rows.length) { feedback("jFeedback", "没有找到匹配的期刊"); return; }
      feedback("jFeedback", "");
      $("jFindResults").innerHTML = rows.map((r) => `
        <button class="chip" data-add="${esc(r.issn)}" data-title="${esc(r.title)}" data-pub="${esc(r.publisher || "")}">
          ＋ ${esc(r.title)} <span class="aux">${esc(r.issn)}</span></button>`).join("");
      $("jFindResults").dataset.rows = JSON.stringify(rows);
    } catch (e) { feedback("jFeedback", "查找失败：" + e.message); }
  }

  $("jFindResults").addEventListener("click", (e) => {
    const b = e.target.closest("[data-add]");
    if (!b) return;
    const rec = { issn: b.dataset.add, title: b.dataset.title, publisher: b.dataset.pub };
    if (addJournal(rec)) { renderJournals(); saveJournalSubs(); toast("已添加：" + rec.title); }
    else toast("这个期刊已经在列表里了");
  });

  $("jPresets").addEventListener("click", async () => {
    $("jPresets").disabled = true;
    feedback("jFeedback", "正在解析常用期刊的 ISSN…");
    try {
      const p = await api("/api/journal/presets");
      const names = (p.presets || []).filter((n) =>
        !state.journals.some((j) => j.title.toLowerCase() === String(n).toLowerCase()));
      if (!names.length) { feedback("jFeedback", "常用期刊都已经在列表里"); return; }
      const d = await api("/api/journal/resolve", { names });
      let added = 0;
      (d.resolved || []).forEach((r) => { if (addJournal(r)) added++; });
      renderJournals();
      await saveJournalSubs();
      feedback("jFeedback", `已添加 ${added} 个期刊`);
    } catch (e) {
      feedback("jFeedback", "载入失败：" + e.message);
    } finally {
      $("jPresets").disabled = false;
    }
  });

  $("jItems").addEventListener("change", (e) => {
    const cb = e.target.closest("[data-jsel]");
    if (!cb) return;
    const i = Number(cb.dataset.jsel);
    if (cb.checked) state.selected.journal.add(i); else state.selected.journal.delete(i);
  });

  $("jOnlyNew").addEventListener("change", renderJournalItems);
  if ($("jhKind")) $("jhKind").addEventListener("change", renderJournalItems);

  $("jRefreshAll").addEventListener("click", () => {
    if (state.journalCurrent) return loadJournal(state.journalCurrent);
    toast("先在左侧选一个期刊");
  });

  $("jMarkRead").addEventListener("click", () => {
    const sub = state.journals.find((j) => j.issn === state.journalCurrent);
    if (!sub) return;
    const newest = state.journalItems.reduce((a, b) => ((b.date || "") > a ? b.date : a), sub.seen || "");
    sub.seen = newest || "";
    renderJournalItems();
    saveJournalSubs();
    toast("已标记到 " + (newest || "—"));
  });

  $("jToDownload").addEventListener("click", () => {
    const sub = state.journals.find((j) => j.issn === state.journalCurrent);
    const seen = sub ? (sub.seen || "") : "";
    const items = state.journalItems.filter((it) => (!$("jOnlyNew").checked || (it.date || "") > seen) && kindOk(it));
    const dois = [...state.selected.journal].map((i) => items[i] && items[i].doi).filter(Boolean);
    if (!dois.length) return toast("没选中任何条目");
    addDois(dois);
    showTab("download");
  });

  (async function initJournals() {
    try {
      const d = await api("/api/health");
      const subs = (d.settings || {}).journal_subs || [];
      state.journals = subs.filter((j) => j && j.issn).map((j) => ({
        issn: j.issn, title: j.title || j.issn, publisher: j.publisher || "", seen: j.seen || "",
      }));
      renderJournals();
    } catch { /* leave empty */ }
  })();

  /* ================================================================ 6. ResearchGate */
  state.rgRows = [];
  state.selected.rg = new Set();

  function renderRg() {
    const rows = state.rgRows;
    $("rgAll").disabled = $("rgNone").disabled = $("rgResolve").disabled = !rows.length;
    const root = $("rgRoot");
    if (!rows.length) {
      root.innerHTML = `<div class="empty"><div class="empty-symbol">R</div>
        <strong>按作者抓取 ResearchGate 上的论文</strong>
        <p>粘贴作者主页链接 → 打开登录 → 抓取列表 → 解析 DOI → 批量下载。</p></div>`;
      return;
    }
    root.innerHTML = `<div class="results-scroll"><table class="results-table">
      <thead><tr><th class="checkcell"></th><th>标题</th><th>ResearchGate</th></tr></thead>
      <tbody>${rows.map((r, i) => `<tr>
        <td class="checkcell"><input type="checkbox" data-sel="rg" data-i="${i}" ${state.selected.rg.has(i) ? "checked" : ""}></td>
        <td><div class="rowtitle">${esc(r.title)}</div></td>
        <td><a class="doi-link" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">打开</a></td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  $("rgRoot").addEventListener("change", (e) => {
    const cb = e.target.closest('[data-sel="rg"]');
    if (!cb) return;
    const i = Number(cb.dataset.i);
    if (cb.checked) state.selected.rg.add(i); else state.selected.rg.delete(i);
  });
  $("rgOpen").addEventListener("click", async () => {
    const url = $("rgUrl").value.trim();
    if (!/^https?:\/\/(www\.)?researchgate\.net\//i.test(url)) return toast("请填 researchgate.net 的作者主页链接");
    try {
      const d = await api("/api/rg/open", { url });
      if (d.ok) toast("已打开：在弹窗里登录/通过验证，然后回来点「抓取」");
      else toast("打开失败：" + (d.error || ""));
    } catch (e) { toast(e.message); }
  });
  $("rgFetch").addEventListener("click", async () => {
    const url = $("rgUrl").value.trim();
    if (!/^https?:\/\/(www\.)?researchgate\.net\//i.test(url)) return toast("请填 researchgate.net 的作者主页链接");
    $("rgFetch").disabled = true;
    feedback("rgFeedback", "正在浏览器里打开并抓取…（若出现人机验证，请在弹出的窗口里点一下）");
    try {
      const d = await api("/api/rg/list", { url });
      if (d.error) { feedback("rgFeedback", d.error, true); return; }
      feedback("rgFeedback", "已提交，正在逐页抓取…翻几十页需要几分钟，期间可以停止");
      pollJob(d.job_id, (j) => {
        $("rgCount").textContent = `抓取中 ${j.done}/${j.total}`;
      }, (j) => {
        $("rgFetch").disabled = false;
        if (j.state === "error") { feedback("rgFeedback", "抓取失败：" + (j.error || ""), true); return; }
        state.rgRows = j.results || [];
        state.selected.rg.clear();
        renderRg();
        feedback("rgFeedback", j.markdown || "");
        $("rgCount").textContent = `抓到 ${state.rgRows.length} 条`;
      });
    } catch (e) {
      feedback("rgFeedback", "抓取失败：" + e.message, true);
    } finally { $("rgFetch").disabled = false; }
  });
  $("rgAll").addEventListener("click", () => {
    state.rgRows.forEach((_, i) => state.selected.rg.add(i));
    renderRg();
  });
  $("rgNone").addEventListener("click", () => { state.selected.rg.clear(); renderRg(); });
  $("rgResolve").addEventListener("click", async () => {
    const picked = [...state.selected.rg].map((i) => state.rgRows[i]).filter(Boolean);
    const use = picked.length ? picked : state.rgRows;
    if (!use.length) return toast("没有可解析的条目");
    $("rgResolve").disabled = true;
    feedback("rgFeedback", `正在用标题匹配 DOI（${use.length} 条）…`);
    try {
      const job = await api("/api/lookup", {
        text: use.map((r) => r.title).join("\n"), mode: "titles",
        author: "", workers: 5, openalex: true, dedup: true, mailto: $("email").value.trim(),
      });
      pollJob(job.job_id, (d) => {
        feedback("rgFeedback", `匹配中… ${d.done} / ${d.total}`);
      }, (d) => {
        const rows = d.results || [];
        const dois = rows.filter((r) => r.doi).map((r) => r.doi);
        state.lookupRows = rows;
        renderLookup();
        if (dois.length) {
          addDois(dois);
          feedback("rgFeedback", `解析完成：${dois.length} / ${rows.length} 条拿到 DOI，已加入下载队列`);
          showTab("download");
        } else {
          feedback("rgFeedback", "这批标题都没匹配到 DOI", true);
        }
      });
    } catch (e) {
      feedback("rgFeedback", "解析失败：" + e.message, true);
    } finally { $("rgResolve").disabled = false; }
  });

  /* ================================================================ 6. archive */
  function renderArchive(items) {
    const root = $("arRoot");
    if (!items.length) {
      root.innerHTML = `<div class="empty"><div class="empty-symbol">⇪</div>
        <strong>没有归档任何文件</strong><p>换一批文件再试。</p></div>`;
      return;
    }
    root.innerHTML = `<div class="results-scroll"><table class="results-table">
      <thead><tr><th>文件</th><th>文件夹</th><th>结果</th></tr></thead>
      <tbody>${items.map((it) => `<tr>
        <td><div class="dl-file">${esc(it.file || "")}</div><div class="aux">${esc(it.title || "")}</div></td>
        <td>${esc(it.folder || "")}</td>
        <td>${it.error
          ? `<span class="pill bad">失败</span> <span class="aux">${esc(it.error)}</span>`
          : `<span class="pill ok">已归档</span> <span class="aux">${Math.round((it.size || 0) / 1024)} KB</span>`}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  $("arPick").addEventListener("click", async () => {
    $("arPick").disabled = true;
    feedback("arFeedback", "在弹出的窗口里选择文件（可多选）…");
    try {
      const d = await api("/api/archive", {
        title: $("arTitle").value.trim(), first_author: $("arAuthor").value.trim(),
        year: $("arYear").value.trim(), journal: $("arJournal").value.trim(),
        file_template: $("arFileTpl").value.trim(),
        folder_template: $("arFolderTpl").value.trim(),
        dir: $("dlDir").value.trim(),
      });
      if (d.error) { feedback("arFeedback", d.error, true); return; }
      const items = d.items || [];
      const ok = items.filter((i) => i.ok).length;
      feedback("arFeedback", `完成：${ok} / ${items.length} 个文件已归档到 ${d.dest}`);
      renderArchive(items);
      api("/api/settings", {
        save: true,
        settings: { archive_file_template: $("arFileTpl").value.trim(),
                    archive_folder_template: $("arFolderTpl").value.trim() },
      }).catch(() => {});
    } catch (e) {
      feedback("arFeedback", "归档失败：" + e.message, true);
    } finally { $("arPick").disabled = false; }
  });

  countInput();
  countDois();
  renderLookup();
  renderDownloads();
  (async () => {
    await loadSettings();
    await restoreLast();
  })();

  const hash = (location.hash || "").replace("#", "");
  if (["lookup", "download", "search", "chem", "rg", "archive"].includes(hash)) {
    showTab(hash);
    if (hash === "chem") loadChem();
  }

  if (DEMO) {
    $("titles").value = SAMPLE;
    countInput();
  }

  // 机构登录：只对没下到的文献出现，打开出版社页让用户登录一次，会话留在配置目录里
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-carsi]");
    if (!btn) return;
    const row = state.downloadRows[Number(btn.dataset.carsi)] || {};
    btn.disabled = true;
    try {
      await api("/api/carsi", { doi: row.doi });
      toast("已打开浏览器，登录完成后回来重新点下载即可复用会话");
    } catch (err) {
      toast("打开失败：" + err.message);
    } finally {
      setTimeout(() => { btn.disabled = false; }, 3000);
    }
  });


  // ---------------------------------------------------------------- 9. 网页文献
  let labJob = "";
  state.labRows = [];
  state.labSel = new Set();

  function renderLab() {
    const root = $("labRoot");
    if (!root) return;
    if (!state.labRows.length) { root.innerHTML = ""; return; }
    const rows = state.labRows.map((r, i) => `<tr>
        <td><input type="checkbox" data-labsel="${i}" ${state.labSel.has(i) ? "checked" : ""}></td>
        <td>${esc(r.year || "")}</td>
        <td>${esc((r.title || "").slice(0, 110))}
          ${r.field ? `<span class="pill info" style="margin-left:6px">${esc(r.field)}</span>` : ""}
          ${(r.tags || []).length ? `<div class="aux">${r.tags.map((t) => `<span class="tiny-tag" style="margin-right:4px">${esc(t)}</span>`).join("")}</div>` : ""}
          ${r.zh ? `<div class="aux" style="color:#2b6cb0">${esc(r.zh)}</div>` : ""}</td>
        <td>${r.doi ? `<a class="doi-link" href="https://doi.org/${esc(r.doi)}" target="_blank" rel="noopener noreferrer">${esc(r.doi)}</a>` : "—"}</td>
        <td>${r.pdf ? `<a class="doi-link" href="${esc(r.pdf)}" target="_blank" rel="noopener noreferrer">PDF</a>` : "—"}</td>
        <td>${r.supp ? `<a class="doi-link" href="${esc(r.supp)}" target="_blank" rel="noopener noreferrer">SI</a>` : "—"}</td>
      </tr>`).join("");
    root.innerHTML = `<div class="results-scroll" style="max-height:520px;margin-top:12px">
      <table class="results-table"><thead><tr><th></th><th>年</th><th>标题</th><th>DOI</th><th>PDF</th><th>补充材料</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
    $("labCount").textContent = `抓到 ${state.labRows.length} 条，选中 ${state.labSel.size} 条`;
  }

  if ($("labFetch")) {
    $("labFetch").addEventListener("click", async () => {
      const url = $("labUrl").value.trim();
      if (!url) { feedback("labFeedback", "先填页面地址", true); return; }
      $("labFetch").disabled = true;
      feedback("labFeedback", "已提交，正在用浏览器打开并逐轮加载…可能需要一两分钟");
      try {
        const r = await api("/api/lab/list", { url });
        labJob = r.job_id;
        pollJob(labJob, (d) => {
          $("labCount").textContent = `抓取中…已见 ${d.done} 条`;
        }, (d) => {
          $("labFetch").disabled = false;
          if (d.state === "error") { feedback("labFeedback", "抓取失败：" + (d.error || ""), true); return; }
          state.labRows = d.results || [];
          state.labSel = new Set(state.labRows.map((_, i) => i));
          renderLab();
          feedback("labFeedback", d.markdown || "");
        });
      } catch (e) {
        $("labFetch").disabled = false;
        feedback("labFeedback", "抓取失败：" + e.message, true);
      }
    });

    $("labStop").addEventListener("click", async () => {
      if (!labJob) return;
      try { await api(`/api/stop/${labJob}`, {}); } catch {}
      $("labFetch").disabled = false;
    });

    $("labRoot").addEventListener("change", (e) => {
      const cb = e.target.closest("[data-labsel]");
      if (!cb) return;
      const i = Number(cb.dataset.labsel);
      if (cb.checked) state.labSel.add(i); else state.labSel.delete(i);
      $("labCount").textContent = `抓到 ${state.labRows.length} 条，选中 ${state.labSel.size} 条`;
    });

    $("labAll").addEventListener("click", () => {
      state.labSel = new Set(state.labRows.map((_, i) => i));
      renderLab();
    });
    $("labNone").addEventListener("click", () => {
      state.labSel = new Set();
      renderLab();
    });

    // 清空：把抓到的结果和勾选一起丢掉，方便重新抓另一个页面
    $("labClear").addEventListener("click", () => {
      state.labRows = [];
      state.labSel = new Set();
      renderLab();
      const c = $("labCount");
      if (c) c.textContent = "尚未抓取";
      feedback("labFeedback", "");
    });


    $("labToDoi").addEventListener("click", () => {
      const picked = [...state.labSel].map((i) => state.labRows[i]).filter(Boolean);
      if (!picked.length) { feedback("labFeedback", "没有选中条目", true); return; }
      const withDoi = picked.map((x) => x.doi).filter(Boolean);
      const without = picked.filter((x) => !x.doi && x.title).map((x) => x.title);
      if (withDoi.length) addDois(withDoi);
      if (without.length) {
        const box = $("titles");
        box.value = without.join("\n");
        if (typeof countInput === "function") countInput();
        showTab("lookup");
        feedback("searchFeedback",
          `${withDoi.length} 条已有 DOI，已放进下载队列；剩下 ${without.length} 条已填进这里，点「开始批量检索」解析`);
      } else {
        showTab("download");
        feedback("dlFeedback", `${withDoi.length} 条 DOI 已放进下载队列`);
      }
      toast(`已处理 ${picked.length} 条`);
    });
  


  // ---------------------------------------------------------------- 9. 期刊文库
  // 独立的整刊回溯页：抓书目 → 勾选 → 按 刊名/年份/Volume 卷, Issue 期 下载。
  // 和「② DOI → PDF」分开，是因为这里的数据源、目录结构和进度都自成一套。
  (() => {
    const run = document.getElementById("jdRun");
    if (!run) return;
    const st = {
      rows: [], sel: new Set(), journal: "", dest: "",
      job: "", dlJob: "",
    };

    const setFb = (msg, bad) => feedback("jdFeedback", msg, !!bad);
    const ISSN_RE = /^\d{4}-?\d{3}[\dXx]$/;
    // The box accepts an ISSN, a followed journal, or a plain journal title (e.g. what the
    // browser shows after picking a suggestion). Titles are resolved to an ISSN via Crossref.
    async function resolveJournal() {
      const raw = (document.getElementById("jdIssn").value || "").trim();
      if (!raw) return null;
      if (ISSN_RE.test(raw)) {
        const k = state.journals.find((j) => j.issn === raw);
        return { issn: raw.toUpperCase(), title: k ? k.title : "" };
      }
      const known = state.journals.find((j) => j.title.toLowerCase() === raw.toLowerCase());
      if (known) return { issn: known.issn, title: known.title };
      const d = await api("/api/journal/resolve", { names: [raw] });
      const hit = (d.resolved || [])[0];
      return hit && hit.issn ? { issn: hit.issn, title: hit.title || raw } : null;
    }


    // 输入刊名时给候选：选中的那条会把 ISSN 填进来，避免同名前缀的期刊被张冠李戴
    const jdInput = document.getElementById("jdIssn");
    const jdListEl = document.getElementById("jdSuggest");
    if (jdInput && jdListEl) {
      let timer = 0;
      jdInput.addEventListener("input", () => {
        if (jdInput.value.trim().length < 4) { jdListEl.innerHTML = ""; return; }
        clearTimeout(timer);
        timer = setTimeout(async () => {
          try {
            const d = await api("/api/journal/search?q=" + encodeURIComponent(jdInput.value.trim()));
            const rows = (d.results || []).slice(0, 8);
            jdListEl.innerHTML = rows.map((r) => {
              const label = `${r.title} — ${r.issn}${r.publisher ? " · " + r.publisher : ""}`;
              return `<option value="${esc(r.issn)}">${esc(label)}</option>`;
            }).join("");
          } catch (e) { /* 候选失败不打断输入 */ }
        }, 450);
      });
    }

    // ---- journal facts (review time / quartile / zone): optional, downloaded on demand
    const infoBox = document.getElementById("jdInfo");
    const fmt = (v, unit = "") => (v === null || v === undefined || v === "" ? "—" : v + unit);
    function drawInfo(info, status, title) {
      if (!infoBox) return;
      const btn = `<button class="mini-btn" id="jdInfoUpdate" type="button">${status && status.loaded ? "更新期刊数据" : "下载期刊数据"}</button>`;
      const credit = `<div class="aux" style="margin-top:6px">${esc((status && status.attribution) || "")}
        <a href="${esc((status && status.homepage) || "#")}" target="_blank" rel="noopener noreferrer">数据来源</a></div>`;
      if (!status || !status.loaded) {
        infoBox.innerHTML = `<strong>期刊信息（审稿周期 / 分区 / 影响因子）</strong>：还没有数据。点右边按钮从 GitHub 下载约 3 MB，只保存在你的电脑上。 ${btn}${credit}`;
      } else if (!info) {
        infoBox.innerHTML = `<strong>${esc(title || "这本期刊")}</strong>：期刊数据里没有收录这本刊（共 ${esc(status.count)} 本，下载于 ${esc(status.downloaded)}）。 ${btn}`;
      } else {
        const rev = info.avg_days === null ? "审稿周期：暂无可核实的数据"
          : `审稿周期：平均 <b>${fmt(Math.round(info.avg_days))}</b> 天 · 中位 ${fmt(Math.round(info.median_days))} 天 · 最短 ${fmt(Math.round(info.min_days))} / 最长 ${fmt(Math.round(info.max_days))}（样本 ${fmt(info.samples)} 篇）`;
        infoBox.innerHTML = `<strong>${esc(info.name || title || "")}</strong> · ${esc(info.publisher || "")}
          <div style="margin-top:4px">${rev}</div>
          <div class="aux" style="margin-top:2px">JCR ${esc(info.quartile || "—")} · 中科院 ${esc(info.cas || "—")} · 影响因子 ${fmt(info.jif)} · ${esc(info.index || "")} · 数据更新 ${esc(info.updated || status.downloaded || "")} ${btn}</div>${credit}`;
      }
      const b = document.getElementById("jdInfoUpdate");
      if (b) b.addEventListener("click", async () => {
        b.disabled = true; b.textContent = "下载中…";
        try {
          await api("/api/journalinfo/update", {});
          const issn = (st.issn || "");
          const d = await api("/api/journalinfo?issn=" + encodeURIComponent(issn) + "&name=" + encodeURIComponent(title || ""));
          drawInfo(d.info, d.status, title);
          toast("期刊数据已更新");
        } catch (e) { b.disabled = false; b.textContent = "重试"; toast("下载失败：" + e.message); }
      });
    }
    async function showInfo(issn, title) {
      try {
        const d = await api("/api/journalinfo?issn=" + encodeURIComponent(issn) + "&name=" + encodeURIComponent(title || ""));
        drawInfo(d.info, d.status, title);
      } catch (e) { /* info is optional */ }
    }
    api("/api/journalinfo/status").then((s) => drawInfo(null, s, "")).catch(() => {});

    const KIND_ZH = { review: "综述", research: "研究", "non-research": "其他" };
    function kindMatches(r) {
      const sel = (document.getElementById("jdKind") || {}).value || "all";
      const k = r.kind || "research";
      if (sel === "research") return k === "research";
      if (sel === "review") return k === "review";
      if (sel === "noncorr") return k !== "non-research";
      return true;
    }
    function selectByKind() {
      st.sel = new Set(st.rows.map((r, i) => (kindMatches(r) ? i : -1)).filter((i) => i >= 0));
    }

    function drawList() {
      const box = document.getElementById("jdList");
      const bar = document.getElementById("jdBar");
      if (!st.rows.length) { box.innerHTML = ""; if (bar) bar.hidden = true; return; }
      if (bar) bar.hidden = false;
      const cnt = document.getElementById("jdCount");
      if (cnt) cnt.textContent = `${st.rows.length} 篇，选中 ${st.sel.size}`;
      box.innerHTML = `<div class="results-scroll"><table class="results-table">
        <thead><tr><th class="checkcell"></th><th style="width:74px">年</th>
          <th style="width:110px">卷 / 期</th><th style="width:64px">类型</th><th>标题</th><th>DOI</th></tr></thead>
        <tbody>${st.rows.map((r, i) => `<tr>
          <td class="checkcell"><input type="checkbox" data-jdsel="${i}" ${st.sel.has(i) ? "checked" : ""}></td>
          <td>${esc(r.year || "")}</td>
          <td class="aux">${esc((r.volume ? "V" + r.volume : "") + (r.issue ? " I" + r.issue : ""))}</td>
          <td class="aux">${esc(KIND_ZH[r.kind] || "")}</td>
          <td><div class="rowtitle">${esc((r.title || "").slice(0, 120))}</div></td>
          <td><a class="doi-link" href="https://doi.org/${esc(r.doi)}" target="_blank" rel="noopener noreferrer">${esc(r.doi)}</a></td>
        </tr>`).join("")}</tbody></table></div>`;
    }

    document.getElementById("jdList").addEventListener("change", (e) => {
      const cb = e.target.closest("[data-jdsel]");
      if (!cb) return;
      const i = Number(cb.dataset.jdsel);
      if (cb.checked) st.sel.add(i); else st.sel.delete(i);
      drawList();
    });

    run.addEventListener("click", async () => {
      if (!(document.getElementById("jdIssn").value || "").trim()) {
        setFb("先填期刊名或 ISSN", true); return;
      }
      run.disabled = true;
      setFb("正在确认期刊…");
      let hit = null;
      try { hit = await resolveJournal(); } catch (e) { /* handled below */ }
      if (!hit) {
        run.disabled = false;
        setFb("找不到这本期刊。请从下拉建议里选一个，或直接填 ISSN（例如 1759-0876）", true);
        return;
      }
      const issn = hit.issn;
      st.issn = issn;
      st.journal = hit.title || "";
      showInfo(issn, st.journal);
      const from = Number(document.getElementById("jdFrom").value || 0);
      const to = Number(document.getElementById("jdTo").value || 0);
      run.disabled = true;
      setFb("抓取中…每年一次请求，全量通常要十几分钟");
      const box = document.getElementById("jdList");
      box.innerHTML = `<div class="empty"><div class="empty-symbol">…</div><strong>正在抓取书目</strong>
        <p>逐年翻页，过程中列表会一段段出现。</p></div>`;
      api("/api/journal/harvest", { issn, year_from: from, year_to: to }).then((r) => {
        st.job = r.job_id;
        pollJob(r.job_id, (d) => {
          setFb(d.markdown || `已抓 ${d.done} 条`);
          if (d.items && d.items.length) {
            st.rows = d.items.filter((x) => x.doi);
            selectByKind();
            drawList();
          }
        }, (d) => {
          run.disabled = false;
          if (d.state === "error") { setFb("抓取失败：" + (d.error || ""), true); return; }
          st.rows = d.items || [];
          selectByKind();
          drawList();
          const errs = (d.errors || []).length;
          setFb(`共 ${st.rows.length} 篇，已按类型勾选 ${st.sel.size} 篇${errs ? `，${errs} 次请求失败` : ""}`);
        });
      }).catch((e) => { run.disabled = false; setFb("抓取失败：" + e.message, true); });
    });

    document.getElementById("jdKind").addEventListener("change", () => { selectByKind(); drawList(); });
    document.getElementById("jdAll").addEventListener("click", () => {
      st.sel = new Set(st.rows.map((_, i) => i)); drawList();
    });
    document.getElementById("jdNone").addEventListener("click", () => {
      st.sel = new Set(); drawList();
    });
    document.getElementById("jdClear").addEventListener("click", () => {
      st.rows = []; st.sel = new Set();
      drawList();
      document.getElementById("jdResultsWrap").hidden = true;
      setFb("");
    });

    document.getElementById("jdDownload").addEventListener("click", async () => {
      const picked = [...st.sel].map((i) => st.rows[i]).filter((x) => x && x.doi);
      if (!picked.length) { setFb("没有选中条目", true); return; }
      const raw = (document.getElementById("jdIssn").value || "").trim();
      const journal = st.journal || (st.rows[0] && st.rows[0].journal) || raw;
      const dest = ($("dlDir") && $("dlDir").value.trim()) || "";
      const btn = document.getElementById("jdDownload");
      btn.disabled = true;
      setFb(`开始下载 ${picked.length} 篇…`);
      try {
        const r = await api("/api/download", {
          dois: picked.map((x) => x.doi),
          dir: dest,
          scihub: $("scihubEnable") ? $("scihubEnable").checked : true,
          workers: Number(($("dlWorkers") && $("dlWorkers").value) || 3),
          layout: {
            journal,
            items: picked.map((x) => ({ doi: x.doi, year: x.year,
                                        volume: x.volume, issue: x.issue })),
          },
        });
        st.dlJob = r.job_id;
        document.getElementById("jdResultsWrap").hidden = false;
        pollJob(r.job_id, (d) => {
          const prog = document.getElementById("jdProg");
          if (prog) prog.textContent = `${d.done || 0} / ${d.total || picked.length}`;
          const rows = d.results || [];
          if (rows.length) {
            document.getElementById("jdResults").innerHTML =
              `<div class="results-scroll"><table class="results-table">
                <thead><tr><th>DOI</th><th>状态</th><th>文件</th></tr></thead>
                <tbody>${rows.map((x) => `<tr>
                  <td>${esc(x.doi || "")}</td>
                  <td>${esc(x.status || "")}</td>
                  <td class="aux">${esc(x.file || x.note || "")}</td></tr>`).join("")}</tbody>
              </table></div>`;
          }
        }, (d) => {
          btn.disabled = false;
          setFb(d.state === "error" ? ("下载失败：" + (d.error || "")) :
                `下载完成：${(d.results || []).length} 条有结果，目录 ${dest}`, d.state === "error");
        });
      } catch (e) {
        btn.disabled = false;
        setFb("下载失败：" + e.message, true);
      }
    });
  })();

  $("labDownload").addEventListener("click", async () => {
      const picked = [...state.labSel].map((i) => state.labRows[i]).filter(Boolean);
      if (!picked.length) { feedback("labFeedback", "没有选中条目", true); return; }
      // 分流：页面给了 PDF 直链的就直下；只给 DOI 的走我们自己的下载渠道；
      // 两样都没有的，只能拿标题去解析。三类一起处理，不留缺口。
      const direct = picked.filter((x) => x.pdf);
      const byDoi = picked.filter((x) => !x.pdf && x.doi);
      const byTitle = picked.filter((x) => !x.pdf && !x.doi && x.title);
      if (byDoi.length) addDois(byDoi.map((x) => x.doi));
      let msg = [];
      if (direct.length) msg.push(`${direct.length} 篇用页面 PDF 直链`);
      if (byDoi.length) msg.push(`${byDoi.length} 篇放进下载队列（走机构/API/开放获取）`);
      if (byTitle.length) {
        const box = $("titles");
        box.value = byTitle.map((x) => x.title).join("\n");
        if (typeof countInput === "function") countInput();
        msg.push(`${byTitle.length} 条只有标题，已填进「① 标题 → DOI」`);
      }
      feedback("labFeedback", msg.join("；"));
      if (!direct.length) {
        showTab(byDoi.length ? "download" : "lookup");
        return;
      }
      try {
        const r = await api("/api/lab/download", { items: direct, dir: $("dlDir") ? $("dlDir").value.trim() : "" });
        pollJob(r.job_id, (d) => {
          $("labCount").textContent = `直链下载 ${d.done}/${d.total}`;
        }, (d) => {
          if (d.state === "error") { feedback("labFeedback", "直链下载失败：" + (d.error || ""), true); return; }
          feedback("labFeedback", (d.markdown || "完成") + (byDoi.length ? `；另 ${byDoi.length} 篇已进下载队列` : ""));
        });
      } catch (e) {
        feedback("labFeedback", "直链下载失败：" + e.message, true);
      }
    });
  }

  
  reloadBtn();
})();
