"""Where the browser-side checks find Chrome: PATH first, then the usual install folders.

Windows runners (and most Windows machines) do not put chrome.exe on PATH, so without the folder
lookup every browser check silently skipped there."""
import os
import shutil
import subprocess
import sys
import threading
import time
import types

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


def _decode(b) -> str:
    return (b or b"").decode("utf-8", "replace")


def _run(cmd, timeout, until: bytes = b""):
    """(stdout, stderr, finished). Returns as soon as `until` has been printed, then ends Chrome itself:
    on the Windows runners Chrome prints the whole DOM and then keeps running (background tasks such as
    an RLZ ping), so waiting for it to exit never ends. Never hangs on pipes held by child processes."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    bufs = {"out": bytearray(), "err": bytearray()}

    def pump(src, key):
        fd = src.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            bufs[key] += chunk

    readers = [threading.Thread(target=pump, args=(p.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, "err"), daemon=True)]
    for t in readers:
        t.start()
    deadline = time.monotonic() + timeout
    finished = False
    while time.monotonic() < deadline:
        if p.poll() is not None:
            finished = True
            break
        if until and until in bufs["out"]:
            finished = True
            time.sleep(0.5)                      # let the last bytes arrive
            break
        time.sleep(0.1)
    if p.poll() is None:
        p.kill()
    for t in readers:
        t.join(timeout=2)
    return _decode(bytes(bufs["out"])), _decode(bytes(bufs["err"])), finished


def run_chrome(cmd: list, timeout: float):
    """Run headless Chrome for --dump-dom. On a timeout, fail with a diagnosis instead of a bare
    TimeoutExpired: Chrome's own log, the partial output, and whether a trivial page works at all."""
    cmd = list(cmd[:1]) + ["--enable-logging=stderr", "--v=0"] + list(cmd[1:])
    out, err, done = _run(cmd, timeout, until=b"</html>")
    if done:
        return types.SimpleNamespace(stdout=out, stderr=err)
    probe_out, probe_err, probe_done = _run([cmd[0], "--headless=new", "--no-sandbox", "--disable-gpu",
                                             "--dump-dom", "data:text/html,<p id=probe>ok</p>"], 45, until=b"</html>")
    probe = ("a trivial page works (" + ("dump ok" if "probe" in probe_out else "no DOM: " + probe_err[-300:]) + ")"
             if probe_done else "even a trivial page times out: Chrome headless itself is stuck here")
    raise SystemExit(f"Chrome did not finish within {timeout:.0f} s; {probe}.\n"
                     f"--- partial stdout (first 800 chars) ---\n{out[:800]}\n"
                     f"--- Chrome log (last 4000 chars) ---\n{err[-4000:]}")
