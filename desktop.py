"""DOI Finder as a desktop app: the same local server, shown in its own native window.

    python desktop.py            (the Windows build starts here too)

The window is a system web view (pywebview: WebView2 on Windows, WebKit on macOS, GTK/Qt on Linux), so
the app has its own taskbar entry and icon, no address bar or browser tabs, and native dialogs:

  * file / folder pickers open on the app window (platform_util.set_picker), not through tkinter;
  * exports are written through a native "Save as" dialog (window.pywebview.api.save_file);
  * PDFs dragged onto the window arrive with their real paths, so they can be archived directly;
  * closing the window while a job runs asks first; closing it stops the server;
  * size and position are remembered (<data dir>/window.json);
  * a second launch brings the running window forward instead of starting another server.

Without pywebview (or without a GUI backend for it) this falls back to server.main()'s behaviour:
a Chrome / Edge --app window or the default browser.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import urllib.request

import platform_util
import server

TITLE = "DOI Finder"
MIN_SIZE = (1040, 680)
DEFAULT_SIZE = (1380, 900)


# ---- window geometry -----------------------------------------------------------------------------
def _state_path() -> str:
    return os.path.join(server.DATA or server._temp_safe(), "window.json")


def load_geometry(path: str | None = None) -> dict:
    """Last size / position, sanitised: never smaller than MIN_SIZE, never parked off-screen."""
    out = {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1], "x": None, "y": None}
    try:
        with open(path or _state_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(raw, dict):
        return out
    for k, lo in (("width", MIN_SIZE[0]), ("height", MIN_SIZE[1])):
        v = raw.get(k)
        if isinstance(v, int) and lo <= v <= 10000:
            out[k] = v
    x, y = raw.get("x"), raw.get("y")
    # Windows reports -32000 for a minimised window; anything far off is not worth restoring
    if isinstance(x, int) and isinstance(y, int) and -200 <= x <= 20000 and 0 <= y <= 20000:
        out["x"], out["y"] = x, y
    return out


def save_geometry(win, path: str | None = None) -> None:
    try:
        geo = {"width": int(win.width), "height": int(win.height), "x": int(win.x), "y": int(win.y)}
    except Exception:  # noqa: BLE001 - the window may already be gone
        return
    if geo["x"] <= -10000 or geo["width"] < 200:           # minimised: keep the previous record
        return
    try:
        with open(path or _state_path(), "w", encoding="utf-8") as fh:
            json.dump(geo, fh)
    except Exception:  # noqa: BLE001
        pass


# ---- dialogs ------------------------------------------------------------------------------------
_EXT = re.compile(r"\*(?:\.(?:\w+|\*))?")


def file_filter(patterns) -> list[str]:
    """(("PDF / CAJ", "*.pdf;*.caj"),) -> ["PDF CAJ (*.pdf;*.caj)", "All files (*.*)"].

    pywebview only accepts 'Description (*.a;*.b)' with a description of word characters and spaces.
    """
    out = []
    for name, pat in patterns or ():
        exts = [p.strip() for p in re.split(r"[;\s]+", str(pat)) if p.strip()]
        exts = [p for p in exts if _EXT.fullmatch(p)]
        if not exts:
            continue
        desc = re.sub(r"[^\w ]+", " ", str(name))
        desc = re.sub(r"\s+", " ", desc).strip() or "Files"
        out.append(f"{desc} ({';'.join(exts)})")
    out.append("All files (*.*)")
    return out


def safe_filename(name: str, fallback: str = "export") -> str:
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .")
    return name[:150] or fallback


class Shell:
    """Owns the native window; everything the server or the page needs from it goes through here."""

    def __init__(self, webview):
        self.wv = webview
        self.window = None
        self._dialog = threading.Lock()                  # one dialog at a time, like the tkinter path
        self.saved: set[str] = set()

    # file / folder pickers for platform_util (called from HTTP handler threads)
    def pick(self, kind: str, title: str, initial: str = "", patterns: tuple = (), multi: bool = False) -> list[str]:
        wv = self.wv
        with self._dialog:
            if kind == "folder":
                got = self.window.create_file_dialog(wv.FileDialog.FOLDER,
                                                     directory=initial if initial and os.path.isdir(initial) else "")
            else:
                got = self.window.create_file_dialog(wv.FileDialog.OPEN, allow_multiple=bool(multi),
                                                     file_types=tuple(file_filter(patterns)))
        if not got:
            return []
        if isinstance(got, str):
            got = [got]
        return [str(x) for x in got if x]

    def save_as(self, name: str, data: bytes) -> str:
        wv = self.wv
        start = server.load_settings().get("download_dir") or ""
        with self._dialog:
            got = self.window.create_file_dialog(wv.FileDialog.SAVE, save_filename=safe_filename(name),
                                                 directory=start if os.path.isdir(start) else "")
        if not got:
            return ""
        path = str(got[0] if isinstance(got, (list, tuple)) else got)
        with open(path, "wb") as fh:
            fh.write(data)
        self.saved.add(os.path.abspath(path))
        return path

    def focus(self) -> None:
        w = self.window
        w.restore()
        w.show()
        w.on_top = True                                  # lifts the window above others, then lets go
        w.on_top = False

    def quit(self) -> None:
        if self.window is not None:
            self.window.destroy()

    # window events
    def on_closing(self):
        save_geometry(self.window)
        jobs = server.running_jobs()
        if not jobs:
            return True
        n = len(jobs)
        msg = (f"还有 {n} 个任务在运行，退出会中断它们（已下载的文件和历史记录都会保留）。仍然退出？\n\n"
               f"{n} task(s) still running. Quitting stops them (downloaded files and history are kept). Quit anyway?")
        try:
            return bool(self.window.create_confirmation_dialog(TITLE, msg))
        except Exception:  # noqa: BLE001 - no dialog: do not trap the user in the window
            return True

    def on_closed(self):
        threading.Thread(target=server.shutdown, daemon=True).start()

    def on_loaded(self):
        try:
            from webview.dom import DOMEventHandler
            doc = self.window.dom.document
            doc.events.dragover += DOMEventHandler(lambda e: None, prevent_default=True)
            doc.events.drop += DOMEventHandler(self.on_drop, prevent_default=True)
        except Exception as e:  # noqa: BLE001 - drag and drop is a convenience
            server.log(f"drop handler not installed: {e}")

    def on_drop(self, e) -> None:
        files = ((e or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if isinstance(f, dict) and f.get("pywebviewFullPath")]
        if paths:
            self.window.evaluate_js("document.dispatchEvent(new CustomEvent('doifinder:drop-paths', {detail: %s}))"
                                    % json.dumps(paths))


class Api:
    """Exposed to the page as window.pywebview.api (only public methods are)."""

    def __init__(self, shell: Shell):
        self._shell = shell

    def info(self) -> dict:
        return {"shell": "desktop", "platform": sys.platform, "version": server.VERSION}

    def save_file(self, name: str, b64: str) -> dict:
        try:
            data = base64.b64decode(b64 or "", validate=False)
            return {"path": self._shell.save_as(name, data)}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def reveal(self, path: str) -> dict:
        """Open the folder of a file this window saved (nothing else)."""
        p = os.path.abspath(str(path or ""))
        if p not in self._shell.saved:
            return {"error": "not a file saved here"}
        try:
            platform_util.open_path(os.path.dirname(p))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def quit(self) -> None:
        self._shell.quit()


def _focus_existing(url: str) -> bool:
    try:
        req = urllib.request.Request(url + "api/app/focus", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    if os.environ.get("DOIFINDER_NO_BROWSER"):           # headless: just the server
        return server.main()
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        server.log(f"pywebview unavailable ({e}); falling back to a browser window")
        if "--open" not in sys.argv:
            sys.argv.append("--open")
        return server.main()

    env_port = int(os.environ.get("DOIFINDER_PORT") or 0)
    if not env_port:
        existing = server.already_running()
        if existing:
            server.log(f"already running at {existing}")
            if not _focus_existing(existing):
                server.show_ui(existing)
            return

    server.SHELL = "desktop"
    url = server.start(env_port)
    http = threading.Thread(target=server.serve, name="http", daemon=True)
    http.start()

    shell = Shell(webview)
    geo = load_geometry()
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:  # noqa: BLE001
        pass
    win = webview.create_window(TITLE, url, js_api=Api(shell), width=geo["width"], height=geo["height"],
                                x=geo["x"], y=geo["y"], min_size=MIN_SIZE, text_select=True,
                                background_color="#F4F6FA")
    shell.window = win
    platform_util.set_picker(shell.pick)
    server.APP_HOOKS.update(focus=shell.focus, quit=shell.quit)
    win.events.closing += shell.on_closing
    win.events.closed += shell.on_closed
    win.events.loaded += shell.on_loaded

    storage = os.path.join(server.DATA or server._temp_safe(), "webview")
    icon = os.path.join(server.RES, "assets", "icon.png")
    try:
        webview.start(private_mode=False, storage_path=storage, icon=icon if os.path.isfile(icon) else None,
                      debug=bool(os.environ.get("DOIFINDER_DEBUG")))
    except Exception as e:  # noqa: BLE001 - e.g. no GUI backend on this Linux box
        server.log(f"native window failed ({e}); falling back to a browser window")
        platform_util.set_picker(None)
        server.APP_HOOKS.clear()
        server.SHELL = "browser"
        server.show_ui(url)
        http.join()
        return
    server.shutdown()
    http.join(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a windowed build has nowhere to print a traceback
        import time
        import traceback
        try:
            with open(os.path.join(server.DATA or server._temp_safe(), "crash.log"), "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + traceback.format_exc() + "\n")
        except Exception:  # noqa: BLE001
            pass
        raise
