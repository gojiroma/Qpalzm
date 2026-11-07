"""QR で URL を受け取る（旧 reqr の QR 部分）。

poet.blue の画面が「ルーム」を作って QR（/scan.html#<room>）を出す。
スマホでそれを読むとクリップボードの URL がルームに入り、
画面側のポーリングが一度だけ取り出して開く。

  POST /api/qr               {}                 -> {room}
  POST /api/qr               {room, url}        -> {ok}
  GET  /api/qr?room=<room>                      -> {url|null}（取り出したら空にする）

Vercel のサーバーレスではプロセスに状態を持てないので Neon (Postgres) に置く。
画面がポーリングしている間は seen_at が更新され、ルームは生き続ける。
"""

import os
import re
import secrets
import string
from urllib.parse import urlparse

import psycopg
from flask import Flask, jsonify, request

app = Flask(__name__)
app.url_map.strict_slashes = False

ROOM_TTL = '6 hours'          # 最後のポーリングからこれだけ経ったルームは消す
ROOM_LEN = 10
ROOM_RE = re.compile(r'^[A-Za-z0-9]{%d}$' % ROOM_LEN)
ALPHABET = string.ascii_letters + string.digits
MAX_URL = 2048

_schema_ready = False


def _dsn():
    """Neon/Vercel が注入する接続文字列を拾う"""
    for key in ('DATABASE_URL', 'POSTGRES_URL', 'NEON_DATABASE_URL', 'POSTGRES_PRISMA_URL'):
        value = os.environ.get(key)
        if value:
            return value
    raise RuntimeError('DATABASE_URL is not set (Neon integration required)')


def connect():
    global _schema_ready
    conn = psycopg.connect(_dsn(), autocommit=True)
    if not _schema_ready:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qr_rooms (
                id      TEXT PRIMARY KEY,
                url     TEXT,
                seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        _schema_ready = True
    return conn


def is_url(value):
    parsed = urlparse(value)
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def reply(body, status=200):
    res = jsonify(body)
    res.status_code = status
    res.headers['Cache-Control'] = 'no-store'
    return res


def create_room():
    with connect() as conn:
        conn.execute(f"DELETE FROM qr_rooms WHERE seen_at < now() - INTERVAL '{ROOM_TTL}'")
        for _ in range(10):
            room = ''.join(secrets.choice(ALPHABET) for _ in range(ROOM_LEN))
            row = conn.execute(
                "INSERT INTO qr_rooms (id) VALUES (%s) ON CONFLICT (id) DO NOTHING RETURNING id",
                (room,),
            ).fetchone()
            if row:
                return reply({'room': room})
    return reply({'error': 'could not allocate a room'}, 503)


def send(room, url):
    if not ROOM_RE.match(room) or len(url) > MAX_URL or not is_url(url):
        return reply({'error': 'invalid room or url'}, 400)
    with connect() as conn:
        row = conn.execute(
            f"UPDATE qr_rooms SET url = %s WHERE id = %s"
            f" AND seen_at > now() - INTERVAL '{ROOM_TTL}' RETURNING id",
            (url, room),
        ).fetchone()
    return reply({'ok': True}) if row else reply({'error': 'no such room'}, 404)


def take(room):
    """URL があれば一度だけ返して空にする。ポーリング自体がルームの延命になる。"""
    if not ROOM_RE.match(room):
        return reply({'error': 'invalid room'}, 400)
    with connect() as conn:
        row = conn.execute(
            f"""UPDATE qr_rooms r SET url = NULL, seen_at = now()
                FROM (SELECT id, url FROM qr_rooms WHERE id = %s
                      AND seen_at > now() - INTERVAL '{ROOM_TTL}' FOR UPDATE) old
                WHERE r.id = old.id RETURNING old.url""",
            (room,),
        ).fetchone()
    return reply({'url': row[0]}) if row else reply({'error': 'no such room'}, 404)


# Vercel では /api/qr として呼ばれる。パスの扱いに依存しないよう全部ここで受ける。
@app.route('/', defaults={'_path': ''}, methods=['GET', 'POST'])
@app.route('/<path:_path>', methods=['GET', 'POST'])
def qr(_path):
    if request.method == 'GET':
        return take(request.args.get('room', ''))
    data = request.get_json(silent=True) or {}
    if 'url' in data:
        return send(str(data.get('room', '')), str(data.get('url', '')).strip())
    return create_room()


if __name__ == '__main__':
    app.run(debug=True)
