import os
import sys
import yaml
import json
import logging
import threading
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, send_from_directory
from bili_uploader import BilibiliUploader
import re

# Helper for filename-safe titles
def slugify(text):
    # Remove Windows illegal characters: \/:*?"<>|
    text = re.sub(r'[\\/:*?"<>|]', '_', text)
    # Remove trailing dots/spaces and limit length
    return text.strip().rstrip('. ')[:100]

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
# Global state for pipeline
S = {
    'status': 'idle',      # idle, scanning, scan_done, downloading, download_done, transcoding, transcode_done, uploading, done
    'candidates': [],       # List of discovered videos
    'downloaded': [],       # List of successfully downloaded videos
    'transcoded': [],       # List of successfully transcoded videos
    'uploaded': [],         # List of successfully uploaded videos
    'errors': [],           # List of {id, step, message}
    'progress': {},         # id -> {pct, message} - for real-time reporting
    'cancel_flag': False,
    'current_task': None
}

def reset_pipeline():
    S['status'] = 'idle'
    S['candidates'] = []
    S['downloaded'] = []
    S['transcoded'] = []
    S['uploaded'] = []
    S['errors'] = []
    S['progress'] = {}
    S['cancel_flag'] = False
    S['current_task'] = None

# ── Config Loader ────────────────────────────────────────────────────────────
CONFIG_FILE = 'config.yaml'

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {
            'app': {'work_dir': './data', 'proxy': '', 'host': '127.0.0.1', 'port': 5000},
            'youtube': {'sources': []},
            'ffmpeg': {'bin_path': 'ffmpeg', 'intro_video_path': './assets/intro.mp4'},
            'bilibili': {'tid': 171, 'desc_prefix': '本视频搬运自YouTube。\n\n原视频链接：{youtube_url}\n\n\n'}
        }
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)

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
    if new_status: S['status'] = new_status
    for k, v in kwargs.items():
        if k in S: S[k] = v
    broadcast('state', {k: S[k] for k in S if k != 'current_task'})

def log_to_web(level, message, video_id=None):
    ts = datetime.now().strftime('%H:%M:%S')
    broadcast('log', {'ts': ts, 'level': level, 'message': message, 'id': video_id})
    if level == 'error':
        logger.error(f"[{video_id or 'GLOBAL'}] {message}")
    else:
        logger.info(f"[{video_id or 'GLOBAL'}] {message}")

def report_progress(video_id, pct, message):
    S['progress'][video_id] = {'pct': pct, 'message': message}
    broadcast('progress', {'id': video_id, 'pct': pct, 'message': message})

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
        for s in sources:
            if S['cancel_flag']: break
            url = s['url']
            log_to_web('info', f"扫描中: {url}")
            
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False, # Fetch full info for filesize data
                'proxy': cfg['app'].get('proxy')
            }
            with YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    entries = info.get('entries', [info])
                    
                    for e in entries:
                        if not e: continue
                        
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

                        new_candidates.append({
                            'id': e['id'],
                            'title': e['title'],
                            'description': e.get('description', ''),
                            'url': f"https://www.youtube.com/watch?v={e['id']}" if 'entries' in info else url,
                            'url_type': 'video',
                            'already_downloaded': False,
                            'formats': all_formats
                        })
                except Exception as e:
                    log_to_web('error', f"源解析失败 {url}: {str(e)}")

        # Deduplicate and check history
        history = get_history()
        for c in new_candidates:
            if c['id'] in history:
                c['already_downloaded'] = True
        
        # Merge with existing candidates (if any)
        S['candidates'] = new_candidates
        log_to_web('info', f"扫描完成，发现 {len(new_candidates)} 个候选视频。")
        update_state('scan_done')
    except Exception as e:
        log_to_web('error', f"扫描阶段崩溃: {str(e)}")
        update_state('idle')

def run_download(video_ids):
    try:
        update_state('downloading')
        cfg = load_config()
        work_dir = cfg['app']['work_dir']
        
        from yt_dlp import YoutubeDL
        
        for vid_entry in video_ids:
            if S['cancel_flag']: break
            vid = vid_entry['id']
            # Accept explicit format_id from frontend selection
            format_id = vid_entry.get('format_id')
            quality = vid_entry.get('quality', '1080p')
            
            c = next((x for x in S['candidates'] if x['id'] == vid), None)
            if not c: continue
            
            log_to_web('info', f"开始下载 [{format_id or quality}]: {c['title']}", vid)
            
            # Phase 1: Slugified Path Naming
            safe_title = slugify(c['title'])
            vid_dir = os.path.join(work_dir, safe_title)
            os.makedirs(vid_dir, exist_ok=True)
            out_tmpl = os.path.join(vid_dir, f"{safe_title}.%(ext)s")
            
            # Phase 2: Format selection
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
                    p = d.get('_percent_str', '0%').replace('%','').strip()
                    try: 
                        import re
                        p = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', p)
                        pct = float(p)
                    except: pct = 0
                    report_progress(vid, pct, f"下载中... {d.get('_speed_str','')}")
                elif d['status'] == 'finished':
                    report_progress(vid, 100, "下载完成")

            ydl_opts = {
                'format': format_sel,
                'outtmpl': out_tmpl,
                'progress_hooks': [ydl_hook],
                'proxy': cfg['app'].get('proxy'),
                'quiet': True,
                'no_warnings': True,
                'merge_output_format': 'mp4',
                'writethumbnail': True,
                'ffmpeg_location': cfg['ffmpeg'].get('bin_path')
            }

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([c['url']])
                
                # Phase 3: Flexible Verify and Map
                # Instead of a fixed filename, scan the directory for the largest MP4/MKV/Video file
                found_video = None
                max_size = 0
                
                if os.path.exists(vid_dir):
                    for f in os.listdir(vid_dir):
                        if f.endswith(('.mp4', '.mkv', '.mov', '.ts', '.flv')):
                            fpath = os.path.join(vid_dir, f)
                            fsize = os.path.getsize(fpath)
                            if fsize > max_size and fsize > 10 * 1024 * 1024: # Must be > 10MB to be a video
                                max_size = fsize
                                found_video = fpath

                if found_video:
                    # Store the local path for downstream steps
                    c['local_path'] = found_video
                    c['local_dir'] = vid_dir
                    
                    # Check for thumbnail (might be .jpg, .png, .webp)
                    # Look for any image file in the directory
                    for f in os.listdir(vid_dir):
                        if f.lower().endswith(('.jpg', '.png', '.webp', '.jpeg')):
                            c['original_thumbnail'] = os.path.join(vid_dir, f)
                            break
                    
                    S['downloaded'].append(c)
                    log_to_web('info', f"成功下载并识别: {os.path.basename(found_video)}", vid)
                else:
                    raise Exception("下载完成但未找到有效的视频文件(>10MB)")
                    
            except Exception as e:
                # Log error but DO NOT DELETE the directory
                log_to_web('error', f"下载阶段异常: {str(e)}", vid)
                S['errors'].append({'id': vid, 'step': 'download', 'message': str(e)})
                log_to_web('error', f"下载失败: {str(e)}", vid)

        update_state('download_done')
    except Exception as e:
        log_to_web('error', f"下载阶段崩溃: {str(e)}")
        update_state('scan_done')

def run_transcode():
    try:
        update_state('transcoding')
        cfg = load_config()
        from video_processor import VideoProcessor
        processor = VideoProcessor(cfg)

        for c in S['downloaded']:
            if S['cancel_flag']: break
            vid = c['id']
            log_to_web('info', f"启动转码流程: {c['title']}", vid)
            
            # Use Slugified Paths
            safe_title = slugify(c['title'])
            video_data = {
                'id': vid,
                'filepath': os.path.join(cfg['app']['work_dir'], safe_title, f"{safe_title}.mp4")
            }

            def transcode_progress(pct):
                report_progress(vid, pct, "转码中 (视频流处理)...")

            try:
                def check_cancel(): return S['cancel_flag']
                res = processor.process(video_data, cancel_check=check_cancel, progress_cb=transcode_progress)
                
                if res:
                    S['transcoded'].append(c)
                    report_progress(vid, 100, "转码完成")
                    log_to_web('info', f"转码成功: {c['title']}", vid)
                else:
                    raise Exception("计算后端返回失败")
            except Exception as e:
                S['errors'].append({'id': vid, 'step': 'transcode', 'message': str(e)})
                log_to_web('error', f"转码失败: {str(e)}", vid)

        update_state('transcode_done')
    except Exception as e:
        log_to_web('error', f"转码阶段崩溃: {str(e)}")
        update_state('download_done')

def run_upload():
    try:
        update_state('uploading')
        cfg = load_config()
        work_dir = cfg['app']['work_dir']
        
        uploader = BilibiliUploader(cfg)
        
        for c in S['transcoded']:
            if S['cancel_flag']: break
            vid = c['id']
            log_to_web('info', f"准备上传至 B站: {c['title']}", vid)
            
            # Use Slugified Paths
            safe_title = slugify(c['title'])
            vid_dir = os.path.join(work_dir, safe_title)
            file_path = os.path.join(vid_dir, f"{safe_title}_final.mp4")
            
            # Check for original thumbnail (downloaded by yt-dlp)
            thumb_path = None
            for ext in ['jpg', 'png', 'webp', 'jpeg']:
                test_thumb = os.path.join(vid_dir, f"{safe_title}.{ext}")
                if os.path.exists(test_thumb):
                    thumb_path = test_thumb
                    break

            def upload_progress(pct, msg):
                report_progress(vid, pct, msg)

            try:
                res = uploader.upload(
                    file_path, 
                    c['title'], 
                    c['url'], 
                    original_thumbnail=thumb_path, 
                    original_description=c.get('description', ''),
                    progress_callback=upload_progress
                )
                if res:
                    S['uploaded'].append(c)
                    add_history(vid)
                    log_to_web('info', f"B站上传成功: {c['title']}", vid)
                else:
                    raise Exception("上传器返回失败状态")
            except Exception as e:
                S['errors'].append({'id': vid, 'step': 'upload', 'message': str(e)})
                log_to_web('error', f"上传失败: {str(e)}", vid)

        update_state('done')
    except Exception as e:
        log_to_web('error', f"上传阶段崩溃: {str(e)}")
        update_state('transcode_done')

# ── History Persistence ──────────────────────────────────────────────────────
HISTORY_FILE = 'history.json'
def get_history():
    if not os.path.exists(HISTORY_FILE): return []
    try:
        with open(HISTORY_FILE, 'r') as f: return json.load(f)
    except: return []

def add_history(vid):
    h = get_history()
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
    if S['status'] in ['scanning', 'downloading', 'transcoding', 'uploading']:
        return jsonify({'error': 'Pipeline busy'}), 400
    S['cancel_flag'] = False
    S['current_task'] = threading.Thread(target=run_scan)
    S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/download', methods=['POST'])
def trigger_download():
    data = request.json or {}
    video_ids = data.get('video_ids', []) # Expected list of {id, quality}
    if not video_ids: return jsonify({'error': 'No IDs provided'}), 400
    if S['status'] != 'scan_done': return jsonify({'error': 'Wrong state'}), 400
    
    S['cancel_flag'] = False
    S['current_task'] = threading.Thread(target=run_download, args=(video_ids,))
    S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/transcode', methods=['POST'])
def trigger_transcode():
    if S['status'] != 'download_done': return jsonify({'error': 'Wrong state'}), 400
    S['cancel_flag'] = False
    S['current_task'] = threading.Thread(target=run_transcode)
    S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/upload', methods=['POST'])
def trigger_upload():
    if S['status'] != 'transcode_done': return jsonify({'error': 'Wrong state'}), 400
    S['cancel_flag'] = False
    S['current_task'] = threading.Thread(target=run_upload)
    S['current_task'].start()
    return jsonify({'ok': True})

@app.route('/api/reset', methods=['POST'])
def trigger_reset():
    reset_pipeline()
    update_state()
    return jsonify({'ok': True})

@app.route('/api/cancel', methods=['POST'])
def trigger_cancel():
    S['cancel_flag'] = True
    log_to_web('warn', "任务取消请求已发出...")
    return jsonify({'ok': True})

@app.route('/api/retry', methods=['POST'])
def trigger_retry():
    data = request.json or {}
    vid = data.get('video_id')
    if not vid: return jsonify({'error': 'Missing video_id'}), 400
    
    # Identify the failed step
    err_entry = next((e for e in S['errors'] if e['id'] == vid), None)
    failed_step = err_entry['step'] if err_entry else 'unknown'
    
    # Remove from errors
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
                src_path = os.path.join(cfg['app']['work_dir'], safe_title, f"{safe_title}.mp4")
                v_data = {'id': vid, 'filepath': src_path}
                def pb(p): report_progress(vid, p, "重试转码中...")
                if processor.process(v_data, cancel_check=lambda: S['cancel_flag'], progress_cb=pb):
                    if video_entry not in S['transcoded']: S['transcoded'].append(video_entry)
                    report_progress(vid, 100, "转码完成")
                    # If all now transcoded, allow manual or auto transition
                    if not S['errors']: update_state('transcode_done')
                else: raise Exception("转码逻辑失败")

            # 3. Retry UPLOAD
            elif failed_step in ['upload', 'transcode_retry']:
                from bili_uploader import BilibiliUploader
                uploader = BilibiliUploader(cfg)
                safe_title = slugify(video_entry['title'])
                vid_dir = os.path.join(cfg['app']['work_dir'], safe_title)
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
                    if video_entry not in S['uploaded']: S['uploaded'].append(video_entry)
                    report_progress(vid, 100, "上传完成")
                    add_history(vid)
                    # Complete batch if no more errors
                    if not S['errors']: update_state('done')
                else: raise Exception("再次投递失败")
            else:
                log_to_web('warn', f"未知的失败状态 '{failed_step}'，无法自动重试", vid)
        except Exception as e:
            S['errors'].append({'id': vid, 'step': f"{failed_step}_retry", 'message': str(e)})
            log_to_web('error', f"重试失败: {str(e)}", vid)
            
    threading.Thread(target=retry_worker, daemon=True).start()
    return jsonify({'ok': True})

# ── Config API ───────────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
def get_config():
    cfg = load_config()
    return jsonify({
        'proxy': cfg['app'].get('proxy', ''),
        'tid': cfg['bilibili'].get('tid', 171),
        'intro_path': cfg['ffmpeg'].get('intro_video_path', ''),
        'desc_prefix': cfg['bilibili'].get('desc_prefix', ''),
    })

@app.route('/api/config', methods=['POST'])
def set_config():
    data = request.json or {}
    cfg = load_config()
    if 'proxy' in data: cfg['app']['proxy'] = data['proxy']
    if 'tid' in data: cfg['bilibili']['tid'] = int(data['tid'])
    if 'intro_path' in data: cfg['ffmpeg']['intro_video_path'] = data['intro_path']
    if 'desc_prefix' in data: cfg['bilibili']['desc_prefix'] = data['desc_prefix']
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
    
    # Simple check for duplicates
    cfg = load_config()
    if any(s['url'] == url for s in cfg['youtube']['sources']):
        return jsonify({'error': 'Already exists'}), 400
        
    # Get Title (Optional helper)
    title = url
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL({'quiet':True, 'extract_flat':True, 'proxy':cfg['app'].get('proxy')}) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', url)
    except: pass

    cfg['youtube']['sources'].append({
        'url': url,
        'title': title,
        'type': 'channel' if '/channel/' in url or '/c/' in url or '/@' in url else 'video'
    })
    save_config(cfg)
    return jsonify({'ok': True})

@app.route('/api/sources/<int:idx>', methods=['DELETE'])
def delete_source(idx):
    cfg = load_config()
    if 0 <= idx < len(cfg['youtube']['sources']):
        cfg['youtube']['sources'].pop(idx)
        save_config(cfg)
        return jsonify({'ok': True})
    return jsonify({'error': 'Invalid index'}), 400

# ── History API ──────────────────────────────────────────────────────────────
@app.route('/api/history', methods=['GET'])
def list_history():
    return jsonify(get_history())

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
    app.run(host=cfg['app']['host'], port=cfg['app']['port'], debug=False)
