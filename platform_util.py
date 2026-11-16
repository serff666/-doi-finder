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
