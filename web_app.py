import os
import sys
import yaml
import json
import logging
import subprocess
import threading
from queue import Queue
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response
from bili_uploader import BilibiliUploader
import re

# Helper for filename-safe titles
def slugify(text):
    # Remove Windows illegal characters: \/:*?"<>|
    text = re.sub(r'[\\/:*?"<>|]', '_', text)
    # Remove trailing dots/spaces and limit length
    return text.strip().rstrip('. ')[:100]

def _truncate_at_sentence(text, limit):
    """在不超过 limit 字符的前提下，按句子边界截断文本。"""
    if len(text) <= limit:
        return text
    chunk = text[:limit]
    for sep in ['\n\n', '。', '！', '？', '\n', '.', '!', '?', '…']:
        idx = chunk.rfind(sep)
        if idx > limit // 3:
            return chunk[:idx + len(sep)]
    return chunk

# ── Logging ──────────────────────────────────────────────────────────────────
os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f"logs/web_{datetime.now().strftime('%Y%m%d')}.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ── App Init ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
state_lock = threading.Lock()
history_lock = threading.Lock()
_meta_lock = threading.Lock()  # 保护 video_meta.json 的所有读写操作
# Global state for pipeline
S = {
    'status': 'idle',      # idle, scanning, scan_done, downloading, download_done, transcoding, transcode_done, uploading, done
    'candidates': [],       # List of discovered videos
    'downloaded': [],       # List of successfully downloaded videos
    'transcoded': [],       # List of successfully transcoded videos
    'uploaded': [],         # List of successfully uploaded videos
    'errors': [],           # List of {id, step, message}
    'progress': {},         # id -> {pct, message} - for real-time reporting
    'video_meta': {},       # vid -> {title, tid, tags, schedule_time, copyright, source}
    'cancel_flag': False,
    'current_task': None,
    'pipeline_active': False,
    'pipeline_auto_upload': False,
    'pipeline_counts': {
        'download':  {'queued': 0, 'done': 0, 'failed': 0},
        'transcode': {'queued': 0, 'done': 0, 'failed': 0},
        'translate': {'queued': 0, 'done': 0, 'failed': 0},
        'upload':    {'queued': 0, 'done': 0, 'failed': 0},
    },
}

# Active pipeline download queue (set by run_pipeline, allows dynamic video injection)
_pipeline_download_q = None

def reset_pipeline():
    with state_lock:
        S['status'] = 'idle'
        S['candidates'] = []
        S['downloaded'] = []
        S['transcoded'] = []
        S['uploaded'] = []
        S['errors'] = []
        S['progress'] = {}
        S['video_meta'] = {}
        S['cancel_flag'] = False
        S['current_task'] = None
        S['pipeline_active'] = False
        S['pipeline_auto_upload'] = False
        S['pipeline_counts'] = {
            'download':  {'queued': 0, 'done': 0, 'failed': 0},
            'transcode': {'queued': 0, 'done': 0, 'failed': 0},
            'translate': {'queued': 0, 'done': 0, 'failed': 0},
            'upload':    {'queued': 0, 'done': 0, 'failed': 0},
        }

# ── Config Loader ────────────────────────────────────────────────────────────
CONFIG_FILE = 'config.yaml'
_config_lock = threading.Lock()

def load_config():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(CONFIG_FILE):
        return {
            'app': {'work_dir': os.path.join(base_dir, 'data'), 'proxy': '', 'host': '127.0.0.1', 'port': 5000},
            'youtube': {'sources': []},
            'ffmpeg': {'bin_path': 'ffmpeg', 'intro_video_path': os.path.join(base_dir, 'assets', 'intro.mp4')},
            'bilibili': {'tid': 122, 'desc_prefix': '本视频搬运自YouTube。\n\n原视频链接：{youtube_url}\n\n\n', 'bili_check_similarity': 0.75}
        }
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    # Resolve relative paths in config against the app directory
    work_dir = cfg.get('app', {}).get('work_dir', './data')
    if not os.path.isabs(work_dir):
        cfg['app']['work_dir'] = os.path.normpath(os.path.join(base_dir, work_dir))
    return cfg

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)

def _js_runtimes():
    """Return js_runtimes dict for yt-dlp if bun is available, else None."""
    candidates = [
        os.path.join(os.path.expanduser('~'), '.bun', 'bin', 'bun.exe'),
        os.path.join(os.path.expanduser('~'), '.bun', 'bin', 'bun'),
        'bun',
    ]
    for p in candidates:
        if os.path.exists(p):
            return {'bun': {'path': p}}
    return None

# ── SSE Event Hub ────────────────────────────────────────────────────────────
clients = []

def broadcast(event, data):
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    for q in list(clients):
        try:
            q.put(payload)
        except:
            clients.remove(q)

@app.route('/events')
def events():
    import queue
    q = queue.Queue()
    clients.append(q)
    def stream():
        # Send initial snapshot
        yield f"event: snapshot\ndata: {json.dumps({k: S[k] for k in S if k != 'current_task'})}\n\n"
        while True:
            yield q.get()
    return Response(stream(), mimetype='text/event-stream')

def update_state(new_status=None, **kwargs):
    with state_lock:
        if new_status: S['status'] = new_status
        for k, v in kwargs.items():
            if k in S: S[k] = v
        snapshot = {k: S[k] for k in S if k != 'current_task'}
    broadcast('state', snapshot)

def log_to_web(level, message, video_id=None):
    ts = datetime.now().strftime('%H:%M:%S')
    broadcast('log', {'ts': ts, 'level': level, 'message': message, 'id': video_id})
    if level == 'error':
        logger.error(f"[{video_id or 'GLOBAL'}] {message}")
    else:
        logger.info(f"[{video_id or 'GLOBAL'}] {message}")

def report_progress(video_id, pct, message):
    with state_lock:
        S['progress'][video_id] = {'pct': pct, 'message': message}
    broadcast('progress', {'id': video_id, 'pct': pct, 'message': message})

# ── Scan metadata cache ───────────────────────────────────────────────────────
_SCAN_CACHE_TTL = 7 * 86400  # 7 days

def _load_scan_cache(work_dir):
    path = os.path.join(work_dir, 'scan_cache.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def _save_scan_cache(work_dir, cache):
    path = os.path.join(work_dir, 'scan_cache.json')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        logging.warning(f"Failed to save scan cache: {e}")

def _load_video_meta(work_dir):
    path = os.path.join(work_dir, 'video_meta.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def _save_video_meta(work_dir, meta):
    path = os.path.join(work_dir, 'video_meta.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def _update_stage(work_dir, vid, stage, status):
    """更新 video_meta.json 中某视频某阶段的状态。
    stage: 'scan'|'download'|'transcode'|'translate'|'upload'
    status: 'pending'|'done'|'failed'|'skipped'
    """
    with _meta_lock:
        meta = _load_video_meta(work_dir)
        if vid not in meta:
            return
        if 'stages' not in meta[vid]:
            meta[vid]['stages'] = {}
        meta[vid]['stages'][stage] = {'status': status, 'at': int(time.time())}
        _save_video_meta(work_dir, meta)
    with state_lock:
        if vid in S['video_meta']:
            if 'stages' not in S['video_meta'][vid]:
                S['video_meta'][vid]['stages'] = {}
            S['video_meta'][vid]['stages'][stage] = meta[vid]['stages'][stage]

def _queue_for_upload(c):
    """转码成功后写入 video_meta.json，不覆盖用户已编辑的字段。"""
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    vid = c['id']
    safe_title = slugify(c['title'])
    vid_dir = os.path.join(work_dir, f"{safe_title}_{vid[:8]}")
    final_path = os.path.join(vid_dir, f"{safe_title}_final.mp4")
    thumb = None
    for ext in ['webp', 'jpg', 'jpeg', 'png']:
        t = os.path.join(vid_dir, f"{safe_title}.{ext}")
        if os.path.exists(t):
            thumb = t
            break

    with _meta_lock:
        meta = _load_video_meta(work_dir)
        if vid not in meta:
            meta[vid] = {
                'title': c.get('translated_title') or c['title'],
                'original_title': c['title'],
                'tid': cfg['bilibili'].get('tid', 122),
                'tags': list(cfg['bilibili'].get('default_tags', [])),
                'copyright': 1,
                'source': c['url'],
                'schedule_time': None,
                'uploaded': False,
                'local_path': final_path,
                'original_thumbnail': thumb,
                'url': c['url'],
                'queued_at': int(time.time()),
                'stages': {
                    'scan':      {'status': 'done',    'at': int(time.time())},
                    'download':  {'status': 'done',    'at': int(time.time())},
                    'transcode': {'status': 'done',    'at': int(time.time())},
                    'translate': {'status': 'pending', 'at': None},
                    'upload':    {'status': 'pending', 'at': None},
                },
            }
        else:
            # 只更新 local_path/thumbnail/title，绝不覆盖 schedule_time/tid/tags 等用户设置
            translated = c.get('translated_title')
            if translated:
                meta[vid]['title'] = translated
            meta[vid]['local_path'] = final_path
            if thumb:
                meta[vid]['original_thumbnail'] = thumb
        _save_video_meta(work_dir, meta)
    with state_lock:
        S['video_meta'][vid] = meta[vid]

def restore_state():
    """On startup, restore pipeline state from video_meta.json.
    - scan_done / download pending/running → restore as candidates
    - download done / transcode pending/running → restore as downloaded (retryable)
    - transcode done → restore as transcoded (ready to upload)
    - running stages are reset to pending (crash recovery)
    """
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    if not os.path.isdir(work_dir):
        return

    # Migrate upload_meta.json → video_meta.json on first run after rename
    old_path = os.path.join(work_dir, 'upload_meta.json')
    new_path = os.path.join(work_dir, 'video_meta.json')
    if os.path.isfile(old_path) and not os.path.isfile(new_path):
        import shutil
        shutil.move(old_path, new_path)
        logging.info("Migrated upload_meta.json → video_meta.json")

    meta = _load_video_meta(work_dir)
    if not meta:
        return

    scan_cache = _load_scan_cache(work_dir)

    candidates_restored = []
    downloaded_restored = []
    transcoded_restored = []
    dirty = False

    for vid, m in list(meta.items()):
        if m.get('uploaded'):
            continue

        # 入队超过5天未上传，自动标记为已上传（视为过期放弃）
        queued_at = m.get('queued_at') or 0
        if queued_at and (time.time() - queued_at) > 5 * 24 * 3600:
            meta[vid]['uploaded'] = True
            meta[vid]['uploaded_at'] = int(time.time())
            dirty = True
            logging.info(f"restore_state: {vid} 入队超5天未上传，自动标记已上传")
            continue

        stages = m.get('stages', {})
        has_stages = bool(stages)
        scan_st   = stages.get('scan',      {}).get('status', 'pending')
        dl_st     = stages.get('download',  {}).get('status', 'pending')
        tc_st     = stages.get('transcode', {}).get('status', 'pending')

        # 旧条目无 stages 字段：根据 local_path 是否存在决定恢复到哪里
        if not has_stages:
            lp = m.get('local_path', '')
            if lp and not os.path.isabs(lp):
                lp = os.path.join(os.path.dirname(os.path.abspath(__file__)), lp)
            if lp and os.path.isfile(lp) and os.path.getsize(lp) >= 1024 * 1024:
                c = {
                    'id': vid,
                    'title': m.get('original_title') or m.get('title', ''),
                    'translated_title': m.get('title', ''),
                    'description': scan_cache.get(vid, {}).get('description', ''),
                    'url': m.get('url', f'https://www.youtube.com/watch?v={vid}'),
                    'url_type': 'video',
                    'already_downloaded': True,
                    'formats': scan_cache.get(vid, {}).get('formats', []),
                    'rec_format_id': scan_cache.get(vid, {}).get('rec_format_id'),
                    'original_thumbnail': m.get('original_thumbnail'),
                    'local_path': lp,
                    'local_dir': os.path.dirname(lp),
                }
                transcoded_restored.append(c)
                downloaded_restored.append(c)
            # 无文件的旧条目跳过（不删除，不加入恢复列表）
            continue

        # Reset any "running" stages to "pending" (crash recovery)
        if dl_st == 'running':
            meta[vid]['stages']['download']['status'] = 'pending'
            dl_st = 'pending'
            dirty = True
        if tc_st == 'running':
            meta[vid]['stages']['transcode']['status'] = 'pending'
            tc_st = 'pending'
            dirty = True
        tr_st = stages.get('translate', {}).get('status', 'pending')
        if tr_st == 'running':
            meta[vid]['stages']['translate']['status'] = 'pending'
            dirty = True

        c = {
            'id': vid,
            'title': m.get('original_title') or m.get('title', ''),
            'translated_title': m.get('title', ''),
            'description': scan_cache.get(vid, {}).get('description', ''),
            'url': m.get('url', f'https://www.youtube.com/watch?v={vid}'),
            'url_type': 'video',
            'already_downloaded': False,
            'formats': scan_cache.get(vid, {}).get('formats', []),
            'rec_format_id': scan_cache.get(vid, {}).get('rec_format_id'),
            'original_thumbnail': m.get('original_thumbnail'),
        }

        if tc_st == 'done':
            # Transcoded: verify local file exists
            lp = m.get('local_path', '')
            if lp and not os.path.isabs(lp):
                lp = os.path.join(os.path.dirname(os.path.abspath(__file__)), lp)
            if not lp or not os.path.isfile(lp) or os.path.getsize(lp) < 1024 * 1024:
                logging.info(f"restore_state: {vid} transcode_done but local_path missing, downgrading to candidate")
                # Downgrade - treat as scan_done candidate
                meta[vid]['stages']['transcode']['status'] = 'pending'
                meta[vid]['stages']['download']['status'] = 'pending'
                dirty = True
                candidates_restored.append(c)
            else:
                c['already_downloaded'] = True
                c['local_path'] = lp
                c['local_dir'] = os.path.dirname(lp)
                transcoded_restored.append(c)
                downloaded_restored.append(c)

        elif dl_st == 'done':
            # Downloaded but not transcoded
            lp = m.get('local_path', '')
            if lp and not os.path.isabs(lp):
                lp = os.path.join(os.path.dirname(os.path.abspath(__file__)), lp)
            if lp and os.path.isfile(lp) and os.path.getsize(lp) >= 1024 * 1024:
                c['already_downloaded'] = True
                c['local_path'] = lp
                c['local_dir'] = os.path.dirname(lp)
                downloaded_restored.append(c)
            else:
                # File gone, reset download stage
                meta[vid]['stages']['download']['status'] = 'pending'
                dirty = True
                candidates_restored.append(c)

        elif scan_st == 'done':
            # Scan done, not yet downloaded
            candidates_restored.append(c)

    if dirty:
        with _meta_lock:
            _save_video_meta(work_dir, meta)

    if transcoded_restored or downloaded_restored or candidates_restored:
        with state_lock:
            S['candidates']  = candidates_restored
            S['downloaded']  = downloaded_restored
            S['transcoded']  = transcoded_restored
            S['video_meta']  = {vid: m for vid, m in meta.items() if not m.get('uploaded')}
            if transcoded_restored:
                S['status'] = 'pipeline_done'
            elif downloaded_restored:
                S['status'] = 'download_done'
            elif candidates_restored:
                S['status'] = 'scan_done'
        logging.info(
            f"State restored: {len(candidates_restored)} candidates, "
            f"{len(downloaded_restored)} downloaded, {len(transcoded_restored)} transcoded."
        )
        print(f"[restore_state] candidates={len(candidates_restored)} downloaded={len(downloaded_restored)} transcoded={len(transcoded_restored)} status={S['status']}", flush=True)

# ── Pipeline Core ─────────────────────────────────────────────────────────────
def run_scan():
    try:
        update_state('scanning')
        log_to_web('info', "开始扫描数据源...")
        cfg = load_config()
        sources = cfg['youtube'].get('sources', [])
        
        from yt_dlp import YoutubeDL
        import uuid
        
        new_candidates = []
        work_dir = cfg['app']['work_dir']
        scan_cache = _load_scan_cache(work_dir)
        cache_dirty = False
        for s in sources:
            if S['cancel_flag']: break
            url = s['url']

            # Strip YouTube Radio Mix playlist params (list=RD...) — treat as single video
            # Radio mixes expand to 25-50+ videos and trigger rate limiting rapidly
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            parsed = urlparse(url)
            qs = parse_qs(parsed.query, keep_blank_values=True)
            list_val = qs.get('list', [''])[0]
            if list_val.startswith('RD'):
                vid_id = qs.get('v', [''])[0]
                if vid_id:
                    url = f"https://www.youtube.com/watch?v={vid_id}"
                    log_to_web('info', f"Radio Mix URL 已简化为单视频: {url}")

            log_to_web('info', f"扫描中: {url}")

            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,   # Fetch full info for filesize data
                'proxy': cfg['app'].get('proxy'),
                'js_runtimes': _js_runtimes(),
                'cookiefile': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'youtube_cookies.txt'),
                'playlistend': 30,       # Cap playlist scans to 30 most recent
                'sleep_interval': 1,     # 1s delay between requests to avoid rate limiting
                'max_sleep_interval': 3,
            }
            with YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    entries = info.get('entries', [info])

                    for e in entries:
                        if not e: continue
                        vid = e.get('id')
                        if not vid: continue

                        now = int(time.time())
                        cached = scan_cache.get(vid)

                        if cached and (now - cached.get('cached_at', 0)) < _SCAN_CACHE_TTL:
                            all_formats = cached['formats']
                            rec_format_id = cached['rec_format_id']
                        else:
                            all_formats = []
                            for f in e.get('formats', []):
                                # Filter out storyboards/fragments but keep a record of thumbnails if needed
                                if f.get('acodec') == 'none' and f.get('vcodec') == 'none': continue

                                is_thumb = f.get('ext') in ['webp', 'jpg', 'jpeg', 'png']

                                all_formats.append({
                                    'format_id': f.get('format_id'),
                                    'ext': f.get('ext'),
                                    'resolution': f.get('resolution') or (f"{f.get('width')}x{f.get('height')}" if f.get('width') else 'audio only'),
                                    'filesize': f.get('filesize') or f.get('filesize_approx') or 0,
                                    'vcodec': f.get('vcodec', 'none'),
                                    'acodec': f.get('acodec', 'none'),
                                    'abr': f.get('abr', 0),
                                    'vbr': f.get('vbr', 0),
                                    'note': f.get('format_note', ''),
                                    'is_thumbnail': is_thumb
                                })

                            # Sort formats: combined first, then resolution desc
                            all_formats.sort(key=lambda x: (x['vcodec'] != 'none' and x['acodec'] != 'none', x['resolution']), reverse=True)

                            # Pick recommended format:
                            # 1. Best video: resolution=1920x1080, ext=mp4, acodec=none → largest filesize
                            # 2. Best audio: ext=m4a, vcodec=none → largest filesize
                            # Fall back to combo only when either track is unavailable.
                            mp4_1080_video = [
                                f for f in all_formats
                                if f['ext'] == 'mp4' and f['acodec'] == 'none'
                                and f['resolution'] == '1920x1080'
                            ]
                            m4a_audio = [
                                f for f in all_formats
                                if f['ext'] == 'm4a' and f['vcodec'] == 'none'
                            ]
                            best_video = (
                                sorted(mp4_1080_video, key=lambda f: f['filesize'], reverse=True)[0]
                                if mp4_1080_video else None
                            )
                            best_audio = (
                                sorted(m4a_audio, key=lambda f: f['filesize'], reverse=True)[0]
                                if m4a_audio else None
                            )

                            def res_height(f):
                                try: return int(f['resolution'].split('x')[1])
                                except: return 0

                            # Fallback: any mp4 video-only <= 1080p by filesize
                            if not best_video:
                                mp4_video = [
                                    f for f in all_formats
                                    if f['ext'] == 'mp4' and f['acodec'] == 'none'
                                    and f['resolution'] not in ('', 'audio only')
                                ]
                                mp4_video_1080 = [f for f in mp4_video if res_height(f) <= 1080]
                                best_video = (
                                    sorted(mp4_video_1080, key=lambda f: f['filesize'], reverse=True)[0]
                                    if mp4_video_1080 else None
                                )

                            # Fallback audio: any audio-only by filesize
                            if not best_audio:
                                audio_only = [f for f in all_formats if f['vcodec'] == 'none' and f['acodec'] != 'none']
                                best_audio = (
                                    sorted(audio_only, key=lambda f: f['filesize'], reverse=True)[0]
                                    if audio_only else None
                                )

                            # Combo fallback: when no separate tracks available
                            combo_formats = [f for f in all_formats if f['acodec'] != 'none' and f['vcodec'] != 'none']
                            combo_1080 = [f for f in combo_formats if res_height(f) <= 1080]
                            best_combo = (
                                sorted(combo_1080, key=lambda f: f['filesize'], reverse=True)[0]
                                if combo_1080 else None
                            )
                            use_combo = best_combo and not (best_video and best_audio)

                            recommended_ids = set()
                            if use_combo:
                                recommended_ids.add(best_combo['format_id'])
                            else:
                                if best_video: recommended_ids.add(best_video['format_id'])
                                if best_audio: recommended_ids.add(best_audio['format_id'])
                            for f in all_formats:
                                f['recommended'] = f['format_id'] in recommended_ids

                            rec_format_id = None
                            if use_combo:
                                rec_format_id = best_combo['format_id']
                            elif best_video and best_audio:
                                rec_format_id = f"{best_video['format_id']}+{best_audio['format_id']}"
                            elif best_video:
                                rec_format_id = best_video['format_id']

                            scan_cache[vid] = {
                                'title': e.get('title', ''),
                                'description': e.get('description', ''),
                                'formats': all_formats,
                                'rec_format_id': rec_format_id,
                                'cached_at': now,
                                'channel_name': e.get('uploader') or e.get('channel', ''),
                                'channel_id': e.get('channel_id') or e.get('uploader_id', ''),
                            }
                            cache_dirty = True

                        new_candidates.append({
                            'id': e['id'],
                            'title': e['title'],
                            'description': e.get('description', ''),
                            'url': f"https://www.youtube.com/watch?v={e['id']}" if 'entries' in info else url,
                            'url_type': 'video',
                            'already_downloaded': False,
                            'formats': all_formats,
                            'rec_format_id': rec_format_id,
                            'channel_name': e.get('uploader') or e.get('channel', '') or scan_cache.get(vid, {}).get('channel_name', ''),
                            'channel_id': e.get('channel_id') or e.get('uploader_id', '') or scan_cache.get(vid, {}).get('channel_id', ''),
                        })
                except Exception as e:
                    err_msg = str(e)
                    if 'Sign in to confirm your age' in err_msg or 'age-restricted' in err_msg.lower():
                        log_to_web('warning', f"⚠ 年龄限制视频，无法下载（需登录验证）: {url}")
                    else:
                        log_to_web('error', f"源解析失败 {url}: {err_msg}")

        if cache_dirty:
            _save_scan_cache(work_dir, scan_cache)

        # Deduplicate and check history
        history = get_history()
        um = _load_video_meta(work_dir)
        for c in new_candidates:
            if c['id'] in history:
                # 若 video_meta 中明确标记 upload 未完成，则不跳过（允许重新处理）
                upload_stage = um.get(c['id'], {}).get('stages', {}).get('upload', {})
                if upload_stage.get('status') == 'done' or c['id'] not in um:
                    c['already_downloaded'] = True
        
        # Merge with existing candidates (if any)
        with state_lock:
            S['candidates'] = new_candidates
            # If there are already transcoded videos in memory, stay in pipeline_done
            new_status = 'pipeline_done' if S['transcoded'] else 'scan_done'
        log_to_web('info', f"扫描完成，发现 {len(new_candidates)} 个候选视频。")
        update_state(new_status)
    except Exception as e:
        log_to_web('error', f"扫描阶段崩溃: {str(e)}")
        update_state('idle')

def _ensure_audio(video_path, vid_dir, vid, candidate, cfg):
    """
    Check if video_path has an audio track via ffprobe.
    If not: find an m4a/aac file in vid_dir, or download best audio, then merge.
    Returns path to the final video file (with audio).
    """
    ffprobe = cfg['ffmpeg'].get('bin_path', 'ffmpeg').replace('ffmpeg', 'ffprobe')
    ffmpeg  = cfg['ffmpeg'].get('bin_path', 'ffmpeg')

    # Check audio streams
    try:
        r = subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'a',
             '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', video_path],
            capture_output=True, text=True, timeout=30
        )
        has_audio = bool(r.stdout.strip())
    except Exception as e:
        logging.warning(f"[{vid}] ffprobe audio check failed: {e}")
        return video_path

    if has_audio:
        logging.info(f"[{vid}] Audio track present, no merge needed.")
        return video_path

    logging.info(f"[{vid}] No audio track found, looking for audio file to merge...")

    # Find existing m4a/aac in directory
    audio_file = None
    for f in os.listdir(vid_dir):
        if f.lower().endswith(('.m4a', '.aac', '.opus', '.webm')) and os.path.getsize(os.path.join(vid_dir, f)) > 10240:
            audio_file = os.path.join(vid_dir, f)
            logging.info(f"[{vid}] Found existing audio: {f}")
            break

    # If not found, download best audio
    if not audio_file:
        logging.info(f"[{vid}] Downloading best audio track...")
        audio_out = os.path.join(vid_dir, f"{vid}_audio.%(ext)s")
        try:
            from yt_dlp import YoutubeDL
            audio_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio',
                'outtmpl': audio_out,
                'quiet': True,
                'no_warnings': True,
                'proxy': cfg['app'].get('proxy'),
                'cookiefile': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'youtube_cookies.txt'),
                'ffmpeg_location': ffmpeg,
            }
            with YoutubeDL(audio_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={vid}"])
            # Find what was written
            for f in os.listdir(vid_dir):
                if f.startswith(f"{vid}_audio") and os.path.getsize(os.path.join(vid_dir, f)) > 10240:
                    audio_file = os.path.join(vid_dir, f)
                    break
        except Exception as e:
            logging.error(f"[{vid}] Audio download failed: {e}")
            return video_path

    if not audio_file:
        logging.error(f"[{vid}] Could not obtain audio file, skipping merge.")
        return video_path

    # Merge video + audio
    merged = video_path.rsplit('.', 1)[0] + '_merged.mp4'
    try:
        r = subprocess.run(
            [ffmpeg, '-y', '-i', video_path, '-i', audio_file,
             '-c:v', 'copy', '-c:a', 'aac', '-shortest', merged],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode == 0 and os.path.getsize(merged) > 0:
            os.replace(merged, video_path)
            logging.info(f"[{vid}] Audio merged successfully.")
        else:
            logging.error(f"[{vid}] Merge failed: {r.stderr[-300:]}")
            if os.path.exists(merged):
                os.remove(merged)
    except Exception as e:
        logging.error(f"[{vid}] Merge exception: {e}")

    return video_path


def _download_one(vid_entry, cfg):
    """Download one video. Returns updated candidate dict on success, None on failure.
    Appends to S['downloaded'] on success."""
    from yt_dlp import YoutubeDL

    work_dir = cfg['app']['work_dir']
    vid = vid_entry['id']
    format_id = vid_entry.get('format_id')
    quality = vid_entry.get('quality', '1080p')
    with_subtitles = vid_entry.get('with_subtitles', True)

    c = next((x for x in S['candidates'] if x['id'] == vid), None)
    if not c:
        return None

    # Carry per-video flags through the pipeline
    c['auto_upload'] = vid_entry.get('auto_upload', False)

    log_to_web('info', f"开始下载 [{format_id or quality}]: {c['title']}", vid)
    _update_stage(work_dir, vid, 'download', 'running')

    safe_title = slugify(c['title'])
    vid_dir = os.path.join(work_dir, f"{safe_title}_{vid[:8]}")
    os.makedirs(vid_dir, exist_ok=True)
    out_tmpl = os.path.join(vid_dir, f"{safe_title}.%(ext)s")

    if format_id:
        format_sel = format_id
    elif quality == '4k':
        format_sel = 'bestvideo+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
    else:
        format_sel = 'bestvideo[height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]'

    def ydl_hook(d):
        if S['cancel_flag']:
            raise Exception("Download cancelled by user")
        if d['status'] == 'downloading':
            p = d.get('_percent_str', '0%').replace('%', '').strip()
            try:
                p = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', p)
                pct = float(p)
            except:
                pct = 0
            speed_raw = d.get('_speed_str') or ''
            speed_str = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', speed_raw).strip()
            report_progress(vid, pct, f"下载中... {speed_str}")
        elif d['status'] == 'finished':
            if not c.get('description'):
                desc = d.get('info_dict', {}).get('description', '')
                if desc:
                    c['description'] = desc
            report_progress(vid, 100, "下载完成")

    ydl_opts = {
        'format': format_sel,
        'outtmpl': out_tmpl,
        'progress_hooks': [ydl_hook],
        'proxy': cfg['app'].get('proxy'),
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'cookiefile': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'youtube_cookies.txt'),
        'merge_output_format': 'mp4',
        'writethumbnail': True,
        'convert_thumbnails': 'jpg',
        'ffmpeg_location': cfg['ffmpeg'].get('bin_path'),
        'js_runtimes': _js_runtimes(),
    }
    if with_subtitles:
        ydl_opts.update({
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['zh-Hans'],
            'subtitlesformat': 'vtt',
        })

    try:
        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([c['url']])
        except Exception as ydl_err:
            err_str = str(ydl_err)
            if 'subtitle' in err_str.lower():
                log_to_web('warning', f"字幕下载失败（已跳过）: {err_str}", vid)
            else:
                raise

        found_video = None
        expected = os.path.join(vid_dir, f"{safe_title}.mp4")
        if os.path.exists(expected) and os.path.getsize(expected) > 0:
            found_video = expected
        else:
            max_size = 0
            if os.path.exists(vid_dir):
                for f in os.listdir(vid_dir):
                    if f.endswith(('.mp4', '.mkv', '.mov', '.ts', '.flv')):
                        fpath = os.path.join(vid_dir, f)
                        fsize = os.path.getsize(fpath)
                        if fsize > max_size and fsize > 1 * 1024 * 1024:
                            max_size = fsize
                            found_video = fpath

        if found_video:
            c['local_path'] = found_video
            c['local_dir'] = vid_dir
            found_video = _ensure_audio(found_video, vid_dir, vid, c, cfg)
            c['local_path'] = found_video

            for f in os.listdir(vid_dir):
                if f.lower().endswith(('.jpg', '.png', '.webp', '.jpeg')) and 'original_thumbnail' not in c:
                    c['original_thumbnail'] = os.path.join(vid_dir, f)
                elif f.endswith('.zh-Hans.vtt') or f.endswith('.zh-Hans.ass'):
                    c['subtitle_zh'] = os.path.join(vid_dir, f)

            if with_subtitles and not c.get('subtitle_zh'):
                log_to_web('info', f"字幕未找到，尝试单独补下...", vid)
                sub_opts = {
                    'skip_download': True,
                    'outtmpl': out_tmpl,
                    'proxy': cfg['app'].get('proxy'),
                    'quiet': True,
                    'no_warnings': True,
                    'cookiefile': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'youtube_cookies.txt'),
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': ['zh-Hans'],
                    'subtitlesformat': 'vtt',
                    'ffmpeg_location': cfg['ffmpeg'].get('bin_path'),
                    'js_runtimes': _js_runtimes(),
                }
                try:
                    with YoutubeDL(sub_opts) as ydl:
                        ydl.download([c['url']])
                    for f in os.listdir(vid_dir):
                        if f.endswith('.zh-Hans.vtt') or f.endswith('.zh-Hans.ass'):
                            c['subtitle_zh'] = os.path.join(vid_dir, f)
                            log_to_web('info', f"补下字幕成功: {f}", vid)
                            break
                except Exception as sub_err:
                    log_to_web('warning', f"字幕补下失败（跳过）: {sub_err}", vid)

            with state_lock:
                S['downloaded'].append(c)
            _update_stage(work_dir, vid, 'download', 'done')
            log_to_web('info', f"成功下载并识别: {os.path.basename(found_video)}", vid)
            return c
        else:
            raise Exception("下载完成但未找到有效的视频文件(>1MB)")

    except Exception as e:
        err_str = str(e)
        if 'Sign in to confirm your age' in err_str or 'age-restricted' in err_str.lower():
            log_to_web('warning', f"⚠ 年龄限制视频，跳过下载: {vid}", vid)
            with state_lock:
                S['errors'].append({'id': vid, 'step': 'download', 'message': '年龄限制视频，无法下载'})
            _update_stage(work_dir, vid, 'download', 'failed')
        else:
            log_to_web('error', f"下载阶段异常: {err_str}", vid)
            with state_lock:
                S['errors'].append({'id': vid, 'step': 'download', 'message': err_str})
            log_to_web('error', f"下载失败: {err_str}", vid)
            _update_stage(work_dir, vid, 'download', 'failed')
        return None


def run_download(video_ids, auto_transcode=False, with_subtitles=True):
    try:
        update_state('downloading')
        cfg = load_config()

        for vid_entry in video_ids:
            if S['cancel_flag']: break
            entry = dict(vid_entry)
            entry['with_subtitles'] = with_subtitles
            _download_one(entry, cfg)

        update_state('download_done')
        if auto_transcode and S['downloaded']:
            log_to_web('info', '下载完成，自动开始转码...')
            run_transcode()
    except Exception as e:
        log_to_web('error', f"下载阶段崩溃: {str(e)}")
        update_state('scan_done')

def _transcode_one(c, processor, cfg):
    """Transcode one video. Returns True on success, False on failure."""
    vid = c['id']
    log_to_web('info', f"启动转码流程: {c['title']}", vid)
    _update_stage(cfg['app']['work_dir'], vid, 'transcode', 'running')

    safe_title = slugify(c['title'])
    vid_dir_name = f"{safe_title}_{vid[:8]}"
    video_data = {
        'id': vid,
        'filepath': c.get('local_path') or os.path.join(cfg['app']['work_dir'], vid_dir_name, f"{safe_title}.mp4"),
        'subtitle_zh': c.get('subtitle_zh', ''),
    }

    def transcode_progress(pct):
        report_progress(vid, pct, "转码中 (视频流处理)...")

    try:
        def check_cancel(): return S['cancel_flag']
        res = processor.process(video_data, cancel_check=check_cancel, progress_cb=transcode_progress)

        if res:
            expected_final = os.path.join(
                cfg['app']['work_dir'], vid_dir_name, f"{safe_title}_final.mp4"
            )
            if res != expected_final and os.path.isfile(res):
                os.replace(res, expected_final)
                res = expected_final

            if not os.path.isfile(res) or os.path.getsize(res) < 1024 * 1024:
                raise Exception(f"转码输出文件缺失或过小: {res}")
            with state_lock:
                if not any(x['id'] == c['id'] for x in S['transcoded']):
                    S['transcoded'].append(c)
            _queue_for_upload(c)
            _update_stage(cfg['app']['work_dir'], vid, 'transcode', 'done')
            report_progress(vid, 100, "转码完成")
            log_to_web('info', f"转码成功: {c['title']}", vid)
            return True
        else:
            raise Exception("计算后端返回失败")
    except Exception as e:
        with state_lock:
            S['errors'].append({'id': vid, 'step': 'transcode', 'message': str(e)})
        _update_stage(cfg['app']['work_dir'], vid, 'transcode', 'failed')
        log_to_web('error', f"转码失败: {str(e)}", vid)
        return False


def run_transcode():
    try:
        update_state('transcoding')
        cfg = load_config()
        from video_processor import VideoProcessor
        processor = VideoProcessor(cfg)

        for c in S['downloaded']:
            if S['cancel_flag']: break
            _transcode_one(c, processor, cfg)

        update_state('transcode_done')
    except Exception as e:
        log_to_web('error', f"转码阶段崩溃: {str(e)}")
        update_state('download_done')

def _translate_one(c, cfg, cover_proc, desc_prefix):
    """Translate title and description for one video. Updates video_meta in place."""
    vid = c['id']
    work_dir = cfg['app']['work_dir']

    _update_stage(work_dir, vid, 'translate', 'running')

    # ── 锁外执行所有耗时的 API 调用，收集结果 ──────────────────────────
    translated = None
    final_desc = None
    cover_text = None
    source_url = c.get('url', '')

    log_to_web('info', f"翻译标题: {c['title']}", vid)
    try:
        result = cover_proc.translate_title(c['title'])
        if result and result != c['title']:
            translated = result
            c['translated_title'] = translated
            log_to_web('info', f"翻译完成: {translated}", vid)
        else:
            log_to_web('warn', f"翻译结果为空或与原文相同，跳过", vid)
    except Exception as e:
        log_to_web('error', f"翻译失败: {e}", vid)

    log_to_web('info', f"翻译简介...", vid)
    try:
        original_desc = c.get('description', '')
        if original_desc:
            try:
                translated_desc = cover_proc.translate_description(original_desc)
            except Exception as e:
                log_to_web('warn', f"简介翻译失败，使用原文: {e}", vid)
                translated_desc = original_desc
            final_desc = f"{translated_desc}\n\n{desc_prefix.replace('{youtube_url}', source_url)}"
            final_desc = _truncate_at_sentence(final_desc, 2000)
            log_to_web('info', f"简介处理完成 ({len(final_desc)} 字)", vid)
    except Exception as e:
        log_to_web('error', f"简介处理失败: {e}", vid)

    translated_title = translated or c.get('title', '')
    if translated_title:
        try:
            cover_text = cover_proc.get_summary(translated_title)
            if cover_text:
                log_to_web('info', f"封面文字: {cover_text}", vid)
        except Exception as e:
            log_to_web('warn', f"封面文字生成失败: {e}", vid)

    # ── 一次加锁，统一写盘 ────────────────────────────────────────────
    with _meta_lock:
        meta = _load_video_meta(work_dir)
        if vid in meta:
            if translated:
                meta[vid]['title'] = translated
            if final_desc:
                meta[vid]['desc'] = final_desc
            elif not c.get('description') and not meta[vid].get('desc'):
                # 无原始描述且无已存简介，写默认前缀
                default_desc = desc_prefix.replace('{youtube_url}', source_url)
                default_desc = _truncate_at_sentence(default_desc, 2000)
                if default_desc:
                    meta[vid]['desc'] = default_desc
                    final_desc = default_desc
            if cover_text:
                meta[vid]['cover_text'] = cover_text
            _save_video_meta(work_dir, meta)

    # 同步内存
    with state_lock:
        if vid in S['video_meta']:
            if translated:
                S['video_meta'][vid]['title'] = translated
            if final_desc:
                S['video_meta'][vid]['desc'] = final_desc
            if cover_text:
                S['video_meta'][vid]['cover_text'] = cover_text

    _update_stage(work_dir, vid, 'translate', 'done')



def run_translate(vids=None):
    """翻译 S['transcoded'] 中的标题并写入 video_meta.json。
    vids=None 翻译全部，否则只翻译指定 vid 列表。"""
    try:
        update_state('translating')
        cfg = load_config()
        from cover_processor import CoverProcessor
        cover_proc = CoverProcessor(cfg)
        desc_prefix = cfg['bilibili'].get('desc_prefix', '')

        targets = [c for c in S['transcoded'] if vids is None or c['id'] in vids]
        for c in targets:
            if S['cancel_flag']: break
            _translate_one(c, cfg, cover_proc, desc_prefix)

        update_state('translate_done')
    except Exception as e:
        log_to_web('error', f"翻译阶段崩溃: {str(e)}")
        update_state('transcode_done')

def _do_upload_single(vid, c, uploader, cfg):
    """Upload one video and update video_meta.json on success. Returns True/False."""
    work_dir = cfg['app']['work_dir']
    meta = S.get('video_meta', {}).get(vid, {})
    upload_title = meta.get('title') or c.get('translated_title') or c['title']
    tid_override = int(meta['tid']) if meta.get('tid') else None
    tags_raw = meta.get('tags', [])
    if isinstance(tags_raw, list):
        tags_override = [t.strip() for t in tags_raw if str(t).strip()] or None
    else:
        tags_override = [t.strip() for t in str(tags_raw).split(',') if t.strip()] or None
    dtime_override = None
    schedule_time_raw = meta.get('schedule_time')
    if schedule_time_raw:
        try:
            if isinstance(schedule_time_raw, (int, float)):
                # Unix timestamp (seconds) — preferred format, no timezone ambiguity
                dtime_override = int(schedule_time_raw)
            else:
                # Legacy ISO string (naive local time) — fromisoformat gives naive datetime,
                # .timestamp() converts using local timezone (correct on same machine)
                dt = datetime.fromisoformat(str(schedule_time_raw))
                dtime_override = int(dt.timestamp())
        except Exception as e:
            log_to_web('warn', f"无法解析定时时间 '{schedule_time_raw}': {e}", vid)
    copyright_override = int(meta['copyright']) if meta.get('copyright') in (1, 2, '1', '2') else None
    source_override = meta.get('source') or None
    desc_override = meta.get('desc') or None
    cover_text = meta.get('cover_text') or None

    file_path = meta.get('local_path') or ''
    if not file_path or not os.path.isfile(file_path):
        safe_title = slugify(c['title'])
        vid_dir = os.path.join(work_dir, f"{safe_title}_{vid[:8]}")
        file_path = os.path.join(vid_dir, f"{safe_title}_final.mp4")

    thumb_path = meta.get('original_thumbnail') or None
    if not thumb_path:
        safe_title = slugify(c['title'])
        vid_dir = os.path.join(work_dir, f"{safe_title}_{vid[:8]}")
        for ext in ['jpg', 'png', 'webp', 'jpeg']:
            t = os.path.join(vid_dir, f"{safe_title}.{ext}")
            if os.path.exists(t):
                thumb_path = t
                break

    def upload_progress(pct, msg):
        report_progress(vid, pct, msg)

    res = uploader.upload(
        file_path, upload_title, c['url'],
        original_thumbnail=thumb_path,
        original_description=c.get('description', ''),
        progress_callback=upload_progress,
        tid_override=tid_override,
        tags_override=tags_override,
        dtime_override=dtime_override,
        title_already_translated='translated_title' in c,
        copyright_override=copyright_override,
        source_override=source_override,
        desc_override=desc_override,
        cover_text=cover_text,
        cancel_check=lambda: S['cancel_flag']
    )
    if res:
        with state_lock:
            S['uploaded'].append(c)
        add_history(vid)
        with _meta_lock:
            um = _load_video_meta(work_dir)
            if vid in um:
                um[vid]['uploaded'] = True
                um[vid]['uploaded_at'] = int(time.time())
                if 'stages' not in um[vid]:
                    um[vid]['stages'] = {}
                um[vid]['stages']['upload'] = {'status': 'done', 'at': int(time.time())}
                _save_video_meta(work_dir, um)
        with state_lock:
            S['video_meta'].pop(vid, None)
            S['transcoded'] = [x for x in S['transcoded'] if x['id'] != vid]
        log_to_web('info', f"B站上传成功: {c['title']}", vid)
        update_state()
    return res

def run_upload():
    try:
        update_state('uploading')
        cfg = load_config()
        uploader = BilibiliUploader(cfg)
        upload_interval = cfg['bilibili'].get('upload_interval', 30)
        uploaded_count = 0

        for c in list(S['transcoded']):
            if S['cancel_flag']: break
            vid = c['id']
            log_to_web('info', f"准备上传至 B站: {c['title']}", vid)
            try:
                res = _do_upload_single(vid, c, uploader, cfg)
                if res:
                    uploaded_count += 1
                    if upload_interval > 0 and not S['cancel_flag']:
                        log_to_web('info', f"等待 {upload_interval} 秒后继续下一个上传...")
                        time.sleep(upload_interval)
                else:
                    raise Exception("上传器返回失败状态")
            except Exception as e:
                with state_lock:
                    S['errors'].append({'id': vid, 'step': 'upload', 'message': str(e)})
                log_to_web('error', f"上传失败: {str(e)}", vid)

        update_state('done')
    except Exception as e:
        log_to_web('error', f"上传阶段崩溃: {str(e)}")
        update_state('transcode_done')

# ── Pipeline Workers ──────────────────────────────────────────────────────────

def _pipeline_worker_download(in_q, out_q):
    cfg = load_config()
    while True:
        item = in_q.get()
        if item is None:
            out_q.put(None)
            break
        if S['cancel_flag']:
            out_q.put(None)
            break
        c = _download_one(item, cfg)
        with state_lock:
            if c:
                S['pipeline_counts']['download']['done'] += 1
            else:
                S['pipeline_counts']['download']['failed'] += 1
        update_state()
        if c:
            out_q.put(c)


def _pipeline_worker_transcode(in_q, out_q):
    cfg = load_config()
    from video_processor import VideoProcessor
    processor = VideoProcessor(cfg)
    while True:
        item = in_q.get()
        if item is None:
            out_q.put(None)
            break
        if S['cancel_flag']:
            out_q.put(None)
            break
        ok = _transcode_one(item, processor, cfg)
        with state_lock:
            if ok:
                S['pipeline_counts']['transcode']['done'] += 1
            else:
                S['pipeline_counts']['transcode']['failed'] += 1
        update_state()
        if ok:
            out_q.put(item)


def _pipeline_worker_translate(in_q, out_q):
    cfg = load_config()
    from cover_processor import CoverProcessor
    cover_proc = CoverProcessor(cfg)
    desc_prefix = cfg['bilibili'].get('desc_prefix', '')
    while True:
        item = in_q.get()
        if item is None:
            out_q.put(None)
            break
        if S['cancel_flag']:
            out_q.put(None)
            break
        _translate_one(item, cfg, cover_proc, desc_prefix)
        with state_lock:
            S['pipeline_counts']['translate']['done'] += 1
        update_state()
        if item.get('auto_upload', False):
            out_q.put(item)


def _pipeline_worker_upload(in_q):
    cfg = load_config()
    uploader = BilibiliUploader(cfg)
    upload_interval = cfg['bilibili'].get('upload_interval', 30)
    while True:
        item = in_q.get()
        if item is None:
            break
        if S['cancel_flag']:
            break
        vid = item['id']
        log_to_web('info', f"准备上传至 B站: {item['title']}", vid)
        try:
            res = _do_upload_single(vid, item, uploader, cfg)
            if res:
                with state_lock:
                    S['pipeline_counts']['upload']['done'] += 1
                if upload_interval > 0 and not S['cancel_flag']:
                    log_to_web('info', f"等待 {upload_interval} 秒后继续下一个上传...")
                    time.sleep(upload_interval)
            else:
                with state_lock:
                    S['pipeline_counts']['upload']['failed'] += 1
                    S['errors'].append({'id': vid, 'step': 'upload', 'message': '上传器返回失败状态'})
        except Exception as e:
            with state_lock:
                S['pipeline_counts']['upload']['failed'] += 1
                S['errors'].append({'id': vid, 'step': 'upload', 'message': str(e)})
            log_to_web('error', f"上传失败: {str(e)}", vid)
        update_state()


def _prevent_sleep():
    import ctypes
    ES_CONTINUOUS       = 0x80000000
    ES_SYSTEM_REQUIRED  = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    )

def _allow_sleep():
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS only

_mouse_jiggle_stop = None

def _start_mouse_jiggle():
    """每60秒移动鼠标1像素再移回来，防止企业版组策略强制自动锁屏。"""
    global _mouse_jiggle_stop
    import ctypes
    stop_event = threading.Event()
    _mouse_jiggle_stop = stop_event

    def jiggle():
        class POINT(ctypes.Structure):
            _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]
        while not stop_event.wait(60):
            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            ctypes.windll.user32.SetCursorPos(pt.x + 1, pt.y)
            ctypes.windll.user32.SetCursorPos(pt.x, pt.y)

    t = threading.Thread(target=jiggle, daemon=True)
    t.start()

def _stop_mouse_jiggle():
    global _mouse_jiggle_stop
    if _mouse_jiggle_stop:
        _mouse_jiggle_stop.set()
        _mouse_jiggle_stop = None


def run_pipeline(video_ids):
    _prevent_sleep()
    _start_mouse_jiggle()
    try:
        update_state('pipeline')
        with state_lock:
            S['pipeline_active'] = True
            S['pipeline_auto_upload'] = any(v.get('auto_upload', False) for v in video_ids)
            S['pipeline_counts'] = {
                'download':  {'queued': len(video_ids), 'done': 0, 'failed': 0},
                'transcode': {'queued': 0, 'done': 0, 'failed': 0},
                'translate': {'queued': 0, 'done': 0, 'failed': 0},
                'upload':    {'queued': 0, 'done': 0, 'failed': 0},
            }

        global _pipeline_download_q
        download_q = Queue()
        _pipeline_download_q = download_q
        transcode_q = Queue()
        translate_q = Queue()
        upload_q = Queue()

        threads = [
            threading.Thread(target=_pipeline_worker_download,  args=(download_q, transcode_q), daemon=True),
            threading.Thread(target=_pipeline_worker_transcode, args=(transcode_q, translate_q), daemon=True),
            threading.Thread(target=_pipeline_worker_translate, args=(translate_q, upload_q),   daemon=True),
            threading.Thread(target=_pipeline_worker_upload,    args=(upload_q,),               daemon=True),
        ]
        for t in threads:
            t.start()

        for vid_entry in video_ids:
            download_q.put(vid_entry)
        download_q.put(None)

        for t in threads:
            t.join()

        with state_lock:
            S['pipeline_active'] = False
        update_state('pipeline_done')
        log_to_web('info', "流水线全部完成！")
    except Exception as e:
        with state_lock:
            S['pipeline_active'] = False
        log_to_web('error', f"流水线崩溃: {str(e)}")
        update_state('scan_done')
    finally:
        _pipeline_download_q = None
        _allow_sleep()
        _stop_mouse_jiggle()

# ── History Persistence ──────────────────────────────────────────────────────
HISTORY_FILE = 'history.json'
def get_history():
    """返回已上传的 vid 集合：合并 history.json 和 video_meta 中 upload.status==done 的记录。"""
    try:
        with open(HISTORY_FILE, 'r') as f:
            h = set(json.load(f))
    except:
        h = set()
    # 补充 video_meta 中明确标记 upload done 的记录
    try:
        cfg = load_config()
        meta = _load_video_meta(cfg['app']['work_dir'])
        for vid, m in meta.items():
            if m.get('stages', {}).get('upload', {}).get('status') == 'done':
                h.add(vid)
            elif m.get('uploaded'):  # 兼容旧格式
                h.add(vid)
    except:
        pass
    return list(h)

def add_history(vid):
    with history_lock:
        try:
            with open(HISTORY_FILE, 'r') as f:
                h = json.load(f)
        except:
            h = []
        if vid not in h:
            h.append(vid)
            with open(HISTORY_FILE, 'w') as f: json.dump(h, f)

# ── API Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({k: S[k] for k in S if k != 'current_task'})

@app.route('/api/scan', methods=['POST'])
def trigger_scan():
    with state_lock:
        if S['status'] in ['scanning', 'pipeline']:
            return jsonify({'error': 'Pipeline busy'}), 400
        S['cancel_flag'] = False
        S['current_task'] = threading.Thread(target=run_scan)
        S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/download', methods=['POST'])
def trigger_download():
    data = request.json or {}
    video_ids = data.get('video_ids', []) # Expected list of {id, quality}
    auto_transcode = bool(data.get('auto_transcode', False))
    with_subtitles = bool(data.get('with_subtitles', True))
    if not video_ids: return jsonify({'error': 'No IDs provided'}), 400
    with state_lock:
        if S['status'] != 'scan_done': return jsonify({'error': 'Wrong state'}), 400
        S['cancel_flag'] = False
        S['current_task'] = threading.Thread(target=run_download, args=(video_ids, auto_transcode, with_subtitles))
        S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/transcode', methods=['POST'])
def trigger_transcode():
    with state_lock:
        if S['status'] != 'download_done': return jsonify({'error': 'Wrong state'}), 400
        S['cancel_flag'] = False
        S['current_task'] = threading.Thread(target=run_transcode)
        S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/video_meta/save', methods=['POST'])
def save_video_meta():
    data = request.json or {}
    incoming = data.get('meta', {})  # {vid: {title, tid, tags, ...}}
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    EDITABLE = {'title', 'tid', 'tags', 'copyright', 'source', 'schedule_time', 'desc', 'cover_text'}
    with _meta_lock:
        disk_meta = _load_video_meta(work_dir)
        for vid, fields in incoming.items():
            if vid in disk_meta:
                for k, v in fields.items():
                    if k in EDITABLE:
                        disk_meta[vid][k] = v
            else:
                disk_meta[vid] = fields
        _save_video_meta(work_dir, disk_meta)
    with state_lock:
        for vid, fields in incoming.items():
            if vid in S['video_meta']:
                for k, v in fields.items():
                    if k in EDITABLE:
                        S['video_meta'][vid][k] = v
    return jsonify({'ok': True})

@app.route('/api/video_meta/<vid>', methods=['DELETE'])
def delete_video_meta(vid):
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    source_url = None
    with _meta_lock:
        meta = _load_video_meta(work_dir)
        if vid in meta:
            source_url = meta[vid].get('url') or meta[vid].get('source')
            del meta[vid]
            _save_video_meta(work_dir, meta)

    if source_url:
        with _config_lock:
            cfg2 = load_config()
            sources = cfg2['youtube'].get('sources', [])
            new_sources = [s for s in sources if s['url'] != source_url]
            if len(new_sources) < len(sources):
                cfg2['youtube']['sources'] = new_sources
                save_config(cfg2)

    with state_lock:
        S['video_meta'].pop(vid, None)
        S['transcoded'] = [x for x in S['transcoded'] if x['id'] != vid]
        S['downloaded']  = [x for x in S['downloaded']  if x['id'] != vid]
        S['candidates']  = [x for x in S['candidates']  if x['id'] != vid]
    update_state()
    return jsonify({'ok': True, 'source_removed': source_url is not None})

@app.route('/api/video_meta/<vid>/stages', methods=['POST'])
def update_stages(vid):
    """手动更新某视频的阶段状态。body: {stage: 'transcode', status: 'pending'}"""
    data = request.json or {}
    stage = data.get('stage')
    status = data.get('status')
    VALID_STAGES = {'scan', 'download', 'transcode', 'translate', 'upload'}
    VALID_STATUS = {'pending', 'done', 'failed', 'skipped'}
    if stage not in VALID_STAGES or status not in VALID_STATUS:
        return jsonify({'error': 'Invalid stage or status'}), 400
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    with _meta_lock:
        meta = _load_video_meta(work_dir)
        if vid not in meta:
            return jsonify({'error': 'Video not found'}), 404
        if 'stages' not in meta[vid]:
            meta[vid]['stages'] = {}
        meta[vid]['stages'][stage] = {'status': status, 'at': int(time.time())}
        if stage == 'upload':
            meta[vid]['uploaded'] = (status == 'done')
            if status == 'done' and not meta[vid].get('uploaded_at'):
                meta[vid]['uploaded_at'] = int(time.time())
        _save_video_meta(work_dir, meta)
    with state_lock:
        if vid in S['video_meta']:
            if 'stages' not in S['video_meta'][vid]:
                S['video_meta'][vid]['stages'] = {}
            S['video_meta'][vid]['stages'][stage] = meta[vid]['stages'][stage]
    update_state()
    return jsonify({'ok': True})

@app.route('/api/video_meta/<vid>/done', methods=['POST'])
def mark_upload_done(vid):
    """Manually mark a video as uploaded (for videos uploaded outside the app)."""
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    with _meta_lock:
        meta = _load_video_meta(work_dir)
        if vid in meta:
            meta[vid]['uploaded'] = True
            meta[vid]['uploaded_at'] = int(time.time())
            _save_video_meta(work_dir, meta)
    add_history(vid)
    with state_lock:
        S['video_meta'].pop(vid, None)
        S['transcoded'] = [x for x in S['transcoded'] if x['id'] != vid]
    update_state()
    return jsonify({'ok': True})

@app.route('/api/video_meta/rescan', methods=['POST'])
def rescan_upload_queue():
    """Scan data/ dir for _final.mp4 files not yet in video_meta.json and add them."""
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    if not os.path.isdir(work_dir):
        return jsonify({'added': 0})

    scan_cache = _load_scan_cache(work_dir)
    added = 0
    dirty = False

    with _meta_lock:
        meta = _load_video_meta(work_dir)

        # 先用 history.json 校正 uploaded 字段，防止并发写盘导致的字段丢失
        hist_ids = get_history()
        for vid, m in meta.items():
            if vid in hist_ids and not m.get('uploaded'):
                meta[vid]['uploaded'] = True
                dirty = True

        for entry in os.scandir(work_dir):
            if not entry.is_dir(): continue
            name = entry.name
            if len(name) < 10 or name[-9] != '_': continue
            vid8 = name[-8:]
            safe_title = name[:-9]
            vid_dir = entry.path
            final_path = os.path.join(vid_dir, f"{safe_title}_final.mp4")
            if not os.path.isfile(final_path) or os.path.getsize(final_path) < 1024 * 1024:
                continue

            matched_vid = next((v for v in scan_cache if v[:8] == vid8), None)
            vid = matched_vid or vid8

            if vid in meta:
                # 超5天未上传，自动标记已上传
                m = meta[vid]
                if not m.get('uploaded'):
                    queued_at = m.get('queued_at') or 0
                    if queued_at and (time.time() - queued_at) > 5 * 24 * 3600:
                        meta[vid]['uploaded'] = True
                        meta[vid]['uploaded_at'] = int(time.time())
                        dirty = True
                        logging.info(f"rescan_upload_queue: {vid} 入队超5天未上传，自动标记已上传")
                continue  # Already in queue (uploaded or pending)

            orig_title = scan_cache.get(vid, {}).get('title') or safe_title
            thumb = None
            for ext in ['webp', 'jpg', 'jpeg', 'png']:
                t = os.path.join(vid_dir, f"{safe_title}.{ext}")
                if os.path.exists(t): thumb = t; break

            meta[vid] = {
                'title': orig_title,
                'original_title': orig_title,
                'tid': cfg['bilibili'].get('tid', 122),
                'tags': list(cfg['bilibili'].get('default_tags', [])),
                'copyright': 1,
                'source': f'https://www.youtube.com/watch?v={vid}',
                'schedule_time': None,
                'uploaded': False,
                'local_path': final_path,
                'original_thumbnail': thumb,
                'url': f'https://www.youtube.com/watch?v={vid}',
                'queued_at': int(os.path.getmtime(final_path)),
                'stages': {
                    'scan':      {'status': 'done',    'at': int(os.path.getmtime(final_path))},
                    'download':  {'status': 'done',    'at': int(os.path.getmtime(final_path))},
                    'transcode': {'status': 'done',    'at': int(os.path.getmtime(final_path))},
                    'translate': {'status': 'pending', 'at': None},
                    'upload':    {'status': 'pending', 'at': None},
                },
            }
            added += 1

        if added or dirty:
            _save_video_meta(work_dir, meta)

    # 每次都重建 S['transcoded']，确保过期/新增条目都反映到内存
    restored = []
    for vid, m in meta.items():
        if m.get('uploaded'): continue
        lp = m.get('local_path') or ''
        if not lp or not os.path.isfile(lp): continue
        restored.append({
            'id': vid,
            'title': m.get('original_title') or m.get('title', ''),
            'translated_title': m.get('title', ''),
            'description': '',
            'url': m.get('url', f'https://www.youtube.com/watch?v={vid}'),
            'url_type': 'video',
            'already_downloaded': True,
            'formats': [],
            'rec_format_id': None,
            'local_path': lp,
            'local_dir': os.path.dirname(lp),
            'original_thumbnail': m.get('original_thumbnail'),
        })
    with state_lock:
        S['transcoded'] = restored
        S['video_meta'] = {v: m for v, m in meta.items() if not m.get('uploaded')}
    update_state()
    logging.info(f"rescan_upload_queue: added {added} new videos, transcoded={len(restored)}.")

    return jsonify({'added': added})

@app.route('/api/translate', methods=['POST'])
def trigger_translate():
    with state_lock:
        if S['status'] not in ('transcode_done', 'translate_done', 'pipeline_done'):
            return jsonify({'error': 'Wrong state'}), 400
        S['cancel_flag'] = False
        S['current_task'] = threading.Thread(target=run_translate)
        S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/translate/<vid>', methods=['POST'])
def trigger_translate_single(vid):
    """重新翻译单条视频标题，不影响全局状态。"""
    def worker():
        cfg = load_config()
        work_dir = cfg['app']['work_dir']
        from cover_processor import CoverProcessor
        cover_proc = CoverProcessor(cfg)
        c = next((x for x in S['transcoded'] if x['id'] == vid), None)
        if not c:
            log_to_web('error', f"单条重翻: vid {vid} 不在转码列表", vid)
            return
        log_to_web('info', f"重新翻译: {c['title']}", vid)
        try:
            translated = cover_proc.translate_title(c['title'])
            if translated and translated != c['title']:
                c['translated_title'] = translated
                with _meta_lock:
                    meta = _load_video_meta(work_dir)
                    if vid in meta:
                        meta[vid]['title'] = translated
                        _save_video_meta(work_dir, meta)
                with state_lock:
                    if vid in S['video_meta']:
                        S['video_meta'][vid]['title'] = translated
                update_state()
                log_to_web('info', f"重翻完成: {translated}", vid)
            else:
                log_to_web('warn', f"翻译结果为空或与原文相同", vid)
        except Exception as e:
            log_to_web('error', f"重翻失败: {e}", vid)
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/upload', methods=['POST'])
def trigger_upload():
    data = request.json or {}
    with state_lock:
        if S['status'] not in ('transcode_done', 'translate_done', 'pipeline_done'): return jsonify({'error': 'Wrong state'}), 400
        S['video_meta'].update(data.get('meta', {}))
        S['cancel_flag'] = False
        S['current_task'] = threading.Thread(target=run_upload)
        S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/upload/<vid>', methods=['POST'])
def trigger_upload_single(vid):
    """Upload a single video immediately, independent of pipeline state."""
    c = next((x for x in S['transcoded'] if x['id'] == vid), None)
    if not c:
        return jsonify({'error': 'Video not in transcoded list'}), 404

    def worker():
        cfg = load_config()
        uploader = BilibiliUploader(cfg)
        log_to_web('info', f"单独上传: {c['title']}", vid)
        try:
            res = _do_upload_single(vid, c, uploader, cfg)
            if not res:
                with state_lock:
                    S['errors'].append({'id': vid, 'step': 'upload', 'message': '上传器返回失败状态'})
                log_to_web('error', f"单独上传失败: {c['title']}", vid)
        except Exception as e:
            with state_lock:
                S['errors'].append({'id': vid, 'step': 'upload', 'message': str(e)})
            log_to_web('error', f"单独上传异常: {str(e)}", vid)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/pipeline/start', methods=['POST'])
def trigger_pipeline():
    data = request.json or {}
    video_ids = data.get('video_ids', [])
    if not video_ids:
        return jsonify({'error': 'No video IDs provided'}), 400
    with state_lock:
        if S['status'] not in ('scan_done', 'pipeline_done'):
            return jsonify({'error': 'Wrong state'}), 400
        S['cancel_flag'] = False
        S['current_task'] = threading.Thread(
            target=run_pipeline, args=(video_ids,), daemon=True
        )
        S['current_task'].start()
    return jsonify({'ok': True})


@app.route('/api/pipeline/add', methods=['POST'])
def pipeline_add():
    """动态向正在运行的流水线追加视频。body: {video_ids: [{id, ...}, ...]}"""
    if not S.get('pipeline_active') or _pipeline_download_q is None:
        return jsonify({'error': 'Pipeline not running'}), 400
    data = request.json or {}
    video_ids = data.get('video_ids', [])
    if not video_ids:
        return jsonify({'error': 'No video IDs provided'}), 400
    for vid_entry in video_ids:
        _pipeline_download_q.put(vid_entry)
        with state_lock:
            S['pipeline_counts']['download']['queued'] += 1
    update_state()
    return jsonify({'ok': True, 'added': len(video_ids)})


@app.route('/api/prescan_meta/<vid>', methods=['POST'])
def prescan_meta(vid):
    """扫描阶段保存定时/元数据预设，下载开始前调用。"""
    data = request.json or {}
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    with _meta_lock:
        meta = _load_video_meta(work_dir)
        if vid not in meta:
            c = next((x for x in S['candidates'] if x['id'] == vid), {})
            meta[vid] = {
                'title': c.get('title', ''),
                'original_title': c.get('title', ''),
                'tid': cfg['bilibili'].get('tid', 122),
                'tags': list(cfg['bilibili'].get('default_tags', [])),
                'copyright': 1,
                'source': c.get('url', ''),
                'schedule_time': None,
                'uploaded': False,
                'local_path': None,
                'url': c.get('url', ''),
                'stages': {
                    'scan':      {'status': 'done',    'at': int(time.time())},
                    'download':  {'status': 'pending', 'at': None},
                    'transcode': {'status': 'pending', 'at': None},
                    'translate': {'status': 'pending', 'at': None},
                    'upload':    {'status': 'pending', 'at': None},
                },
            }
        for key in ('schedule_time', 'tid', 'tags', 'copyright', 'cover_text'):
            if key in data:
                meta[vid][key] = data[key]
        _save_video_meta(work_dir, meta)
    with state_lock:
        S['video_meta'][vid] = meta[vid]
    return jsonify({'ok': True})


@app.route('/api/reset', methods=['POST'])
def trigger_reset():
    reset_pipeline()
    update_state()
    return jsonify({'ok': True})

@app.route('/api/cancel', methods=['POST'])
def trigger_cancel():
    with state_lock:
        S['cancel_flag'] = True
    log_to_web('warn', "任务取消请求已发出...")
    return jsonify({'ok': True})

@app.route('/api/retry', methods=['POST'])
def trigger_retry():
    data = request.json or {}
    vid = data.get('video_id')
    if not vid: return jsonify({'error': 'Missing video_id'}), 400
    
    # Identify the failed step — normalize _retry suffix back to base step
    err_entry = next((e for e in S['errors'] if e['id'] == vid), None)
    failed_step = err_entry['step'] if err_entry else 'unknown'
    # Strip _retry suffix so retries of retries still map correctly
    failed_step = failed_step.replace('_retry', '')
    
    # Remove from errors
    with state_lock:
        S['errors'] = [e for e in S['errors'] if e['id'] != vid]
    
    video_entry = next((c for c in S['candidates'] if c['id'] == vid), None)
    if not video_entry:
        video_entry = next((c for x in [S['downloaded'], S['transcoded'], S['uploaded']] for c in x if c['id'] == vid), None)
    
    if not video_entry:
        return jsonify({'error': 'Video not found'}), 404
        
    log_to_web('info', f"重试任务 ({failed_step}): {video_entry['title']}", vid)
    
    def retry_worker():
        try:
            cfg = load_config()
            # 1. Retry DOWNLOAD
            if failed_step == 'download':
                run_download([{'id': vid, 'quality': video_entry.get('quality', '1080p')}])
            
            # 2. Retry TRANSCODE
            elif failed_step == 'transcode':
                from video_processor import VideoProcessor
                processor = VideoProcessor(cfg)
                safe_title = slugify(video_entry['title'])
                src_path = os.path.join(cfg['app']['work_dir'], f"{safe_title}_{vid[:8]}", f"{safe_title}.mp4")
                v_data = {'id': vid, 'filepath': src_path}
                def pb(p): report_progress(vid, p, "重试转码中...")
                if processor.process(v_data, cancel_check=lambda: S['cancel_flag'], progress_cb=pb):
                    with state_lock:
                        if video_entry not in S['transcoded']: S['transcoded'].append(video_entry)
                    report_progress(vid, 100, "转码完成")
                    with state_lock:
                        has_errors = bool(S['errors'])
                    if not has_errors: update_state('transcode_done')
                else: raise Exception("转码逻辑失败")

            # 3. Retry UPLOAD
            elif failed_step in ['upload', 'transcode_retry']:
                from bili_uploader import BilibiliUploader
                uploader = BilibiliUploader(cfg)
                safe_title = slugify(video_entry['title'])
                vid_dir = os.path.join(cfg['app']['work_dir'], f"{safe_title}_{vid[:8]}")
                final_mp4 = os.path.join(vid_dir, f"{safe_title}_final.mp4")
                
                # Check for thumbnail
                thumb = None
                for ext in ['jpg', 'png', 'webp', 'jpeg']:
                    tf = os.path.join(vid_dir, f"{safe_title}.{ext}")
                    if os.path.exists(tf): thumb = tf; break
                def up_pb(p, s): report_progress(vid, p, s)
                if uploader.upload(final_mp4, video_entry['title'], video_entry['url'],
                                   original_thumbnail=thumb,
                                   original_description=video_entry.get('description', ''),
                                   progress_callback=up_pb):
                    with state_lock:
                        if video_entry not in S['uploaded']: S['uploaded'].append(video_entry)
                    report_progress(vid, 100, "上传完成")
                    add_history(vid)
                    with state_lock:
                        has_errors = bool(S['errors'])
                    if not has_errors: update_state('done')
                else: raise Exception("再次投递失败")
            else:
                log_to_web('warn', f"未知的失败状态 '{failed_step}'，无法自动重试", vid)
        except Exception as e:
            with state_lock:
                S['errors'].append({'id': vid, 'step': f"{failed_step}_retry", 'message': str(e)})
            log_to_web('error', f"重试失败: {str(e)}", vid)
            
    threading.Thread(target=retry_worker, daemon=True).start()
    return jsonify({'ok': True})

# ── Jump API ──────────────────────────────────────────────────────────────────
# Maps each clickable step → the state BEFORE that step (so user can re-run it)
# Clicking "translate" means "go back to before translate, let me run it again"
_JUMP_TARGET = {
    'scan':     'idle',
    'pipeline': 'scan_done',
}

@app.route('/api/jump/<step>', methods=['POST'])
def jump_to_step(step):
    if step not in _JUMP_TARGET:
        return jsonify({'error': f'Unknown step: {step}'}), 400
    with state_lock:
        if S['status'].endswith('ing'):
            return jsonify({'error': 'Task is running, cannot jump'}), 409
        target = _JUMP_TARGET[step]
        # Don't demote to scan_done if transcoded videos still exist
        if target == 'scan_done' and S.get('transcoded'):
            target = 'pipeline_done'
        S['status'] = target
    broadcast('state', {k: S[k] for k in S if k != 'current_task'})
    return jsonify({'ok': True, 'status': target})

# ── Config API ───────────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
def get_config():
    cfg = load_config()
    return jsonify({
        'proxy': cfg['app'].get('proxy', ''),
        'tid': cfg['bilibili'].get('tid', 122),
        'intro_path': cfg['ffmpeg'].get('intro_video_path', ''),
        'desc_prefix': cfg['bilibili'].get('desc_prefix', ''),
        'zhipu_key': cfg.get('zhipu', {}).get('api_key', ''),
        'default_tags': cfg['bilibili'].get('default_tags', []),
        'upload_interval': cfg['bilibili'].get('upload_interval', 30),
        'bili_check_similarity': cfg['bilibili'].get('bili_check_similarity', 0.75),
    })

@app.route('/api/config', methods=['POST'])
def set_config():
    data = request.json or {}
    with _config_lock:
        cfg = load_config()
        if 'proxy' in data: cfg['app']['proxy'] = data['proxy']
        if 'tid' in data: cfg['bilibili']['tid'] = int(data['tid'])
        if 'intro_path' in data: cfg['ffmpeg']['intro_video_path'] = data['intro_path']
        if 'desc_prefix' in data: cfg['bilibili']['desc_prefix'] = data['desc_prefix']
        if 'zhipu_key' in data:
            if 'zhipu' not in cfg: cfg['zhipu'] = {}
            cfg['zhipu']['api_key'] = data['zhipu_key']
        if 'default_tags' in data:
            cfg['bilibili']['default_tags'] = [t.strip() for t in data['default_tags'] if t.strip()]
        if 'upload_interval' in data:
            cfg['bilibili']['upload_interval'] = max(0, int(data['upload_interval']))
        if 'bili_check_similarity' in data:
            val = float(data['bili_check_similarity'])
            cfg['bilibili']['bili_check_similarity'] = max(0.5, min(1.0, val))
        save_config(cfg)
    return jsonify({'ok': True})

# ── Sources API ──────────────────────────────────────────────────────────────

@app.route('/api/sources', methods=['GET'])
def get_sources():
    cfg = load_config()
    return jsonify(cfg['youtube']['sources'])

@app.route('/api/sources', methods=['POST'])
def add_source():
    data = request.json or {}
    url = data.get('url')
    if not url: return jsonify({'error': 'Missing URL'}), 400

    with _config_lock:
        cfg = load_config()
        if any(s['url'] == url for s in cfg['youtube']['sources']):
            return jsonify({'error': 'Already exists'}), 400

    # Full scan outside the lock (may take several seconds)
    cfg = load_config()
    title = url
    new_candidates = []
    try:
        from yt_dlp import YoutubeDL
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'proxy': cfg['app'].get('proxy'),
            'js_runtimes': _js_runtimes(),
            'cookiefile': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'youtube_cookies.txt'),
            'playlistend': 30,
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', url)
            entries = info.get('entries', [info])
            work_dir = cfg['app']['work_dir']
            scan_cache = _load_scan_cache(work_dir)
            now = int(time.time())
            for e in entries:
                if not e: continue
                vid = e.get('id')
                if not vid: continue
                all_formats = []
                for f in e.get('formats', []):
                    if f.get('acodec') == 'none' and f.get('vcodec') == 'none': continue
                    all_formats.append({
                        'format_id': f.get('format_id'),
                        'ext': f.get('ext'),
                        'resolution': f.get('resolution') or (f"{f.get('width')}x{f.get('height')}" if f.get('width') else 'audio only'),
                        'filesize': f.get('filesize') or f.get('filesize_approx') or 0,
                        'vcodec': f.get('vcodec', 'none'),
                        'acodec': f.get('acodec', 'none'),
                        'abr': f.get('abr', 0),
                        'vbr': f.get('vbr', 0),
                        'note': f.get('format_note', ''),
                        'is_thumbnail': f.get('ext') in ['webp', 'jpg', 'jpeg', 'png'],
                    })
                all_formats.sort(key=lambda x: (x['vcodec'] != 'none' and x['acodec'] != 'none', x['resolution']), reverse=True)

                def res_height(f):
                    try: return int(f['resolution'].split('x')[1])
                    except: return 0

                mp4_1080_video = [f for f in all_formats if f['ext'] == 'mp4' and f['acodec'] == 'none' and f['resolution'] == '1920x1080']
                m4a_audio = [f for f in all_formats if f['ext'] == 'm4a' and f['vcodec'] == 'none']
                best_video = sorted(mp4_1080_video, key=lambda f: f['filesize'], reverse=True)[0] if mp4_1080_video else None
                best_audio = sorted(m4a_audio, key=lambda f: f['filesize'], reverse=True)[0] if m4a_audio else None
                if not best_video:
                    mp4_video_1080 = [f for f in all_formats if f['ext'] == 'mp4' and f['acodec'] == 'none' and f['resolution'] not in ('', 'audio only') and res_height(f) <= 1080]
                    best_video = sorted(mp4_video_1080, key=lambda f: f['filesize'], reverse=True)[0] if mp4_video_1080 else None
                if not best_audio:
                    audio_only = [f for f in all_formats if f['vcodec'] == 'none' and f['acodec'] != 'none']
                    best_audio = sorted(audio_only, key=lambda f: f['filesize'], reverse=True)[0] if audio_only else None
                combo_1080 = [f for f in all_formats if f['acodec'] != 'none' and f['vcodec'] != 'none' and res_height(f) <= 1080]
                best_combo = sorted(combo_1080, key=lambda f: f['filesize'], reverse=True)[0] if combo_1080 else None
                use_combo = best_combo and not (best_video and best_audio)
                recommended_ids = set()
                if use_combo:
                    recommended_ids.add(best_combo['format_id'])
                else:
                    if best_video: recommended_ids.add(best_video['format_id'])
                    if best_audio: recommended_ids.add(best_audio['format_id'])
                for f in all_formats:
                    f['recommended'] = f['format_id'] in recommended_ids
                if use_combo:
                    rec_format_id = best_combo['format_id']
                elif best_video and best_audio:
                    rec_format_id = f"{best_video['format_id']}+{best_audio['format_id']}"
                elif best_video:
                    rec_format_id = best_video['format_id']
                else:
                    rec_format_id = None

                scan_cache[vid] = {
                    'title': e.get('title', ''),
                    'description': e.get('description', ''),
                    'formats': all_formats,
                    'rec_format_id': rec_format_id,
                    'cached_at': now,
                    'channel_name': e.get('uploader') or e.get('channel', ''),
                    'channel_id': e.get('channel_id') or e.get('uploader_id', ''),
                }
                new_candidates.append({
                    'id': vid,
                    'title': e.get('title', ''),
                    'description': e.get('description', ''),
                    'url': f"https://www.youtube.com/watch?v={vid}" if 'entries' in info else url,
                    'url_type': 'video',
                    'already_downloaded': False,
                    'formats': all_formats,
                    'rec_format_id': rec_format_id,
                    'channel_name': e.get('uploader') or e.get('channel', ''),
                    'channel_id': e.get('channel_id') or e.get('uploader_id', ''),
                })
            _save_scan_cache(work_dir, scan_cache)

            # Mark already-uploaded videos and write video_meta for new ones
            history = get_history()
            with _meta_lock:
                um = _load_video_meta(work_dir)
                skipped_ids = set()
                for c in new_candidates:
                    vid = c['id']
                    already_uploaded = (
                        vid in history or
                        um.get(vid, {}).get('uploaded') or
                        um.get(vid, {}).get('stages', {}).get('upload', {}).get('status') == 'done'
                    )
                    if already_uploaded:
                        c['already_uploaded'] = True
                        skipped_ids.add(vid)
                    else:
                        # Write video_meta entry if not already present
                        if vid not in um:
                            um[vid] = {
                                'title': c.get('title', ''),
                                'original_title': c.get('title', ''),
                                'tid': cfg['bilibili'].get('tid', 122),
                                'tags': list(cfg['bilibili'].get('default_tags', [])),
                                'copyright': 1,
                                'source': c['url'],
                                'schedule_time': None,
                                'uploaded': False,
                                'local_path': None,
                                'original_thumbnail': None,
                                'url': c['url'],
                                'queued_at': int(time.time()),
                                'stages': {
                                    'scan':      {'status': 'done',    'at': int(time.time())},
                                    'download':  {'status': 'pending', 'at': None},
                                    'transcode': {'status': 'pending', 'at': None},
                                    'translate': {'status': 'pending', 'at': None},
                                    'upload':    {'status': 'pending', 'at': None},
                                },
                            }
                _save_video_meta(work_dir, um)

            # Filter out already-uploaded from candidates to add
            new_candidates_to_add = [c for c in new_candidates if c['id'] not in skipped_ids]
    except Exception as ex:
        logger.warning(f"add_source full scan failed, title-only fallback: {ex}")
        skipped_ids = set()
        new_candidates_to_add = new_candidates

    skipped = len(skipped_ids)
    all_skipped = skipped == len(new_candidates) and len(new_candidates) > 0

    # Only save source URL if there are non-uploaded videos (or scan failed and we don't know)
    if not all_skipped:
        with _config_lock:
            cfg = load_config()
            if any(s['url'] == url for s in cfg['youtube']['sources']):
                pass  # already exists, skip
            else:
                cfg['youtube']['sources'].append({
                    'url': url,
                    'title': title,
                    'type': 'channel' if '/channel/' in url or '/c/' in url or '/@' in url else 'video'
                })
                save_config(cfg)
    # Merge new (non-uploaded) candidates into S['candidates']
    if new_candidates_to_add:
        with state_lock:
            existing_ids = {c['id'] for c in S['candidates']}
            for c in new_candidates_to_add:
                if c['id'] not in existing_ids:
                    S['candidates'].append(c)
            # Also sync video_meta in memory
            cfg2 = load_config()
            um2 = _load_video_meta(cfg2['app']['work_dir'])
            for c in new_candidates_to_add:
                vid = c['id']
                if vid in um2 and vid not in S['video_meta']:
                    S['video_meta'][vid] = um2[vid]
            new_status = 'scan_done' if S['status'] == 'idle' else None
        update_state(new_status)

    return jsonify({'ok': True, 'candidates': len(new_candidates_to_add), 'skipped': skipped, 'all_skipped': all_skipped})

@app.route('/api/sources/<int:idx>', methods=['DELETE'])
def delete_source(idx):
    with _config_lock:
        cfg = load_config()
        if 0 <= idx < len(cfg['youtube']['sources']):
            cfg['youtube']['sources'].pop(idx)
            save_config(cfg)
            return jsonify({'ok': True})
    return jsonify({'error': 'Invalid index'}), 400

@app.route('/api/sources', methods=['DELETE'])
def clear_sources():
    with _config_lock:
        cfg = load_config()
        cfg['youtube']['sources'] = []
        save_config(cfg)
    return jsonify({'ok': True})

# ── History API ──────────────────────────────────────────────────────────────
@app.route('/api/history', methods=['GET'])
def list_history():
    return jsonify(get_history())

@app.route('/api/video_meta', methods=['GET'])
def get_video_meta_all():
    """返回 video_meta.json 全量数据（含已上传条目），供日历视图使用。
    同时把 history.json 里有但 video_meta 里没有的条目补充进去（用文件 mtime 作为 uploaded_at）。"""
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    meta = _load_video_meta(work_dir)
    scan_cache = _load_scan_cache(work_dir)
    hist_ids = get_history()

    for vid in hist_ids:
        if vid in meta:
            continue  # 已在 video_meta 里，不覆盖
        uploaded_at = None
        title = scan_cache.get(vid, {}).get('title') or vid
        # 目录名格式: {slugified_title}_{vid[:8]}
        vid8 = vid[:8]
        if os.path.isdir(work_dir):
            for entry in os.scandir(work_dir):
                if entry.is_dir() and entry.name.endswith('_' + vid8):
                    safe_title = entry.name[: -(len(vid8) + 1)]
                    fp = os.path.join(entry.path, f"{safe_title}_final.mp4")
                    if os.path.isfile(fp):
                        uploaded_at = int(os.path.getmtime(fp))
                    break
        meta[vid] = {
            'title': title,
            'uploaded': True,
            'uploaded_at': uploaded_at,
        }

    return jsonify(meta)

@app.route('/api/thumb/<vid>')
def get_thumb(vid):
    """Serve local thumbnail for a video by its YouTube ID."""
    from flask import send_file
    cfg = load_config()
    work_dir = cfg['app']['work_dir']
    app_root = os.path.dirname(os.path.abspath(__file__))

    def resolve(path):
        if not path:
            return None
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(app_root, path))
        return path if os.path.isfile(path) else None

    # 1. Check video_meta.json first (most reliable)
    meta = _load_video_meta(work_dir)
    m = meta.get(vid)
    if m:
        p = resolve(m.get('original_thumbnail', ''))
        if p:
            return send_file(p)

    # 2. Search data/ directory for any image starting with vid prefix
    if os.path.isdir(work_dir):
        for entry in os.scandir(work_dir):
            if not entry.is_dir():
                continue
            # Match directories ending with _<vid8> (first 8 chars of vid)
            if not entry.name.endswith('_' + vid[:8]):
                continue
            for ext in ('webp', 'jpg', 'jpeg', 'png'):
                # Thumbnail filename = slugified title (same as dir name minus _vid8 suffix)
                stem = entry.name[: -(len(vid[:8]) + 1)]
                candidate = os.path.join(entry.path, stem + '.' + ext)
                if os.path.isfile(candidate):
                    return send_file(candidate)
            # Fallback: any image in that dir (cover_custom excluded)
            for fname in os.listdir(entry.path):
                if fname == 'cover_custom.jpg':
                    continue
                if fname.lower().endswith(('.webp', '.jpg', '.jpeg', '.png')):
                    return send_file(os.path.join(entry.path, fname))

    return '', 404

# ── Bilibili author check ──────────────────────────────────────────────────────
@app.route('/api/bili_check', methods=['POST'])
def trigger_bili_check():
    """扫描完成后前端调用，后台逐个查询频道在B站的同名账号。"""
    data = request.json or {}
    channels = data.get('channels', [])
    if not channels:
        return jsonify({'ok': True, 'skipped': True})

    cfg = load_config()
    threshold = float(cfg.get('bilibili', {}).get('bili_check_similarity', 0.75))

    def worker():
        from bili_checker import check_channel
        for ch in channels:
            if S['cancel_flag']:
                break
            cid = ch.get('channel_id', '')
            cname = ch.get('channel_name', '')
            if not cname:
                continue
            try:
                result = check_channel(cname, cid, threshold=threshold)
                broadcast('bili_check', {
                    'channel_id': cid,
                    'channel_name': cname,
                    'result': result,
                })
            except Exception as exc:
                logger.warning(f"bili_check failed for '{cname}': {exc}")
                broadcast('bili_check', {
                    'channel_id': cid,
                    'channel_name': cname,
                    'result': {'status': 'error', 'match_name': '', 'match_mid': 0,
                               'match_fans': 0, 'similarity': 0.0, 'bili_url': ''},
                })
            time.sleep(0.5)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({'ok': True})


# ── Bilibili status (sidebar) ─────────────────────────────────────────────────
@app.route('/api/bilibili/status', methods=['GET'])
def get_bili_status():
    if not os.path.exists('cookies.json'):
         return jsonify({'logged_in': False})
    
    # Simple logic: check file age
    mtime = os.path.getmtime('cookies.json')
    age_days = (time.time() - mtime) / 86400
    return jsonify({
        'logged_in': True,
        'last_login': datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M'),
        'age_days': int(age_days),
        'warning': age_days > 15
    })

if __name__ == '__main__':
    cfg = load_config()
    restore_state()
    app.run(host=cfg['app']['host'], port=cfg['app']['port'], debug=False)
