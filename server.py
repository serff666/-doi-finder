"""DOI Finder — local app server (stdlib only).

Tabs served by this process:
  • titles → DOI        (Crossref/OpenAlex matching)
  • DOI → PDF           (open-access download to a chosen folder)
  • topic search        (OpenAlex)
  • ChemASAP feed       (api.baranlab.org)

Runs from source (python server.py) or frozen as a one-file executable.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import chem
import browser
import downloader
import filing
import taskstore
import journalinfo
import journals
import netpolicy
import platform_util
import preprints
import scihub
import sources
from doi_engine import Cache, Http, lookup_all, parse_entries
from exporters import EXPORTERS, summary

FROZEN = bool(getattr(sys, "frozen", False))
VERSION = "2.1.1"
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(RES, "web")
SAMPLES = os.path.join(RES, "samples")


def _temp_safe() -> str:
    """tempfile.gettempdir() itself raises when no candidate is writable."""
    try:
        return tempfile.gettempdir()
    except Exception:  # noqa: BLE001
        return os.path.dirname(os.path.abspath(sys.executable)) or os.getcwd()


def _resolve_data_dir(preferred: str) -> str:
    """Pick a writable place for cache/settings/log; never raise.

    Honours DOIFINDER_DATA (portable mode), then the usual per-user folder,
    then a few fallbacks so a locked-down machine still runs.
    """
    candidates = [
        os.environ.get("DOIFINDER_DATA") or "",
        preferred,
        os.path.join(os.environ.get("APPDATA") or "", "DOIFinder"),
        os.path.join(RES, "data"),
        os.path.join(_temp_safe(), "DOIFinder"),
    ]
    for c in candidates:
        if not c:
            continue
        try:
            os.makedirs(os.path.join(c, ".cache"), exist_ok=True)
            probe = os.path.join(c, ".write-test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
            return c
        except Exception:  # noqa: BLE001
            continue
    return ""


_PREFERRED = (platform_util.data_dir() if FROZEN else RES)
DATA = _resolve_data_dir(_PREFERRED)
journalinfo.configure(DATA)
CACHE_PATH = os.path.join(DATA, ".cache", "lookup_cache.json") if DATA else ""
SETTINGS_PATH = os.path.join(DATA, "settings.json") if DATA else ""
LOG_PATH = os.path.join(DATA, "doifinder.log") if DATA else ""

try:
    sources.configure_meta_cache(os.path.join(DATA, ".cache", "meta_cache.json") if DATA else "")
except Exception:  # noqa: BLE001
    pass

MAX_BODY = 8 * 1024 * 1024
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 3600
SERVER: ThreadingHTTPServer | None = None
# Set by desktop.py when the UI runs in its own native window: "desktop"; otherwise "browser".
SHELL = "browser"
# Callbacks the desktop shell registers: "focus" (bring the window forward), "quit" (close the window).
APP_HOOKS: dict = {}


def running_jobs() -> list[dict]:
    """Jobs that are still working (paused ones included), for the close-window guard."""
    return [{"id": jid, "kind": j.get("kind") or "", "done": j.get("done") or 0, "total": j.get("total") or 0}
            for jid, j in list(JOBS.items()) if (j.get("state") or "") in ("running", "paused")]

DEFAULT_SETTINGS = {
    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads", "DOI Finder"),
    "email": "",
    "template": downloader.DEFAULT_TEMPLATE,
    "author": "",
    # paywall fallback (see scihub.py): open access is always tried first
    "scihub_enable": True,
    "scihub_mirrors": "\n".join(scihub.DEFAULT_MIRRORS),
    "scinet": scihub.DEFAULT_SCINET,
    "scihub_timeout": 30,
    "scihub_insecure_tls": False,   # True = skip certificate checks for Sci-Hub mirrors (off by default)
    "download_workers": 3,
    # institutional access: a real Chrome window the user logs into once
    "inst_enable": True,
    "inst_login_url": "",
    "inst_timeout": 75,
    "elsevier_key": "",
    "openalex_key": "",
    "core_key": "",
    "elsevier_token": "",
    "wiley_token": "",
    "journal_subs": [],
    "inst_first": False,
    "preprint_early": True,
    "browser": "auto",       # 'auto' | 'chrome' | 'edge' —— 驱动哪个浏览器
    "archive_file_template": "{year}_{first_author}_{title}",
    "archive_folder_template": "{year}",
    "my_orcid": "",
    "recent_orcids": "",
}


def _build_stamp() -> str:
    """The exe's own mtime — lets the user confirm which build is running."""
    try:
        target = sys.executable if FROZEN else os.path.abspath(__file__)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(target)))
    except Exception:  # noqa: BLE001
        return ""


def last_path(kind: str) -> str:
    return os.path.join(DATA, f"last_{kind}.json") if DATA else ""


def save_last(kind: str, payload: dict) -> None:
    """Keep the last finished job on disk so reopening the app is not a blank page."""
    path = last_path(kind)
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log(f"save_last({kind}) failed: {e}")


def load_last(kind: str) -> dict:
    path = last_path(kind)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}




def _pdf_year(path: str) -> str:
    """从 PDF 首页文字里找出版年份。找不到返回空串。"""
    text = ""
    if not text:
        try:
            import pypdf
            with open(path, "rb") as fh:
                reader = pypdf.PdfReader(fh)
                text = (reader.pages[0].extract_text() or "")[:4000]
        except Exception:  # noqa: BLE001
            return ""
    # 首页正文也会提到别人的旧论文年份（"... in 2002 ..."），所以先认引用行
    # "J. Org. Chem. 2025, 90, 2052–2061"，再认发表日期、版权行，最后才取最新的年份
    return filing.year_from_first_page(text)

def _pdf_title(src: str) -> str:
    """Best-effort title from the PDF's own /Title entry (no third-party lib)."""
    try:
        with open(src, "rb") as fh:
            head = fh.read(400000)
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(rb"/Title\s*\((.{4,300}?)\)", head, re.S)
    if not m:
        return ""
    raw = m.group(1)
    try:
        text = (raw.decode("utf-16-be", "ignore") if raw.startswith(b"\xfe\xff")
                else raw.decode("latin-1", "ignore"))
    except Exception:  # noqa: BLE001
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) > 3 else ""


def log(msg: str):
    if not LOG_PATH:
        return
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
            try:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            except Exception:  # noqa: BLE001
                pass
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


_OLD_NAME_TEMPLATE = "{first_author}_{year}_{title}"


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if not SETTINGS_PATH:
        return s
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            s.update(json.load(fh) or {})
    except Exception:  # noqa: BLE001
        pass
    browser.set_pref(s.get("browser"))
    netpolicy.set_openalex_key(s.get("openalex_key"))
    netpolicy.set_core_key(s.get("core_key"))
    netpolicy.set_email(s.get("email"))
    # an old built-in mirror list follows the built-in order, not a user choice
    saved = [ln.strip() for ln in str(s.get("scihub_mirrors") or "").splitlines() if ln.strip()]
    if set(saved) in _OLD_MIRROR_SETS:
        s["scihub_mirrors"] = "\n".join(scihub.DEFAULT_MIRRORS)
    # the built-in file name order changed (year first, author second); a saved copy of the old default follows it
    for k in ("template", "archive_file_template"):
        if s.get(k) == _OLD_NAME_TEMPLATE:
            s[k] = DEFAULT_SETTINGS[k]
    return s


_SETTINGS_LOCK = threading.RLock()


def _write_settings(s: dict) -> None:
    """Atomic write: a crash mid-save can no longer leave a truncated settings.json."""
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_PATH)


_INT_LIMITS = {"download_workers": (1, 5), "scihub_timeout": (5, 120), "inst_timeout": (5, 300)}
_SECRET_KEYS = ("elsevier_key", "elsevier_token", "wiley_token", "openalex_key", "core_key")
_MASK = "********"


def public_settings() -> dict:
    """Settings for the browser: secrets are masked, never sent back in clear."""
    s = load_settings()
    for k in _SECRET_KEYS:
        if s.get(k):
            s[k] = _MASK
    return s


def save_settings(patch: dict) -> dict:
    with _SETTINGS_LOCK:
        return _save_settings(patch)


def _save_settings(patch: dict) -> dict:
    s = load_settings()
    for k, default in DEFAULT_SETTINGS.items():
        if k not in patch:
            continue
        v = patch[k]
        if k in _SECRET_KEYS and v == _MASK:
            continue  # unchanged masked value from the UI
        if isinstance(default, bool):
            if isinstance(v, bool):
                s[k] = v
            elif isinstance(v, str):
                s[k] = v.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            try:
                lo, hi = _INT_LIMITS.get(k, (0, 3600))
                s[k] = max(lo, min(int(v), hi))
            except (TypeError, ValueError):
                pass
        elif isinstance(v, list):
            s[k] = [x for x in v if isinstance(x, (str, int, float, dict))][:300]
        elif isinstance(v, str):
            s[k] = v.strip()
    s["template"] = s["template"] or downloader.DEFAULT_TEMPLATE
    if not SETTINGS_PATH:
        return s
    try:
        os.makedirs(DATA, exist_ok=True)
        _write_settings(s)
    except Exception:  # noqa: BLE001
        pass
    return s


_OLD_MIRROR_SETS = (
    {"https://sci-hub.ee", "https://sci-hub.in", "https://sci-hub.ren",
     "https://sci-hub.usualwant.com", "https://sci-hub.vg", "https://sci-hub.ru", "https://sci-hub.su"},
    {"https://sci-hub.ru", "https://sci-hub.st", "https://sci-hub.se",
     "https://sci-hub.ee", "https://sci-hub.vg", "https://sci-hub.su"},
)


def scihub_conf(opts: dict) -> dict:
    """Resolve the Sci-Hub fallback options: request body first, then saved settings."""
    s = load_settings()
    enabled = opts.get("scihub")
    if not isinstance(enabled, bool):
        enabled = bool(s.get("scihub_enable"))
    raw = opts.get("mirrors")
    if not isinstance(raw, str):
        raw = s.get("scihub_mirrors") or ""
    mirrors = [ln.strip() for ln in raw.replace(",", "\n").splitlines() if ln.strip()]
    if set(mirrors) in _OLD_MIRROR_SETS:  # migrate the old built-in order
        mirrors = list(scihub.DEFAULT_MIRRORS)
    return {"enabled": enabled,
            "mirrors": mirrors or list(scihub.DEFAULT_MIRRORS),
            "scinet": s.get("scinet") or scihub.DEFAULT_SCINET,
            "timeout": float(s.get("scihub_timeout") or 30),
            "insecure": bool(s.get("scihub_insecure_tls"))}


def inst_conf(opts: dict) -> dict:
    """Options for the institutional browser channel (login state lives in a profile)."""
    s = load_settings()
    enabled = opts.get("inst")
    if not isinstance(enabled, bool):
        enabled = bool(s.get("inst_enable"))
    return {"enabled": enabled,
            "profile": os.path.join(DATA, "browser-profile" + browser.browser_tag()) if DATA else "",
            "login_url": (opts.get("login_url") if isinstance(opts.get("login_url"), str)
                          else s.get("inst_login_url")) or "",
            "first": bool(s.get("inst_first")),
            "preprint_early": (opts.get("preprint_early") if isinstance(opts.get("preprint_early"), bool)
                               else s.get("preprint_early") is not False),
            "timeout": float(s.get("inst_timeout") or 75),
            "elsevier_key": str(s.get("elsevier_key") or "").strip(),
            "elsevier_token": str(s.get("elsevier_token") or "").strip(),
            "wiley_token": str(s.get("wiley_token") or "").strip()}


def persist_mirror_stats():
    """Keep the mirror health counters so the next run starts with a better order."""
    if not SETTINGS_PATH:
        return
    with _SETTINGS_LOCK:
        s = load_settings()
        s["mirror_stats"] = scihub.stats_snapshot()
        _persist_try(s)


def _persist_try(s: dict) -> None:
    try:
        _write_settings(s)
    except Exception:  # noqa: BLE001
        pass


try:
    scihub.load_stats(load_settings().get("mirror_stats") or {})
except Exception:  # noqa: BLE001
    pass




def _cleanup_jobs():
    now = time.time()
    with JOBS_LOCK:
        for jid in [k for k, v in JOBS.items() if now - v["created"] > JOB_TTL]:
            JOBS.pop(jid, None)


# --------------------------------------------------------------------------- jobs
def run_lookup_job(job_id: str, entries: list[dict], opts: dict):
    job = JOBS[job_id]

    def progress(done: int, total: int):
        job["done"] = done
        job["total"] = total

    try:
        started = time.time()
        results = lookup_all(
            entries,
            author_hint=(opts.get("author") or None),
            rows=int(opts.get("rows") or 10),
            workers=int(opts.get("workers") or 5),
            mailto=opts.get("mailto") or "",
            cache=None if opts.get("no_cache") else Cache(CACHE_PATH or None),
            use_openalex=bool(opts.get("openalex", True)),
            progress=progress,
            stop_event=job["cancel"],
            dedup=bool(opts.get("dedup", True)),
            on_result=lambda r: job["results"].append(r),
        )
        job["results"] = results
        job["summary"] = summary(results)
        save_last("lookup", {"at": time.time(), "results": results,
                              "summary": job["summary"], "stopped": job["cancel"].is_set()})
        job["state"] = "stopped" if job["cancel"].is_set() else "done"
        log(f"lookup job {job_id}: {len(results)} titles in {time.time() - started:.0f}s "
            f"-> {job['summary']}")
    except Exception as e:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        log(f"lookup job {job_id} failed: {e}")


def preprint_one(entry: dict, http, email: str = "") -> dict:
    """Preprint candidates for one input line (a title, a citation, or a journal DOI)."""
    title = (entry.get("title") or "").strip()
    doi = preprints.norm_doi(entry.get("doi"))
    first_author, year = "", None
    as_doi = preprints.norm_doi(title)
    if re.match(r"^10\.\d{4,9}/\S+$", as_doi):          # the line was only a DOI: the title comes from its record
        doi, title = doi or as_doi, ""
    row = {"n": entry.get("n"), "input": entry.get("input") or title or doi, "doi": doi, "title": title,
           "first_author": "", "year": "", "status": "none", "candidates": [], "auto": "",
           "notes": [], "error": ""}
    if doi and not preprints.is_chemrxiv(doi):
        try:
            meta = sources.work_meta(doi, email) or {}
        except Exception as e:  # noqa: BLE001
            meta = {}
            row["notes"].append(f"期刊记录没取到：{type(e).__name__}")
        title = title or meta.get("title") or ""
        first_author = meta.get("first_author") or ""
        year = meta.get("year") or None
        row["title"] = title
    if not title and not doi:
        row.update({"status": "error", "error": "没有可查的标题或 DOI"})
        return row
    if not title:
        row.update({"status": "error", "error": "查不到这个 DOI 的标题，没法找预印本；可以直接粘贴标题再试"})
        return row
    row["first_author"], row["year"] = first_author, str(year or "")
    found = preprints.find(title, doi=doi, first_author=first_author, year=year, http=http)
    row["notes"] += found["notes"]
    for c in found["candidates"]:
        if c.get("pdf_url"):
            downloader.register_pdf_hint(c["doi"], c["pdf_url"])
    pick = preprints.best_auto(found)
    row["candidates"] = found["candidates"]
    row["auto"] = pick["doi"] if pick else ""
    if found["error"]:
        row.update({"status": "error", "error": found["error"]})
    else:
        row["status"] = "found" if found["candidates"] else "none"
    return row


def run_preprint_job(jid: str, entries: list[dict], opts: dict) -> None:
    job = JOBS[jid]
    email = opts.get("email") or load_settings()["email"]
    http = Http(email)
    out: list[dict | None] = [None] * len(entries)
    lock = threading.Lock()

    def one(i: int) -> None:
        e = entries[i]
        if job["cancel"].is_set():
            out[i] = {"n": e.get("n"), "input": e.get("input") or e.get("title"), "doi": e.get("doi") or "",
                      "title": e.get("title") or "", "status": "stopped", "candidates": [], "auto": "",
                      "notes": [], "error": "", "first_author": "", "year": ""}
            return
        try:
            out[i] = preprint_one(e, http, email)
        except Exception as ex:  # noqa: BLE001
            out[i] = {"n": e.get("n"), "input": e.get("input") or e.get("title"), "doi": e.get("doi") or "",
                      "title": e.get("title") or "", "status": "error", "candidates": [], "auto": "",
                      "notes": [], "error": f"{type(ex).__name__}: {ex}", "first_author": "", "year": ""}
        with lock:
            job["done"] += 1

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(one, range(len(entries))))
        results = [r for r in out if r]
        counts: dict[str, int] = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        job["results"] = results
        job["summary"] = " · ".join(f"{k} {v}" for k, v in counts.items())
        job["state"] = "stopped" if job["cancel"].is_set() else "done"
        log(f"preprint job {jid}: {len(results)} inputs -> {job['summary']}")
    except Exception as e:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        log(f"preprint job {jid} failed: {e}")


def _carsi_open(doi: str) -> dict:
    """后台打开机构登录页。用户登录一次，会话留在 carsi_profile 里，之后 browser 路线复用。"""
    try:
        import carsi
        return carsi.open_login(doi)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "note": f"{type(e).__name__}: {e}"}


class _PauseGate:
    """给下载器用的"可暂停"停止信号。

    下载器每处理一篇前都会问一次 is_set()。这里在暂停时挂住，直到继续或取消，
    于是不用改下载器一行代码就得到暂停能力：暂停时正在下的那篇会下完，后面的不动。
    """

    def __init__(self, job: dict):
        self.job = job

    def is_set(self) -> bool:
        while self.job.get("paused") and not self.job["cancel"].is_set():
            time.sleep(0.3)
        return self.job["cancel"].is_set()

    def set(self) -> None:
        self.job["cancel"].set()

    def paused_seconds(self) -> float:
        """Wall-clock seconds this job has spent paused, so a long pause is not counted as a timeout."""
        total = float(self.job.get("paused_total") or 0.0)
        if self.job.get("paused") and self.job.get("paused_at"):
            total += time.monotonic() - self.job["paused_at"]
        return total


def run_lab_job(jid: str, url: str, opts: dict) -> None:
    """抓任意课题组 publications 页。用真实浏览器，静态抓只能拿第一屏。"""
    job = JOBS[jid]
    try:
        import labpage
        items, err = labpage.crawl(url, timeout=int(opts.get("timeout") or 180))
        job["results"] = items
        job["done"] = len(items)
        job["total"] = max(1, len(items))
        if not items:
            job["error"] = err or "没有抓到论文条目"
            job["state"] = "error"
        else:
            job["markdown"] = f"抓到 {len(items)} 条" + (f"；{err}" if err else "")
            job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        job["error"] = f"{type(e).__name__}: {e}"
        job["state"] = "error"


_JOB_DIRS: set[str] = set()      # folders a download job of this session wrote to (so "open folder" works for them)


def _norm_dir(x: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(x))))


def _register_job_dir(dest: str) -> None:
    try:
        _JOB_DIRS.add(_norm_dir(dest))
    except (ValueError, OSError):
        pass


def run_lab_dl_job(jid: str, items: list, opts: dict) -> None:
    job = JOBS[jid]
    try:
        import labpage
        root, dest, structure = job_paths(opts)
        if not dest:
            dest = os.path.join(os.path.expanduser("~"), "Downloads", "DOI Finder", "lab")
        _register_job_dir(root or dest)
        _register_job_dir(dest)
        done = {"n": 0}

        def prog(i, total, rec):
            done["n"] = i
            job["done"] = i
            job["results"].append(rec)

        log(f"lab download: {len(items)} items -> {dest}")
        results = labpage.download_pdfs(items, dest, progress=prog,
                                        stop_event=job["cancel"], structure=structure)
        log(f"lab download done: {len(results)} records, {sum(1 for r in results if r.get('status')=='downloaded')} ok")
        job["markdown"] = f"下载完成：{sum(1 for r in results if r['status'] == 'downloaded')}/{len(results)} 篇，目录 {dest}"
        job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        job["error"] = f"{type(e).__name__}: {e}"
        job["state"] = "error"


def run_rg_job(jid: str, conf: dict, url: str) -> None:
    """ResearchGate 抓取任务。翻页慢，必须走后台，前端按 job 轮询。"""
    job = JOBS[jid]
    try:
        def progress(n: int, expected: int, page: int) -> None:
            job["done"] = n
            job["total"] = max(expected, n, 1)
            job["current"] = f"第 {page} 页，已抓到 {n} 条" + (f" / 页面标明 {expected} 条" if expected else "")

        items, err = browser.rg_publications(conf["profile"], url,
                                             timeout=float(conf.get("timeout") or 90), progress=progress)
        job["results"] = items
        job["total"] = max(1, len(items))
        job["done"] = len(items)
        if err and not items:
            job["error"] = err
            job["state"] = "error"
        else:
            job["markdown"] = f"抓到 {len(items)} 条" + (f"。{err}" if err else "，与页面标明的数量一致或页面未标明数量")
            job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        job["error"] = f"{type(e).__name__}: {e}"
        job["state"] = "error"


def job_paths(opts: dict) -> tuple[str, str, str]:
    """(workspace, collection folder, structure) for a download request.

    New requests carry workspace + collection + structure; an old one carries just `dir`
    (then there is no collection level and the files go straight into it)."""
    root = str(opts.get("workspace") or opts.get("dir") or load_settings()["download_dir"]).strip()
    coll = opts.get("collection")
    dest = filing.destination(root, coll) if isinstance(coll, str) and coll.strip() else root
    structure = opts.get("structure") if opts.get("structure") in filing.STRUCTURES else "flat"
    return root, dest, structure


def run_download_job(job_id: str, dois: list[str], opts: dict):
    job = JOBS[job_id]
    root, dest, structure = job_paths(opts)
    _register_job_dir(root)
    _register_job_dir(dest)
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = f"无法创建下载文件夹：{e}"
        return

    def progress(done: int, total: int, rec: dict):
        job["done"] = done
        job["total"] = total
        job["results"].append(rec)

    def step(text: str):
        job["current"] = text

    try:
        started = time.time()
        results = downloader.download_many(
            dois, dest,
            email=opts.get("email") or load_settings()["email"],
            template=opts.get("template") or load_settings()["template"],
            progress=progress,
            stop_event=_PauseGate(job),
            force=bool(opts.get("force")),
            scihub_conf=scihub_conf(opts),
            inst_conf=inst_conf(opts),
            step=step,
            workers=int(opts.get("workers") or load_settings().get("download_workers") or 3),
            folders=opts.get("_folders") or None,
            root=root, structure=structure,
        )
        job["results"] = results
        counts: dict[str, int] = {}
        srcs: dict[str, int] = {}
        fails: dict[str, int] = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if r["status"] in ("downloaded", "exists") and r.get("source"):
                srcs[r["source"]] = srcs.get(r["source"], 0) + 1
            elif r.get("note"):
                # first clause usually names the reason (Sci-Hub 未收录 / 机构通道… / HTTP 403)
                key = str(r["note"]).split("；")[0][:24]
                fails[key] = fails.get(key, 0) + 1
        job["summary"] = " · ".join(f"{k} {v}" for k, v in counts.items())
        job["dest"] = dest
        # "stopped" only if something was actually left undone; a stop pressed after the last
        # paper finished is still a completed job.
        was_stopped = any(r.get("status") == "stopped" for r in results)
        job["state"] = "stopped" if was_stopped else "done"
        save_last("download", {"at": time.time(), "dest": dest, "results": results,
                                "summary": job["summary"],
                                "stopped": was_stopped})
        top_src = " · ".join(f"{k} {v}" for k, v in sorted(srcs.items(), key=lambda x: -x[1])[:6])
        top_fail = " · ".join(f"{k} ×{v}" for k, v in sorted(fails.items(), key=lambda x: -x[1])[:4])
        log(f"download job {job_id}: {len(results)} DOI in {time.time() - started:.0f}s "
            f"-> {job['summary']} ({dest})")
        if top_src:
            log(f"  sources: {top_src}")
        if top_fail:
            log(f"  failures: {top_fail}")
        # 逐条记下没拿到的文献和原因，出了问题才查得动
        for r in results:
            if r.get("status") not in ("downloaded", "exists"):
                log(f"  miss {r.get('doi', '')} [{r.get('status')}] {str(r.get('note') or '')[:180]}")
    except Exception as e:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        log(f"download job {job_id} failed: {e}")
    finally:
        persist_mirror_stats()
        try:
            sources.flush_meta_cache()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "DOIFinder/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("DOIFINDER_VERBOSE"):
            log(fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _archive(self, body: dict) -> dict:
        """File manually downloaded PDFs (CNKI etc.) under a naming convention.

        No DOI is required: metadata comes from the form, then the filename, then
        the PDF's own /Title. The folder is built from a separate template so
        "2020/作者_2020_标题.pdf" style libraries can be reproduced.
        """
        paths = [p for p in (body.get("paths") or [])
                 if isinstance(p, str) and p.lower().endswith(".pdf") and os.path.isfile(p)]
        if not paths:
            try:
                paths = platform_util.pick_files("选择要归档的 PDF（可多选）", (("PDF", "*.pdf"),), multi=True)
            except Exception as e:  # noqa: BLE001
                return {"error": f"无法打开文件选择器：{e}"}
        if not paths:
            return {"error": "没有选择文件"}

        dest = os.path.abspath((body.get("dir") or load_settings()["download_dir"]).strip())
        file_tpl = (body.get("file_template") or load_settings().get("archive_file_template")
                    or "{year}_{first_author}_{title}")
        folder_tpl = (body.get("folder_template") or load_settings().get("archive_folder_template")
                      or "{year}")
        fields = {k: str(body.get(k) or "").strip()
                  for k in ("title", "first_author", "year", "journal")}

        def render(tpl: str, meta: dict, is_folder: bool = False) -> str:
            out = tpl or ""
            for key in ("first_author", "year", "title", "journal", "doi"):
                value = str(meta.get(key) or "").strip()
                piece = downloader.sanitize(value, 90) if value else ""
                out = out.replace("{" + key + "}", piece)
            if is_folder:  # "/" is a real path separator here
                segs = [re.sub(r"\s+", " ", s).strip(" ._-") for s in out.split("/")]
                segs = [s for s in segs if s]
                return os.path.join(*segs) if segs else "Unsorted"
            out = re.sub(r"[\\/]+", "_", out)
            out = re.sub(r"_{2,}", "_", out)
            out = re.sub(r"\s+", " ", out).strip(" ._-")
            return out or "未分类"

        items = []
        for src in paths:
            src = str(src)
            if not os.path.isfile(src):
                items.append({"error": f"文件不存在：{os.path.basename(src)}"})
                continue
            try:
                with open(src, "rb") as fh:
                    head = fh.read(1200)
                size = os.path.getsize(src)
            except Exception as e:  # noqa: BLE001
                items.append({"error": f"读取失败：{e}"})
                continue
            ext = os.path.splitext(src)[1].lower()
            if ext not in (".pdf", ".caj", ".kdh", ".nh", ".teb"):
                ext = ".pdf"
            if ext == ".pdf" and b"%PDF" not in head:
                items.append({"error": f"{os.path.basename(src)} 不是 PDF"})
                continue

            stem = os.path.splitext(os.path.basename(src))[0]
            meta = dict(fields)
            parts = [p for p in re.split(r"[_\-\s]+", stem) if p]
            if len(parts) >= 3 and filing.find_year(parts[1]) == parts[1]:
                meta["first_author"] = meta["first_author"] or parts[0]
                meta["year"] = meta["year"] or parts[1]
                meta["title"] = meta["title"] or "_".join(parts[2:])
            if not meta["title"]:
                meta["title"] = _pdf_title(src) or stem
            if not meta["year"]:
                meta["year"] = filing.find_year(stem)
            if not meta["year"] and ext == ".pdf":
                # 文件名里没有年份，就从 PDF 首页读，别直接扔进"未分类"
                meta["year"] = _pdf_year(src)
            meta["doi"] = ""

            folder = render(folder_tpl, meta, is_folder=True)
            target_dir = os.path.join(dest, folder)
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception as e:  # noqa: BLE001
                items.append({"error": f"无法创建文件夹：{e}"})
                continue
            base = render(file_tpl, meta)
            name = base + ext
            target = os.path.join(target_dir, name)
            n = 2
            while os.path.exists(target):
                name = f"{base} ({n}){ext}"
                target = os.path.join(target_dir, name)
                n += 1
            try:
                import shutil
                shutil.copy2(src, target)
            except Exception as e:  # noqa: BLE001
                items.append({"error": f"复制失败：{e}"})
                continue
            log(f"archived {os.path.basename(src)} -> {target}")
            items.append({"ok": True, "file": name, "folder": folder,
                          "path": target, "size": size,
                          "title": meta.get("title", ""), "year": meta.get("year", "")})
        return {"items": items, "dest": dest}

    def _attach(self, body: dict) -> dict:
        """File a manually downloaded PDF under the naming template + download folder.

        Nothing is downloaded here: the user picked a local file, we validate it is
        a PDF, build the correct name from the DOI's metadata, copy it in and record
        it in .doi-index.json so the row counts as downloaded.
        """
        doi = (body.get("doi") or "").strip().lower()
        if not re.match(r"^10\.\d{4,9}/\S+$", doi):
            return {"error": "DOI 格式不正确"}
        dest = (body.get("dir") or load_settings()["download_dir"]).strip()
        if not self._path_allowed(dest) and os.path.isabs(os.path.expanduser(dest)):
            # The folder box on the page *is* the download folder: if it was edited but
            # not saved yet, adopt it instead of rejecting a path the user just typed.
            save_settings({"download_dir": os.path.expanduser(dest)})
        if not self._path_allowed(dest):
            return {"error": f"下载目录无效：{dest}（请填写完整路径）"}
        template = body.get("template") or load_settings()["template"]
        src = (body.get("path") or "").strip()
        if not src:
            try:
                got = platform_util.pick_files(f"选择 {doi} 的 PDF / CAJ", (("PDF / CAJ", "*.pdf;*.caj"),))
                src = got[0] if got else ""
            except Exception as e:  # noqa: BLE001
                return {"error": f"无法打开文件选择器：{e}"}
        if not src:
            return {"error": "没有选择文件"}
        if os.path.splitext(src)[1].lower() not in (".pdf", ".caj"):
            return {"error": "只接受 PDF / CAJ 文件"}
        if not os.path.isfile(src):
            return {"error": "文件不存在"}
        try:
            with open(src, "rb") as fh:
                head = fh.read(1024)
            size = os.path.getsize(src)
        except Exception as e:  # noqa: BLE001
            return {"error": f"读取失败：{e}"}
        # CNKI downloads are CAJ/KDH, not PDF; keep the extension and accept both
        ext = os.path.splitext(src)[1].lower()
        if ext not in (".pdf", ".caj", ".kdh", ".nh", ".teb"):
            ext = ".pdf"
        if ext == ".pdf" and b"%PDF" not in head:
            return {"error": "这个文件不是 PDF（拖进来的可能是网页）"}
        if ext != ".pdf" and size < 2000:
            return {"error": "这个文件太小，可能下载没完成"}
        try:
            meta = sources.work_meta(doi, load_settings()["email"])
        except Exception:  # noqa: BLE001
            meta = {"title": "", "year": "", "first_author": "", "journal": ""}
        if not meta.get("title"):
            meta["title"] = doi.replace("/", "_")
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            return {"error": f"无法创建下载文件夹：{e}"}
        base = downloader.build_name(meta, doi, template)
        index = downloader.load_index(dest)
        prev = index.get(doi) or {}
        prev_name = prev.get("file") or ""
        prev_path = os.path.join(dest, prev_name) if prev_name else ""
        note = f"{size // 1024} KB · 手动归档"
        if prev_path and os.path.exists(prev_path) and prev_name.lower().endswith(ext):
            # same DOI archived before: never create "(2)" copies, replace in place
            try:
                if os.path.getsize(prev_path) == size:
                    record = {"status": "exists", "doi": doi, "file": prev_name,
                              "source": "手动归档", "url": f"https://doi.org/{doi}",
                              "title": prev.get("title", meta.get("title", "")),
                              "year": str(meta.get("year") or ""),
                              "note": "同一文件已归档，跳过", "dest": dest}
                    return record
                import shutil
                shutil.copy2(src, prev_path)
                name, target = prev_name, prev_path
                note = f"{size // 1024} KB · 已替换原文件"
            except Exception as e:  # noqa: BLE001
                return {"error": f"替换失败：{e}"}
        else:
            name = base + ext
            target = os.path.join(dest, name)
            n = 2
            while os.path.exists(target):
                name = f"{base} ({n}){ext}"
                target = os.path.join(dest, name)
                n += 1
            try:
                import shutil
                shutil.copy2(src, target)
            except Exception as e:  # noqa: BLE001
                return {"error": f"复制失败：{e}"}
        index[doi] = {"file": name, "title": meta.get("title", ""), "saved_at": int(time.time()),
                      "source": "手动归档", "url": f"https://doi.org/{doi}"}
        downloader.save_index(dest, index)
        log(f"attached {doi} -> {target}")
        record = {"status": "downloaded", "doi": doi, "file": name, "source": "手动归档",
                  "url": f"https://doi.org/{doi}", "title": meta.get("title", ""),
                  "year": str(meta.get("year") or ""), "note": note,
                  "dest": dest}
        # keep the restored list in sync so the update survives a restart too
        last = load_last("download")
        if last.get("results"):
            rows = last["results"]
            hits = [r for r in rows if (r.get("doi") or "").lower() == doi]
            if hits:
                for r in hits:
                    r.update(record)
            else:
                rows.append(record)
            save_last("download", last)
        return record

    # ---------------------------------------------------------------- guards
    def _host_ok(self) -> bool:
        """Reject foreign Host headers — kills DNS rebinding."""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _origin_ok(self) -> bool:
        """Only our own UI may drive the API — kills CSRF from any web page."""
        port = self.server.server_address[1]
        good = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            return origin in good
        ref = (self.headers.get("Referer") or "").strip()
        if ref:
            return any(ref.startswith(g + "/") for g in good)
        return True  # no Origin/Referer: a local tool, curl, or the app itself

    def _path_allowed(self, path: str) -> bool:
        """Only paths inside the download folder may be opened or written to."""
        def norm(x: str) -> str:
            # realpath + normcase: "C:/a/b" == "c:\\A\\b\\" on Windows, symlinks resolved
            return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(x))))
        try:
            base = norm(load_settings()["download_dir"])
            p = norm(path)
            return os.path.commonpath([p, base]) == base
        except (ValueError, OSError):
            return False

    def _openable(self, path: str) -> bool:
        """The saved download folder, or a folder a download job of this session used (per-page folders)."""
        if self._path_allowed(path):
            return True
        try:
            p = _norm_dir(path)
            return any(os.path.commonpath([p, b]) == b for b in _JOB_DIRS)
        except (ValueError, OSError):
            return False

    def _json(self, obj, code: int = 200):

        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(n).decode("utf-8-sig", "replace"))

    def _static(self, path: str, root: str | None = None):
        root = root or WEB
        rel = unquote(path).lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(root, rel))
        rootn = os.path.normpath(root)
        try:
            inside = os.path.commonpath([full, rootn]) == rootn
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(full):
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        p = urlparse(self.path).path
        qs = urlparse(self.path).query
        if p in ("/", "/index.html"):
            return self._static("index.html")
        if p == "/api/health":
            return self._json({"ok": True, "app": "doi-finder", "version": VERSION,
                               "build": _build_stamp(),
                               "jobs": len(JOBS), "frozen": FROZEN, "platform": sys.platform,
                               "shell": SHELL,
                               "settings": public_settings()})
        if p == "/api/jobs":
            now = time.time()
            out = []
            for jid, j in list(JOBS.items()):
                out.append({"id": jid, "kind": j.get("kind") or "",
                            "state": j.get("state") or "",
                            "done": j.get("done") or 0,
                            "total": j.get("total") or 0,
                            "items": len(j.get("items") or []),
                            "errors": len(j.get("errors") or []),
                            "markdown": (j.get("markdown") or "")[:120],
                            "error": (j.get("error") or "")[:200],
                            "age": round(now - (j.get("created") or now), 1)})
            out.sort(key=lambda x: x["age"])
            return self._json({"jobs": out})

        if p.startswith("/samples/"):
            return self._static(p[len("/samples/"):], SAMPLES)
        if p == "/api/history":
            kind = (parse_qs(urlparse(self.path).query).get("kind") or [""])[0]
            try:
                return self._json({"items": taskstore.listing(DATA, kind) if DATA else []})
            except taskstore.StoreError as e:
                return self._json({"error": str(e)}, 400)
        if p == "/api/history/get":
            q = parse_qs(urlparse(self.path).query)
            try:
                rec = taskstore.load(DATA, (q.get("kind") or [""])[0], (q.get("id") or [""])[0]) if DATA else None
            except taskstore.StoreError as e:
                return self._json({"error": str(e)}, 400)
            return self._json(rec) if rec else self._json({"error": "not found"}, 404)
        if p.startswith("/api/last/"):
            kind = p.rsplit("/", 1)[-1]
            if kind not in ("lookup", "download"):
                return self._json({"error": "unknown kind"}, 404)
            return self._json(load_last(kind))
        if p.startswith("/api/job/"):
            job = JOBS.get(p.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "unknown job"}, 404)
            with JOBS_LOCK:
                payload = {"state": job["state"], "kind": job.get("kind", "lookup"),
                           "done": job.get("done", 0), "total": job.get("total", 0),
                           "summary": job.get("summary", ""), "error": job.get("error", ""),
                           "results": job.get("results", []), "dest": job.get("dest", ""),
                           "current": job.get("current", ""),
                           "markdown": job.get("markdown", ""),
                           "items": job.get("items") or [],
                           "errors": job.get("errors") or [],
                           "paused": bool(job.get("paused")),
                           "cancelled": job["cancel"].is_set()}
            return self._json(payload)
        if p == "/api/chem/feed":
            q = parse_qs(qs)
            ids = [x for x in (q.get("journals", [""])[0] or "").split(",") if x]
            try:
                data = chem.feed(query=(q.get("q", [""])[0] or ""),
                                 journal_ids=ids or None,
                                 limit=int(q.get("limit", ["200"])[0] or 200),
                                 only_oa=(q.get("oa", ["0"])[0] == "1"))
                return self._json(data)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"ChemASAP 源不可用：{e}"}, 502)
        if p == "/api/chem/journals":
            try:
                return self._json({"journals": chem.journals()})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 502)
        if p == "/api/journalinfo/status":
            return self._json(journalinfo.status())
        if p == "/api/journalinfo":
            q = urllib.parse.parse_qs(qs)
            try:
                info = journalinfo.lookup((q.get("issn") or [""])[0], (q.get("name") or [""])[0])
            except Exception:  # noqa: BLE001
                info = None
            return self._json({"info": info, "status": journalinfo.status()})
        if p == "/api/journal/presets":
            return self._json({"presets": journals.PRESETS})
        if p.startswith("/api/journal/search"):
            q = (parse_qs(qs).get("q", [""])[0] or "").strip()
            if not q:
                return self._json({"error": "缺少 q"}, 400)
            try:
                return self._json({"results": journals.search(q)})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"Crossref 不可用：{e}"}, 502)
        if p.startswith("/api/journal/latest"):
            q = parse_qs(qs)
            issn = (q.get("issn", [""])[0] or "").strip()
            if not issn:
                return self._json({"error": "缺少 issn"}, 400)
            try:
                items = journals.latest(
                    issn,
                    rows=int(q.get("rows", ["40"])[0] or 40),
                    since=(q.get("since", [""])[0] or "").strip(),
                    kind=(q.get("kind", [""])[0] or "").strip())
                try:
                    import classify
                    items = classify.enrich(items, journal=(items[0].get("journal") if items else "")
                                            or "", issn=issn)
                except Exception:  # noqa: BLE001
                    pass
                # OpenAlex 给的是学术分类体系（topics + keywords），比标题猜词可靠得多。
                # 一次请求批量取一页，不逐个 DOI 打。
                try:
                    import topics as _topics
                    got = _topics.topics_for([it.get("doi") or "" for it in items],
                                             mailto=load_settings().get("email") or "")
                    for it in items:
                        t = got.get((it.get("doi") or "").lower())
                        if not t:
                            continue
                        # 期刊口径明确时以期刊为准，否则用 OpenAlex 的 subfield
                        if not it.get("field") or it.get("field") == "综合":
                            if t.get("field"):
                                it["field"] = t["field"]
                                it["field_basis"] = "OpenAlex 主题"
                        if t.get("tags"):
                            it["tags"] = (t.get("tags") or [])[:4]
                except Exception:  # noqa: BLE001
                    pass
                return self._json({"issn": issn, "items": items})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"Crossref 不可用：{e}"}, 502)
        if p in ("/favicon.ico", "/favicon.png"):
            return self._static(p.lstrip("/"))
        return self._static(p)

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._json({"error": "forbidden"}, 403)
        # 只收 application/json：跨站“简单请求”发不出这个类型，必须先过预检
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json({"error": "unsupported media type"}, 415)
        try:
            self._do_post()
        except Exception as e:  # noqa: BLE001 - never drop the connection silently
            log(f"POST {self.path} failed: {type(e).__name__}: {e}")
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _do_post(self):
        p = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as e:
            return self._json({"error": str(e)}, 413)

        if p == "/api/history/save":
            if not DATA:
                return self._json({"error": "no data folder"}, 500)
            try:
                meta = taskstore.save(DATA, str(body.get("kind") or ""), body.get("id") or None,
                                      body.get("title") or "", body.get("summary") or "", body.get("payload"))
            except taskstore.StoreError as e:
                return self._json({"error": str(e)}, 400)
            return self._json(meta)
        if p == "/api/history/delete":
            try:
                ok = taskstore.delete(DATA, str(body.get("kind") or ""), str(body.get("id") or "")) if DATA else False
            except taskstore.StoreError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": ok})

        if p == "/api/parse":
            entries = parse_entries(body.get("text", ""), body.get("mode", "auto"))
            return self._json({"count": len(entries), "entries": entries[:400]})

        if p == "/api/lookup":
            entries = parse_entries(body.get("text", ""), body.get("mode", "auto"))
            if not entries:
                return self._json({"error": "no titles found in input"}, 400)
            if len(entries) > 2000:
                return self._json({"error": "单次最多处理 2000 条，请分批"}, 400)
            _cleanup_jobs()
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"created": time.time(), "state": "running", "kind": "lookup",
                         "done": 0, "total": len(entries), "results": [],
                         "cancel": threading.Event()}
            threading.Thread(target=run_lookup_job, args=(jid, entries, body), daemon=True).start()
            return self._json({"job_id": jid, "total": len(entries)})

        if p == "/api/preprint/find":
            entries = parse_entries(body.get("text", ""), "auto")
            if not entries:
                return self._json({"error": "没有找到标题或 DOI"}, 400)
            if len(entries) > 500:
                return self._json({"error": "单次最多 500 条，请分批"}, 400)
            _cleanup_jobs()
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"created": time.time(), "state": "running", "kind": "preprint",
                         "done": 0, "total": len(entries), "results": [],
                         "cancel": threading.Event()}
            threading.Thread(target=run_preprint_job, args=(jid, entries, body), daemon=True).start()
            return self._json({"job_id": jid, "total": len(entries)})

        if p.startswith("/api/pause/"):
            job = JOBS.get(p.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "unknown job"}, 404)
            if not job.get("paused"):
                job["paused_at"] = time.monotonic()
            job["paused"] = True
            return self._json({"ok": True, "paused": True})

        if p.startswith("/api/resume/"):
            job = JOBS.get(p.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "unknown job"}, 404)
            if job.get("paused") and job.get("paused_at"):
                job["paused_total"] = float(job.get("paused_total") or 0.0) + time.monotonic() - job["paused_at"]
            job["paused"] = False
            job["paused_at"] = 0.0
            return self._json({"ok": True, "paused": False})

        if p.startswith("/api/stop/"):
            job = JOBS.get(p.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "unknown job"}, 404)
            job["cancel"].set()
            return self._json({"ok": True})

        if p == "/api/carsi":
            doi = (body.get("doi") or "").strip()
            if not doi:
                return self._json({"error": "缺少 DOI"}, 400)
            try:
                import carsi
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"机构登录模块不可用：{e}"}, 500)
            # 必须和下载器的浏览器通道用同一个配置目录，否则登录态复用不上
            carsi.PROFILE_DIR = os.path.join(DATA, "browser-profile" + browser.browser_tag())
            threading.Thread(target=_carsi_open, args=(doi,), daemon=True).start()
            return self._json({"ok": True, "url": carsi.article_url(doi),
                               "profile": carsi.profile_ready()})

        if p == "/api/download":
            raw = body.get("dois")
            if isinstance(raw, str):
                dois = [d.strip() for d in raw.replace(",", "\n").splitlines() if d.strip()]
            else:
                dois = [str(d).strip() for d in (raw or []) if str(d).strip()]
            dois = list(dict.fromkeys(d for d in dois if d))
            if not dois:
                return self._json({"error": "没有可下载的 DOI"}, 400)
            layout = body.get("layout") if isinstance(body.get("layout"), dict) else None
            if len(dois) > (20000 if layout else 2000):
                return self._json({"error": "单次最多 %d 个 DOI，请分批" % (20000 if layout else 2000)}, 400)
            if layout:
                # journal harvest: file each DOI under <Journal>/<Year>/Volume V, Issue I
                jname = str(layout.get("journal") or "")
                folders = {}
                named = isinstance(body.get("collection"), str) and body["collection"].strip()
                for it in (layout.get("items") or []):
                    if isinstance(it, dict) and it.get("doi"):
                        # with a collection (the journal's own folder) only Year/Volume, Issue is left
                        folders[str(it["doi"]).strip().lower()] = (
                            filing.sub_for("volume_issue", it) if named else
                            downloader.journal_folder(jname, it.get("year"), it.get("volume"), it.get("issue")))
                body["_folders"] = folders
            _cleanup_jobs()
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"created": time.time(), "state": "running", "kind": "download",
                         "done": 0, "total": len(dois), "results": [],
                         "cancel": threading.Event()}
            threading.Thread(target=run_download_job, args=(jid, dois, body), daemon=True).start()
            return self._json({"job_id": jid, "total": len(dois)})

        if p == "/api/search":
            q = (body.get("query") or "").strip()
            orcid = (body.get("orcid") or "").strip()
            if not q and not body.get("author") and not orcid:
                return self._json({"error": "请输入主题关键词、作者或 ORCID"}, 400)
            try:
                diag: dict = {}
                if orcid:
                    # 先走 ORCID 官方接口：条数与作者主页一致，且不受 OpenAlex 限流影响
                    try:
                        works = sources.orcid_public_works(orcid)
                    except Exception:  # noqa: BLE001 - 官方接口不通再退回 OpenAlex
                        works = []
                    if not works:
                        works = sources.orcid_works(
                            orcid, limit=min(int(body.get("limit") or 200), 2000),
                            mailto=(body.get("email") or load_settings()["email"]),
                            year_from=str(body.get("year_from") or ""),
                            year_to=str(body.get("year_to") or ""),
                            oa_only=bool(body.get("oa_only")),
                        )
                    else:
                        if body.get("year_from") or body.get("year_to"):
                            y0 = str(body.get("year_from") or "0")
                            y1 = str(body.get("year_to") or "9999")
                            works = [w for w in works if y0 <= (w.get("year") or "0") <= y1]
                    diag["source"] = "ORCID"
                else:
                    works = sources.search_works(
                        q, email=(body.get("email") or load_settings()["email"]),
                        year_from=str(body.get("year_from") or ""),
                        year_to=str(body.get("year_to") or ""),
                        oa_only=bool(body.get("oa_only")),
                        author=(body.get("author") or ""),
                        limit=int(body.get("limit") or 40),
                        sort=body.get("sort") or "relevance_score:desc",
                        diag=diag,
                    )
                return self._json({"count": len(works), "works": works,
                                   "source": diag.get("source", "OpenAlex"),
                                   "note": diag.get("note", "")})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"检索失败：{e}"}, 502)

        if p == "/api/meta":
            doi = (body.get("doi") or "").strip()
            if not re.match(r"^10\.\d{4,9}/\S+$", doi):
                return self._json({"error": "DOI 格式不正确"}, 400)
            try:
                meta = sources.work_meta(doi, load_settings()["email"])
                return self._json({"doi": doi, **(meta or {})})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"取元数据失败：{e}"}, 502)

        if p == "/api/baidu/lookup":
            text = (body.get("text") or "").strip()
            paper_id = sources.baidu_paper_id(text)
            if not paper_id:
                return self._json({"error": "没有识别到 paperid（请粘贴百度学术的论文链接）"}, 400)
            try:
                return self._json({"found": True, **sources.baidu_paper(paper_id)})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 502)

        if p == "/api/orcid/search":
            name = (body.get("name") or "").strip()
            if len(name) < 3:
                return self._json({"error": "请输入至少 3 个字符的作者名"}, 400)
            try:
                return self._json({"candidates": sources.orcid_search(name, rows=12)})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"ORCID 查询失败：{e}"}, 502)

        if p == "/api/inst/login":
            conf = inst_conf(body)
            url = (body.get("url") or conf["login_url"] or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._json({"error": "请先填写学校图书馆 / 登录页地址"}, 400)
            ok, err = browser.open_login(conf["profile"], url)
            return self._json({"ok": ok, "error": err, "profile": conf["profile"]})

        if p == "/api/inst/close":
            conf = inst_conf(body)
            browser.close_browser(conf["profile"])
            return self._json({"ok": True})

        if p == "/api/inst/status":
            conf = inst_conf(body)
            exe = browser.find_browser() or ""
            return self._json({
                "chrome": exe,
                "browser_name": os.path.basename(exe),
                "browser_pref": browser.browser_pref() or "auto",
                "profile": conf["profile"],
                "logged_in": browser.profile_ready(conf["profile"]),
                "enabled": conf["enabled"],
                "login_url": conf["login_url"],
            })

        if p == "/api/settings":
            if body.get("save"):
                save_settings(body.get("settings") or {})
                return self._json({"settings": public_settings()})
            return self._json({"settings": load_settings()})

        if p == "/api/journalinfo/update":
            try:
                return self._json({"status": journalinfo.update()})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 502)

        if p == "/api/journal/harvest":
            issn = str(body.get("issn") or "").strip()
            if not re.fullmatch(r"\d{4}-?\d{3}[\dXx]", issn):
                return self._json({"error": "需要 ISSN（形如 1759-0876），期刊名请先解析成 ISSN"}, 400)
            _cleanup_jobs()
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"created": time.time(), "state": "running", "kind": "harvest",
                         "done": 0, "total": 0, "markdown": "", "items": [],
                         "cancel": threading.Event()}

            def _run() -> None:
                job = JOBS[jid]
                try:
                    import journals as jm

                    def on_page(done: int, total: int, year: int, partial=None) -> None:
                        job["done"] = done
                        job["total"] = max(total, done)
                        job["markdown"] = f"抓到 {done} 条（{year} 年）"
                        if partial:
                            # 边抓边给，界面不用等到最后才看到东西
                            job["items"] = list(partial)

                    errs: list = []
                    got = jm.harvest(issn,
                                     int(body.get("year_from") or 0),
                                     int(body.get("year_to") or 0),
                                     on_page=on_page,
                                     stop=job["cancel"],
                                     errors=errs,
                                     kind=(lambda k: "" if k in ("", "all", "全部类型",
                                                                  "research", "review",
                                                                  "noncorr", "non-research")
                                           else k)(str(body.get("kind") or "journal-article")))
                    job["items"] = got
                    job["errors"] = errs
                    job["done"] = len(got)
                    job["state"] = "done" if got else "error"
                    if errs:
                        job["markdown"] = f"共 {len(got)} 条，{len(errs)} 次请求失败"
                    job["markdown"] = f"共 {len(got)} 条"
                    if not got:
                        job["error"] = ("Crossref 没有返回条目" if not errs
                                        else "Crossref 请求失败：" + errs[0])
                except Exception as e:  # noqa: BLE001
                    job["error"] = f"{type(e).__name__}: {e}"
                    job["state"] = "error"

            threading.Thread(target=_run, daemon=True).start()
            return self._json({"job_id": jid})

        if p == "/api/journal/subs":
            # The subscription list is small plain data; keeping it in settings
            # means it survives restarts without another file to explain.
            subs = body.get("subs")
            if not isinstance(subs, list):
                return self._json({"error": "subs 必须是数组"}, 400)
            clean = []
            for it in subs[:200]:
                if not isinstance(it, dict):
                    continue
                issn = str(it.get("issn") or "").strip()
                if not issn:
                    continue
                clean.append({"issn": issn,
                              "title": str(it.get("title") or issn)[:200],
                              "publisher": str(it.get("publisher") or "")[:120],
                              "seen": str(it.get("seen") or "")[:32]})
            save_settings({"journal_subs": clean})
            return self._json({"settings": public_settings()})

        if p == "/api/journal/resolve":
            names = body.get("names")
            if not isinstance(names, list):
                return self._json({"error": "names 必须是数组"}, 400)
            out = []
            for name in names[:40]:
                try:
                    hit = journals.resolve(str(name))
                except Exception:  # noqa: BLE001
                    hit = None
                if hit:
                    out.append(hit)
            return self._json({"resolved": out})

        if p == "/api/scihub/test":
            conf = scihub_conf(body)
            from concurrent.futures import ThreadPoolExecutor

            def probe(mirror: str) -> dict:
                t0 = time.time()
                try:
                    status, _b, _u, _ct = scihub.Client(mirrors=[mirror], timeout=8, insecure=bool(load_settings().get("scihub_insecure_tls"))).session.get(mirror)
                    return {"mirror": mirror, "status": status, "ok": status < 500,
                            "ms": int((time.time() - t0) * 1000)}
                except Exception as e:  # noqa: BLE001
                    return {"mirror": mirror, "status": 0, "ok": False,
                            "error": type(e).__name__, "ms": int((time.time() - t0) * 1000)}

            with ThreadPoolExecutor(max_workers=6) as ex:
                rows = list(ex.map(probe, conf["mirrors"]))
            return self._json({"mirrors": rows, "scinet": conf["scinet"],
                               "enabled": conf["enabled"]})

        if p == "/api/rg/open":
            conf = inst_conf(body)
            url = (body.get("url") or "").strip()
            if not url.lower().startswith("https://www.researchgate.net/"):
                return self._json({"error": "请填写 researchgate.net 的作者主页链接"}, 400)
            ok, err = browser.open_login(conf["profile"], url)
            return self._json({"ok": ok, "error": err})

        if p == "/api/lab/list":
            url = (body.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._json({"error": "请填写以 http(s):// 开头的页面地址"}, 400)
            _cleanup_jobs()
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"created": time.time(), "state": "running", "kind": "lab",
                         "done": 0, "total": 1, "results": [], "markdown": "正在打开页面…",
                         "cancel": threading.Event()}
            threading.Thread(target=run_lab_job, args=(jid, url, body), daemon=True).start()
            return self._json({"job_id": jid})

        if p == "/api/lab/download":
            items = body.get("items") or []
            if not items:
                return self._json({"error": "没有可下载的条目"}, 400)
            _cleanup_jobs()
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"created": time.time(), "state": "running", "kind": "labdl",
                         "done": 0, "total": len(items), "results": [], "markdown": "",
                         "cancel": threading.Event()}
            threading.Thread(target=run_lab_dl_job, args=(jid, items, body), daemon=True).start()
            return self._json({"job_id": jid})

        if p == "/api/rg/list":
            conf = inst_conf(body)
            url = (body.get("url") or "").strip()
            if not url.lower().startswith("https://www.researchgate.net/"):
                return self._json({"error": "请填写 researchgate.net 的作者主页链接"}, 400)
            # 抓几百篇要翻几十页，同步返回会让浏览器等到断开。改成后台任务，前端轮询。
            _cleanup_jobs()
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"created": time.time(), "state": "running", "kind": "rg",
                         "done": 0, "total": 1, "results": [], "markdown": "正在打开作者页…",
                         "cancel": threading.Event()}
            threading.Thread(target=run_rg_job, args=(jid, conf, url), daemon=True).start()
            return self._json({"job_id": jid})

        if p == "/api/archive":
            return self._json(self._archive(body))

        if p == "/api/attach":
            return self._json(self._attach(body))

        if p == "/api/pick-folder":
            try:
                path = platform_util.pick_folder("选择下载文件夹",
                                                 body.get("initial") or load_settings()["download_dir"])
                return self._json({"path": path or ""})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"无法打开文件夹选择器：{e}"}, 501)

        if p == "/api/open-url":
            url = (body.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._json({"error": "无效链接"}, 400)
            try:
                webbrowser.open(url)
                return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)

        if p == "/api/open-folder":
            path = body.get("path") or load_settings()["download_dir"]
            if not self._openable(path):
                return self._json({"error": "只能打开下载目录内的路径"}, 403)
            try:
                os.makedirs(path, exist_ok=True)
                platform_util.open_path(path)
                return self._json({"ok": True, "path": path})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)

        if p.startswith("/api/export/"):
            fmt = p.rsplit("/", 1)[-1]
            if fmt not in EXPORTERS:
                return self._json({"error": f"unknown format {fmt}"}, 400)
            rows = body.get("results") or []
            fn, ctype, ext = EXPORTERS[fmt]
            data = fn(rows)
            if isinstance(data, str):
                data = data.encode("utf-8")
            name = (body.get("basename") or "dois").strip() or "dois"
            safe = "".join(c for c in name if c not in '\\/:*?"<>|')[:60]
            fname = f"{safe}.{ext}"
            # HTTP headers are latin-1: a Chinese basename must go through RFC 5987
            ascii_name = fname.encode("ascii", "ignore").decode() or f"export.{ext}"
            quoted = urllib.parse.quote(fname)
            return self._send(200, data, ctype, {
                "Content-Disposition":
                    f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"})

        if p == "/api/quit":
            self._json({"ok": True})
            threading.Timer(0.4, APP_HOOKS.get("quit") or shutdown).start()
            return

        if p == "/api/app/focus":                  # a second launch asks the running window to come forward
            hook = APP_HOOKS.get("focus")
            if not hook:
                return self._json({"ok": False})
            try:
                hook()
                return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)})

        return self._json({"error": "not found"}, 404)


def open_window(url: str) -> bool:
    """Open the UI as its own app window (Chrome/Edge --app), not as a browser tab."""
    exe = browser.find_browser()
    if not exe:
        return False
    profile = os.path.join(DATA or _temp_safe(), "ui-profile")
    try:
        os.makedirs(profile, exist_ok=True)
        subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}",
                          "--window-size=1440,960", "--no-first-run",
                          "--no-default-browser-check", "--disable-features=Translate"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False


def show_ui(url: str) -> None:
    """App window when a Chromium browser exists, otherwise the default browser."""
    if not open_window(url):
        webbrowser.open(url)


def pick_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


def already_running() -> str | None:
    for port in range(8765, 8776):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.25) as r:
                data = json.loads(r.read().decode())
                if data.get("app") == "doi-finder":
                    return f"http://127.0.0.1:{port}/"
        except Exception:  # noqa: BLE001
            continue
    return None


def shutdown():
    global SERVER
    if SERVER:
        SERVER.shutdown()


def start(port: int = 0) -> str:
    """Bind the server (not yet serving) and return its URL. desktop.py serves it in a thread."""
    global SERVER
    port = port or pick_port()
    SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    SERVER.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    try:  # a windowed build has no stdout
        print(f"DOI-FINDER-URL {url}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    log(f"started {url} (frozen={FROZEN}, shell={SHELL})")
    return url


def serve() -> None:
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVER.server_close()
        log("stopped")


def main():
    env_port = int(os.environ.get("DOIFINDER_PORT") or 0)
    open_browser = (FROZEN or "--open" in sys.argv or env_port) and not os.environ.get("DOIFINDER_NO_BROWSER")

    if FROZEN and not env_port:
        existing = already_running()
        if existing:
            log(f"already running at {existing}")
            show_ui(existing)
            return

    url = start(env_port)
    if open_browser:
        threading.Timer(0.7, lambda: show_ui(url)).start()
    serve()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a windowed build has nowhere to print a traceback
        import traceback
        try:
            target = os.path.join(DATA or _temp_safe(), "crash.log")
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
                fh.write(traceback.format_exc() + "\n")
        except Exception:  # noqa: BLE001
            pass
        raise
