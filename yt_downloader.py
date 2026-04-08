import os
import json
import re
import logging
from typing import List, Dict, Optional, Callable


def detect_url_type(url: str) -> str:
    """
    Auto-detect YouTube URL type.
    Returns: 'video', 'playlist', or 'channel'
    """
    url = url.strip()
    # If the URL contains a list, honor the user's intent to scan the whole playlist
    if 'list=' in url:
        return 'playlist'
    # Single video
    if re.search(r'(youtube\.com/watch\?v=|youtu\.be/)', url):
        return 'video'
    # Channel (/@xxx, /channel/UC..., /c/xxx, /user/xxx)
    return 'channel'


class YouTubeDownloader:
    def __init__(self, config):
        self.config = config
        self.work_dir = config['app']['work_dir']
        self.proxy = config['app'].get('proxy', '')
        self.history_file = os.path.join(self.work_dir, 'history.json')
        self.bun_path = config.get('_bun_path', '')
        self._load_history()

    # ─── History management ──────────────────────────────────────────

    def _load_history(self):
        os.makedirs(self.work_dir, exist_ok=True)
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.history = json.load(f)
            except json.JSONDecodeError:
                # Corrupted file — back it up and start fresh rather than silently losing history
                import shutil
                backup = self.history_file + '.bak'
                shutil.copy2(self.history_file, backup)
                logging.warning(f"history.json is corrupted. Backed up to {backup} and starting fresh.")
                self.history = []
            except Exception:
                self.history = []
        else:
            self.history = []

    def save_history(self, video_id: str):
        if video_id not in self.history:
            self.history.append(video_id)
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)

    def is_downloaded(self, video_id: str) -> bool:
        return video_id in self.history

    def _find_video_dir(self, video_id: str):
        if not os.path.exists(self.work_dir):
            return None
        # Check backward-compatible exact match
        exact_match = os.path.join(self.work_dir, video_id)
        if os.path.isdir(exact_match):
            return exact_match
            
        # Check new pattern "Title [ID]" or anything containing the ID
        for entry in os.listdir(self.work_dir):
            if f"[{video_id}]" in entry or video_id in entry:
                path = os.path.join(self.work_dir, entry)
                if os.path.isdir(path):
                    return path
        return None

    # ─── yt-dlp options builder ──────────────────────────────────────

    def _make_ydl_opts(self, extra: dict = None) -> dict:
        """Build yt-dlp options with verified Bun JS runtime injected."""
        opts = {
            'outtmpl': os.path.join(self.work_dir, '%(id)s', '%(id)s.%(ext)s'),
            'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
            'merge_output_format': 'mp4',
            'writethumbnail': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['zh-Hans'],
            'subtitlesformat': 'vtt',
            'ignoreerrors': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'color': 'no_color',
            'retries': 10,
            'file_access_retries': 10,
            'fragment_retries': 10,
            'compat_opts': ['no-keep-alive'],
            'socket_timeout': 15,
            'cookiefile': 'youtube_cookies.txt',
            'js_runtimes': (
                {'bun': {'path': self.bun_path}}
                if self.bun_path and os.path.exists(self.bun_path)
                else None
            ),
            'ffmpeg_location': self.config['ffmpeg'].get('bin_path', 'ffmpeg'),
        }
        if self.proxy:
            opts['proxy'] = self.proxy
        if extra:
            opts.update(extra)
        return opts

    # ─── Scan (metadata only, no download) ───────────────────────────

    def scan_all_sources(self, progress_cb: Callable = None, cancel_check: Callable = None) -> List[Dict]:
        """
        Reads config sources, detects type, and returns list of
        candidate video dicts WITHOUT downloading.
        Each dict: {id, title, url, source_url, url_type, already_downloaded}
        """
        import yt_dlp

        sources = self.config['youtube'].get('sources', [])
        # Backward compat
        if not sources:
            sources = self.config['youtube'].get('channel_urls', [])

        candidates = []

        for item in sources:
            if isinstance(item, dict):
                source_url = item.get('url')
                url_type = item.get('type') or detect_url_type(source_url)
            else:
                source_url = item
                url_type = detect_url_type(source_url)
                
            if progress_cb:
                progress_cb(f"Scanning {url_type}: {source_url}")
            logging.info(f"Scanning [{url_type}] {source_url}")

            if cancel_check and cancel_check():
                raise Exception("Scan cancelled by user")

            try:
                entries = self._fetch_entries(source_url, url_type, yt_dlp)
            except Exception as e:
                logging.error(f"Failed to scan {source_url}: {e}")
                continue

            for entry in entries:
                if cancel_check and cancel_check():
                    raise Exception("Scan cancelled by user")
                    
                if not entry:
                    continue
                v_id = entry.get('id', '')
                if not v_id:
                    continue
                    
                # Identify sizes for different qualities
                size_1080p = 0
                size_4k = 0
                has_4k = False
                
                formats = entry.get('formats', [])
                for f in formats:
                    h = f.get('height')
                    if h is None: continue
                    fsize = f.get('filesize') or f.get('filesize_approx') or 0
                    
                    if h <= 1080:
                        size_1080p = max(size_1080p, fsize)
                    if h > 1080:
                        size_4k = max(size_4k, fsize)
                        has_4k = True
                
                # Fallback: if no specific format sizing found, use root filesize
                root_fs = entry.get('filesize') or entry.get('filesize_approx') or 0
                if root_fs > size_1080p and not has_4k:
                    size_1080p = root_fs
                if root_fs > size_4k and has_4k:
                    size_4k = root_fs
                
                candidates.append({
                    'id': v_id,
                    'title': entry.get('title', 'Unknown'),
                    'url': f"https://www.youtube.com/watch?v={v_id}",
                    'source_url': source_url,
                    'url_type': url_type,
                    'already_downloaded': self.is_downloaded(v_id),
                    'thumbnail': entry.get('thumbnail', ''),
                    'duration': entry.get('duration', 0),
                    'has_4k': has_4k,
                    'size_1080p': size_1080p,
                    'size_4k': size_4k,
                    'filesize': size_1080p, # Default visible
                    'quality': '1080p'    # Default quality
                })

        logging.info(f"Scan complete: {len(candidates)} videos found, "
                     f"{sum(1 for c in candidates if not c['already_downloaded'])} new.")
        return candidates

    def fetch_source_metadata(self, source_url: str, url_type: str) -> dict:
        """
        Fetch the exact title/name of a source (video, playlist, or channel) immediately.
        """
        import yt_dlp
        opts = self._make_ydl_opts({
            'extract_flat': True,
            'skip_download': True,
            'quiet': True,
            'noplaylist': False,
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source_url, download=False)
                if not info:
                    return {'url': source_url, 'type': url_type, 'title': 'Unknown Source'}
                return {'url': source_url, 'type': url_type, 'title': info.get('title', 'Unknown Source')}
        except Exception as e:
            logging.error(f"Failed to fetch metadata for {source_url}: {e}")
            return {'url': source_url, 'type': url_type, 'title': 'Unknown Source'}

    def _fetch_entries(self, url: str, url_type: str, yt_dlp) -> List[Dict]:
        """Fetch video metadata entries from a source URL."""
        if url_type == 'channel':
            # Ensure we point to /videos tab
            if '/@' in url and '/videos' not in url:
                url = url.rstrip('/') + '/videos'
            elif '/channel/' in url and '/videos' not in url:
                url = url.rstrip('/') + '/videos'
            list_opts = self._make_ydl_opts({
                'extract_flat': False, # We must fully extract to get 4K formats
                'playlistend': 10,   # scan latest 10 for channels
                'quiet': True,
                'noplaylist': False,
            })
        elif url_type == 'playlist':
            list_opts = self._make_ydl_opts({
                'extract_flat': False, # Changed to get full resolutions
                'quiet': True,
                'noplaylist': False,
            })
        else:  # video — just extract that one video's metadata
            list_opts = self._make_ydl_opts({
                'extract_flat': False,
                'skip_download': True,
                'quiet': True,
            })

        with yt_dlp.YoutubeDL(list_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return []
            # Single video returns dict without 'entries'
            if 'entries' not in info:
                return [info]
            return list(info.get('entries', []))

    # ─── Download ────────────────────────────────────────────────────

    def download_video(
        self,
        video_id: str,
        video_url: str,
        title: str = None,
        progress_cb: Callable = None,
        cancel_check: Callable = None,
        quality: str = None
    ) -> Optional[Dict]:
        """
        Download a single video by ID+URL.
        Returns metadata dict on success, None on failure.
        Does NOT add to history (caller's responsibility after upload).
        """
        import yt_dlp
        from yt_dlp.utils import sanitize_filename

        if self.is_downloaded(video_id):
            # Check if file actually exists on disk
            v_dir = self._find_video_dir(video_id)
            if v_dir:
                filepath = os.path.join(v_dir, f"{video_id}.mp4")
                # Also accept title-named mp4 (yt-dlp may use title for merged formats)
                if not os.path.exists(filepath):
                    mp4s = [f for f in os.listdir(v_dir)
                            if f.endswith('.mp4') and not f.endswith('_final.mp4')]
                    if mp4s:
                        mp4s.sort(key=lambda f: os.path.getsize(os.path.join(v_dir, f)), reverse=True)
                        filepath = os.path.join(v_dir, mp4s[0])
                if os.path.exists(filepath):
                    # Download subtitles if missing
                    has_sub = any(f.endswith('.zh-Hans.vtt') or f.endswith('.zh-Hans.ass')
                                  for f in os.listdir(v_dir))
                    if not has_sub:
                        logging.info(f"[{video_id}] Fetching missing subtitles...")
                        sub_opts = self._make_ydl_opts({
                            'skip_download': True,
                            'outtmpl': os.path.join(v_dir, f'{video_id}.%(ext)s'),
                            'quiet': True,
                        })
                        try:
                            import yt_dlp as _yt
                            with _yt.YoutubeDL(sub_opts) as ydl:
                                ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
                        except Exception as e:
                            logging.warning(f"[{video_id}] Subtitle fetch failed: {e}")
                    logging.info(f"[{video_id}] Already downloaded, skipping.")
                    return self._build_video_dict(video_id, filepath, v_dir, title=title)

        if progress_cb:
            progress_cb(f"Downloading {video_id}...")
        
        safe_title = sanitize_filename(title or video_id, restricted=False)
        folder_name = f"{(safe_title[:150]).strip()} [{video_id}]"
        
        logging.info(f"Downloading {video_id} into {folder_name}...")

        # Use existing dir if found, or create new formatted dir
        v_dir = self._find_video_dir(video_id) or os.path.join(self.work_dir, folder_name)
        os.makedirs(v_dir, exist_ok=True)

        dl_opts = self._make_ydl_opts({
            'outtmpl': os.path.join(v_dir, '%(id)s.%(ext)s'),
            'extract_flat': False,
            'overwrites': False,       # don't re-download video if exists
            'write_all_thumbnails': False,
            'progress_hooks': [self._make_progress_hook(progress_cb, cancel_check)],
        })
        
        # Override format if 4K explicitly chosen by user
        if quality == '4k':
            dl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                dl_info = ydl.extract_info(video_url, download=True)
        except Exception as e:
            logging.error(f"Download failed for {video_id}: {e}")
            return None

        if not dl_info:
            logging.error(f"No info returned for {video_id}")
            return None

        filepath = os.path.join(v_dir, f"{video_id}.mp4")
        if not os.path.exists(filepath):
            # yt-dlp may use title-based filename when merging certain format combinations
            mp4s = [f for f in os.listdir(v_dir)
                    if f.endswith('.mp4') and not f.endswith('_final.mp4')]
            if mp4s:
                # Pick the largest mp4 (the actual video, not a stray small file)
                mp4s.sort(key=lambda f: os.path.getsize(os.path.join(v_dir, f)), reverse=True)
                filepath = os.path.join(v_dir, mp4s[0])
                logging.info(f"[{video_id}] Expected {video_id}.mp4 not found, using: {mp4s[0]}")
            else:
                logging.error(f"File not found after download: {filepath}")
                return None

        result = self._build_video_dict(
            video_id, filepath, v_dir,
            title=dl_info.get('title'),
            description=dl_info.get('description', ''),
            youtube_url=video_url
        )
        logging.info(f"Successfully downloaded: {result['title']}")
        return result

    def _build_video_dict(
        self, video_id: str, filepath: str, v_dir: str,
        title: str = None, description: str = '',
        youtube_url: str = None
    ) -> Dict:
        cover_path = ''
        subtitle_zh = ''
        for fname in os.listdir(v_dir):
            if not fname.startswith(video_id):
                continue
            if fname.endswith(('.webp', '.jpg', '.jpeg', '.png')) and not cover_path:
                cover_path = os.path.join(v_dir, fname)
            elif fname.endswith('.zh-Hans.vtt') or fname.endswith('.zh-Hans.ass'):
                subtitle_zh = os.path.join(v_dir, fname)
        return {
            'id': video_id,
            'title': title or video_id,
            'description': description,
            'youtube_url': youtube_url or f'https://www.youtube.com/watch?v={video_id}',
            'filepath': filepath,
            'cover_path': cover_path,
            'subtitle_zh': subtitle_zh,
        }

    def _make_progress_hook(self, progress_cb: Callable, cancel_check: Callable = None):
        """Returns a yt-dlp progress hook that calls progress_cb with status strings."""
        def hook(d):
            if cancel_check and cancel_check():
                from yt_dlp.utils import DownloadCancelled
                raise DownloadCancelled("Download was cancelled by the user")
                
            if not progress_cb:
                return
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                pct = int(downloaded / total * 100) if total > 0 else 0
                speed = d.get('_speed_str', '?')
                if isinstance(speed, str):
                    # Strip ANSI escape codes
                    speed = re.sub(r'\x1b\[[0-9;]*m', '', speed)
                progress_cb(f"  {pct}% @ {speed}", pct)
            elif d['status'] == 'finished':
                progress_cb("  Download complete, merging...", 99)
        return hook

    # ─── Legacy entry point for CLI (main.py) ────────────────────────

    def download_all_sources(self, progress_cb: Callable = None) -> List[Dict]:
        """
        Full scan + download in one go (used by CLI main.py).
        Returns list of downloaded video dicts.
        """
        candidates = self.scan_all_sources(progress_cb)
        new_videos = []
        for c in candidates:
            if c['already_downloaded']:
                continue
            result = self.download_video(c['id'], c['url'], c.get('title', c['id']), progress_cb, quality=c.get('quality'))
            if result:
                new_videos.append(result)
        return new_videos
