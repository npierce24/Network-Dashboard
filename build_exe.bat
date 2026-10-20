@echo off
REM ============================================================
REM  Network Device Dashboard - EXE Builder
REM  Double-click this file on the Windows machine you want to
REM  build the .exe on. It installs PyInstaller (one-time, only
REM  needed for building) and packages everything into a single
REM  Network Dashboard.exe with no console window and your icon.
REM ============================================================

echo.
echo === Network Device Dashboard - EXE Builder ===
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this machine.
    echo Install it from https://www.python.org/downloads/
    echo During install, check "Add python.exe to PATH", then run this again.
    pause
    exit /b 1
)

REM Some PCs have a leftover certificate setting (for example from a
REM PostgreSQL install) pointing at a file that no longer exists, which
REM makes pip fail with "Could not find a suitable TLS CA certificate
REM bundle". Clearing these only affects this window, nothing permanent.
set SSL_CERT_FILE=
set SSL_CERT_DIR=
set REQUESTS_CA_BUNDLE=
set CURL_CA_BUNDLE=
set PIP_CERT=

python -m PyInstaller --version >nul 2>nul
if not errorlevel 1 goto have_pyinstaller

echo Installing PyInstaller - only needed on this build machine...
python -m pip install --upgrade pyinstaller --quiet
if errorlevel 1 (
    echo.
    echo Could not install PyInstaller.
    echo - Check that this PC has an internet connection.
    echo - If the error above mentions a certificate file, run this in a
    echo   Command Prompt and then try again:   pip config list
    echo   and look for a "cert" line, or check the SSL_CERT_FILE and
    echo   REQUESTS_CA_BUNDLE environment variables.
    pause
    exit /b 1
)
goto build

:have_pyinstaller
echo PyInstaller is already installed - skipping download.

:build
echo.
echo Building Network Dashboard.exe ...
echo.

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "Network Dashboard" ^
    --icon "dashboard.ico" ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --add-data "devices.json;." ^
    --add-data "dashboard.ico;." ^
    app.py

if errorlevel 1 (
    echo.
    echo Build failed - scroll up for the error.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done! Your exe is at:  dist\Network Dashboard.exe
echo.
echo   Copy "Network Dashboard.exe" anywhere you like (a folder on
echo   the server works well) and double-click it to run. It will
echo   open the dashboard in its own window automatically. A
echo   devices.json will appear next to the exe after first run -
echo   that's where your device list is saved, including any
echo   edits you make from the dashboard.
echo ============================================================
echo.
pause
