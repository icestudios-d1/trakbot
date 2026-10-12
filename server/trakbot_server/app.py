"""TrakBot server: ingest API, reports, rule + alert management, dashboard."""
import hmac
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .classify import LABELS, classify, matching_rule
from .db import DB

ADMIN_TOKEN = os.environ.get("TRAKBOT_ADMIN_TOKEN", "")
AGENT_KEY = os.environ.get("TRAKBOT_AGENT_KEY", "")
DB_PATH = os.environ.get("TRAKBOT_DB", "data/trakbot.db")
MAX_EVENT_SECONDS = 3600

app = FastAPI(title="TrakBot")
db = DB(DB_PATH)
STATIC = Path(__file__).parent / "static"


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


@app.get("/api/health")
def health():
    return {"ok": True, "time": time.time()}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
