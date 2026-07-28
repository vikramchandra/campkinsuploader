@echo off
REM Double-click to run the app from source.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo The virtual environment is missing. Run these first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    echo   .venv\Scripts\python -m playwright install chromium
    pause
    exit /b 1
)
REM pythonw keeps the console window from hanging around behind the app.
start "" ".venv\Scripts\pythonw.exe" run.py
