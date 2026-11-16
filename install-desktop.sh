#!/usr/bin/env bash
# Adds a "DOI Finder" entry to the Linux application menu (no root needed).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/doi-finder.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=DOI Finder
Comment=Title to DOI to PDF, whole-journal download
Exec=bash -c 'cd "$HERE" && ./run.sh'
Icon=$HERE/assets/icon.png
Terminal=true
Categories=Education;Science;
DESK
echo "Installed: $HOME/.local/share/applications/doi-finder.desktop"
