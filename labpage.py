"""实验室主页的论文清单抓取。

很多课题组的 publications 页是 Next.js 生成的卡片列表，每张卡片带标题、期刊、年份
和一个直连 PDF。这类页面没有 API，但结构稳定，正则就能拿到。

取页面走 curl：Python 的 urllib 在这台机器上和该站 TLS 握手会超时（IPv6 优先导致），
curl 强制 IPv4 + HTTP/1.1 就正常。curl 是 Windows 10 以后自带的，不需要额外安装。
"""
from __future__ import annotations

import platform_util
import html as html_mod
import json
import re
import subprocess
import time
import tempfile
import os

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/157.0 Safari/537.36"


def fetch_html(url: str, timeout: int = 45) -> str:
    """用 curl 取页面；失败再退回 urllib。"""
    out_path = os.path.join(tempfile.gettempdir(), "lab_fetch.html")
    try:
        subprocess.run([platform_util.curl_bin(), "-s", "-L", "-4", "--http1.1", "-A", UA,
                        "--max-time", str(timeout), url, "-o", out_path],
                       capture_output=True, timeout=timeout + 10, **platform_util.popen_kwargs())
        if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
            data = open(out_path, "rb").read()
            return data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def clean_title(raw: str) -> str:
    """标题来自图片 alt，里面可能带 <sub>2</sub> 和 &#195; 这类实体，必须清掉。"""
    t = html_mod.unescape(html_mod.unescape(raw or ""))
    t = re.sub(r"<[^>]+>", "", t)          # <sub>, <sup>, <i> 等
    t = re.sub(r"\s+", " ", t).strip()
    # 去掉列表编号 "53. " 和结尾多余标点
    t = re.sub(r"^\s*\d{1,3}[.)、]\s*", "", t)
    # 标题后面常直接粘着作者串：“…Study Case M. Morillas-Rozas, A. López…”
    # 找第一个“首字母.+姓, ”的位置切掉；太靠前的切分不动，避免把标题本身切断
    m = re.search(r"\s([A-Z]\.[\s-]?[A-Z]?\.?\s?[A-Z][A-Za-z\-]+)\s*,", t)
    if m and m.start() > 25:
        t = t[:m.start()]
    # 有些页面把 “(2026)” 也拼在标题里
    t = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", t)
    t = t.rstrip(" .,;")
    return t


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return html_mod.unescape(re.sub(r"\s+", " ", s)).strip()


def parse_publications(page: str, base: str = "") -> list[dict]:
    """从页面里抽出 {title, venue, year, pdf}。

    按卡片容器切块，每块里同时有标题（图片 alt）和 PDF 链接，配对才可靠。
    先剥掉 srcSet/src，否则里面的 "1920w" 会被当成年份。
    """
    page = re.sub(r'srcSet="[^"]*"', "", page)
    page = re.sub(r'\bsrc="[^"]*"', "", page)
    chunks = re.split(r'class="rounded-xl border bg-card', page)
    items: list[dict] = []
    for ch in chunks[1:]:
        m_alt = re.search(r'alt="([^"]{8,300})"', ch)
        if not m_alt:
            continue
        title = html_mod.unescape(m_alt.group(1)).strip()
        m_pdf = re.search(r'href="([^"]+\.pdf[^"]*)"', ch, re.I)
        pdf = html_mod.unescape(m_pdf.group(1)) if m_pdf else ""
        if pdf and not pdf.lower().startswith("http"):
            pdf = (base.rstrip("/") + "/" + pdf.lstrip("/")) if base else pdf
        text = _clean(ch[:8000])
        m_year = re.search(r"\((19|20)\d{2}\)|\b(19|20)\d{2}\b", text)
        year = ""
        if m_year:
            y = re.search(r"(19|20)\d{2}", m_year.group(0))
            year = y.group(0) if y else ""
        venue = ""
        m_venue = re.search(r"([A-Z][A-Za-z0-9&.,'’\- ]{2,45})\s*\(?(?:19|20)\d{2}", text)
        if m_venue:
            venue = m_venue.group(1).strip()
        items.append({"title": title, "venue": venue[:60], "year": year,
                      "pdf": pdf, "url": pdf})
    # 去重
    seen, out = set(), []
    for it in items:
        key = (it["pdf"] or it["title"]).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def scrape(url: str) -> tuple[list[dict], str]:
    try:
        page = fetch_html(url)
    except Exception as e:  # noqa: BLE001
        return [], f"取页面失败：{type(e).__name__}: {e}"
    items = parse_publications(page, base=url)
    if not items:
        return [], "页面上没有识别出论文卡片（可能不是这类结构）"
    return items, ""


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://example.edu/group/publications.html"
    got, err = scrape(u)
    print("err:", err or "(none)")
    print("count:", len(got))
    for it in got[:5]:
        print(f"  [{it['year']}] {it['venue'][:28]} | {it['title'][:50]} | {it['pdf'][:60]}")


# ---------------------------------------------------------------- 浏览器抓全
_CARDS_JS = r"""(() => {
  const out = [];
  const seen = new Set();
  const anchors = [...document.querySelectorAll('a[href]')];
  const blockOf = (a) => {
    let el = a.parentElement, best = "";
    while (el && el !== document.body) {
      const t = (el.innerText || "").replace(/\s+/g, " ").trim();
      if (t.length > 900) break;
      if (t.length >= 30) best = t;
      el = el.parentElement;
    }
    if (!best) {
      const c = a.closest("p, li, td, tr, div");
      if (c) best = (c.innerText || "").replace(/\s+/g, " ").trim().slice(0, 900);
    }
    return best;
  };
  for (const a of anchors) {
    const href = a.href || "";
    const isPdf = /\.pdf(\?|$)/i.test(href);
    const isDoi = /doi\.org\/10\./i.test(href);
    if (!isPdf && !isDoi) continue;
    const block = blockOf(a);
    if (!block) continue;
    const key = href;
    if (seen.has(key)) continue;
    seen.add(key);
    let doi = isDoi ? href.replace(/^.*doi\.org\//i, "") : "";
    if (!doi) {
      const m = block.match(/10\.\d{4,9}\/[^\s,;)]+/);
      if (m) doi = m[0];
    }
    out.push({ pdf: isPdf ? href : "", doi, block });
  }

  // 第三条路径：不依赖链接，直接按"看起来像一条文献"的容器抓。
  // 判据：文本 60–800 字、含四位年份、且提到 DOI/arXiv 或期刊卷期。
  const entryContainers = [];
  const seenText = new Set();
  for (const el of document.querySelectorAll("li, article, tr, .publication-entry, .publication-record")) {
    const t = (el.innerText || "").replace(/\s+/g, " ").trim();
    if (t.length < 60 || t.length > 900) continue;
    if (!/(19|20)\d{2}/.test(t)) continue;
    if (!/10\.\d{4,9}\/|arxiv|doi|journal|proc\.|phys\.|chem\.|nat\.|science/i.test(t)) continue;
    if (seenText.has(t)) continue;
    seenText.add(t);
    const pdfA = el.querySelector('a[href$=".pdf"], a[href*=".pdf?"]');
    const doiA = el.querySelector('a[href*="doi.org/10."]');
    entryContainers.push({
      pdf: pdfA ? pdfA.href : "",
      doi: doiA ? (doiA.href || "").replace(/^.*doi\.org\//i, "") : "",
      block: t,
    });
  }
  const best = entryContainers.length > out.length ? entryContainers : out;
  return JSON.stringify({ items: best, h: document.body.innerText.length });
})()"""

_MORE_JS = """(() => {
  const btns = [...document.querySelectorAll('button, a')].filter((b) =>
    /load more|show more|more results|加载更多|显示更多/i.test(b.textContent || ""));
  btns.slice(0, 3).forEach((b) => b.click());
  window.scrollTo(0, document.body.scrollHeight);
  return btns.length;
})()"""



def parse_block(text: str) -> dict:
    """把一条引文块拆成字段：标题、作者串、期刊、年、卷、期、页、DOI。

    设计原则：只做"有把握的切分"，拿不准的字段留空。宁可少一项，也不给错的值。
    """
    t = " ".join((text or "").split())
    t = re.sub(r"^\s*\d{1,3}[.)、]\s*", "", t)          # 开头的序号
    doi_m = re.search(r"10\.\d{4,9}/[^\s,;)]+", t)
    doi = doi_m.group(0).rstrip(".,;") if doi_m else ""
    if doi_m:
        t = (t[:doi_m.start()] + " " + t[doi_m.end():]).strip()
    if not doi:
        # 很多课题组页面只给 arXiv 号，没有 DOI。arXiv 有官方 DOI 前缀，可以合成。
        ax = re.search(r"arxiv[:\s]*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", t, re.I)
        if ax:
            doi = "10.48550/arXiv." + ax.group(1)
            t = (t[:ax.start()] + " " + t[ax.end():]).strip()

    # 年份：优先取最后一个 (YYYY)，退而求其次取行内最后一个四位年
    ym = re.findall(r"\(((?:19|20)\d{2})\)", t)
    year = ""
    if ym:
        year = ym[-1]
    else:
        ys = re.findall(r"\b((?:19|20)\d{2})\b", t)
        year = ys[-1] if ys else ""

    # 作者串起点：形如 "M. Curie" / "A. Einstein" 之后跟逗号或 and
    cut = None
    m = re.search(r"\s([A-Z]\.[\s-]?[A-Z]?\.?\s?[A-Z][A-Za-z\-]+)\s*(?:,|\band\b)", t)
    if m and m.start() > 20:
        cut = m.start()
    title = (t[:cut] if cut else t).strip()
    rest = (t[cut:] if cut else "").strip()

    # 期刊、卷(期)、页。年份可能出现在两处：
    #   末尾带括号 “期刊 163(23) (2025)”      → 卷期页在年份之前
    #   中间不带括号 “期刊 2025, 16, 22, 5466” → 卷期页在年份之后
    journal = vol = issue = pages = ""
    if rest:
        tail_end = re.search(r"\(\s*(?:19|20)\d{2}\s*\)\s*$", rest)
        if tail_end:
            head = rest[:tail_end.start()]
        else:
            y2 = re.search(r"\b(?:19|20)\d{2}\b", rest)
            head = rest[:y2.start()] if y2 else rest
            tail_after = rest[y2.end():] if y2 else ""
        jm = re.search(r"([A-Z][A-Za-z.&'\- ]{4,60}?)\s*(\d{1,4})\s*(?:\((\d{1,3})\))?", head)
        if jm:
            journal = jm.group(1)
            vol = jm.group(2) or ""
            issue = jm.group(3) or ""
        else:
            journal = head
        journal = re.sub(r"^(?:(?:and\s+)?[A-Z]\.(?:\s*-?\s*[A-Z]\.)*\s*[A-Z][A-Za-z\-]*\s*,?\s*)+",
                         "", journal.strip(" ,.")).strip(" ,.")
        if not tail_end:
            nums = re.findall(r"\b(\d{1,5})\b", tail_after)
            if not vol and nums:
                vol = nums[0]
            if not issue and len(nums) > 1:
                issue = nums[1]
        pm = re.search(r"(\d{1,5})\s*[–\-]\s*(\d{1,5})", rest)
        if pm:
            pages = f"{pm.group(1)}-{pm.group(2)}"
    title = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", title).strip(" .,;")
    if not year:
        ys = re.findall(r"\b(19|20)\d{2}\b", text or "")
        year = ys[-1] if ys else ""
    return {"title": clean_title(title), "authors": rest[:0], "journal": journal,
            "year": year, "volume": vol, "issue": issue, "pages": pages, "doi": doi}



def _merge_items(items: list[dict]) -> list[dict]:
    """同一条文献常有两条记录：一条带 PDF 链接，一条带 DOI 链接。按 DOI 合并。"""
    import re as _re

    def norm(t: str) -> str:
        return _re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (t or "").lower())[:90]

    merged: dict[str, dict] = {}
    order: list[str] = []
    for it in items:
        key = (it.get("doi") or "").lower().strip() or "t:" + norm(it.get("title"))
        if not key or key == "t:":
            continue
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(it)
            order.append(key)
            continue
        # 补空缺字段，不覆盖已有值
        for f in ("pdf", "supp", "doi", "title", "year", "journal",
                  "volume", "issue", "pages"):
            if not cur.get(f) and it.get(f):
                cur[f] = it[f]
    return [merged[k] for k in order]


def crawl(url: str, profile_dir: str = "", timeout: int = 240,
          max_rounds: int = 80) -> tuple[list[dict], str]:
    """用真实浏览器打开页面，反复触发"加载更多"，把卡片抓成列表。

    静态抓只能拿到第一屏；这类 Next.js 站点要 JS 才会继续加载，所以借浏览器。
    """
    try:
        import browser as B
    except Exception as e:  # noqa: BLE001
        return [], f"浏览器组件不可用：{e}"
    if not profile_dir:
        profile_dir = os.path.join(platform_util.data_dir(), "browser-profile" + B.browser_tag())
    if not B.find_browser():
        return [], "没有找到 Chrome 或 Edge"

    b = B._shared(profile_dir)
    tab = None
    items: list[dict] = []
    seen: set[str] = set()
    try:
        tab = b.open_tab(url)
        tab.enable()
        # 等页面把卡片渲染出来再开始数：Next.js 客户端渲染要几秒，早读只会读到空
        warm = time.time()
        while time.time() - warm < 30:
            try:
                probe = tab.cdp.call("Runtime.evaluate",
                                     {"expression": "document.querySelectorAll('img[alt]').length",
                                      "returnByValue": True}, timeout=15)
                if (probe.get("result", {}).get("value") or 0) > 0:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        time.sleep(1.5)
        started = time.time()
        stalled = 0
        while time.time() - started < timeout:
            try:
                got = tab.cdp.call("Runtime.evaluate",
                                   {"expression": _CARDS_JS, "returnByValue": True},
                                   timeout=30).get("result", {}).get("value") or "{}"
                data = json.loads(got)
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
                continue
            for it in data.get("items") or []:
                # 去重键必须包含 DOI：只有 DOI 没有 PDF 的条目，前面三项都是空，
                # 用它们做键会把这类条目整批丢掉（DOI 列全是"—"就是这么来的）
                key = (it.get("pdf") or it.get("supp") or it.get("doi")
                       or it.get("title") or "").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                text = it.get("text") or ""
                y = re.search(r"\b(19|20)\d{2}\b", text)
                parsed = parse_block(it.get("block") or it.get("text") or "")
                items.append({
                    "title": parsed["title"],
                    "pdf": it.get("pdf") or "",
                    "supp": it.get("supp") or "",
                    "doi": (it.get("doi") or parsed["doi"] or "").lower().strip(),
                    "year": parsed["year"] or (y.group(0) if y else ""),
                    "journal": parsed["journal"],
                    "volume": parsed["volume"], "issue": parsed["issue"],
                    "pages": parsed["pages"],
                    "text": text,
                })
            if len(items) >= max_rounds * 20:
                break
            before = len(items)
            try:
                tab.cdp.call("Runtime.evaluate",
                             {"expression": _MORE_JS, "returnByValue": True}, timeout=20)
            except Exception:  # noqa: BLE001
                pass
            # 点完按钮后站点要拉数据再渲染，等久一点；短等待会被误判成"到底了"
            time.sleep(2.0)
            if len(items) == before:
                stalled += 1
                if stalled >= 30:
                    break
            else:
                stalled = 0
        return _merge_items(items), ""
    except Exception as e:  # noqa: BLE001
        return _merge_items(items), f"{type(e).__name__}: {e}"
    finally:
        try:
            if tab is not None:
                b.close_tab(tab.target_id)
                tab.detach()
        except Exception:  # noqa: BLE001
            pass


def _fetch_pdf(url: str, referer: str = "") -> bytes | None:
    """下载 PDF。先走 curl（强制 IPv4），失败再退回 urllib。

    这台机器上 urllib 到这些站点会卡在 TLS 握手（IPv6 优先导致超时），curl 不会。
    """
    import subprocess
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "lab_pdf.bin")

    def _complete(data: bytes) -> bool:
        # 截断的 PDF 仍然带 %PDF 头，WPS 打开是一堆空白页。必须看到结尾标记才算完整。
        return (bool(data) and data[:4].startswith(b"%PDF")
                and b"%%EOF" in data[-4096:])

    # 两次机会：第一次 300 秒，不够就再来一次 600 秒。服务器慢（约 6 KB/s）。
    for max_time in (300, 600):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            subprocess.run([platform_util.curl_bin(), "-s", "-L", "-4", "--http1.1", "-A", UA,
                            "-e", referer or "https://", "--max-time", str(max_time),
                            url, "-o", tmp],
                           capture_output=True, timeout=max_time + 30,
                           **platform_util.popen_kwargs())  # 不弹黑色控制台窗口
            if os.path.exists(tmp):
                data = open(tmp, "rb").read()
                if _complete(data):
                    return data
        except Exception:  # noqa: BLE001
            pass
    return None


def download_pdfs(items: list[dict], dest: str, progress=None, stop_event=None) -> list[dict]:
    """把 items 里的直链 PDF 存到 dest。文件名用标题，冲突时加序号。"""
    import os
    import re as _re
    import urllib.request

    out = []
    for i, it in enumerate(items, 1):
        if stop_event is not None and stop_event.is_set():
            break
        url = it.get("pdf") or ""
        title = (it.get("title") or f"paper_{i}").strip()
        year = (it.get("year") or "").strip() or "unknown"
        # 按年份分类：lab/2026/2026_Title.pdf，年份缺失的放 lab/unknown/
        folder = os.path.join(dest, year)
        os.makedirs(folder, exist_ok=True)
        safe = _re.sub(r'[\\/:*?"<>|]+', " ", title)
        safe = _re.sub(r"\s+", " ", safe).strip()[:90] or f"paper_{i}"
        safe = f"{year}_{safe}"
        # 不同论文可能抽出同一个标题（页面结构所致），用 PDF 链接哈希兜底区分
        if it.get("pdf"):
            safe = safe + "_" + str(abs(hash(it["pdf"])) % 10000)
        rec = {"title": title, "pdf": url, "file": "", "status": "failed", "note": ""}
        if not url:
            rec["note"] = "没有 PDF 直链"
            out.append(rec)
            continue
        saved = []
        try:
            data = _fetch_pdf(url)
            if data is None:
                rec["note"] = "返回内容不是 PDF"
                out.append(rec)
                continue
            path = os.path.join(folder, safe + ".pdf")
            n = 1
            while os.path.exists(path):
                n += 1
                path = os.path.join(folder, f"{safe} ({n}).pdf")
            open(path, "wb").write(data)
            saved.append(path)
        except Exception as e:  # noqa: BLE001
            rec["note"] = f"{type(e).__name__}: {str(e)[:60]}"
            out.append(rec)
            continue
        # 有补充材料就一起下，存成 "_SI" 后缀
        supp = it.get("supp") or ""
        if supp:
            try:
                sdata = _fetch_pdf(supp)
                if sdata is not None:
                    si_path = os.path.join(folder, safe + "_SI.pdf")
                    open(si_path, "wb").write(sdata)
                    saved.append(si_path)
            except Exception:  # noqa: BLE001 - 补充材料失败不影响正文
                pass
        rec.update({"file": saved[0], "files": saved, "status": "downloaded",
                    "note": f"{len(data)//1024} KB" + (" +SI" if len(saved) > 1 else "")})
        out.append(rec)
        if progress:
            progress(i, len(items), rec)
    return out
