/* 界面语言：中文 / English。跟浏览器语言走，右上角可手动切换。
 *
 * 三部分：
 *   1) MAP：关键控件按选择器替换，必须准确。
 *   2) DF_EN：其余文本按内容整节点替换，命中就换，不命中留原文。
 *   3) 计数器与动态渲染：MutationObserver 盯着 DOM，新内容出来后再替换一次，
 *      否则表格、提示语这类运行时生成的文字永远是中文。
 * 替换前把原文记在 WeakMap 里，切回中文时原样还原。
 */
(() => {
  const MAP = [
    { sel: "#statusText", zh: "本地应用 · Crossref / OpenAlex / ChemASAP", en: "Local app · Crossref / OpenAlex / ChemASAP" },
    { sel: "#reloadApp", zh: "刷新界面", en: "Reload UI" },
    { sel: "#quit", zh: "退出", en: "Quit" },
    { sel: '#tabs button[data-tab="lookup"]', zh: "① 标题 → DOI", en: "① Title → DOI" },
    { sel: '#tabs button[data-tab="download"]', zh: "② DOI → PDF", en: "② DOI → PDF" },
    { sel: '#tabs button[data-tab="search"]', zh: "③ 主题搜索", en: "③ Topic search" },
    { sel: '#tabs button[data-tab="chem"]', zh: "④ ChemASAP", en: "④ ChemASAP" },
    { sel: '#tabs button[data-tab="journal"]', zh: "⑤ 期刊追新", en: "⑤ Journal feed" },
    { sel: '#tabs button[data-tab="rg"]', zh: "⑥ ResearchGate", en: "⑥ ResearchGate" },
    { sel: '#tabs button[data-tab="archive"]', zh: "⑦ 归档", en: "⑦ Archive" },
    { sel: '#tabs button[data-tab="lab"]', zh: "⑧ 网页文献", en: "⑧ Lab pages" },
    { sel: '#tabs button[data-tab="journaldl"]', zh: "⑨ 期刊文库", en: "⑨ Journal library" },
    { sel: '#tabs button[data-tab="settings"]', zh: "⚙ 设置", en: "⚙ Settings" },
    { sel: "#start", zh: "开始批量检索", en: "Start batch lookup" },
    { sel: "#startDownload", zh: "⇩ 开始下载", en: "⇩ Start download" },
    { sel: "#pauseDownload", zh: "暂停", en: "Pause" },
    { sel: "#jMarkRead", zh: "标记已读", en: "Mark read" },
    { sel: "#jToDownload", zh: "选中项加入下载", en: "Add selected to downloads" },
    { sel: "#labFetch", zh: "抓取", en: "Fetch" },
    { sel: "#labToDoi", zh: "送去解析 DOI（推荐）", en: "Resolve DOIs (recommended)" },
    { sel: "#labDownload", zh: "下载选中（PDF 直链 / DOI 分流）", en: "Download selected (PDF links / DOIs)" },
  ];

  // 运行时拼出来的计数，词表对不上，单独按模式处理
  const PATTERNS = [
    [/^(\d+)\s*个期刊$/, "$1 journals"],
    [/^(\d+)\s*篇文献$/, "$1 titles"],
    [/^(\d+)\s*个 DOI$/, "$1 DOIs"],
    [/^已处理\s*(\d+)\s*篇$/, "Processed $1"],
    [/^已处理\s*(\d+)\s*\/\s*(\d+)$/, "Processed $1 / $2"],
    [/^(\d+)\s*条$/, "$1 items"],
    [/^(\d+)%\s*匹配度$/, "$1% match"],
    [/^(\d+)%\s*匹配$/, "$1% match"],
    [/^抓取中…已见 (\d+) 条$/, "Fetching… $1 seen"],
    [/^抓取中…已见 (\d+)$/, "Fetching… $1 seen"],
    [/^已见 (\d+) 条$/, "$1 seen"],
  ];

  // 带前后缀的句子按前缀替换，剩下半句保留原文总比全中文好
  const PREFIXES = [
    [/^已恢复上次检索/, "Restored the previous lookup"],
    [/^已恢复上次下载/, "Restored the previous download"],
    [/^已找到 \d+ 条/, "Found results"],
  ];

  const originals = new WeakMap();
  const phOriginals = new WeakMap();
  let lang = "zh";
  let busy = false;

  const hasCJK = (t) => /[\u4e00-\u9fff]/.test(t);
  const norm = (t) => t.replace(/\s+/g, " ").trim();

  function lookup(dict, key) {
    if (dict[key]) return dict[key];
    for (const [re, rep] of PATTERNS) {
      if (re.test(key)) return key.replace(re, rep);
    }
    for (const [re, rep] of PREFIXES) {
      if (re.test(key)) return key.replace(re, rep);
    }
    return "";
  }

  function swapTexts(toLang) {
    const dict = toLang === "en" ? (window.DF_EN || {}) : null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => {
        const p = n.parentElement;
        if (!p || /^(SCRIPT|STYLE|TEXTAREA|TITLE)$/.test(p.tagName)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push(n);

    for (const node of nodes) {
      if (!originals.has(node)) originals.set(node, node.nodeValue);
      const orig = originals.get(node);
      if (!hasCJK(orig)) continue;
      if (!dict) { node.nodeValue = orig; continue; }
      const hit = lookup(dict, norm(orig));
      if (!hit) continue;
      const lead = orig.match(/^\s*/)[0];
      const tail = orig.match(/\s*$/)[0];
      const next = lead + hit + tail;
      if (node.nodeValue !== next) node.nodeValue = next;
    }

    document.querySelectorAll("input[placeholder], textarea[placeholder]").forEach((el) => {
      if (!phOriginals.has(el)) phOriginals.set(el, el.getAttribute("placeholder"));
      const orig = phOriginals.get(el) || "";
      if (!hasCJK(orig)) return;
      el.setAttribute("placeholder", dict ? (lookup(dict, norm(orig)) || orig) : orig);
    });
    document.querySelectorAll("[title]").forEach((el) => {
      if (!el.dataset.dfTitleOrig) el.dataset.dfTitleOrig = el.getAttribute("title") || "";
      const orig = el.dataset.dfTitleOrig;
      if (!hasCJK(orig)) return;
      el.setAttribute("title", dict ? (lookup(dict, norm(orig)) || orig) : orig);
    });
  }


  // 表格单元格常被拆成多个文本节点（"100% 匹配" + "度"），逐节点翻译必然漏。
  // 这一类改成整元素替换：元素内没有链接、子元素不多时，用 textContent 直接换。
  function swapElements(dict) {
    const tags = "TD,TH,BUTTON,SUMMARY,STRONG,P,SPAN";
    document.querySelectorAll(tags).forEach((el) => {
      if (el.dataset.dfElemOrig === undefined) el.dataset.dfElemOrig = el.innerHTML;
      const text = norm(el.textContent || "");
      if (!hasCJK(text)) {
        if (!dict) el.innerHTML = el.dataset.dfElemOrig;
        return;
      }
      if (el.querySelector("a, input, select, textarea, img")) return;
      if (el.children.length > 3) return;
      if (!dict) { el.innerHTML = el.dataset.dfElemOrig; return; }
      const hit = lookup(dict, text);
      if (hit) {
        if (!el.dataset.dfElemDone) {
          el.dataset.dfElemDone = "1";
          el.textContent = hit;
        }
      }
    });
  }

  function apply(next) {
    lang = next === "en" ? "en" : "zh";
    document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
    busy = true;
    swapTexts(lang);
    swapElements(lang === "en" ? (window.DF_EN || {}) : null);
    for (const item of MAP) {
      const el = document.querySelector(item.sel);
      if (el) el.textContent = item[lang] || item.zh;
    }
    const btn = document.getElementById("langToggle");
    if (btn) btn.textContent = lang === "en" ? "中文" : "EN";
    try { localStorage.setItem("df_lang", lang); } catch {}
    setTimeout(() => { busy = false; }, 0);
  }

  function current() {
    try {
      const saved = localStorage.getItem("df_lang");
      if (saved) return saved;
    } catch {}
    return (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";
  }

  function init() {
    const host = document.querySelector(".status-top");
    if (host && !document.getElementById("langToggle")) {
      const btn = document.createElement("button");
      btn.id = "langToggle";
      btn.className = "quit-btn";
      btn.type = "button";
      btn.style.marginLeft = "8px";
      btn.addEventListener("click", () => apply(lang === "en" ? "zh" : "en"));
      host.appendChild(btn);
    }
    apply(current());

    // 表格、提示语都是运行时生成的，DOM 一变就补一次翻译
    const obs = new MutationObserver(() => {
      if (busy || lang !== "en") return;
      busy = true;
      swapTexts("en");
      swapElements(window.DF_EN || {});
      setTimeout(() => { busy = false; }, 0);
    });
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  window.DF_I18N = { apply, current };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
