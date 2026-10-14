import base64
import os
import tempfile
import time

os.environ["TRAKBOT_ADMIN_TOKEN"] = "admin"
os.environ["TRAKBOT_AGENT_KEY"] = "agent"
os.environ["TRAKBOT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from fastapi.testclient import TestClient  # noqa: E402

from trakbot_server.app import app  # noqa: E402

c = TestClient(app)
A = {"Authorization": "Bearer admin"}
K = {"X-Api-Key": "agent"}


def test_auth():
    assert c.get("/api/summary").status_code == 401
    assert c.post("/api/ingest", json={"user": "a", "device": "d", "events": []}).status_code == 401


def test_flow():
    now = time.time()
    ev = [
        {"ts": now, "dur": 600, "app": "code", "title": "x"},
        {"ts": now + 600, "dur": 300, "app": "chrome", "title": "FB", "url": "https://www.facebook.com/feed"},
        {"ts": now + 900, "dur": 200, "app": "chrome", "title": "t", "url": "https://thepiratebay.org/"},
        {"ts": now + 1100, "dur": 100, "idle": True},
    ]
    r = c.post("/api/ingest", headers=K, json={"user": "bob", "device": "pc1", "events": ev})
    assert r.json() == {"stored": 4, "alerts": 1}
    s = c.get("/api/summary", headers=A, params={"user": "bob"}).json()
    assert s["seconds"]["productive"] == 600
    assert s["seconds"]["unproductive"] == 500
    assert s["seconds"]["idle"] == 100
    assert s["productivity_pct"] == 54.5
    assert s["sites"][0]["name"] == "facebook.com"
    assert c.get("/api/alerts", headers=A).json()[0]["category"] == "Piracy"
    # reclassification: make facebook productive, history updates
    c.post("/api/rules", headers=A, json={"kind": "domain", "pattern": "facebook.com", "category": "Training", "label": "productive", "priority": 100})
    s = c.get("/api/summary", headers=A, params={"user": "bob"}).json()
    assert s["seconds"]["productive"] == 900


def test_validation():
    r = c.post("/api/ingest", headers=K, json={"user": "a", "device": "d", "events": [{"ts": 1, "dur": 99999}]})
    assert r.status_code == 422


def test_private_browsing_flagged():
    now = time.time()
    ev = [{"ts": now, "dur": 60, "app": "chrome", "title": "(private/incognito browsing)"}]
    c.post("/api/ingest", headers=K, json={"user": "priv", "device": "pc1", "events": ev})
    alerts = c.get("/api/alerts", headers=A).json()
    assert any(a["category"] == "Private/Incognito Browsing" and a["user"] == "priv" for a in alerts)


def test_trend():
    now = time.time()
    c.post("/api/ingest", headers=K, json={"user": "trendy", "device": "d", "events": [
        {"ts": now, "dur": 300, "app": "code", "title": "x"}]})
    rows = c.get("/api/trend", headers=A, params={"user": "trendy", "days": 3}).json()
    assert len(rows) == 3
    assert sum(r["productive"] for r in rows) == 300


def test_screenshot_upload_list_image_delete():
    jpeg_b64 = base64.b64encode(b"\xff\xd8\xff\xe0fakejpegbytes").decode()
    now = time.time()
    r = c.post("/api/screenshot", headers=K, json={
        "user": "shotty", "device": "pc1", "ts": now, "app": "chrome", "title": "t", "image_b64": jpeg_b64})
    sid = r.json()["id"]
    rows = c.get("/api/screenshots", headers=A, params={"user": "shotty", "start": now - 5, "end": now + 5}).json()
    assert len(rows) == 1 and rows[0]["id"] == sid
    img = c.get(f"/api/screenshots/{sid}/image", headers=A)
    assert img.status_code == 200 and img.content == base64.b64decode(jpeg_b64)
    # no auth -> 401; wrong path traversal in user is sanitized, not exploitable
    assert c.get(f"/api/screenshots/{sid}/image").status_code == 401
    d = c.post("/api/screenshots/delete", headers=A, json={"ids": [sid]})
    assert d.json() == {"deleted": 1}
    assert c.get(f"/api/screenshots/{sid}/image", headers=A).status_code == 404


def test_install_scripts_served():
    assert "ScheduledTask" in c.get("/install.ps1").text
    assert "Unregister-ScheduledTask" in c.get("/uninstall.ps1").text


def _day():
    return time.strftime("%Y-%m-%d")


def test_pages_and_me():
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/me", headers=A).json() == {"ok": True}
    for path in ("/", "/login", "/employees", "/employee", "/overlap", "/alerts", "/screenshots", "/rules"):
        r = c.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"], path
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/api/overview").status_code == 401


def test_studio_rules_classify_creative_apps():
    for name, cat in (("Photoshop.exe", "Design - Photoshop"), ("Illustrator.exe", "Design - Illustrator"),
                      ("AfterFX.exe", "Motion - After Effects"), ("SketchUp.exe", "3D"),
                      ("Adobe Premiere Pro.exe", "Video Editing"), ("Spotify.exe", "Music")):
        r = c.post("/api/rules/test", headers=A, json={"app": name}).json()["rule"]
        assert r and r["category"] == cat, name
    assert c.post("/api/rules/test", headers=A, json={"domain": "https://www.youtube.com/x"}).json()["rule"]["label"] == "neutral"
    # "community.exe" must not be mistaken for Unity
    assert c.post("/api/rules/test", headers=A, json={"app": "community.exe"}).json()["rule"] is None


def test_rule_edit_and_seed():
    rid = c.post("/api/rules", headers=A, json={"kind": "app", "pattern": "zzz*", "category": "X", "label": "neutral"}).json()["id"]
    assert c.put(f"/api/rules/{rid}", headers=A, json={"kind": "app", "pattern": "zzz*", "category": "Y", "label": "productive"}).status_code == 200
    assert [r for r in c.get("/api/rules", headers=A).json() if r["id"] == rid][0]["category"] == "Y"
    assert c.put("/api/rules/999999", headers=A, json={"kind": "app", "pattern": "a", "category": "Y", "label": "neutral"}).status_code == 404
    assert c.post("/api/rules/seed-studio", headers=A).json() == {"added": 0}  # fresh DB already has them
    c.delete(f"/api/rules/{rid}", headers=A)


def test_overview_employee_and_background_overlap():
    now = time.time()
    # dana designs in Photoshop for 30m with YouTube music playing in the background, then watches YouTube for 10m
    ev = [
        {"ts": now - 4000, "dur": 1800, "app": "Photoshop.exe", "title": "poster.psd", "bg": ["youtube.com"]},
        {"ts": now - 2200, "dur": 600, "app": "chrome", "title": "mix", "url": "https://www.youtube.com/watch", "bg": ["youtube.com"]},
        {"ts": now - 1600, "dur": 300, "idle": True, "bg": ["youtube.com"]},
    ]
    assert c.post("/api/ingest", headers=K, json={"user": "dana", "device": "d1", "events": ev}).json()["stored"] == 3
    q = {"start": _day(), "end": _day(), "tz": 0}
    o = c.get("/api/overlap", headers=A, params={"user": "dana", **q}).json()
    assert o["has_background_data"]
    yt = [x for x in o["sources"] if x["name"] == "youtube.com"][0]
    # foreground YouTube (600s) is active; it's only passive while something else is in front / user idle
    assert yt["active"] == 600 and yt["passive"] == 1800 + 300
    assert yt["during"][0] == {"name": "Photoshop.exe", "seconds": 1800}
    assert o["while_working_seconds"] == 1800 and o["while_idle_seconds"] == 300
    ps = [x for x in o["sources"] if x["name"] == "Photoshop.exe"][0]
    assert ps["active"] == 1800 and ps["passive"] == 0
    e = c.get("/api/employee", headers=A, params={"user": "dana", **q}).json()
    assert e["seconds"]["productive"] == 1800 and e["creative_seconds"] == 1800
    assert e["creative_apps"][0]["name"] == "Photoshop.exe"
    assert len(e["daily"]) == 1 and len(e["hourly"]) == 24 and len(e["heatmap"]) == 7
    assert e["overlap"]["passive_seconds"] == 2100
    ov = c.get("/api/overview", headers=A, params=q).json()
    assert ov["creative_seconds"] >= 1800 and "dana" in ov["by_user"]
    assert ov["background"]["has_background_data"] and "previous" in ov


def test_range_validation_and_timezone():
    assert c.get("/api/overview", headers=A, params={"start": "2020-01-01", "end": "2022-01-01"}).status_code == 400
    # an event at 23:30 UTC lands in the *next* local day for UTC+2
    ts = time.mktime(time.strptime("2031-03-04 23:30", "%Y-%m-%d %H:%M")) - time.timezone  # 23:30 UTC
    c.post("/api/ingest", headers=K, json={"user": "tzu", "device": "d", "events": [{"ts": ts, "dur": 60, "app": "code"}]})
    r = c.get("/api/employee", headers=A, params={"user": "tzu", "start": "2031-03-05", "end": "2031-03-05", "tz": 120}).json()
    assert r["seconds"]["productive"] == 60 and r["hourly"][1]["productive"] == 60
    r = c.get("/api/employee", headers=A, params={"user": "tzu", "start": "2031-03-05", "end": "2031-03-05", "tz": 0}).json()
    assert r["seconds"]["productive"] == 0


def test_uncategorized_and_alert_filters():
    now = time.time()
    c.post("/api/ingest", headers=K, json={"user": "unc", "device": "d", "events": [
        {"ts": now, "dur": 90, "app": "mystery.exe"}, {"ts": now + 90, "dur": 40, "app": "chrome", "url": "https://weird.example/x"}]})
    u = c.get("/api/uncategorized", headers=A).json()
    assert any(a["name"] == "mystery.exe" for a in u["apps"]) and any(x["name"] == "weird.example" for x in u["sites"])
    assert all(a["user"] == "priv" for a in c.get("/api/alerts", headers=A, params={"user": "priv"}).json())
