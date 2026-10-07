import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient
from app import browser_capture as capture, core, storage
from app.main import app


class BrowserCaptureTests(unittest.TestCase):
    def test_media_domain_validation_and_matching_video_only(self):
        for url in ['https://127.0.0.1/video', 'https://douyinvod.com.evil.test/a.mp4',
                    'http://v3.douyinvod.com/a.mp4', 'https://user:pass@v3.douyinvod.com/a.mp4']:
            self.assertFalse(capture.media_url_allowed(url))
        address = 'https://v3-web.douyinvod.com/path/video.mp4'
        self.assertTrue(capture.media_url_allowed(address))
        record, urls = capture.metadata_from_payload({'items': [
            {'aweme_id': '999', 'desc': '推荐视频', 'video': {'play_addr': {'url_list': [address]}}},
            {'aweme_id': '123', 'desc': '目标视频', 'author': {'nickname': '目标作者'},
             'video': {'play_addr': {'url_list': [address]}}, 'statistics': {'digg_count': 20}}]},
            '123', 'https://www.douyin.com/video/123')
        self.assertEqual(record['title'], '目标视频')
        self.assertEqual(record['likes'], 20)
        self.assertIsNone(record['views'])
        self.assertEqual(urls, [address])

    def test_download_rejects_unsafe_redirect_and_deletes_partial_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'source.mp4'
            output.write_bytes(b'partial')
            request = httpx.Request('GET', 'https://v3-web.douyinvod.com/video')
            response = httpx.Response(302, headers={'location': 'http://127.0.0.1/private'}, request=request)
            with patch('app.browser_capture.httpx.Client') as client:
                client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = response
                with self.assertRaisesRegex(ValueError, '媒体域名'):
                    capture.download_loaded_video(str(request.url), output, [], 'test', 'https://www.douyin.com/')
                self.assertEqual(client.return_value.__enter__.return_value.stream.call_count, 1)
            self.assertFalse(output.exists())

    def test_local_endpoint_rejects_remote_and_non_platform_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            old = storage.DATA_DIR, storage.DB_PATH
            storage.DATA_DIR = Path(temporary)
            storage.DB_PATH = storage.DATA_DIR / 'test.db'
            try:
                with patch.dict(os.environ, {'VIRALLAB_LOCAL_LOGIN': '1'}):
                    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1000)) as client:
                        body = {'url': 'https://www.douyin.com/video/123'}
                        self.assertEqual(client.post('/api/browser-capture', json=body).status_code, 403)
                        self.assertEqual(client.post('/api/browser-capture', json={'url': 'https://example.com/video'},
                                                    headers={'X-ViralLab-Local': '1'}).status_code, 422)
            finally:
                storage.DATA_DIR, storage.DB_PATH = old

    def test_real_browser_job_loads_metadata_downloads_original_and_edits_it(self):
        browser = os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium')
        if not Path(browser).is_file():
            self.skipTest('Fixture browser executable unavailable')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clip = root / 'input.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                '-i', 'testsrc2=size=320x240:rate=30', '-t', '3', '-c:v', 'libx264', '-threads', '2', str(clip)], check=True)
            identifier = '91234567890123'
            cookies_seen = []
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path.startswith('/video/'):
                        data = b'<html><title>Fixture video</title><body><video autoplay muted src="/clip.mp4"></video><script>fetch("/aweme/v1/web/aweme/detail/")</script></body></html>'
                        kind = 'text/html'
                    elif self.path == '/clip.mp4':
                        data, kind = clip.read_bytes(), 'video/mp4'
                        cookies_seen.append(self.headers.get('Cookie', ''))
                    elif self.path.startswith('/aweme/'):
                        data = json.dumps({'aweme_detail': {'aweme_id': identifier, 'desc': '浏览器采集真实流程',
                            'author': {'nickname': '测试作者'}, 'statistics': {'digg_count': 50},
                            'video': {'play_addr': {'url_list': [f'http://127.0.0.1:{self.server.server_port}/clip.mp4']}}}}).encode()
                        kind = 'application/json'
                    else:
                        self.send_error(404); return
                    self.send_response(200)
                    self.send_header('Content-Type', kind)
                    self.send_header('Content-Length', str(len(data)))
                    if kind == 'text/html': self.send_header('Set-Cookie', 'platform_sid=fake-login; Path=/')
                    self.end_headers()
                    self.wfile.write(data)

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            old = storage.DATA_DIR, storage.DB_PATH
            storage.DATA_DIR, storage.DB_PATH = root / 'data', root / 'data' / 'test.db'
            def real_capture(url, directory, progress):
                return capture.capture(url, directory, progress, timeout=15,
                    launch_options={'executable_path': browser, 'headless': True, 'args': ['--no-sandbox']})
            def wait(client, job_id):
                for _ in range(300):
                    result = client.get('/api/jobs/' + job_id).json()
                    if result['status'] in ('completed', 'failed'): return result
                    time.sleep(.1)
                self.fail('Browser job timed out')
            try:
                with patch.dict(os.environ, {'VIRALLAB_LOCAL_LOGIN': '1'}), \
                     patch.object(core, 'ALLOWED_HOSTS', ('127.0.0.1',)), \
                     patch.object(core, 'clean_url', side_effect=lambda url: url), \
                     patch.object(capture, 'media_url_allowed', side_effect=lambda url: urlparse(url).hostname == '127.0.0.1'), \
                     patch('app.main.capture_browser_video', side_effect=real_capture):
                    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
                        response = client.post('/api/browser-capture', json={'url': f'http://127.0.0.1:{server.server_port}/video/{identifier}'},
                                               headers={'X-ViralLab-Local': '1'})
                        self.assertEqual(response.status_code, 202, response.text)
                        result = wait(client, response.json()['job_id'])
                        self.assertEqual(result['status'], 'completed', result)
                        self.assertNotIn('fake-login', json.dumps(result))
                        video = storage.get_video(result['result']['video_id'])
                        self.assertEqual(video['title'], '浏览器采集真实流程')
                        self.assertEqual(video['likes'], 50)
                        self.assertIsNone(video['views'])
                        self.assertEqual(client.get(result['result']['original']['download_url']).content, clip.read_bytes())
                        self.assertTrue(any('platform_sid=fake-login' in cookie for cookie in cookies_seen))
                        edited = client.post(f"/api/videos/{video['id']}/edit", json={'end': 2, 'aspect': 'square'})
                        job = wait(client, edited.json()['job_id'])
                        self.assertEqual(job['status'], 'completed', job)
                        self.assertEqual(client.get(job['result']['edited']['download_url']).status_code, 200)
            finally:
                storage.DATA_DIR, storage.DB_PATH = old
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
