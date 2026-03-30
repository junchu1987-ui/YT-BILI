@echo off
chcp 65001 >nul
title YT-BI-Anti 全自动环境配置
setlocal enabledelayedexpansion

echo =================================================
echo   YT to Bilibili Automation - 自动环境配置脚本
echo =================================================
echo.

:: 1. 检查并安装 Git
git --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] 未检测到 Git，正在尝试自动安装...
    winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [ERROR] Git 自动安装失败，请手动安装: https://git-scm.com/
        pause
        exit /b 1
    )
    echo [OK] Git 安装请求已发送，请等待后台完成或重启脚本。
) else (
    echo [OK] Git 已就绪。
)

:: 2. 检查并安装 FFmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [INFO] 未检测到 FFmpeg，正在尝试自动安装...
    winget install -e --id FFmpeg.FFmpeg --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [ERROR] FFmpeg 自动安装失败，请手动安装并加入 PATH: https://ffmpeg.org/
        pause
        exit /b 1
    )
    echo [OK] FFmpeg 安装请求已发送。
) else (
    echo [OK] FFmpeg 已就绪。
)

:: 3. 检查并安装 Python 3.11
python --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] 未检测到 Python，正在尝试自动安装 Python 3.11...
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [ERROR] Python 自动安装失败，请手动安装: https://python.org
        pause
        exit /b 1
    )
    echo [IMPORTANT] Python 正在安装，请在安装完成后【重启此脚本】以继续配置虚拟环境。
    pause
    exit /b 0
)
for /f "tokens=2" %%v in ('python --version') do set PYVER=%%v
echo [OK] Python %PYVER% 已就绪。

:: 4. 创建虚拟环境
if not exist ".venv" (
    echo [INFO] 正在创建 Python 虚拟环境...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] 虚拟环境创建失败。
        pause
        exit /b 1
    )
    echo [OK] 虚拟环境创建成功。
) else (
    echo [OK] 虚拟环境已存在。
)

:: 5. 安装 Python 依赖包
echo [INFO] 正在安装核心依赖包 (pip)...
".venv\Scripts\pip.exe" install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] 依赖包安装失败。请检查网络。
) else (
    echo [OK] 依赖包安装成功。
)

:: 6. 检查 bin\bun.exe
if not exist "bin\bun.exe" (
    echo.
    echo [WARN] bin\bun.exe 不存在 (可选性能组件)。
    echo        强烈建议下载并放入 bin 目录以提升下载速度和稳定性。
    echo        下载地址: https://github.com/oven-sh/bun/releases (windows-x64)
    echo.
) else (
    echo [OK] bin\bun.exe 已就绪。
)

echo.
echo =================================================
echo   配置已完成！后续步骤：
echo   1. 运行: run_web.bat   (启动可视化界面)
echo   2. 访问: http://127.0.0.1:5000
echo   3. 核心工具 (Git/FFmpeg) 如果是刚安装的，可能需要重启电脑或命令行窗口才能生效。
echo =================================================
echo.
pause
