@echo off
setlocal
echo ========================================================
echo   YT-BILI Project - One-Click Setup (Windows)
echo ========================================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.9+ first.
    pause
    exit /b 1
)

REM Step 1: Create Virtual Environment
echo [1/4] Creating virtual environment (venv)...
if not exist venv (
    python -m venv venv
    echo [SUCCESS] Virtual environment created.
) else (
    echo [SKIP] venv already exists.
)

REM Step 2: Install/Update Dependencies
echo [2/4] Installing/Updating dependencies from requirements.txt...
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Library installation failed. Please check your network.
    pause
    exit /b 1
)

REM Step 3: Initialize Configuration
echo [3/4] Initializing configuration...
if not exist config.yaml (
    copy config.yaml.example config.yaml
    echo [SUCCESS] Created config.yaml from template. Please edit it with your Keys.
) else (
    echo [SKIP] config.yaml already exists.
)

REM Step 4: Final Check
echo [4/4] Environment ready!
echo.
echo ========================================================
echo   Next Steps:
echo   1. Edit config.yaml with your Baidu/FFmpeg credentials.
echo   2. Run run_web.bat to start the application.
echo ========================================================
echo.
pause
