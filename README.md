# YouTube to Bilibili Automation

这是一个自动化工具，用于监控特定YouTube频道，下载最新的最高画质视频（支持4K/1080P）和音频，使用 `ffmpeg` 自动转码并拼接预设的片头，最后通过 `biliup` 自动上传至Bilibili。

## 环境依赖准备

1. **Python 3.8+**
2. **FFmpeg**: 必须安装 FFmpeg 并将其加入到系统的PATH环境变量中。
   - Linux: `sudo apt install ffmpeg` 或 `sudo yum install ffmpeg`
   - Windows: 下载可执行文件并添加到系统环境变量。

3. **Python 库安装**:
   ```bash
   pip install -r requirements.txt
   ```

## 首次配置与运行流程

### 1. 修改配置文件 (`config.yaml`)
编辑 `config.yaml` 文件：
- **`proxy`**: 如果在国内服务器或本地运行，必须配置科学上网代理（如 `socks5://127.0.0.1:10808` 或 `http://127.0.0.1:10809`）。
- **`channel_url`**: 填入需要搬运的 YouTube 频道首页地址（如 `https://www.youtube.com/@xxx/videos`）。
- **`intro_video_path`**: 准备一个片头视频（尽量简短，会被自动转码适配主视频画质），放在对应路径。如果没有片头需求，可以将此项留空。
- **`tid`**: 指定B站分区ID（例如17是单机游戏，需查询B站分区列表）。

### 2. 准备 Bilibili Cookie (`cookies.json`)
本工具使用 `biliup` 进行上传，需要先在环境中登录：
在终端执行以下命令并扫码登录：
```bash
biliup login
```
登录成功后，会在当前目录下生成 `cookies.json` 文件。请妥善保管！

*(如果在纯命令行 Linux 服务器上运行，可以在本地机器使用 `biliup login` 生成 `cookies.json` 后，将其上传复制到服务器的本目录中。)*

### 3. 测试运行
在配置好 `config.yaml` 和 `cookies.json`，且备好了片头视频后，进行首次测试运行：
```bash
python main.py
```

## 按需运行（当前模式）

本工具当前采用**手动按需触发**模式。每次您想要执行一次完整的"检查-下载-处理-上传"流程时，有两种方式：

### 方式一：双击运行（推荐）

直接双击项目目录下的 **`run.bat`** 文件，脚本会自动：
1. 切换到正确的工作目录
2. 执行 `python main.py`
3. 完成后暂停等待查看日志

> [!IMPORTANT]
> 运行前请确保 **v2rayN 已启动**（用于 yt-dlp 通过代理访问 YouTube），且代理端口与 `config.yaml` 中 `proxy` 配置一致。

### 方式二：命令行手动运行

```powershell
cd e:\DevProject\YT_BI_Anti
python main.py
```

---

> [!NOTE]
> **未来若要切换为定时自动执行（Linux 服务器）**，只需将 `.py` 源文件、`config.yaml`、`cookies.json` 和 `history.json` 复制到服务器，安装相同依赖，然后用 crontab 触发：
> ```cron
> 0 */12 * * * cd /opt/YT_BI_Anti && venv/bin/python main.py >> run.log 2>&1
> ```
