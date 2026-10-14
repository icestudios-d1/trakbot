import base64
import os
import tempfile
import time
from unittest import mock

os.environ["CYRIL_ADMIN_TOKEN"] = "admin"
os.environ["CYRIL_AGENT_KEY"] = "agent"
os.environ["CYRIL_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CYRIL_DIST_DIR"] = tempfile.mkdtemp()

from cyril_server.objstore import ObjectStoreError  # noqa: E402


class FakeStore:
    """In-memory stand-in for the real S3Store (SeaweedFS client) -- keeps
    these tests from needing a running SeaweedFS container."""

    def __init__(self, *a, **kw):
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self):
        pass

    def put(self, key, data, content_type="image/jpeg"):
        self.objects[key] = data

    def get(self, key):
        if key not in self.objects:
            raise ObjectStoreError("not found")
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)


with mock.patch("cyril_server.objstore.S3Store", FakeStore):
    from fastapi.testclient import TestClient  # noqa: E402

    from cyril_server.app import app  # noqa: E402

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
    """Screenshots round-trip through the (faked) SeaweedFS S3 store, not local disk."""
    jpeg_b64 = base64.b64encode(b"\xff\xd8\xff\xe0fakejpegbytes").decode()
    now = time.time()
    r = c.post("/api/screenshot", headers=K, json={
        "user": "shotty", "device": "pc1", "ts": now, "app": "chrome", "title": "t", "image_b64": jpeg_b64})
    sid = r.json()["id"]
    rows = c.get("/api/screenshots", headers=A, params={"user": "shotty", "start": now - 5, "end": now + 5}).json()
    assert len(rows) == 1 and rows[0]["id"] == sid
    img = c.get(f"/api/screenshots/{sid}/image", headers=A)
    assert img.status_code == 200 and img.content == base64.b64decode(jpeg_b64)
    # no auth -> 401; path traversal in user is sanitized before becoming an object key
    assert c.get(f"/api/screenshots/{sid}/image").status_code == 401
    d = c.post("/api/screenshots/delete", headers=A, json={"ids": [sid]})
    assert d.json() == {"deleted": 1}
    assert c.get(f"/api/screenshots/{sid}/image", headers=A).status_code == 404


def test_install_scripts_served():
    assert "ScheduledTask" in c.get("/install.ps1").text
    assert "Unregister-ScheduledTask" in c.get("/uninstall.ps1").text
    assert "cyril.exe" in c.get("/install.ps1").text


def test_agent_version_manifest():
    # no manifest dropped yet -> version: None, no update
    assert c.get("/api/agent/version").json() == {"version": None}
    dist_dir = os.environ["CYRIL_DIST_DIR"]
    with open(os.path.join(dist_dir, "manifest.json"), "w") as f:
        f.write('{"version": "0.2.0", "windows_exe": {"url": "/dist/cyril.exe", "sha256": "abc123"}}')
    assert c.get("/api/agent/version").json()["version"] == "0.2.0"


def test_update_log_written_by_agent_and_readable_by_admin():
    assert c.get("/api/agent/update-log", headers=A).json() == []
    r = c.post("/api/agent/update-log", headers=K, json={
        "user": "bob", "device": "pc1", "from_version": "0.1.0", "to_version": "0.2.0",
        "ok": True, "detail": "applied"})
    assert r.json() == {"ok": True}
    rows = c.get("/api/agent/update-log", headers=A).json()
    assert len(rows) == 1 and rows[0]["to_version"] == "0.2.0" and rows[0]["ok"] == 1
    # agent key doesn't grant admin reads, and vice versa
    assert c.get("/api/agent/update-log").status_code == 401
    assert c.post("/api/agent/update-log", headers=A, json={
        "user": "x", "device": "y", "from_version": "a", "to_version": "b", "ok": True}).status_code == 401


def _day():
    return time.strftime("%Y-%m-%d")


def test_pages_and_me():
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/me", headers=A).json() == {"ok": True}
    for path in ("/", "/login", "/employees", "/employee", "/day", "/alerts", "/screenshots", "/rules"):
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


def test_overview_and_employee_reports():
    now = time.time()
    ev = [
        {"ts": now - 4000, "dur": 1800, "app": "Photoshop.exe", "title": "poster.psd"},
        {"ts": now - 2200, "dur": 600, "app": "chrome", "title": "mix", "url": "https://www.youtube.com/watch"},
        {"ts": now - 1600, "dur": 300, "idle": True},
    ]
    assert c.post("/api/ingest", headers=K, json={"user": "dana", "device": "d1", "events": ev}).json()["stored"] == 3
    q = {"start": _day(), "end": _day(), "tz": 0}
    e = c.get("/api/employee", headers=A, params={"user": "dana", **q}).json()
    assert e["seconds"]["productive"] == 1800 and e["creative_seconds"] == 1800
    assert e["creative_apps"][0]["name"] == "Photoshop.exe"
    assert len(e["daily"]) == 1 and len(e["hourly"]) == 24 and len(e["heatmap"]) == 7
    ov = c.get("/api/overview", headers=A, params=q).json()
    assert ov["creative_seconds"] >= 1800 and "dana" in ov["by_user"] and "previous" in ov
    tl = c.get("/api/timeline", headers=A, params={"user": "dana", "day": _day(), "tz": 0}).json()
    assert [t["app"] for t in tl][:2] == ["Photoshop.exe", "chrome"] and tl[2]["idle"] and tl[0]["label"] == "productive"


def test_rename_user():
    c.post("/api/ingest", headers=K, json={"user": "pc-front-desk", "device": "d", "events": [{"ts": time.time(), "dur": 30, "app": "code"}]})
    row = lambda: [u for u in c.get("/api/users", headers=A).json() if u["user"] == "pc-front-desk"][0]
    assert row()["name"] is None
    assert c.post("/api/users/rename", json={"user": "pc-front-desk", "name": "Ana"}).status_code == 401
    r = c.post("/api/users/rename", headers=A, json={"user": "pc-front-desk", "name": "  Ana   Lopez "})
    assert r.json() == {"user": "pc-front-desk", "name": "Ana Lopez"} and row()["name"] == "Ana Lopez"
    # data stays keyed on the login, so history is untouched and renaming again just replaces the tag
    assert c.get("/api/employee", headers=A, params={"user": "pc-front-desk", "start": _day(), "end": _day()}).json()["seconds"]["productive"] == 30
    c.post("/api/users/rename", headers=A, json={"user": "pc-front-desk", "name": "Ben"})
    assert row()["name"] == "Ben"
    c.post("/api/users/rename", headers=A, json={"user": "pc-front-desk", "name": "  "})
    assert row()["name"] is None


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
