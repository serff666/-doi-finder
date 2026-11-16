@echo off
cd /d "%~dp0"
echo Starting DOI Finder...
python server.py --open
pause
