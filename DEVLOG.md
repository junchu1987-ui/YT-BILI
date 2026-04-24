# YouTube-to-Bilibili Automation Pipeline DevLog

## v1.0 - The Foundation (Initial Prototype)
- **Core Functionality**: Base classes for YouTube scanning (`yt-dlp`), downloading, and Bilibili uploading (`biliup`).
- **Metadata Management**: Capturing titles, descriptions, and thumbnails.
- **Intro Prepending**: Basic logic to merge a fixed intro with downloaded videos.

## v1.5 - Stability & UI Evolution
- **Web UI**: Transitioned from CLI to a modern, glassmorphism-inspired Web Dashboard.
- **Cancellation System**: Implemented `_cancel_requested` global flags and `taskkill` logic to allow users to safely abort long-running downloads or transcodes.
- **Progress Visibility**: Added SSE (Server-Sent Events) to stream real-time progress bars to the browser.
- **Bug Fixes**:
    - Handled UTF-8 character issues on Windows.
    - Improved .webp to .jpg conversion for Bilibili cover compatibility.

## v1.8 - Version Control & State Persistence
- **Git Integration**: Initialized Git repository and synced with GitHub.
- **Selective Sync**: Implemented `.gitignore` to protect cookies and large media files.
- **Dynamic Meta**: Fixed UI bug where filesize labels didn't update when switching between 1080p and 4K.

## v2.0 - High-Performance Pipeline (Current)
- **Full GPU Hardware Acceleration**: 
    - Offloaded decoding, scaling, and encoding to NVENC/CUDA.
    - Switched from CPU `scale` to `scale_cuda` to eliminate memory transfer bottlenecks.
- **Resolution-Matching Architecture**: 
    - Removed 1080p caps. The pipeline now natively supports 4K-to-4K and 1080p-to-1080p processing.
- **Smart Metadata**:
    - **Dynamic Tagging**: Automatically extracts up to 10 relevant tags from YouTube video keywords.
    - **Original Content Logic**: Strictly enforces Bilibili "Original" status (`--copyright 1`) and automatically omits the source URL as per platform requirements.
- **UI & Logging Enhancements**:
    - **Persistent State**: User quality choices are now persisted to the server state immediately upon selection.
    - **Progress Parsing**: Enhanced regex-based parsing for `biliup` output to provide smooth percentage updates in the log panel.
    - **Throttled Logging**: Added 10% interval progress logs to keep the text panel clean but informative.

## v1.1.0 - Automation & Stability (Restored 04/01)
- **Multi-GPU HWAccel**: Added Intel QSV (h264_qsv) support next to NVIDIA NVENC.
- **Automated Cover Processing**:
    - **Baidu ERNIE LLM**: Summarizes titles into 1-2 words for impact.
    - **Pillow Overlay**: Renders artistic summaries on thumbnails (1920x1080).
- **Core Improvements**:
    - **Upload Robustness**: Added 3-retry upload loop for Bilibili CLI.
    - **Progress Fixes**: Fixed `\r` (carriage return) parsing for real-time `biliup` status.
- **Maintenance**: Fixed `run_web.bat` relative path issues.

---
*Maintained by Antigravity (Advanced Agentic Coding)*

---

## v3.0 计划 - Windows 可执行程序打包（待实施）

**目标：** 将 YT-BILI 打包为无需 Python 环境即可双击运行的 Windows 程序。

**选定方案：Python Embeddable + Inno Setup**

| 维度 | PyInstaller | **Embeddable + Inno Setup ✅** | MSIX |
|------|-------------|-------------------------------|------|
| 路径问题 | 必须改代码 | launcher 设置 cwd 解决 | 沙盒重定向 |
| ffmpeg 打包 | 同级目录 | 安装包内置 | 受限 |
| 防病毒误报 | 常见 | 无 | 无 |
| 卸载 | 无 | 控制面板标准卸载 | 系统管理 |
| 推荐指数 | 6/10 | **9/10** | 3/10 |

### Phase 0：代码预处理

**`web_app.py`** — 添加全局常量，修复 5 处裸路径：

```python
# 在 import 区块后添加
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
```

| 行号 | 原内容 | 改为 |
|------|--------|------|
| 22 | `os.makedirs('logs', ...)` | `os.makedirs(os.path.join(_APP_DIR, 'logs'), ...)` |
| 27 | `FileHandler("logs/web_...")` | `FileHandler(os.path.join(_APP_DIR, 'logs', f'web_...'))` |
| 65 | `CONFIG_FILE = 'config.yaml'` | `CONFIG_FILE = os.path.join(_APP_DIR, 'config.yaml')` |
| 1010 | `HISTORY_FILE = 'history.json'` | `HISTORY_FILE = os.path.join(_APP_DIR, 'history.json')` |
| 344/561/656/724 | `os.path.dirname(os.path.abspath(__file__))` | `_APP_DIR` |

`load_config()` 末尾追加注入：
```python
cfg['_app_dir'] = _APP_DIR
```

**`bili_uploader.py`** — 第 14 行：

```python
# 原
self.cookie_file = 'cookies.json'
# 改
app_dir = config.get('_app_dir', os.path.dirname(os.path.abspath(__file__)))
self.cookie_file = os.path.join(app_dir, 'cookies.json')
```

**`requirements.txt`** — 补充缺失依赖：
```
zhipuai
pystray
```

### Phase 1：打包环境

新建 `packaging/build_embeddable.bat`，步骤：
1. 下载 Python 3.13 embeddable zip
2. 修改 `python313._pth` 启用 site-packages
3. 安装 pip，然后 `pip install -r requirements.txt`
4. 复制 `.py`、`templates/`、`static/`、`assets/`、`config.yaml.example` 到 `packaging/dist/app/`

### Phase 2：Launcher

新建 `launcher.py`（~150 行）：
- `os.chdir()` 切到安装目录
- 检查 config.yaml，不存在则从 example 复制
- 检查 ffmpeg，缺失时弹 MessageBox 提示
- 后台 subprocess 启动 Flask
- 轮询 `http://127.0.0.1:5000/`，就绪后 `webbrowser.open()`
- `pystray` 系统托盘图标（打开界面 / 查看日志 / 退出）

新建 `packaging/launcher.spec`（PyInstaller 仅打包 launcher.py，~15-20 MB）

### Phase 3：安装包

新建 `packaging/ytbili_setup.iss`（Inno Setup 6）：
- 安装到 `{localappdata}\YT-BILI`（无需管理员权限）
- 内置 Python 环境、应用文件、可选 ffmpeg
- 桌面快捷方式、开始菜单、标准卸载
- 首次安装自动生成 config.yaml

最终产物：`YT-BILI-Setup-v3.0.exe`，约 120-150 MB（含 ffmpeg）。

### Phase 4：测试

| 场景 | 验证点 |
|------|--------|
| 全新 Win10 VM（无 Python） | 安装、启动、浏览器打开 |
| 安装路径含中文 | 无编码错误 |
| 完整流程 | 扫描→下载→转码→翻译→上传 |
| 托盘退出 | Flask 进程正确终止 |
| 覆盖升级 | config.yaml / cookies.json 不被覆盖 |

### 主要风险

| 风险 | 缓解措施 |
|------|---------|
| biliup asyncio 事件循环 | launcher 启动前设置 `WindowsSelectorEventLoopPolicy` |
| 中文路径编码 | 设置 `PYTHONUTF8=1` |
| ffmpeg 未配置 | launcher 检测缺失时弹窗提示 |
| pystray 图标被 Win11 隐藏 | 首次运行显示通知气泡引导固定 |
