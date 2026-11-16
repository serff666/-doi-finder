"""Batch PDF download.

Resolution order: open-access sources first (arXiv, OpenAlex, Unpaywall, Europe PMC,
PMC, publisher landing pages), then — only if nothing was found and the user left the
option on — Sci-Hub / Sci-Net as a paywall fallback (see scihub.py).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import platform_util
import sources
import scihub
import browser
import elsevier
import wiley
import routing
import preprints

DEFAULT_TEMPLATE = "{first_author}_{year}_{title}"
INDEX_NAME = ".doi-index.json"
MAX_BYTES = 150 * 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# The institutional channel borrows a campus subscription, and publishers police
# it by volume: Elsevier has shut down whole university EZproxy ranges for a day
# over "systematic downloading". One article at a time, spaced out, is the
# difference between a slow batch and getting your whole institution blocked.
INST_MIN_INTERVAL = 3.0
_inst_lock = threading.Lock()
_inst_last = [0.0]

def _inst_gate() -> None:
    """Serialise institutional fetches and keep at least INST_MIN_INTERVAL apart."""
    with _inst_lock:
        gap = INST_MIN_INTERVAL - (time.time() - _inst_last[0])
        if gap > 0:
            time.sleep(gap)
        _inst_last[0] = time.time()


# Names Windows refuses to create as files or folders, whatever the extension.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name: str, maxlen: int = 110) -> str:
    name = _BAD.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
    if name.split(".")[0].strip().upper() in _RESERVED:
        name = "_" + name
    return name or "untitled"


def _fit_name(dest: str, base: str, ext: str) -> str:
    """Shorten `base` so the file can actually be created.

    Windows fails past ~260 characters for the whole path; Linux/macOS fail past 255 *bytes* for one
    name, and Chinese titles take 3 bytes per character. Room is left for a " (99)" duplicate suffix.
    """
    room_path = 250 - len(os.path.abspath(dest)) - 1 - len(ext) - 5 if os.name == "nt" else 10 ** 6
    room_bytes = 255 - len(ext.encode()) - 5
    limit = max(24, min(len(base), room_path))
    base = base[:limit]
    while len(base.encode("utf-8")) > room_bytes and len(base) > 1:
        base = base[:-1]
    return base.rstrip(" .") or "untitled"


_TAG_RE = re.compile(r"<[^>]{1,20}>")
_ENT_RE = re.compile(r"&(?:amp|lt|gt|quot|apos|#\d{1,4});")


def clean_title(text: str) -> str:
    """Crossref titles arrive with markup: '<i>Drosophila</i>', 'H<sub>2</sub>O'.

    Left alone it ends up in filenames as "i Drosophila i" and
    "[FS sub 2 sub O sub 6 sub ]", so strip the tags and the entities they
    leave behind.
    """
    s = _TAG_RE.sub("", text or "")
    s = _ENT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_name(meta: dict, doi: str, template: str = DEFAULT_TEMPLATE) -> str:
    values = {
        "first_author": sanitize(meta.get("first_author") or "anon", 40),
        "year": sanitize(str(meta.get("year") or "nd"), 8),
        "title": sanitize(clean_title(meta.get("title") or ""), 90),
        "journal": sanitize(meta.get("journal") or "", 40),
        "doi": sanitize(doi.replace("/", "_"), 80),
    }
    tpl = template or DEFAULT_TEMPLATE
    out = tpl
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    out = sanitize(out, 130)
    return out


def journal_folder(journal: str, year, volume, issue) -> str:
    """Relative folder: <Journal>/<Year>/Volume <V>, Issue <I>."""
    vol, iss = str(volume or "").strip(), str(issue or "").strip()
    if vol and iss:
        vi = f"Volume {vol}, Issue {iss}"
    elif vol:
        vi = f"Volume {vol}"
    elif iss:
        vi = f"Issue {iss}"
    else:
        vi = "No volume-issue info"
    yr = str(year or "").strip()[:4] or "Unknown year"
    return "/".join([sanitize(journal or "Unknown journal", 80), sanitize(yr, 20), sanitize(vi, 60)])


def load_index(dest: str) -> dict:
    path = os.path.join(dest, INDEX_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_index(dest: str, index: dict):
    path = os.path.join(dest, INDEX_NAME)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _looks_pdf(head: bytes) -> bool:
    return b"%PDF" in head[:1024]


def _get(url: str, timeout: int = 40, referer: str = "") -> tuple[bytes | None, str, str, str]:
    """Return (body, content_type, final_url, error)."""
    try:
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,ja;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            final_url = r.geturl() or url
            buf = bytearray()
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    return None, ctype, final_url, "文件超过 150 MB"
        return bytes(buf), ctype, final_url, ""
    except urllib.error.HTTPError as e:
        return None, "", url, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, "", url, type(e).__name__


def fetch_pdf(url: str, timeout: int = 40, _depth: int = 0, _referer: str = "",
              _tries: int = 2) -> tuple[bytes | None, str, str]:
    """Download a PDF, following one level of HTML landing page if needed.

    Returns (pdf_bytes, error, resolved_url). A publisher article page is not a
    failure: it usually advertises the PDF through <meta name="citation_pdf_url">.
    Transient network errors are retried; HTTP 4xx answers are not.
    """
    body = ctype = None
    final_url = url
    err = ""
    for attempt in range(max(1, _tries)):
        body, ctype, final_url, err = _get(url, timeout, referer=_referer)
        if body is not None:
            break
        if err.startswith("HTTP 4") or err.startswith("文件超过"):
            break
        if attempt + 1 < _tries:
            time.sleep(0.8 * (attempt + 1))
    if body is None:
        return None, err, final_url
    if not body:
        return None, "空响应", final_url
    if _looks_pdf(body):
        return body, "", final_url

    is_html = "html" in ctype or body[:400].lstrip().lower().startswith((b"<!doctype", b"<html"))
    if is_html and _depth < 1:
        try:
            links = sources.pdf_links_from_html(final_url, body)
        except Exception:  # noqa: BLE001
            links = []
        for link in links[:3]:
            data, ierr, iurl = fetch_pdf(link, timeout, _depth + 1, _referer=final_url)
            if data:
                return data, "", iurl
        return None, "页面里没有 PDF 链接（可能需要订阅）" if not links else "PDF 链接取不到（可能需要订阅）", final_url

    if "html" in ctype:
        return None, "返回网页而不是 PDF（可能需要订阅）", final_url
    return None, f"不是 PDF（{ctype or '未知类型'}）", final_url


def _save(dest: str, base: str, data: bytes, force: bool) -> tuple[str | None, str]:
    """Write the PDF under a free filename; returns (path, name)."""
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    # 拿不到 PDF 时可能拿到结构化全文（XML/HTML），扩展名要跟着变，别把 XML 存成 .pdf
    if data[:4] == b"%PDF":
        ext = ".pdf"
    elif data.lstrip()[:5] in (b"<?xml", b"<html", b"<!DOC") or b"<ce:para" in data[:4000]:
        ext = ".xml"
    else:
        ext = ".txt"
    base = _fit_name(dest, base, ext)
    name = base + ext
    path = os.path.join(dest, name)
    n = 2
    for _ in range(1000):
        try:
            # "x" = create exclusively: two workers writing the same title can never overwrite each other
            with open(path, "wb" if force and n == 2 else "xb") as fh:
                fh.write(data)
            return path, name
        except FileExistsError:
            path = os.path.join(dest, f"{base} ({n}){ext}")
            name = os.path.basename(path)
            n += 1
        except Exception as e:  # noqa: BLE001
            return None, str(e)
    return None, "同名文件太多"


def _try_scihub(doi: str, conf: dict, should_stop=None) -> tuple[bytes | None, str, str, str]:
    """Sci-Hub / Sci-Net fallback. Returns (pdf, error, source, page_url)."""
    mirrors = conf.get("mirrors") or scihub.DEFAULT_MIRRORS
    scinet = conf.get("scinet") or scihub.DEFAULT_SCINET
    timeout = float(conf.get("timeout") or 30)
    return scihub.fetch(doi, mirrors=mirrors, scinet=scinet, timeout=timeout, should_stop=should_stop,
                        insecure=bool(conf.get("insecure")))


DEADLINE_PER_DOI = 240  # seconds; past this no further source is started for the DOI

# Direct PDF links a finder already knows (ChemRxiv's own search returns them), so the download
# does not have to rediscover them.  Keyed by lower-case DOI.
_PDF_HINTS: dict[str, str] = {}


def register_pdf_hint(doi: str, url: str) -> None:
    if doi and url:
        _PDF_HINTS[doi.strip().lower()] = url


def fetch_preprint(pick: dict, profile: str = "", timeout: float = 75.0) -> tuple[bytes | None, str, str]:
    """PDF bytes of a preprint candidate (from preprints.find): the direct link if the search gave
    one, then the DOI landing page, then a real browser (ChemRxiv sits behind a JS challenge)."""
    errs = []
    for u in [pick.get("pdf_url"), "https://doi.org/" + urllib.parse.quote(pick["doi"], safe="/()")]:
        if not u:
            continue
        data, err, resolved = fetch_pdf(u)
        if data:
            return data, "", resolved or u
        errs.append(err)
    if browser.find_browser():
        try:
            prof = profile or os.path.join(platform_util.data_dir(), "browser-profile" + browser.browser_tag())
            data, err, burl = browser.fetch_pdf(pick.get("pdf_url") or pick["doi"], prof,
                                                timeout=max(60.0, timeout))
        except Exception as e:  # noqa: BLE001
            data, err, burl = None, f"{type(e).__name__}: {e}", ""
        if data:
            return data, "", burl or ("https://doi.org/" + pick["doi"])
        errs.append(err or "浏览器没拿到 PDF")
    return None, next((e for e in errs if e), "没拿到 PDF"), ""


def download_one(doi: str, dest: str, email: str, template: str,
                 index: dict, force: bool = False, scihub_conf: dict | None = None,
                 inst_conf: dict | None = None, step=None, stop=None) -> dict:
    t_start = time.monotonic()
    paused0 = float(getattr(stop, "paused_seconds", lambda: 0.0)())

    def active_seconds() -> float:
        """Time spent working on this DOI, excluding time the user had the job paused."""
        paused = float(getattr(stop, "paused_seconds", lambda: 0.0)()) - paused0
        return time.monotonic() - t_start - max(0.0, paused)

    def stopped() -> bool:
        # a pausable stop signal blocks here while paused, so pause also works mid-DOI
        return bool(stop and stop.is_set())

    def say(text: str) -> None:
        if step:
            try:
                step(text)
            except Exception:  # noqa: BLE001
                pass

    doi = (doi or "").strip().lower()
    rec = {"doi": doi, "status": "failed", "file": "", "url": "", "source": "",
           "note": "", "title": "", "year": ""}
    if not re.match(r"^10\.\d{4,9}/\S+$", doi):
        rec["status"] = "invalid"
        rec["note"] = "DOI 格式不正确"
        return rec

    hit = index.get(doi)
    if hit and not force:
        path = os.path.join(dest, hit.get("file", ""))
        if os.path.exists(path):
            rec.update({"status": "exists", "file": hit["file"], "title": hit.get("title", ""),
                        "note": "已存在，跳过"})
            return rec

    try:
        meta = sources.work_meta(doi, email)
    except Exception:  # noqa: BLE001
        meta = {"title": "", "year": "", "first_author": "", "journal": ""}
    rec["title"] = meta.get("title", "")
    rec["year"] = str(meta.get("year", ""))
    if not meta.get("title"):
        meta["title"] = doi.replace("/", "_")

    diag: list[str] = []
    oa_cache: list[dict] | None = None
    oa_real: int | None = None      # None = never looked up; 0 = looked up, nothing found

    def oa_candidates() -> list[dict]:
        """Resolve OA links the first time something actually needs them.

        Most Elsevier/Wiley downloads end at the publisher API, so resolving the
        ten OA sources up front is pure latency on the common path. This runs
        only when try_oa is reached.
        """
        nonlocal oa_cache, oa_real
        if oa_cache is not None:
            return oa_cache
        say("查询开放获取来源…")
        try:
            oa_cache = sources.resolve_oa(doi, email, diag=diag)
        except Exception:  # noqa: BLE001
            oa_cache = []
        oa_real = len(oa_cache)      # real open-access records, before the DOI landing page is added
        # DOI 本身就指向出版社落地页，OA 文章在那页上直接给出 citation_pdf_url。
        oa_cache.insert(0, {"url": "https://doi.org/" + urllib.parse.quote(doi, safe="/()"),
                            "source": "DOI 落地页"})
        hint = _PDF_HINTS.get(doi)          # a link the finder already holds beats rediscovering it
        if hint:
            oa_cache.insert(0, {"url": hint, "source": "ChemRxiv"})
            oa_real += 1
        return oa_cache

    errors: list[str] = []
    requested_doi = doi          # what the caller asked for; the index must answer to this key
    # 预印本 DOI 先换成正式发表版本：SSRN 上要登录，正式版常常是开放获取
    try:
        import sources as _src
        alt = _src.published_version(doi, (meta or {}).get("title") or "", email or "")
        if alt and alt != doi.lower():
            errors.append(f"改用正式发表版本：{alt}")
            say(f"这是预印本，改下正式版 {alt}")
            doi = alt
    except Exception:  # noqa: BLE001
        pass

    base = build_name(meta, doi, template)
    page_hint = ""
    oa_last_err = ""
    oa_blocked = False

    conf = scihub_conf or {}
    inst = inst_conf or {}
    use_inst = bool(inst.get("enabled") and inst.get("profile"))
    if use_inst and not browser.find_browser():
        use_inst = False
        errors.append("机构通道：没有找到 Chrome / Edge")

    def finish(got: dict) -> dict:
        path, name = _save(dest, base, got["data"], force)
        if not path:
            rec.update({"status": "failed", "note": f"写入失败：{name}"})
            return rec
        entry = {"file": name, "title": meta.get("title", ""), "saved_at": int(time.time()),
                 "source": got["source"], "url": got["url"]}
        index[doi] = entry
        index[requested_doi] = entry   # else a preprint DOI would be re-downloaded on every run
        rec.update({"status": "downloaded", "file": name, "url": got["url"],
                    "source": got["source"],
                    "note": f"{len(got['data']) // 1024} KB · "
                            + ("PDF" if got["data"][:4] == b"%PDF" else "全文（非 PDF）")})
        return rec

    # ---------------------------------------------------------------- sources
    def try_scihub() -> dict | None:
        if not conf.get("enabled"):
            return None
        say("尝试 Sci-Hub / Sci-Net…")
        data, sh_err, sh_src, sh_page = _try_scihub(doi, conf, stop.is_set if stop else None)
        nonlocal page_hint
        page_hint = sh_page or page_hint
        if not data:
            errors.append(sh_err)
            return None
        return {"data": data, "source": sh_src, "url": sh_page}

    def try_inst() -> dict | None:
        if not use_inst:
            return None
        say("用已登录的浏览器打开出版社页面…")
        _inst_gate()
        # 网络慢不等于没权限：先按原超时试一次，失败再用双倍超时重试一次。
        base_timeout = float(inst.get("timeout") or 75)
        data, ierr, iurl = None, "", ""
        for attempt, tmo in enumerate((base_timeout, base_timeout * 2), 1):
            data, ierr, iurl = browser.fetch_pdf(doi, inst["profile"], timeout=tmo,
                                                 login_url=inst.get("login_url") or "")
            if data:
                break
            if attempt == 1:
                say(f"机构通道第一遍没拿到，加倍超时重试（{int(tmo * 2)} 秒）…")
        nonlocal page_hint
        page_hint = iurl or page_hint
        if not data:
            # 说不清是没权限还是网络慢时，别把话说死。网络慢的情况用户可以自己重试。
            errors.append(f"{ierr}（已按原超时和双倍超时各试一次；"
                          f"若确认有订阅权限，多半是网络慢，重试即可）")
            return None
        return {"data": data, "source": "机构浏览器", "url": iurl}

    def try_oa() -> dict | None:
        nonlocal oa_last_err, oa_blocked
        cands = oa_candidates()
        if oa_real == 0:
            errors.append("没有开放获取记录")
        for c in cands[:8]:
            if stopped():
                break
            data, err, resolved = fetch_pdf(c["url"])
            if not data:
                oa_last_err = err
                if "403" in err or "401" in err or "订阅" in err:
                    oa_blocked = True
                continue
            return {"data": data, "source": c["source"], "url": resolved or c["url"]}
        errors.append("开放获取：" + (oa_last_err or "无可用链接"))
        # OA 文章的 DOI 落地页往往要过 JS 挑战，纯 HTTP 拿不到 PDF 链接。
        # 有浏览器就直接在真实浏览器里打开 doi.org，OA 文章这一步就能下。
        if browser.find_browser():
            say("纯 HTTP 没拿到（出版社常挡爬虫），改用浏览器打开…")
            try:
                prof = inst.get("profile") or os.path.join(
                    platform_util.data_dir(), "browser-profile" + browser.browser_tag())
                # 优先用解析出来的 PDF 直链，没有就退回 doi.org
                first = next((c.get("url") for c in cands if (c.get("url") or "").endswith("/pdf")
                              or "/pdf?" in (c.get("url") or "")), "")
                bdata, berr, burl = browser.fetch_pdf(
                    first or doi, prof,
                    timeout=max(60.0, float(inst.get("timeout") or 75)))
            except Exception as e:  # noqa: BLE001
                bdata, berr, burl = None, f"{type(e).__name__}: {e}", ""
            if bdata:
                return {"data": bdata, "source": "DOI 落地页（浏览器）",
                        "url": burl or ("https://doi.org/" + doi)}
            errors.append("浏览器取 DOI 页：" + (berr or "没拿到 PDF"))
        return None

    def try_elsevier() -> dict | None:
        """Publisher API first for Elsevier DOIs: no browser, no challenge, no click."""
        key = (inst.get("elsevier_key") or "").strip()
        if not key or not elsevier.is_elsevier(doi):
            return None
        say("通过 Elsevier API 取 PDF…")
        data, label, eerr = elsevier.fetch(doi, key, (inst.get("elsevier_token") or "").strip())
        if not data:
            errors.append("Elsevier API：" + (eerr or "无 PDF"))
            return None
        return {"data": data, "source": label, "url": "https://api.elsevier.com/content/article/doi/"
                + urllib.parse.quote(doi, safe="/()")}

    def try_wiley() -> dict | None:
        """Wiley TDM: same idea as Elsevier — the licensed door, not the front end."""
        token = (inst.get("wiley_token") or "").strip()
        if not token or not wiley.is_wiley(doi):
            return None
        say("通过 Wiley TDM 取 PDF…")
        data, label, werr = wiley.fetch(doi, token)
        if not data:
            errors.append("Wiley TDM：" + (werr or "无 PDF"))
            return None
        return {"data": data, "source": label, "url": wiley.API + urllib.parse.quote(doi, safe="/()")}

    def try_rg() -> dict | None:
        """ResearchGate 兜底：很多文章是作者自己传的全文。"""
        if not use_inst or not browser.find_browser():
            return None
        if not hasattr(browser, "rg_pdf"):
            return None
        say("在 ResearchGate 上找全文…")
        data, err, url = browser.rg_pdf(doi, inst["profile"],
                                        timeout=float(inst.get("timeout") or 75))
        if not data:
            errors.append("ResearchGate：" + (err or "没拿到"))
            return None
        return {"data": data, "source": "ResearchGate", "url": url}

    def try_preprint() -> dict | None:
        """The journal version could not be had: look for the same paper on ChemRxiv.

        Title and DOI usually differ, so this matches on the journal record's own link to the
        preprint, else on title + first author + year.  Only a confident match is used, and the
        source label says it is the preprint, because it may differ from the published text."""
        if routing.family(doi) == "preprint":
            return None
        title = rec["title"]
        if not title:
            errors.append("ChemRxiv：没有标题可查")
            return None
        say("正式版没拿到，去 ChemRxiv 找同一篇的预印本…")
        try:
            import doi_engine as _de
            found = preprints.find(title, doi=doi, first_author=meta.get("first_author") or "",
                                   year=meta.get("year"), http=_de.Http(email))
        except Exception as e:  # noqa: BLE001
            errors.append(f"ChemRxiv：查询出错（{type(e).__name__}），可重试")
            return None
        pick = preprints.best_auto(found)
        if not pick:
            if found.get("error"):
                errors.append("ChemRxiv：没连上，可重试（" + found["error"][:80] + "）")
            elif found.get("candidates"):
                errors.append("ChemRxiv：有相似记录但把握不够，没有自动采用（到「预印本」页面自己看）")
            else:
                errors.append("ChemRxiv：没有找到同一篇的预印本")
            return None
        data, purl, perr = fetch_preprint(pick, inst.get("profile") or "", float(inst.get("timeout") or 75))
        if not data:
            errors.append(f"ChemRxiv 预印本 {pick['doi']}：{perr or '没拿到 PDF'}")
            return None
        return {"data": data, "source": f"ChemRxiv 预印本 {pick['doi']}（{pick['why']}）", "url": purl}

    def try_arxiv() -> dict | None:
        """正式版拿不到时，回头找同一篇的 arXiv 预印本。"""
        title = (meta or {}).get("title") or ""
        if not title:
            return None
        say("正式版没拿到，去 arXiv 找同一篇的预印本…")
        try:
            import sources as _s
            got = _s.arxiv_by_title(title, doi)
        except Exception:  # noqa: BLE001
            got = []
        for c in got[:2]:
            data, err, resolved = fetch_pdf(c["url"])
            if data:
                return {"data": data, "source": c["source"], "url": resolved or c["url"]}
        errors.append("arXiv 预印本：没有匹配到同一篇")
        return None

    def try_epmc_repo() -> dict | None:
        """Europe PMC 仓储：非 OA 记录也可能有 PDF 或 JATS 全文。"""
        say("查 Europe PMC 仓储文件…")
        try:
            import sources as _s
            data, label = _s.europepmc_repo_file(doi)
        except Exception:  # noqa: BLE001
            data, label = None, ""
        if not data:
            errors.append("Europe PMC 仓储：" + (label or "没有"))
            return None
        return {"data": data, "source": label, "url": "https://europepmc.org/"}

    def try_epmc_xml() -> dict | None:
        """最后一道：拿 Europe PMC 的 JATS 全文（不是 PDF，但就是正文）。"""
        say("试试 Europe PMC 的全文 XML…")
        try:
            import sources as _s
            data = _s.europepmc_fulltext_xml(doi)
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            errors.append("Europe PMC 全文：没有")
            return None
        return {"data": data, "source": "Europe PMC 全文 XML",
                "url": f"https://europepmc.org/article/MED/{doi}"}

    def try_pmc() -> dict | None:
        say("尝试 PMC 全文…")
        data = sources.pmc_pow_pdf(doi, email)
        if not data:
            errors.append("PMC：无全文")
            return None
        return {"data": data, "source": "PMC PoW", "url": f"https://doi.org/{doi}"}

    # Route by publisher family and publication year, not by a single fixed
    # order: a ChemRxiv preprint, a 2015 ACS paper and a 2026 ACS paper need
    # three different plans.
    def try_api() -> dict | None:
        return try_elsevier() or try_wiley()

    step_map = {"api": try_api, "direct": try_oa, "oa": try_oa,
                "pmc": try_pmc, "scihub": try_scihub, "browser": try_inst}
    plan = routing.plan(
        doi, meta.get("year"),
        has_inst=use_inst,
        has_api=bool((inst.get("elsevier_key") or "").strip() or (inst.get("wiley_token") or "").strip()),
        scihub_on=bool(conf.get("enabled")))
    order, _seen = [], set()
    for s_ in plan:
        fn = step_map.get(s_)
        if fn is not None and fn not in _seen:  # "direct" and "oa" share try_oa
            _seen.add(fn)
            order.append((s_, fn))

    for name, attempt in order:
        if active_seconds() > DEADLINE_PER_DOI:
            errors.append(f"单篇超过 {DEADLINE_PER_DOI} 秒，已放弃剩余来源")
            break
        if stopped():
            rec.update({"status": "stopped", "note": "已停止"})
            return rec
        got = attempt()
        if not got and stopped():
            rec.update({"status": "stopped", "note": "已停止"})
            return rec
        if got:
            return finish(got)

    # Last resort, after every route to the journal version: the preprint.  It is cheap (a few
    # HTTP calls), so it runs even when the per-paper time budget is spent; only Stop ends it.
    if not stopped():
        got = try_preprint()
        if got:
            return finish(got)
    if stopped():
        rec.update({"status": "stopped", "note": "已停止"})
        return rec

    # A dead end the user can actually fix is worth naming precisely. Elsevier
    # sits behind a challenge no automated client passes, but its API does not.
    if elsevier.is_elsevier(doi) and not (inst.get("elsevier_key") or "").strip():
        errors.append("Elsevier 文章走 API 才能自动下载：在设置里填 elsevier_key（dev.elsevier.com 免费申请）")
    if wiley.is_wiley(doi) and not (inst.get("wiley_token") or "").strip():
        errors.append("Wiley 文章需要 TDM token：在设置里填 wiley_token（onlinelibrary.wiley.com 的 TDM 页面获取）")
    # 同一句失败原因常常出现两次（开放获取和直连各试一次），去重后读者才看得清
    deduped: list[str] = []
    for e in errors:
        if e and e not in deduped:
            deduped.append(e)
    joined = "；".join(deduped)
    sources_tail = ("；" + "；".join(diag[:6])) if diag else ""
    no_oa_record = oa_real == 0
    if any(k in joined for k in ("验证码", "DDoS", "订阅", "登录")):
        status = "blocked"
    elif "未收录" in joined or no_oa_record:
        status = "no_oa"
    else:
        status = "failed"
    rec.update({"status": status, "note": joined + sources_tail,
                "url": page_hint or (oa_cache[0]["url"] if oa_cache else "")})
    return rec


def download_many(dois: list[str], dest: str, email: str = "", template: str = DEFAULT_TEMPLATE,
                  progress=None, stop_event=None, force: bool = False,
                  scihub_conf: dict | None = None,
                  inst_conf: dict | None = None, step=None, workers: int = 1,
                  folders: dict | None = None) -> list[dict]:
    """Download a batch. workers>1 runs several DOIs at once, results stay in order."""
    os.makedirs(dest, exist_ok=True)
    folders = folders or {}
    indexes: dict[str, dict] = {}
    lock = threading.Lock()

    def index_for(d: str) -> dict:
        with lock:
            if d not in indexes:
                os.makedirs(d, exist_ok=True)
                indexes[d] = load_index(d)
            return indexes[d]

    results: list[dict | None] = [None] * len(dois)
    total = len(dois)
    done = 0
    workers = max(1, min(int(workers or 1), 6))

    def task(i: int, doi: str) -> None:
        nonlocal done
        sub = folders.get((doi or "").strip().lower(), "")
        d = os.path.join(dest, *sub.split("/")) if sub else dest
        index = index_for(d)
        if stop_event and stop_event.is_set():
            rec = {"doi": doi, "status": "stopped", "note": "已停止", "file": "", "url": "",
                   "source": "", "title": "", "year": ""}
        else:
            try:
                rec = download_one(doi, d, email, template, index, force,
                                   scihub_conf, inst_conf, step=step, stop=stop_event)
                if sub and rec.get("file"):
                    rec["file"] = sub + "/" + rec["file"]
            except Exception as e:  # noqa: BLE001 - one DOI must not kill the batch
                rec = {"doi": doi, "status": "failed", "note": f"{type(e).__name__}: {e}",
                       "file": "", "url": "", "source": "", "title": "", "year": ""}
        with lock:
            results[i] = rec
            done += 1
            save_index(d, index)
            if progress:
                progress(done, total, rec)

    if workers == 1:
        for i, doi in enumerate(dois):
            task(i, doi)
            time.sleep(0.2)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(task, i, doi) for i, doi in enumerate(dois)]
            for fut in futures:
                fut.result()
    return [r for r in results if r is not None]
