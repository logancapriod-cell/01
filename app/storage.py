import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from .core import utcnow, viral_score

DATA_DIR = Path(os.environ.get('VIRALLAB_DATA_DIR', Path(__file__).resolve().parents[1] / 'data'))
DB_PATH = DATA_DIR / 'virallab.db'


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def initialize():
    with connect() as db:
        db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL,
                is_demo INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
                UNIQUE(platform, external_id, is_demo));
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, last_run TEXT, next_run TEXT NOT NULL,
                last_error TEXT, created_at TEXT NOT NULL,
                auto_edit INTEGER NOT NULL DEFAULT 0, min_score INTEGER NOT NULL DEFAULT 65,
                auto_caption TEXT NOT NULL DEFAULT '', auto_speed REAL NOT NULL DEFAULT 1,
                auto_seconds INTEGER NOT NULL DEFAULT 30);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                source_id INTEGER, result TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT);
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL, kind TEXT NOT NULL,
                path TEXT NOT NULL, created_at TEXT NOT NULL);
        ''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(sources)')}
        for name, declaration in {'auto_edit': 'INTEGER NOT NULL DEFAULT 0',
                                  'min_score': 'INTEGER NOT NULL DEFAULT 65',
                                  'auto_caption': "TEXT NOT NULL DEFAULT ''",
                                  'auto_speed': 'REAL NOT NULL DEFAULT 1',
                                  'auto_seconds': 'INTEGER NOT NULL DEFAULT 30'}.items():
            if name not in columns:
                db.execute(f'ALTER TABLE sources ADD COLUMN {name} {declaration}')
        # A process restart cannot resume a worker that no longer exists.
        db.execute("UPDATE jobs SET status='failed', error='服务已重启，请重新执行任务。', finished_at=? WHERE status IN ('queued','running')", (utcnow(),))


def upsert_video(video):
    demo = int(bool(video.get('is_demo')))
    with connect() as db:
        row = db.execute('SELECT id, payload FROM videos WHERE platform=? AND external_id=? AND is_demo=?',
                         (video['platform'], video['external_id'], demo)).fetchone()
        if row:
            old = json.loads(row['payload'])
            video['first_collected_at'] = old.get('first_collected_at', old.get('collected_at'))
            if video.get('views') is not None and old.get('views') is not None:
                video['view_growth'] = max(0, video['views'] - old['views'])
                video['growth_since'] = old.get('collected_at')
            video.update(viral_score(video))
            db.execute('UPDATE videos SET payload=? WHERE id=?', (json.dumps(video, ensure_ascii=False), row['id']))
            return row['id']
        video['first_collected_at'] = video.get('collected_at', utcnow())
        cursor = db.execute('INSERT INTO videos(platform,external_id,is_demo,payload) VALUES(?,?,?,?)',
                            (video['platform'], video['external_id'], demo, json.dumps(video, ensure_ascii=False)))
        return cursor.lastrowid


def get_video(video_id):
    with connect() as db:
        row = db.execute('SELECT id, payload FROM videos WHERE id=?', (video_id,)).fetchone()
    return {'id': row['id'], **json.loads(row['payload'])} if row else None


def all_videos():
    with connect() as db:
        rows = db.execute('SELECT id,payload FROM videos').fetchall()
    result = []
    for row in rows:
        video = {'id': row['id'], **json.loads(row['payload'])}
        video.update(viral_score(video))
        result.append(video)
    return result


def seed_demo():
    samples = [
        ('douyin', '早上 10 分钟，把普通早餐做成仪式感', '小陈的日常', '生活方式', 1820000, 156000, 9200, 18800, 28, 1, 'morning'),
        ('tiktok', '3 个镜头，拍出电影感咖啡', 'framebyframe', '摄影技巧', 3260000, 248000, 8200, 32000, 24, 2, 'coffee'),
        ('douyin', '租来的房子，也能有自己的小花园', '一平米生活', '家居改造', 960000, 86000, 4600, 12500, 42, 1, 'room'),
        ('tiktok', '把城市的声音，变成一段节奏', 'sounddiary', '创意灵感', 2150000, 181000, 6400, 23000, 18, 3, 'city'),
        ('douyin', '一件白衬衫的 5 种周末穿法', '穿搭实验室', '穿搭', 740000, 57000, 2400, 6800, 32, 2, 'style'),
        ('tiktok', '一个平底锅，就够了', 'tinykitchen', '美食', 1240000, 104000, 5300, 17000, 35, 4, 'food'),
    ]
    for index, (platform, title, author, tag, views, likes, comments, shares, duration, days, artwork) in enumerate(samples):
        # Explicit fixture namespace. These links are examples, never claimed as collected videos.
        video = {'external_id': f'demo-{index}', 'platform': platform, 'title': title, 'author': author,
                 'url': '', 'description': title + ' #' + tag, 'tags': [tag], 'views': views, 'likes': likes,
                 'comments': comments, 'shares': shares, 'duration': duration, 'is_demo': True,
                 'published_at': (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
                 'collected_at': utcnow(), 'artwork': artwork, 'view_growth': round(views * .14)}
        video.update(viral_score(video))
        with connect() as db:
            exists = db.execute('SELECT 1 FROM videos WHERE external_id=? AND is_demo=1', (video['external_id'],)).fetchone()
        if not exists:
            upsert_video(video)
