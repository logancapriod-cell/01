"""Local-only platform login configuration; never return cookie values or diagnostics."""
import os
import tempfile
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path

from . import storage

MAX_COOKIE_BYTES = 2 * 1024 * 1024


def login_path():
    return storage.DATA_DIR / 'private' / 'platform-cookies.txt'


def enabled():
    return os.environ.get('VIRALLAB_LOCAL_LOGIN') == '1'


def restore():
    if enabled() and login_path().is_file() and not os.environ.get('VIRALLAB_COOKIES_FILE'):
        os.environ['VIRALLAB_COOKIES_FILE'] = str(login_path().resolve())


def platform_domain(domain):
    domain = domain.lower().lstrip('.')
    return any(domain == root or domain.endswith('.' + root) for root in ('douyin.com', 'tiktok.com'))


def parse(data):
    try:
        text = data.decode('utf-8-sig')
        lines = text.splitlines()
        if not lines or lines[0].strip() not in ('# Netscape HTTP Cookie File', '# HTTP Cookie File'):
            raise ValueError()
        jar = MozillaCookieJar()
        # Parse strictly without third-party warnings that may contain cookie values.
        for line in lines[1:]:
            http_only = line.startswith('#HttpOnly_')
            if http_only:
                line = line[len('#HttpOnly_'):]
            elif not line.strip() or line.startswith('#'):
                continue
            domain, include_subdomains, path, secure, expires, name, value = line.split('\t')
            if not domain or not name or not path.startswith('/') or include_subdomains not in ('TRUE', 'FALSE') or secure not in ('TRUE', 'FALSE') or (include_subdomains == 'TRUE') != domain.startswith('.'):
                raise ValueError()
            if expires and (not expires.isascii() or not expires.isdigit()):
                raise ValueError()
            expiry = int(expires) if expires else None
            if expiry == 0:
                expiry = None
            jar.set_cookie(Cookie(0, name, value, None, False, domain, include_subdomains == 'TRUE',
                                  domain.startswith('.'), path, True, secure == 'TRUE', expiry,
                                  expiry is None, None, None, {'HttpOnly': ''} if http_only else {}))
        return jar
    except Exception:
        raise ValueError('登录文件格式不正确，请选择 Netscape 格式的 cookies.txt，不能使用 JSON 或截图。') from None


def save(jar):
    cookies = [cookie for cookie in jar if platform_domain(cookie.domain) and not cookie.is_expired()]
    if not cookies:
        raise ValueError('没有找到有效的抖音或 TikTok cookies。请先在浏览器打开平台、登录并完成验证，再重试。')
    target = login_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = parse(target.read_bytes()) if target.is_file() else MozillaCookieJar()
    # Replace cookies for the imported platform, including any old login session.
    roots = {root for root in ('douyin.com', 'tiktok.com') if any(
        cookie.domain.lstrip('.') == root or cookie.domain.lstrip('.').endswith('.' + root) for cookie in cookies)}
    for cookie in list(merged):
        if not platform_domain(cookie.domain) or cookie.is_expired() or any(
                cookie.domain.lstrip('.') == root or cookie.domain.lstrip('.').endswith('.' + root) for root in roots):
            merged.clear(cookie.domain, cookie.path, cookie.name)
    for cookie in cookies:
        merged.set_cookie(cookie)
    handle, temporary = tempfile.mkstemp(prefix='cookies-', suffix='.txt', dir=target.parent)
    os.close(handle)
    try:
        merged.save(temporary, ignore_discard=True, ignore_expires=False)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    os.environ['VIRALLAB_COOKIES_FILE'] = str(target.resolve())
    return {'configured': True, 'platforms': sorted(roots)}


class QuietLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def from_browser(browser):
    from yt_dlp.cookies import extract_cookies_from_browser
    try:
        jar = extract_cookies_from_browser(browser, logger=QuietLogger())
    except Exception:
        raise ValueError('无法读取这个浏览器的登录状态。请把工作台地址复制到另一个浏览器打开，再完全退出用于登录平台的浏览器后重试；Chrome / Edge 的加密保护也可能阻止读取。可以改用 Firefox 登录平台，或选择自己导出的 cookies.txt。') from None
    return save(jar)


def clear():
    login_path().unlink(missing_ok=True)
    os.environ.pop('VIRALLAB_COOKIES_FILE', None)
    return {'configured': False}
