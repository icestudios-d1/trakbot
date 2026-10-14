CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, user TEXT NOT NULL, device TEXT NOT NULL,
  ts REAL NOT NULL, dur REAL NOT NULL, app TEXT, title TEXT, url TEXT,
  domain TEXT, idle INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ev_user_ts ON events(user, ts);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, pattern TEXT NOT NULL,
  category TEXT NOT NULL, label TEXT NOT NULL, alert INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, user TEXT NOT NULL, device TEXT,
  rule_id INTEGER, category TEXT, app TEXT, title TEXT, url TEXT, acked INTEGER DEFAULT 0);
-- Screenshot pixels live in ImageKit; D1 keeps the metadata + ImageKit file id.
CREATE TABLE IF NOT EXISTS screenshots(
  id INTEGER PRIMARY KEY, user TEXT NOT NULL, device TEXT NOT NULL,
  ts REAL NOT NULL, app TEXT, title TEXT,
  ik_file_id TEXT NOT NULL, ik_path TEXT NOT NULL, bytes INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ss_user_ts ON screenshots(user, ts);
CREATE INDEX IF NOT EXISTS ss_ts ON screenshots(ts);
