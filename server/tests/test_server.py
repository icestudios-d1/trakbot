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
        {"ts": now + 600, "dur": 300, "app": "chrome", "title": "YT", "url": "https://www.youtube.com/watch"},
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
    assert s["sites"][0]["name"] == "youtube.com"
    assert c.get("/api/alerts", headers=A).json()[0]["category"] == "Piracy"
    # reclassification: make youtube productive, history updates
    c.post("/api/rules", headers=A, json={"kind": "domain", "pattern": "youtube.com", "category": "Training", "label": "productive", "priority": 100})
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
