"""Reuse a user's successful AI answers, including after a restart."""
import contextlib
import json
import sqlite3
import threading
import time

from . import config, ctx

KEEP_SECONDS = 7 * 86400
KEEP_ANSWERS = 256
_pending = {}
_lock = threading.Lock()


@contextlib.contextmanager
def request(key):
    try:
        path = config.user_dir(ctx.uid()) / "ai-cache.sqlite3"
    except (ctx.NoUser, ValueError):
        yield None
        return
    identity = (str(path), key)
    while True:
        with _lock:
            waiting = _pending.get(identity)
            if waiting is None:
                event = _pending[identity] = threading.Event()
                break
        from . import ai
        while not waiting.wait(.1):
            ai.remaining()
    try:
        yield path
    finally:
        with _lock:
            _pending.pop(identity, None)
            event.set()


def read(path, key):
    if path is None or not path.exists():
        return None
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=2)) as conn:
            row = conn.execute("SELECT answer FROM answers WHERE key=? AND saved>=?", (key, time.time() - KEEP_SECONDS)).fetchone()
        return json.loads(row[0]) if row else None
    except (OSError, sqlite3.Error, ValueError):
        return None


def save(path, key, answer):
    if path is None:
        return
    try:
        body = json.dumps(answer, ensure_ascii=False, allow_nan=False)
        if len(body.encode("utf-8")) > 128_000:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path, timeout=2)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, saved REAL, answer TEXT)")
            conn.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?)", (key, time.time(), body))
            conn.execute("DELETE FROM answers WHERE saved<?", (time.time() - KEEP_SECONDS,))
            conn.execute("DELETE FROM answers WHERE key IN (SELECT key FROM answers ORDER BY saved DESC LIMIT -1 OFFSET ?)", (KEEP_ANSWERS,))
        path.chmod(0o600)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        pass  # saving an answer must never stop a video
