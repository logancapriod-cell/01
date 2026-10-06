"""Exercise collection storage, scheduling, exports and real FFmpeg editing."""
import io
import json
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import core, storage
from app.main import app


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous = storage.DATA_DIR, storage.DB_PATH
        storage.DATA_DIR = Path(cls.temp.name) / 'data'
        storage.DB_PATH = storage.DATA_DIR / 'virallab.db'
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.clip = Path(cls.temp.name) / 'input.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                        '-t', '3', '-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', str(cls.clip)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        storage.DATA_DIR, storage.DB_PATH = cls.previous
        cls.temp.cleanup()

    def import_video(self, suffix='1', **extra):
        item = {'id': suffix, 'url': 'https://www.tiktok.com/@test/video/' + suffix,
                'title': '三个细节，做出更好的早餐', 'author': 'test', 'views': 200000,
                'likes': 20000, 'comments': 1200, 'shares': 1800, 'duration': 3,
                'published_at': core.utcnow(), **extra}
        response = self.client.post('/api/import', json={'videos': [item]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['video_ids'][0]

    def wait_job(self, job_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.client.get('/api/jobs/' + job_id).json()
            if result['status'] in ('completed', 'failed'):
                return result
            time.sleep(.1)
        self.fail('Job did not finish within timeout')

    def test_collection_adapter_job_persists_and_deduplicates(self):
        metadata = core.normalize({'id': '999', 'url': 'https://www.douyin.com/video/999',
                                   'title': '收纳实测', 'view_count': 10000, 'like_count': 800})
        with patch('app.core.extract', return_value=[metadata]) as extractor:
            response = self.client.post('/api/collect', json={'url': metadata['url']})
            self.assertEqual(response.status_code, 202)
            result = self.wait_job(response.json()['job_id'])
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['result']['count'], 1)
            extractor.assert_called_once_with(metadata['url'])
        id1 = result['result']['video_ids'][0]
        metadata['views'] = 12000
        id2 = storage.upsert_video(metadata)
        self.assertEqual(id1, id2)
        self.assertEqual(storage.get_video(id1)['view_growth'], 2000)

    def test_automatic_pipeline_collects_scores_downloads_and_edits_once(self):
        url = 'https://www.tiktok.com/@auto/video/777'
        metadata = core.normalize({'id': '777', 'url': url, 'title': '自动二剪测试',
                                   'view_count': 1000000, 'like_count': 100000,
                                   'timestamp': time.time(), 'duration': 3})
        def download_stub(url, directory):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / 'source.mp4'
            path.write_bytes(self.clip.read_bytes())
            return path
        with patch('app.core.extract', return_value=[metadata]), patch('app.main.download_original', side_effect=download_stub) as downloader:
            response = self.client.post('/api/sources', json={'name': '自动流水线', 'url': url,
                'auto_edit': True, 'min_score': 65, 'auto_seconds': 2, 'auto_speed': 1.25, 'auto_caption': '自动剪辑实测'})
            source_id = response.json()['id']
            collection = self.client.post(f'/api/sources/{source_id}/run').json()['job_id']
            job = self.wait_job(collection)
            self.assertEqual(job['status'], 'completed', job)
            self.assertEqual(len(job['result']['auto_edit_jobs']), 1)
            edited = self.wait_job(job['result']['auto_edit_jobs'][0])
            self.assertEqual(edited['status'], 'completed', edited)
            self.assertEqual(edited['result']['options']['end'], 2)
            self.assertEqual(self.client.get(edited['result']['edited']['download_url']).status_code, 200)
            downloader.assert_called_once()
            second = self.wait_job(self.client.post(f'/api/sources/{source_id}/run').json()['job_id'])
            self.assertEqual(second['result']['auto_edit_jobs'], [])
            self.client.delete(f'/api/sources/{source_id}')

    def test_silent_storyboard_preview_renders_expected_duration(self):
        video_id = self.import_video('666')
        plan = self.client.post(f'/api/videos/{video_id}/plans', json={'duration': 15}).json()
        job = self.wait_job(self.client.post(f"/api/plans/{plan['id']}/render").json()['job_id'])
        self.assertEqual(job['status'], 'completed', job)
        file = Path(self.temp.name) / 'storyboard-preview.mp4'
        file.write_bytes(self.client.get(job['result']['download_url']).content)
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(file)], text=True))
        self.assertAlmostEqual(float(info['format']['duration']), 15, delta=.1)
        self.assertEqual(len(info['streams']), 1)
        self.assertEqual(info['streams'][0]['codec_type'], 'video')

    def test_failed_collection_records_actionable_error(self):
        with patch('app.core.extract', side_effect=ValueError('平台登录失效')):
            response = self.client.post('/api/collect', json={'url': 'https://www.tiktok.com/@test/video/9'})
            job = self.wait_job(response.json()['job_id'])
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error'], '平台登录失效')

    def test_import_is_validated_before_writes_and_missing_metrics_not_fabricated(self):
        before = self.client.get('/api/stats').json()['real_videos']
        response = self.client.post('/api/import', json={'videos': [
            {'url': 'https://www.douyin.com/video/123', 'title': 'valid'},
            {'url': 'https://example.com/private', 'title': 'invalid'}]})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/api/stats').json()['real_videos'], before)
        id1 = self.import_video('112', views=None, likes=None, comments=None, shares=None)
        video = storage.get_video(id1)
        self.assertIsNone(video['views'])
        self.assertIsNone(video['engagement'])
        self.assertFalse(video['metrics_complete'])

    def test_platform_validation_and_cross_origin_protection(self):
        for url in ['https://127.0.0.1/admin', 'https://tiktok.com.evil.test/video/1',
                    'https://name:pass@www.tiktok.com/video/1', 'http://www.tiktok.com/video/1',
                    'https://www.douyin.com/user/123']:
            self.assertEqual(self.client.post('/api/collect', json={'url': url}).status_code, 422)
        response = self.client.post('/api/collect', json={'url': 'https://www.tiktok.com/@a/video/1'}, headers={'Origin': 'https://evil.test'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(core.clean_url('分享给你 https://v.douyin.com/abcd/ 复制打开'), 'https://v.douyin.com/abcd/')

    def test_plan_edit_export_and_invalid_timing(self):
        video_id = self.import_video('333')
        response = self.client.post(f'/api/videos/{video_id}/plans', json={'topic': '早餐', 'audience': '上班族', 'duration': 30})
        self.assertEqual(response.status_code, 201)
        plan = response.json()
        self.assertIn('未分析', plan['basis'])
        self.assertEqual(plan['storyboard'][-1]['end'], 30)
        body = {key: plan[key] for key in ('title', 'caption', 'storyboard')}
        body['storyboard'][0]['voiceover'] = '我自己的实拍台词'
        self.assertEqual(self.client.patch(f"/api/plans/{plan['id']}", json=body).status_code, 200)
        response = self.client.get(f"/api/plans/{plan['id']}/export")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn('我自己的实拍台词', archive.read('shooting-plan.md').decode())
            self.assertEqual(json.loads(archive.read('storyboard.json'))['id'], plan['id'])
        body['storyboard'][1]['start'] = 0
        self.assertEqual(self.client.patch(f"/api/plans/{plan['id']}", json=body).status_code, 422)

    def test_real_upload_edit_preserves_original_and_changes_duration_and_canvas(self):
        video_id = self.import_video('444')
        original_bytes = self.clip.read_bytes()
        response = self.client.post(f'/api/videos/{video_id}/upload', files={'file': ('input.mp4', original_bytes, 'video/mp4')})
        self.assertEqual(response.status_code, 201, response.text)
        original_url = response.json()['download_url']
        self.assertAlmostEqual(response.json()['duration'], 3, delta=.1)
        response = self.client.post(f'/api/videos/{video_id}/edit', json={
            'start': .5, 'end': 2.5, 'speed': 1.25, 'aspect': 'square', 'fit': 'cover',
            'caption': "100% 好吃: '测试'", 'mute': False})
        self.assertEqual(response.status_code, 202)
        job = self.wait_job(response.json()['job_id'])
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(self.client.get(original_url).content, original_bytes)
        output = Path(self.temp.name) / 'edited-test.mp4'
        output.write_bytes(self.client.get(job['result']['edited']['download_url']).content)
        raw = subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output)], text=True)
        info = json.loads(raw)
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        self.assertEqual((video['width'], video['height']), (720, 720))
        self.assertAlmostEqual(float(info['format']['duration']), 1.6, delta=.12)
        self.assertTrue(any(s['codec_type'] == 'audio' for s in info['streams']))

    def test_local_video_upload_creates_editable_record_without_platform_metrics(self):
        response = self.client.post('/api/local-videos', files={'file': ('my-original.mp4', self.clip.read_bytes(), 'video/mp4')})
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        video = storage.get_video(record['video_id'])
        self.assertEqual(video['platform'], 'local')
        self.assertEqual(video['title'], 'my-original')
        self.assertEqual(video['url'], '')
        self.assertFalse(video['is_demo'])
        self.assertIsNone(video['views'])
        self.assertIn(video['id'], [x['id'] for x in self.client.get('/api/videos?mode=real').json()['videos']])
        response = self.client.post(f"/api/videos/{video['id']}/edit", json={'start': .2, 'end': 1.5, 'caption': '本地视频测试'})
        job = self.wait_job(response.json()['job_id'])
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(self.client.get(job['result']['edited']['download_url']).status_code, 200)

    def test_invalid_local_upload_does_not_create_a_video(self):
        before = self.client.get('/api/stats').json()['real_videos']
        response = self.client.post('/api/local-videos', files={'file': ('fake.mp4', b'not a video', 'video/mp4')})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/api/stats').json()['real_videos'], before)

    def test_invalid_file_and_invalid_range_fail_without_success_outputs(self):
        video_id = self.import_video('555')
        r = self.client.post(f'/api/videos/{video_id}/upload', files={'file': ('not-video.mp4', b'fake data', 'video/mp4')})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.get(f'/api/videos/{video_id}/media').json()['media'], [])
        self.client.post(f'/api/videos/{video_id}/upload', files={'file': ('input.mp4', self.clip.read_bytes(), 'video/mp4')})
        r = self.client.post(f'/api/videos/{video_id}/edit', json={'start': 0, 'end': 20})
        job = self.wait_job(r.json()['job_id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('3.0', job['error'])

    def test_scheduled_source_runs_without_manual_trigger(self):
        url = 'https://www.tiktok.com/@scheduled/video/888'
        metadata = core.normalize({'id': '888', 'url': url, 'title': '定时任务实测'})
        with patch('app.core.extract', return_value=[metadata]) as extractor:
            response = self.client.post('/api/sources', json={'name': '自动采集测试', 'url': url, 'interval_minutes': 5})
            self.assertEqual(response.status_code, 201)
            source_id = response.json()['id']
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rows = self.client.get('/api/sources').json()['sources']
                source = next(x for x in rows if x['id'] == source_id)
                if source['last_run']:
                    break
                time.sleep(.2)
            self.assertIsNotNone(source['last_run'])
            self.assertIsNone(source['last_error'])
            extractor.assert_called_once_with(url)
            self.assertEqual(self.client.post('/api/sources', json={'name': '重复', 'url': url}).status_code, 409)
            self.assertEqual(self.client.patch(f'/api/sources/{source_id}', json={'enabled': False}).status_code, 200)
            self.assertEqual(self.client.delete(f'/api/sources/{source_id}').status_code, 200)


if __name__ == '__main__':
    unittest.main()
