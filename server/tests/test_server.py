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
    for path in ("/", "/login", "/employees", "/employee", "/day", "/web", "/install", "/alerts", "/screenshots", "/rules"):
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
    assert e["creative_apps"][0]["name"] == "Photoshop"
    assert len(e["daily"]) == 1 and len(e["hourly"]) == 24 and len(e["heatmap"]) == 7
    ov = c.get("/api/overview", headers=A, params=q).json()
    assert ov["creative_seconds"] >= 1800 and "dana" in ov["by_user"] and "previous" in ov
    tl = c.get("/api/timeline", headers=A, params={"user": "dana", "day": _day(), "tz": 0}).json()
    assert [t["name"] for t in tl][:2] == ["Photoshop", "YouTube"] and tl[2]["idle"] and tl[0]["label"] == "productive"


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


def test_install_script_is_personalised_per_request():
    plain = c.get("/install.ps1").text
    assert "{{" not in plain
    assert "[string]$ServerUrl = 'http://testserver'" in plain and "[string]$AgentKey = ''" in plain
    keyed = c.get("/install.ps1", params={"key": "agent"}, headers={"Host": "cyril.icestudios.in", "X-Forwarded-Proto": "https"}).text
    assert "[string]$ServerUrl = 'https://cyril.icestudios.in'" in keyed and "[string]$AgentKey = 'agent'" in keyed
    assert c.get("/install.ps1", params={"key": "nope"}).status_code == 401
    assert c.get("/install.ps1", headers={"Host": "evil host;x"}).status_code == 400
    assert c.get("/install.ps1", params={"key": "agent"}).headers["cache-control"] == "no-store"
    from cyril_server.app import _ps_quote
    assert _ps_quote("a'b") == "a''b"


def test_windows_scripts_are_ascii_only():
    # Windows PowerShell 5.1 mis-decodes non-ASCII served as text/plain without a BOM
    for f in ("install.ps1", "uninstall.ps1"):
        text = c.get("/" + f).text
        assert text.isascii(), f


def test_install_info_and_devices():
    assert c.get("/api/install-info").status_code == 401 and c.get("/api/devices").status_code == 401
    i = c.get("/api/install-info", headers=A).json()
    assert i["install"] == "& ([scriptblock]::Create((irm 'http://testserver/install.ps1?key=agent')))"
    assert i["uninstall"].endswith("/uninstall.ps1')))") and i["exe_published"] is False
    c.post("/api/ingest", headers=K, json={"user": "devy", "device": "PC-DEVY", "events": [{"ts": time.time(), "dur": 5, "app": "code"}]})
    d = [x for x in c.get("/api/devices", headers=A).json() if x["device"] == "PC-DEVY"]
    assert len(d) == 1 and d[0]["user"] == "devy" and d[0]["events"] == 1


def test_static_files_always_revalidate_and_health_reports_build():
    r = c.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert c.get("/", headers={}).headers["cache-control"] == "no-cache"
    h = c.get("/api/health").json()
    assert h["ok"] and len(h["build"]) == 8


def test_agent_seen_for_installer_verification():
    now = time.time()
    q = {"device": "PC-SEEN", "since": now - 60}
    assert c.get("/api/agent/seen", params=q).status_code == 401           # needs the agent key
    assert c.get("/api/agent/seen", params=q, headers=A).status_code == 401  # admin token is not enough
    assert c.get("/api/agent/seen", params=q, headers=K).json()["seen"] is False
    c.post("/api/ingest", headers=K, json={"user": "u", "device": "PC-SEEN", "events": [
        {"ts": now - 20, "dur": 10, "app": "", "title": "", "idle": False},   # blind sample (no window)
        {"ts": now - 10, "dur": 10, "app": "Photoshop.exe", "title": "x"}]})
    r = c.get("/api/agent/seen", params=q, headers=K).json()
    assert r["seen"] and r["events"] == 2 and r["with_app"] == 1
    assert c.get("/api/agent/seen", params={"device": "PC-SEEN", "since": now + 600}, headers=K).json()["seen"] is False


def test_dist_files_are_never_cached():
    d = os.environ["CYRIL_DIST_DIR"]
    with open(os.path.join(d, "probe.bin"), "wb") as f:
        f.write(b"x")
    r = c.get("/dist/probe.bin")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert c.get("/static/app.css").headers["cache-control"] == "no-cache"


def test_manifest_with_utf8_bom_is_readable():
    # Windows PowerShell 5.1 writes a BOM; this used to 500 and the installer then saw "no checksum"
    d = os.environ["CYRIL_DIST_DIR"]
    with open(os.path.join(d, "manifest.json"), "wb") as f:
        f.write(b"\xef\xbb\xbf" + b'{"version": "0.3.0", "build": "abc", "windows_exe": {"url": "/dist/cyril.exe", "sha256": "ff"}}')
    r = c.get("/api/agent/version")
    assert r.status_code == 200 and r.json()["windows_exe"]["sha256"] == "ff"
    assert c.get("/api/install-info", headers=A).json()["version"] == "0.3.0"
    with open(os.path.join(d, "manifest.json"), "w") as f:
        f.write("{not json")
    assert c.get("/api/agent/version").status_code == 500  # clear error rather than a crash trace
    os.remove(os.path.join(d, "manifest.json"))


def test_reporting_interval_setting_and_its_effect():
    assert c.get("/api/settings").status_code == 401
    st0 = c.get("/api/settings", headers=A).json()
    assert st0["report_interval"] == 0 and st0["report_interval_choices"] == [0, 1, 5, 10, 15, 30]
    assert st0["screenshot_every"] == 60 and st0["screenshot_stable_every"] == 180
    assert c.put("/api/settings", headers=A, json={"report_interval": 7}).status_code == 400
    # two apps inside one 15-minute block: Photoshop 6 min, reddit 8 min -> the block counts as reddit (unproductive)
    base = time.mktime(time.strptime("2031-06-02 10:00", "%Y-%m-%d %H:%M")) - time.timezone  # 10:00 UTC
    c.post("/api/ingest", headers=K, json={"user": "slotty", "device": "d", "events": [
        {"ts": base, "dur": 360, "app": "Photoshop.exe", "title": "p.psd"},
        {"ts": base + 360, "dur": 480, "app": "chrome", "title": "m", "url": "https://www.reddit.com/x"}]})
    q = {"user": "slotty", "start": "2031-06-02", "end": "2031-06-02", "tz": 0}
    exact = c.get("/api/employee", headers=A, params=q).json()
    assert exact["slot"] == 0 and exact["seconds"]["productive"] == 360 and exact["seconds"]["unproductive"] == 480
    macro = c.get("/api/employee", headers=A, params={**q, "slot": 15}).json()
    assert macro["slot"] == 15 and macro["seconds"]["unproductive"] == 840 and macro["seconds"]["productive"] == 0
    assert macro["apps"][0]["name"] == "Reddit"
    assert c.get("/api/employee", headers=A, params={**q, "slot": 3}).status_code == 400
    # the saved setting is the default for every report, and ?slot= still overrides it
    assert c.put("/api/settings", headers=A, json={"report_interval": 15}).json()["report_interval"] == 15
    assert c.get("/api/employee", headers=A, params=q).json()["seconds"]["unproductive"] == 840
    assert c.get("/api/employee", headers=A, params={**q, "slot": 0}).json()["seconds"]["productive"] == 360
    ov = c.get("/api/overview", headers=A, params={"start": "2031-06-02", "end": "2031-06-02", "tz": 0}).json()
    assert ov["slot"] == 15
    tl = c.get("/api/timeline", headers=A, params={"user": "slotty", "day": "2031-06-02", "tz": 0}).json()
    assert len(tl) == 1 and tl[0]["slot"] is True and tl[0]["domain"] == "reddit.com" and tl[0]["dur"] == 840
    assert tl[0]["breakdown"][0] == {"name": "Reddit", "seconds": 480} and tl[0]["name"] == "Reddit"
    rows = c.get("/api/slots", headers=A, params={"user": "slotty", "start": "2031-06-02", "end": "2031-06-02", "tz": 0}).json()
    assert rows["slot"] == 15 and len(rows["rows"]) == 1 and rows["rows"][0]["label"] == "unproductive"
    assert c.get("/api/slots", headers=A, params={"user": "slotty", "start": "2031-01-01", "end": "2031-03-01"}).status_code == 400
    c.put("/api/settings", headers=A, json={"report_interval": 0})


def test_web_report_has_full_urls_and_friendly_site_names():
    assert c.get("/api/web").status_code == 401
    base = time.mktime(time.strptime("2031-07-01 09:00", "%Y-%m-%d %H:%M")) - time.timezone
    u1, u2 = "https://www.freepik.com/search?format=search&query=poster+mockup", "https://claude.ai/chat/abc-123"
    c.post("/api/ingest", headers=K, json={"user": "webby", "device": "d", "events": [
        {"ts": base, "dur": 120, "app": "msedge.exe", "title": "poster mockup | Freepik", "url": u1},
        {"ts": base + 120, "dur": 60, "app": "msedge.exe", "title": "poster mockup | Freepik", "url": u1},   # same page, back to back
        {"ts": base + 180, "dur": 300, "app": "msedge.exe", "title": "Claude", "url": u2},
        {"ts": base + 480, "dur": 100, "app": "Photoshop.exe", "title": "x.psd"},                            # not web
        {"ts": base + 580, "dur": 50, "idle": True}]})
    r = c.get("/api/web", headers=A, params={"user": "webby", "start": "2031-07-01", "end": "2031-07-01", "tz": 0}).json()
    assert [p["url"] for p in r["pages"]] == [u2, u1]                       # newest first, FULL urls kept
    assert [p for p in r["pages"] if p["url"] == u1][0]["dur"] == 180       # merged
    assert [(x["name"], x["seconds"], x["visits"]) for x in r["sites"]] == [("Claude", 300, 1), ("Freepik", 180, 1)]
    # main reports use the friendly names and never show the browser for that time
    e = c.get("/api/employee", headers=A, params={"user": "webby", "start": "2031-07-01", "end": "2031-07-01", "tz": 0}).json()
    names = [a["name"] for a in e["apps"]]
    assert names[:3] == ["Claude", "Freepik", "Photoshop"] and "Edge" not in names
    assert [s_["name"] for s_ in e["sites"]] == ["Claude", "Freepik"]
    assert c.get("/api/web", headers=A, params={"start": "2031-01-01", "end": "2031-06-01"}).status_code == 400


def test_agent_status_shows_up_on_devices():
    assert c.post("/api/agent/status", json={"user": "u", "device": "PC-ST", "info": {}}).status_code == 401
    c.post("/api/ingest", headers=K, json={"user": "stu", "device": "PC-ST", "events": [{"ts": time.time(), "dur": 5, "app": "code"}]})
    info = {"version": "0.2.0", "session": 1, "browser_samples": 12, "url_uia": 0, "uia_not_found": 12, "shots_ok": 0,
            "shots_failed": 2, "last_error": "/api/screenshot: HTTPError 502"}
    assert c.post("/api/agent/status", headers=K, json={"user": "stu", "device": "PC-ST", "info": info}).json() == {"ok": True}
    d = [x for x in c.get("/api/devices", headers=A).json() if x["device"] == "PC-ST"][0]
    assert d["status"]["shots_failed"] == 2 and d["status"]["uia_not_found"] == 12
    info["shots_failed"] = 0
    c.post("/api/agent/status", headers=K, json={"user": "stu", "device": "PC-ST", "info": info})   # upsert, not append
    assert [x for x in c.get("/api/devices", headers=A).json() if x["device"] == "PC-ST"][0]["status"]["shots_failed"] == 0


def test_published_build_id_falls_back_to_the_exe_checksum():
    d = os.environ["CYRIL_DIST_DIR"]
    path = os.path.join(d, "manifest.json")
    with open(path, "w") as f:
        f.write('{"version": "0.3.0", "windows_exe": {"url": "/dist/cyril.exe", "sha256": "0123456789abcdef0123"}}')
    assert c.get("/api/install-info", headers=A).json()["build"] == "0123456789ab"      # first 12 of the hash
    with open(path, "w") as f:
        f.write('{"version": "0.3.0", "build": "7feedf0", "windows_exe": {"sha256": "ff"}}')
    assert c.get("/api/install-info", headers=A).json()["build"] == "7feedf0"
    os.remove(path)
    assert c.get("/api/install-info", headers=A).json()["build"] is None


def test_screenshot_intervals_are_a_server_setting_agents_pick_up():
    r = c.put("/api/settings", headers=A, json={"screenshot_every": 30, "screenshot_stable_every": 300}).json()
    assert r["screenshot_every"] == 30 and r["screenshot_stable_every"] == 300
    assert r["report_interval"] == 0                                     # untouched settings stay put
    assert c.put("/api/settings", headers=A, json={"screenshot_every": 7}).status_code == 400
    assert c.get("/api/agent/poll", params={"device": "PC-X"}).status_code == 401
    assert c.get("/api/agent/poll", params={"device": "PC-X"}, headers=K).json()["config"] == {"screenshot_every": 30, "screenshot_stable_every": 300}
    c.put("/api/settings", headers=A, json={"screenshot_every": 60, "screenshot_stable_every": 180})


def test_screenshot_now_round_trip():
    now = time.time()
    c.post("/api/ingest", headers=K, json={"user": "live", "device": "PC-LIVE", "events": [{"ts": now, "dur": 5, "app": "code"}]})
    assert c.post("/api/screenshot-now", json={"device": "PC-LIVE"}).status_code == 401
    assert c.post("/api/screenshot-now", headers=A, json={"device": "NOPE"}).status_code == 404
    cid = c.post("/api/screenshot-now", headers=A, json={"device": "PC-LIVE"}).json()["id"]
    assert c.get(f"/api/screenshot-now/{cid}", headers=A).json()["status"] == "pending"
    # another PC's poll sees nothing; this PC's poll gets the command once, then it is "delivered"
    assert c.get("/api/agent/poll", params={"device": "PC-OTHER"}, headers=K).json()["commands"] == []
    assert c.get("/api/agent/poll", params={"device": "PC-LIVE"}, headers=K).json()["commands"] == [{"id": cid, "kind": "screenshot"}]
    assert c.get("/api/agent/poll", params={"device": "PC-LIVE"}, headers=K).json()["commands"] == []
    assert c.get(f"/api/screenshot-now/{cid}", headers=A).json()["status"] == "delivered"
    # the agent answers by uploading a screenshot that names the command
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0fake").decode()
    sid = c.post("/api/screenshot", headers=K, json={"user": "live", "device": "PC-LIVE", "ts": now, "app": "code",
                                                    "title": "t", "image_b64": jpeg, "command_id": cid}).json()["id"]
    done = c.get(f"/api/screenshot-now/{cid}", headers=A).json()
    assert done["status"] == "done" and done["screenshot_id"] == sid
    # a failed capture is reported back with its reason
    cid2 = c.post("/api/screenshot-now", headers=A, json={"device": "PC-LIVE"}).json()["id"]
    c.get("/api/agent/poll", params={"device": "PC-LIVE"}, headers=K)
    c.post("/api/agent/command-result", headers=K, json={"device": "PC-LIVE", "id": cid2, "ok": False, "error": "screen locked"})
    assert c.get(f"/api/screenshot-now/{cid2}", headers=A).json() == {**c.get(f"/api/screenshot-now/{cid2}", headers=A).json(), "status": "failed", "error": "screen locked"}
    # a PC that never answers expires instead of hanging forever
    cid3 = c.post("/api/screenshot-now", headers=A, json={"device": "PC-LIVE"}).json()["id"]
    with mock.patch("cyril_server.app.time.time", return_value=time.time() + 200):
        assert c.get(f"/api/screenshot-now/{cid3}", headers=A).json()["status"] == "expired"
        assert c.get("/api/agent/poll", params={"device": "PC-LIVE"}, headers=K).json()["commands"] == []   # stale ones are not delivered


def test_delete_employee_removes_everything_about_them_and_only_them():
    now = time.time()
    for u, d in (("gone", "PC-RENTAL"), ("stays", "PC-OWN"), ("gone", "PC-SHARED"), ("stays", "PC-SHARED")):
        c.post("/api/ingest", headers=K, json={"user": u, "device": d, "events": [
            {"ts": now - 700, "dur": 20, "app": "qbittorrent.exe", "title": "t"},      # also raises an alert
            {"ts": now - 600, "dur": 30, "app": "code"}]})
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0fake").decode()
    ids = [c.post("/api/screenshot", headers=K, json={"user": u, "device": d, "ts": now - 650 + i, "app": "code", "title": "t",
                                                      "image_b64": jpeg}).json()["id"] for i, (u, d) in enumerate(
        (("gone", "PC-RENTAL"), ("gone", "PC-RENTAL"), ("stays", "PC-OWN")))]
    c.post("/api/users/rename", headers=A, json={"user": "gone", "name": "Returned Renter"})
    for dev, u in (("PC-RENTAL", "gone"), ("PC-SHARED", "gone"), ("PC-OWN", "stays")):
        c.post("/api/agent/status", headers=K, json={"user": u, "device": dev, "info": {"v": 1}})

    assert c.get("/api/users/delete-preview", params={"user": "gone"}).status_code == 401
    assert c.post("/api/users/delete", json={"user": "gone"}).status_code == 401
    pv = c.get("/api/users/delete-preview", headers=A, params={"user": "gone"}).json()
    assert pv["events"] == 4 and pv["screenshots"] == 2 and pv["alerts"] == 2 and sorted(pv["devices"]) == ["PC-RENTAL", "PC-SHARED"]
    assert pv["still_reporting"] is True        # their PC reported minutes ago: warn to uninstall the agent first

    r = c.post("/api/users/delete", headers=A, json={"user": "gone"}).json()
    assert r == {"deleted": {"events": 4, "screenshots": 2, "alerts": 2}, "failed_screenshots": 0}
    # gone: rows, image files, name; the other employee is untouched
    assert "gone" not in [u["user"] for u in c.get("/api/users", headers=A).json()]
    assert c.get(f"/api/screenshots/{ids[0]}/image", headers=A).status_code == 404
    assert c.get(f"/api/screenshots/{ids[2]}/image", headers=A).status_code == 200
    assert not [a for a in c.get("/api/alerts", headers=A, params={"user": "gone"}).json()]
    assert [a for a in c.get("/api/alerts", headers=A, params={"user": "stays"}).json()]
    devs = {d["device"] for d in c.get("/api/devices", headers=A).json()}
    assert "PC-RENTAL" not in devs and {"PC-OWN", "PC-SHARED"} <= devs        # a shared PC stays (the other login uses it)
    st = {d["device"]: d["status"] for d in c.get("/api/devices", headers=A).json()}
    assert st["PC-OWN"] == {"v": 1} and st["PC-SHARED"] == {"v": 1}
    assert c.post("/api/users/delete", headers=A, json={"user": "gone"}).status_code == 404     # nothing left
    assert c.get("/api/users/delete-preview", headers=A, params={"user": "gone"}).status_code == 404


def test_storage_health_and_the_502_explains_itself():
    assert c.get("/api/storage-health").status_code == 401
    assert c.get("/api/storage-health", headers=A).json() == {"ok": True, "error": "", "hint": ""}
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0fake").decode()
    body = {"user": "u", "device": "d", "ts": time.time(), "app": "a", "title": "t", "image_b64": jpeg}
    from cyril_server import app as appmod
    boom = ObjectStoreError("PUT u/1.jpg -> 500: no free volumes")
    with mock.patch.object(appmod.store, "put", side_effect=boom):
        r = c.post("/api/screenshot", headers=K, json=body)
        assert r.status_code == 502 and "no free volumes" in r.json()["detail"]          # the reason is in the response
        h = c.get("/api/storage-health", headers=A).json()
        assert h["ok"] is False and "no free volumes" in h["error"]


def test_storage_health_explains_a_bad_access_key_and_other_common_failures():
    from cyril_server import app as appmod
    # the exact failure seen in production: an access key containing "/" makes the credential malformed
    with mock.patch.object(appmod.store, "access_key", "ab/cd+ef", create=True):
        h = c.get("/api/storage-health", headers=A).json()
    assert h["ok"] is False and "'/'" in h["hint"] and "letters and digits" in h["hint"]
    with mock.patch.object(appmod.store, "access_key", "", create=True):
        assert "empty" in c.get("/api/storage-health", headers=A).json()["hint"]
    malformed = ObjectStoreError("PUT x -> 400: <Error><Code>AuthorizationQueryParametersError</Code><Message>the Credential is mal-formed")
    for err, word in ((malformed, "malformed"), (ObjectStoreError("PUT x -> 403: SignatureDoesNotMatch"), "secret key"),
                      (ObjectStoreError("PUT x -> 403: InvalidAccessKeyId"), "does not know"),
                      (ObjectStoreError("PUT x -> unreachable: [Errno -2] Name or service not known"), "docker compose ps"),
                      (ObjectStoreError("PUT x -> 500: no free volumes"), "disk space")):
        with mock.patch.object(appmod.store, "access_key", "goodkey", create=True), mock.patch.object(appmod.store, "put", side_effect=err):
            assert word in c.get("/api/storage-health", headers=A).json()["hint"], err


def test_devices_show_whether_an_agent_checks_in_for_requests():
    c.post("/api/ingest", headers=K, json={"user": "polly", "device": "PC-POLL", "events": [{"ts": time.time(), "dur": 5, "app": "code"}]})
    dev = lambda: [d for d in c.get("/api/devices", headers=A).json() if d["device"] == "PC-POLL"][0]
    assert dev()["last_poll"] is None          # an old agent never polls: the dashboard can say so immediately
    c.get("/api/agent/poll", params={"device": "PC-POLL"}, headers=K)
    assert time.time() - dev()["last_poll"] < 5


def test_browser_rule_does_not_flatten_every_website():
    rid = c.post("/api/rules", headers=A, json={"kind": "app", "pattern": "msedge*", "category": "Browsing", "label": "neutral",
                                                 "priority": 100}).json()["id"]          # the user's own rule, outranks the built-ins
    now = time.time()
    c.post("/api/ingest", headers=K, json={"user": "edgy", "device": "d", "events": [
        {"ts": now - 900, "dur": 300, "app": "msedge.exe", "title": "poster | Freepik", "url": "https://www.freepik.com/x"},
        {"ts": now - 600, "dur": 200, "app": "msedge.exe", "title": "feed", "url": "https://www.reddit.com/r/x"},
        {"ts": now - 400, "dur": 100, "app": "msedge.exe", "title": "some page", "url": ""}]})     # address unreadable
    q = {"user": "edgy", "start": _day(), "end": _day(), "tz": 0}
    e = c.get("/api/employee", headers=A, params=q).json()
    assert e["seconds"]["productive"] == 300 and e["seconds"]["unproductive"] == 200 and e["seconds"]["neutral"] == 100
    assert {a["name"]: a["label"] for a in e["sites"]} == {"Freepik": "productive", "Reddit": "unproductive"}
    # tester on the Rules page agrees: site first, browser rule only without an address
    t = lambda **kw: c.post("/api/rules/test", headers=A, json=kw).json()["rule"]
    assert t(app="msedge.exe", domain="reddit.com")["label"] == "unproductive"
    assert t(app="msedge.exe")["label"] == "neutral"
    c.delete(f"/api/rules/{rid}", headers=A)


def test_mac_installer_and_manifest_routes():
    # the installer is personalised like the Windows one
    plain = c.get("/install.sh").text
    assert "{{" not in plain and "SERVER_URL='http://testserver'" in plain and "AGENT_KEY=''" in plain
    keyed = c.get("/install.sh", params={"key": "agent"}, headers={"Host": "cyril.icestudios.in", "X-Forwarded-Proto": "https"}).text
    assert "SERVER_URL='https://cyril.icestudios.in'" in keyed and "AGENT_KEY='agent'" in keyed
    assert c.get("/install.sh", params={"key": "nope"}).status_code == 401
    assert c.get("/install.sh").headers["cache-control"] == "no-store" and plain.isascii()
    assert "launchctl" in plain and "uninstall.sh" in c.get("/install.sh").text
    assert "Nothing is left running" in c.get("/uninstall.sh").text
    # install-info carries the Mac commands and which CPU builds are published
    i = c.get("/api/install-info", headers=A).json()
    assert i["mac_install"] == "curl -fsSL 'http://testserver/install.sh?key=agent' | sudo bash"
    assert i["mac_uninstall"] == "curl -fsSL 'http://testserver/uninstall.sh' | sudo bash"
    assert i["mac_published"] == {"arm64": False, "x86_64": False}
    # plain key=value manifest for the shell installer (no JSON parser on a fresh Mac)
    assert c.get("/api/agent/mac-manifest", params={"arch": "arm64"}).status_code == 404
    assert c.get("/api/agent/mac-manifest", params={"arch": "ppc"}).status_code == 400
    d = os.environ["CYRIL_DIST_DIR"]
    with open(os.path.join(d, "manifest-mac-arm64.json"), "wb") as f:
        f.write(b'\xef\xbb\xbf{"version": "0.3.0", "build": "abc1234", "url": "/dist/cyril-mac-arm64", "sha256": "ff00", "selftest": true}')
    open(os.path.join(d, "cyril-mac-arm64"), "wb").write(b"x")
    m = c.get("/api/agent/mac-manifest", params={"arch": "arm64"})
    assert m.text == "version=0.3.0\nbuild=abc1234\nurl=/dist/cyril-mac-arm64\nsha256=ff00\nselftest=1\n"
    assert c.get("/api/install-info", headers=A).json()["mac_published"] == {"arm64": True, "x86_64": False}
    os.remove(os.path.join(d, "manifest-mac-arm64.json")); os.remove(os.path.join(d, "cyril-mac-arm64"))
