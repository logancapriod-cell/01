"""Optional UI check. Requires Playwright and an installed Chromium executable."""
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def run():
    with tempfile.TemporaryDirectory(prefix='virallab-browser-') as temporary:
        clip = Path(temporary) / 'sample.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                        '-i', 'testsrc2=size=320x240:rate=30', '-t', '3', '-c:v', 'libx264',
                        '-threads', '2', str(clip)], check=True)
        environment = dict(os.environ, VIRALLAB_DATA_DIR=str(Path(temporary) / 'data'), VIRALLAB_LOCAL_LOGIN='1', VIRALLAB_COOKIES_FILE='')
        server = subprocess.Popen([str(ROOT / '.venv/bin/python'), '-m', 'uvicorn', 'app.main:app',
                                   '--host', '127.0.0.1', '--port', '8001'], cwd=ROOT, env=environment,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    if httpx.get('http://127.0.0.1:8001/api/health').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                            headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1050})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto('http://127.0.0.1:8001', wait_until='networkidle')
                expect(page.locator('.video-card')).to_have_count(6)

                page.locator('#open-settings').click()
                expect(page.locator('#browser-login-form')).to_be_visible()
                page.locator('#login-file').set_input_files({'name': 'cookies.txt', 'mimeType': 'text/plain',
                    'buffer': b'# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tfake-browser-test\n'})
                expect(page.locator('#login-feedback')).to_contain_text('登录状态已保存', timeout=15000)
                expect(page.locator('#login-state')).to_contain_text('已配置')
                page.reload(wait_until='networkidle')
                page.locator('#open-settings').click()
                expect(page.locator('#login-state')).to_contain_text('已配置')
                page.locator('#clear-login').click()
                expect(page.locator('#login-feedback')).to_contain_text('已清除')
                page.locator('#login-file').set_input_files({'name': 'cookies.txt', 'mimeType': 'text/plain', 'buffer': b'not-cookies'})
                expect(page.locator('#login-feedback')).to_contain_text('格式不正确')
                page.locator('#close-modal').click()

                page.route('**/api/collect', lambda route: route.fulfill(json={'job_id': 'douyin-detail-fixture'}, status=202))
                page.route('**/api/jobs/douyin-detail-fixture', lambda route: route.fulfill(json={
                    'id': 'douyin-detail-fixture', 'kind': 'collect', 'status': 'failed',
                    'error': '抖音详情接口未返回视频数据（已使用登录文件）。不能据此判断登录已失效。'}))
                page.locator('#add-video').click()
                page.locator('#collect-form [name=url]').fill('https://www.douyin.com/video/7667152036157720827')
                page.locator('#collect-form [type=submit]').click()
                expect(page.locator('#collect-progress')).to_contain_text('不能据此判断登录已失效', timeout=10000)
                expect(page.locator('#collect-progress [data-action=login]')).to_have_count(0)
                expect(page.locator('#collect-progress [data-action=browser-capture]')).to_be_visible()
                with page.expect_file_chooser() as chooser:
                    page.locator('#collect-progress [data-action=local-video]').click()
                assert chooser.value.element.get_attribute('id') == 'local-video-file'
                page.unroute('**/api/collect')
                page.unroute('**/api/jobs/douyin-detail-fixture')
                page.locator('[data-platform=tiktok]').click()
                expect(page.locator('.video-card')).to_have_count(3)
                page.locator('#video-search').fill('咖啡')
                expect(page.locator('.video-card')).to_have_count(1)

                page.locator('#video-search').fill('')
                page.get_by_role('button', name='全部平台', exact=True).click()
                expect(page.locator('.video-card')).to_have_count(6)

                page.get_by_role('button', name='生成方案', exact=True).first.click()
                page.locator('#brief-form [name=topic]').fill('咖啡拉花')
                page.locator('#brief-form [name=audience]').fill('新手咖啡爱好者')
                page.get_by_role('button', name='生成并保存方案', exact=True).click()
                expect(page.locator('#plan-edit-form')).to_be_visible()
                page.locator('#plan-edit-form [name=title]').fill('属于我的咖啡小故事')
                page.get_by_role('button', name='保存修改', exact=True).click()
                expect(page.locator('#toast')).to_contain_text('修改已保存')
                with page.expect_download() as download:
                    page.get_by_role('link', name='导出方案包', exact=True).click()
                assert download.value.suggested_filename.endswith('.zip')
                page.locator('#close-modal').click()

                page.get_by_role('button', name='下载 / 二剪', exact=True).first.click()
                page.locator('#upload-video').set_input_files(str(clip))
                expect(page.locator('#edit-progress')).to_contain_text('原片已上传', timeout=15000)
                page.locator('#edit-form [name=start]').fill('0.5')
                page.locator('#edit-form [name=end]').fill('2.5')
                page.locator('#edit-form [name=speed]').select_option('1.25')
                page.locator('#edit-form [name=caption]').fill('用自己的表达，再创作')
                page.get_by_role('button', name='下载并生成二剪视频', exact=True).click()
                expect(page.locator('#edit-progress')).to_contain_text('任务已完成', timeout=30000)
                expect(page.locator('#edit-progress video')).to_be_visible()
                with page.expect_download() as download:
                    page.get_by_role('link', name='下载二剪成片').click()
                assert download.value.suggested_filename.endswith('.mp4')
                page.screenshot(path='/tmp/virallab-edit-studio.png', full_page=True)
                page.locator('#close-modal').click()
                page.locator('[data-page=jobs]').click()
                expect(page.locator('#job-list')).to_contain_text('已完成')
                page.locator('[data-page=plans]').click()
                expect(page.locator('#plan-list')).to_contain_text('属于我的咖啡小故事')
                page.locator('[data-page=sources]').click()
                page.get_by_role('button', name='添加采集源', exact=True).first.click()
                expect(page.locator('#source-form [name=auto_edit]')).to_be_checked()
                expect(page.locator('#source-form [name=min_score]')).to_have_value('65')
                page.locator('#close-modal').click()
                page.locator('[data-page=discover]').click()
                page.screenshot(path='/tmp/virallab-desktop.png', full_page=True)
                page.locator('#local-video-file').set_input_files(str(clip))
                expect(page.locator('#edit-form')).to_be_visible(timeout=15000)
                expect(page.locator('.edit-reference')).to_contain_text('本地素材')
                expect(page.locator('.edit-reference video')).to_be_visible()
                page.locator('#close-modal').click()
                expect(page.locator('.video-card')).to_have_count(1)
                imported_id = page.locator('[data-edit-video]').first.get_attribute('data-edit-video')
                original = httpx.get(f'http://127.0.0.1:8001/api/videos/{imported_id}/media').json()['media'][0]
                browser_polls = [0]
                def capture_job(route):
                    browser_polls[0] += 1
                    route.fulfill(json={'id': 'browser-fixture', 'kind': 'browser',
                        'status': 'running' if browser_polls[0] < 3 else 'completed',
                        'result': {'progress': '请在新窗口播放视频', 'count': 1,
                                   'video_id': int(imported_id), 'video_ids': [int(imported_id)], 'original': original}})
                def capture_request(route):
                    assert route.request.headers.get('x-virallab-local') == '1'
                    route.fulfill(json={'job_id': 'browser-fixture'}, status=202)
                page.route('**/api/browser-capture', capture_request)
                page.route('**/api/jobs/browser-fixture', capture_job)
                page.locator('#add-video').click()
                page.locator('#collect-form [name=url]').fill('https://v.douyin.com/a8K-9UYxFnI/')
                page.locator('#browser-collect').click()
                expect(page.locator('#collect-progress')).to_contain_text('请在新窗口播放视频', timeout=10000)
                expect(page.get_by_role('button', name='原片已保存，开始二剪')).to_be_visible(timeout=10000)
                page.get_by_role('button', name='原片已保存，开始二剪').click()
                expect(page.locator('#edit-form')).to_be_visible()
                expect(page.locator('.edit-reference video')).to_be_visible()
                page.locator('#close-modal').click()
                page.unroute('**/api/browser-capture')
                page.unroute('**/api/jobs/browser-fixture')
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path='/tmp/virallab-mobile.png', full_page=True)
                assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                assert not errors, errors
                browser.close()
                print('UI passed: browser capture entry/progress/result/edit handoff, local login import/reload/clear/error, filters, plan editing/export, upload, actual editing, playback, MP4 download, job history, mobile layout. No JS errors.')
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    run()
