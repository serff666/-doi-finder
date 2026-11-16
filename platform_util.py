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
# The server answers from an HTTP worker thread, where a Tk window is unreliable (the dialog may
# never appear). So the system dialogs are opened through PowerShell on its own STA thread. That is
# the only path: Windows only, no fallback. Cancelling returns "" / []; any failure raises.

_PS_PICK = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
# Windows refuses to let a background process take the foreground, so a dialog opened by a child
# process can end up behind the browser.  Fix: give the dialog a visible-to-the-system, top-most
# owner window and bring it to the front explicitly (an Alt key tap lifts the foreground lock).
Add-Type -Namespace DF -Name Win -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int cmd);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, int flags, System.UIntPtr extra);
'@
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true; $owner.ShowInTaskbar = $false; $owner.Opacity = 0
$owner.FormBorderStyle = 'None'; $owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.StartPosition = 'CenterScreen'; $owner.Show()
[DF.Win]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [DF.Win]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[void][DF.Win]::ShowWindow($owner.Handle, 9); [void][DF.Win]::SetForegroundWindow($owner.Handle)
$owner.Activate(); $owner.Focus() | Out-Null
if ($env:DF_KIND -eq 'folder') {
  $d = New-Object System.Windows.Forms.FolderBrowserDialog
  $d.Description = $env:DF_TITLE
  if ($env:DF_INITIAL -and (Test-Path -LiteralPath $env:DF_INITIAL)) { $d.SelectedPath = $env:DF_INITIAL }
  if ($d.ShowDialog($owner) -eq 'OK') { Write-Output $d.SelectedPath }
} else {
  $d = New-Object System.Windows.Forms.OpenFileDialog
  $d.Title = $env:DF_TITLE; $d.Filter = $env:DF_FILTER
  $d.Multiselect = ($env:DF_MULTI -eq '1')
  if ($d.ShowDialog($owner) -eq 'OK') { $d.FileNames | ForEach-Object { Write-Output $_ } }
}
$owner.Close()
"""


def _powershell_pick(kind: str, title: str, initial: str = "", filter_: str = "",
                     multi: bool = False) -> list[str]:
    env = dict(os.environ, DF_KIND=kind, DF_TITLE=title, DF_INITIAL=initial or "",
               DF_FILTER=filter_ or "所有文件|*.*", DF_MULTI="1" if multi else "0")
    r = subprocess.run(["powershell", "-STA", "-NoProfile", "-NonInteractive", "-Command", _PS_PICK],
                       capture_output=True, env=env, timeout=1800, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip() or "PowerShell failed")
    out = (r.stdout or b"").decode("utf-8", "replace")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _filter_string(patterns: tuple) -> str:
    parts = [f"{name}|{pat}" for name, pat in patterns] + ["所有文件|*.*"]
    return "|".join(parts)


def pick_folder(title: str, initial: str = "") -> str:
    """Ask the user for a folder. "" when cancelled. Windows only; raises elsewhere."""
    if not IS_WIN:
        raise RuntimeError("文件夹选择器只在 Windows 上提供，请直接输入路径")
    got = _powershell_pick("folder", title, initial)
    return got[0] if got else ""


def pick_files(title: str, patterns: tuple = (), multi: bool = False) -> list[str]:
    """Ask the user for file(s). patterns: (("PDF", "*.pdf"), ...). [] when cancelled. Windows only."""
    if not IS_WIN:
        raise RuntimeError("文件选择器只在 Windows 上提供，请直接输入路径")
    return _powershell_pick("file", title, filter_=_filter_string(patterns), multi=multi)
