@echo off
chcp 65001 >nul
title YT-BI-Anti Web UI

:: Use venv if available, otherwise use system Python
if exist ".venv\Scripts\python.exe" (
    set PYTHON=".venv\Scripts\python.exe"
) else (
    set PYTHON=python
)

set PYTHONUTF8=1

echo Starting Web UI...
echo Open your browser: http://127.0.0.1:5000
echo Press Ctrl+C to stop.
echo.

%PYTHON% web_app.py
pause
