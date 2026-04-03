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

            vf_str = f"scale={m['width']}:{m['height']}:force_original_aspect_ratio=decrease,pad={m['width']}:{m['height']}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"

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

        filter_str = "[0:v]format=yuv420p[v0];[1:v]format=yuv420p[v1];[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[outv][outa]"
        fps_frac = m['fps']  # e.g. "60000/1001" or "30/1"
        if use_qsv:
            cmd_merge = [
                self.ffmpeg_path, '-y',
                '-i', matched_intro, '-i', filepath,
                '-filter_complex', filter_str,
                '-map', '[outv]', '-map', '[outa]',
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
                '-map', '[outv]', '-map', '[outa]',
                '-c:v', merge_enc, '-preset', 'medium', '-crf', '23',
                '-c:a', 'aac', '-b:a', a_bitrate,
                final_output
            ]

        try:
            self._run_proc(cmd_merge, cancel_check, progress_cb, m['duration_us'])
            return final_output
        except Exception as e:
            logging.error(f"GPU merge failed: {e}. Falling back to libx264...")
            cmd_fb = [
                self.ffmpeg_path, '-y',
                '-i', matched_intro,
                '-i', filepath,
                '-filter_complex', "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
                '-map', '[outv]', '-map', '[outa]',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'aac', '-b:a', a_bitrate,
                final_output
            ]
            self._run_proc(cmd_fb, cancel_check, progress_cb, m['duration_us'])
            return final_output
