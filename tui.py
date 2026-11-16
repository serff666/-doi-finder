"""DOI Finder — terminal client.

Layout follows Claude Code: an append-only transcript that lives in the terminal's own
scrollback (`⏺ action` / `⎿ result`), and a small live region at the bottom with a status
line, a bordered prompt and a one-line hint. No full-screen mode, no dependencies.

    python tui.py            # starts the local engine in-process and opens the prompt

Paste titles (or DOIs) and press Enter. Everything else is a /command (try /help).
"""
from __future__ import annotations

import codecs
import os
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import json

IS_WIN = os.name == "nt"
VERSION = "2.1.1"

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;\"'\)\]<>]+", re.I)
ISSN_RE = re.compile(r"\d{4}-?\d{3}[\dXx]")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# --------------------------------------------------------------------------- language
def _lang() -> str:
    forced = (os.environ.get("DOIFINDER_LANG") or "").lower()
    if forced:
        return "zh" if forced.startswith("zh") else "en"
    loc = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    return "zh" if loc.startswith("zh") else ("en" if loc else ("zh" if IS_WIN else "en"))


LANG = _lang()


def T(zh: str, en: str) -> str:
    return zh if LANG == "zh" else en


# --------------------------------------------------------------------------- text metrics
def cw(ch: str) -> int:
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0 or unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(s: str) -> int:
    return sum(cw(c) for c in ANSI_RE.sub("", s))


def fit(s: str, w: int) -> str:
    """Truncate plain text to w terminal cells."""
    if w <= 0:
        return ""
    if width(s) <= w:
        return s
    out, n = [], 0
    for c in s:
        k = cw(c)
        if n + k > w - 1:
            break
        out.append(c)
        n += k
    return "".join(out) + "…"


def pad(s: str, w: int) -> str:
    return s + " " * max(0, w - width(s))


def layout_input(buf: str, pos: int, tw: int) -> tuple[list[str], int, int]:
    """Hard-wrap a single-line buffer to tw cells. Returns rows and the cursor (row, col)."""
    tw = max(4, tw)
    rows, cur, col = [], "", 0
    cur_rc = (0, 0)
    for i, c in enumerate(buf):
        if i == pos:
            cur_rc = (len(rows), col)
        k = cw(c)
        if col + k > tw:
            rows.append(cur)
            cur, col = "", 0
            if i == pos:
                cur_rc = (len(rows), 0)
        cur += c
        col += k
    if pos >= len(buf):
        if col >= tw:
            rows.append(cur)
            cur, col = "", 0
        cur_rc = (len(rows), col)
    rows.append(cur)
    return rows, cur_rc[0], cur_rc[1]


# --------------------------------------------------------------------------- styling
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
ACCENT = "38;2;215;119;87"


def sty(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if USE_COLOR else s


def accent(s): return sty(ACCENT, s)
def dim(s): return sty("2", s)
def bold(s): return sty("1", s)
def green(s): return sty("32", s)
def red(s): return sty("31", s)
def yellow(s): return sty("33", s)


# --------------------------------------------------------------------------- keys
class _Parser:
    """Turns raw text into tokens: ("text", s) | ("key", name) | ("paste", s)."""

    CTRL = {"\x03": "ctrl-c", "\x04": "ctrl-d", "\x01": "home", "\x05": "end", "\x0b": "ctrl-k",
            "\x15": "ctrl-u", "\x17": "ctrl-w", "\x0c": "ctrl-l", "\x7f": "backspace",
            "\x08": "backspace", "\t": "tab", "\r": "enter", "\n": "enter"}
    CSI = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
    TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "3": "delete"}

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, data: str, final: bool = False) -> list[tuple[str, str]]:
        self.buf += data
        toks: list[tuple[str, str]] = []
        b = self.buf
        # A multi-character chunk with a line break inside can only be a paste.
        if "\x1b" not in b and len(b) > 1 and re.search(r"[\r\n].", b, re.S):
            self.buf = ""
            return [("paste", b.replace("\r\n", "\n").replace("\r", "\n"))]
        i, text = 0, ""

        def flush() -> None:
            nonlocal text
            if text:
                toks.append(("text", text))
                text = ""

        while i < len(b):
            c = b[i]
            if c == "\x1b":
                rest = b[i:]
                if rest.startswith("\x1b[200~"):
                    end = rest.find("\x1b[201~")
                    if end < 0:
                        break  # wait for the rest of the paste
                    flush()
                    body = rest[6:end].replace("\r\n", "\n").replace("\r", "\n")
                    toks.append(("paste", body))
                    i += end + 6
                    continue
                m = re.match(r"\x1b\[([0-9;?]*)([A-Za-z~])", rest)
                if m:
                    flush()
                    params, fin = m.group(1), m.group(2)
                    key = self.TILDE.get(params.split(";")[0]) if fin == "~" else self.CSI.get(fin)
                    if key:
                        toks.append(("key", key))
                    i += m.end()
                    continue
                m = re.match(r"\x1bO([A-Za-z])", rest)
                if m:
                    flush()
                    key = self.CSI.get(m.group(1))
                    if key:
                        toks.append(("key", key))
                    i += m.end()
                    continue
                if len(rest) == 1:
                    if final:
                        flush()
                        toks.append(("key", "esc"))
                        i += 1
                    break  # maybe the start of a sequence: wait
                if rest[1] == "[" or rest[1] == "O":
                    if not final:
                        break
                i += 1  # unknown escape: drop it
                continue
            k = self.CTRL.get(c)
            if k:
                flush()
                toks.append(("key", k))
            elif c >= " ":
                text += c
            i += 1
        flush()
        self.buf = b[i:]
        return toks


class Keys:
    """Raw key reader. Unix: termios + select. Windows: msvcrt."""

    def __init__(self) -> None:
        self.parser = _Parser()
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")

    def __enter__(self):
        if IS_WIN:
            _enable_vt()
        else:
            import termios
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            new = termios.tcgetattr(self.fd)
            new[0] &= ~(termios.ICRNL | termios.IXON | termios.INLCR)
            new[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
        return self

    def __exit__(self, *exc):
        if not IS_WIN:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def read(self, timeout: float) -> list[tuple[str, str]]:
        if IS_WIN:
            return self._read_win(timeout)
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return self.parser.feed("", final=True) if self.parser.buf else []
        data = os.read(self.fd, 65536)
        if not data:
            return [("key", "eof")]
        text = self.dec.decode(data)
        toks = self.parser.feed(text)
        if self.parser.buf == "\x1b":  # lone ESC: wait briefly for a sequence
            r, _, _ = select.select([self.fd], [], [], 0.03)
            if not r:
                toks += self.parser.feed("", final=True)
        return toks

    def _read_win(self, timeout: float) -> list[tuple[str, str]]:
        import msvcrt
        end = time.time() + timeout
        while not msvcrt.kbhit():
            if time.time() >= end:
                return []
            time.sleep(0.01)
        toks: list[tuple[str, str]] = []
        chunk = ""
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                code = msvcrt.getwch()
                key = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home",
                       "O": "end", "S": "delete"}.get(code)
                if chunk:
                    toks += self.parser.feed(chunk, final=True)
                    chunk = ""
                if key:
                    toks.append(("key", key))
            else:
                chunk += ch
        if chunk:
            toks += self.parser.feed(chunk, final=True)
        return toks


def _enable_vt() -> None:
    try:
        import ctypes
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- engine access
class Api:
    """Talks to the same local HTTP engine the web UI uses, started in-process."""

    def __init__(self) -> None:
        import server
        from http.server import ThreadingHTTPServer
        self.server = server
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.srv.daemon_threads = True
        server.SERVER = self.srv
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:  # noqa: BLE001
            pass

    def _do(self, req: urllib.request.Request, timeout: float) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error") or str(e)
            except Exception:  # noqa: BLE001
                msg = str(e)
            raise RuntimeError(msg) from None
        except urllib.error.URLError as e:
            raise RuntimeError(str(e.reason)) from None

    def get(self, path: str, timeout: float = 30) -> dict:
        return self._do(urllib.request.Request(self.base + path), timeout)

    def post(self, path: str, body: dict | None = None, timeout: float = 120) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return self._do(req, timeout)


# --------------------------------------------------------------------------- pure helpers
COMMANDS = [
    ("/help", T("查看全部命令", "show all commands")),
    ("/download", T("下载 PDF（可带序号 / DOI）", "download PDFs (numbers or DOIs)")),
    ("/search", T("按主题或作者检索", "search by topic or author")),
    ("/journal", T("抓取整本期刊的文章列表", "list every article of a journal")),
    ("/results", T("重新显示上一次的列表", "show the last list again")),
    ("/retry", T("重试上次没下到的", "retry what the last download missed")),
    ("/settings", T("查看设置", "show settings")),
    ("/set", T("修改设置：/set 项 值", "change a setting: /set key value")),
    ("/dir", T("设置下载目录", "set the download folder")),
    ("/open", T("打开下载目录", "open the download folder")),
    ("/clear", T("清屏", "clear the screen")),
    ("/quit", T("退出", "quit")),
]

SETTING_KEYS = {
    "dir": "download_dir", "email": "email", "template": "template", "workers": "download_workers",
    "scihub": "scihub_enable", "insecure": "scihub_insecure_tls", "inst": "inst_enable",
    "core": "core_key", "openalex": "openalex_key", "elsevier": "elsevier_key",
    "insttoken": "elsevier_token", "wiley": "wiley_token",
}
SECRET_SETTINGS = {"core_key", "openalex_key", "elsevier_key", "elsevier_token", "wiley_token"}
BOOL_SETTINGS = {"scihub_enable", "scihub_insecure_tls", "inst_enable"}


def classify_input(text: str) -> str:
    """'download' when every non-empty line holds a DOI, otherwise 'lookup'."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "empty"
    if all(DOI_RE.search(ln) and len(ln) < 200 for ln in lines):
        return "download"
    return "lookup"


def parse_selection(args: list[str], listing: list[dict]) -> tuple[list[str], list[str]]:
    """Resolve '/download' arguments to DOIs. Numbers index `listing` (1-based, ranges ok)."""
    dois: list[str] = []
    bad: list[str] = []
    for a in args:
        m = DOI_RE.search(a)
        if m:
            dois.append(m.group(0).rstrip(".").lower())
            continue
        if a.lower() == "all":
            dois += [r["doi"] for r in listing if r.get("doi")]
            continue
        m2 = re.fullmatch(r"(\d+)(?:-(\d+))?", a)
        if m2:
            lo = int(m2.group(1))
            hi = int(m2.group(2) or lo)
            if lo < 1 or hi < lo or hi > len(listing):
                bad.append(a)
                continue
            for r in listing[lo - 1:hi]:
                if r.get("doi"):
                    dois.append(r["doi"])
            continue
        bad.append(a)
    return list(dict.fromkeys(dois)), bad


def parse_search_args(args: list[str]) -> dict:
    out: dict = {"query": "", "oa_only": False, "year_from": "", "year_to": "", "author": "", "orcid": ""}
    words: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--oa":
            out["oa_only"] = True
        elif a == "--year" and i + 1 < len(args):
            i += 1
            m = re.fullmatch(r"(\d{4})(?:-(\d{4}))?", args[i])
            if m:
                out["year_from"], out["year_to"] = m.group(1), m.group(2) or m.group(1)
        elif a == "--author" and i + 1 < len(args):
            i += 1
            out["author"] = args[i]
        elif a == "--orcid" and i + 1 < len(args):
            i += 1
            out["orcid"] = args[i]
        else:
            words.append(a)
        i += 1
    out["query"] = " ".join(words)
    return out


def parse_journal_args(args: list[str]) -> dict:
    """'/journal WIREs Computational Molecular Science 2011-2024 --all'"""
    out = {"name": "", "year_from": 0, "year_to": 0, "kind": "noncorr"}
    words: list[str] = []
    for a in args:
        m = re.fullmatch(r"(\d{4})-(\d{4})", a)
        if not words and ISSN_RE.fullmatch(a):
            words.append(a)                       # "2041-1480" is an ISSN, not a year range
        elif m and 1800 <= int(m.group(1)) <= int(m.group(2)) <= 2100:
            out["year_from"], out["year_to"] = int(m.group(1)), int(m.group(2))
        elif re.fullmatch(r"(19|20)\d{2}", a) and words:
            out["year_from"] = out["year_to"] = int(a)
        elif a in ("--all", "--research", "--review"):
            out["kind"] = {"--all": "all", "--research": "research", "--review": "review"}[a]
        else:
            words.append(a)
    out["name"] = " ".join(words)
    return out


def kind_matches(kind_sel: str, item: dict) -> bool:
    k = item.get("kind") or "research"
    if kind_sel == "research":
        return k == "research"
    if kind_sel == "review":
        return k == "review"
    if kind_sel == "noncorr":
        return k != "non-research"
    return True


def short_dir(p: str) -> str:
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if home and p.startswith(home) else p


def mark(status: str) -> str:
    return {"matched": green("✓"), "provided": green("✓"), "likely": yellow("◐"),
            "uncertain": yellow("?"), "downloaded": green("✓"), "exists": green("✓"),
            "stopped": dim("–")}.get(status, red("✗"))


def first_clause(note: str, n: int = 60) -> str:
    return fit(str(note or "").split("；")[0].split("; ")[0], n)


# --------------------------------------------------------------------------- the app
class App:
    SPIN = "·✢✳✶✻✽"

    def __init__(self, api, out=None) -> None:
        self.api = api
        self.out = out or sys.stdout
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.buf = ""
        self.pos = 0
        self.pastes: dict[str, str] = {}
        self.paste_n = 0
        self.history: list[str] = []
        self.hist_i = -1
        self.draft = ""
        self.quit = False
        self.quit_armed = 0.0
        self.job: dict | None = None
        self.cursor_up = 0
        self.dirty = True
        self.term_w = 80
        self.listing: list[dict] = []
        self.default_dois: list[str] = []
        self.layout: dict | None = None   # journal harvest: file under Journal/Year/Volume
        self.last_failed: list[str] = []
        self.dl_dir = ""
        self.last_results: list[dict] = []
        self.settings: dict = {}
        self.pending_journal: dict = {"title": "", "kind": "noncorr"}

    # ------------------------------------------------------------- terminal output
    def w(self, s: str) -> None:
        self.out.write(s)
        self.out.flush()

    def size(self) -> int:
        try:
            return max(40, shutil.get_terminal_size((80, 24)).columns)
        except Exception:  # noqa: BLE001
            return 80

    def clear_region(self) -> str:
        up = f"\x1b[{self.cursor_up}A" if self.cursor_up else ""
        return up + "\r\x1b[J"

    def emit(self, lines: list[str]) -> None:
        """Append to the transcript (above the live region)."""
        self.w(self.clear_region() + "\n".join(lines) + "\n")
        self.cursor_up = 0
        self.dirty = True
        self.draw()

    def block(self, title: str, rows: list[str] | None = None, blank: bool = True) -> list[str]:
        lines = [f"{accent('⏺')} {bold(title)}"]
        for i, r in enumerate(rows or []):
            lines.append(("  ⎿  " if i == 0 else "     ") + r)
        if blank:
            lines.append("")
        return lines

    def say(self, title: str, rows: list[str] | None = None, blank: bool = True) -> None:
        self.emit(self.block(title, rows, blank))

    def err(self, msg: str) -> None:
        self.emit(self.block(red(T("出错", "Error")), [fit(str(msg), self.term_w - 8)]))

    # ------------------------------------------------------------- live region
    def display_buf(self) -> str:
        return self.buf

    def region(self) -> tuple[list[str], int, int]:
        w = self.term_w = self.size()
        bw = w - 1
        lines: list[str] = []
        if self.job:
            j = self.job
            spin = self.SPIN[int(time.time() * 8) % len(self.SPIN)]
            el = int(time.time() - j["t0"])
            prog = f" {j['done']}/{j['total']}" if j.get("total") else ""
            cur = f" · {j['current']}" if j.get("current") else ""
            txt = f"{j['label']}{prog}{cur}"
            lines.append(accent(spin) + " " + fit(txt, bw - 24) + dim(f"  ({el}s · " + T("Esc 停止", "Esc to stop") + ")"))
            lines.append("")
        inner = bw - 4
        tw = inner - 2
        rows, cr, cc = layout_input(self.display_buf(), self.pos, tw)
        top = 0
        if len(rows) > 6:
            top = min(max(0, cr - 5), len(rows) - 6)
            rows = rows[top:top + 6]
            cr -= top
        box_top = len(lines)
        lines.append(dim("╭" + "─" * (bw - 2) + "╮"))
        for i, r in enumerate(rows):
            prompt = accent("❯") if (i == 0 and top == 0) else " "
            lines.append(dim("│") + " " + prompt + " " + pad(r, tw) + " " + dim("│"))
        lines.append(dim("╰" + "─" * (bw - 2) + "╯"))
        menu = self.menu_items()
        for name, desc in menu[:6]:
            lines.append("  " + accent(pad(name, 11)) + dim(fit(desc, bw - 16)))
        lines.append(dim("  " + fit(self.hint(), bw - 4)))
        cur_row = box_top + 1 + cr
        cur_col = 4 + cc
        return lines, cur_row, cur_col

    def hint(self) -> str:
        if self.job:
            return T("Esc 停止当前任务", "Esc stops the running job")
        if self.quit_armed and time.time() - self.quit_armed < 2:
            return T("再按一次 Ctrl+C 退出", "press Ctrl+C again to quit")
        base = T("/ 命令 · 粘贴标题或 DOI 后回车", "/ commands · paste titles or DOIs, then Enter")
        d = short_dir(self.dl_dir) if self.dl_dir else ""
        return base + (f"   ▸ {d}" if d else "")

    def menu_items(self) -> list[tuple[str, str]]:
        b = self.buf
        if self.job or not b.startswith("/") or " " in b:
            return []
        return [c for c in COMMANDS if c[0].startswith(b)]

    def draw(self) -> None:
        lines, cr, cc = self.region()
        out = [self.clear_region(), "\n".join(lines)]
        up = len(lines) - 1 - cr
        if up:
            out.append(f"\x1b[{up}A")
        out.append("\r" + (f"\x1b[{cc}C" if cc else ""))
        self.w("".join(out))
        self.cursor_up = cr
        self.dirty = False

    # ------------------------------------------------------------- input editing
    def insert(self, s: str) -> None:
        self.buf = self.buf[:self.pos] + s + self.buf[self.pos:]
        self.pos += len(s)

    def expand(self, s: str) -> str:
        for k, v in self.pastes.items():
            s = s.replace(k, v)
        return s

    def placeholder_at(self, end: int) -> str | None:
        for k in self.pastes:
            if self.buf[:end].endswith(k):
                return k
        return None

    def on_token(self, kind: str, val: str) -> None:
        self.dirty = True
        if kind == "text":
            self.insert(val)
            return
        if kind == "paste":
            if "\n" in val.strip("\n") or len(val) > 160:
                self.paste_n += 1
                n = len(val.strip("\n").splitlines())
                tag = T(f"[已粘贴 {n} 行 #{self.paste_n}]", f"[Pasted {n} lines #{self.paste_n}]")
                self.pastes[tag] = val.strip("\n")
                self.insert(tag)
            else:
                self.insert(val.strip("\n").replace("\n", " "))
            return
        k = val
        if k == "enter":
            self.submit()
        elif k == "backspace":
            ph = self.placeholder_at(self.pos)
            if ph:
                self.buf = self.buf[:self.pos - len(ph)] + self.buf[self.pos:]
                self.pos -= len(ph)
                self.pastes.pop(ph, None)
            elif self.pos:
                self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
                self.pos -= 1
        elif k == "delete":
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif k == "left":
            self.pos = max(0, self.pos - 1)
        elif k == "right":
            self.pos = min(len(self.buf), self.pos + 1)
        elif k == "home":
            self.pos = 0
        elif k == "end":
            self.pos = len(self.buf)
        elif k == "ctrl-u":
            self.buf, self.pos = self.buf[self.pos:], 0
        elif k == "ctrl-k":
            self.buf = self.buf[:self.pos]
        elif k == "ctrl-w":
            head = self.buf[:self.pos].rstrip()
            cut = max(head.rfind(" "), -1) + 1
            self.buf = self.buf[:cut] + self.buf[self.pos:]
            self.pos = cut
        elif k == "up":
            self.history_move(1)
        elif k == "down":
            self.history_move(-1)
        elif k == "tab":
            m = self.menu_items()
            if m:
                self.buf = m[0][0] + " "
                self.pos = len(self.buf)
        elif k == "esc":
            if self.job:
                self.stop_job()
            elif self.buf:
                self.reset_input()
        elif k == "ctrl-c":
            if self.job:
                self.stop_job()
            elif self.buf:
                self.reset_input()
            elif self.quit_armed and time.time() - self.quit_armed < 2:
                self.quit = True
            else:
                self.quit_armed = time.time()
        elif k in ("ctrl-d", "eof"):
            if not self.buf:
                self.quit = True
        elif k == "ctrl-l":
            self.cmd_clear()

    def reset_input(self) -> None:
        self.buf, self.pos, self.pastes, self.hist_i = "", 0, {}, -1

    def history_move(self, d: int) -> None:
        if not self.history:
            return
        if self.hist_i == -1:
            self.draft = self.buf
        ni = self.hist_i + d
        if ni < -1 or ni >= len(self.history):
            return
        self.hist_i = ni
        self.buf = self.draft if ni == -1 else self.history[-1 - ni]
        self.pos = len(self.buf)

    # ------------------------------------------------------------- submit & commands
    def submit(self) -> None:
        raw = self.buf.strip()
        text = self.expand(self.buf).strip()
        self.reset_input()
        if not text:
            return
        shown = raw  # the placeholder is what the user saw, not the pasted body
        parts = text.split()
        if len(parts) >= 3 and parts[0].lower() == "/set" and SETTING_KEYS.get(parts[1].lower()) in SECRET_SETTINGS:
            shown = " ".join(parts[:2]) + " ••••••"      # keys never reach the transcript or the history
        elif raw and "\n" not in raw and raw not in self.history:
            self.history.append(raw)
        self.emit([f"{accent('❯')} {fit(shown, self.term_w - 4)}", ""])
        if self.job:
            self.say(T("正在运行任务", "A job is running"),
                     [T("等它结束，或按 Esc 停止。", "Wait for it to finish, or press Esc to stop.")])
            return
        if text.startswith("/") and "\n" not in text:
            self.command(text)
            return
        mode = classify_input(text)
        if mode == "download":
            dois = list(dict.fromkeys(m.group(0).rstrip(".").lower() for m in DOI_RE.finditer(text)))
            self.layout = None
            self.start_download(dois)
        else:
            self.start_lookup(text)

    def command(self, text: str) -> None:
        parts = text.split()
        name, args = parts[0].lower(), parts[1:]
        fn = {
            "/help": self.cmd_help, "/?": self.cmd_help,
            "/download": self.cmd_download, "/dl": self.cmd_download,
            "/search": self.cmd_search, "/journal": self.cmd_journal,
            "/results": self.cmd_results, "/retry": self.cmd_retry,
            "/settings": self.cmd_settings, "/set": self.cmd_set, "/dir": self.cmd_dir,
            "/open": self.cmd_open, "/clear": self.cmd_clear,
            "/quit": self.cmd_quit, "/exit": self.cmd_quit,
        }.get(name)
        if not fn:
            near = [c[0] for c in COMMANDS if c[0].startswith(name)]
            self.say(T(f"没有这个命令：{name}", f"Unknown command: {name}"),
                     [T("可用：", "Try: ") + (" ".join(near) if near else "/help")])
            return
        try:
            fn(args)
        except Exception as e:  # noqa: BLE001
            self.err(f"{type(e).__name__}: {e}")

    def cmd_help(self, args=None) -> None:
        rows = [accent(pad(n, 11)) + d for n, d in COMMANDS]
        rows += ["",
                 T("直接粘贴多行标题 → 查 DOI；粘贴 DOI → 直接下载。", "Paste titles → find DOIs; paste DOIs → download."),
                 T("检索/期刊的结果带序号：/download 1 3 5-8", "Search and journal lists are numbered: /download 1 3 5-8"),
                 T("检索选项：--oa  --year 2020-2024  --author 姓名  --orcid 号", "Search options: --oa  --year 2020-2024  --author NAME  --orcid ID"),
                 T("期刊选项：/journal 期刊名或ISSN 2011-2024 [--all|--research|--review]", "Journal: /journal NAME-or-ISSN 2011-2024 [--all|--research|--review]")]
        self.say(T("命令", "Commands"), rows)

    def cmd_quit(self, args=None) -> None:
        self.quit = True

    def cmd_clear(self, args=None) -> None:
        self.w("\x1b[2J\x1b[3J\x1b[H")
        self.cursor_up = 0
        self.emit(self.welcome())

    def cmd_dir(self, args: list[str]) -> None:
        if not args:
            self.say(T("下载目录", "Download folder"), [self.dl_dir or "-"])
            return
        self.cmd_set(["dir", " ".join(args)])

    def cmd_open(self, args=None) -> None:
        self.api.post("/api/open-folder", {})
        self.say(T("已打开下载目录", "Opened the download folder"), [self.dl_dir])

    def load_settings(self) -> dict:
        self.settings = (self.api.post("/api/settings", {}) or {}).get("settings") or {}
        self.dl_dir = self.settings.get("download_dir") or self.dl_dir
        return self.settings

    def cmd_settings(self, args=None) -> None:
        s = self.load_settings()
        def yn(v): return green(T("开", "on")) if v else dim(T("关", "off"))
        def secret(k): return green(T("已设置", "set")) if s.get(k) else dim(T("未设置", "not set"))
        rows = [
            f"dir       {s.get('download_dir', '')}",
            f"email     {s.get('email') or dim('-')}",
            f"template  {s.get('template', '')}",
            f"workers   {s.get('download_workers', 3)}",
            f"scihub    {yn(s.get('scihub_enable', True))}   insecure {yn(s.get('scihub_insecure_tls'))}   inst {yn(s.get('inst_enable', True))}",
            f"keys      core {secret('core_key')} · openalex {secret('openalex_key')} · elsevier {secret('elsevier_key')} · insttoken {secret('elsevier_token')} · wiley {secret('wiley_token')}",
            "",
            dim(T("修改：/set dir ~/Papers · /set scihub off · /set workers 5 · /set core 密钥",
                  "Change: /set dir ~/Papers · /set scihub off · /set workers 5 · /set core KEY")),
        ]
        self.say(T("设置", "Settings"), rows)

    def cmd_set(self, args: list[str]) -> None:
        if len(args) < 2 or args[0].lower() not in SETTING_KEYS:
            self.say(T("用法：/set 项 值", "Usage: /set key value"),
                     [T("可设置的项：", "Keys: ") + " ".join(SETTING_KEYS)])
            return
        key = SETTING_KEYS[args[0].lower()]
        val: object = " ".join(args[1:]).strip()
        if key in BOOL_SETTINGS:
            low = str(val).lower()
            if low not in ("on", "off", "true", "false", "1", "0", "开", "关"):
                self.say(T("这一项只能是 on 或 off", "This setting takes on or off"))
                return
            val = low in ("on", "true", "1", "开")
        elif key == "download_workers":
            if not str(val).isdigit() or not 1 <= int(val) <= 8:
                self.say(T("workers 要在 1 到 8 之间", "workers must be 1–8"))
                return
            val = int(val)
        elif key == "download_dir":
            val = os.path.expanduser(str(val))
        self.api.post("/api/settings", {"save": True, "settings": {key: val}})
        self.load_settings()
        shown = T("已更新", "updated") if key in SECRET_SETTINGS else str(val)
        self.say(T(f"已保存 {args[0]}", f"Saved {args[0]}"), [shown])

    # ------------------------------------------------------------- jobs
    def begin(self, kind: str, jid: str, label: str, total: int = 0) -> None:
        self.job = {"kind": kind, "id": jid, "label": label, "t0": time.time(),
                    "done": 0, "total": total, "current": "", "stopping": False}
        self.dirty = True
        threading.Thread(target=self._poll, args=(self.job,), daemon=True).start()

    def _poll(self, job: dict) -> None:
        d: dict = {}
        fails = 0
        while True:
            try:
                d = self.api.get(f"/api/job/{job['id']}")
                fails = 0
            except Exception as e:  # noqa: BLE001
                fails += 1
                if fails > 5:
                    d = {"state": "error", "error": str(e)}
                    break
                time.sleep(0.6)
                continue
            job["done"] = d.get("done", 0)
            job["total"] = d.get("total", 0) or job.get("total", 0)
            cur = d.get("current") or d.get("markdown") or ""
            job["current"] = cur
            if d.get("state") in ("done", "stopped", "error"):
                break
            time.sleep(0.4)
        self.q.put(("finish", job, d))

    def async_task(self, label: str, work, done) -> None:
        """Run a slow call off the UI thread; `done(result)` runs back on it."""
        job = {"kind": "task", "id": "", "label": label, "t0": time.time(), "done": 0, "total": 0,
               "current": "", "stopping": False}
        self.job = job
        self.dirty = True

        def run() -> None:
            try:
                self.q.put(("task", job, done, work(), None))
            except Exception as e:  # noqa: BLE001
                self.q.put(("task", job, done, None, e))
        threading.Thread(target=run, daemon=True).start()

    def stop_job(self) -> None:
        j = self.job
        if not j or j.get("stopping"):
            return
        if j["kind"] == "task":
            j["stopping"] = True   # the request can't be recalled; its result is dropped
            j["current"] = T("已放弃", "abandoned")
            return
        j["stopping"] = True
        j["current"] = T("正在停止…", "stopping…")
        try:
            self.api.post(f"/api/stop/{j['id']}", {})
        except Exception:  # noqa: BLE001
            pass

    def drain(self) -> None:
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            if item[0] == "finish":
                _, job, d = item
                self.job = None
                try:
                    self.finish(job, d)
                except Exception as e:  # noqa: BLE001
                    self.err(f"{type(e).__name__}: {e}")
            elif item[0] == "task":
                _, job, done, result, exc = item
                if self.job is job:
                    self.job = None
                if job.get("stopping"):
                    self.emit(self.block(T("已停止", "Stopped")))
                elif exc is not None:
                    self.err(exc)
                else:
                    try:
                        done(result)
                    except Exception as e:  # noqa: BLE001
                        self.err(f"{type(e).__name__}: {e}")

    def finish(self, job: dict, d: dict) -> None:
        if d.get("state") == "error":
            self.err(d.get("error") or T("任务失败", "job failed"))
            return
        {"lookup": self.finish_lookup, "download": self.finish_download,
         "harvest": self.finish_harvest}[job["kind"]](job, d)

    # --- lookup
    def start_lookup(self, text: str) -> None:
        s = self.settings or self.load_settings()
        r = self.api.post("/api/lookup", {"text": text, "mode": "auto", "mailto": s.get("email", "")})
        self.begin("lookup", r["job_id"], T("查找 DOI", "Finding DOIs"), r.get("total", 0))

    def finish_lookup(self, job: dict, d: dict) -> None:
        res = d.get("results") or []
        self.last_results = res
        counts: dict[str, int] = {}
        for r in res:
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        ok = counts.get("matched", 0) + counts.get("provided", 0)
        lik = counts.get("likely", 0)
        unc = counts.get("uncertain", 0)
        bad = sum(v for k, v in counts.items() if k not in ("matched", "provided", "likely", "uncertain"))
        self.listing = [{"doi": r.get("doi", ""), "title": r.get("input") or r.get("title") or "",
                         "status": r.get("status"), "score": r.get("score", 0), "year": r.get("year"),
                         "note": r.get("note", "")} for r in res]
        self.default_dois = [x["doi"] for x in self.listing
                             if x["doi"] and x["status"] in ("matched", "provided", "likely")]
        self.layout = None
        head = " · ".join(filter(None, [
            green(f"✓ {ok}") if ok else "", yellow(f"◐ {lik}") if lik else "",
            yellow(f"? {unc}") if unc else "", red(f"✗ {bad}") if bad else ""])) or T("没有结果", "no results")
        if d.get("state") == "stopped":
            head += dim(T("   （已停止，结果不全）", "   (stopped, partial)"))
        rows = [head] + self.list_rows()
        tail = []
        if self.default_dois:
            tail.append(dim(T(f"/download 下载其中 {len(self.default_dois)} 个已匹配的；/download all 连同需核对的",
                              f"/download fetches the {len(self.default_dois)} matched ones; /download all includes the uncertain")))
        self.emit(self.block(T("标题 → DOI", "Title → DOI") + dim(f"  {len(res)}"), rows + tail))

    def list_rows(self, limit: int = 40) -> list[str]:
        avail = self.term_w - 1 - 5          # transcript indent ("  ⎿  ")
        rows = []
        for i, x in enumerate(self.listing[:limit], 1):
            doi = x.get("doi") or ""
            extra = ""
            if x.get("status") in ("uncertain", "likely") and x.get("score"):
                extra += f" ({x['score']:.2f})"
            if x.get("oa"):
                extra += " OA"
            if "status" in x:
                mk = mark(x["status"] or "matched")
            elif "_sel" in x:
                mk = accent("●") if x["_sel"] else dim("○")
            else:
                mk = dim("·")
            yr = (f"{x['year']} " if x.get("year") else "") + (f"{x['vi']} " if x.get("vi") else "")
            fixed = 3 + 2 + (len(doi) + 2 if doi else 0) + width(extra)
            title = fit(f"{yr}{x.get('title') or ''}", max(12, avail - fixed))
            extra_s = extra.replace(" OA", green(" OA")) if " OA" in extra else extra
            if "(" in extra_s:
                extra_s = dim(extra_s) if " OA" not in extra else extra_s
            rows.append(f"{dim(pad(str(i), 3))}{mk} {title}" + (f"  {dim(doi)}" if doi else "") + extra_s)
        if len(self.listing) > limit:
            rows.append(dim(T(f"… 还有 {len(self.listing) - limit} 条（/results all 全部显示）",
                              f"… {len(self.listing) - limit} more (/results all shows everything)")))
        return rows

    def cmd_results(self, args: list[str]) -> None:
        if not self.listing:
            self.say(T("还没有列表", "No list yet"), [T("先粘贴标题，或用 /search、/journal。", "Paste titles, or use /search or /journal.")])
            return
        self.say(T("上一次的列表", "Last list"), self.list_rows(10 ** 6 if args and args[0] == "all" else 40))

    # --- download
    def cmd_download(self, args: list[str]) -> None:
        if args:
            dois, bad = parse_selection(args, self.listing)
            if bad:
                self.say(T("看不懂这些参数", "Can't read these"), [" ".join(bad), T("用序号（1 3 5-8）、all 或 DOI", "Use numbers (1 3 5-8), all or DOIs")])
                return
            keep_layout = bool(self.layout) and all(not DOI_RE.search(a) for a in args)
        else:
            dois, keep_layout = list(self.default_dois), bool(self.layout)
        if not dois:
            self.say(T("没有可下载的 DOI", "Nothing to download"),
                     [T("先粘贴标题，或用 /search、/journal 得到列表。", "Paste titles, or get a list with /search or /journal.")])
            return
        if not keep_layout:
            self.layout = None
        self.start_download(dois)

    def start_download(self, dois: list[str]) -> None:
        self.load_settings()
        body: dict = {"dois": dois}
        if self.layout:
            want = set(dois)
            body["layout"] = {"journal": self.layout["journal"],
                              "items": [it for it in self.layout["items"] if (it.get("doi") or "").lower() in want]}
        r = self.api.post("/api/download", body)
        self.begin("download", r["job_id"], T("下载 PDF", "Downloading PDFs"), r.get("total", len(dois)))

    def finish_download(self, job: dict, d: dict) -> None:
        res = d.get("results") or []
        ok = [r for r in res if r.get("status") in ("downloaded", "exists")]
        miss = [r for r in res if r.get("status") not in ("downloaded", "exists", "stopped")]
        stopped = [r for r in res if r.get("status") == "stopped"]
        self.last_failed = [r["doi"] for r in miss + stopped if r.get("doi")]
        head = " · ".join(filter(None, [green(f"✓ {len(ok)}") if ok else "",
                                        red(f"✗ {len(miss)}") if miss else "",
                                        dim(f"– {len(stopped)}") if stopped else ""])) or T("没有结果", "no results")
        rows = [head]
        srcs: dict[str, int] = {}
        for r in ok:
            srcs[r.get("source") or "?"] = srcs.get(r.get("source") or "?", 0) + 1
        if srcs:
            rows.append(dim(T("来源 ", "via ") + " · ".join(f"{k} {v}" for k, v in sorted(srcs.items(), key=lambda x: -x[1]))))
        w = self.term_w - 8
        for r in miss[:25]:
            rows.append(f"{red('✗')} {r.get('doi', '')}  {dim(first_clause(r.get('note'), max(10, w - len(r.get('doi', '')) - 6)))}")
        if len(miss) > 25:
            rows.append(dim(T(f"… 另有 {len(miss) - 25} 个没下到", f"… {len(miss) - 25} more missed")))
        if d.get("dest"):
            rows.append(dim(T("保存在 ", "saved to ") + short_dir(d["dest"])))
        if self.last_failed:
            rows.append(dim(T("/retry 重试没下到的 · /open 打开目录", "/retry tries the missed ones · /open opens the folder")))
        else:
            rows.append(dim(T("/open 打开目录", "/open opens the folder")))
        self.emit(self.block(T("下载 PDF", "Download PDFs") + dim(f"  {len(res)}"), rows))

    def cmd_retry(self, args=None) -> None:
        if not self.last_failed:
            self.say(T("没有需要重试的", "Nothing to retry"))
            return
        self.start_download(list(self.last_failed))

    # --- search
    def cmd_search(self, args: list[str]) -> None:
        a = parse_search_args(args)
        if not (a["query"] or a["author"] or a["orcid"]):
            self.say(T("用法：/search 关键词 [--oa] [--year 2020-2024] [--author 姓名]",
                       "Usage: /search WORDS [--oa] [--year 2020-2024] [--author NAME]"))
            return
        a["email"] = (self.settings or self.load_settings()).get("email", "")
        a["limit"] = 40
        self.async_task(T("检索中", "Searching"), lambda: self.api.post("/api/search", a, timeout=90),
                        lambda r: self.show_search(a, r))

    def show_search(self, a: dict, r: dict) -> None:
        works = r.get("works") or []
        self.listing = [{"doi": w.get("doi") or "", "title": w.get("title") or "", "year": w.get("year"),
                         "oa": bool(w.get("oa"))} for w in works]
        self.default_dois = [x["doi"] for x in self.listing if x["doi"]]
        self.layout = None
        rows = [dim(T(f"{len(works)} 条 · 来源 {r.get('source', '')}", f"{len(works)} results · {r.get('source', '')}"))]
        rows += self.list_rows()
        if r.get("note"):
            rows.append(yellow(first_clause(r["note"], self.term_w - 8)))
        rows.append(dim(T("/download 1 3 5-8 下载选中的；/download 下载全部", "/download 1 3 5-8 fetches those; /download fetches all")))
        self.emit(self.block(T("检索", "Search") + dim(f"  {a['query'] or a['author'] or a['orcid']}"), rows))

    # --- journal
    def cmd_journal(self, args: list[str]) -> None:
        a = parse_journal_args(args)
        if not a["name"]:
            self.say(T("用法：/journal 期刊名或 ISSN [2011-2024] [--all|--research|--review]",
                       "Usage: /journal NAME-or-ISSN [2011-2024] [--all|--research|--review]"))
            return
        self.pending_journal = {"title": a["name"], "kind": a["kind"]}
        if ISSN_RE.fullmatch(a["name"].strip()):
            self.start_harvest(a["name"].strip(), a["name"].strip(), a)
            return

        def work():
            return self.api.post("/api/journal/resolve", {"names": [a["name"]]}, timeout=60)

        def done(r):
            hit = (r.get("resolved") or [None])[0]
            if not hit or not hit.get("issn"):
                self.err(T("没找到这本期刊，换个写法或直接给 ISSN。", "Journal not found; try another spelling or give the ISSN."))
                return
            self.start_harvest(hit["issn"], hit.get("title") or a["name"], a)
        self.async_task(T("解析期刊名", "Resolving journal"), work, done)

    def start_harvest(self, issn: str, title: str, a: dict) -> None:
        self.pending_journal = {"title": title, "kind": a["kind"]}
        r = self.api.post("/api/journal/harvest", {"issn": issn, "year_from": a["year_from"], "year_to": a["year_to"]})
        self.begin("harvest", r["job_id"], T(f"抓取 {fit(title, 40)}", f"Fetching {fit(title, 40)}"), 0)

    def finish_harvest(self, job: dict, d: dict) -> None:
        items = [x for x in (d.get("items") or []) if x.get("doi")]
        info = getattr(self, "pending_journal", {"title": "", "kind": "noncorr"})
        if not items:
            self.err(d.get("error") or T("没有抓到文章", "no articles found"))
            return
        self.layout = {"journal": info["title"], "items": items}
        self.listing = [{"doi": x["doi"], "title": x.get("title") or "", "year": x.get("year")} for x in items]
        for x, it in zip(self.listing, items):
            x["_sel"] = kind_matches(info["kind"], it)
            x["vi"] = " ".join(filter(None, [f"v{it.get('volume')}" if it.get("volume") else "", f"#{it.get('issue')}" if it.get("issue") else ""]))
        self.default_dois = [x["doi"] for x in self.listing if x["_sel"]]
        years = sorted({str(x.get("year")) for x in items if x.get("year")})
        kinds: dict[str, int] = {}
        for it in items:
            kinds[it.get("kind") or "research"] = kinds.get(it.get("kind") or "research", 0) + 1
        names = {"research": T("论文", "research"), "review": T("综述", "review"), "non-research": T("更正/社论等", "corrections etc.")}
        rows = [T(f"共 {len(items)} 篇", f"{len(items)} articles") + (f" · {years[0]}–{years[-1]}" if years else "")
                + "  " + dim(" · ".join(f"{names.get(k, k)} {v}" for k, v in kinds.items()))]
        rows += self.list_rows(12)
        rows.append(dim(T(f"/download 下载其中 {len(self.default_dois)} 篇（已排除更正/社论），按 刊名/年/卷期 分文件夹；/download all 全部；/download 1-20 指定",
                          f"/download fetches {len(self.default_dois)} (corrections excluded) into Journal/Year/Volume folders; /download all for everything")))
        self.emit(self.block(T("期刊", "Journal") + dim(f"  {info['title']}"), rows))

    # ------------------------------------------------------------- frame
    def welcome(self) -> list[str]:
        w = min(self.size() - 1, 74)
        inner = w - 4

        def row(s: str) -> str:
            return dim("│") + " " + pad(s, inner) + " " + dim("│")
        return [dim("╭" + "─" * (w - 2) + "╮"),
                row(accent("✻") + " " + bold("DOI Finder") + dim(f"  v{VERSION}")),
                row(dim(T("标题 → DOI → PDF · 终端版", "title → DOI → PDF · terminal edition"))),
                row(""),
                row(T("粘贴标题 → 查 DOI；粘贴 DOI → 下载 PDF", "paste titles → find DOIs; paste DOIs → download")),
                row(dim(T("/help 查看全部命令", "/help lists every command"))),
                dim("╰" + "─" * (w - 2) + "╯"), ""]

    def run(self, keys) -> None:
        self.w("\x1b[?2004h")  # bracketed paste
        try:
            self.load_settings()
        except Exception as e:  # noqa: BLE001
            self.dl_dir = ""
            self.emit([red(f"settings: {e}")])
        self.emit(self.welcome())
        last_spin = 0.0
        try:
            while not self.quit:
                for kind, val in keys.read(0.08):
                    self.on_token(kind, val)
                self.drain()
                now = time.time()
                if self.job and now - last_spin > 0.12:
                    self.dirty, last_spin = True, now
                if self.quit_armed and now - self.quit_armed > 2:
                    self.quit_armed, self.dirty = 0.0, True
                if self.size() != self.term_w:
                    self.dirty = True
                if self.dirty:
                    self.draw()
        finally:
            self.w(self.clear_region() + "\x1b[?2004l")
            self.cursor_up = 0


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("tui.py needs an interactive terminal. For scripts use cli.py.", file=sys.stderr)
        return 2
    if IS_WIN:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    api = Api()
    app = App(api)
    try:
        with Keys() as keys:
            app.run(keys)
    finally:
        api.close()
    print(dim(T("再见。", "Bye.")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
