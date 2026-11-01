@echo off
REM Run without building an .exe (needs Python 3.10+ installed)
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
start "" pythonw SimConfigManager.pyw
