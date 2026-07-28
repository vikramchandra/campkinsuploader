@echo off
REM Builds a distributable folder for colleagues.
REM Run this on Windows, on the oldest Windows version you need to support.

setlocal

echo [1/5] Installing dependencies
REM The py launcher is not on every machine; fall back to the winget path.
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv 2>nul || "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m venv .venv || goto :fail
)
call .venv\Scripts\activate
python -m pip install -r requirements.txt || goto :fail

echo [2/5] Downloading Chromium into the project folder
REM Downloading it here rather than into AppData is what makes it shippable.
set PLAYWRIGHT_BROWSERS_PATH=%CD%\ms-playwright
playwright install chromium || goto :fail

echo [3/6] Building the executable
pyinstaller --noconfirm --onedir --windowed ^
  --name CampkinsBatchUploader ^
  --icon assets\icon.ico ^
  --version-file version_info.txt ^
  --add-data "app\static;app\static" ^
  --collect-all playwright ^
  --collect-all webview ^
  --hidden-import openpyxl ^
  --hidden-import multipart ^
  run.py || goto :fail

echo [4/6] Copying the browser and config alongside the executable
xcopy /E /I /Y ms-playwright dist\CampkinsBatchUploader\ms-playwright >nul
copy /Y config.example.json dist\CampkinsBatchUploader\config.example.json >nul
copy /Y README-USERS.txt dist\CampkinsBatchUploader\README.txt >nul

echo [5/6] Zipping for distribution
powershell -Command "Compress-Archive -Path dist\CampkinsBatchUploader\* -DestinationPath dist\CampkinsBatchUploader.zip -Force"

echo [6/6] Building the installer
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
  "%ISCC%" /Q installer.iss || goto :fail
  echo Installer built: dist\CampkinsUploaderSetup.exe
) else (
  echo Inno Setup not found, skipped the installer. To get it:
  echo   winget install -e --id JRSoftware.InnoSetup
)

echo.
echo Done. Send dist\CampkinsUploaderSetup.exe to your colleagues.
echo The zip in dist\ is the portable fallback.
goto :eof

:fail
echo.
echo Build failed at the step above.
exit /b 1
