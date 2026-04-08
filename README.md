# YT-BILI

YouTube to Bilibili 自动搬运工具。提供 Web UI，支持全流程：扫描 → 下载 → 转码（片头拼接 + 字幕烧录） → AI 翻译 → B站上传。

## 功能

- **数据源管理**：支持 YouTube 频道/播放列表/单个视频，Web UI 添加删除
- **智能扫描**：自动获取视频格式信息，推荐最优下载格式（优先 combined format），7 天缓存
- **视频下载**：yt-dlp 引擎，支持代理、cookie、自动下载字幕和缩略图
- **GPU 转码**：FFmpeg + Intel QSV 硬件加速（自动 fallback 到 libx264），片头拼接、字幕烧录
- **AI 翻译**：GLM-4-Flash 翻译标题和简介，独立步骤可单条重翻
- **封面处理**：用户可在上传前输入封面文字（最多 6 字），自动生成带艺术文字的封面；不输入则使用原始缩略图
- **B站上传**：biliup Python API，支持分区/标签/版权/定时投递/上传间隔，实时进度
- **上传队列**：upload_meta.json 持久化，支持编辑元数据、单条上传、手动标记已完成、重新扫描队列
- **投稿日历**：周历视图，可视化定时投递和已上传视频
- **实时通信**：SSE 事件流推送状态/日志/进度到前端

## 架构

```
Browser ──HTTP/SSE──▶ Flask (web_app.py)
                          │
                    ┌─────┴──────┐
                    ▼            ▼
              REST API      SSE /events
                    │
        ┌───────────┼───────────┬────────────┬──────────┐
        ▼           ▼           ▼            ▼          ▼
    run_scan   run_download  run_transcode  run_translate  run_upload
    (yt-dlp)   (yt-dlp+FFmpeg) (VideoProcessor) (CoverProcessor) (BilibiliUploader)
        │           │           │            │          │
        ▼           ▼           ▼            ▼          ▼
     YouTube     data/       FFmpeg       GLM-4-Flash  Bilibili API
                 *.mp4       QSV/x264     (智谱 AI)    (biliup)
```

详细架构图见 `ytbili_arch.pdf`（Graphviz 生成）。

## 环境依赖

- Python 3.8+
- FFmpeg（建议含 Intel QSV 支持）
- [biliup](https://github.com/biliup/biliup)（B站上传）
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)（YouTube 下载）
- Bun.js（可选，加速 yt-dlp JS 运行时）

## 安装

```bash
git clone https://github.com/junchu1987-ui/YT-BILI.git
cd YT-BILI
pip install -r requirements.txt
```

## 配置

编辑 `config.yaml`：

```yaml
app:
  work_dir: ./data          # 工作目录
  proxy: socks5://127.0.0.1:10808  # YouTube 代理（B站上传不走代理）
  host: 127.0.0.1
  port: 5000

youtube:
  sources: []               # Web UI 中添加

ffmpeg:
  bin_path: ffmpeg           # FFmpeg 路径
  intro_video_path: ./assets/intro.mp4  # 片头视频（留空则不拼接）

bilibili:
  tid: 122                  # 默认分区 ID
  desc_prefix: '本视频搬运自YouTube。\n\n原视频链接：{youtube_url}\n\n'
  default_tags:             # 默认标签
    - YouTube
    - 搬运
  upload_interval: 30       # 连续上传间隔（秒）

zhipu:
  api_key: ''               # 智谱 AI API Key（用于标题翻译和封面摘要）
```

## Bilibili 登录

首次使用需扫码登录生成 `cookies.json`：

```bash
biliup login
```

登录成功后 `cookies.json` 出现在当前目录。Cookie 有效期约 30 天，Web UI 侧边栏会提示过期。

## 运行

```bash
python web_app.py
```

浏览器访问 `http://127.0.0.1:5000`。

## 使用流程

1. **设置** — 侧边栏配置代理、分区、标签、API Key 等
2. **添加源** — 输入 YouTube 频道/播放列表/视频 URL
3. **扫描** — 点击扫描，发现候选视频并推荐下载格式
4. **下载** — 勾选视频，选择格式，开始下载（自动获取字幕和缩略图）
5. **转码** — 自动拼接片头、烧录中文字幕（宋体 30px），生成 `*_final.mp4`
6. **翻译** — AI 翻译标题和简介，支持一键全翻或逐条重翻
7. **编辑** — 修改标题、封面文字、分区、标签、版权、定时发布、简介
8. **上传** — 单条上传或批量上传至 B站

## 本地文件结构

```
config.yaml          # 配置文件
cookies.json         # B站登录 Cookie
history.json         # 已上传视频 ID 列表
data/
  scan_cache.json    # 扫描元数据缓存（7天TTL）
  upload_meta.json   # 上传队列（持久化）
  <title>_<vid8>/    # 每个视频的工作目录
    *.mp4            # 下载的视频
    *.webp           # 缩略图
    *.zh-Hans.vtt    # 中文字幕（VTT）
    *.zh-Hans.srt    # 转换后的字幕（SRT）
    *_final.mp4      # 转码后的最终视频
    cover_custom.jpg # 生成的封面
```
