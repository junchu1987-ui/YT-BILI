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
        # Look in current dir or PATH
        if os.path.exists('biliup.exe'):
            return os.path.abspath('biliup.exe')
        return 'biliup' # assume in PATH

    def upload(self, file_path, title, source_url, original_thumbnail=None, progress_callback=None):
        """
        Uploads a video to Bilibili using the biliup CLI.
        Features: Retry logic (3x), Automated Cover Generation with Baidu ERNIE + Pillow.
        """
        if not os.path.exists(self.cookie_file):
            raise Exception("cookies.json not found. Please login via biliup first.")

        # Step 1: Automated Cover Processing
        cover_path = None
        if original_thumbnail and os.path.exists(original_thumbnail):
            try:
                # 1.1 Generate LLM summary (1-2 words)
                summary_text = self.cover_proc.get_summary(title)
                # 1.2 Generate artistic cover
                cover_path = os.path.join(os.path.dirname(file_path), "cover_custom.jpg")
                if self.cover_proc.generate_cover(original_thumbnail, summary_text, cover_path):
                    logger.info(f"Custom cover generated for upload: {cover_path}")
                else:
                    cover_path = original_thumbnail # Fallback to original
            except Exception as e:
                logger.error(f"Cover processing failed: {e}. Falling back to default.")
                cover_path = original_thumbnail

        tid = self.config['bilibili'].get('tid', 171)
        desc_prefix = self.config['bilibili'].get('desc_prefix', '')
        description = desc_prefix.replace('{youtube_url}', source_url)
        tags = ["YouTube", "Automated", "搬运"]

        # Step 2: Upload Command
        cmd = [
            self.biliup_path, 'upload', file_path,
            '--title', title,
            '--tid', str(tid),
            '--tag', ','.join(tags),
            '--desc', description,
            '--copyright', '1' # Always mark as Original (Bilibili policy for automated uploads)
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
                    bufsize=1
                )

                # Special loop to handle \r without standard line splitting
                buffer = ""
                while True:
                    char = process.stdout.read(1)
                    if not char and process.poll() is not None:
                        break
                    
                    if char == '\r' or char == '\n':
                        # Process the "line" (up to \r)
                        line = buffer.strip()
                        if line:
                            # Parse progress percentage e.g. [#######] 45%
                            # Using regex for precision
                            pct_match = re.search(r'(\d+)%', line)
                            if pct_match:
                                pct = int(pct_match.group(1))
                                if progress_callback:
                                    progress_callback(pct, f"Bilibili上传中... {pct}%")
                            
                            # Log critical info but avoid flooding for every \r update
                            if "Upload success" in line or "投稿成功" in line:
                                logger.info(f"Bilibili upload success: {title}")
                        
                        buffer = "" # clear for next segment
                    else:
                        buffer += char

                process.wait()
                if process.returncode == 0:
                    return True
                else:
                    logger.warning(f"Upload attempt {attempt+1} failed with exit code: {process.returncode}")
                    time.sleep(5) # Cooldown before retry

            except Exception as e:
                logger.error(f"Error during Bilibili upload attempt {attempt+1}: {e}")
                time.sleep(5)
        
        return False
