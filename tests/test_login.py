import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import core, login, storage
from app.main import app

HEADER = '# Netscape HTTP Cookie File\n'
HEADERS = {'X-ViralLab-Local': '1', 'Origin': 'http://127.0.0.1'}


def cookie(domain='.douyin.com', value='fake-session', expires='0'):
    return f'{domain}\tTRUE\t/\tTRUE\t{expires}\tsessionid\t{value}\n'


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = storage.DATA_DIR, storage.DB_PATH
        storage.DATA_DIR = Path(self.temp.name) / 'data'
        storage.DB_PATH = storage.DATA_DIR / 'virallab.db'
        self.environment = patch.dict(os.environ, {'VIRALLAB_LOCAL_LOGIN': '1', 'VIRALLAB_COOKIES_FILE': ''})
        self.environment.start()
        self.client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 12345))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        storage.DATA_DIR, storage.DB_PATH = self.previous
        self.temp.cleanup()

    def upload(self, contents, **kwargs):
        return self.client.post('/api/login/file', files={'file': ('cookies.txt', contents, 'text/plain')},
                                headers=kwargs.pop('headers', HEADERS), **kwargs)

    def test_upload_filters_domains_restores_after_restart_and_clear_preserves_browser(self):
        response = self.upload(HEADER + cookie() + cookie('.example.com', 'other-site-secret'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['platforms'], ['douyin.com'])
        saved = login.login_path().read_text()
        self.assertIn('fake-session', saved)
        self.assertNotIn('other-site-secret', saved)
        self.assertNotIn('fake-session', response.text)
        self.assertNotIn('fake-session', self.client.get('/api/settings').text)
        os.environ.pop('VIRALLAB_COOKIES_FILE')
        login.restore()
        self.assertEqual(os.environ['VIRALLAB_COOKIES_FILE'], str(login.login_path().resolve()))
        from yt_dlp.cookies import YoutubeDLCookieJar
        jar = YoutubeDLCookieJar(os.environ['VIRALLAB_COOKIES_FILE'])
        jar.load()
        self.assertEqual(len(jar), 1)
        browser_file = Path(self.temp.name) / 'browser-original.txt'
        browser_file.write_text('browser remains logged in')
        response = self.client.delete('/api/login', headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(login.login_path().exists())
        self.assertNotIn('VIRALLAB_COOKIES_FILE', os.environ)
        self.assertEqual(browser_file.read_text(), 'browser remains logged in')

    def test_reimport_replaces_same_platform_and_preserves_other_platform(self):
        self.assertEqual(self.upload(HEADER + cookie() + cookie('.tiktok.com', 'tiktok-session')).status_code, 200)
        self.assertEqual(self.upload(HEADER + cookie(value='new-douyin-session')).status_code, 200)
        saved = login.login_path().read_text()
        self.assertIn('tiktok-session', saved)
        self.assertIn('new-douyin-session', saved)
        self.assertNotIn('fake-session', saved)

    def test_invalid_expired_unrelated_and_oversized_files_do_not_replace_login(self):
        self.upload(HEADER + cookie())
        before = login.login_path().read_bytes()
        cases = ['{"secret":"do-not-log-this"}', HEADER + 'do-not-log-this\n',
                 HEADER + cookie(expires='do-not-log-this'), HEADER + cookie(expires='1'),
                 HEADER + cookie('.douyin.com.evil.test'), HEADER + cookie('.example.com')]
        for body in cases:
            capture = io.StringIO()
            with contextlib.redirect_stderr(capture):
                response = self.upload(body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn('do-not-log-this', response.text + capture.getvalue())
            self.assertEqual(login.login_path().read_bytes(), before)
        self.assertEqual(self.upload(b'x' * (login.MAX_COOKIE_BYTES + 1)).status_code, 413)
        self.assertEqual(login.login_path().read_bytes(), before)

    def test_login_configuration_requires_local_mode_host_client_and_custom_header(self):
        with patch.dict(os.environ, {'VIRALLAB_LOCAL_LOGIN': '0'}):
            self.assertEqual(self.upload(HEADER + cookie()).status_code, 403)
        self.assertEqual(self.upload(HEADER + cookie(), headers={}).status_code, 403)
        self.assertEqual(self.upload(HEADER + cookie(), headers={**HEADERS, 'Origin': 'https://evil.test'}).status_code, 403)
        with TestClient(app, base_url='http://evil.test', client=('127.0.0.1', 12345)) as client:
            self.assertEqual(client.post('/api/login/browser', json={'browser': 'firefox'},
                                         headers={'X-ViralLab-Local': '1'}).status_code, 403)
        with TestClient(app, base_url='http://127.0.0.1', client=('192.0.2.1', 12345)) as client:
            self.assertEqual(client.post('/api/login/browser', json={'browser': 'firefox'}, headers=HEADERS).status_code, 403)

    def test_real_firefox_adapter_reads_fixture_database_and_filters_other_sites(self):
        profile = Path(self.temp.name) / 'FirefoxProfile'
        profile.mkdir()
        with sqlite3.connect(profile / 'cookies.sqlite') as db:
            db.execute('PRAGMA user_version=16')
            db.execute('CREATE TABLE moz_cookies(host,name,value,path,expiry,isSecure)')
            db.executemany('INSERT INTO moz_cookies VALUES(?,?,?,?,?,?)', [
                ('.douyin.com', 'sessionid', 'fake-firefox-session', '/', 4102444800000, 1),
                ('.example.com', 'private', 'unrelated-secret', '/', 4102444800000, 1)])
        with patch('yt_dlp.cookies._firefox_browser_dirs', return_value=[str(profile)]):
            response = self.client.post('/api/login/browser', json={'browser': 'firefox'}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        saved = login.login_path().read_text()
        self.assertIn('fake-firefox-session', saved)
        self.assertNotIn('unrelated-secret', saved)
        self.assertNotIn('fake-firefox-session', response.text)

    def test_browser_error_is_sanitized_and_collection_uses_saved_file(self):
        with patch('yt_dlp.cookies.extract_cookies_from_browser', side_effect=RuntimeError('private-profile / secret-session')):
            response = self.client.post('/api/login/browser', json={'browser': 'edge'}, headers=HEADERS)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('secret-session', response.text)
        self.assertIn('Firefox', response.text)
        self.upload(HEADER + cookie())
        result = type('Result', (), {'returncode': 1, 'stderr': 'Fresh cookies are needed'})()
        with patch('app.core.subprocess.run', return_value=result) as run:
            with self.assertRaisesRegex(ValueError, '已使用登录文件'):
                core.extract('https://www.douyin.com/video/7333154430646373672')
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--cookies') + 1], str(login.login_path().resolve()))


if __name__ == '__main__':
    unittest.main()
