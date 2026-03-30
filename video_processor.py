import os
import subprocess
import logging
import json

class VideoProcessor:
    def __init__(self, config):
        self.config = config
        self.ffmpeg_path = config['ffmpeg'].get('bin_path', 'ffmpeg')
        self.intro_path = config['ffmpeg'].get('intro_video_path', '')
        self.work_dir = config['app']['work_dir']
        self.has_nvenc = self._check_nvenc()
        if self.has_nvenc:
            logging.info("NVIDIA NVENC detected. GPU encoding enabled.")
        else:
            logging.info("NVENC not found. Using CPU encoding (libx264).")

    def _check_nvenc(self):
        try:
            res = subprocess.run([self.ffmpeg_path, '-hide_banner', '-encoders'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return 'h264_nvenc' in res.stdout
        except:
            return False

    def _run_proc(self, cmd, cancel_check=None, progress_cb=None, total_duration_us=None):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        import time
        import re
        import threading
        
        output = []
        def read_output():
            for line in proc.stdout:
                line = line.strip()
                if not line: continue
                output.append(line + "\n")
                
                # Parse progress: out_time_us=XXXXXXX
                if progress_cb and total_duration_us:
                    match = re.search(r'out_time_us=(\d+)', line)
                    if match:
                        current_us = int(match.group(1))
                        pct = int((current_us / total_duration_us) * 100)
                        if pct > 99: pct = 99 # Cap at 99 until finished
                        progress_cb(pct)
        
        t = threading.Thread(target=read_output, daemon=True)
        t.start()
        
        while proc.poll() is None:
            if cancel_check and cancel_check():
                logging.warning("Cancellation requested during transcode. Terminating FFmpeg...")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # Hard kill if terminate failed on Windows
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], 
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                raise Exception("Transcoding cancelled by user")
            time.sleep(0.5)
            
        t.join()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output="".join(output).encode('utf-8'))
        return True

    def _run_ffmpeg_with_fallback(self, cmd_nvenc_full, cmd_nvenc_hybrid, cmd_cpu, cancel_check=None, progress_cb=None, duration_us=None):
        def inject_progress_flags(cmd):
            return cmd[:-1] + ['-progress', 'pipe:1', '-nostats'] + [cmd[-1]]

        if self.has_nvenc:
            # STAGE 1: Full GPU (Zero-Copy)
            try:
                logging.info("Attempting Full GPU Transcoding (Zero-Copy)...")
                self._run_proc(inject_progress_flags(cmd_nvenc_full), cancel_check, progress_cb, duration_us)
                return True
            except Exception as e:
                if "cancelled" in str(e).lower(): raise
                logging.warning(f"Full GPU failed, attempting Hybrid GPU... Error: {str(e)[-100:]}")
            
            # STAGE 2: Hybrid GPU (CPU Decode -> GPU Scale/Encode)
            try:
                logging.info("Attempting Hybrid GPU Transcoding (CPU Decode + GPU Scale/Encode)...")
                self._run_proc(inject_progress_flags(cmd_nvenc_hybrid), cancel_check, progress_cb, duration_us)
                return True
            except Exception as e:
                if "cancelled" in str(e).lower(): raise
                logging.warning(f"Hybrid GPU failed, falling back to CPU... Error: {str(e)[-100:]}")

        # STAGE 3: Full CPU (Legacy)
        try:
            logging.info("Using CPU Transcoding (Legacy Fallback)...")
            self._run_proc(inject_progress_flags(cmd_cpu), cancel_check, progress_cb, duration_us)
            return True
        except Exception as e:
            if "cancelled" in str(e).lower(): raise
            logging.error(f"FFmpeg CPU encoding failed: {e}")
            return False

    def _get_video_info(self, filepath):
        """Uses ffprobe to extract resolution, fps, and codecs from a video."""
        ffprobe_path = self.ffmpeg_path.replace('ffmpeg', 'ffprobe')
        cmd = [
            ffprobe_path,
            '-v', 'error',
            '-show_entries', 'stream=width,height,r_frame_rate,codec_name:format=duration',
            '-of', 'json',
            filepath
        ]
        
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            info = json.loads(result.stdout)
            stream = info['streams'][0]
            
            width = stream.get('width')
            height = stream.get('height')
            fps_raw = stream.get('r_frame_rate', '30/1')
            duration = float(info.get('format', {}).get('duration', 0))
            
            return {
                'width': width,
                'height': height,
                'fps': fps_raw,
                'vcodec': stream.get('codec_name', 'h264'),
                'duration_us': int(duration * 1000000)
            }
        except Exception as e:
            logging.error(f"Failed to probe video {filepath}: {e}")
            return None

    def _transcode_intro(self, main_video_info, output_dir, cancel_check=None):
        """Transcodes the intro video to match the main video's properties exactly."""
        if not self.intro_path or not os.path.exists(self.intro_path):
            logging.warning("Intro video not found or not configured. Skipping intro addition.")
            return None
            
        transcoded_intro_path = os.path.join(output_dir, "intro_transcoded.mp4")
        if os.path.exists(transcoded_intro_path):
            return transcoded_intro_path # Already done for a previous video

        logging.info("Transcoding intro video to match main video properties...")
        
        width = main_video_info.get('width', 1920)
        height = main_video_info.get('height', 1080)
        fps = main_video_info.get('fps', '30000/1001')
        
        # We enforce h264 and aac for broad compatibility and seamless merging
        # STAGE 1: Full GPU (Zero-Copy)
        cmd_nvenc_full = [
            self.ffmpeg_path, '-y',
            '-hwaccel', 'cuda',
            '-hwaccel_output_format', 'cuda',
            '-i', self.intro_path,
            '-vf', f'scale_cuda={width}:{height}:format=nv12',
            '-r', str(fps),
            '-c:v', 'h264_nvenc', '-preset', 'p6', '-rc', 'vbr', '-cq', '24', '-b:v', '0',
            '-c:a', 'aac', '-ar', '44100',
            transcoded_intro_path
        ]
        
        # STAGE 2: Hybrid GPU (CPU Decode + GPU Scale/Encode)
        # More robust if hwaccel_output_format cuda fails for specific sources
        cmd_nvenc_hybrid = [
            self.ffmpeg_path, '-y',
            '-i', self.intro_path,
            '-vf', f'hwupload_cuda,scale_cuda={width}:{height}:format=nv12',
            '-r', str(fps),
            '-c:v', 'h264_nvenc', '-preset', 'p6', '-rc', 'vbr', '-cq', '24', '-b:v', '0',
            '-c:a', 'aac', '-ar', '44100',
            transcoded_intro_path
        ]

        # STAGE 3: Full CPU
        cmd_cpu = [
            self.ffmpeg_path, '-y',
            '-i', self.intro_path,
            '-vf', f'scale={width}:{height},setsar=1:1',
            '-r', str(fps),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', '-ar', '44100',
            transcoded_intro_path
        ]
        
        try:
            if self._run_ffmpeg_with_fallback(cmd_nvenc_full, cmd_nvenc_hybrid, cmd_cpu, cancel_check):
                return transcoded_intro_path
        except Exception as e:
            logging.error(f"Intro transcode failed: {e}. Cleaning up partial file...")
            if os.path.exists(transcoded_intro_path):
                try: os.remove(transcoded_intro_path)
                except: pass
            raise
        return None

    def process(self, video_data, cancel_check=None, progress_cb=None):
        """
        Transcodes intro and merges it with the downloaded video.
        """
        filepath = video_data['filepath']
        video_id = video_data['id']
        video_dir = os.path.dirname(filepath)
        final_output = os.path.join(video_dir, f"{video_id}_final.mp4")
        
        if os.path.exists(final_output):
            logging.info(f"Final video already exists: {final_output}. Skipping transcoding.")
            if progress_cb: progress_cb(100)
            return final_output

        if not os.path.exists(filepath):
            logging.error(f"Main video file not found: {filepath}")
            return None

        # Step 1: Probe main video
        main_info = self._get_video_info(filepath)
        if not main_info:
            logging.error("Cannot proceed without main video info.")
            return None
            
        # Resolution Logic: Resolution-matching (4K stays 4K, 1080p stays 1080p)
        target_w = main_info.get('width') or 1920
        target_h = main_info.get('height') or 1080
            
        # Optional Step 2: Transcode Intro to match main video
        prepared_intro = self._transcode_intro(main_info, self.work_dir, cancel_check)
        
        # Step 3: Standardize Main Video (always, to ensure H264/AAC compatibility)
        logging.info(f"Standardizing main video ({target_w}x{target_h}) to H264/AAC...")
        standardized_main = os.path.join(video_dir, "main_standardized.mp4")
        
        try:
            if not os.path.exists(standardized_main):
                # STAGE 1: Full GPU (Zero-Copy)
                cmd_nvenc_full = [
                    self.ffmpeg_path, '-y',
                    '-hwaccel', 'cuda',
                    '-hwaccel_output_format', 'cuda',
                    '-i', filepath,
                    '-vf', f'scale_cuda={target_w}:{target_h}:format=nv12',
                    '-c:v', 'h264_nvenc', '-preset', 'p6', '-rc', 'vbr', '-cq', '24', '-b:v', '0',
                    '-c:a', 'aac',
                    standardized_main
                ]
                
                # STAGE 2: Hybrid GPU (Safe decode + GPU Scale/Encode)
                cmd_nvenc_hybrid = [
                    self.ffmpeg_path, '-y',
                    '-i', filepath,
                    '-vf', f'hwupload_cuda,scale_cuda={target_w}:{target_h}:format=nv12',
                    '-c:v', 'h264_nvenc', '-preset', 'p6', '-rc', 'vbr', '-cq', '24', '-b:v', '0',
                    '-c:a', 'aac',
                    standardized_main
                ]

                # STAGE 3: Full CPU Fallback
                cmd_cpu = [
                    self.ffmpeg_path, '-y',
                    '-i', filepath,
                    '-vf', f'scale={target_w}:{target_h},setsar=1:1',
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                    '-c:a', 'aac',
                    standardized_main
                ]
                
                if not self._run_ffmpeg_with_fallback(cmd_nvenc_full, cmd_nvenc_hybrid, cmd_cpu, cancel_check, progress_cb, main_info.get('duration_us')):
                    raise Exception("Transcoding failed across all stages.")

            # Step 4: Concat if intro exists, else standardized_main is our final result
            if prepared_intro:
                list_file_path = os.path.join(video_dir, 'concat_list.txt')
                with open(list_file_path, 'w', encoding='utf-8') as f:
                    # Escape paths for ffmpeg concat list
                    f.write(f"file '{os.path.abspath(prepared_intro).replace(os.sep, '/')}'\n")
                    f.write(f"file '{os.path.abspath(standardized_main).replace(os.sep, '/')}'\n")
                    
                logging.info("Merging intro and main video...")
                cmd_concat = [
                    self.ffmpeg_path, '-y', '-f', 'concat', '-safe', '0',
                    '-i', list_file_path, '-c', 'copy', final_output
                ]
                self._run_proc(cmd_concat, cancel_check)
            else:
                logging.info("No intro prepended. Final output is copy of standardized version.")
                import shutil
                shutil.copy2(standardized_main, final_output)
                
            return final_output

        except Exception as e:
            # CLEANUP: Remove partial files on failure or cancellation
            logging.error(f"Processing interrupted: {e}. Cleaning up partial files...")
            for f in [standardized_main, final_output]:
                if os.path.exists(f):
                    try: 
                        os.remove(f)
                        logging.info(f"Cleaned up partial file: {f}")
                    except: pass
            raise # Ensure the runner receives the exception to update task status
