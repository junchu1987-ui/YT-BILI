import os
import subprocess
import logging

class BilibiliUploader:
    def __init__(self, config):
        self.config = config
        self.tid = config['bilibili'].get('tid', 17)
        self.work_dir = config['app']['work_dir']
        self.desc_prefix_template = config['bilibili'].get(
            'desc_prefix',
            '本视频搬运自YouTube。\n原视频链接：{youtube_url}\n\n'
        )

    def upload(self, video_data, final_video_path, cancel_check=None, progress_cb=None):
        """
        Uploads the given video to Bilibili using `biliup` CLI.
        Assumes `biliup` is installed and `cookies.json` is in the working directory.
        """
        if not os.path.exists(final_video_path):
            logging.error(f"Cannot upload missing video: {final_video_path}")
            return False

        title = video_data.get('title', 'Unknown Title')
        youtube_url = video_data.get('youtube_url', '')
        raw_desc = video_data.get('description', '') or ''

        # --- Auto Translation (No-Key) ---
        try:
            import translators as ts
            if title and title != 'Unknown Title':
                logging.info(f"Translating Title via [bing]...")
                trans_title = ts.translate_text(title, translator='bing', to_language='zh-Hans')
                if trans_title:
                    logging.info(f"Translated Title: {trans_title}")
                    title = trans_title

            if raw_desc:
                logging.info(f"Translating Description via [bing]...")
                short_desc = raw_desc[:400]
                trans_desc = ts.translate_text(short_desc, translator='bing', to_language='zh-Hans')
                if trans_desc:
                    logging.info("Translated Description successfully.")
                    raw_desc = trans_desc
        except Exception as e:
            logging.warning(f"Translation failed (fallback to original English): {e}")

        # Ensure length limits
        title = title[:80]
        
        # Build description: configurable prefix + translated desc
        prefix = self.desc_prefix_template.format(youtube_url=youtube_url)
        full_desc = (prefix + raw_desc)[:500]
        
        logging.info(f"Uploading to Bilibili: {title}")
        
        cmd = [
            'biliup',
            'upload',
            final_video_path,
            '--title', title,
            '--desc', full_desc,
            '--tid', str(self.tid),
            '--copyright', '2',
            '--source', youtube_url,
            '--tag', 'YouTube,搬运'
        ]
        
        # Attach cover image if available
        cover_path = video_data.get('cover_path')
        if cover_path and os.path.exists(cover_path):
            # Bilibili API strictly rejects .webp formats with -400 Bad Request
            if cover_path.lower().endswith('.webp'):
                logging.info(f"Converting unsupported .webp cover to .jpg for Bilibili...")
                jpg_cover = cover_path.rsplit('.', 1)[0] + '.jpg'
                if not os.path.exists(jpg_cover):
                    # Quickly strip the webp format into standard jpeg
                    subprocess.run(['ffmpeg', '-y', '-i', cover_path, jpg_cover], 
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                if os.path.exists(jpg_cover):
                    cmd.extend(['--cover', jpg_cover])
            else:
                cmd.extend(['--cover', cover_path])
        
        # Prepare the environment with proxy if configured
        env = os.environ.copy()
        env['PYTHONUTF8'] = "1"  # Fix biliup GBK char print issues on Windows
        proxy = self.config['app'].get('proxy', '')
        if proxy:
            env['HTTP_PROXY'] = proxy
            env['HTTPS_PROXY'] = proxy
            env['http_proxy'] = proxy
            env['https_proxy'] = proxy

        try:
            # Stream output in real-time to avoid freezing the UI during long uploads
            process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                env=env,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1
            )
            
            # Read stdout in a separate thread so the main loop can poll cancel_check instantly
            import threading
            import time
            import re
            def log_reader(pipe):
                try:
                    for line in pipe:
                        line = line.strip()
                        if line:
                            logging.info(f"[biliup] {line}")
                            
                            # Extract percentage if present (e.g. "55.5%" or "55%")
                            if progress_cb:
                                match = re.search(r'(\d+(?:\.\d+)?)%', line)
                                if match:
                                    try:
                                        pct = int(float(match.group(1)))
                                        if pct > 99: pct = 99
                                        progress_cb(pct)
                                    except:
                                        pass
                except Exception:
                    pass
            
            reader_thread = threading.Thread(target=log_reader, args=(process.stdout,), daemon=True)
            reader_thread.start()

            # Main polling loop
            while process.poll() is None:
                if cancel_check and cancel_check():
                    logging.warning("Cancellation requested during upload. Terminating biliup...")
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # Hard kill if terminate failed
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    raise Exception("Upload cancelled by user")
                time.sleep(0.5)
            
            process.wait()
            if process.returncode == 0:
                logging.info("Upload completed successfully.")
                return True
            else:
                logging.error(f"Upload failed with exit code {process.returncode}")
                return False
        except Exception as e:
            logging.error(f"Upload process experienced an error: {e}")
            return False
