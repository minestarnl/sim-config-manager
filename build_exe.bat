@echo off
REM Builds dist\SimConfigManager.exe  (needs Python 3.10+ from python.org, "Add to PATH" ticked)
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name SimConfigManager SimConfigManager.pyw
python -m PyInstaller --noconfirm --onefile --console --exclude-module PySide6 --name SimConfigManager-MCP SimConfigManager-MCP.py
echo.
echo Done. Your programs are in: %~dp0dist  (SimConfigManager.exe + SimConfigManager-MCP.exe)
echo Copy both to e.g. C:\SimConfigManager\ on each sim and run it once.
pause
