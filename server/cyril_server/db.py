import os
import sqlite3
import threading
from contextlib import contextmanager

from .default_rules import DEFAULT_RULES

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, user TEXT NOT NULL, device TEXT NOT NULL,
  ts REAL NOT NULL, dur REAL NOT NULL, app TEXT, title TEXT, url TEXT,
  domain TEXT, idle INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ev_user_ts ON events(user, ts);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, pattern TEXT NOT NULL,
  category TEXT NOT NULL, label TEXT NOT NULL, alert INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, user TEXT NOT NULL, device TEXT,
  rule_id INTEGER, category TEXT, app TEXT, title TEXT, url TEXT, acked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS agent_status(device TEXT PRIMARY KEY, user TEXT, ts REAL NOT NULL, info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_names(user TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS screenshots(
  id INTEGER PRIMARY KEY, user TEXT NOT NULL, device TEXT NOT NULL,
  ts REAL NOT NULL, app TEXT, title TEXT, path TEXT NOT NULL, bytes INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ss_user_ts ON screenshots(user, ts);
CREATE TABLE IF NOT EXISTS update_log(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, user TEXT NOT NULL, device TEXT NOT NULL,
  from_version TEXT, to_version TEXT, ok INTEGER NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS ul_ts ON update_log(ts);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            if c.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0:
                c.executemany(
                    "INSERT INTO rules(kind,pattern,category,label,alert) VALUES(?,?,?,?,?)",
                    DEFAULT_RULES)

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    def rules(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM rules ORDER BY priority DESC, id ASC").fetchall()
        return [dict(r) for r in rows]
