# YT-BILI

YouTube 到 Bilibili 全自动搬运工具。提供 Web UI，支持完整流水线：**扫描 → 下载 → 转码 → AI 翻译 → B站上传**。

---

## 功能特性

| 功能 | 说明 |
|------|------|
| **数据源管理** | 支持 YouTube 频道 / 播放列表 / 单个视频，Web UI 增删 |
| **智能扫描** | 自动获取视频格式，推荐最优下载组合，7 天本地缓存 |
| **视频下载** | yt-dlp 引擎，支持代理、Cookie、自动下载字幕和缩略图 |
| **GPU 转码** | FFmpeg + Intel QSV 加速（自动 fallback libx264），片头拼接 + 中文字幕烧录 |
| **AI 翻译** | 智谱 GLM-4-Flash 翻译标题和简介，支持单条重翻 |
| **封面处理** | 上传前可输入封面文字（最多 6 字），自动生成艺术字封面 |
| **B站上传** | biliup Python API，支持分区 / 标签 / 版权 / 定时发布 / 上传间隔 |
| **上传队列** | `video_meta.json` 持久化，支持编辑元数据、单条上传、手动标记完成 |
| **投稿日历** | 周历视图，可视化定时投递和历史上传记录 |
| **实时通信** | SSE 事件流推送状态 / 日志 / 进度到前端，无需刷新页面 |
| **B站频道检测** | 自动检查 YouTube 频道是否在 B站有同名账号（防重复搬运） |

---

## 系统架构

```
浏览器 ──HTTP/SSE──▶ Flask (web_app.py)
                          │
                    ┌─────┴──────────┐
                    ▼                ▼
              REST API          SSE /events
                    │
        ┌───────────┼──────────┬──────────────┬──────────┐
        ▼           ▼          ▼              ▼          ▼
    run_scan   run_download  run_transcode  run_translate  run_upload
    (yt-dlp)  (yt-dlp)    (VideoProcessor) (CoverProcessor) (BilibiliUploader)
        │           │          │              │              │
        ▼           ▼          ▼              ▼              ▼
     YouTube     data/      FFmpeg         智谱 AI        Bilibili API
                 *.mp4      QSV/x264      GLM-4-Flash      (biliup)
```

详细架构图见 `ytbili_arch.pdf`。

---

## 环境依赖

| 依赖 | 版本要求 | 用途 |
|------|----------|------|
| Python | 3.10+ | 核心运行环境 |
| FFmpeg | 任意（建议含 QSV） | 视频转码和合并 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 最新版 | YouTube 下载 |
| [biliup](https://github.com/biliup/biliup) | 最新版 | B站上传 |
| Bun.js | 可选 | 加速 yt-dlp JS 运行时 |

---

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/junchu1987-ui/YT-BILI.git
cd YT-BILI
```

### 2. 初始化环境

双击运行 **`setup.bat`**，脚本会自动完成：

- 检测并安装 Python / FFmpeg / Git（通过 winget）
- 创建 Python 虚拟环境 `.venv`
- 安装所有依赖包

> FFmpeg 必须加入系统 PATH。安装完成后执行 `ffmpeg -version` 验证。

### 3. 手动安装（可选）

```bash
pip install -r requirements.txt
```

---

## 配置

复制示例配置并编辑：

```bash
cp config.yaml.example config.yaml
```

```yaml
app:
  work_dir: ./data                      # 工作目录（存放下载文件）
  proxy: socks5://127.0.0.1:10808       # YouTube 代理（B站上传不走代理）
  host: 127.0.0.1
  port: 5000

youtube:
  sources: []                           # 在 Web UI 中添加，无需手动填写

ffmpeg:
  bin_path: ffmpeg                      # FFmpeg 可执行文件路径
  intro_video_path: ./assets/1.mp4      # 片头视频路径（留空则不拼接）

bilibili:
  tid: 122                              # 默认投稿分区 ID
  desc_prefix: '本视频搬运自YouTube。\n\n原视频链接：{youtube_url}\n\n'
  default_tags:                         # 默认标签列表
    - YouTube
    - 搬运
  upload_interval: 30                   # 连续上传间隔（秒）
  bili_check_similarity: 0.75           # B站同名频道检测相似度阈值

zhipu:
  api_key: ''                           # 智谱 AI API Key（用于翻译和封面摘要）
```

> 所有配置均可在 Web UI 的「设置」页面实时修改，无需重启。

---

## B站登录

首次使用需扫码登录，生成 `cookies.json`：

```bash
.venv\Scripts\biliup login
```

扫码成功后 `cookies.json` 出现在项目根目录。Cookie 有效期约 30 天，Web UI 侧边栏会在过期前提示。

---

## YouTube Cookie

下载年龄限制或私有视频需要 YouTube Cookie：

1. 在浏览器登录 YouTube
2. 使用浏览器插件（如 "Get cookies.txt LOCALLY"）导出 Cookie
3. 重命名为 `youtube_cookies.txt` 放到项目根目录

---

## 启动

```bash
python web_app.py
```

或双击 **`run_web.bat`**，浏览器自动打开 `http://127.0.0.1:5000`。

---

## 使用流程

```
① 设置  →  ② 添加来源  →  ③ 扫描  →  ④ 下载  →  ⑤ 转码  →  ⑥ 翻译  →  ⑦ 编辑  →  ⑧ 上传
```

| 步骤 | 操作 | 说明 |
|------|------|------|
| **① 设置** | 侧边栏 → 设置 | 配置代理、分区、标签、API Key |
| **② 添加来源** | 来源管理 → 输入 URL | 支持频道 / 播放列表 / 单个视频 |
| **③ 扫描** | 点击「扫描」 | 发现候选视频，推荐下载格式，7 天缓存 |
| **④ 下载** | 勾选视频 → 选择格式 → 「下载」 | 自动下载字幕和缩略图 |
| **⑤ 转码** | 点击「转码」 | 拼接片头、烧录中文字幕，生成 `*_final.mp4` |
| **⑥ 翻译** | 点击「翻译」 | AI 翻译标题和简介，支持单条重翻 |
| **⑦ 编辑** | 展开每个视频的编辑区 | 修改标题、封面文字、分区、标签、版权、定时发布时间 |
| **⑧ 上传** | 「上传」或单条上传 | 实时显示进度，支持定时发布 |

---

## 目录结构

```
YT-BILI/
├── web_app.py            # Flask 主程序
├── yt_downloader.py      # YouTube 下载模块
├── video_processor.py    # 转码 + 字幕处理
├── cover_processor.py    # 封面生成
├── bili_uploader.py      # B站上传模块
├── bili_checker.py       # B站同名频道检测
├── config.yaml           # 配置文件（本地，不入库）
├── config.yaml.example   # 配置模板
├── cookies.json          # B站登录 Cookie（本地，不入库）
├── youtube_cookies.txt   # YouTube Cookie（本地，不入库）
├── history.json          # 已上传视频 ID（本地，不入库）
├── assets/
│   └── 1.mp4             # 片头视频
├── static/
│   ├── app.js            # 前端 JS
│   └── style.css         # 前端样式
├── templates/
│   └── index.html        # Web UI 模板
├── data/                 # 工作目录（本地，不入库）
│   ├── scan_cache.json   # 扫描缓存（7 天 TTL）
│   ├── video_meta.json   # 上传队列持久化
│   └── <标题>_<vid8>/    # 每个视频的工作目录
│       ├── *.mp4         # 下载的视频
│       ├── *.webp        # 缩略图
│       ├── *.vtt         # 中文字幕（原始）
│       ├── *.srt         # 转换后的字幕
│       ├── *_final.mp4   # 转码后的最终视频
│       └── cover_custom.jpg  # 生成的封面
└── logs/
    └── web_YYYYMMDD.log  # 按天切割的运行日志
```

---

## 跨电脑迁移

迁移到新机器时，只需复制以下 5 个文件：

| 文件 | 说明 |
|------|------|
| `config.yaml` | 全部设置 |
| `youtube_cookies.txt` | YouTube 登录状态 |
| `cookies.json` | B站登录状态 |
| `history.json` | 已上传记录（**必须复制，防止重复上传**） |
| `data/video_meta.json` | 上传队列（可选，保留编辑中的元数据） |

在新电脑上 `git clone` 后运行 `setup.bat`，再将上述文件覆盖到项目根目录即可。

---

## 常见问题

**Q：扫描后没有发现视频？**  
A：检查代理配置是否正确，确认 YouTube Cookie 有效。查看 `logs/` 目录下当天日志获取详细错误。

**Q：转码速度慢 / QSV 不可用？**  
A：程序会自动 fallback 到 libx264。若要使用 QSV 加速，确认 Intel 显卡驱动已安装且 FFmpeg 包含 QSV 支持。

**Q：B站 Cookie 过期怎么办？**  
A：侧边栏会显示登录状态。重新执行 `.venv\Scripts\biliup login` 扫码即可。

**Q：字幕没有烧录进视频？**  
A：部分视频没有自动生成的中文字幕。若 yt-dlp 未下载到 `.zh-Hans.vtt`，转码时会跳过字幕步骤。

**Q：上传报错 / 视频审核不通过？**  
A：检查标题是否含违禁词，版权选项是否正确（搬运视频选「转载」，并填写原视频链接作为来源）。
