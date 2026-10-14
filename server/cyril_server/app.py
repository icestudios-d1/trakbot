"""Cyril server: ingest API, reports, rule + alert management, dashboard.

Self-hosted (office server, Docker). Screenshots live in SeaweedFS, not on
the app container's disk, so they survive container rebuilds and don't
bloat the SQLite file. Events/rules/alerts stay in SQLite -- small, and the
whole point of self-hosting is that nothing here depends on cloud
reachability; agents spool locally and catch up whenever this server is back.
"""
import base64
import hashlib
import hmac
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .classify import LABELS, classify, matching_rule
from .db import DB
from .objstore import ObjectStoreError, S3Store

ADMIN_TOKEN = os.environ.get("CYRIL_ADMIN_TOKEN", "")
AGENT_KEY = os.environ.get("CYRIL_AGENT_KEY", "")
DB_PATH = os.environ.get("CYRIL_DB", "data/cyril.db")
MAX_EVENT_SECONDS = 3600
MAX_SHOT_B64 = 3_000_000  # ~2.2MB decoded; agent targets far smaller JPEGs

app = FastAPI(title="Cyril")
db = DB(DB_PATH)
STATIC = Path(__file__).parent / "static"
DIST_DIR = Path(os.environ.get("CYRIL_DIST_DIR", "dist"))
if DIST_DIR.exists():
    app.mount("/dist", StaticFiles(directory=str(DIST_DIR)), name="dist")
WINSCRIPTS = Path(__file__).parent / "winscripts"

# SeaweedFS's S3 gateway (bundled as the `seaweedfs` service in docker-compose.yml).
store = S3Store(
    endpoint=os.environ.get("CYRIL_S3_ENDPOINT", "seaweedfs:8333"),
    access_key=os.environ.get("CYRIL_S3_ACCESS_KEY", ""),
    secret_key=os.environ.get("CYRIL_S3_SECRET_KEY", ""),
    bucket=os.environ.get("CYRIL_S3_BUCKET", "cyril-screenshots"),
    secure=os.environ.get("CYRIL_S3_SECURE", "false").lower() == "true",
)
store.ensure_bucket()


def _check(given: str | None, expected: str):
    if not expected or not given or not hmac.compare_digest(given, expected):
        raise HTTPException(401, "unauthorized")


def admin(authorization: str | None = Header(None)):
    _check((authorization or "").removeprefix("Bearer "), ADMIN_TOKEN)


def agent(x_api_key: str | None = Header(None)):
    _check(x_api_key, AGENT_KEY)


# ---------------------------------------------------------------- ingest
class Event(BaseModel):
    ts: float
    dur: float = Field(ge=0, le=MAX_EVENT_SECONDS)
    app: str = ""
    title: str = ""
    url: str = ""
    idle: bool = False


class IngestBatch(BaseModel):
    user: str = Field(min_length=1, max_length=128)
    device: str = Field(min_length=1, max_length=128)
    events: list[Event] = Field(max_length=2000)


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


@app.post("/api/ingest", dependencies=[Depends(agent)])
def ingest(b: IngestBatch):
    rules = db.rules()
    alerts = 0
    with db.conn() as c:
        for e in b.events:
            dom = domain_of(e.url)
            c.execute(
                "INSERT INTO events(user,device,ts,dur,app,title,url,domain,idle) VALUES(?,?,?,?,?,?,?,?,?)",
                (b.user, b.device, e.ts, e.dur, e.app, e.title, e.url, dom, int(e.idle)))
            r = matching_rule({"app": e.app, "title": e.title, "domain": dom, "idle": e.idle}, rules)
            if r and r["alert"]:
                c.execute(
                    "INSERT INTO alerts(ts,user,device,rule_id,category,app,title,url) VALUES(?,?,?,?,?,?,?,?)",
                    (e.ts, b.user, b.device, r["id"], r["category"], e.app, e.title, e.url))
                alerts += 1
    return {"stored": len(b.events), "alerts": alerts}


# --------------------------------------------------------------- reports
def _range(day: str | None, days: int):
    d = datetime.strptime(day, "%Y-%m-%d") if day else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    d = d.astimezone() if d.tzinfo else d
    start = d.timestamp()
    return start, (d + timedelta(days=days)).timestamp()


def _events(user: str | None, start: float, end: float):
    q, args = "SELECT * FROM events WHERE ts>=? AND ts<?", [start, end]
    if user:
        q += " AND user=?"
        args.append(user)
    with db.conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY ts", args)]


def _top(d: dict, n=10):
    return [{"name": k, "seconds": round(v)} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]


@app.get("/api/users", dependencies=[Depends(admin)])
def users():
    with db.conn() as c:
        rows = c.execute("SELECT user, MAX(ts+dur) last_seen, COUNT(DISTINCT device) devices FROM events GROUP BY user ORDER BY user").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/summary", dependencies=[Depends(admin)])
def summary(user: str | None = None, day: str | None = None, days: int = Query(1, ge=1, le=92)):
    start, end = _range(day, days)
    rules = db.rules()
    labels, cats = defaultdict(float), defaultdict(float)
    apps, sites = defaultdict(float), defaultdict(float)
    per_user = defaultdict(lambda: defaultdict(float))
    hourly = [defaultdict(float) for _ in range(24)]
    for e in _events(user, start, end):
        cat, lab = classify(e, rules)
        labels[lab] += e["dur"]
        cats[cat] += e["dur"]
        per_user[e["user"]][lab] += e["dur"]
        hourly[datetime.fromtimestamp(e["ts"]).hour][lab] += e["dur"]
        if lab != "idle":
            apps[e["app"] or "unknown"] += e["dur"]
            if e["domain"]:
                sites[e["domain"]] += e["dur"]
    active = sum(v for k, v in labels.items() if k != "idle")
    prod = labels.get("productive", 0)
    return {
        "start": start, "end": end, "user": user,
        "seconds": {k: round(labels.get(k, 0)) for k in (*LABELS, "idle")},
        "productivity_pct": round(100 * prod / active, 1) if active else None,
        "categories": _top(cats, 15), "apps": _top(apps), "sites": _top(sites),
        "by_user": {u: {k: round(v) for k, v in m.items()} for u, m in per_user.items()},
        "hourly": [{k: round(v) for k, v in h.items()} for h in hourly],
    }


@app.get("/api/timeline", dependencies=[Depends(admin)])
def timeline(user: str, day: str | None = None):
    start, end = _range(day, 1)
    rules = db.rules()
    out = []
    for e in _events(user, start, end):
        cat, lab = classify(e, rules)
        out.append({"ts": e["ts"], "dur": e["dur"], "app": e["app"], "title": e["title"],
                    "url": e["url"], "category": cat, "label": lab})
    return out


# ----------------------------------------------------------------- rules
class RuleIn(BaseModel):
    kind: str = Field(pattern="^(app|domain|title)$")
    pattern: str = Field(min_length=1, max_length=256)
    category: str = Field(min_length=1, max_length=64)
    label: str = Field(pattern="^(productive|unproductive|neutral)$")
    alert: bool = False
    priority: int = 0


@app.get("/api/rules", dependencies=[Depends(admin)])
def list_rules():
    return db.rules()


@app.post("/api/rules", dependencies=[Depends(admin)])
def add_rule(r: RuleIn):
    with db.conn() as c:
        cur = c.execute("INSERT INTO rules(kind,pattern,category,label,alert,priority) VALUES(?,?,?,?,?,?)",
                        (r.kind, r.pattern, r.category, r.label, int(r.alert), r.priority))
        return {"id": cur.lastrowid}


@app.delete("/api/rules/{rid}", dependencies=[Depends(admin)])
def del_rule(rid: int):
    with db.conn() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rid,))
    return {"ok": True}


# ---------------------------------------------------------------- alerts
@app.get("/api/alerts", dependencies=[Depends(admin)])
def list_alerts(unacked: bool = False, limit: int = Query(200, le=1000)):
    q = "SELECT * FROM alerts" + (" WHERE acked=0" if unacked else "") + " ORDER BY ts DESC LIMIT ?"
    with db.conn() as c:
        return [dict(r) for r in c.execute(q, (limit,))]


@app.post("/api/alerts/{aid}/ack", dependencies=[Depends(admin)])
def ack_alert(aid: int):
    with db.conn() as c:
        c.execute("UPDATE alerts SET acked=1 WHERE id=?", (aid,))
    return {"ok": True}


# ----------------------------------------------------------- trend (charts)
@app.get("/api/trend", dependencies=[Depends(admin)])
def trend(user: str | None = None, days: int = Query(14, ge=1, le=92)):
    rules = db.rules()
    end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=days)
    buckets = [defaultdict(float) for _ in range(days)]
    for e in _events(user, start.timestamp(), end.timestamp()):
        idx = int((e["ts"] - start.timestamp()) // 86400)
        if 0 <= idx < days:
            _, lab = classify(e, rules)
            buckets[idx][lab] += e["dur"]
    out = []
    for i in range(days):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"day": day, **{k: round(buckets[i].get(k, 0)) for k in (*LABELS, "idle")}})
    return out


# ------------------------------------------------------------- screenshots
_SAFE = re.compile(r"[^a-zA-Z0-9_.-]")


class ScreenshotIn(BaseModel):
    user: str = Field(min_length=1, max_length=128)
    device: str = Field(min_length=1, max_length=128)
    ts: float
    app: str = ""
    title: str = ""
    image_b64: str = Field(min_length=1, max_length=MAX_SHOT_B64)


@app.post("/api/screenshot", dependencies=[Depends(agent)])
def upload_screenshot(s: ScreenshotIn):
    try:
        data = base64.b64decode(s.image_b64, validate=True)
    except Exception:
        raise HTTPException(400, "bad image data")
    user_dir = _SAFE.sub("_", s.user)
    key = f"{user_dir}/{int(s.ts * 1000)}_{_SAFE.sub('_', s.device)}.jpg"
    try:
        store.put(key, data)
    except ObjectStoreError as e:
        # Previously silent: the agent (and anyone reading `docker compose
        # logs server`) only ever saw a bare 502 with no indication of what
        # SeaweedFS actually said -- auth mismatch, bucket missing, disk
        # full, unreachable, etc. all looked identical from the outside.
        print(f"screenshot upload failed, image store error: {e!r}")
        raise HTTPException(502, "image store unavailable")
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO screenshots(user,device,ts,app,title,path,bytes) VALUES(?,?,?,?,?,?,?)",
            (s.user, s.device, s.ts, s.app, s.title, key, len(data)))
    return {"id": cur.lastrowid}


@app.get("/api/screenshots", dependencies=[Depends(admin)])
def list_screenshots(user: str, start: float, end: float, limit: int = Query(500, le=2000)):
    with db.conn() as c:
        rows = c.execute(
            "SELECT id,user,device,ts,app,title,bytes FROM screenshots "
            "WHERE user=? AND ts>=? AND ts<? ORDER BY ts LIMIT ?",
            (user, start, end, limit)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/screenshots/{sid}/image", dependencies=[Depends(admin)])
def get_screenshot(sid: int):
    with db.conn() as c:
        row = c.execute("SELECT path FROM screenshots WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    try:
        data = store.get(row["path"])
    except ObjectStoreError as e:
        print(f"screenshot fetch failed, image store error: {e!r}")
        raise HTTPException(404)
    return Response(content=data, media_type="image/jpeg")


class ScreenshotDelete(BaseModel):
    ids: list[int] = Field(max_length=5000)


@app.post("/api/screenshots/delete", dependencies=[Depends(admin)])
def delete_screenshots(d: ScreenshotDelete):
    if not d.ids:
        return {"deleted": 0}
    marks = ",".join("?" * len(d.ids))
    with db.conn() as c:
        rows = c.execute(f"SELECT id, path FROM screenshots WHERE id IN ({marks})", d.ids).fetchall()
        for r in rows:
            store.delete(r["path"])
        c.execute(f"DELETE FROM screenshots WHERE id IN ({marks})", d.ids)
    return {"deleted": len(rows)}


# ------------------------------------------------------------- agent updates
# Visible, logged self-update: IT drops a new build + manifest in CYRIL_DIST_DIR
# (see windows/build.ps1 and README "Shipping an agent update"). Agents poll this
# once an hour; the update itself runs with no popup (same as any managed IT
# agent's background patching -- Chrome, CrowdStrike, etc. all do this), but
# every step is written to the agent's own log file and to this server's
# update_log table, so it is fully auditable, never hidden from inspection, and
# never alters what Task Manager / the Scheduled Task list shows.
@app.get("/api/agent/version")
def agent_version():
    manifest_path = DIST_DIR / "manifest.json"
    if not manifest_path.exists():
        return {"version": None}
    import json
    return json.loads(manifest_path.read_text())


class UpdateLogIn(BaseModel):
    user: str = Field(min_length=1, max_length=128)
    device: str = Field(min_length=1, max_length=128)
    from_version: str = Field(max_length=64)
    to_version: str = Field(max_length=64)
    ok: bool
    detail: str = Field(default="", max_length=2000)


@app.post("/api/agent/update-log", dependencies=[Depends(agent)])
def agent_update_log(u: UpdateLogIn):
    with db.conn() as c:
        c.execute(
            "INSERT INTO update_log(ts,user,device,from_version,to_version,ok,detail) VALUES(?,?,?,?,?,?,?)",
            (time.time(), u.user, u.device, u.from_version, u.to_version, int(u.ok), u.detail))
    return {"ok": True}


@app.get("/api/agent/update-log", dependencies=[Depends(admin)])
def list_update_log(limit: int = Query(200, le=1000)):
    with db.conn() as c:
        rows = c.execute("SELECT * FROM update_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/install.ps1")
def install_script():
    return FileResponse(WINSCRIPTS / "install.ps1", media_type="text/plain")


@app.get("/uninstall.ps1")
def uninstall_script():
    return FileResponse(WINSCRIPTS / "uninstall.ps1", media_type="text/plain")


@app.get("/api/health")
def health():
    return {"ok": True, "time": time.time()}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
