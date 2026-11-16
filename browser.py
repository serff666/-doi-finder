"""Institutional access through the user's own Chrome — no password ever touches us.

Why this design: a library/school subscription is bound to a browser session
(SSO/EZproxy cookies) or to the campus IP. The app cannot "read your account off
the network", but it can drive a real Chrome window that is already logged in, and
that window carries the entitlement. So:

  • one dedicated Chrome profile lives in the app data folder
  • you log in once (with MFA / robot check, in the visible window)
  • every later DOI opens inside that same session, and the PDF response is captured
  • if a site asks "are you a robot", the window is right there — click once and the
    download continues

Implemented over the DevTools Protocol with the standard library only, so the
frozen build stays dependency-free. Chrome/Edge is used as installed.
"""
from __future__ import annotations

import base64
import json
import os
import random
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request

import platform_util

CHROME_CANDIDATES = platform_util.browser_candidates()

# titles that mean "a human has to click something first"
_CHALLENGE_TITLES = ("just a moment", "verification", "проверка", "are you a robot",
                     "attention required", "checking your browser", "ddos-guard",
                     "请稍候", "稍候", "正在验证", "安全验证")
_CHALLENGE_HTML = ("cf-challenge", "challenge-platform", "cf_chl_", "just a moment",
                   "ddos-guard", "verifying you are human")


class BrowserError(RuntimeError):
    pass


_PREF = ""  # '' = auto (Chrome first) | 'chrome' | 'edge'; set from settings


def set_pref(value) -> None:
    """Apply the user's choice from settings.json ('auto' / 'chrome' / 'edge')."""
    global _PREF
    v = str(value or "").strip().lower()
    if v in ("edge", "msedge"):
        _PREF = "edge"
    elif v in ("chrome", "google"):
        _PREF = "chrome"
    else:
        _PREF = ""


def browser_pref() -> str:
    """Which browser the user wants: 'edge', 'chrome' or '' (auto, Chrome first)."""
    return _PREF


def browser_tag() -> str:
    """Suffix for the profile folder so Chrome and Edge never share one profile."""
    p = browser_pref()
    if p in ("edge", "msedge"):
        return "-edge"
    if p in ("chrome", "google"):
        return "-chrome"
    return ""


def find_browser() -> str | None:
    pref = browser_pref()
    if pref in ("edge", "msedge"):
        ordered = [c for c in CHROME_CANDIDATES if platform_util.is_edge(c)]
    elif pref in ("chrome", "google"):
        ordered = [c for c in CHROME_CANDIDATES if not platform_util.is_edge(c)]
    else:
        ordered = list(CHROME_CANDIDATES)
    for p in ordered:
        if p and os.path.isfile(p):
            return p
    # 指定的浏览器没装：回落到全部候选，至少别让功能直接不可用
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


# --------------------------------------------------------------------------- websocket
class _WS:
    """Minimal RFC 6455 client — enough for DevTools messages."""

    def __init__(self, url: str, timeout: float = 30):
        assert url.startswith("ws://"), url
        rest = url[5:]
        host_port, _, path = rest.partition("/")
        host, _, port = host_port.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("WebSocket 握手被拒绝")
            head += chunk
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise BrowserError("WebSocket 握手失败：" + head.split(b"\r\n", 1)[0].decode("latin1"))
        self._buf = head.split(b"\r\n\r\n", 1)[1]

    def _send(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text: str):
        self._send(0x1, text.encode("utf-8"))

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(262144)
            if not chunk:
                raise BrowserError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self, timeout: float | None = None) -> str:
        """Return the next complete text message (pings are answered transparently)."""
        if timeout is not None:
            self.sock.settimeout(timeout)
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping
                self._send(0xA, payload)
                continue
            if opcode == 0x8:  # close
                raise BrowserError("浏览器关闭了连接")
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self._send(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


class _CDP:
    """DevTools connection: id-tagged calls plus a queue of events."""

    def __init__(self, ws_url: str, timeout: float = 30):
        self.ws = _WS(ws_url, timeout=timeout)
        self.next_id = 0
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def call(self, method: str, params: dict | None = None, timeout: float = 30):
        self.next_id += 1
        mid = self.next_id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while True:
            remain = max(0.1, end - time.time())
            msg = json.loads(self.ws.recv(timeout=remain))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise BrowserError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result") or {}
            if "method" in msg:
                self.events.append(msg)

    def pump(self, timeout: float = 0.5) -> list[dict]:
        """Collect whatever events arrive within the timeout."""
        out = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv(timeout=max(0.05, end - time.time())))
            except (socket.timeout, TimeoutError):
                break
            if "method" in msg:
                out.append(msg)
            else:
                self.events.append(msg)
        return out

    def close(self):
        self.ws.close()


_RG_CHALLENGE = ("security check", "temporarily unavailable", "unusual activity",
                 "attention required", "checking your browser")

_RG_JS = """(() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/publication/"]').forEach((a) => {
    const t = (a.textContent || '').replace(/\\s+/g, ' ').trim();
    if (t.length < 12) return;
    if (seen.has(a.href)) return;
    seen.add(a.href);
    out.push({ title: t, url: a.href });
  });
  return JSON.stringify({ n: out.length, items: out.slice(0, %d),
                          title: document.title });
})()"""


def rg_publications(profile_dir: str, url: str, timeout: float = 90,
                    max_items: int = 500) -> tuple[list[dict], str]:
    """Read an author's publication list from ResearchGate in the logged-in browser.

    ResearchGate sits behind Cloudflare: plain HTTP always gets 403, and even a
    real browser is challenged until the user clears the check once in the visible
    window. So this waits while the challenge is on screen, then scrapes the links.
    """
    browser = _shared(profile_dir)
    tab = None
    challenged = False
    try:
        tab = browser.open_tab(url)  # 直接开在目标页，省掉 about:blank 那一跳
        tab.enable()
        tab.focus()  # 验证页必须让人看见，否则点不了
        started = time.time()
        deadline = started + timeout
        challenged = False
        while time.time() < deadline:
            tab.cdp.pump(0.4)
            title = tab.title()
            low = title.lower()
            if any(k in low for k in _RG_CHALLENGE):
                challenged = True
                continue
            html_len = tab.cdp.call("Runtime.evaluate", {
                "expression": "document.body ? document.body.innerText.length : 0",
                "returnByValue": True}, timeout=10).get("result", {}).get("value") or 0
            if html_len > 1500 or time.time() - started > 20:
                break
        res = tab.cdp.call("Runtime.evaluate", {"expression": _RG_JS % max_items,
                                                "returnByValue": True}, timeout=40)
        raw = (res.get("result") or {}).get("value") or "{}"
        data = json.loads(raw)
        items = [x for x in (data.get("items") or []) if x.get("title")]

        # ResearchGate 的论文列表是分页的，不是滚动加载。点"显示更多"或滚到底都没用，
        # 必须逐页翻。这里按 URL 的 page 参数翻，直到某页不再给出新条目。
        base = url.split("?")[0].rstrip("/")
        if not base.endswith("/research"):
            base = base + "/research"
        seen = {x.get("url") or x.get("title") for x in items}
        empty_pages = 0
        for page_no in range(2, 61):
            if len(items) >= max_items:
                break
            try:
                tab.cdp.call("Page.navigate", {"url": f"{base}?page={page_no}"}, timeout=45)
            except Exception:  # noqa: BLE001
                break
            # 等列表渲染出来：最多等 12 秒，看到条目就够了
            waited = 0.0
            while waited < 12:
                time.sleep(0.8)
                waited += 0.8
                try:
                    res = tab.cdp.call("Runtime.evaluate", {"expression": _RG_JS % max_items,
                                                            "returnByValue": True}, timeout=40)
                    got = json.loads((res.get("result") or {}).get("value") or "{}")
                except Exception:  # noqa: BLE001
                    continue
                page_items = [x for x in (got.get("items") or []) if x.get("title")]
                if page_items:
                    break
            fresh = 0
            for it in page_items:
                key = it.get("url") or it.get("title")
                if key and key not in seen:
                    seen.add(key)
                    items.append(it)
                    fresh += 1
            if not fresh:
                empty_pages += 1
                if empty_pages >= 2:  # 连续两页没有新内容，认为到底
                    break
            else:
                empty_pages = 0
        if not items:
            if challenged:
                return [], "ResearchGate 仍在人机验证：窗口已调到前台并保留，请点一下验证后重试"
            return [], f"页面上没有找到论文链接（当前标题：{data.get('title') or '空'}）"
        return items, ""
    except BrowserError as e:
        return [], f"浏览器通道失败：{e}"
    except Exception as e:  # noqa: BLE001
        return [], f"抓取失败：{type(e).__name__}"
    finally:
        if tab is not None:
            if not challenged:  # 验证页留着，用户点完还能复用
                try:
                    browser.close_tab(tab.target_id)
                except Exception:  # noqa: BLE001
                    pass
            tab.detach()


# --------------------------------------------------------------------------- browser
def _kill_stale(profile_dir: str) -> None:
    """Kill Chrome instances already holding this profile (they block the debug port).

    A leftover Chrome from a previous run keeps the user-data-dir locked; a fresh
    launch then just forwards the URL to that instance and exits, so our debugging
    port never comes up. Only processes whose command line names our profile are
    touched.
    """
    platform_util.kill_browser_using_profile(profile_dir)


def _ensure_pdf_download_pref(profile_dir: str) -> None:
    """Make Chrome download PDFs instead of rendering them, so we can capture the file.

    With the built-in viewer the PDF never appears as a page network response
    (it is streamed by a plugin process), which would leave us empty-handed.
    """
    try:
        default = os.path.join(profile_dir, "Default")
        os.makedirs(default, exist_ok=True)
        path = os.path.join(default, "Preferences")
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:  # noqa: BLE001
                data = {}
        plugins = data.setdefault("plugins", {})
        if plugins.get("always_open_pdf_externally") is True:
            return
        plugins["always_open_pdf_externally"] = True
        data.setdefault("download", {})["prompt_for_download"] = False
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


class Browser:
    """One shared Chrome process with a persistent profile."""

    def __init__(self, profile_dir: str, headless: bool = False):
        self.profile_dir = profile_dir
        self.headless = headless
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.lock = threading.Lock()

    # ---------------- lifecycle ----------------
    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def running(self) -> bool:
        if not self.port:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1) as r:
                return b"webSocketDebuggerUrl" in r.read()
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> None:
        with self.lock:
            if self.running():
                return
            exe = find_browser()
            if not exe:
                raise BrowserError("没有找到 Chrome / Edge")
            os.makedirs(self.profile_dir, exist_ok=True)
            # a previous run may still hold the profile without our debug port
            _kill_stale(self.profile_dir)
            time.sleep(0.4)
            _ensure_pdf_download_pref(self.profile_dir)
            self.port = self._free_port()
            args = [exe,
                    f"--remote-debugging-port={self.port}",
                    f"--user-data-dir={self.profile_dir}",
                    "--no-first-run", "--no-default-browser-check",
                    "--disable-features=Translate,MediaRouter",
                    "--restore-last-session=false",
                    "--no-startup-window",  # 不建初始窗口：需要时才由 open_tab 开一个
                    "--window-size=1280,900"]
            if self.headless:
                args += ["--headless=new", "--disable-gpu"]
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            end = time.time() + 25
            while time.time() < end:
                if self.running():
                    return
                time.sleep(0.35)
            raise BrowserError("Chrome 启动超时（调试端口没有响应）")

    def stop(self) -> None:
        with self.lock:
            if self.proc:
                try:
                    self.proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                self.proc = None
            self.port = 0

    def version(self) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=5) as r:
            return json.loads(r.read().decode())

    def open_tab(self, url: str = "about:blank") -> "_Tab":
        """Create a tab and attach to it (the browser-level socket is per call)."""
        self.start()
        ws_url = self.version()["webSocketDebuggerUrl"]
        browser_cdp = _CDP(ws_url)
        try:
            target = browser_cdp.call("Target.createTarget", {"url": url})
            target_id = target["targetId"]
        finally:
            browser_cdp.close()
        # find the page socket
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=5) as r:
            tabs = json.loads(r.read().decode())
        page_ws = ""
        for t in tabs:
            if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                page_ws = t["webSocketDebuggerUrl"]
                break
        if not page_ws:
            raise BrowserError("无法连接到新打开的标签页")
        return _Tab(self, target_id, page_ws)

    def activate(self, target_id: str) -> None:
        """Raise a tab's window to the front — browser-level, works when backgrounded."""
        try:
            browser_cdp = _CDP(self.version()["webSocketDebuggerUrl"])
            try:
                browser_cdp.call("Target.activateTarget", {"targetId": target_id}, timeout=5)
            finally:
                browser_cdp.close()
        except Exception:  # noqa: BLE001
            pass

    def close_tab(self, target_id: str) -> None:
        try:
            browser_cdp = _CDP(self.version()["webSocketDebuggerUrl"])
            try:
                browser_cdp.call("Target.closeTarget", {"targetId": target_id})
            finally:
                browser_cdp.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- login window ----------------
    def open_login(self, url: str) -> None:
        """Open a normal, visible tab the user can log in with."""
        self.headless = False
        tab = self.open_tab(url)
        time.sleep(0.5)
        try:
            tab.cdp.call("Page.bringToFront", timeout=5)
        except Exception:  # noqa: BLE001
            pass
        tab.detach()  # leave the tab open; the user owns it now


class _Tab:
    def __init__(self, browser: Browser, target_id: str, ws_url: str):
        self.browser = browser
        self.target_id = target_id
        self.cdp = _CDP(ws_url)
        self.html = ""

    def enable(self, download_dir: str = ""):
        self.cdp.call("Page.enable")
        self.cdp.call("Network.enable", {"maxPostDataSize": 65536})
        if download_dir:
            self.download_dir = download_dir
            try:
                self.cdp.call("Browser.setDownloadBehavior",
                              {"behavior": "allow", "downloadPath": download_dir,
                               "eventsEnabled": True}, timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self.cdp.call("Page.setDownloadBehavior",
                                  {"behavior": "allow", "downloadPath": download_dir}, timeout=5)
                except Exception:  # noqa: BLE001
                    pass

    def focus(self) -> None:
        """Bring the tab to the foreground so a human can see a challenge page."""
        try:
            self.cdp.call("Page.bringToFront", timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.browser.activate(self.target_id)
        except Exception:  # noqa: BLE001
            pass

    def navigate(self, url: str, timeout: float = 45):
        self.cdp.call("Page.navigate", {"url": url}, timeout=timeout)

    def title(self) -> str:
        try:
            r = self.cdp.call("Runtime.evaluate",
                              {"expression": "document.title", "returnByValue": True}, timeout=5)
            return (r.get("result") or {}).get("value") or ""
        except Exception:  # noqa: BLE001
            return ""

    def page_html(self) -> str:
        try:
            r = self.cdp.call("Runtime.evaluate",
                              {"expression": "document.documentElement.outerHTML",
                               "returnByValue": True}, timeout=10)
            return (r.get("result") or {}).get("value") or ""
        except Exception:  # noqa: BLE001
            return ""

    def cookies(self) -> list[dict]:
        try:
            return self.cdp.call("Network.getAllCookies", timeout=10).get("cookies") or []
        except Exception:  # noqa: BLE001
            return []

    def current_url(self) -> str:
        try:
            r = self.cdp.call("Runtime.evaluate",
                              {"expression": "location.href", "returnByValue": True}, timeout=5)
            return (r.get("result") or {}).get("value") or ""
        except Exception:  # noqa: BLE001
            return ""

    def detach(self):
        try:
            self.cdp.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- fetch
def _html_to_pdf_url(page_url: str, html: str) -> str:
    """Prefer the academic meta tag, then reuse the generic landing-page extractor."""
    import re
    import urllib.parse
    m = re.search(r"<meta\b[^>]*(?:name|property)\s*=\s*['\"]citation_pdf_url['\"][^>]*>", html, re.I)
    if m:
        mm = re.search(r"content\s*=\s*['\"]([^'\"]+)", m.group(0), re.I)
        if mm:
            return urllib.parse.urljoin(page_url, mm.group(1).replace("&amp;", "&"))
    m = re.search(r"<link\b[^>]*type\s*=\s*['\"]application/pdf['\"][^>]*>", html, re.I)
    if m:
        mm = re.search(r"href\s*=\s*['\"]([^'\"]+)", m.group(0), re.I)
        if mm:
            return urllib.parse.urljoin(page_url, mm.group(1))
    try:
        import sources
        links = sources.pdf_links_from_html(page_url, html.encode("utf-8", "replace"))
        for link in links:
            low = urllib.parse.urlparse(link).path.lower()
            if low.endswith(".pdf") or "/pdf" in low or "download" in low:
                return link
    except Exception:  # noqa: BLE001
        pass
    return ""


def _is_challenge(title: str) -> bool:
    t = (title or "").lower()
    return any(x in t for x in _CHALLENGE_TITLES)


def _page_fetch(tab: "_Tab", pdf_url: str) -> tuple[bytes | None, str]:
    """Fetch a URL inside the page: inherits its cookies, TLS and Cloudflare pass.

    A page-initiated fetch keeps the Cloudflare clearance that the tab already
    earned, which a separate urllib request cannot reproduce.
    """
    js = """
    (async () => {
      try {
        const r = await fetch(%s, {credentials: 'include', redirect: 'follow'});
        if (!r.ok) return 'ERRHTTP:' + r.status;
        const b = new Uint8Array(await r.arrayBuffer());
        window.__doif = window.__doif || {};
        window.__doif.buf = b;
        return 'OK:' + b.length;
      } catch (e) { return 'ERR:' + e; }
    })()
    """ % json.dumps(pdf_url)
    try:
        res = tab.cdp.call("Runtime.evaluate",
                           {"expression": js, "awaitPromise": True, "returnByValue": True},
                           timeout=90)
        value = str((res.get("result") or {}).get("value") or "")
    except Exception as e:  # noqa: BLE001
        return None, f"页面内下载失败：{type(e).__name__}"
    if not value.startswith("OK:"):
        return None, f"页面内下载失败：{value[:60]}"
    total = int(value[3:] or 0)
    if total < 3000:
        return None, f"页面内下载内容过小（{total} 字节）"

    out = bytearray()
    step = 4 * 1024 * 1024
    for start in range(0, total, step):
        chunk_js = ("(() => { const b = window.__doif.buf; const s = b.subarray(%d, %d);"
                    " let out = ''; for (let i = 0; i < s.length; i += 0x8000) {"
                    " out += String.fromCharCode.apply(null, s.subarray(i, i + 0x8000)); }"
                    " return btoa(out); })()" % (start, min(start + step, total)))
        try:
            res = tab.cdp.call("Runtime.evaluate",
                               {"expression": chunk_js, "returnByValue": True}, timeout=60)
            b64 = str((res.get("result") or {}).get("value") or "")
            out.extend(base64.b64decode(b64))
        except Exception as e:  # noqa: BLE001
            return None, f"分块读取失败：{type(e).__name__}"
    data = bytes(out)
    if not data[:5].startswith(b"%PDF"):
        return None, "返回内容不是 PDF"
    return data, ""


def _read_new_file(folder: str) -> bytes | None:
    """Return a finished PDF that Chrome wrote into the download folder."""
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        if name.endswith((".crdownload", ".part", ".tmp")):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.getsize(path) < 3000:
                continue
            with open(path, "rb") as fh:
                if not fh.read(5).startswith(b"%PDF"):
                    continue
            size = os.path.getsize(path)
            time.sleep(0.25)
            if os.path.getsize(path) != size:
                continue  # still growing
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            continue
    return None


def fetch_pdf(doi: str, profile_dir: str, timeout: float = 75,
              login_url: str = "") -> tuple[bytes | None, str, str]:
    """Open the DOI in the logged-in browser profile and capture the PDF.

    Returns (pdf_bytes, error, page_url). Blocks while a human solves a challenge,
    up to `timeout` seconds after the first challenge title is seen.
    """
    browser = _shared(profile_dir)
    tab = None
    dl_dir = ""
    challenge_seen = 0.0
    try:
        import tempfile
        dl_dir = tempfile.mkdtemp(prefix="doif_dl_")
        tab = browser.open_tab("about:blank")
        tab.enable(download_dir=dl_dir)
        # 允许直接传完整 URL：MDPI 这类站把 PDF 放在固定路径，403 只挡纯 HTTP
        target = doi if str(doi).lower().startswith("http") else f"https://doi.org/{doi}"
        tab.navigate(target)
        tab.focus()  # 验证页必须让人看见，否则点不了
        started = time.time()
        deadline = started + timeout
        challenge_focus = 0.0
        pdf_navigated_at = 0.0
        last_scan = 0.0
        while time.time() < deadline:
            # a PDF that Chrome displays in its viewer arrives as a network response
            for ev in tab.cdp.pump(0.4):
                if ev.get("method") != "Network.responseReceived":
                    continue
                resp = ev["params"]["response"]
                mime = (resp.get("mimeType") or "").lower()
                url = resp.get("url") or ""
                if "pdf" not in mime and not url.lower().split("?")[0].endswith(".pdf"):
                    continue
                rid = ev["params"]["requestId"]
                try:
                    body = tab.cdp.call("Network.getResponseBody", {"requestId": rid}, timeout=40)
                except Exception:  # noqa: BLE001
                    continue
                data = (base64.b64decode(body["body"]) if body.get("base64Encoded")
                        else (body.get("body") or "").encode("latin-1"))
                if data[:5].startswith(b"%PDF"):
                    return data, "", url

            # a PDF served as an attachment is written to disk instead
            got = _read_new_file(dl_dir)
            if got:
                return got, "", tab.current_url()

            now = time.time()
            title = tab.title()
            if _is_challenge(title):
                challenge_seen = challenge_seen or now
                if now - challenge_focus > 12:  # 验证页可能被别的窗口盖住，隔一会再拎到前台
                    challenge_focus = now
                    tab.focus()
                if now - challenge_seen > 25 and login_url:
                    # the session may have expired: hand the window back to the user
                    tab.navigate(login_url)
                    challenge_seen = now
                continue
            challenge_seen = 0.0

            # scan the page repeatedly: publisher pages render their PDF button late
            if not pdf_navigated_at and now - last_scan > 2:
                last_scan = now
                html = tab.page_html()
                pdf_url = _html_to_pdf_url(tab.current_url(), html)
                if pdf_url:
                    pdf_navigated_at = now
                    data, ferr = _page_fetch(tab, pdf_url)
                    if data:
                        return data, "", pdf_url
                    # fall back to a real navigation (viewer / attachment)
                    tab.navigate(pdf_url)
                    continue
            if pdf_navigated_at and now - pdf_navigated_at > 20:
                break
            if not pdf_navigated_at and now - started > 45:
                break

        if challenge_seen:
            return (None,
                    "需要你点一下人机验证：窗口已调到前台并保留，验证完成后直接重试即可",
                    tab.current_url() if tab else "")
        return None, "机构通道没有拿到 PDF（可能需要登录或没有订阅）", tab.current_url() if tab else ""
    except BrowserError as e:
        return None, f"浏览器通道失败：{e}", ""
    except Exception as e:  # noqa: BLE001
        return None, f"浏览器通道失败：{type(e).__name__}", ""
    finally:
        if tab is not None:
            if not challenge_seen:  # 验证页留着：用户点完，这次的 clearance 下次还能用
                try:
                    browser.close_tab(tab.target_id)
                except Exception:  # noqa: BLE001
                    pass
            tab.detach()
        if dl_dir:
            try:
                import shutil
                shutil.rmtree(dl_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


_BROWSERS: dict[str, Browser] = {}
_BROWSERS_LOCK = threading.Lock()


def _shared(profile_dir: str) -> Browser:
    with _BROWSERS_LOCK:
        b = _BROWSERS.get(profile_dir)
        if b is None:
            b = Browser(profile_dir)
            _BROWSERS[profile_dir] = b
        return b


def open_login(profile_dir: str, url: str) -> tuple[bool, str]:
    try:
        b = _shared(profile_dir)
        b.open_login(url)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def close_browser(profile_dir: str) -> None:
    with _BROWSERS_LOCK:
        b = _BROWSERS.pop(profile_dir, None)
    if b:
        b.stop()


def profile_ready(profile_dir: str) -> bool:
    """True when the profile directory already holds a browser profile."""
    try:
        return any(os.scandir(os.path.join(profile_dir, "Default")))
    except Exception:  # noqa: BLE001
        return False

_RG_FIND = r"""(() => {
  const a = [...document.querySelectorAll('a[href*="/publication/"]')][0];
  return a ? a.href : "";
})()"""

_RG_DL = r"""(() => {
  const btns = [...document.querySelectorAll('a, button')];
  const hit = btns.find((b) => /download full-?text|download pdf|下载全文/i.test(b.innerText || ""));
  if (!hit) return "";
  hit.click();
  return hit.href || "clicked";
})()"""


def rg_pdf(doi: str, profile_dir: str, timeout: float = 90) -> tuple[bytes | None, str, str]:
    """在已登录的浏览器里，用 DOI 搜 ResearchGate，点进条目再点下载。

    ResearchGate 上很多文献是作者自己上传的全文，别的渠道拿不到，但它的页面结构不稳，
    所以每一步都只做一次尝试：搜不到就放弃，交给后面的渠道。
    """
    import tempfile
    import time as _t
    b = _shared(profile_dir)
    tab = None
    dl_dir = ""
    try:
        dl_dir = tempfile.mkdtemp(prefix="rg_dl_")
        tab = b.open_tab("about:blank")
        tab.enable(download_dir=dl_dir)
        tab.navigate("https://www.researchgate.net/search/publication?q="
                     + __import__("urllib.parse", fromlist=["quote"]).quote(doi))
        tab.focus()
        deadline = _t.time() + timeout
        target = ""
        while _t.time() < deadline and not target:
            for _ in tab.cdp.pump(0.4):
                pass
            try:
                target = (tab.cdp.call("Runtime.evaluate",
                                       {"expression": _RG_FIND, "returnByValue": True},
                                       timeout=15).get("result", {}).get("value") or "")
            except Exception:  # noqa: BLE001
                target = ""
            _t.sleep(1.0)
        if not target:
            return None, "ResearchGate 上搜不到这个 DOI", ""
        tab.navigate(target)
        clicked = False
        while _t.time() < deadline and not clicked:
            for _ in tab.cdp.pump(0.4):
                pass
            try:
                got = (tab.cdp.call("Runtime.evaluate",
                                    {"expression": _RG_DL, "returnByValue": True},
                                    timeout=15).get("result", {}).get("value") or "")
            except Exception:  # noqa: BLE001
                got = ""
            if got:
                clicked = True
                break
            _t.sleep(1.0)
        if not clicked:
            return None, "ResearchGate 页面上没有可下载的全文", target
        # 等文件落地
        while _t.time() < deadline:
            data = _read_new_file(dl_dir)
            if data:
                return data, "", target
            _t.sleep(1.0)
        return None, "ResearchGate 点了下载但没等到文件", target
    except Exception as e:  # noqa: BLE001
        return None, f"ResearchGate 失败：{type(e).__name__}", ""
    finally:
        try:
            if tab is not None:
                b.close_tab(tab.target_id)
                tab.detach()
        except Exception:  # noqa: BLE001
            pass
