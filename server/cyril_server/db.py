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
CREATE TABLE IF NOT EXISTS agent_commands(
  id INTEGER PRIMARY KEY, device TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL, delivered REAL,
  status TEXT NOT NULL DEFAULT 'pending', screenshot_id INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS agent_status(device TEXT PRIMARY KEY, user TEXT, ts REAL NOT NULL, info TEXT NOT NULL);
-- Remote uninstall orders, one per device. Durable (unlike the TTL'd agent_commands screenshot rail): an offline
-- machine stays `pending` until it next checks in, then is torn down. History (events/screenshots) is kept; only
-- the device is marked removed. status: pending -> removing (agent flushed + went quiet) -> removed (privileged
-- teardown done) | canceled.
CREATE TABLE IF NOT EXISTS device_removals(
  device TEXT PRIMARY KEY, requested REAL NOT NULL, requested_by TEXT,
  status TEXT NOT NULL DEFAULT 'pending', flushed REAL, removed REAL, detail TEXT);
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
-- Team-wide reports filter on time alone; (user, ts) cannot serve that, so every overview used to scan the table.
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ss_ts ON screenshots(ts);
CREATE INDEX IF NOT EXISTS al_ts ON alerts(ts);
-- Employee groups (business units, "not allocated yet" machines...). An employee is in at most one group; a group
-- marked `excluded` is left out of the regular (unfiltered) reports and only shows when picked explicitly.
CREATE TABLE IF NOT EXISTS groups(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, excluded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_groups(user TEXT PRIMARY KEY, group_id INTEGER NOT NULL);
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
