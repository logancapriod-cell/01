import json
import os
import subprocess
import sys
from pathlib import Path
from .core import clean_url

MAX_BYTES = 300 * 1024 * 1024


def download_original(url, directory):
    url = clean_url(url)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-progress', '--no-warnings',
            '--socket-timeout', '15', '--retries', '1', '--extractor-retries', '1',
            '--max-filesize', '300M', '--match-filter', 'duration <= 600',
            '-f', 'bv*[height<=1080]+ba/b[height<=1080]/best', '--merge-output-format', 'mp4',
            '--remux-video', 'mp4', '--paths', str(directory), '-o', 'source.%(ext)s']
    if os.environ.get('VIRALLAB_COOKIES_FILE'):
        cookie_file = os.environ['VIRALLAB_COOKIES_FILE']
        if not Path(cookie_file).is_file():
            raise ValueError('配置的 cookies 文件不存在。')
        args += ['--cookies', cookie_file]
    try:
        process = subprocess.run(args + ['--', url], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise ValueError('视频下载超时，请检查网络后重试。') from None
    path = directory / 'source.mp4'
    if process.returncode or not path.is_file():
        stderr = process.stderr.lower()
        if any(t in stderr for t in ('cookie', 'login', 'captcha', 'verify')):
            raise ValueError('下载需要平台登录或验证，请配置你有权使用的 cookies 文件后重试。')
        if 'filesize' in stderr or 'duration' in stderr:
            raise ValueError('仅支持下载 10 分钟以内、300 MB 以内的视频。')
        raise ValueError('原视频下载失败：平台访问受限、链接失效或网络不可达。可上传本地原片继续二剪。')
    if path.stat().st_size > MAX_BYTES:
        path.unlink()
        raise ValueError('视频超过 300 MB 限制。')
    return path


def probe(path):
    proc = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries', 'format=duration:stream=codec_type',
                           '-of', 'json', str(path)], capture_output=True, text=True, timeout=15)
    if proc.returncode:
        raise ValueError('无法识别视频文件，请上传有效的 MP4、MOV 或 WebM。')
    try:
        info = json.loads(proc.stdout)
        duration = float(info['format']['duration'])
        if duration <= 0 or duration > 600:
            raise ValueError('仅支持 10 分钟以内的视频。')
        if not any(s['codec_type'] == 'video' for s in info['streams']):
            raise ValueError('文件没有视频轨道。')
        return {'duration': duration, 'has_audio': any(s['codec_type'] == 'audio' for s in info['streams'])}
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ValueError('视频信息无法解析。') from None


def edit_video(source, directory, options):
    info = probe(source)
    start = options['start']
    end = options.get('end') if options.get('end') is not None else info['duration']
    if start >= end or end > info['duration'] + .05:
        raise ValueError(f"截取范围无效。原片时长 {info['duration']:.1f} 秒，请调整起止时间。")
    speed = options['speed']
    width, height = {'portrait': (720, 1280), 'square': (720, 720), 'landscape': (1280, 720)}[options['aspect']]
    fit = options['fit']
    if fit == 'cover':
        scale = f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}'
    else:
        scale = f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x10171f'
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    filters = [f'setpts=(PTS-STARTPTS)/{speed}', scale, 'setsar=1', 'fps=30']
    caption = options.get('caption', '').strip()
    if caption:
        # Text is read from a file, never interpolated into FFmpeg filter syntax.
        import textwrap
        caption_path = directory / 'caption.txt'
        caption_path.write_text('\n'.join(textwrap.wrap(caption, width=20)), encoding='utf-8')
        font_path = os.environ.get('VIRALLAB_FONT', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
        if not Path(font_path).is_file():
            raise ValueError('字幕字体不存在，请配置 VIRALLAB_FONT。')
        # Paths are controlled by the server. Escape special characters in configured font path.
        escaped_font = font_path.replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'")
        filters.append(f"drawtext=fontfile='{escaped_font}':textfile=caption.txt:expansion=none:fontsize=34:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=16:x=(w-text_w)/2:y=h-text_h-90")
    output = directory / 'edited.mp4'
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-filter_threads', '2',
               '-protocol_whitelist', 'file,pipe', '-ss', str(start), '-i', str(source),
               '-t', str((end - start) / speed), '-map', '0:v:0', '-vf', ','.join(filters)]
    if info['has_audio'] and not options['mute']:
        command += ['-map', '0:a:0', '-af', f'atempo={speed}', '-c:a', 'aac', '-b:a', '128k']
    else:
        command += ['-an']
    command += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-threads', '2',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)]
    try:
        process = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise ValueError('剪辑编码超时，请缩短片段后重试。') from None
    if process.returncode:
        raise ValueError('视频编码失败，请检查原片格式、字幕字体与 FFmpeg 支持。')
    return output
