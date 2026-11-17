"""Where the browser-side checks find Chrome: PATH first, then the usual install folders.

Windows runners (and most Windows machines) do not put chrome.exe on PATH, so without the folder
lookup every browser check silently skipped there."""
import os
import shutil
import sys

# These checks print Chinese (page text, failure details). On Windows a piped stdout uses the ANSI code
# page (cp1252 on CI), which cannot encode it; write UTF-8 everywhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - not a text stream (e.g. replaced by a test runner)
        pass


# Callers run Chrome WITHOUT --user-data-dir: headless Chrome then uses its own throwaway profile.
# A fresh --user-data-dir makes Chrome do first-run work (component updates, network probes) that hung
# --dump-dom until the timeout on the Windows runners and in sandboxed containers.


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
