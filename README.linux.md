# DOI Finder — Linux edition (Ubuntu 22.04+/Debian/Fedora, Python 3.11+)

```bash
sudo apt install python3 python3-venv     # Fedora: sudo dnf install python3
sudo apt install python3-gi gir1.2-webkit2-4.1   # GTK backend for the desktop window (Fedora: python3-gobject webkit2gtk4.1)
./run.sh                                  # opens the desktop window
DOIFINDER_PORT=8790 DOIFINDER_NO_BROWSER=1 ./run.sh   # custom port / server only, no window
./install-desktop.sh                      # optional: application-menu entry
```

The desktop window needs pywebview (`run.sh` installs it) and a GUI backend: GTK (above) or Qt
(`pip install "pywebview[qt]"`). Without one, the same interface opens in Chrome / Chromium or the
default browser; the file pickers then use tkinter (`sudo apt install python3-tk`).

Where things live: settings and data in `~/.local/share/DOIFinder`, downloads in
`~/Downloads/DOI Finder` (change it in Settings).

Differences from the Windows edition
| | Windows | Linux |
|---|---|---|
| Start | `DOI Finder.exe` / `run_server.cmd` | `./run.sh` |
| Window | WebView2 | GTK WebKit or Qt WebEngine |
| Data dir | `%LOCALAPPDATA%\DOIFinder` | `~/.local/share/DOIFinder` |
| Open folder | Explorer | `xdg-open` |
| Browser channel (institutional login) | Chrome or Edge | `google-chrome`, `chromium`, `microsoft-edge` on `PATH` (snap: `/snap/bin/chromium`) |
| Packaging | PyInstaller `.spec` | run from source |

Tested here: unit + regression tests, API, and an end-to-end journal flow
(`python tests/e2e_journal.py`) on Linux with Python 3.11–3.13. Not tested: real
Crossref traffic, real PDF sources, the institutional-login browser path.
