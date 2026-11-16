/* 界面语言：中文 / English。跟浏览器语言走，右上角可手动切换。
 *
 * 四部分：
 *   1) MAP：关键控件按选择器替换，必须准确。
 *   2) DF_EN：其余文本按内容整节点替换，命中就换，不命中留原文。
 *   3) DF_EN_RULES：带数字、路径等变量的句子，按句式换掉固定的那部分（写在 i18n.en.js）。
 *   4) MutationObserver 盯着 DOM，运行时生成的表格、提示语出来后再替换一次。
 * 替换前把原文记在 WeakMap 里，切回中文时原样还原。
 *
 * app.js 里由程序拼出来的文字（占位符、路径预览等）不在 DOM 替换范围内，
 * 用 DF_I18N.t(中文) 取英文，并监听 "doifinder:lang" 事件在切换语言时重算，
 * 这样中文只写一份（键就是中文原文），英文只放在 i18n.en.js 里。
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
    { sel: "#start", zh: "开始批量检索", en: "Start batch lookup" },
    { sel: "#startDownload", zh: "⇩ 开始下载", en: "⇩ Start download" },
    { sel: "#pauseDownload", zh: "暂停", en: "Pause" },
    { sel: "#jMarkRead", zh: "标记已读", en: "Mark read" },
    { sel: "#jToDownload", zh: "选中项加入下载", en: "Add selected to downloads" },
    { sel: "#labFetch", zh: "抓取", en: "Fetch" },
    { sel: "#labToDoi", zh: "送去解析 DOI（推荐）", en: "Resolve DOIs (recommended)" },
    { sel: "#labDownload", zh: "下载选中（PDF 直链 / DOI 分流）", en: "Download selected (PDF links / DOIs)" },
  ];


  const originals = new WeakMap();     // 文本节点 → 中文原文
  const written = new WeakMap();       // 文本节点 → 我们上次写进去的英文
  const attrOriginals = new WeakMap(); // 元素 → { 属性名: 中文原文 }
  const ATTRS = ["placeholder", "title", "aria-label"];
  let lang = "zh";
  let titleZh = null;     // 页面标题的中文原文
  let busy = false;

  const hasCJK = (t) => /[一-鿿]/.test(t || "");
  const norm = (t) => (t || "").replace(/\s+/g, " ").trim();
  // 中文文本里弯引号与直引号经常混用，查词表前统一
  const normKey = (t) => norm(t).replace(/[“”„″]/g, '"').replace(/[‘’]/g, "'");

  let normDict = null;
  let normSource = null;
  function dictNorm(dict) {
    if (normSource !== dict) {
      normSource = dict;
      normDict = Object.create(null);
      for (const k of Object.keys(dict)) normDict[normKey(k)] = dict[k];
    }
    return normDict;
  }

  // 句式规则（写在 i18n.en.js 的 DF_EN_RULES 里）：[中文句式, 英文句式]
  //   {n} = 一个数字   {s} = 任意文字（可以为空）   英文里用 {1} {2}… 取出对应的变量
  //   也可以直接写 [正则, 函数]。变量里如果还有中文，会再翻译一遍。
  const esc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  let compiled = null;
  let compiledFrom = null;
  function compileRules(rules) {
    if (compiledFrom === rules) return compiled;
    compiledFrom = rules;
    compiled = (rules || []).map(([pat, rep]) => {
      if (pat instanceof RegExp) return [pat, rep];
      let re = "";
      const parts = norm(pat).split(/(\{[ns]\})/);
      parts.forEach((part, i) => {
        if (part === "{n}") re += "(\\d+)";
        else if (part === "{s}") {
          // 末尾的 {s} 为空时，它前面的空格已被 norm() 吃掉，所以这个空格要可选
          if (i === parts.length - 1 && re.endsWith(" ")) re = re.slice(0, -1) + "(?: (.*))?";
          else re += "(.*?)";
        } else re += esc(part);
      });
      return [new RegExp("^" + re + "$"), rep];
    });
    return compiled;
  }

  function fragment(dict, x, depth) {          // 变量片段：有中文就再翻译，翻不动就原样
    return x && hasCJK(x) ? (lookup(dict, x, depth + 1) || x) : (x || "");
  }

  function lookup(dict, key, depth) {
    depth = depth || 0;
    if (!key || depth > 3) return "";
    if (Object.prototype.hasOwnProperty.call(dict, key)) return dict[key];
    const hit = dictNorm(dict)[normKey(key)];
    if (hit) return hit;
    for (const [re, rep] of compileRules(window.DF_EN_RULES)) {
      const m = re.exec(key);
      if (!m) continue;
      if (typeof rep === "function") return rep(m, (x) => fragment(dict, x, depth));
      return rep.replace(/\{(\d)\}/g, (_, i) => fragment(dict, m[+i], depth));
    }
    // "甲 · 乙 · 丙"、"甲；乙" 这类拼接句：每一段都能翻译才整体采用，否则宁可留中文
    for (const sep of [" · ", "；"]) {
      if (key.indexOf(sep) < 0 && !(sep === " · " && key.endsWith(" ·"))) continue;
      const pieces = key.split(sep === " · " ? / · | ·$/ : sep);
      if (pieces.length < 2) continue;
      const out = pieces.map((x) => (hasCJK(x) ? lookup(dict, norm(x), depth + 1) : x));
      if (out.every((x, i) => x || !pieces[i].trim()) && out.some((x, i) => x !== pieces[i])) {
        return out.join(sep === " · " ? " · " : "; ").replace(/ · $/, " ·");
      }
    }
    return "";
  }

  // 给 app.js 用：中文进，英文出（英文界面且词表里有才换，否则原样返回）
  function t(text) {
    if (lang !== "en" || !hasCJK(text)) return text;
    const lead = text.match(/^\s*/)[0];
    const tail = text.match(/\s*$/)[0];
    const hit = lookup(window.DF_EN || {}, norm(text));
    return hit ? lead + hit + tail : text;
  }

  function textNodes() {
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
    return nodes;
  }

  function swapTexts(toLang) {
    const dict = toLang === "en" ? (window.DF_EN || {}) : null;
    for (const node of textNodes()) {
      // 程序又把这个节点改写过（不是我们写的值）：当前值才是新的原文
      if (!originals.has(node) || (written.has(node) && node.nodeValue !== written.get(node))) {
        originals.set(node, node.nodeValue);
        written.delete(node);
      }
      const orig = originals.get(node);
      if (!hasCJK(orig)) continue;
      if (!dict) {
        if (node.nodeValue !== orig) node.nodeValue = orig;
        written.delete(node);
        continue;
      }
      const hit = lookup(dict, norm(orig));
      if (!hit) continue;
      const next = orig.match(/^\s*/)[0] + hit + orig.match(/\s*$/)[0];
      if (node.nodeValue !== next) node.nodeValue = next;
      written.set(node, next);
    }

    document.querySelectorAll("[placeholder], [title], [aria-label]").forEach((el) => {
      let store = attrOriginals.get(el);
      if (!store) { store = {}; attrOriginals.set(el, store); }
      for (const a of ATTRS) {
        if (!el.hasAttribute(a)) continue;
        const cur = el.getAttribute(a) || "";
        // 值不是我们上次写的：程序改过了，把它当作新的原文
        if (!(a in store) || (store[a].shown !== undefined && cur !== store[a].shown)) store[a] = { orig: cur };
        const orig = store[a].orig;
        if (!hasCJK(orig)) continue;
        const shown = dict ? (lookup(dict, norm(orig)) || orig) : orig;
        if (cur !== shown) el.setAttribute(a, shown);
        store[a].shown = shown;
      }
    });
  }

  // 表格单元格常被拆成多个文本节点（"100% 匹配" + "度"），逐节点翻译必然漏。
  // 这一类改成整元素替换：元素内没有链接、子元素不多时，用 textContent 直接换。
  // 原文（innerHTML）必须在任何翻译动作之前记下，否则记下的就是已经翻成英文的内容，切回中文还原不了。
  const ELEMENT_TAGS = "TD,TH,BUTTON,SUMMARY,STRONG,P,SPAN";
  const elemState = new WeakMap();        // 元素 → { orig: 中文原文的 innerHTML, shown: 我们写进去的 innerHTML 或 null }

  function snapshotElements() {
    document.querySelectorAll(ELEMENT_TAGS).forEach((el) => {
      const st = elemState.get(el);
      const cur = el.innerHTML;
      // 没记过，或者程序又改写过它（当前内容不是我们写的）：当前内容就是新的原文
      if (!st || (st.shown !== null && cur !== st.shown)) elemState.set(el, { orig: cur, shown: null });
    });
  }

  function swapElements(dict) {
    document.querySelectorAll(ELEMENT_TAGS).forEach((el) => {
      const st = elemState.get(el);
      if (!st) return;
      if (!dict) {                         // 还原成中文
        if (st.shown !== null) { el.innerHTML = st.orig; st.shown = null; }
        return;
      }
      if (st.shown !== null) return;       // 已经整元素翻译过了
      const text = norm(el.textContent || "");
      if (!hasCJK(text)) return;
      if (el.querySelector("a, input, select, textarea, img")) return;
      if (el.children.length > 3) return;
      const hit = lookup(dict, text);
      if (hit) { el.textContent = hit; st.shown = el.innerHTML; }
    });
  }

  function apply(next) {
    lang = next === "en" ? "en" : "zh";
    document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
    busy = true;
    snapshotElements();
    swapTexts(lang);
    swapElements(lang === "en" ? (window.DF_EN || {}) : null);
    for (const item of MAP) {
      const el = document.querySelector(item.sel);
      if (el) el.textContent = item[lang] || item.zh;
    }
    if (titleZh === null) titleZh = document.title;
    document.title = lang === "en" ? (lookup(window.DF_EN || {}, norm(titleZh)) || titleZh) : titleZh;
    const btn = document.getElementById("langToggle");
    if (btn) btn.textContent = lang === "en" ? "中文" : "EN";
    try { localStorage.setItem("df_lang", lang); } catch {}
    // app.js 里程序拼出来的文字（路径预览、占位符等）在这里重算
    document.dispatchEvent(new CustomEvent("doifinder:lang", { detail: { lang } }));
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
      snapshotElements();
      swapTexts("en");
      swapElements(window.DF_EN || {});
      setTimeout(() => { busy = false; }, 0);
    });
    obs.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS,
    });
  }

  // lookup / t 也给测试和 app.js 用；lang() 返回当前界面语言
  window.DF_I18N = { apply, current, t, lookup: (text) => lookup(window.DF_EN || {}, norm(text)), lang: () => lang };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
