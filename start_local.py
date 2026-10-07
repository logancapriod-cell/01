"""Start the installed local app and open its page after a functional health check."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def is_our_server(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/settings', timeout=1) as response:
            payload = json.load(response)
        return 'extractor_version' in payload and 'capabilities' in payload
    except (OSError, ValueError, urllib.error.URLError):
        return False


def select_port():
    preferred = int(os.environ.get('PORT', '8000'))
    if is_our_server(preferred):
        return preferred, True
    for port in [preferred, *range(8001, 8011)]:
        with socket.socket() as candidate:
            try:
                candidate.bind(('127.0.0.1', port))
            except OSError:
                continue
        return port, False
    raise RuntimeError('8000–8010 端口均被占用，请关闭其他程序后重试。')


def main():
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise RuntimeError(f'未找到 {tool}，请使用 启动Windows.cmd 安装依赖。')
    port, existing = select_port()
    url = f'http://127.0.0.1:{port}'
    if existing:
        print('工作台已在运行，正在打开浏览器。', flush=True)
        webbrowser.open(url)
        return
    print('正在启动 Viral Lab。请保留此窗口，关闭窗口会停止工具。', flush=True)
    environment = dict(os.environ, VIRALLAB_LOCAL_LOGIN='1')
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                                '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=environment)
    try:
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError('服务启动失败，请查看此窗口上方的错误。')
            if is_our_server(port):
                print('工作台已就绪，正在打开浏览器。', flush=True)
                webbrowser.open(url)
                process.wait()
                return
            time.sleep(.25)
        raise RuntimeError('启动超时，请关闭后重试。')
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'启动失败：{error}', file=sys.stderr)
        sys.exit(1)
