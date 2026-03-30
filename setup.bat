@echo off
chcp 65001 >nul
title YT-BI-Anti Setup

echo =================================================
echo   YT to Bilibili Automation - Setup Script
echo =================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ from python.org
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version') do set PYVER=%%v
echo [OK] Python %PYVER% detected.

:: Create virtual environment
if not exist ".venv" (
    echo [INFO] Creating virtual environment...
    python -m venv .venv
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment already exists.
)

:: Activate venv and install dependencies
echo [INFO] Installing dependencies...
".venv\Scripts\pip.exe" install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.

:: Check ffmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARN] ffmpeg not found in PATH.
    echo        Download from: https://ffmpeg.org/download.html
    echo        Or install via: winget install ffmpeg
    echo        After install, restart this script.
    echo.
) else (
    echo [OK] ffmpeg detected.
)

:: Check bun.exe
if not exist "bun.exe" (
    echo.
    echo [WARN] bun.exe not found.
    echo        Download bun-windows-x64.zip from: https://github.com/oven-sh/bun/releases
    echo        Extract bun.exe into this folder.
    echo.
) else (
    echo [OK] bun.exe detected.
)

:: Check cookies
if not exist "youtube_cookies.txt" (
    echo.
    echo [INFO] youtube_cookies.txt not found.
    echo        Export YouTube cookies using a browser extension (e.g., Get cookies.txt LOCALLY).
    echo        Save the file as youtube_cookies.txt in this folder.
    echo.
)

echo.
echo =================================================
echo   Setup complete! Next steps:
echo   1. Run: run_web.bat   (start the Web UI)
echo   2. Open: http://127.0.0.1:5000
echo   3. In the Web UI, go to B-Station login status
echo      and run: biliup login   (in a terminal)
echo =================================================
echo.
pause
