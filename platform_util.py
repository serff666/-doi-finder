"""Small cross-platform helpers (Windows / macOS / Linux).

Windows behaviour is unchanged: the data directory is still
%LOCALAPPDATA%\\DOIFinder and Chrome/Edge are found at their usual paths.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Windows only: keep helper processes from flashing a console window.
NO_WINDOW = 0x08000000 if IS_WIN else 0


def popen_kwargs() -> dict:
    """Extra subprocess kwargs: creationflags exists on Windows only."""
    return {"creationflags": NO_WINDOW} if IS_WIN else {}


def data_dir() -> str:
    """Per-user application data folder."""
    if IS_WIN:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DOIFinder")
    if IS_MAC:
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "DOIFinder")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "DOIFinder")


def browser_candidates() -> list[str]:
    """Chrome / Chromium / Edge executables that exist on this machine, best first."""
    out: list[str] = []
    if IS_WIN:
        lad = os.environ.get("LOCALAPPDATA", "")
        out = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
               r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
               os.path.join(lad, "Google", "Chrome", "Application", "chrome.exe"),
               r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
               r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
    elif IS_MAC:
        out = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "microsoft-edge-stable"):
            p = shutil.which(name)
            if p:
                out.append(p)
        # snap / flatpak installs live outside PATH on some desktops
        out += ["/snap/bin/chromium", "/usr/bin/chromium", "/usr/bin/google-chrome"]
    return out


def is_edge(path: str) -> bool:
    low = path.lower()
    return "msedge" in low or "microsoft-edge" in low or "microsoft edge" in low


def open_path(path: str) -> None:
    """Open a folder/file in the system file manager."""
    if IS_WIN:
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_browser_using_profile(profile_dir: str) -> None:
    """Kill leftover Chrome/Edge processes whose command line names our profile."""
    try:
        if IS_WIN:
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
                  "Where-Object { $_.CommandLine -and $_.CommandLine.Contains('%s') } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
                  % profile_dir.replace("'", "''"))
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW)
        else:
            subprocess.run(["pkill", "-f", "--", f"--user-data-dir={profile_dir}"],
                           capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def curl_bin() -> str:
    return "curl.exe" if IS_WIN else "curl"


# ---- file / folder pickers -------------------------------------------------------------
# One implementation, tkinter, on every platform; no fallback. It is the path that is known to open
# on the target machines (the PowerShell dialog returned an empty answer without showing anything).
# The owner window is a 1x1 transparent top-most window that is given the foreground *before* the
# dialog is shown (on Windows the dialog is otherwise top-most but without focus). One dialog at a time. Cancel -> "" / [];
# any failure raises, and the server reports it.
import threading  # noqa: E402

_PICK_LOCK = threading.Lock()


def _force_foreground(root) -> None:
    """Give the (tiny, invisible) owner window the keyboard focus, so the dialog opened on it does too.

    Measured on a real machine: Tk's -topmost put the dialog on top but it was not the foreground
    window (no keyboard focus). Windows' foreground lock silently ignores SetForegroundWindow from a
    background process; tapping Alt first lifts it. Best effort by design: if it fails, the dialog
    still opens, just without focus.
    """
    if not IS_WIN:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()   # Tk's real top-level window
        user32.keybd_event(0x12, 0, 0, 0)       # Alt down
        user32.keybd_event(0x12, 0, 2, 0)       # Alt up
        user32.ShowWindow(hwnd, 5)              # SW_SHOW
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def _tk_pick(kind: str, title: str, initial: str = "", patterns: tuple = (), multi: bool = False) -> list[str]:
    import tkinter as tk
    from tkinter import filedialog
    with _PICK_LOCK:
        root = tk.Tk()
        try:
            # The owner must be a real, shown window to be able to hold the foreground, so it is
            # not withdrawn: a 1x1 fully transparent top-most window instead.
            root.overrideredirect(True)
            root.geometry("1x1+0+0")
            root.attributes("-alpha", 0.0)
            root.attributes("-topmost", True)
            root.update()
            root.lift()
            root.focus_force()
            _force_foreground(root)
            if kind == "folder":
                got = filedialog.askdirectory(parent=root, title=title, initialdir=initial or None, mustexist=True)
                return [got] if got else []
            types = [(n, pat.replace(";", " ")) for n, pat in patterns] + [("所有文件", "*.*")]
            if multi:
                return list(filedialog.askopenfilenames(parent=root, title=title, filetypes=types) or ())
            got = filedialog.askopenfilename(parent=root, title=title, filetypes=types)
            return [got] if got else []
        finally:
            root.destroy()


def pick_folder(title: str, initial: str = "") -> str:
    """Ask the user for a folder. "" when cancelled; raises when no dialog can be shown."""
    got = _tk_pick("folder", title, initial)
    return got[0] if got else ""


def pick_files(title: str, patterns: tuple = (), multi: bool = False) -> list[str]:
    """Ask the user for file(s). patterns: (("PDF", "*.pdf"), ...). [] when cancelled."""
    return _tk_pick("file", title, patterns=patterns, multi=multi)
