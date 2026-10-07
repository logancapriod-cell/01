"""Collect a playable video using an ordinary, visible browser session.

The user handles platform login and challenges. No signatures or verification
tokens are generated here. Only metadata for the opened video is retained.
"""
import json
import re
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import core, storage
from .media import MAX_BYTES, probe

capture_lock = threading.Lock()
MEDIA_DOMAINS = ('douyinvod.com', 'douyin.com', 'iesdouyin.com', 'tiktok.com',
                 'tiktokcdn.com', 'tiktokcdn-us.com', 'tiktokcdn-eu.com',
                 'byteoversea.com', 'ibytedtos.com', 'muscdn.com', 'snssdk.com',
                 'bytecdn.cn', 'bytecdn.com', 'zjcdn.com', 'pstatp.com')


def media_url_allowed(url):
    try:
        parts = urlparse(url)
        host = (parts.hostname or '').lower()
        return parts.scheme == 'https' and not parts.username and not parts.password and parts.port in (None, 443) and any(
            host == domain or host.endswith('.' + domain) for domain in MEDIA_DOMAINS)
    except ValueError:
        return False


def video_id(url):
    match = re.search(r'/(?:video|note)/(\d+)', urlparse(url).path)
    return match.group(1) if match else None


def metadata_from_payload(payload, wanted_id, canonical):
    # Bound traversal and only accept the currently opened video's identifier.
    pending = [payload]
    for _ in range(5000):
        if not pending:
            break
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        identifier = str(item.get('aweme_id') or item.get('id') or '')
        media = item.get('video')
        if identifier == wanted_id and isinstance(media, dict):
            stats = item.get('statistics') or item.get('stats') or {}
            author = item.get('author') or {}
            if not isinstance(stats, dict):
                stats = {}
            if not isinstance(author, dict):
                author = {}
            addresses = media.get('play_addr') or media.get('playAddr') or media.get('play_addr_h264')
            candidates = addresses.get('url_list', []) if isinstance(addresses, dict) else [addresses]
            if not isinstance(candidates, list):
                candidates = []
            candidates = [address for address in candidates if isinstance(address, str) and media_url_allowed(address)]
            record = core.normalize({'id': identifier, 'url': canonical,
                'title': item.get('desc') or item.get('description') or '浏览器采集视频',
                'description': item.get('desc') or item.get('description') or '',
                'uploader': author.get('nickname') or author.get('uniqueId'),
                'timestamp': item.get('create_time') or item.get('createTime'),
                'view_count': stats.get('play_count', stats.get('playCount')),
                'like_count': stats.get('digg_count', stats.get('diggCount')),
                'comment_count': stats.get('comment_count', stats.get('commentCount')),
                'repost_count': stats.get('share_count', stats.get('shareCount'))})
            record['collection_method'] = '浏览器辅助采集'
            return record, candidates
        pending.extend(value for value in item.values() if isinstance(value, (dict, list)))
    return None, []


def download_loaded_video(url, destination, cookies, user_agent, referer):
    # Stream the ordinary browser-loaded URL. Retain watermarks and use no URL
    # substitutions. Validate each redirect, cap bytes, and validate the video.
    jar = httpx.Cookies()
    for cookie in cookies:
        jar.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie.get('path', '/'))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with httpx.Client(verify=ssl.create_default_context(), cookies=jar,
                          headers={'User-Agent': user_agent, 'Referer': referer},
                          follow_redirects=False, timeout=20) as client:
            for _ in range(6):
                if not media_url_allowed(url):
                    raise ValueError('浏览器视频地址不在支持的平台媒体域名内。')
                with client.stream('GET', url) as response:
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        url = urljoin(str(response.url), response.headers.get('location', ''))
                        continue
                    if response.status_code != 200:
                        raise ValueError('页面视频地址未返回完整文件。请重新播放视频后重试，或上传本地原片。')
                    try:
                        length = int(response.headers.get('content-length', '0'))
                    except ValueError:
                        length = 0
                    if length > MAX_BYTES:
                        raise ValueError('视频超过 300 MB 限制。')
                    size = 0
                    with destination.open('wb') as file:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise ValueError('视频超过 300 MB 限制。')
                            if time.monotonic() - started > 180:
                                raise ValueError('浏览器视频下载超时。请重试或上传本地原片。')
                            file.write(chunk)
                    return probe(destination)
            raise ValueError('视频地址重定向次数过多。')
    except Exception as error:
        destination.unlink(missing_ok=True)
        if isinstance(error, ValueError):
            raise
        raise ValueError('浏览器视频下载失败，可能是媒体地址过期或平台限制。请重试或上传本地原片。') from None


def capture(url, directory, progress, *, launch_options=None, timeout=180):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout, Error as BrowserError
    url = core.clean_url(url)
    if not capture_lock.acquire(blocking=False):
        raise ValueError('已有浏览器采集窗口正在运行。请完成或关闭该窗口后再试。')
    try:
        directory = Path(directory)
        profile = storage.DATA_DIR / 'private' / 'capture-browser'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        options = launch_options or {'channel': 'msedge', 'headless': False, 'chromium_sandbox': True}
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(str(profile), **options)
            except Exception:
                raise ValueError('无法打开浏览器采集窗口。请确认已安装 Microsoft Edge，关闭此前的采集窗口后重试。') from None
            try:
                page = context.pages[0] if context.pages else context.new_page()
                state = {'id': video_id(url), 'canonical': url, 'record': None, 'addresses': []}

                def update_target():
                    try:
                        current = core.clean_url(page.url)
                    except ValueError:
                        return False
                    identifier = video_id(current)
                    if identifier and (state['id'] is None or identifier == state['id']):
                        state['id'], state['canonical'] = identifier, current
                        return True
                    return False

                def received(response):
                    try:
                        if not update_target():
                            return
                        response_url = urlparse(response.url)
                        host = response_url.hostname or ''
                        if not any(host == domain or host.endswith('.' + domain) for domain in core.ALLOWED_HOSTS):
                            return
                        if 'json' not in response.headers.get('content-type', '').lower() or not state['id']:
                            return
                        size = int(response.headers.get('content-length', '0'))
                        if size > 5 * 1024 * 1024:
                            return
                        body = response.body()
                        if len(body) > 5 * 1024 * 1024:
                            return
                        record, addresses = metadata_from_payload(json.loads(body), state['id'], state['canonical'])
                        if record and addresses:
                            state['record'], state['addresses'] = record, addresses
                    except Exception:
                        # Never print network payloads, signed URLs or login diagnostics.
                        pass

                page.on('response', received)
                progress('浏览器已打开。请在新窗口登录平台、完成验证，并播放这条视频；工具正在等待可用视频。')
                started = time.monotonic()
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=45000)
                except BrowserTimeout:
                    pass
                except BrowserError as error:
                    if 'net::ERR_CERT_' in str(error):
                        raise ValueError('浏览器证书校验失败。请检查本机网络或系统证书后重试。') from None
                    raise ValueError('浏览器无法打开平台页面，请检查本机网络和该链接是否有效。') from None
                candidate = None
                while time.monotonic() - started < timeout:
                    if page.is_closed():
                        raise ValueError('浏览器采集窗口已关闭。需要时重新点击「浏览器辅助采集」。')
                    active = update_target()
                    try:
                        scripts = page.evaluate("""() => ['SIGI_STATE','__UNIVERSAL_DATA_FOR_REHYDRATION__','RENDER_DATA']
                            .map(id => document.getElementById(id)?.textContent || '')
                            .filter(text => text && text.length <= 5242880)""") if active and not state['record'] else []
                        source = page.evaluate("""() => Array.from(document.querySelectorAll('video'))
                            .filter(v => v.readyState >= 2 && (!v.paused || v.played.length))
                            .sort((a,b) => b.clientWidth*b.clientHeight-a.clientWidth*a.clientHeight)[0]?.currentSrc || ''""") if active else ''
                    except BrowserError:
                        if page.is_closed():
                            raise ValueError('浏览器采集窗口已关闭。') from None
                        page.wait_for_timeout(500)
                        continue
                    if scripts:
                        for script in scripts:
                            try:
                                from urllib.parse import unquote
                                payload = json.loads(script if script.lstrip().startswith(('{', '[')) else unquote(script))
                                record, addresses = metadata_from_payload(payload, state['id'], state['canonical'])
                                if record and addresses:
                                    state['record'], state['addresses'] = record, addresses
                                    break
                            except (ValueError, TypeError):
                                pass
                    if active and state['record'] and state['addresses']:
                        candidate = state['addresses'][0]
                        break
                    if active and state['id'] and media_url_allowed(source):
                        candidate = source
                        break
                    page.wait_for_timeout(500)
                if not candidate:
                    raise ValueError('等待播放超时，或页面使用了暂不支持的分段视频。请确认正常播放目标视频；仍失败时可上传本地原片。')
                progress('已找到页面加载的视频，正在下载并检查原片…')
                source = directory / 'source.mp4'
                info = download_loaded_video(candidate, source, context.cookies([candidate]),
                                             page.evaluate('navigator.userAgent'), state['canonical'])
                record = state['record'] or core.normalize({'id': state['id'], 'url': state['canonical'],
                    'title': page.title(), 'description': '浏览器实际播放的视频；未取得互动指标。'})
                record['duration'] = round(info['duration'])
                record['collection_method'] = '浏览器辅助采集'
                return record, source, info
            finally:
                context.close()
    except ValueError:
        raise
    except Exception:
        raise ValueError('浏览器采集未完成。请保持窗口打开、完成平台验证并播放目标视频后重试。') from None
    finally:
        capture_lock.release()
