"""Windowed entry point: records a breadcrumb before/after imports so a silent
startup failure leaves evidence in %TEMP%\\doifinder_boot.log."""
import os
import sys
import time
import traceback


def _log(msg: str):
    for base in (os.environ.get("DOIFINDER_DATA"), os.environ.get("TEMP"), os.path.expanduser("~")):
        if not base:
            continue
        try:
            with open(os.path.join(base, "doifinder_boot.log"), "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
            return
        except Exception:
            continue


try:
    _log(f"boot pid={os.getpid()} exe={sys.executable} frozen={getattr(sys, 'frozen', False)}")
    _log("importing server")
    import server
    _log(f"imported; data_dir={server.DATA!r}")
    server.main()
    _log("main returned")
except BaseException:
    _log("CRASH:\n" + traceback.format_exc())
    raise
