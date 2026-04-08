import os
import re
import json
import logging
import time
from datetime import datetime
from cover_processor import CoverProcessor

logger = logging.getLogger(__name__)

class BilibiliUploader:
    def __init__(self, config):
        self.config = config
        self.cookie_file = 'cookies.json'
        self.cover_proc = CoverProcessor(config)

    def upload(self, file_path, title, source_url, original_thumbnail=None, original_description=None, progress_callback=None, tid_override=None, tags_override=None, dtime_override=None, title_already_translated=False, copyright_override=None, source_override=None, desc_override=None):
        """
        Uploads a video to Bilibili using biliup's internal Python API (no CLI).
        """
        if not os.path.exists(self.cookie_file):
            raise Exception("cookies.json not found. Please login via biliup first.")

        # Step 1: AI Title & Description Translation
        bili_title = title
        try:
            if title_already_translated:
                logger.info(f"Title already translated, skipping: {title}")
            else:
                logger.info(f"Translating title...")
                bili_title = self.cover_proc.translate_title(title)
            logger.info(f"Final Bilibili Title: {bili_title}")
        except Exception as e:
            logger.error(f"Title translation failed: {e}")

        # Description: use pre-processed value from meta if available, else translate now
        if desc_override is not None:
            final_description = desc_override
            logger.info(f"Using pre-translated description ({len(final_description)} chars)")
        else:
            desc_prefix = self.config['bilibili'].get('desc_prefix', '')
            try:
                if original_description:
                    raw_desc = self.cover_proc.translate_description(original_description)
                else:
                    raw_desc = ''
            except Exception as e:
                logger.error(f"Description translation failed: {e}")
                raw_desc = original_description or ''
            final_description = f"{raw_desc}\n\n{desc_prefix.replace('{youtube_url}', source_url)}"
            final_description = final_description[:2000]

        # Ensure absolute path for video
        file_path = os.path.abspath(file_path)

        # Step 2: Automated Cover Processing
        cover_path = None
        if original_thumbnail and os.path.exists(original_thumbnail):
            try:
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
        tags = tags_override if tags_override is not None else []

        copyright_val = copyright_override if copyright_override in (1, 2) else 1

        # Step 3: Upload via biliup Python API
        max_retries = 3
        for attempt in range(max_retries):
            logger.info(f"Starting Bilibili upload (Attempt {attempt+1}/{max_retries}): {bili_title}")
            try:
                from biliup.plugins.bili_webup import BiliBili, Data

                video = Data()
                video.title = bili_title[:80]
                video.tid = tid
                video.desc = final_description
                video.copyright = copyright_val
                if copyright_val == 2:
                    video.source = source_override or source_url
                video.set_tag(tags)
                if dtime_override:
                    video.delay_time(dtime_override)

                with BiliBili(video) as bili:
                    # Load cookies
                    with open(self.cookie_file, 'r', encoding='utf-8') as f:
                        cookies_data = json.load(f)
                    bili.login_by_cookies(cookies_data)

                    # Upload video file with progress reporting
                    if progress_callback:
                        progress_callback(5, "开始上传视频文件...")

                    logger.info(f"Uploading video file: {file_path}")

                    # Intercept biliup's chunk-level logger to report real-time progress
                    _biliup_logger = logging.getLogger('biliup')
                    class _ProgressHandler(logging.Handler):
                        def emit(self, record):
                            if progress_callback:
                                msg = record.getMessage()
                                m = re.search(r'=>\s*([\d.]+)%', msg)
                                if m:
                                    chunk_pct = float(m.group(1))
                                    # Map 0-100% chunk progress to 5-83% overall
                                    overall = 5 + chunk_pct * 0.78
                                    progress_callback(int(overall), f"上传中 {chunk_pct:.1f}%")
                    _ph = _ProgressHandler()
                    _biliup_logger.addHandler(_ph)
                    try:
                        video_part = bili.upload_file(file_path, lines='AUTO', tasks=3)
                    finally:
                        _biliup_logger.removeHandler(_ph)
                    video_part['title'] = video_part['title'][:80]
                    video.append(video_part)

                    if progress_callback:
                        progress_callback(85, "视频上传完成，处理封面...")

                    # Upload cover
                    if cover_path and os.path.exists(cover_path):
                        try:
                            cover_url = bili.cover_up(cover_path).replace('http:', '')
                            video.cover = cover_url
                            logger.info(f"Cover uploaded: {cover_url}")
                        except Exception as e:
                            logger.warning(f"Cover upload failed (non-fatal): {e}")

                    if progress_callback:
                        progress_callback(95, "提交稿件...")

                    # Submit
                    ret = bili.submit('web')
                    if ret.get('code') == 0:
                        logger.info(f"Bilibili upload success: {bili_title}, aid={ret.get('data', {}).get('aid')}")
                        if progress_callback:
                            progress_callback(100, "上传成功!")
                        return True
                    else:
                        raise Exception(f"Submit failed: {ret}")

            except Exception as e:
                logger.error(f"Upload attempt {attempt+1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)

        return False
