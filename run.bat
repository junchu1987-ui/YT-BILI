@echo off
set PATH=%PATH%;C:\Program Files\nodejs
echo ================================================
echo   YT to Bilibili Auto Pipeline
echo ================================================
echo.
cd /d "%~dp0"
python main.py
echo.
echo ================================================
echo   [DONE] Pipeline Execution Finished
echo ================================================
pause
