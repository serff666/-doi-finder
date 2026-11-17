"""Where the browser-side checks find Chrome: PATH first, then the usual install folders.

Windows runners (and most Windows machines) do not put chrome.exe on PATH, so without the folder
lookup every browser check silently skipped there."""
import os
import shutil


def find_chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        hit = shutil.which(name)
        if hit:
            return hit
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    for root in filter(None, roots):
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
            p = os.path.join(root, rel)
            if os.path.isfile(p):
                return p
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return mac if os.path.isfile(mac) else ""
