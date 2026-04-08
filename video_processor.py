import os
import re
import subprocess
import logging
import json
import shutil
from fractions import Fraction

class VideoProcessor:
    def __init__(self, config):
        self.config = config
        self.ffmpeg_path = config['ffmpeg'].get('bin_path', 'ffmpeg')
        self.intro_path = config['ffmpeg'].get('intro_video_path', '')
        self.work_dir = config['app']['work_dir']
        
        # Test available HW encoders (functional probe)
        self.encoders = {
            'h264': self._get_hw_encoder('h264'),
            'hevc': self._get_hw_encoder('hevc'),
            'av1': self._get_hw_encoder('av1')
        }
        logging.info(f"Detected functional hardware encoders: {self.encoders}")

    def _get_hw_encoder(self, codec):
        """Probes for functional hardware encoders (QSV, then fallback)."""
        # We prioritize QSV as detected in user's system
        candidates = {
            'h264': ['h264_qsv', 'h264_nvenc', 'libx264'],
            'hevc': ['hevc_qsv', 'hevc_nvenc', 'libx265'],
            'av1': ['av1_qsv', 'av1_nvenc', 'libsvtav1']
        }
        
        for enc in candidates.get(codec, []):
            probe_cmd = [
                self.ffmpeg_path, '-hide_banner', '-y',
                '-f', 'lavfi', '-i', 'nullsrc=s=64x64',
                '-t', '0.01', '-c:v', enc,
                '-f', 'null', '-'
            ]
            try:
                res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode == 0:
                    return enc
            except:
                continue
        return None

    def _get_video_info(self, filepath):
        """Uses ffprobe to extract exhaustive metadata for cloning."""
        # Build ffprobe path by replacing only the filename, not path substrings
        ffmpeg_dir = os.path.dirname(os.path.abspath(self.ffmpeg_path))
        ffmpeg_basename = os.path.basename(self.ffmpeg_path)
        ffprobe_basename = re.sub(r'(?i)ffmpeg', 'ffprobe', ffmpeg_basename)
        ffprobe_path = os.path.join(ffmpeg_dir, ffprobe_basename) if ffmpeg_dir else ffprobe_basename
        cmd = [
            ffprobe_path, '-v', 'error',
            '-show_entries', 'stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,channels,bit_rate:format=duration',
            '-of', 'json', filepath
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            info = json.loads(result.stdout)
            
            v_stream = next((s for s in info['streams'] if s.get('codec_type') == 'video'), {})
            a_stream = next((s for s in info['streams'] if s.get('codec_type') == 'audio'), {})
            
            # Map common names to ffmpeg encoder names
            v_codec = v_stream.get('codec_name')
            a_codec = a_stream.get('codec_name')
            
            duration = float(info.get('format', {}).get('duration', 0))
            
            return {
                'v_codec': v_codec,
                'width': v_stream.get('width'),
                'height': v_stream.get('height'),
                'pix_fmt': v_stream.get('pix_fmt'),
                'fps': v_stream.get('r_frame_rate'),
                'a_codec': a_codec,
                'a_rate': a_stream.get('sample_rate') or '44100',
                'a_channels': a_stream.get('channels') or 2,
                'a_bitrate': a_stream.get('bit_rate') or '128000',
                'duration_us': int(duration * 1000000)
            }
        except Exception as e:
            logging.error(f"Deep probe failed for {filepath}: {e}")
            return None

    def _run_proc(self, cmd, cancel_check=None, progress_cb=None, total_duration_us=None):
        cmd_final = cmd[:-1] + ['-progress', 'pipe:1', '-nostats', '-loglevel', 'error'] + [cmd[-1]]
        proc = subprocess.Popen(cmd_final, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        
        import time
        import threading
        output = []
        def read_output():
            for line in proc.stdout:
                line = line.strip()
                if not line: continue
                output.append(line + "\n")
                if progress_cb and total_duration_us and 'out_time_us=' in line:
                    try:
                        current_us = int(line.split('=')[1])
                        pct = int((current_us / total_duration_us) * 100)
                        if pct < 0: pct = 0
                        if pct > 99: pct = 99
                        progress_cb(pct)
                    except: pass
        
        t = threading.Thread(target=read_output, daemon=True)
        t.start()
        
        try:
            while proc.poll() is None:
                if cancel_check and cancel_check():
                    proc.terminate()
                    raise Exception("Transcoding cancelled")
                time.sleep(0.5)
        finally:
            t.join(timeout=1)
            
        if proc.returncode != 0:
            last_msg = "".join(output[-15:])
            raise Exception(f"FFmpeg failed (code {proc.returncode}). Last output:\n{last_msg}")
        return True

    def _vtt_to_srt(self, vtt_path):
        """Parse YouTube rolling VTT and produce clean single-line SRT."""
        srt_path = re.sub(r'\.vtt$', '.srt', vtt_path, flags=re.IGNORECASE)

        def ts_to_ms(ts):
            ts = ts.replace('.', ',')
            h, m, s = ts.split(':')
            s, ms = s.split(',')
            return int(h)*3600000 + int(m)*60000 + int(s)*1000 + int(ms)

        def ms_to_ts(ms):
            h = ms // 3600000; ms %= 3600000
            m = ms // 60000;   ms %= 60000
            s = ms // 1000;    ms %= 1000
            return f"{h:02}:{m:02}:{s:02},{ms:03}"

        def strip_cue_tags(text):
            """Remove VTT word-level timing tags like <00:00:01.000><c>word</c>"""
            text = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d+>', '', text)
            text = re.sub(r'</?c>', '', text)
            return text.strip()

        with open(vtt_path, encoding='utf-8', errors='replace') as f:
            content = f.read()

        entries = []
        for block in re.split(r'\n\n+', content.strip()):
            lines = block.strip().splitlines()
            if len(lines) < 2:
                continue
            # Find timestamp line
            ts_line = None
            text_lines = []
            for line in lines:
                if '-->' in line and ts_line is None:
                    ts_line = line
                elif ts_line is not None:
                    text_lines.append(line)
            if not ts_line:
                continue
            m = re.match(r'([\d:\.]+)\s*-->\s*([\d:\.]+)', ts_line)
            if not m:
                continue
            start_ms = ts_to_ms(m.group(1))
            end_ms = ts_to_ms(m.group(2))
            if end_ms - start_ms < 200:
                continue

            # Strip cue tags from all text lines, keep non-empty
            clean_lines = [strip_cue_tags(l) for l in text_lines]
            clean_lines = [l for l in clean_lines if l]
            if not clean_lines:
                continue

            # YouTube rolling subtitle structure:
            # Line 1 = previous sentence (already complete)
            # Line 2 = current sentence being spoken (with cue tags)
            # We want line 2 (the new content). If only one line, use it.
            if len(clean_lines) >= 2:
                text = clean_lines[1]  # current sentence
            else:
                text = clean_lines[0]

            if not text:
                continue
            entries.append((start_ms, end_ms, text))

        # Merge consecutive entries with identical text
        merged = []
        for start, end, text in entries:
            if merged and merged[-1][2] == text:
                merged[-1][1] = end
            else:
                merged.append([start, end, text])

        # Merge entries that are very close in time (gap < 300ms) and form natural sentences
        # i.e. previous entry doesn't end with sentence-final punctuation
        sentence_end = re.compile(r'[。！？…]$')
        joined = []
        for start, end, text in merged:
            if (joined and not sentence_end.search(joined[-1][2])
                    and start - joined[-1][1] < 300):
                joined[-1][1] = end
                joined[-1][2] += text
            else:
                joined.append([start, end, text])

        with open(srt_path, 'w', encoding='utf-8') as f:
            for i, (start, end, text) in enumerate(joined, 1):
                f.write(f"{i}\n{ms_to_ts(start)} --> {ms_to_ts(end)}\n{text}\n\n")

        return srt_path

    def _sub_filter(self, srt_path, margin_v, font_size, colour):
        """Build a subtitles= filter string with force_style for a given SRT file."""
        # ffmpeg subtitles filter on Windows needs forward slashes and escaped colons
        safe_path = srt_path.replace('\\', '/').replace(':', '\\:')
        style = (f"PlayResX=1920,PlayResY=1080,Alignment=2,MarginV={margin_v},"
                 f"FontSize={font_size},PrimaryColour={colour},"
                 f"OutlineColour=&H000000&,BorderStyle=3,Outline=2,Shadow=0,"
                 f"WrapStyle=1")
        return f"subtitles='{safe_path}':force_style='{style}'"

    def process(self, video_data, cancel_check=None, progress_cb=None):
        """
        Seamless GPU Strategy (Hybrid):
        1. Deep Probe the Standard (Main Video).
        2. Transcode Intro using QSV to EXACTLY match dimensions/pixfmt.
        3. Perform a clean merge using filter_complex + h264_qsv (fixes audio noise).
        """
        filepath = video_data['filepath']
        video_dir = os.path.dirname(filepath)
        base_name = os.path.splitext(os.path.basename(filepath))[0]
        final_output = os.path.join(video_dir, f"{base_name}_final.mp4")
        
        if os.path.exists(final_output):
            if os.path.getsize(final_output) > 0:
                if progress_cb: progress_cb(100)
                return final_output
            else:
                os.remove(final_output)

        # Step 1: Deep Probe Main Video (The Standard)
        m = self._get_video_info(filepath)
        if not m: return None
        
        # Step 2: Transcode Intro to EXACTLY match the Main Video's Format
        v_enc = self.encoders.get('h264') or 'libx264'

        # Resolve audio bitrate from probe (ffprobe returns bits/s as string e.g. "128000")
        raw_br = m.get('a_bitrate', '192k')
        try:
            a_bitrate = f"{int(raw_br) // 1000}k"
        except (ValueError, TypeError):
            a_bitrate = str(raw_br) if raw_br else '192k'
        # Precise hash for the matching intro — fps must be included to avoid reusing wrong cache
        try:
            fps_val = float(Fraction(m['fps'])) if m.get('fps') else 30.0
        except (ValueError, ZeroDivisionError):
            fps_val = 30.0
        m_hash = f"{m['width']}x{m['height']}_{fps_val:.3f}_{m['v_codec']}_{m['a_codec']}_{m['a_rate']}_{m['a_channels']}"
        cache_key = f"intro_match_{m_hash}.mp4"
        matched_intro = os.path.join(self.work_dir, cache_key)

        if not os.path.exists(matched_intro):
            logging.info(f"GPU Pre-aligner: Matching intro to {m['width']}x{m['height']} @ {fps_val:.3f}fps")

            vw, vh = m['width'], m['height']
            # Round to even dimensions for encoder compatibility (yuv420p requires even w/h)
            vw = vw if vw % 2 == 0 else vw - 1
            vh = vh if vh % 2 == 0 else vh - 1
            vf_str = f"scale={vw}:{vh}:force_original_aspect_ratio=decrease,pad={vw}:{vh}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"

            cmd_intro = [
                self.ffmpeg_path, '-y',
                '-i', self.intro_path,
                '-vf', vf_str,
                '-r', f"{fps_val:.3f}",
                '-c:v', v_enc, '-global_quality', '18',
            ]
            
            cmd_intro += [
                '-c:a', 'aac', '-ar', str(m['a_rate']), '-ac', str(m['a_channels']), '-b:a', a_bitrate,
                matched_intro
            ]
            try:
                self._run_proc(cmd_intro, cancel_check)
            except Exception as e:
                logging.error(f"High-precision alignment failed: {e}")
                return None

        # Step 3: Seamless Merger via filter_complex
        # QSV needs explicit -r because filter_complex concat doesn't propagate fps metadata,
        # causing h264_qsv encoder init to fail with "Function not implemented" (-40).
        use_qsv = 'qsv' in v_enc
        merge_enc = v_enc if use_qsv else v_enc
        logging.info(f"Merging with {merge_enc} (fps={fps_val:.3f})")

        # Intro has already been pre-scaled to match main video dimensions.
        # Normalize SAR to 1:1 on both streams before concat (SAR mismatch causes -22).
        filter_str = "[0:v]format=yuv420p,setsar=1[v0];[1:v]format=yuv420p,setsar=1[v1];[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[outv][outa]"

        # Build subtitle burn-in chain (appended after concat)
        sub_filters = []
        for vtt_key, margin_v, font_size, colour in [
            ('subtitle_zh', 20, 28, '&H00FFFFFF&'),   # 中文：白色，底部贴边
        ]:
            vtt_path = video_data.get(vtt_key, '')
            if vtt_path and os.path.isfile(vtt_path):
                srt_path = self._vtt_to_srt(vtt_path)
                if srt_path:
                    sub_filters.append(self._sub_filter(srt_path, margin_v, font_size, colour))
                    logging.info(f"Subtitle burn-in: {vtt_key} -> {srt_path}")

        if sub_filters:
            # Chain subtitle filters after concat: [outv] → sub1 → sub2 → [outv_final]
            chain = '[outv]'
            for i, sf in enumerate(sub_filters):
                out_tag = '[outv_final]' if i == len(sub_filters) - 1 else f'[vsub{i}]'
                filter_str += f';{chain}{sf}{out_tag}'
                chain = out_tag
            video_out_tag = '[outv_final]'
        else:
            video_out_tag = '[outv]'
        fps_frac = m['fps']  # e.g. "60000/1001" or "30/1"
        if use_qsv:
            cmd_merge = [
                self.ffmpeg_path, '-y',
                '-i', matched_intro, '-i', filepath,
                '-filter_complex', filter_str,
                '-map', video_out_tag, '-map', '[outa]',
                '-r', fps_frac,
                '-c:v', merge_enc, '-global_quality', '25',
                '-c:a', 'aac', '-b:a', a_bitrate,
                final_output
            ]
        else:
            cmd_merge = [
                self.ffmpeg_path, '-y',
                '-i', matched_intro, '-i', filepath,
                '-filter_complex', filter_str,
                '-map', video_out_tag, '-map', '[outa]',
                '-c:v', merge_enc, '-preset', 'medium', '-crf', '23',
                '-c:a', 'aac', '-b:a', a_bitrate,
                final_output
            ]

        try:
            self._run_proc(cmd_merge, cancel_check, progress_cb, m['duration_us'])
            return final_output
        except Exception as e:
            logging.error(f"GPU merge failed: {e}. Falling back to libx264...")
            # Rebuild filter without subtitle on fallback to isolate the issue
            filter_fb = "[0:v]format=yuv420p,setsar=1[v0];[1:v]format=yuv420p,setsar=1[v1];[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[outv][outa]"
            if sub_filters:
                chain = '[outv]'
                for i, sf in enumerate(sub_filters):
                    out_tag = '[outv_final]' if i == len(sub_filters) - 1 else f'[vsub{i}]'
                    filter_fb += f';{chain}{sf}{out_tag}'
                    chain = out_tag
                fb_video_out = '[outv_final]'
            else:
                fb_video_out = '[outv]'
            cmd_fb = [
                self.ffmpeg_path, '-y',
                '-i', matched_intro,
                '-i', filepath,
                '-filter_complex', filter_fb,
                '-map', fb_video_out, '-map', '[outa]',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'aac', '-b:a', a_bitrate,
                final_output
            ]
            self._run_proc(cmd_fb, cancel_check, progress_cb, m['duration_us'])
            return final_output
