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
import io
import math
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .classify import LABELS, make_classifier, matching_rule
from .db import DB
from .default_rules import STUDIO_RULES
from .reports import SLOT_CHOICES, aggregate, clip_hours, in_work_hours, previous_span, slotize, span, totals
from .names import display_name, site_name
from .objstore import ObjectStoreError, S3Store
from .payloadcrypto import BadEnvelope, Opener

ADMIN_TOKEN = os.environ.get("CYRIL_ADMIN_TOKEN", "")
AGENT_KEY = os.environ.get("CYRIL_AGENT_KEY", "")
DB_PATH = os.environ.get("CYRIL_DB", "data/cyril.db")
WORK_START = int(os.environ.get("CYRIL_WORK_START", "8"))    # the dashboard's "Work" view: local hours [start, end)
WORK_END = int(os.environ.get("CYRIL_WORK_END", "22"))
SESSION_COOKIE = "cyril_session"
SUPER_COOKIE = "cyril_super"
# Web origins whose pages may read the agent's "OMS Ping" answer on 127.0.0.1:7077. Defaults to the company OMS;
# agents also have it built in. Set CYRIL_OMS_ORIGINS (comma separated) to list others.
OMS_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("CYRIL_OMS_ORIGINS", "https://office.icestudios.in").split(",")
               if o.strip()]
# Agents seal what they collect to this server's public key (agent/cyril_agent/seal.py): the queue on the PC and the
# bytes on the wire are ciphertext that only this process can open. Set CYRIL_REQUIRE_SEALED=1 once every PC has
# updated, to refuse unsealed (old-style) uploads.
REQUIRE_SEALED = os.environ.get("CYRIL_REQUIRE_SEALED", "").lower() in ("1", "true", "yes")
MAX_EVENT_SECONDS = 3600
MAX_SHOT_B64 = 3_000_000  # ~2.2MB decoded; agent targets far smaller JPEGs

app = FastAPI(title="Cyril")
db = DB(DB_PATH)
opener = Opener(os.path.join(os.path.dirname(DB_PATH) or ".", "payload.key"))
STATIC = Path(__file__).parent / "static"
class _Static(StaticFiles):
    """StaticFiles with an explicit Cache-Control. Without one, browsers and CDNs (Cloudflare caches .exe by
    default) keep serving an old copy after the files change."""

    def __init__(self, *a, cache_control: str = "no-cache", **kw):
        super().__init__(*a, **kw)
        self._cache_control = cache_control

    def file_response(self, *a, **kw):
        r = super().file_response(*a, **kw)
        r.headers["Cache-Control"] = self._cache_control
        return r


DIST_DIR = Path(os.environ.get("CYRIL_DIST_DIR", "dist"))
if DIST_DIR.exists():
    # no-store: the agent build must never be served from a stale proxy/CDN cache.
    app.mount("/dist", _Static(directory=str(DIST_DIR), cache_control="no-store"), name="dist")
WINSCRIPTS = Path(__file__).parent / "winscripts"
MACSCRIPTS = Path(__file__).parent / "macscripts"

# SeaweedFS's S3 gateway (bundled as the `seaweedfs` service in docker-compose.yml).
store = S3Store(
    endpoint=os.environ.get("CYRIL_S3_ENDPOINT", "seaweedfs:8333"),
    access_key=os.environ.get("CYRIL_S3_ACCESS_KEY", ""),
    secret_key=os.environ.get("CYRIL_S3_SECRET_KEY", ""),
    bucket=os.environ.get("CYRIL_S3_BUCKET", "cyril-screenshots"),
    secure=os.environ.get("CYRIL_S3_SECURE", "false").lower() == "true",
)
_ak = getattr(store, "access_key", None)
if _ak is not None and (not _ak or re.search(r"[\s/,]", _ak)):
    print("WARNING: CYRIL_S3_ACCESS_KEY is empty or contains '/', space or comma: screenshot storage will fail. "
          "Use letters and digits only (see Install agents in the dashboard).")
store.ensure_bucket()


def _check(given: str | None, expected: str):
    if not expected or not given or not hmac.compare_digest(given, expected):
        raise HTTPException(401, "unauthorized")


def _session_value() -> str:
    """What the sign-in cookie holds: a keyed hash of the admin token, never the token itself."""
    return hmac.new(ADMIN_TOKEN.encode(), b"cyril-session-v1", hashlib.sha256).hexdigest() if ADMIN_TOKEN else ""


def _signed_in(request: Request) -> bool:
    given, want = request.cookies.get(SESSION_COOKIE), _session_value()
    return bool(given and want and hmac.compare_digest(given, want))


def _super_value() -> str:
    return hmac.new(ADMIN_TOKEN.encode(), b"cyril-superadmin-v1", hashlib.sha256).hexdigest() if ADMIN_TOKEN else ""


def _is_super(request: Request) -> bool:
    """SuperAdmin mode: signed in AND has visited /superadmin on this browser (same admin token, no second secret)."""
    given, want = request.cookies.get(SUPER_COOKIE), _super_value()
    return bool(_signed_in(request) and given and want and hmac.compare_digest(given, want))


def admin(request: Request, authorization: str | None = Header(None)):
    """The admin token as a Bearer header (every API call the dashboard makes). The sign-in cookie is accepted for
    plain GETs only, which is what lets <img> tags load screenshots (browsers cache them, and thumbnails can be lazy
    loaded) without ever allowing a cookie to change anything (no CSRF)."""
    tok = (authorization or "").removeprefix("Bearer ")
    if tok:
        return _check(tok, ADMIN_TOKEN)
    if request.method in ("GET", "HEAD") and _signed_in(request):
        return
    raise HTTPException(401, "unauthorized")


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
    events: list[dict] = Field(max_length=2000)    # each is an event, or {"enc": ...} sealed by the agent


def _unseal(raw: dict) -> dict:
    """The plain dict for an upload item: opened if sealed (400 if it will not open: the agent then discards just
    that item), passed through if a pre-sealing agent sent it plain (403 when REQUIRE_SEALED forbids that)."""
    if "enc" in raw:
        try:
            return opener.open(raw)
        except BadEnvelope as e:
            raise HTTPException(400, str(e))
    if REQUIRE_SEALED:
        raise HTTPException(403, "this server only accepts sealed uploads: update the agent")
    return raw


def _validated(model, data: dict):
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False, include_input=False))


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(v: str, limit: int) -> str:
    """Text SQLite and the dashboard will certainly accept: no NUL or other control characters, no lone surrogates
    (a window title cut in the middle of an emoji), bounded length."""
    return _CONTROL.sub("", (v or "").encode("utf-8", "replace").decode("utf-8"))[:limit]


@app.post("/api/ingest", dependencies=[Depends(agent)])
def ingest(batch: dict = Body(...)):
    """One odd event must never cost a PC its whole day: an event that cannot be stored is skipped and counted, and
    the rest of the batch is kept (the agent only sees 2xx and moves on)."""
    b = _validated(IngestBatch, batch)
    events = [_validated(Event, _unseal(raw)) for raw in b.events]
    rules = db.rules()
    alerts = stored = skipped = 0
    with db.conn() as c:
        for e in events:
            if not math.isfinite(e.ts) or e.ts <= 0 or not math.isfinite(e.dur):
                skipped += 1
                continue
            app_, title, url = _clean(e.app, 256), _clean(e.title, 1000), _clean(e.url, 2048)
            try:
                dom = domain_of(url)
                c.execute(
                    "INSERT INTO events(user,device,ts,dur,app,title,url,domain,idle) VALUES(?,?,?,?,?,?,?,?,?)",
                    (b.user, b.device, e.ts, e.dur, app_, title, url, dom, int(e.idle)))
                stored += 1
            except Exception as err:
                skipped += 1
                print(f"ingest: skipped one event from {b.user}/{b.device}: {err!r}")
                continue
            try:
                r = matching_rule({"app": app_, "title": title, "domain": dom, "idle": e.idle}, rules)
                if r and r["alert"]:
                    c.execute(
                        "INSERT INTO alerts(ts,user,device,rule_id,category,app,title,url) VALUES(?,?,?,?,?,?,?,?)",
                        (e.ts, b.user, b.device, r["id"], r["category"], app_, title, url))
                    alerts += 1
            except Exception as err:   # a broken rule must not lose the activity itself
                print(f"ingest: rule check failed for one event: {err!r}")
    return {"stored": stored, "alerts": alerts, **({"skipped": skipped} if skipped else {})}


# --------------------------------------------------------------- reports
def _range(day: str | None, days: int):
    d = datetime.strptime(day, "%Y-%m-%d") if day else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    d = d.astimezone() if d.tzinfo else d
    start = d.timestamp()
    return start, (d + timedelta(days=days)).timestamp()


# A report is cached for a short while: the Overview and the Employees page ask for the very same numbers, and people
# flip between pages. Editing rules, groups or settings clears it at once, so those changes are never shown stale.
CACHE_TTL = float(os.environ.get("CYRIL_CACHE_TTL", "30"))
_CACHE: dict = {}


def _cached(key: tuple, fn):
    if CACHE_TTL <= 0:
        return fn()
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    val = fn()
    if len(_CACHE) >= 48:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    _CACHE[key] = (now, val)
    return val


def _bust() -> None:
    _CACHE.clear()


_LIGHT = "user,device,ts,dur,app,title,domain,idle"     # what a headline report needs (no URL: it is long and unused)
_FULL = "id,user,device,ts,dur,app,title,url,domain,idle"


def _group_filter(group: str | None):
    """Which employees a report covers, from the dashboard's group picker:
    ''/None -> everyone except groups marked "exclude from reports" (the normal view);  'all' -> truly everyone;
    'none' -> employees in no group;  '<id>' -> that group (even an excluded one, so it can still be looked at).
    Returns None (no filter), ("in", users) or ("out", users)."""
    if group == "all":
        return None
    with db.conn() as c:
        if group == "none":
            return ("out", [r[0] for r in c.execute("SELECT user FROM user_groups")])
        if group and group.isdigit():
            return ("in", [r[0] for r in c.execute("SELECT user FROM user_groups WHERE group_id=?", (int(group),))])
        out = [r[0] for r in c.execute("SELECT user FROM user_groups WHERE group_id IN (SELECT id FROM groups WHERE excluded=1)")]
    return ("out", out) if out else None


def _is_work(scope: str | None) -> bool:
    return scope == "work"


def _events(user: str | None, start: float, end: float, *, group: str | None = None, scope: str | None = None,
            tz: int | None = None, cols: str = _LIGHT):
    """Events in [start, end) for one person, or for the team narrowed by group. scope='work' trims everything to the
    working window (WORK_START-WORK_END, the viewer's local time)."""
    q, args = f"SELECT {cols} FROM events WHERE ts>=? AND ts<?", [start, end]
    post = None
    if user:
        q += " AND user=?"
        args.append(user)
    else:
        f = _group_filter(group)
        if f is not None:
            mode, users = f
            if mode == "in" and not users:
                return []
            if users and len(users) <= 800:
                q += f" AND user {'NOT ' if mode == 'out' else ''}IN ({','.join('?' * len(users))})"
                args += users
            elif users:
                keep = set(users)
                post = (lambda e: e["user"] in keep) if mode == "in" else (lambda e: e["user"] not in keep)
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(q + " ORDER BY ts", args)]
    if post:
        rows = [e for e in rows if post(e)]
    if _is_work(scope):
        rows = clip_hours(rows, tz, WORK_START, WORK_END)
    return rows


def _top(d: dict, n=10):
    return [{"name": k, "seconds": round(v)} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]


@app.get("/api/users", dependencies=[Depends(admin)])
def users():
    """Accounts that have reported, with the display name an admin gave them (`name`, None if not renamed)."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT e.user, n.name, MAX(e.ts+e.dur) last_seen, COUNT(DISTINCT e.device) devices, g.id group_id, g.name \"group\", "
            "COALESCE(g.excluded,0) group_excluded FROM events e "
            "LEFT JOIN user_names n ON n.user=e.user LEFT JOIN user_groups ug ON ug.user=e.user LEFT JOIN groups g ON g.id=ug.group_id "
            "GROUP BY e.user ORDER BY COALESCE(n.name, e.user) COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def _user_footprint(c, user: str) -> dict:
    n = lambda q: c.execute(q, (user,)).fetchone()[0]  # noqa: E731
    devs = [r[0] for r in c.execute("SELECT device FROM events WHERE user=? UNION SELECT device FROM screenshots WHERE user=?", (user, user))]
    return {"events": n("SELECT COUNT(*) FROM events WHERE user=?"), "screenshots": n("SELECT COUNT(*) FROM screenshots WHERE user=?"),
            "alerts": n("SELECT COUNT(*) FROM alerts WHERE user=?"), "devices": devs,
            "last_seen": c.execute("SELECT MAX(ts+dur) FROM events WHERE user=?", (user,)).fetchone()[0]}


@app.get("/api/users/delete-preview", dependencies=[Depends(admin)])
def delete_user_preview(user: str):
    """Exactly what deleting this employee would remove, plus whether their PC is still reporting."""
    with db.conn() as c:
        f = _user_footprint(c, user)
    if not (f["events"] or f["screenshots"] or f["alerts"]):
        raise HTTPException(404, "no data for that employee")
    return {**f, "user": user, "still_reporting": bool(f["last_seen"] and time.time() - f["last_seen"] < 600)}


class UserDeleteIn(BaseModel):
    user: str = Field(min_length=1, max_length=128)


@app.post("/api/users/delete", dependencies=[Depends(admin)])
def delete_user(b: UserDeleteIn):
    """Permanently remove an employee and everything recorded about them: activity, screenshots (the image files
    too), alerts, their display name, and the status/commands of devices nobody else uses. Typical use: a rented
    PC is returned, the agent is uninstalled, and the data is wiped. If a screenshot file can't be removed its row is
    kept (so nothing is orphaned) and the count is returned so the delete can simply be run again."""
    _bust()
    with db.conn() as c:
        f = _user_footprint(c, b.user)
        shots = [dict(r) for r in c.execute("SELECT id, path FROM screenshots WHERE user=?", (b.user,))]
    if not (f["events"] or f["screenshots"] or f["alerts"]):
        raise HTTPException(404, "no data for that employee")
    removed, failed = [], 0
    for r in shots:                       # image files first, outside the DB lock (thousands of HTTP calls)
        try:
            store.delete(r["path"])
            _drop_thumb(r["path"])
            removed.append(r["id"])
        except ObjectStoreError as e:
            failed += 1
            print(f"delete employee: could not remove screenshot {r['path']}: {e!r}")
    with db.conn() as c:
        for i in range(0, len(removed), 500):
            chunk = removed[i:i + 500]
            c.execute(f"DELETE FROM screenshots WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        c.execute("DELETE FROM events WHERE user=?", (b.user,))
        c.execute("DELETE FROM alerts WHERE user=?", (b.user,))
        if not failed:
            c.execute("DELETE FROM user_names WHERE user=?", (b.user,))
            c.execute("DELETE FROM user_groups WHERE user=?", (b.user,))
        for d in f["devices"]:            # a device other logins still use keeps its status
            if not c.execute("SELECT 1 FROM events WHERE device=? LIMIT 1", (d,)).fetchone():
                c.execute("DELETE FROM agent_status WHERE device=?", (d,))
                c.execute("DELETE FROM agent_commands WHERE device=?", (d,))
                c.execute("DELETE FROM device_removals WHERE device=?", (d,))
    return {"deleted": {"events": f["events"], "screenshots": len(removed), "alerts": f["alerts"]},
            "failed_screenshots": failed}


class RenameIn(BaseModel):
    user: str = Field(min_length=1, max_length=128)
    name: str = Field(max_length=100)


@app.post("/api/users/rename", dependencies=[Depends(admin)])
def rename_user(r: RenameIn):
    """Tag a device login with the real person using it. An empty name clears the tag."""
    name = " ".join(r.name.split())
    with db.conn() as c:
        if name:
            c.execute("INSERT INTO user_names(user,name) VALUES(?,?) ON CONFLICT(user) DO UPDATE SET name=excluded.name",
                      (r.user, name))
        else:
            c.execute("DELETE FROM user_names WHERE user=?", (r.user,))
    return {"user": r.user, "name": name or None}


@app.get("/api/summary", dependencies=[Depends(admin)])
def summary(user: str | None = None, day: str | None = None, days: int = Query(1, ge=1, le=92),
            group: str | None = None, scope: str | None = None, tz: int | None = Query(None, ge=-840, le=840)):
    start, end = _range(day, days)
    clf = make_classifier(db.rules())
    labels, cats = defaultdict(float), defaultdict(float)
    apps, sites = defaultdict(float), defaultdict(float)
    per_user = defaultdict(lambda: defaultdict(float))
    hourly = [defaultdict(float) for _ in range(24)]
    for e in _events(user, start, end, group=group, scope=scope, tz=tz):
        cat, lab = clf(e)
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


def _rows(events: list[dict], rules: list[dict]) -> list[dict]:
    out = []
    clf = make_classifier(rules)
    for e in events:
        cat, lab = clf(e)
        row = {"ts": e["ts"], "dur": e["dur"], "app": e["app"], "title": e["title"], "url": e["url"],
               "domain": e["domain"], "idle": bool(e["idle"]), "category": cat, "label": lab,
               "name": "Idle / away" if e["idle"] else display_name(e["app"], e["domain"])}
        if e.get("slot"):
            row.update(slot=True, share=e["share"], breakdown=e["breakdown"])
        out.append(row)
    return out


@app.get("/api/timeline", dependencies=[Depends(admin)])
def timeline(user: str, day: str | None = None, tz: int | None = Query(None, ge=-840, le=840), slot: int | None = None,
             scope: str | None = None):
    """One person's day. With a reporting interval set, each block shows the app used most in that block."""
    start, end, _ = _span_or_400(day, day, tz)
    return _rows(slotize(_events(user, start, end, scope=scope, tz=tz, cols=_FULL), _slot(slot), tz), db.rules())


@app.get("/api/web", dependencies=[Depends(admin)])
def web_report(user: str | None = None, start: str | None = None, end: str | None = None,
               tz: int | None = Query(None, ge=-840, le=840), limit: int = Query(20000, le=50000),
               group: str | None = None, scope: str | None = None):
    """Detailed web report: every page visited with its FULL URL, plus per-site totals. Always exact (never
    grouped into blocks). Private/incognito windows are not in here: their address is not recorded."""
    s, e, days = _span_or_400(start, end, tz)
    if len(days) > 31:
        raise HTTPException(400, "the web report covers at most 31 days")
    clf = make_classifier(db.rules())
    pages, sites = [], {}
    last = {}  # (user, url) -> page row, to merge back-to-back stretches on the same page
    for ev in _events(user, s, e, group=group, scope=scope, tz=tz, cols=_FULL):
        if ev["idle"] or not ev["url"]:
            continue
        cat, lab = clf(ev)
        prev = last.get((ev["user"], ev["url"]))
        if prev and ev["ts"] - (prev["ts"] + prev["dur"]) <= 60:
            prev["dur"] = ev["ts"] + ev["dur"] - prev["ts"]
        else:
            prev = {"ts": ev["ts"], "dur": ev["dur"], "user": ev["user"], "name": site_name(ev["domain"]) or ev["domain"],
                    "domain": ev["domain"], "url": ev["url"], "title": ev["title"], "category": cat, "label": lab}
            pages.append(prev)
            last[(ev["user"], ev["url"])] = prev
    for p in pages:
        st = sites.setdefault(p["name"], {"name": p["name"], "domain": p["domain"], "seconds": 0, "visits": 0,
                                          "category": p["category"], "label": p["label"]})
        st["seconds"] += p["dur"]
        st["visits"] += 1
    pages.sort(key=lambda r: -r["ts"])
    return {"sites": sorted(({**v, "seconds": round(v["seconds"])} for v in sites.values()), key=lambda v: -v["seconds"]),
            "pages": pages[:limit], "total_pages": len(pages)}


@app.get("/api/slots", dependencies=[Depends(admin)])
def slot_report(user: str, start: str | None = None, end: str | None = None,
                tz: int | None = Query(None, ge=-840, le=840), slot: int | None = None, scope: str | None = None):
    """The time-slot report for a date range: one row per block (or per raw event when the interval is Exact)."""
    s, e, days = _span_or_400(start, end, tz)
    if len(days) > 31:
        raise HTTPException(400, "the time-slot report covers at most 31 days")
    slot = _slot(slot)
    return {"slot": slot, "rows": _rows(slotize(_events(user, s, e, scope=scope, tz=tz, cols=_FULL), slot, tz), db.rules())}


@app.get("/api/me", dependencies=[Depends(admin)])
def me(request: Request):
    return {"ok": True, "superadmin": _is_super(request)}


def _https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip() == "https"


class SessionIn(BaseModel):
    remember: bool = False


@app.post("/api/session", dependencies=[Depends(admin)])
def start_session(b: SessionIn, request: Request, response: Response):
    """Called right after the admin token is accepted: sets an HttpOnly cookie so the SERVER can tell a signed-in
    browser before it sends any page (the dashboard HTML is not served to anyone else), and so <img> tags can load
    screenshots. The cookie holds a keyed hash, never the token, and is only honoured for GET requests."""
    response.set_cookie(SESSION_COOKIE, _session_value(), httponly=True, samesite="lax", secure=_https(request),
                        max_age=30 * 86400 if b.remember else None, path="/")
    return {"ok": True}


@app.post("/api/logout")
def end_session(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(SUPER_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/superadmin/exit", dependencies=[Depends(admin)])
def exit_superadmin(response: Response):
    response.delete_cookie(SUPER_COOKIE, path="/")
    return {"ok": True}


def _span_or_400(start, end, tz):
    try:
        return span(start, end, tz)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _slot_setting() -> int:
    with db.conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key='report_interval'").fetchone()
    try:
        v = int(row["value"]) if row else 0
    except ValueError:
        v = 0
    return v if v in SLOT_CHOICES else 0


def _slot(param: int | None) -> int:
    """Reporting granularity in minutes: an explicit ?slot= wins, else the saved setting (0 = exact)."""
    if param is None:
        return _slot_setting()
    if param not in SLOT_CHOICES:
        raise HTTPException(400, f"slot must be one of {list(SLOT_CHOICES)}")
    return param


SHOT_EVERY = (15, 30, 60, 120, 300)            # seconds between shots while the app in front keeps changing
SHOT_STABLE_EVERY = (60, 120, 180, 300, 600, 900)  # seconds between shots while the same app stays in front


def _setting_int(key: str, default: int, allowed: tuple) -> int:
    with db.conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        v = int(row["value"]) if row else default
    except ValueError:
        v = default
    return v if v in allowed else default


def _screenshot_settings() -> dict:
    return {"screenshot_every": _setting_int("screenshot_every", 60, SHOT_EVERY),
            "screenshot_stable_every": _setting_int("screenshot_stable_every", 180, SHOT_STABLE_EVERY)}


class SettingsIn(BaseModel):
    report_interval: int | None = None
    screenshot_every: int | None = None
    screenshot_stable_every: int | None = None


@app.get("/api/settings", dependencies=[Depends(admin)])
def get_settings():
    return {"report_interval": _slot_setting(), "report_interval_choices": list(SLOT_CHOICES),
            **_screenshot_settings(), "screenshot_every_choices": list(SHOT_EVERY),
            "screenshot_stable_every_choices": list(SHOT_STABLE_EVERY)}


@app.put("/api/settings", dependencies=[Depends(admin)])
def put_settings(s: SettingsIn):
    """Change any subset of the settings. Screenshot intervals reach every agent within about a minute."""
    _bust()
    changes = {}
    for key, value, allowed in (("report_interval", s.report_interval, SLOT_CHOICES),
                                ("screenshot_every", s.screenshot_every, SHOT_EVERY),
                                ("screenshot_stable_every", s.screenshot_stable_every, SHOT_STABLE_EVERY)):
        if value is None:
            continue
        if value not in allowed:
            raise HTTPException(400, f"{key} must be one of {list(allowed)}")
        changes[key] = value
    with db.conn() as c:
        for key, value in changes.items():
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, str(value)))
    return get_settings()


def _delta(cur: dict, prev: dict):
    return {"productivity_pct": prev["productivity_pct"], "active_seconds": prev["active_seconds"]}


def _alert_counts(s: float, e: float, group: str | None, scope: str | None, tz: int | None) -> dict:
    with db.conn() as c:
        rows = c.execute("SELECT ts, user, acked FROM alerts WHERE ts>=? AND ts<?", (s, e)).fetchall()
    f = _group_filter(group)
    if f is not None:
        users = set(f[1])
        rows = [r for r in rows if (r["user"] in users) == (f[0] == "in")]
    if _is_work(scope):
        rows = [r for r in rows if in_work_hours(r["ts"], tz, WORK_START, WORK_END)]
    return {"total": len(rows), "open": sum(1 for r in rows if not r["acked"])}


@app.get("/api/overview", dependencies=[Depends(admin)])
def overview(start: str | None = None, end: str | None = None, tz: int | None = Query(None, ge=-840, le=840),
             slot: int | None = None, group: str | None = None, scope: str | None = None):
    """Team-wide dashboard numbers for an inclusive local-date range, plus the previous equal-length range."""
    s, e, days = _span_or_400(start, end, tz)
    slot = _slot(slot)
    return _cached(("overview", s, e, tz, slot, group or "", scope or ""), lambda: _overview(s, e, days, tz, slot, group, scope))


def _overview(s, e, days, tz, slot, group, scope):
    clf = make_classifier(db.rules())
    cur = aggregate(slotize(_events(None, s, e, group=group, scope=scope, tz=tz), slot, tz), clf, tz, days, s)
    ps, pe = previous_span(days)
    ps_ts, pe_ts, pdays = span(ps, pe, tz)
    prev = totals(slotize(_events(None, ps_ts, pe_ts, group=group, scope=scope, tz=tz), slot, tz), clf)
    return {**cur, "start": s, "end": e, "days": days, "previous": _delta(cur, prev), "slot": slot,
            "alerts": _alert_counts(s, e, group, scope, tz), "scope": scope or "all"}


@app.get("/api/employee", dependencies=[Depends(admin)])
def employee(user: str, start: str | None = None, end: str | None = None,
             tz: int | None = Query(None, ge=-840, le=840), slot: int | None = None, scope: str | None = None):
    """Everything for one person over a date range (the per-employee report page)."""
    s, e, days = _span_or_400(start, end, tz)
    slot = _slot(slot)
    return _cached(("employee", user, s, e, tz, slot, scope or ""), lambda: _employee(user, s, e, days, tz, slot, scope))


def _employee(user, s, e, days, tz, slot, scope):
    clf = make_classifier(db.rules())
    events = slotize(_events(user, s, e, scope=scope, tz=tz), slot, tz)
    cur = aggregate(events, clf, tz, days, s, top_n=15)
    ps, pe = previous_span(days)
    ps_ts, pe_ts, pdays = span(ps, pe, tz)
    prev = totals(slotize(_events(user, ps_ts, pe_ts, scope=scope, tz=tz), slot, tz), clf)
    with db.conn() as c:
        devices = [r[0] for r in c.execute("SELECT DISTINCT device FROM events WHERE user=? AND ts>=? AND ts<?", (user, s, e))]
        shot_ts = [r[0] for r in c.execute("SELECT ts FROM screenshots WHERE user=? AND ts>=? AND ts<?", (user, s, e))]
    shots = sum(1 for t in shot_ts if not _is_work(scope) or in_work_hours(t, tz, WORK_START, WORK_END))
    worked = [d for d in cur["daily"] if d["first"] is not None]
    return {**cur, "user": user, "start": s, "end": e, "days": days, "previous": _delta(cur, prev), "slot": slot,
            "scope": scope or "all", "devices": devices, "screenshots": shots, "days_active": len(worked),
            "avg_active_per_day": round(cur["active_seconds"] / len(worked)) if worked else 0}


# ---------------------------------------------------------------- groups
class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    excluded: bool = False


def _group_name(name: str) -> str:
    return " ".join(name.split())


@app.get("/api/groups", dependencies=[Depends(admin)])
def list_groups():
    """Groups with how many employees each holds, plus the count of employees in no group."""
    with db.conn() as c:
        rows = c.execute("SELECT g.id, g.name, g.excluded, COUNT(ug.user) members FROM groups g "
                         "LEFT JOIN user_groups ug ON ug.group_id=g.id GROUP BY g.id ORDER BY g.name COLLATE NOCASE").fetchall()
        total = c.execute("SELECT COUNT(DISTINCT user) FROM events").fetchone()[0]
        grouped = c.execute("SELECT COUNT(*) FROM user_groups").fetchone()[0]
    return {"groups": [{**dict(r), "excluded": bool(r["excluded"])} for r in rows], "ungrouped": max(0, total - grouped)}


@app.post("/api/groups", dependencies=[Depends(admin)])
def add_group(g: GroupIn):
    _bust()
    name = _group_name(g.name)
    if not name:
        raise HTTPException(400, "give the group a name")
    with db.conn() as c:
        if c.execute("SELECT 1 FROM groups WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, f'there is already a group called "{name}"')
        cur = c.execute("INSERT INTO groups(name, excluded) VALUES(?,?)", (name, int(g.excluded)))
    return {"id": cur.lastrowid, "name": name, "excluded": g.excluded}


@app.put("/api/groups/{gid}", dependencies=[Depends(admin)])
def edit_group(gid: int, g: GroupIn):
    _bust()
    name = _group_name(g.name)
    with db.conn() as c:
        if c.execute("SELECT 1 FROM groups WHERE name=? AND id<>?", (name, gid)).fetchone():
            raise HTTPException(409, f'there is already a group called "{name}"')
        cur = c.execute("UPDATE groups SET name=?, excluded=? WHERE id=?", (name, int(g.excluded), gid))
    if not cur.rowcount:
        raise HTTPException(404)
    return {"ok": True}


@app.delete("/api/groups/{gid}", dependencies=[Depends(admin)])
def del_group(gid: int):
    """Deleting a group never deletes people: its members simply become ungrouped."""
    _bust()
    with db.conn() as c:
        c.execute("DELETE FROM user_groups WHERE group_id=?", (gid,))
        c.execute("DELETE FROM groups WHERE id=?", (gid,))
    return {"ok": True}


class AssignIn(BaseModel):
    users: list[str] = Field(min_length=1, max_length=2000)
    group_id: int | None = None      # None = take them out of any group


@app.post("/api/users/group", dependencies=[Depends(admin)])
def assign_group(a: AssignIn):
    _bust()
    with db.conn() as c:
        if a.group_id is not None and not c.execute("SELECT 1 FROM groups WHERE id=?", (a.group_id,)).fetchone():
            raise HTTPException(404, "no such group")
        for u in a.users:
            if a.group_id is None:
                c.execute("DELETE FROM user_groups WHERE user=?", (u,))
            else:
                c.execute("INSERT INTO user_groups(user, group_id) VALUES(?,?) ON CONFLICT(user) DO UPDATE SET group_id=excluded.group_id",
                          (u, a.group_id))
    return {"updated": len(a.users)}


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
    _bust()
    with db.conn() as c:
        cur = c.execute("INSERT INTO rules(kind,pattern,category,label,alert,priority) VALUES(?,?,?,?,?,?)",
                        (r.kind, r.pattern, r.category, r.label, int(r.alert), r.priority))
        return {"id": cur.lastrowid}


@app.put("/api/rules/{rid}", dependencies=[Depends(admin)])
def edit_rule(rid: int, r: RuleIn):
    _bust()
    with db.conn() as c:
        cur = c.execute("UPDATE rules SET kind=?,pattern=?,category=?,label=?,alert=?,priority=? WHERE id=?",
                        (r.kind, r.pattern, r.category, r.label, int(r.alert), r.priority, rid))
    if not cur.rowcount:
        raise HTTPException(404)
    return {"ok": True}


class RuleTest(BaseModel):
    app: str = ""
    domain: str = ""
    title: str = ""


@app.post("/api/rules/test", dependencies=[Depends(admin)])
def test_rule(t: RuleTest):
    """Which rule would classify this app / site / title?"""
    r = matching_rule({"app": t.app, "domain": domain_of(t.domain) or t.domain, "title": t.title}, db.rules())
    return {"rule": r}


@app.post("/api/rules/seed-studio", dependencies=[Depends(admin)])
def seed_studio_rules():
    """Add the design-studio starter rules that aren't already present (never overwrites existing rules)."""
    _bust()
    with db.conn() as c:
        have = {(r["kind"], r["pattern"].lower()) for r in c.execute("SELECT kind, pattern FROM rules")}
        new = [r for r in STUDIO_RULES if (r[0], r[1].lower()) not in have]
        # Studio rules go ahead of the generic ones that overlap (e.g. a media/youtube rule) via higher priority.
        c.executemany("INSERT INTO rules(kind,pattern,category,label,alert,priority) VALUES(?,?,?,?,?,10)", new)
    return {"added": len(new)}


@app.get("/api/uncategorized", dependencies=[Depends(admin)])
def uncategorized(days: int = Query(30, ge=1, le=92), limit: int = Query(30, le=200)):
    """Most-used apps and sites no rule matches yet: the quickest way to grow the rule set."""
    start, end, _ = span(None, None, None)
    start -= (days - 1) * 86400
    rules = db.rules()
    clf = make_classifier(rules)
    apps, sites = defaultdict(float), defaultdict(float)
    for e in _events(None, start, end, group="all"):
        if e["idle"]:
            continue
        if clf.rule_for(e) is None:
            if e["domain"]:
                sites[e["domain"]] += e["dur"]
            elif e["app"]:
                apps[e["app"]] += e["dur"]
    return {"apps": _top(apps, limit), "sites": _top(sites, limit)}


@app.delete("/api/rules/{rid}", dependencies=[Depends(admin)])
def del_rule(rid: int):
    _bust()
    with db.conn() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rid,))
    return {"ok": True}


# ---------------------------------------------------------------- alerts
@app.get("/api/alerts", dependencies=[Depends(admin)])
def list_alerts(unacked: bool = False, user: str | None = None, start: float | None = None,
                end: float | None = None, limit: int = Query(200, le=1000)):
    where, args = [], []
    if unacked:
        where.append("acked=0")
    if user:
        where.append("user=?")
        args.append(user)
    if start is not None:
        where.append("ts>=?")
        args.append(start)
    if end is not None:
        where.append("ts<?")
        args.append(end)
    q = "SELECT * FROM alerts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC LIMIT ?"
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(q, (*args, limit))]
    return [{**r, "name": display_name(r["app"] or "", domain_of(r["url"] or ""))} for r in rows]


@app.post("/api/alerts/{aid}/ack", dependencies=[Depends(admin)])
def ack_alert(aid: int):
    _bust()
    with db.conn() as c:
        c.execute("UPDATE alerts SET acked=1 WHERE id=?", (aid,))
    return {"ok": True}


# ----------------------------------------------------------- trend (charts)
@app.get("/api/trend", dependencies=[Depends(admin)])
def trend(user: str | None = None, days: int = Query(14, ge=1, le=92), group: str | None = None,
          scope: str | None = None, tz: int | None = Query(None, ge=-840, le=840)):
    clf = make_classifier(db.rules())
    end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=days)
    buckets = [defaultdict(float) for _ in range(days)]
    for e in _events(user, start.timestamp(), end.timestamp(), group=group, scope=scope, tz=tz):
        idx = int((e["ts"] - start.timestamp()) // 86400)
        if 0 <= idx < days:
            _, lab = clf(e)
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
    command_id: int | None = None   # set when this shot answers a "screenshot now" request


@app.post("/api/screenshot", dependencies=[Depends(agent)])
def upload_screenshot(body: dict = Body(...)):
    # who/when/which request stay readable; what was on screen (image, window title, app) is inside the seal
    fields = {**{k: v for k, v in body.items() if k != "enc"}, **_unseal(body)} if "enc" in body else _unseal(body)
    s = _validated(ScreenshotIn, fields)
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
        # The reason travels back to the agent (and from there to Install agents), so "502" is never a mystery.
        raise HTTPException(502, f"image store error: {str(e)[:240]}")
    _store_thumb(key, data)
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO screenshots(user,device,ts,app,title,path,bytes) VALUES(?,?,?,?,?,?,?)",
            (s.user, s.device, s.ts, s.app, s.title, key, len(data)))
        if s.command_id is not None:
            c.execute("UPDATE agent_commands SET status='done', screenshot_id=? WHERE id=? AND device=?",
                      (cur.lastrowid, s.command_id, s.device))
    return {"id": cur.lastrowid}


@app.get("/api/screenshots", dependencies=[Depends(admin)])
def list_screenshots(user: str, start: float, end: float, limit: int = Query(500, le=2000),
                     scope: str | None = None, tz: int | None = Query(None, ge=-840, le=840)):
    with db.conn() as c:
        rows = c.execute(
            "SELECT id,user,device,ts,app,title,bytes FROM screenshots "
            "WHERE user=? AND ts>=? AND ts<? ORDER BY ts LIMIT ?",
            (user, start, end, limit)).fetchall()
    if _is_work(scope):
        rows = [r for r in rows if in_work_hours(r["ts"], tz, WORK_START, WORK_END)]
    return [{**dict(r), "name": display_name(r["app"] or "")} for r in rows]


# Thumbnails are made on the server, once, and stored next to the original: a gallery of 500 screenshots then moves
# ~500 x 15 KB instead of 500 x 400 KB. Older screenshots get theirs the first time they are looked at.
THUMB_SIZE = (360, 225)
IMAGE_CACHE = "private, max-age=604800, immutable"   # a screenshot never changes (it can only be deleted)


def _thumb_key(path: str) -> str:
    return "thumbs/" + path


def _make_thumb(data: bytes) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (THUMB_SIZE[0] * 2, THUMB_SIZE[1] * 2))   # JPEG: decode at reduced size (much faster)
    im = im.convert("RGB")
    im.thumbnail(THUMB_SIZE)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=62, optimize=True)
    return buf.getvalue()


def _store_thumb(path: str, data: bytes) -> bytes | None:
    """Best effort: a missing thumbnail must never fail an upload (it is rebuilt on demand)."""
    try:
        t = _make_thumb(data)
        store.put(_thumb_key(path), t)
        return t
    except Exception as e:
        print(f"thumbnail for {path} not stored: {e!r}")
        return None


def _drop_thumb(path: str) -> None:
    try:
        store.delete(_thumb_key(path))
    except ObjectStoreError as e:
        print(f"could not remove thumbnail of {path}: {e!r}")


def _screenshot_path(sid: int) -> str:
    with db.conn() as c:
        row = c.execute("SELECT path FROM screenshots WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    return row["path"]


@app.get("/api/screenshots/{sid}/image", dependencies=[Depends(admin)])
def get_screenshot(sid: int):
    path = _screenshot_path(sid)
    try:
        data = store.get(path)
    except ObjectStoreError as e:
        print(f"screenshot fetch failed, image store error: {e!r}")
        raise HTTPException(404)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": IMAGE_CACHE})


@app.get("/api/screenshots/{sid}/thumb", dependencies=[Depends(admin)])
def get_screenshot_thumb(sid: int):
    """The small version used by galleries and the filmstrip. Falls back to building it from the original."""
    path = _screenshot_path(sid)
    try:
        data = store.get(_thumb_key(path))
    except ObjectStoreError:
        try:
            full = store.get(path)
        except ObjectStoreError as e:
            print(f"screenshot fetch failed, image store error: {e!r}")
            raise HTTPException(404)
        data = _store_thumb(path, full) or full   # cannot resize (corrupt image?): the original is better than nothing
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": IMAGE_CACHE})


class ScreenshotDelete(BaseModel):
    ids: list[int] = Field(max_length=5000)


@app.post("/api/screenshots/delete", dependencies=[Depends(admin)])
def delete_screenshots(d: ScreenshotDelete):
    _bust()
    if not d.ids:
        return {"deleted": 0}
    marks = ",".join("?" * len(d.ids))
    with db.conn() as c:
        rows = c.execute(f"SELECT id, path FROM screenshots WHERE id IN ({marks})", d.ids).fetchall()
        for r in rows:
            store.delete(r["path"])
            _drop_thumb(r["path"])
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
    try:
        # utf-8-sig: Windows PowerShell 5.1 writes a BOM with `Set-Content -Encoding UTF8`, which plain
        # json.loads rejects (that made this endpoint 500 and the installer see "no checksum").
        return json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except ValueError:
        raise HTTPException(500, "dist/manifest.json is not valid JSON; rebuild with windows\\build.ps1")


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


PUBLIC_URL = os.environ.get("CYRIL_PUBLIC_URL", "").rstrip("/")
_HOST_OK = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")


def _base_url(request: Request) -> str:
    """The address agents should use: CYRIL_PUBLIC_URL if set, else whatever the browser/proxy used to reach us."""
    if PUBLIC_URL:
        return PUBLIC_URL
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if proto not in ("http", "https") or not _HOST_OK.match(host):
        raise HTTPException(400, "cannot work out the server address; set CYRIL_PUBLIC_URL")
    return f"{proto}://{host}"


MAC_ARCHES = ("arm64", "x86_64")


def _sh_quote(v: str) -> str:
    return v.replace("'", "'\\''")   # for use inside a '...' string in a shell script


def _ps_quote(v: str) -> str:
    return v.replace("'", "''")  # for use inside a PowerShell single-quoted string


def _agent_one_liners(base: str, key: str) -> dict:
    return {
        "install": f"& ([scriptblock]::Create((irm '{base}/install.ps1?key={quote(key, safe='')}')))",
        "uninstall": f"& ([scriptblock]::Create((irm '{base}/uninstall.ps1')))",
        # Silent roll-out variants: no step list, no first-report wait, one line out (see install.ps1 -Silent).
        "install_silent": f"& ([scriptblock]::Create((irm '{base}/install.ps1?key={quote(key, safe='')}'))) -Silent",
        "install_silent_cmd": "powershell -NoProfile -ExecutionPolicy Bypass -Command \"& ([scriptblock]::Create((irm '"
                              + f"{base}/install.ps1?key={quote(key, safe='')}'))) -Silent\"",
        "mac_install_silent": f"curl -fsSL '{base}/install.sh?key={quote(key, safe='')}' | sudo bash -s -- --silent",
        "mac_install": f"curl -fsSL '{base}/install.sh?key={quote(key, safe='')}' | sudo bash",
        "mac_uninstall": f"curl -fsSL '{base}/uninstall.sh' | sudo bash",
    }


@app.get("/install.ps1")
def install_script(request: Request, key: str | None = None):
    """The agent installer with this server's address (and, if the right key is given, the key) filled in,
    so a machine only needs the one-liner. Without a key it still works if -AgentKey is passed explicitly."""
    if key is not None and not (AGENT_KEY and hmac.compare_digest(key, AGENT_KEY)):
        raise HTTPException(401, "wrong agent key in the install URL")
    text = (WINSCRIPTS / "install.ps1").read_text(encoding="utf-8")
    text = text.replace("{{SERVER_URL}}", _ps_quote(_base_url(request))).replace("{{AGENT_KEY}}", _ps_quote(key or "")).replace("{{PAYLOAD_KEY}}", opener.public_b64)
    return PlainTextResponse(text, headers={"Cache-Control": "no-store"})


@app.get("/uninstall.ps1")
def uninstall_script():
    return PlainTextResponse((WINSCRIPTS / "uninstall.ps1").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


def _published_build(manifest: dict | None) -> str | None:
    """Id of the published agent build; installers stamp the same value on each PC (build.txt)."""
    if not manifest:
        return None
    return manifest.get("build") or (manifest.get("windows_exe", {}).get("sha256") or "")[:12] or None


@app.get("/install.sh")
def install_script_mac(request: Request, key: str | None = None):
    """The macOS installer with this server's address (and, with the right ?key=, the agent key) filled in."""
    if key is not None and not (AGENT_KEY and hmac.compare_digest(key, AGENT_KEY)):
        raise HTTPException(401, "wrong agent key in the install URL")
    text = (MACSCRIPTS / "install.sh").read_text(encoding="utf-8")
    text = text.replace("{{SERVER_URL}}", _sh_quote(_base_url(request))).replace("{{AGENT_KEY}}", _sh_quote(key or "")).replace("{{PAYLOAD_KEY}}", opener.public_b64)
    return PlainTextResponse(text, headers={"Cache-Control": "no-store"})


@app.get("/uninstall.sh")
def uninstall_script_mac():
    return PlainTextResponse((MACSCRIPTS / "uninstall.sh").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/api/agent/mac-manifest")
def mac_manifest(arch: str):
    """The published Mac build for a CPU type, as plain key=value lines (the shell installer has no JSON parser)."""
    import json
    if arch not in MAC_ARCHES:
        raise HTTPException(400, "arch must be arm64 or x86_64")
    path = DIST_DIR / f"manifest-mac-{arch}.json"
    if not path.exists():
        raise HTTPException(404, f"no Mac build for {arch} has been published; run mac/build.sh on a {arch} Mac")
    m = json.loads(path.read_text(encoding="utf-8-sig"))
    lines = {"version": m.get("version", ""), "build": m.get("build", ""), "url": m.get("url", f"/dist/netmon-mac-{arch}"),
             "sha256": m.get("sha256", ""), "selftest": "1" if m.get("selftest") else "0"}
    return PlainTextResponse("".join(f"{k}={v}\n" for k, v in lines.items()))


@app.get("/api/install-info", dependencies=[Depends(admin)])
def install_info(request: Request):
    """What the dashboard's Install page shows: the copy-paste commands and whether the agent build is published."""
    base = _base_url(request)
    manifest = None
    if (DIST_DIR / "manifest.json").exists():
        import json
        try:
            manifest = json.loads((DIST_DIR / "manifest.json").read_text(encoding="utf-8-sig"))
        except ValueError:
            pass
    return {"server_url": base, "sealed_uploads": True, "require_sealed": REQUIRE_SEALED, **_agent_one_liners(base, AGENT_KEY),
            "exe_published": (DIST_DIR / "netmon.exe").is_file(),
            "mac_published": {a: (DIST_DIR / f"manifest-mac-{a}.json").is_file() and (DIST_DIR / f"netmon-mac-{a}").is_file()
                              for a in MAC_ARCHES},
            "version": (manifest or {}).get("version"), "build": _published_build(manifest),
            "mac_builds": [b for b in (_mac_published_build(a) for a in MAC_ARCHES) if b],
            # per CPU type, so the page can say when one Mac build is older than the rest of the release (its build
            # machine never turned up and the previous binary was carried over: "updating" then changes nothing)
            "mac_build_by_arch": {a: _mac_published_build(a) for a in MAC_ARCHES}}


def _mac_published_build(arch: str) -> str | None:
    """Build id the Mac installer stamps for this CPU type (build.txt): the manifest's build, else the start of its sha256."""
    import json
    try:
        m = json.loads((DIST_DIR / f"manifest-mac-{arch}.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return m.get("build") or (m.get("sha256") or "")[:12] or None


class AgentStatusIn(BaseModel):
    user: str = Field(min_length=1, max_length=128)
    device: str = Field(min_length=1, max_length=128)
    info: dict


@app.post("/api/agent/status", dependencies=[Depends(agent)])
def agent_status(s: AgentStatusIn):
    """Each agent reports its own health (can it read web addresses? are screenshots getting through?) so the
    dashboard can say why something is missing on a PC instead of someone reading logs there."""
    import json
    blob = json.dumps(s.info)[:4000]
    with db.conn() as c:
        c.execute("INSERT INTO agent_status(device,user,ts,info) VALUES(?,?,?,?) ON CONFLICT(device) DO UPDATE SET "
                  "user=excluded.user, ts=excluded.ts, info=excluded.info", (s.device, s.user, time.time(), blob))
    return {"ok": True}


_LAST_POLL: dict[str, float] = {}   # device -> when its agent last asked for work
COMMAND_TTL = 90   # seconds a "screenshot now" request stays valid; a PC that is off just never answers


class ShotNowIn(BaseModel):
    device: str = Field(min_length=1, max_length=128)


@app.post("/api/screenshot-now", dependencies=[Depends(admin)])
def screenshot_now(b: ShotNowIn):
    """Ask one PC for a screenshot right now (agents pick it up on their next poll, ~10 s)."""
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM events WHERE device=? LIMIT 1", (b.device,)).fetchone():
            raise HTTPException(404, "no such device")
        cur = c.execute("INSERT INTO agent_commands(device,kind,created) VALUES(?, 'screenshot', ?)", (b.device, time.time()))
    return {"id": cur.lastrowid}


@app.get("/api/screenshot-now/{cid}", dependencies=[Depends(admin)])
def screenshot_now_status(cid: int):
    """pending (waiting for the PC) -> delivered (PC has it) -> done (screenshot_id set) | failed | expired."""
    with db.conn() as c:
        r = c.execute("SELECT * FROM agent_commands WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404)
    status = r["status"]
    if status in ("pending", "delivered") and time.time() - r["created"] > COMMAND_TTL:
        status = "expired"
    return {"id": cid, "status": status, "screenshot_id": r["screenshot_id"], "error": r["error"],
            "waited": round(time.time() - r["created"], 1)}


@app.get("/api/agent/poll", dependencies=[Depends(agent)])
def agent_poll(device: str = Query(min_length=1, max_length=128)):
    """Agents ask every few seconds: is there anything for me to do, and what intervals should I use?"""
    now = time.time()
    _LAST_POLL[device] = now
    with db.conn() as c:
        rows = c.execute("SELECT id, kind FROM agent_commands WHERE device=? AND status='pending' AND created>=?",
                         (device, now - COMMAND_TTL)).fetchall()
        for r in rows:
            c.execute("UPDATE agent_commands SET status='delivered', delivered=? WHERE id=?", (now, r["id"]))
        # A standing uninstall order is sent on every poll until the device is gone (durable, no TTL): a machine that
        # was offline when it was ordered still picks it up the moment it comes back. id 0 = not an agent_commands row.
        removing = bool(c.execute("SELECT 1 FROM device_removals WHERE device=? AND status IN('pending','removing')",
                                  (device,)).fetchone())
    cmds = [{"id": r["id"], "kind": r["kind"]} for r in rows]
    if removing:
        cmds.append({"id": 0, "kind": "uninstall"})
    return {"commands": cmds, "config": {**_screenshot_settings(), "oms_origins": OMS_ORIGINS}}


class CommandResultIn(BaseModel):
    device: str = Field(min_length=1, max_length=128)
    id: int
    ok: bool
    error: str = Field(default="", max_length=500)


@app.post("/api/agent/command-result", dependencies=[Depends(agent)])
def agent_command_result(r: CommandResultIn):
    """Only failures need reporting; a successful screenshot completes its command when it is uploaded."""
    if not r.ok:
        with db.conn() as c:
            c.execute("UPDATE agent_commands SET status='failed', error=? WHERE id=? AND device=?", (r.error, r.id, r.device))
    return {"ok": True}


class DeviceIn(BaseModel):
    device: str = Field(min_length=1, max_length=128)


def _removal(c, device: str) -> dict | None:
    r = c.execute("SELECT * FROM device_removals WHERE device=?", (device,)).fetchone()
    return dict(r) if r else None


@app.post("/api/devices/remove", dependencies=[Depends(admin)])
def remove_device(b: DeviceIn):
    """Order one machine's agent to uninstall itself ("Uninstall now"). Durable: the agent picks it up on its next
    poll (~10 s) and goes quiet after a final flush; the machine's privileged updater/recovery task then tears the
    agent down silently (no popup) and reports back. The device's past activity is KEPT on the server."""
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM events WHERE device=? LIMIT 1", (b.device,)).fetchone():
            raise HTTPException(404, "no such device")
        cur = _removal(c, b.device)
        if cur and cur["status"] == "removed":
            return {"status": "removed", "removed": cur["removed"]}   # already gone; ordering again is a no-op
        c.execute("INSERT INTO device_removals(device,requested,requested_by,status) VALUES(?,?,'admin','pending') "
                  "ON CONFLICT(device) DO UPDATE SET requested=excluded.requested, requested_by='admin', "
                  "status='pending', flushed=NULL, removed=NULL, detail=NULL",
                  (b.device, time.time()))
    return {"status": "pending"}


@app.post("/api/devices/remove/cancel", dependencies=[Depends(admin)])
def cancel_device_removal(b: DeviceIn):
    """Call off a removal while it is still pending or in progress. Once a machine reports it is fully removed it
    cannot be un-removed from here (reinstall the agent on it instead)."""
    with db.conn() as c:
        cur = _removal(c, b.device)
        if not cur:
            raise HTTPException(404, "no removal ordered for this device")
        if cur["status"] == "removed":
            raise HTTPException(409, "this device is already removed")
        c.execute("UPDATE device_removals SET status='canceled' WHERE device=?", (b.device,))
    return {"status": "canceled"}


@app.get("/api/devices/remove/{device}", dependencies=[Depends(admin)])
def device_removal_status(device: str):
    """pending (ordered) -> removing (agent flushed and went quiet) -> removed (privileged teardown done) | canceled."""
    with db.conn() as c:
        cur = _removal(c, device)
    if not cur:
        return {"device": device, "status": "none"}
    return cur


@app.get("/api/agent/removal", dependencies=[Depends(agent)])
def agent_removal(device: str = Query(min_length=1, max_length=128)):
    """Read by a machine's privileged task (the Windows SYSTEM recovery/updater, the mac root daemon) to decide
    whether to tear the agent down. Plain key=value lines, so a shell with no JSON parser can read it (same as
    /api/agent/mac-manifest)."""
    with db.conn() as c:
        cur = _removal(c, device)
    remove = bool(cur and cur["status"] in ("pending", "removing"))
    return PlainTextResponse(f"remove={'1' if remove else '0'}\nstatus={cur['status'] if cur else 'none'}\n")


class RemovalStateIn(BaseModel):
    device: str = Field(min_length=1, max_length=128)
    state: str = Field(pattern="^(removing|removed)$")
    detail: str = Field(default="", max_length=2000)


@app.post("/api/agent/removal-state", dependencies=[Depends(agent)])
def agent_removal_state(s: RemovalStateIn):
    """The agent reports `removing` once it has flushed and gone quiet; the privileged teardown reports `removed`.
    Reporting on a device with no order (or a canceled one) is ignored, so a late report can't resurrect a removal."""
    now = time.time()
    with db.conn() as c:
        cur = _removal(c, s.device)
        if not cur or cur["status"] == "canceled":
            return {"ok": True, "status": cur["status"] if cur else "none"}
        if s.state == "removing" and cur["status"] == "pending":
            c.execute("UPDATE device_removals SET status='removing', flushed=?, detail=? WHERE device=?",
                      (now, s.detail[:2000], s.device))
        elif s.state == "removed":
            c.execute("UPDATE device_removals SET status='removed', removed=?, detail=? WHERE device=?",
                      (now, s.detail[:2000], s.device))
    return {"ok": True, "status": s.state}


@app.get("/api/agent/seen", dependencies=[Depends(agent)])
def agent_seen(device: str = Query(min_length=1, max_length=128), since: float = 0):
    """Used by the installer to confirm a first report arrived. `with_app` counts samples where the agent saw a
    real foreground window while the user was active; zero with `events` > 0 means it reports but is blind
    (typically running outside the user's desktop session)."""
    with db.conn() as c:
        r = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(app<>'' AND idle=0),0) with_app, MAX(ts+dur) last "
            "FROM events WHERE device=? AND ts+dur>=?", (device, since)).fetchone()
    return {"seen": r["n"] > 0, "events": r["n"], "with_app": r["with_app"], "last_seen": r["last"]}


_BAD_KEY_CHARS = re.compile(r"[\s/,]")


def _storage_hint(error: str, access_key: str) -> str:
    """Plain-English next step for the common ways the image store setup goes wrong."""
    e = error.lower()
    if access_key and _BAD_KEY_CHARS.search(access_key):
        return ("CYRIL_S3_ACCESS_KEY contains a '/', a space or a comma. It is used as the storage login id and cannot. "
                "In server/.env change it to letters and digits only (for example cyrilstorage), then run "
                "docker compose up -d --force-recreate. The secret key may keep any characters.")
    if not access_key:
        return "CYRIL_S3_ACCESS_KEY is empty. Set CYRIL_S3_ACCESS_KEY and CYRIL_S3_SECRET_KEY in server/.env, then docker compose up -d --force-recreate."
    if "mal-formed" in e or "authorizationqueryparameterserror" in e:
        return ("The storage login id is malformed: CYRIL_S3_ACCESS_KEY probably has a '/', space or comma in it. Use letters "
                "and digits only, then docker compose up -d --force-recreate.")
    if "signaturedoesnotmatch" in e:
        return "The secret key differs between the server and SeaweedFS. Make sure server/.env has one CYRIL_S3_SECRET_KEY, then docker compose up -d --force-recreate."
    if "invalidaccesskeyid" in e:
        return "SeaweedFS does not know this access key. Run docker compose up -d --force-recreate so both containers read the same server/.env."
    if "unreachable" in e:
        return "The server cannot reach SeaweedFS. Run docker compose ps: the seaweedfs container should be Up (healthy). Try docker compose logs seaweedfs --tail 30."
    if "no free volume" in e or "no writable volume" in e:
        return "SeaweedFS has no room to allocate. Free some disk space, then docker compose up -d --force-recreate seaweedfs."
    return "On the server run docker compose logs seaweedfs --tail 30 and docker compose logs server --tail 30."


@app.get("/api/storage-health", dependencies=[Depends(admin)])
def storage_health():
    """Write, read back and delete a tiny test object in the screenshot store, and say exactly what failed and what to do."""
    access_key = getattr(store, "access_key", None)
    if access_key is not None and (not access_key or _BAD_KEY_CHARS.search(access_key)):   # a request with that id cannot be valid
        return {"ok": False, "error": "invalid CYRIL_S3_ACCESS_KEY", "hint": _storage_hint("", access_key or "")}
    key = f"_healthcheck/{int(time.time() * 1000)}.txt"
    try:
        store.put(key, b"ok", "text/plain")
        ok = store.get(key) == b"ok"
        store.delete(key)
    except ObjectStoreError as e:
        return {"ok": False, "error": str(e)[:400], "hint": _storage_hint(str(e), access_key or "")}
    return {"ok": ok, "error": "" if ok else "wrote an object but could not read the same bytes back", "hint": ""}


@app.get("/api/devices", dependencies=[Depends(admin)])
def devices():
    """Every machine that has reported, newest first, with the health each agent reported about itself."""
    import json
    with db.conn() as c:
        rows = c.execute(
            "SELECT e.device, e.user, n.name, MAX(e.ts+e.dur) last_seen, MIN(e.ts) first_seen, COUNT(*) events, "
            "st.info status, st.ts status_ts, rm.status removal_status, rm.requested removal_requested, "
            "rm.removed removal_removed FROM events e "
            "LEFT JOIN user_names n ON n.user=e.user LEFT JOIN agent_status st ON st.device=e.device "
            "LEFT JOIN device_removals rm ON rm.device=e.device "
            "GROUP BY e.device, e.user ORDER BY last_seen DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Surface a removal order (Uninstall now) so the Install page can show pending/removing/removed per machine.
        rm_status = d.pop("removal_status", None)
        rm_requested = d.pop("removal_requested", None)
        rm_removed = d.pop("removal_removed", None)
        d["removal"] = {"status": rm_status, "requested": rm_requested, "removed": rm_removed} if rm_status else None
        d["last_poll"] = _LAST_POLL.get(d["device"])   # None = this agent has never asked for work (too old, or off)
        try:
            d["status"] = json.loads(d["status"]) if d["status"] else None
        except ValueError:
            d["status"] = None
        out.append(d)
    return out


def _build_id() -> str:
    """Short hash of the code + dashboard files actually running, shown in the dashboard so it is obvious
    which version is live (and whether the browser is showing a stale copy)."""
    h = hashlib.sha1()
    for f in sorted(Path(__file__).parent.rglob("*")):
        if f.is_file() and f.suffix in (".py", ".html", ".js", ".css", ".ps1"):
            h.update(f.relative_to(Path(__file__).parent).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:8]


BUILD = _build_id()


@app.get("/api/health")
def health():
    return {"ok": True, "time": time.time(), "build": BUILD}


app.mount("/static", _Static(directory=str(STATIC)), name="static")

# The dashboard pages are NOT served to anyone who is not signed in: the server answers with a redirect to /login
# before a single byte of the interface is sent (the old behaviour sent the whole page and let JavaScript bounce the
# visitor, so the interface flashed up first). Only /login, and the generic /static/app.js + app.css it needs, are
# public. Everything private (all data) was always behind the token-protected /api/* endpoints.
PAGES_DIR = Path(__file__).parent / "pages"
PAGES = {"/": "index.html", "/employees": "employees.html", "/employee": "employee.html",
         "/day": "day.html", "/web": "web.html", "/alerts": "alerts.html", "/install": "install.html", "/screenshots": "screenshots.html",
         "/rules": "rules.html"}
_NO_STORE = {"Cache-Control": "no-store"}   # never let a shared/proxy cache keep a signed-in page


def _safe_next(n: str | None) -> str:
    return n if n and re.match(r"^/(?!/)[^\\\s]*$", n) else "/"


_ADMIN_PAGES = {"/"}   # Admin mode sees the Overview only; every other page needs SuperAdmin mode


def _page(name: str, path: str):
    def handler(request: Request):
        if not _signed_in(request):
            nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
            return RedirectResponse("/login?next=" + quote(nxt, safe=""), status_code=302, headers=_NO_STORE)
        sup = _is_super(request)
        if path not in _ADMIN_PAGES and not sup:
            return RedirectResponse("/", status_code=302, headers=_NO_STORE)
        # The page learns its mode from this tag (the cookie itself is HttpOnly), so the nav is right on first paint.
        html = (PAGES_DIR / name).read_text(encoding="utf-8").replace(
            "<head>", f'<head><meta name="tb-mode" content="{"super" if sup else "admin"}">', 1)
        return HTMLResponse(html, headers=_NO_STORE)
    return handler


for _path, _file in PAGES.items():
    app.get(_path, include_in_schema=False)(_page(_file, _path))


@app.get("/superadmin", include_in_schema=False)
def superadmin_page(request: Request):
    """Unlocks SuperAdmin mode for this browser. Same admin token: sign in first (login bounces back here)."""
    if not _signed_in(request):
        return RedirectResponse("/login?next=%2Fsuperadmin", status_code=302, headers=_NO_STORE)
    r = RedirectResponse("/", status_code=302, headers=_NO_STORE)
    r.set_cookie(SUPER_COOKIE, _super_value(), httponly=True, samesite="lax", secure=_https(request), path="/")
    return r


@app.get("/login", include_in_schema=False)
def login_page(request: Request, next: str | None = None):
    if _signed_in(request):
        return RedirectResponse(_safe_next(next), status_code=302, headers=_NO_STORE)
    return FileResponse(PAGES_DIR / "login.html", headers=_NO_STORE)
