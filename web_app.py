"""
web_app.py — Flask Web UI for YT_BI_Anti pipeline.
Run: python web_app.py
Open: http://127.0.0.1:5000
"""
import os
import sys
import json
import queue
import logging
import threading
import subprocess
import time
from datetime import datetime

# ── Resolve bun path before any yt-dlp import ──────────────────────────────
_BUN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bun.exe')

from flask import Flask, render_template, jsonify, request, Response, stream_with_context
import yaml

from yt_downloader import YouTubeDownloader, detect_url_type
from video_processor import VideoProcessor
from bili_uploader import BilibiliUploader

# ── App setup ───────────────────────────────────────────────────────────────
app = Flask(__name__)
app.json.ensure_ascii = False

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')

# ── Global pipeline state (in-memory) ───────────────────────────────────────
# Status machine:
#   idle → scanning → scan_done → downloading → download_done
#   → transcoding → transcode_done → uploading → done (→ idle)

_state_lock = threading.Lock()
_cancel_requested = False
_pipeline = {
    'status': 'idle',
    'candidates': [],    # from scan — all videos including already_downloaded
    'downloaded': [],    # successfully downloaded this session
    'transcoded': [],    # successfully transcoded this session
    'uploaded': [],      # successfully uploaded this session
    'errors': [],
    'progress': {},      # {video_id: {pct, message}}
    'current_video': None,
}

# SSE event queue — multiple clients share the same events
_event_queues: list[queue.Queue] = []
_eq_lock = threading.Lock()

def check_cancel():
    return _cancel_requested


def _emit(event_type: str, data: dict):
    """Push an SSE event to all connected clients."""
    payload = json.dumps(data, ensure_ascii=False)
    msg = f"event: {event_type}\ndata: {payload}\n\n"
    with _eq_lock:
        dead = []
        for q in _event_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _event_queues.remove(q)


def _log(level: str, message: str):
    logging.info(f"[{level}] {message}")
    _emit('log', {
        'level': level,
        'message': message,
        'ts': datetime.now().strftime('%H:%M:%S')
    })


def _set_status(status: str, extra: dict = None):
    with _state_lock:
        _pipeline['status'] = status
        if extra:
            _pipeline.update(extra)
    payload = {'status': status}
    if extra:
        payload.update(extra)
    _emit('state', payload)


def _set_progress(video_id: str, pct: int, message: str = ''):
    with _state_lock:
        _pipeline['progress'][video_id] = {'pct': pct, 'message': message}
    _emit('progress', {'id': video_id, 'pct': pct, 'message': message})


# ── Config helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    cfg['_bun_path'] = _BUN_PATH
    return cfg


def save_config(cfg: dict):
    """Write config back to disk, stripping internal keys."""
    out = {k: v for k, v in cfg.items() if not k.startswith('_')}
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(out, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# ── SSE endpoint ─────────────────────────────────────────────────────────────

@app.route('/events')
def events():
    q = queue.Queue(maxsize=200)
    with _eq_lock:
        _event_queues.append(q)

    def generate():
        # Send current state immediately on connect
        with _state_lock:
            snap = json.dumps(_pipeline, ensure_ascii=False)
        yield f"event: snapshot\ndata: {snap}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            with _eq_lock:
                if q in _event_queues:
                    _event_queues.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


# ── Sources API ──────────────────────────────────────────────────────────────

@app.route('/api/sources', methods=['GET'])
def get_sources():
    cfg = load_config()
    sources_raw = cfg['youtube'].get('sources', cfg['youtube'].get('channel_urls', []))
    sources = []
    for s in sources_raw:
        if isinstance(s, dict):
            sources.append(s)
        else:
            sources.append({'url': s, 'type': detect_url_type(s), 'title': s})
    return jsonify(sources)


@app.route('/api/sources', methods=['POST'])
def add_source():
    url = request.json.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    cfg = load_config()
    sources = cfg['youtube'].setdefault('sources', cfg['youtube'].pop('channel_urls', []))
    
    # Duplicate check
    for s in sources:
        if (isinstance(s, dict) and s.get('url') == url) or (s == url):
            return jsonify({'error': 'URL already exists'}), 400
            
    # Fetch Meta
    downloader = YouTubeDownloader(cfg)
    url_type = detect_url_type(url)
    meta = downloader.fetch_source_metadata(url, url_type)
    
    sources.append(meta)
    save_config(cfg)
    return jsonify(meta)


@app.route('/api/sources/<int:idx>', methods=['DELETE'])
def delete_source(idx):
    cfg = load_config()
    sources = cfg['youtube'].get('sources', cfg['youtube'].get('channel_urls', []))
    if 0 <= idx < len(sources):
        sources.pop(idx)
        cfg['youtube']['sources'] = sources
        save_config(cfg)
        return jsonify({'ok': True})
    return jsonify({'error': 'Index out of range'}), 404


# ── Config API ───────────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
def get_config():
    cfg = load_config()
    return jsonify({
        'proxy': cfg['app'].get('proxy', ''),
        'tid': cfg['bilibili'].get('tid', 171),
        'intro_path': cfg['ffmpeg'].get('intro_video_path', ''),
        'ffmpeg_path': cfg['ffmpeg'].get('bin_path', 'ffmpeg'),
        'desc_prefix': cfg['bilibili'].get('desc_prefix', ''),
    })


@app.route('/api/config', methods=['POST'])
def set_config():
    data = request.json or {}
    cfg = load_config()
    if 'proxy' in data:
        cfg['app']['proxy'] = data['proxy']
    if 'tid' in data:
        cfg['bilibili']['tid'] = int(data['tid'])
    if 'intro_path' in data:
        cfg['ffmpeg']['intro_video_path'] = data['intro_path']
    if 'desc_prefix' in data:
        cfg['bilibili']['desc_prefix'] = data['desc_prefix']
    save_config(cfg)
    return jsonify({'ok': True})


# ── History API ──────────────────────────────────────────────────────────────

@app.route('/api/history')
def get_history():
    cfg = load_config()
    history_file = os.path.join(cfg['app']['work_dir'], 'history.json')
    if not os.path.exists(history_file):
        return jsonify([])
    with open(history_file, 'r', encoding='utf-8') as f:
        ids = json.load(f)
    return jsonify(ids)


# ── Pipeline state API ───────────────────────────────────────────────────────

@app.route('/api/state')
def get_state():
    with _state_lock:
        return jsonify(dict(_pipeline))


@app.route('/api/cancel', methods=['POST'])
def cancel_task():
    global _cancel_requested
    if _pipeline['status'] in ('scanning', 'downloading', 'transcoding', 'uploading'):
        _cancel_requested = True
        _log('warning', "User dispatched cancellation signal. Aborting sequence...")
        return jsonify({'ok': True})
    return jsonify({'error': 'No active task to cancel'}), 400


@app.route('/api/reset', methods=['POST'])
def reset_pipeline():
    global _cancel_requested
    if _pipeline['status'] in ('scanning', 'downloading', 'transcoding', 'uploading'):
        return jsonify({'error': 'Pipeline is running, cannot reset'}), 400

    _cancel_requested = False

    # 1. Clear out history.json & temporary files
    try:
        import glob
        cfg = load_config()
        work_dir = cfg['app']['work_dir']
        
        history_file = os.path.join(work_dir, 'history.json')
        if os.path.exists(history_file):
            os.remove(history_file)
            _log('info', "Deleted history.json")
            
        # Clean up temporary/broken yt-dlp fragments & concat files
        for pattern in ('*.part', '*.ytdl', 'concat_list.txt', 'main_standardized.mp4', 'intro_transcoded.mp4', '*.temp.mp4'):
            for p in glob.glob(os.path.join(work_dir, '**', pattern), recursive=True):
                try: 
                    os.remove(p)
                    _log('info', f"Cleaned temp file: {os.path.basename(p)}")
                except: pass
    except Exception as e:
        _log('error', f"Reset cleanup error: {e}")

    # 2. Reset in-memory state
    with _state_lock:
        _pipeline.update({
            'status': 'idle',
            'candidates': [],
            'downloaded': [],
            'transcoded': [],
            'uploaded': [],
            'errors': [],
            'progress': {},
            'current_video': None,
        })
    _emit('state', {'status': 'idle'})
    return jsonify({'ok': True})


# ── Step 1: Scan ─────────────────────────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
def start_scan():
    if _pipeline['status'] not in ('idle', 'done'):
        return jsonify({'error': f"Cannot scan while status={_pipeline['status']}"}), 400

    _set_status('scanning', {'candidates': [], 'progress': {}})

    def run():
        try:
            cfg = load_config()
            downloader = YouTubeDownloader(cfg)

            def cb(msg, pct=None):
                _log('info', msg)

            candidates = downloader.scan_all_sources(progress_cb=cb, cancel_check=check_cancel)

            with _state_lock:
                _pipeline['candidates'] = candidates

            _log('info', f"Scan complete: {len(candidates)} total, "
                         f"{sum(1 for c in candidates if not c['already_downloaded'])} new.")
            _set_status('scan_done', {'candidates': candidates})

        except Exception as e:
            if "cancelled" in str(e).lower():
                _log('warning', "Scan cancelled by user.")
                _set_status('idle')
            else:
                _log('error', f"Scan failed: {e}")
                _set_status('idle')
        finally:
            global _cancel_requested
            _cancel_requested = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


# ── Step 2: Download ──────────────────────────────────────────────────────────

@app.route('/api/download', methods=['POST'])
def start_download():
    """
    Body: {"video_ids": ["id1", "id2", ...]}
    Downloads selected videos that aren't already on disk.
    """
    if _pipeline['status'] != 'scan_done':
        return jsonify({'error': 'Must scan first'}), 400

    video_ids = request.json.get('video_ids', [])
    if not video_ids:
        return jsonify({'error': 'No video IDs provided'}), 400

    _set_status('downloading', {'downloaded': [], 'progress': {}})

    def run():
        global _cancel_requested
        try:
            cfg = load_config()
            downloader = YouTubeDownloader(cfg)

            # Build map from candidates
            candidate_map = {c['id']: c for c in _pipeline.get('candidates', [])}
            downloaded = []

            for i, item in enumerate(video_ids):
                if isinstance(item, dict):
                    vid_id = item.get('id')
                    quality_pref = item.get('quality')
                else:
                    vid_id = item
                    quality_pref = None

                candidate = candidate_map.get(vid_id, {})
                vid_url = candidate.get('url', f'https://www.youtube.com/watch?v={vid_id}')
                title = candidate.get('title', vid_id)
                _pipeline['current_video'] = vid_id
                _log('info', f"[{i+1}/{len(video_ids)}] Downloading {vid_id}...")

                def cb(msg, pct=None):
                    _log('info', f"  {msg}")
                    if pct is not None:
                        _set_progress(vid_id, pct, msg)
                            
                result = downloader.download_video(vid_id, vid_url, title, progress_cb=cb, cancel_check=check_cancel, quality=quality_pref)
                if result:
                    downloaded.append(result)
                    _set_progress(vid_id, 100, 'Download complete')
                    _log('info', f"[{vid_id}] ✓ Downloaded: {result['title']}")
                else:
                    _log('error', f"[{vid_id}] ✗ Download failed")
                    with _state_lock:
                        _pipeline['errors'].append({'id': vid_id, 'step': 'download'})

            with _state_lock:
                _pipeline['downloaded'] = downloaded
                _pipeline['current_video'] = None

            _log('info', f"Download complete: {len(downloaded)}/{len(video_ids)} succeeded.")
            _set_status('download_done', {'downloaded': downloaded})

        except Exception as e:
            if "cancelled" in str(e).lower():
                _log('warning', "Download cancelled by user.")
                _set_status('scan_done')
            else:
                _log('error', f"Download phase failed: {e}")
                _set_status('scan_done')
        finally:
            _cancel_requested = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


# ── Step 3: Transcode ─────────────────────────────────────────────────────────

@app.route('/api/transcode', methods=['POST'])
def start_transcode():
    """Transcode all downloaded videos (add intro + H264 standardize)."""
    if _pipeline['status'] != 'download_done':
        return jsonify({'error': 'Must download first'}), 400

    videos = _pipeline.get('downloaded', [])
    if not videos:
        return jsonify({'error': 'No downloaded videos to transcode'}), 400

    _set_status('transcoding', {'transcoded': [], 'progress': {}})

    def run():
        global _cancel_requested
        try:
            cfg = load_config()
            processor = VideoProcessor(cfg)
            transcoded = []

            for i, video in enumerate(videos):
                vid_id = video['id']
                _pipeline['current_video'] = vid_id
                _log('info', f"[{i+1}/{len(videos)}] Transcoding {vid_id}: {video['title']}")
                _set_progress(vid_id, 0, 'Starting transcode...')

                try:
                    final_path = processor.process(video, cancel_check=check_cancel)
                    if final_path:
                        video_out = dict(video)
                        video_out['final_path'] = final_path
                        transcoded.append(video_out)
                        _set_progress(vid_id, 100, 'Transcode complete')
                        _log('info', f"[{vid_id}] ✓ Transcoded → {final_path}")
                    else:
                        if _cancel_requested:
                            raise Exception("Transcode cancelled by user")
                        _log('error', f"[{vid_id}] ✗ Transcode returned no output")
                        with _state_lock:
                            _pipeline['errors'].append({'id': vid_id, 'step': 'transcode'})
                except Exception as e:
                    if "cancelled" in str(e).lower() or _cancel_requested:
                        raise e  # Bubble up to abort the entire batch
                    _log('error', f"[{vid_id}] Transcode error: {e}")
                    with _state_lock:
                        _pipeline['errors'].append({'id': vid_id, 'step': 'transcode', 'detail': str(e)})

            with _state_lock:
                _pipeline['transcoded'] = transcoded
                _pipeline['current_video'] = None

            _log('info', f"Transcode complete: {len(transcoded)}/{len(videos)} succeeded.")
            _set_status('transcode_done', {'transcoded': transcoded})

        except Exception as e:
            if "cancelled" in str(e).lower():
                _log('warning', "Transcode cancelled by user.")
                _set_status('download_done')
            else:
                _log('error', f"Transcode phase failed: {e}")
                _set_status('download_done')
        finally:
            _cancel_requested = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


# ── Step 4: Upload ─────────────────────────────────────────────────────────────

@app.route('/api/upload', methods=['POST'])
def start_upload():
    """Upload all transcoded videos to Bilibili."""
    if _pipeline['status'] != 'transcode_done':
        return jsonify({'error': 'Must transcode first'}), 400

    videos = _pipeline.get('transcoded', [])
    if not videos:
        return jsonify({'error': 'No transcoded videos to upload'}), 400

    _set_status('uploading', {'uploaded': [], 'progress': {}})

    def run():
        global _cancel_requested
        try:
            cfg = load_config()
            downloader = YouTubeDownloader(cfg)
            uploader = BilibiliUploader(cfg)
            uploaded = []

            for i, video in enumerate(videos):
                vid_id = video['id']
                final_path = video.get('final_path', video.get('filepath'))
                _pipeline['current_video'] = vid_id
                _log('info', f"[{i+1}/{len(videos)}] Uploading {vid_id}: {video['title']}")
                _set_progress(vid_id, 0, 'Starting upload...')

                try:
                    success = uploader.upload(
                        video_data=video,
                        final_video_path=final_path,
                        cancel_check=check_cancel
                    )
                    if success:
                        downloader.save_history(vid_id)
                        video_out = dict(video)
                        uploaded.append(video_out)
                        _set_progress(vid_id, 100, 'Upload complete ✓')
                        _log('info', f"[{vid_id}] ✓ Upload successful. Added to history.")
                    else:
                        if _cancel_requested:
                            raise Exception("Upload cancelled by user")
                        _log('error', f"[{vid_id}] ✗ Upload failed")
                        _set_progress(vid_id, 0, 'Upload FAILED')
                        with _state_lock:
                            _pipeline['errors'].append({'id': vid_id, 'step': 'upload'})
                except Exception as e:
                    if "cancelled" in str(e).lower() or _cancel_requested:
                        raise e  # Bubble up to abort the entire batch
                    _log('error', f"[{vid_id}] Upload error: {e}")
                    with _state_lock:
                        _pipeline['errors'].append({'id': vid_id, 'step': 'upload', 'detail': str(e)})

            with _state_lock:
                _pipeline['uploaded'] = uploaded
                _pipeline['current_video'] = None

            _log('info', f"Upload complete: {len(uploaded)}/{len(videos)} succeeded.")
            _set_status('done', {'uploaded': uploaded})

        except Exception as e:
            if "cancelled" in str(e).lower():
                _log('warning', "Upload cancelled by user.")
                _set_status('transcode_done')
            else:
                _log('error', f"Upload phase failed: {e}")
                _set_status('transcode_done')
        finally:
            _cancel_requested = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


# ── Bilibili login status ─────────────────────────────────────────────────────

@app.route('/api/bilibili/status')
def bilibili_status():
    cookies_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.json')
    if not os.path.exists(cookies_path):
        return jsonify({'logged_in': False, 'message': 'cookies.json not found'})
    mtime = os.path.getmtime(cookies_path)
    age_days = (time.time() - mtime) / 86400
    return jsonify({
        'logged_in': True,
        'last_login': datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M'),
        'age_days': round(age_days, 1),
        'warning': age_days > 14,
    })


# ── Main page ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    cfg = load_config()
    host = cfg['app'].get('host', '127.0.0.1')
    port = int(cfg['app'].get('port', 5000))

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('web_app.log', encoding='utf-8'),
        ]
    )

    print(f"\n{'='*50}")
    print(f"  YT → Bilibili Automation Web UI")
    print(f"  Open: http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}")
    print(f"{'='*50}\n")

    app.run(host=host, port=port, debug=False, threaded=True)
