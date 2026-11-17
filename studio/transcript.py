"""Live words are appended in small pieces; a checkpoint keeps reopening them quick."""
import json
import threading
import sqlite3
import contextlib
from pathlib import Path

_lock = threading.Lock()


def read(folder):
    # A reader must see the checkpoint and its journal from the same moment.
    with _lock:
        return _read(folder)


def _read(folder):
    folder = Path(folder)
    if (folder / "live-words.sqlite3").exists():
        return LiveStore(folder).between(0, float("inf"))
    try:
        words = json.loads((folder / "words.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        words = []
    journal = folder / "words.jsonl"
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                start = entry["from"]
                if start <= len(words):
                    words.extend(entry["words"][max(0, len(words) - start):])
            except (ValueError, KeyError, TypeError):
                break  # a power failure may leave only the last line unfinished
    return words


def append(folder, start, words):
    with _lock, (Path(folder) / "words.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"from": start, "words": words}) + "\n")


def checkpoint(folder, words):
    with _lock:
        folder = Path(folder)
        tmp = folder / "words.json.tmp"
        tmp.write_text(json.dumps(words), encoding="utf-8")
        tmp.replace(folder / "words.json")
        # If interrupted before this, read() skips words already in the checkpoint.
        (folder / "words.jsonl").write_text("", encoding="utf-8")


class LiveStore:
    """Indexed history on disk. Only the recent part needs to stay in memory."""
    def __init__(self, folder):
        self.path = Path(folder) / "live-words.sqlite3"
        new = not self.path.exists()
        legacy = _read(folder) if new else []
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS words (n INTEGER PRIMARY KEY, s REAL, e REAL, w TEXT)")
            conn.execute("CREATE INDEX IF NOT EXISTS words_time ON words(s)")
        if legacy:
            self.append(0, legacy)

    @contextlib.contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def append(self, start, words):
        with self.connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO words VALUES (?, ?, ?, ?)",
                             [(start + i, w["s"], w["e"], w["w"]) for i, w in enumerate(words)])

    def count(self):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]

    def between(self, start, end):
        with self.connect() as conn:
            return [{"w": w, "s": s, "e": e} for s, e, w in conn.execute(
                "SELECT s, e, w FROM words WHERE s>=? AND s<=? ORDER BY s, n", (start, end))]

    def checkpoint(self):
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


class Timeline:
    """A bounded sequence whose positive indices stay on the recording clock."""
    def __init__(self, keep):
        from collections import deque
        self.values = deque(maxlen=keep)
        self.total = 0

    def append(self, value):
        self.values.append(value)
        self.total += 1

    def __len__(self):
        return self.total

    def __iter__(self):
        return iter(self.values)

    def __getitem__(self, key):
        values = list(self.values)
        base = self.total - len(values)
        if isinstance(key, slice):
            start, end, step = key.indices(self.total)
            return values[max(0, start - base):max(0, end - base):step]
        index = key if key >= 0 else self.total + key
        if index < base or index >= self.total:
            raise IndexError(index)
        return values[index - base]
