@echo off
REM Builds dist\SimConfigKeeper.exe  (needs Python 3.10+ from python.org, "Add to PATH" ticked)
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin --name SimConfigKeeper SimConfigKeeper.pyw
echo.
echo Done. Your program is in: %~dp0dist\SimConfigKeeper.exe
echo Copy it to e.g. C:\SimConfigKeeper\ on each sim and run it once.
pause
