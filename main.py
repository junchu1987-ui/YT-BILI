"""
main.py — CLI entry point for YT_BI_Anti pipeline.
For the Web UI, run web_app.py instead.
"""
import os
import sys
import subprocess

# PRE-IMPORT: resolve bun.exe absolute path before yt-dlp is loaded
_BUN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'bun.exe')

import yaml
import logging
from yt_downloader import YouTubeDownloader
from video_processor import VideoProcessor
from bili_uploader import BilibiliUploader

# Ensure logs directory exists
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(log_dir, 'pipeline.log'), encoding='utf-8'),
    ]
)


def load_config(config_path='config.yaml'):
    if not os.path.exists(config_path):
        logging.error(f'Config file {config_path} not found.')
        return None
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def run_pipeline():
    logging.info('Starting YouTube → Bilibili Pipeline (CLI mode)')

    config = load_config()
    if not config:
        return

    config['_bun_path'] = _BUN_PATH

    # Verify bun runtime
    try:
        v = subprocess.run(
            [_BUN_PATH, '--version'],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        logging.info(f'Bun JS runtime: v{v}')
    except Exception as e:
        logging.warning(f'Bun runtime check failed: {e}')

    downloader = YouTubeDownloader(config)
    processor = VideoProcessor(config)
    uploader = BilibiliUploader(config)

    def log_cb(msg, pct=None):
        logging.info(msg)

    # Step 1: Scan + download all new videos
    new_videos = downloader.download_all_sources(progress_cb=log_cb)

    if not new_videos:
        logging.info('No new videos found or downloaded.')
        return

    # Step 2 + 3: Process and upload each video
    for video in new_videos:
        video_id = video.get('id', '?')
        video_title = video.get('title', 'Unknown')

        logging.info(f'=== Migrating: {video_title} ===')
        try:
            logging.info(f'[{video_id}] TRANSCODING...')
            processed_path = processor.process(video)

            if not processed_path:
                logging.error(f'[{video_id}] TRANSCODE FAILED — skipping upload.')
                continue

            logging.info(f'[{video_id}] TRANSCODING DONE → {processed_path}')
            logging.info(f'[{video_id}] UPLOADING to Bilibili...')

            success = uploader.upload(video_data=video, final_video_path=processed_path)

            if success:
                downloader.save_history(video_id)
                logging.info(f'[{video_id}] MIGRATION COMPLETE ✓')
            else:
                logging.error(f'[{video_id}] UPLOAD FAILED — will retry next run.')

        except Exception as e:
            logging.error(f'[{video_id}] Error: {e}')

    logging.info('=== Pipeline Finished ===')


if __name__ == '__main__':
    run_pipeline()
