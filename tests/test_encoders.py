import subprocess
import json

def check_encoder(ffmpeg_path, encoder_name):
    probe_cmd = [
        ffmpeg_path, '-hide_banner', '-y',
        '-f', 'lavfi', '-i', 'nullsrc=s=64x64',
        '-t', '0.01', '-c:v', encoder_name,
        '-f', 'null', '-'
    ]
    try:
        res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.returncode == 0
    except:
        return False

ffmpeg_path = "ffmpeg"
encoders = ['h264_qsv', 'h264_nvenc', 'hevc_qsv', 'hevc_nvenc', 'av1_qsv', 'av1_nvenc', 'libx264']

results = {}
for e in encoders:
    results[e] = check_encoder(ffmpeg_path, e)

print(json.dumps(results, indent=2))
