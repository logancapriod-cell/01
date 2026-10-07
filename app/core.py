"""Collection adapters, transparent viral scoring, and editable remake plans."""
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

ALLOWED_HOSTS = ('tiktok.com', 'douyin.com', 'iesdouyin.com')


def clean_url(value: str) -> str:
    # Accept the share text copied from a mobile app as well as a plain URL.
    match = re.search(r'https?://[^\s<>"\u3000]+', value)
    if not match:
        raise ValueError('请输入 TikTok 或抖音视频链接；也可以直接粘贴分享文案。')
    url = match.group().rstrip('.,;，。；）)')
    parts = urlparse(url)
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError('只支持不含登录凭据的 HTTPS 平台链接。')
    if not any(host == domain or host.endswith('.' + domain) for domain in ALLOWED_HOSTS):
        raise ValueError('仅支持 tiktok.com、douyin.com 和 iesdouyin.com 域名。')
    if 'douyin.com' in host and '/user/' in parts.path:
        raise ValueError('当前抖音适配器支持视频和分享链接；账号主页请通过 JSON 数据导入。')
    return url


def platform_for(url: str) -> str:
    return 'tiktok' if 'tiktok.com' in (urlparse(url).hostname or '') else 'douyin'


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def metric(value):
    if value is None:
        return None
    try:
        result = int(value)
        return max(0, result)
    except (TypeError, ValueError, OverflowError):
        return None


def viral_score(video: dict) -> dict:
    views, likes, comments, shares = [metric(video.get(k)) for k in ('views', 'likes', 'comments', 'shares')]
    views_known = views is not None and views > 0
    engagement = ((likes or 0) + 3 * (comments or 0) + 5 * (shares or 0)) / views if views_known else None
    reach = min(1.0, math.log10(1 + (views or 0)) / 7)
    quality = min(1.0, (engagement or 0) / .12)
    freshness = 0.0
    try:
        published_raw = video.get('published_at')
        if not isinstance(published_raw, str):
            raise ValueError('Unknown publication time')
        published = datetime.fromisoformat(published_raw.replace('Z', '+00:00'))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age = max(0, (datetime.now(timezone.utc) - published).total_seconds() / 86400)
        freshness = math.exp(-age / 14)
    except (KeyError, TypeError, ValueError):
        pass
    growth = max(0, metric(video.get('view_growth')) or 0)
    momentum = min(1.0, growth / max(views or 1, 1) / .2)
    score = round(100 * (.45 * reach + .30 * quality + .15 * freshness + .10 * momentum))
    return {'score': score, 'engagement': round(engagement * 100, 2) if engagement is not None else None,
            'score_details': {'reach': round(45 * reach, 1), 'engagement': round(30 * quality, 1),
                              'freshness': round(15 * freshness, 1), 'momentum': round(10 * momentum, 1)},
            'metrics_complete': all(v is not None for v in (views, likes, comments, shares))}


def normalize(info: dict, fallback_url: str = '') -> dict:
    url = info.get('webpage_url') or info.get('original_url') or info.get('url') or fallback_url
    if url and not url.startswith('http'):
        url = fallback_url
    url = clean_url(url)
    timestamp = info.get('timestamp') or info.get('release_timestamp')
    published = info.get('published_at')
    if not published and timestamp:
        try:
            published = datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            pass
    if not published and info.get('upload_date'):
        try:
            published = datetime.strptime(info['upload_date'], '%Y%m%d').replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    title = str(info.get('title') or info.get('description') or '未命名视频')[:200]
    description = str(info.get('description') or title)[:6000]
    result = {
        'external_id': str(info.get('id') or info.get('external_id') or url)[:300],
        'platform': platform_for(url), 'url': url,
        'title': title, 'description': description,
        'author': str(info.get('uploader') or info.get('creator') or info.get('author') or '未知作者')[:200],
        'duration': metric(info.get('duration')), 'published_at': published,
        'views': metric(info.get('view_count', info.get('views'))),
        'likes': metric(info.get('like_count', info.get('likes'))),
        'comments': metric(info.get('comment_count', info.get('comments'))),
        'shares': metric(info.get('repost_count', info.get('shares'))),
        'tags': [str(t)[:60] for t in (info.get('tags') or re.findall(r'#([\w\u4e00-\u9fff]+)', description))[:20]],
        'is_demo': False, 'collected_at': utcnow(),
    }
    result.update(viral_score(result))
    return result


def douyin_detail_error(url, stderr, cookie_file=None):
    # DouyinIE uses this message for every empty detail response, including 403
    # and incompatible responses. It does not establish that a login expired.
    if platform_for(url) == 'douyin' and 'fresh cookies' in stderr.lower():
        status = '已使用登录文件' if cookie_file else '尚未配置登录文件'
        return (f'抖音详情接口未返回视频数据（{status}）。这类错误也可能来自平台访问限制或采集适配器不兼容，'
                '不能据此判断登录已失效。当前自动采集未成功，可以上传本地原片继续二剪。')
    return None


def extract(url: str) -> list[dict]:
    url = clean_url(url)
    command = [sys.executable, '-m', 'yt_dlp', '--dump-single-json', '--flat-playlist',
               '--playlist-end', '20', '--skip-download', '--socket-timeout', '15',
               '--retries', '1', '--extractor-retries', '1', '--no-warnings']
    cookie_file = os.environ.get('VIRALLAB_COOKIES_FILE')
    if cookie_file:
        if not os.path.isfile(cookie_file):
            raise ValueError('VIRALLAB_COOKIES_FILE 指向的文件不存在。')
        command += ['--cookies', cookie_file]
    # Platform-only input, no shell, bounded execution, TLS verification retained.
    try:
        proc = subprocess.run(command + ['--', url], capture_output=True, text=True, timeout=100)
    except subprocess.TimeoutExpired:
        raise ValueError('平台请求超时，请稍后重试，或使用 JSON 导入。') from None
    if proc.returncode:
        error = proc.stderr.lower()
        detail_error = douyin_detail_error(url, error, cookie_file)
        if detail_error:
            raise ValueError(detail_error)
        if any(word in error for word in ('cookie', 'login', 'sign in', 'captcha', 'verify')):
            if cookie_file:
                raise ValueError('已使用登录文件，但平台仍要求登录或验证。请在浏览器打开同一视频，登录并完成验证后，重新读取或导入登录状态；平台也可能限制此采集适配器。可以上传本地原片继续二剪。')
            raise ValueError('平台要求登录或验证。请打开「连接设置 → 平台登录」，配置本机浏览器登录状态或自己的 cookies.txt 后重试。也可以上传本地原片直接二剪。')
        if 'unsupported url' in error:
            raise ValueError('该链接类型尚不受采集适配器支持，请使用单个视频分享链接或 JSON 导入。')
        if any(word in error for word in ('403', 'blocked', 'proxy', 'connection', 'resolve', 'network', 'unable to download')):
            raise ValueError('平台拒绝请求或当前网络不可达。请检查网络和登录状态，或通过 JSON 导入。')
        raise ValueError('平台采集失败，请检查链接是否有效；详细原因可在平台访问验证后排查。')
    try:
        payload = json.loads(proc.stdout)
        entries = payload.get('entries') if payload.get('_type') == 'playlist' else [payload]
        videos = []
        for entry in (entries or []):
            if entry:
                try:
                    videos.append(normalize(entry, url))
                except ValueError:
                    continue
        if not videos:
            raise ValueError('平台没有返回可用视频。')
        return videos
    except json.JSONDecodeError:
        raise ValueError('平台返回的数据无法解析。') from None


def make_plan(video: dict, brief: dict) -> dict:
    """Offline planning uses metadata, not claimed audiovisual understanding."""
    topic = brief.get('topic') or video['title'].split('#')[0].strip()[:60]
    audience = brief.get('audience') or '对这个主题感兴趣的短视频观众'
    tone = brief.get('tone') or '自然、有信息量'
    duration = max(15, min(90, int(brief.get('duration', 30))))
    fractions = [0, .1, .3, .57, .82, 1]
    shots = [
        ('结果先行', f'你也遇到过「{topic}」的问题吗？先看这个结果。', '用自己的素材展示前后变化，第一帧直接给结果。', '紧凑切入；字幕突出结果，不夸大效果。'),
        ('交代场景', f'这是给{audience}的一次实际尝试，我会讲清楚怎么做。', '人物或产品中景，加一个具体使用场景。', '中近景交替，删除不必要的铺垫。'),
        ('核心步骤', f'第一步，明确目标。第二步，展示关键操作。第三步，对比变化。', f'围绕「{topic}」拍摄三个可验证的动作或细节。', '每个动作一个镜头；关键步骤用简短字幕标注。'),
        ('证据与差异', '最有用的地方是这个细节；适用条件和局限也一起告诉你。', '拍摄真实证据、细节特写或实际对比，加入你的独特观点。', '放慢关键细节，留出阅读字幕的时间。'),
        ('互动收尾', '你更想看哪一个步骤？留言告诉我，下次展开。', '人物正面或结果全景，停留一秒收尾。', '给一个明确问题；不要重复片头。'),
    ]
    storyboard = []
    for i, (name, voiceover, visual, edit) in enumerate(shots):
        start, end = round(duration * fractions[i]), round(duration * fractions[i + 1])
        storyboard.append({'index': i + 1, 'start': start, 'end': end, 'name': name,
                           'voiceover': voiceover, 'visual': visual, 'edit': edit})
    tags = video.get('tags') or []
    return {
        'topic': topic, 'audience': audience, 'tone': tone, 'duration': duration,
        'title': f'{topic}：一次讲清楚的实测',
        'basis': '基于标题、描述、时长和互动数据的模板方案；未分析原视频画面、音频或逐字稿。',
        'analysis': [
            {'label': '选题线索', 'text': video['description'][:220] or video['title']},
            {'label': '传播信号', 'text': f"当前评分 {video['score']} / 100；加权互动率 {video['engagement']}%。" if video.get('engagement') is not None else '缺少播放量，无法计算互动率；评分信息有限。'},
            {'label': '可借鉴的结构', 'text': '结果 → 场景 → 步骤 → 证据 → 互动；这是推荐创作结构，不是对原片分镜的识别。'},
            {'label': '原创差异', 'text': f'面向{audience}，用自己的素材、证据和观点，以「{tone}」表达。'},
        ],
        'storyboard': storyboard,
        'caption': f'关于{topic}，把过程和结果都拍给你看。你还想知道哪些细节？',
        'hashtags': list(dict.fromkeys(tags + ['实测', '经验分享']))[:6],
        'checklist': ['拍摄自己的片头结果画面', '准备三个核心操作镜头', '核对涉及数字与效果的事实',
                      '使用自己拥有或获授权的素材、音乐与字体', '检查字幕与节奏，发布前再次预览'],
        'generator': '本地模板', 'created_at': utcnow(),
    }


def plan_markdown(video: dict, plan: dict) -> str:
    lines = [f"# {plan['title']}", '', f"参考链接：{video['url']}", f"生成方式：{plan['generator']}",
             f"方案依据：{plan['basis']}", f"目标受众：{plan['audience']}", f"语气：{plan['tone']}",
             f"时长：{plan['duration']} 秒", '', '## 分镜与口播', '']
    for s in plan['storyboard']:
        lines += [f"### {s['start']}–{s['end']} 秒 · {s['name']}", f"口播：{s['voiceover']}",
                  f"画面：{s['visual']}", f"剪辑：{s['edit']}", '']
    lines += ['## 发布文案', plan['caption'], ' '.join('#' + t for t in plan['hashtags']), '', '## 拍摄清单']
    lines += ['- ' + x for x in plan['checklist']]
    return '\n'.join(lines)
