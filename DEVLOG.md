# YT_BI_Anti 项目开发日志 (DEVLOG)

> 本文件为项目长期记忆文件，记录所有关键决策、重要修改和历史版本信息。
> 每次对话/操作后更新本文件。

---

## 项目概述

**目标**: 自动抓取 YouTube 频道最新视频 → 添加片头 → 上传至 Bilibili  
**触发方式**: Windows 本地手动双击 `run.bat`（按需触发，非定时任务）  
**语言**: Python 3.13  

---

## 技术栈

| 组件 | 工具 | 版本/备注 |
|------|------|-----------|
| 视频下载 | yt-dlp (Nightly) | `nightly@2026.03.21` |
| JS 挑战解密 | Bun (本地) | v1.3.11，位于项目根目录 `bun.exe` |
| EJS 脚本分发 | yt-dlp-ejs | v0.8.0，pip 安装 |  
| 视频处理 | ffmpeg | v8.1 |
| B站上传 | biliup CLI | 需 `cookies.json` |
| 配置管理 | PyYAML | `config.yaml` |

---

## 目录结构

```
YT_BI_Anti/
├── main.py              # 入口：初始化Bun路径，调度pipeline
├── yt_downloader.py     # YouTube下载模块（yt-dlp Python API）
├── video_processor.py   # 视频处理：片头拼接（ffmpeg）
├── bili_uploader.py     # B站上传（biliup CLI）
├── config.yaml          # 主配置文件
├── run.bat              # Windows 双击启动脚本
├── bun.exe              # Bun JS运行时（115MB，本地）
├── deno.exe             # Deno JS运行时（128MB，本地，目前无效）
├── youtube_cookies.txt  # YouTube登录cookies（Netscape格式）
├── cookies.json         # Bilibili登录状态（biliup login生成）
├── data/                # 下载数据目录
│   └── {video_id}/      # 每个视频独立子目录
│       ├── {id}.mp4
│       ├── {id}.webp    # 封面图
│       └── {id}_final.mp4  # 处理后视频
├── assets/              # 片头视频等：assets/intro.mp4
├── DEVLOG.md            # 本文件
└── requirements.txt
```

---

## 关键配置 (config.yaml)

```yaml
app:
  work_dir: ./data
  proxy: http://127.0.0.1:10808  # 代理（根据实际填写）

youtube:
  channel_urls:
    - https://www.youtube.com/@shiroh73  # 可填多个频道
  max_downloads_per_run: 5  # 仅用于备用，主逻辑为每频道1个

ffmpeg:
  bin_path: ffmpeg
  intro_video_path: ./assets/intro.mp4

bilibili:
  tid: 17          # 分区ID
  desc_prefix: "本视频搬运自YouTube。\n原视频链接：{youtube_url}\n\n"
```

---

## 核心问题记录与解决方案

### ❌ 问题1：YouTube n-Challenge 签名解密失败

**症状**: `n challenge solving failed`, `Requested format is not available`  
**根本原因**: yt-dlp 需要一个 JavaScript 运行时来解密 YouTube 动态生成的 signature n 参数。  
**尝试的失败方案**:
- 注入 `deno.exe` 到 PATH → 失败（deno.exe 在此系统无法执行，`--version` 无输出）
- 猴子补丁 `yt_dlp.jsinterp.JS_EXECUTABLES` → 失败（该属性不存在于此版本）
- `--js-runtime bun /path/to/bun` CLI 参数 → 失败（Windows 路径含反斜杠时被错误解析）
- `player_client=android,ios`（绕过 web 端检测）→ 失败（这些客户端不支持 cookies）

**✅ 最终解决方案**: 使用 yt-dlp Python API 的 `js_runtimes` 参数，直接传入 `bun.exe` 绝对路径：
```python
'js_runtimes': {'bun': {'path': 'E:\\DevProject\\YT_BI_Anti\\bun.exe'}}
```
确认工作日志：`[youtube] [jsc:bun] Solving JS challenges using bun`

**已安装辅助包**:
- `pip install yt-dlp-ejs` (v0.8.0) — 提供 JS solver 脚本分发

---

### ❌ 问题2：Bun 自动检测失败

**症状**: `[debug] JS runtimes: none`, `bun (unavailable)` — 即使 `bun.exe` 在 cwd  
**根本原因**: yt-dlp 的 `_find_exe()` 在调用 `_get_exe_version_output()` 时失败（可能是 Windows 路径/权限问题），导致 bun 被标记为 unavailable。  
**解决**: 绕过自动检测，直接通过 Python API 的 `js_runtimes` dict 指定绝对路径。

---

### ❌ 问题3：Deno.exe 无法执行

**症状**: `.\deno.exe --version` 无输出，BunJsRuntime 检测时 `_get_exe_version_output` 返回 None  
**状态**: Deno 在此系统上不可用，已放弃，使用 Bun 替代。

---

### ❌ 问题4：下载模块重写（从 Python API → CLI subprocess → 回归 Python API）

- **第一版**: yt-dlp Python API，直接使用 `ydl_opts` dict
- **第二版**: 改为 `subprocess` 调用 CLI — 以为 CLI 会正确识别 PATH 中的 node/bun。**失败**，CLI 同样找不到运行时。
- **最终版**: 回归 Python API，但通过 `js_runtimes` 精确注入 Bun 路径。

---

### ❌ 问题5：API 调用签名不匹配

**症状**: `upload() got an unexpected keyword argument 'video_path'`  
**原因**: `main.py` 调用 `uploader.upload(video_path=..., title=..., ...)` 但 `bili_uploader.py` 的实际签名是 `upload(video_data, final_video_path)`  
**修复**: `main.py` 改为 `uploader.upload(video_data=video, final_video_path=processed_path)`

**症状2**: `string indices must be integers, not 'str'`  
**原因**: `main.py` 调用 `processor.process(video['filepath'])` 但 `video_processor.py` 的 `process()` 期望接收完整 video dict  
**修复**: 改为 `processor.process(video)`

---

## 版本历史 (关键节点)

### v0.1 — 项目初始化
- 基本架构：config.yaml + 4个Python模块
- 使用 `cookiesfrombrowser` 获取 YouTube cookies
- 计划用 Linux crontab 定时运行

### v0.2 — Windows 适配
- 放弃 Linux crontab，改为 `run.bat` 本地双击触发
- `run.bat` 使用纯 ASCII 避免 CMD 乱码

### v0.3 — n-Challenge 首次尝试
- 下载 `deno.exe` 到项目目录
- 在 `main.py` 注入项目目录到 `os.environ["PATH"]`
- 安装 yt-dlp nightly 版
- 结果：仍然失败

### v0.4 — 客户端伪装
- 添加 `extractor_args: player_client=ios,android`
- 尝试 `player_skip=web,tv`
- 结果：android/ios 客户端不支持 cookies，失败

### v0.5 — 安装 yt-dlp-ejs + 下载 Bun
- `pip install yt-dlp-ejs` (v0.8.0)
- 从 GitHub 下载 `bun-windows-x64.zip`，解压得到 `bun.exe` (115MB, v1.3.11)
- 验证：`BunJsRuntime('E:\\...\\bun.exe').info` = `JsRuntimeInfo(version='1.3.11', supported=True)`

### v0.6 — **n-Challenge 突破** ✅ (2026-03-28)
- 发现 yt-dlp Python API 的 `js_runtimes` 参数格式：`{'bun': {'path': '/abs/path/to/bun.exe'}}`
- 在 `yt_downloader._make_ydl_opts()` 中注入此参数
- 确认日志：`[jsc:bun] Solving JS challenges using bun`
- 视频成功下载：258MB @ 10MB/s

### v0.7 — 下载逻辑修正 + Pipeline 修复 (2026-03-28)
- **下载逻辑**: 从"批量下载多个"改为"每频道只下最新1个未下载视频"
  - `playlistend: 5` 只扫描最近5条
  - 找到第一个未下载的 video_id 后立即 `break`，只下载这1个
- **API修复**: `processor.process(video)` 传完整 dict
- **API修复**: `uploader.upload(video_data=video, final_video_path=processed_path)`

### v0.8 — 健壮的流状态日志与历史管理修正 (2026-03-28)
- **致命逻辑修复**: `yt_downloader.py` 原本在下载完成后**立即**将视频写入 `history.json`。导致如果后续转码或上传失败，下次运行会直接跳过该视频。现已重构，暴露出公开方法 `downloader.save_history()`。
- **main.py 状态监控增强**: 将状态写入统一转移至 `main.py` 的主循环最后。现在每个步骤（下载 → 转码 → 上传）都会在控制台打印 `[Status] STARTED/SUCCESS/FAILED`，并且只有当哔哩哔哩**上传成功后**，视频才会被记入已下载历史 `history.json` 中，确保失败的视频在下次运行脚本时会自动重试。

### v0.9 — 终于打通哔哩哔哩最后的一公里: 解决静默失败 -400 (2026-03-28)
- **发现 Bilibili API 的暗坑**: 之前 `biliup upload` 会经历 0.7 秒的秒级静默崩溃，通过重定向详细日志排查，发现 Bilibili 接口抛出了 `-400 请求错误 (Bad Request)`。
- **导致该错误的两个原因与修复**:
  1. **不支持 WebP 封面**: yt-dlp 默认抓取到的 YouTube 封面是 `.webp`，但 Bilibili API 严格拒绝该格式并直接报错。解决：在 `bili_uploader.py` 加入了一层拦截防线，碰到 `.webp` 封面时，自动通过 `ffmpeg` 转换为 `.jpg` 后再提交给 `biliup`。
  2. **版权参数冲突**: 搬运标签的视频如果不匹配版权声明也会报错。已将参数从 `--copyright 1` (自制) 标准化更正为 `--copyright 2` (转载)，并动态附加了 `--source {youtube_url}` 参数。
- **状态**: 彻底成功！终端日志均已清晰打印 `['APP接口投稿成功', 'UPLOAD SUCCESSFUL']` 并正确同步至 history 流水账。

---

## 当前已知问题 / TODO

- [ ] **Bilibili上传测试**: 需要验证 `biliup` CLI 上传是否真正成功（需要 `cookies.json` 有效）
- [ ] **history.json 管理**: 测试过程中下载了大量视频，history已满。下次测中需考虑重置
- [ ] **iD6xitsmqcQ合并失败**: 出现过 `[WinError 32] 另一个程序正在使用此文件` — 可能是 ffmpeg 文件锁问题，出现概率低
- [ ] **Deno.exe**: 128MB，目前无用，可以删除节省空间
- [ ] **多频道支持**: 代码已支持多频道，但尚未用多频道测试
- [ ] **日志优化**: 目前 yt-dlp 在静默模式下不输出进度，考虑添加进度回调

---

## 操作手册

### 首次初始化
```bash
# 1. 安装依赖
pip install yt-dlp-ejs yt-dlp biliup pyyaml

# 2. 获取YouTube cookies
# 在Chrome登录YouTube后，用扩展导出 youtube_cookies.txt (Netscape格式)

# 3. 登录Bilibili
biliup login  # 扫码，生成 cookies.json

# 4. 编辑配置
# 修改 config.yaml 中的 channel_urls, tid, proxy 等
```

### 日常运行
```bash
# 双击 run.bat
# 或手动:
python main.py
```

### 更新 yt-dlp
```bash
pip install -U --pre yt-dlp
```

### 重置下载历史（测试用）
```bash
# 删除或清空 data/history.json
del data\history.json
```

---

## 附录：关键文件逐行说明

### main.py 核心逻辑
```
1. 确定 bun.exe 绝对路径
2. 验证 bun.exe 可用（subprocess --version）
3. 将 bun_path 注入 config dict
4. 初始化 downloader / processor / uploader
5. 运行 download → process → upload 流水线
```

### yt_downloader.py 核心逻辑
```
_make_ydl_opts():
  - 注入 js_runtimes = {'bun': {'path': bun_path}}
  - 设置 cookiefile, format, merge_output_format

download_latest_videos():
  - 对每个 channel_url：
    - 用 extract_flat 获取最近5个视频列表
    - 找第一个未在 history.json 的 video_id
    - 下载这1个视频
    - 保存到 history.json
```

### v1.1 — NVIDIA GPU (NVENC) 硬件加速编解码 (2026-03-30)
- **需求**: `libx264` 纯 CPU 编码在处理视频标准化和片头合并时速度过慢且 CPU 占用极高。
- **解决方案**: 在 `video_processor.py` 中加入了 `NVIDIA NVENC` 硬件加速检测机制。
  - 启动时自动运行 `ffmpeg -hide_banner -encoders` 检测是否支持 `h264_nvenc`。
  - 如果支持，自动调用 NVENC 进行编码，预设使用高速高画质组合：`-c:v h264_nvenc -preset p6 -rc vbr -cq 24 -b:v 0`。
  - **动态回退 (Fallback)**: 即使检测到 NVENC，若在处理视频时因驱动或显存问题导致失败，系统会自动捕获并回退（Fallback）到现有的 `libx264` CPU 编码模式，保证流水线的绝对稳健。
- **效果**: 成功将片头转码和合并阶段的速度提升了 5~10 倍，几乎不增加主 CPU 负担。

### v1.2 — 目录命名人性化与高级重置清理 (2026-03-30)
- **需求**：
  1. 需要一键清理 `history.json` 和所有的缓存碎片文件，实现真正的“重新开始”。
  2. 视频下载文件夹原先仅使用晦涩的 `video_id` 命名，希望使用原标题便于观察。
- **重置功能升级 (`web_app.py`)**：在 `/api/reset` 中增加了深度的 glob 文件铲除逻辑，一并清除 `.part`, `.ytdl`, `concat_list.txt`，以及核心历史记录器 `history.json`。保留了残缺的 `.mp4` 实体让下一次下载可以走断点续传。
- **路径系统重构 (`yt_downloader.py`)**：为了安全和兼顾可读性，采用了 `[安全文件名] [video_id]` 的混合文件夹命名制。并把传统的精确匹配目录改写为了模糊包含 `[video_id]` 查询，从而保持向下兼容所有的历史下载。
