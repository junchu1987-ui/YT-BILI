import os
import re
import json
import logging
import subprocess
import time
from datetime import datetime
from cover_processor import CoverProcessor

logger = logging.getLogger(__name__)

class BilibiliUploader:
    def __init__(self, config):
        self.config = config
        self.biliup_path = self._find_biliup()
        self.cookie_file = 'cookies.json'
        self.cover_proc = CoverProcessor(config)

    def _find_biliup(self):
        # Look in current dir, .venv, or PATH
        candidates = [
            'biliup.exe',
            os.path.join('.venv', 'Scripts', 'biliup.exe'),
            os.path.join('.venv', 'bin', 'biliup'), # Linux
            'biliup'
        ]
        for c in candidates:
            if os.path.exists(c):
                p = os.path.abspath(c)
                logger.info(f"Using biliup binary at: {p}")
                return p
        return 'biliup' # assume in PATH

    def upload(self, file_path, title, source_url, original_thumbnail=None, original_description=None, progress_callback=None, tid_override=None, tags_override=None):
        """
        Uploads a video to Bilibili using the biliup CLI.
        Features: AI-Translation, Robust Retry, Automated Cover.
        """
        if not os.path.exists(self.cookie_file):
            raise Exception("cookies.json not found. Please login via biliup first.")

        # Step 1: AI Title & Description Translation
        bili_title = title
        bili_desc = ""
        try:
            logger.info(f"Translating meta (title & description)...")
            bili_title = self.cover_proc.translate_title(title)
            if original_description:
                bili_desc = self.cover_proc.translate_description(original_description)
            logger.info(f"Final Bilibili Title: {bili_title}")
        except Exception as e:
            logger.error(f"Meta translation failed: {e}")

        # Ensure absolute path for video
        file_path = os.path.abspath(file_path)

        # Step 2: Automated Cover Processing
        cover_path = None
        if original_thumbnail and os.path.exists(original_thumbnail):
            try:
                # Use translated title for summary if possible
                summary_text = self.cover_proc.get_summary(bili_title)
                cover_path = os.path.abspath(os.path.join(os.path.dirname(file_path), "cover_custom.jpg"))
                if self.cover_proc.generate_cover(original_thumbnail, summary_text, cover_path):
                    logger.info(f"Custom cover generated: {cover_path}")
                else:
                    cover_path = os.path.abspath(original_thumbnail) 
            except Exception as e:
                logger.error(f"Cover processing failed: {e}")
                cover_path = os.path.abspath(original_thumbnail)

        tid = tid_override if tid_override is not None else self.config['bilibili'].get('tid', 171)
        desc_prefix = self.config['bilibili'].get('desc_prefix', '')

        # Combine translated description with original link and prefix
        final_description = f"{bili_desc}\n\n{desc_prefix.replace('{youtube_url}', source_url)}"
        tags = tags_override if tags_override is not None else ["YouTube", "Automated", "搬运", "AI翻译", "中字"]

        # Step 3: Upload Command
        cmd = [
            self.biliup_path, 'upload', file_path,
            '--title', bili_title,
            '--tid', str(tid),
            '--tag', ','.join(tags),
            '--desc', final_description,
            '--copyright', '1'
        ]
        
        if cover_path and os.path.exists(cover_path):
            cmd += ['--cover', cover_path]

        # Step 3: Retry Loop (3x)
        max_retries = 3
        for attempt in range(max_retries):
            logger.info(f"Starting Bilibili upload (Attempt {attempt+1}/{max_retries}): {title}")
            try:
                # We use raw subprocess to capture \r based progress streams
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                    universal_newlines=True
                )

                # Read output line-by-line; handle \r-terminated progress lines
                # by treating \r as a line separator via universal newlines mode.
                full_output = []
                upload_timeout = 3600  # 1 hour max per upload attempt
                deadline = time.time() + upload_timeout
                while True:
                    if time.time() > deadline:
                        process.kill()
                        raise Exception("Upload timed out after 1 hour")
                    line_raw = process.stdout.readline()
                    if not line_raw:
                        if process.poll() is not None:
                            break
                        continue
                    line = line_raw.rstrip('\r\n').strip()
                    if not line:
                        continue
                    full_output.append(line)
                    # Parse progress percentage e.g. [#######] 45%
                    pct_match = re.search(r'(\d+)%', line)
                    if pct_match:
                        pct = int(pct_match.group(1))
                        if progress_callback:
                            progress_callback(pct, f"Bilibili上传中... {pct}%")

                    if "Upload success" in line or "投稿成功" in line:
                        logger.info(f"Bilibili upload success: {title}")

                process.wait()
                if process.returncode == 0:
                    return True
                else:
                    error_msg = "\n".join(full_output[-20:]) # Get last 20 lines of output
                    logger.error(f"Upload attempt {attempt+1} failed (code {process.returncode}). Output:\n{error_msg}")
                    time.sleep(5) # Cooldown before retry

            except Exception as e:
                logger.error(f"Error during Bilibili upload attempt {attempt+1}: {e}")
                time.sleep(5)
        
        return False
