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
  "$PY" -m pip install -q -r requirements.txt 2>/dev/null \
    || echo "[setup] could not install optional packages (Excel export / PDF guessing disabled)"
fi

export DOIFINDER_PORT="${DOIFINDER_PORT:-8765}"
echo "DOI Finder → http://127.0.0.1:${DOIFINDER_PORT}/   (Ctrl+C to quit)"
exec "$PY" server.py
