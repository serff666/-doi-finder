"""Sci-Hub fallback resolver — standard-library implementation (derived from the author's own AuthorSciHub tool).

Only used when no open-access copy exists (see downloader.download_one): the
app still tries arXiv/OpenAlex/Unpaywall/Europe PMC/PMC first.

Ported rules:
  • several mirrors, ordered by observed failure rate
  • PDF direct-link extraction: iframe#pdf, meta[citation_pdf_url],
    object[type=application/pdf], embed/iframe, .download a, regex fallbacks
  • page classification: ok / not-indexed / DDoS-Guard / captcha / error
  • automatic ALTCHA proof-of-work solving (no browser, no ScraperAPI)
  • Sci-Net fallback for articles published after Sci-Hub stopped indexing
  • %PDF magic-number check before a download is accepted

No third-party dependency (the original used requests + BeautifulSoup).
"""
from __future__ import annotations

import base64
import hashlib
import html as _html
import http.cookiejar
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_MIRRORS = [
    # user-preferred order (2026-11)
    "https://sci-hub.usualwant.com",
    "https://sci-hub.ee",
    "https://sci-hub.in",
    "https://sci-hub.ren",
    "https://sci-hub.vg",
    "https://sci-hub.ru",
    "https://sci-hub.su",
]
# impersonation/phishing hosts the mirror tracker warns about
FAKE_DOMAINS = ("sci-hub.tf", "sci-hub.shop", "scihub.wikicn.top", "sci-hub.scihubtw.tw")

DISCOVERY_URL = "http://tool.yovisun.com/scihub/"
DISCOVERY_TTL = 6 * 3600
_DISCOVERY = {"at": 0.0, "mirrors": []}
_DISCOVERY_LOCK = threading.Lock()

DEFAULT_SCINET = "https://sci-net.xyz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/142.0.0.0 Safari/537.36")
MAX_BYTES = 150 * 1024 * 1024
MIN_PDF_BYTES = 3000


class SciHubError(Exception):
    """Generic Sci-Hub failure."""


class CaptchaRequired(SciHubError):
    """A mirror demands an interactive captcha we cannot answer headlessly."""


class DdosGuardBlocked(SciHubError):
    """DDoS-Guard interstitial."""


class NotFound(SciHubError):
    """The article is not in the database."""


class NotPdf(SciHubError):
    """The response body is not a PDF."""


# "article not in database" wording, from zotero-scihub (2026)
_NOT_FOUND_PATTERNS = (
    "please try to search again using doi",
    "статья не найдена в базе",
    "не найдена в базе",
    "отсутствует в базе",
    "нет в базе",
    "not in database",
    "article not found",
    "not available through sci-hub",
    "not yet available in my database",
    "is not available in my database",
    "try sci-net",
    "paper not found",
)

# --------------------------------------------------------------------------- HTML → PDF link
_RE_IFRAME_PDF = re.compile(
    r"<(?:iframe|embed)\b[^>]*\bid\s*=\s*['\"]pdf['\"][^>]*?\bsrc\s*=\s*['\"]([^'\"]+)", re.I)
_RE_IFRAME_PDF2 = re.compile(
    r"<(?:iframe|embed)\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"][^>]*?\bid\s*=\s*['\"]pdf['\"]", re.I)
_RE_META_PDF = re.compile(
    r"<meta\b[^>]*(?:name|property)\s*=\s*['\"]citation_pdf_url['\"][^>]*>", re.I)
_RE_CONTENT = re.compile(r"content\s*=\s*['\"]([^'\"]+)", re.I)
_RE_OBJECT_PDF = re.compile(
    r"<object\b[^>]*\bdata\s*=\s*['\"]([^'\"]+)", re.I)
_RE_EMBED = re.compile(r"<(?:embed|iframe)\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)", re.I)
_RE_DOWNLOAD = re.compile(
    r"<[^>]*class\s*=\s*['\"][^'\"]*\bdownload\b[^'\"]*['\"][^>]*>\s*<a\b[^>]*href\s*=\s*['\"]([^'\"]+)",
    re.I)
_RE_ANCHOR_PDF = re.compile(r"<a\b[^>]*href\s*=\s*['\"]([^'\"]+\.pdf[^'\"]*)", re.I)

_RE_FALLBACKS = (
    r'<iframe[^>]+id=["\']pdf["\'][^>]*src=["\']([^"\']+)',
    r'<iframe[^>]+src=["\']([^"\']+)["\'][^>]*id=["\']pdf["\']',
    r'<(?:embed|object)[^>]+(?:src|data)=["\']([^"\']+\.pdf[^"\']*)',
    r'location\.href\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']',
    r'["\'](/downloads/[^"\']+\.pdf[^"\']*)["\']',
    r'["\']((?:https?://[^"\']+|/[^"\']+)\.pdf(?:\?[^"\']*)?)["\']',
)


def _clean_url(base_url: str, raw: str) -> str:
    raw = _html.unescape(raw or "").replace("\\/", "/").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    return urllib.parse.urljoin(base_url, raw)


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _clean_mirror(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.startswith("http://"):
        url = "https://" + url[7:]
    if url.startswith("https://www."):
        url = "https://" + url[12:]
    return url


def _is_fake(mirror: str) -> bool:
    low = mirror.lower()
    return any(bad in low for bad in FAKE_DOMAINS)


def discover_mirrors(timeout: float = 10.0) -> list[str]:
    """Scrape the live mirror tracker (yovisun) — cached for six hours.

    The domain landscape changes every few months, so a static list alone goes
    stale. Failure is silent: the caller falls back to DEFAULT_MIRRORS.
    """
    now = time.time()
    with _DISCOVERY_LOCK:
        if _DISCOVERY["mirrors"] and now - _DISCOVERY["at"] < DISCOVERY_TTL:
            return list(_DISCOVERY["mirrors"])
    try:
        req = urllib.request.Request(DISCOVERY_URL, headers={"User-Agent": UA,
                                                             "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    found = []
    for m in re.finditer(r'href=["\'](https?://(?:www\.)?sci-hub\.[a-z]{2,6})[^"\']*["\']',
                         html, re.I):
        url = _clean_mirror(m.group(1))
        if url not in found and not _is_fake(url):
            found.append(url)
    with _DISCOVERY_LOCK:
        _DISCOVERY["mirrors"] = found
        _DISCOVERY["at"] = now
    return list(found)



def pdf_url_from_html(page_url: str, text: str) -> str | None:
    """Return the first plausible PDF link on a Sci-Hub article page."""
    for rx in (_RE_IFRAME_PDF, _RE_IFRAME_PDF2):
        m = rx.search(text or "")
        if m:
            return _clean_url(page_url, m.group(1))
    tag = _RE_META_PDF.search(text or "")
    if tag:
        m = _RE_CONTENT.search(tag.group(0))
        if m:
            return _clean_url(page_url, m.group(1))
    m = _RE_OBJECT_PDF.search(text or "")
    if m:
        return _clean_url(page_url, m.group(1))
    m = _RE_DOWNLOAD.search(text or "")
    if m:
        return _clean_url(page_url, m.group(1))
    m = _RE_ANCHOR_PDF.search(text or "")
    if m:
        return _clean_url(page_url, m.group(1))
    m = _RE_EMBED.search(text or "")
    if m:
        return _clean_url(page_url, m.group(1))
    for pat in _RE_FALLBACKS:
        m = re.search(pat, text or "", re.I)
        if m:
            return _clean_url(page_url, m.group(1))
    return None


# --------------------------------------------------------------------------- session
class _Session:
    """A cookie-keeping opener that tolerates broken mirror certificates."""

    def __init__(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]

    def get(self, url: str, timeout: float = 30, referer: str = "", accept: str = "") -> tuple[int, bytes, str, str]:
        headers = {}
        if referer:
            headers["Referer"] = referer
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(url, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                buf = bytearray()
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        raise SciHubError("文件超过 150 MB")
                return r.status, bytes(buf), r.geturl() or url, (r.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read() or b""
            except Exception:  # noqa: BLE001
                pass
            return e.code, body, url, (e.headers.get("Content-Type") or "").lower() if e.headers else ""

    def post_json(self, url: str, payload: dict, timeout: float = 30) -> tuple[int, bytes]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""


# --------------------------------------------------------------------------- client
class Client:
    def __init__(self, mirrors: list[str] | None = None, scinet: str = DEFAULT_SCINET,
                 timeout: float = 30, stats: dict | None = None,
                 stats_lock: threading.Lock | None = None):
        self.mirrors = [m.strip().rstrip("/") for m in (mirrors or DEFAULT_MIRRORS) if (m or "").strip()]
        self.scinet = (scinet or DEFAULT_SCINET).strip().rstrip("/")
        self.timeout = timeout
        self.stats = stats if stats is not None else {}
        self.stats_lock = stats_lock or threading.Lock()
        self._local = threading.local()
        self.last_attempts: list[tuple[str, str]] = []

    @property
    def session(self) -> _Session:
        # one cookie jar per thread: a solved ALTCHA must stick to that mirror
        if not hasattr(self._local, "sess"):
            self._local.sess = _Session()
        return self._local.sess

    # ---------------- mirror health ordering ----------------
    def _ordered_mirrors(self) -> list[str]:
        if not self.stats:
            return list(self.mirrors)

        def score(m: str) -> float:
            st = self.stats.get(m) or {"ok": 0, "fail": 0}
            return st["fail"] / (st["ok"] + st["fail"] + 0.01)

        return sorted(self.mirrors, key=score)

    def _bump(self, mirror: str, ok: bool) -> None:
        with self.stats_lock:
            st = self.stats.setdefault(mirror, {"ok": 0, "fail": 0})
            st["ok" if ok else "fail"] += 1

    # ---------------- main flow ----------------
    def resolve_pdf_url(self, doi: str) -> tuple[str, str]:
        """Return (pdf_url, source). Raises SciHubError subclasses when nothing works."""
        self.last_attempts = []
        doi_q = urllib.parse.quote(doi, safe="/")
        last_err: Exception | None = None
        captcha_err: Exception | None = None
        ddos_err: Exception | None = None
        not_indexed = False
        for mirror in self._ordered_mirrors():
            url = f"{mirror}/{doi_q}"
            try:
                pdf_url = self._resolve_on_mirror(url, doi)
                if pdf_url:
                    self._bump(mirror, True)
                    return pdf_url, "Sci-Hub"
            except DdosGuardBlocked as e:
                self._bump(mirror, False)
                self.last_attempts.append((mirror, "ddos-guard"))
                last_err = ddos_err = e
            except CaptchaRequired as e:
                self._bump(mirror, False)
                self.last_attempts.append((mirror, "captcha"))
                last_err = e
                captcha_err = captcha_err or e
            except NotFound as e:
                self._bump(mirror, False)
                self.last_attempts.append((mirror, "未收录"))
                # mirrors share one corpus: the first "not in database" is
                # authoritative, and asking the other six only burns time
                last_err = e
                not_indexed = True
                break
            except SciHubError as e:
                self._bump(mirror, False)
                self.last_attempts.append((mirror, self._short(e)))
                last_err = e
            except Exception as e:  # noqa: BLE001 - a dead mirror must not abort the chain
                self._bump(mirror, False)
                self.last_attempts.append((mirror, type(e).__name__))
                last_err = SciHubError(f"镜像 {mirror} 失败：{type(e).__name__}")

        # Sci-Net covers post-2021 articles Sci-Hub never indexed
        try:
            pdf_url = self.resolve_scinet_pdf_url(doi)
            if pdf_url:
                return pdf_url, "Sci-Net"
        except Exception:  # noqa: BLE001
            pass

        if not_indexed:
            raise NotFound(f"文章不在 Sci-Hub 数据库：{doi}")

        if captcha_err is not None:
            raise captcha_err
        if ddos_err is not None:
            raise ddos_err
        if last_err is not None:
            raise last_err
        raise NotFound(f"所有镜像均未返回可用结果：{doi}")

    @staticmethod
    def _short(e: Exception) -> str:
        return str(e)[:40] or type(e).__name__

    def _resolve_on_mirror(self, url: str, doi: str) -> str | None:
        for _attempt in range(2):
            status, body, final_url, ctype = self.session.get(url, timeout=self.timeout)
            html = body.decode("utf-8", "replace") if body else ""
            kind, value = self._classify(status, html, final_url)
            if kind == "ok":
                return value
            if kind == "notfound":
                raise NotFound(f"文章不在 Sci-Hub 数据库：{doi}")
            if kind == "ddos":
                raise DdosGuardBlocked("被 DDoS-Guard 拦截")
            if kind == "captcha":
                info = value or {}
                if info.get("kind") == "altcha" and info.get("challenge_url"):
                    if self._solve_altcha(final_url, info["challenge_url"]):
                        continue  # cookie is set now; retry the article page
                    raise CaptchaRequired("ALTCHA 求解失败")
                raise CaptchaRequired("Sci-Hub 要求人工验证码")
            raise SciHubError(f"无法从页面解析 PDF 直链：{url}")
        raise CaptchaRequired("ALTCHA 多次求解后仍被拦截")

    # ---------------- ALTCHA proof of work ----------------
    @staticmethod
    def _pow(challenge: dict) -> int:
        alg = (challenge.get("algorithm") or "SHA-256").replace("-", "").lower()
        salt = challenge.get("salt", "")
        target = challenge.get("challenge", "")
        maxn = int(challenge.get("maxNumber") or 1000000)
        for n in range(maxn + 1):
            if hashlib.new(alg, (salt + str(n)).encode()).hexdigest() == target:
                return n
        raise SciHubError("ALTCHA PoW 未解出")

    def _solve_altcha(self, page_url: str, challenge_url: str) -> bool:
        try:
            cid_url = _clean_url(page_url, challenge_url)
            status, body, _final, _ctype = self.session.get(cid_url, timeout=self.timeout,
                                                            accept="application/json")
            if status != 200 or not body:
                return False
            ch = json.loads(body.decode("utf-8", "replace"))
            num = self._pow(ch)
            payload = base64.b64encode(json.dumps({
                "algorithm": ch.get("algorithm"),
                "challenge": ch.get("challenge"),
                "number": num,
                "salt": ch.get("salt"),
                "signature": ch.get("signature"),
            }).encode()).decode()
            sol_url = cid_url.replace("/challenge/", "/solution/")
            if sol_url == cid_url:
                return False
            status, body = self.session.post_json(sol_url, {"captcha": payload}, timeout=self.timeout)
            return status == 200 and b'"success":true' in body.replace(b" ", b"")
        except Exception:  # noqa: BLE001
            return False

    # ---------------- classification ----------------
    def _classify(self, status: int, html: str, base_url: str) -> tuple[str, object]:
        text = (html or "").lower()
        if status == 403 and "ddos-guard" in text:
            return ("ddos", None)
        if status == 404:
            return ("notfound", None)
        if status != 200 and not html:
            return ("error", None)

        # a real PDF link means this is the article page (the report form also has altcha)
        pdf_url = pdf_url_from_html(base_url, html)
        if pdf_url:
            return ("ok", pdf_url)

        for pat in _NOT_FOUND_PATTERNS:
            if pat in text:
                return ("notfound", None)
        if not (html or "").strip():
            return ("notfound", None)

        if any(mk in text for mk in ("turnstile", "cf-turnstile", "challenges.cloudflare.com", "pow_challenge")):
            return ("captcha", {"kind": "challenge"})

        m = re.search(r"<altcha-widget\b[^>]*\bchallengeurl\s*=\s*['\"]([^'\"]+)", html or "", re.I)
        if m:
            return ("captcha", {"kind": "altcha", "challenge_url": m.group(1)})

        if re.search(r'id\s*=\s*["\']captcha["\']', html or "", re.I) or \
                re.search(r'<input\b[^>]*name\s*=\s*["\']id["\']', html or "", re.I):
            img = re.search(r'<[^>]*id\s*=\s*["\']captcha["\'][^>]*src\s*=\s*["\']([^"\']+)', html or "", re.I)
            cid = re.search(r'<input\b[^>]*name\s*=\s*["\']id["\'][^>]*value\s*=\s*["\']([^"\']+)', html or "", re.I)
            return ("captcha", {"kind": "image",
                                "image_url": _clean_url(base_url, img.group(1)) if img else "",
                                "captcha_id": cid.group(1) if cid else ""})

        if re.search(r'class\s*=\s*["\'][^"\']*\bquestion\b', html or "", re.I):
            return ("captcha", {"kind": "altcha"})

        return ("error", None)

    # ---------------- Sci-Net ----------------
    def resolve_scinet_pdf_url(self, doi: str) -> str | None:
        url = f"{self.scinet}/{urllib.parse.quote(doi, safe='/')}"
        try:
            status, body, final_url, _ = self.session.get(url, timeout=self.timeout)
        except Exception:  # noqa: BLE001
            return None
        if status != 200 or not body:
            return None
        html = body.decode("utf-8", "replace")
        m = re.search(r'<(?:iframe|embed)\b[^>]*\bsrc\s*=\s*["\']([^"\']+\.pdf[^"\']*)', html, re.I)
        if m:
            return _clean_url(final_url, m.group(1))
        for pat in (r'<a\b[^>]*href\s*=\s*["\']([^"\']+\.pdf[^"\']*)',
                    r'["\']((?:https?://[^"\']+|/[^"\']+)\.pdf(?:\?[^"\']*)?)["\']'):
            m = re.search(pat, html, re.I)
            if m:
                return _clean_url(final_url, m.group(1))
        return pdf_url_from_html(final_url, html)

    # ---------------- download ----------------
    def download_pdf(self, pdf_url: str, referer: str = "") -> bytes:
        """Fetch the PDF and verify the magic number before the caller writes it."""
        url = _strip_fragment(pdf_url)
        status, body, final_url, ctype = self.session.get(
            url, timeout=self.timeout, referer=referer,
            accept="application/pdf,application/octet-stream;q=0.9,*/*;q=0.8")
        if status != 200 or not body:
            raise SciHubError(f"PDF 下载失败（HTTP {status}）")
        if not body[:1024].lstrip().startswith(b"%PDF") and b"%PDF" not in body[:1024]:
            # some mirrors answer with an HTML interstitial naming the real file
            alt = pdf_url_from_html(final_url, body.decode("utf-8", "replace")) \
                if "html" in ctype else None
            if alt and alt != url:
                return self.download_pdf(alt, referer=final_url)
            raise NotPdf("下载内容不是 PDF（可能被拦截或跳转到网页）")
        if len(body) < MIN_PDF_BYTES:
            raise SciHubError(f"PDF 过小（{len(body)} 字节），可能不是正文")
        return body


# --------------------------------------------------------------------------- module helpers
_LOCK = threading.Lock()
_CLIENT: Client | None = None
_CLIENT_KEY: tuple | None = None
STATS: dict[str, dict[str, int]] = {}


def client(mirrors: list[str] | None = None, scinet: str = DEFAULT_SCINET,
           timeout: float = 30) -> Client:
    """One shared client per (mirror list, scinet) combination so stats accumulate."""
    global _CLIENT, _CLIENT_KEY
    key = (tuple(mirrors or DEFAULT_MIRRORS), scinet or DEFAULT_SCINET, int(timeout))
    with _LOCK:
        if _CLIENT is None or _CLIENT_KEY != key:
            _CLIENT = Client(mirrors=key[0], scinet=key[1], timeout=timeout, stats=STATS)
            _CLIENT_KEY = key
        return _CLIENT


def load_stats(saved: dict | None) -> None:
    """Restore per-mirror success/failure counters from a previous session."""
    if not isinstance(saved, dict):
        return
    with _LOCK:
        for mirror, st in saved.items():
            if isinstance(st, dict) and "ok" in st and "fail" in st:
                STATS[mirror] = {"ok": int(st.get("ok") or 0), "fail": int(st.get("fail") or 0)}


def stats_snapshot() -> dict:
    with _LOCK:
        return {m: dict(st) for m, st in STATS.items()}



def _priority(mirror: str) -> int:
    """Keep the user's configured order; unknown mirrors go last."""
    for i, m in enumerate(DEFAULT_MIRRORS):
        if m in mirror or mirror in m:
            return i
    return 99


def fetch(doi: str, mirrors: list[str] | None = None, scinet: str = DEFAULT_SCINET,
          timeout: float = 30, discover: bool = True,
          should_stop=None, budget: float = 90.0) -> tuple[bytes | None, str, str, str]:
    """Resolve and download one DOI via Sci-Hub / Sci-Net.

    Returns (pdf_bytes, error_text, source_label, page_url). pdf_bytes is None on
    failure; error_text says why (未收录 / 验证码 / DDoS-Guard / …).

    Resolution and download happen per mirror: a mirror that shows a PDF link but
    refuses the file (DDoS-Guard on /storage) must not end the whole attempt.
    """
    chosen = [m for m in (mirrors or DEFAULT_MIRRORS) if not _is_fake(m)]
    if discover:
        live = discover_mirrors(timeout=5.0)
        if live:
            merged, seen = [], set()
            # the configured order wins; discovered hosts are only appended
            for m in chosen + live:
                key = m.lower()
                if key not in seen:
                    seen.add(key)
                    merged.append(m)
            chosen = merged
    chosen = sorted(chosen, key=_priority)

    c = client(chosen, scinet, timeout)
    doi_q = urllib.parse.quote(doi, safe="/")
    errors: list[str] = []
    captcha = ddos = False
    t_end = time.monotonic() + budget   # whole Sci-Hub attempt, all mirrors together
    for mirror in c._ordered_mirrors():
        if should_stop and should_stop():  # stop/pause between mirrors
            return None, "已停止", "", ""
        if time.monotonic() > t_end:
            errors.append("总时限到")
            break
        url = f"{mirror}/{doi_q}"
        try:
            pdf_url = c._resolve_on_mirror(url, doi)
        except NotFound:
            return None, "Sci-Hub 未收录", "", ""
        except CaptchaRequired:
            captcha = True
            errors.append(f"{mirror}:验证码")
            continue
        except DdosGuardBlocked:
            ddos = True
            errors.append(f"{mirror}:DDoS-Guard")
            continue
        except SciHubError as e:
            errors.append(f"{mirror}:{str(e)[:24]}")
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{mirror}:{type(e).__name__}")
            continue
        if not pdf_url:
            errors.append(f"{mirror}:无直链")
            continue
        page = pdf_url.split("#", 1)[0]
        try:
            body = c.download_pdf(pdf_url)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{mirror}:下载失败({str(e)[:20]})")
            c._bump(mirror, False)
            continue
        c._bump(mirror, True)
        return body, "", "Sci-Hub", page

    # Sci-Net covers post-2021 articles Sci-Hub never indexed
    if (should_stop and should_stop()):
        return None, "已停止", "", ""
    try:
        scinet_url = c.resolve_scinet_pdf_url(doi)
        if scinet_url:
            body = c.download_pdf(scinet_url)
            return body, "", "Sci-Net", scinet_url.split("#", 1)[0]
    except Exception:  # noqa: BLE001
        pass

    detail = "；".join(errors[:3])
    if captcha:
        return None, "Sci-Hub 要求验证码（已自动尝试 ALTCHA）", "", ""
    if ddos:
        return None, "Sci-Hub 镜像被 DDoS-Guard 拦截", "", ""
    return None, f"Sci-Hub 失败：{detail or '所有镜像不可用'}", "", ""
