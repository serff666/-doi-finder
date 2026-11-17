#!/usr/bin/env bash
# DOI Finder — Linux/macOS launcher (Windows users: run_server.cmd or the .exe).
# First run creates ./.venv and installs openpyxl + pypdf (both optional: without them
# Excel export and PDF text guessing are simply unavailable).
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "python3 not found: sudo apt install python3 python3-venv"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11+ required"; exit 1; }

if [ ! -d .venv ]; then
  echo "[setup] creating .venv ..."
  "$PY" -m venv .venv --system-site-packages 2>/dev/null || echo "[setup] venv unavailable, using system python"
fi
[ -x .venv/bin/python ] && PY=.venv/bin/python
if ! "$PY" -c 'import openpyxl, pypdf' 2>/dev/null; then
  "$PY" -m pip install -q openpyxl pypdf 2>/dev/null \
    || echo "[setup] could not install optional packages (Excel export / PDF guessing disabled)"
fi
# the native window: pywebview plus a GUI backend (GTK via python3-gi, or Qt). Without them the UI
# opens in Chrome / Chromium or the default browser instead.
if ! "$PY" -c 'import webview' 2>/dev/null; then
  "$PY" -m pip install -q pywebview 2>/dev/null || echo "[setup] pywebview not installed: the UI opens in the browser"
fi

echo "DOI Finder starting (Ctrl+C to quit)…"
exec "$PY" desktop.py --open
