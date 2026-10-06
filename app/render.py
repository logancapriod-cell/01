"""Render an honest silent storyboard preview, without copying source media."""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def font(size):
    candidates = [os.environ.get('VIRALLAB_FONT', ''), '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                  '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise ValueError('未找到可用字体，请配置 VIRALLAB_FONT。')


def wrap(text, width=19):
    return '\n'.join(textwrap.wrap(str(text), width=width, break_long_words=True))


def render_preview(plan, directory):
    if not shutil.which('ffmpeg'):
        raise ValueError('缺少 FFmpeg，无法生成分镜预演。脚本导出仍可使用。')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, shot in enumerate(plan['storyboard']):
        image = Image.new('RGB', (720, 1280), '#10171f')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((40, 64, 240, 115), radius=24, fill='#ee773f')
        draw.text((65, 74), 'VIRAL LAB', font=font(24), fill='#121820')
        draw.text((42, 168), 'STORYBOARD / 分镜预演', font=font(24), fill='#8998a6')
        draw.text((40, 235), f'{i+1:02d}', font=font(118), fill='#ee773f')
        draw.text((43, 420), shot['name'], font=font(52), fill='#faf4ed')
        draw.multiline_text((43, 510), wrap(shot['visual']), font=font(32), fill='#b9c5d0', spacing=16)
        draw.rounded_rectangle((35, 830, 685, 1150), radius=24, fill='#202b37')
        draw.text((62, 858), '口播草稿', font=font(24), fill='#ee773f')
        draw.multiline_text((62, 918), wrap(shot['voiceover'], 19), font=font(29), fill='#f9f4ec', spacing=14)
        draw.text((42, 1200), f"{shot['start']}–{shot['end']}s  ·  无声预演 / 待替换实拍素材", font=font(22), fill='#8998a6')
        image_path = directory / f'shot-{i}.png'
        image.save(image_path)
        manifest += [f"file '{image_path.name}'", f"duration {shot['end'] - shot['start']}"]
    manifest.append(f"file 'shot-{len(plan['storyboard'])-1}.png'")
    (directory / 'concat.txt').write_text('\n'.join(manifest), encoding='utf-8')
    proc = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '1',
                           '-i', 'concat.txt', '-t', str(plan['duration']), '-vf', 'fps=24', '-c:v', 'libx264',
                           '-threads', '2', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                           'preview.mp4'], cwd=directory, capture_output=True, text=True, timeout=180)
    if proc.returncode:
        raise ValueError('分镜视频编码失败，请检查 FFmpeg 是否支持 libx264。')
    return directory / 'preview.mp4'
