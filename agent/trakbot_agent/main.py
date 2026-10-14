"""TrakBot agent: samples the foreground app/window + idle state, reports to
the TrakBot server, and takes periodic screenshots of long-running apps.
Config via env vars, a --config JSON file (used by the Windows installer), or flags."""
import argparse
import base64
import getpass
import json
import os
import platform
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import Aggregator, Spool, background_audio, browser_url, is_private
from .platform.active_window import get_active_window, idle_seconds
from .shots import ScreenshotThrottle, capture_jpeg_b64

ext_report = None  # latest {"url","title","at"} from the browser extension


def device_id() -> str:
    """A stable per-machine ID, independent of hostname (which can be
    renamed or duplicated across cloned images)."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                return winreg.QueryValueEx(k, "MachineGuid")[0]
        except Exception:
            pass
    elif sys.platform == "linux":
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                return open(p).read().strip()
            except OSError:
                pass
    elif sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        except Exception:
            pass
    return platform.node()  # fallback: hostname


class ExtHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        global ext_report
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))[:8192])
            audible = [str(h)[:253] for h in (body.get("audible") or [])[:8]]
            ext_report = {"url": str(body["url"])[:2048], "title": str(body.get("title", ""))[:512],
                          "audible": audible, "at": time.time()}
            self.send_response(204)
        except Exception:
            self.send_response(400)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        for h, v in (("Allow-Origin", "*"), ("Allow-Headers", "Content-Type"), ("Allow-Methods", "POST")):
            self.send_header("Access-Control-" + h, v)
        self.end_headers()

    def log_message(self, *a):
        pass


def _post(server: str, path: str, key: str, payload: dict) -> bool:
    req = urllib.request.Request(
        server.rstrip("/") + path,
        json.dumps(payload).encode(),
        {"Content-Type": "application/json", "X-Api-Key": key})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception:
        return False


def send_events(server: str, key: str, user: str, device: str, events: list) -> bool:
    return _post(server, "/api/ingest", key, {"user": user, "device": device, "events": events})


def send_screenshot(server: str, key: str, user: str, device: str, ts: float, app: str, title: str) -> None:
    try:
        img = capture_jpeg_b64()
    except Exception:
        return
    _post(server, "/api/screenshot", key,
          {"user": user, "device": device, "ts": ts, "app": app, "title": title, "image_b64": img})


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="JSON file with server/key/user (written by the Windows installer)")
    ap.add_argument("--server", default=os.environ.get("TRAKBOT_SERVER", "http://localhost:8000"))
    ap.add_argument("--key", default=os.environ.get("TRAKBOT_AGENT_KEY", ""))
    ap.add_argument("--user", default=os.environ.get("TRAKBOT_USER", getpass.getuser()))
    ap.add_argument("--device", default=None)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--idle-after", type=float, default=120.0)
    ap.add_argument("--flush", type=float, default=60.0)
    ap.add_argument("--ext-port", type=int, default=7077)
    ap.add_argument("--screenshot-after", type=float, default=60.0, help="seconds an app must hold the foreground before the first shot")
    ap.add_argument("--screenshot-every", type=float, default=60.0, help="seconds between shots while that app stays foreground")
    ap.add_argument("--no-screenshots", action="store_true")
    ap.add_argument("--spool", default=os.path.expanduser("~/.trakbot-spool.jsonl"))
    a = ap.parse_args()

    cfg = load_config(a.config)
    server = cfg.get("server", a.server)
    key = cfg.get("key", a.key)
    user = cfg.get("user", a.user)
    device = cfg.get("device") or a.device or device_id()

    srv = HTTPServer(("127.0.0.1", a.ext_port), ExtHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    agg, spool, last_flush = Aggregator(), Spool(a.spool), time.time()
    shot_throttle = ScreenshotThrottle(a.screenshot_after, a.screenshot_every)
    while True:
        now = time.time()
        w = get_active_window() or {"app": "", "title": ""}
        idle = idle_seconds() >= a.idle_after
        url = browser_url(w["app"], ext_report, now)
        title = ext_report["title"] if url and ext_report and not w["title"] else w["title"]

        if is_private(title):
            # We can see a private-browsing window is open, but not its content
            # (that's the browser's own privacy boundary) -- don't store the title/URL.
            title, url = "(private/incognito browsing)", ""

        bg_audio = background_audio(ext_report, now)  # recorded even while idle (music left playing)
        agg.add(now, a.interval, "" if idle else w["app"], "" if idle else title, "" if idle else url, idle,
                bg_audio)

        if not a.no_screenshots and shot_throttle.tick(now, w["app"], idle):
            threading.Thread(target=send_screenshot, args=(server, key, user, device, now, w["app"], title), daemon=True).start()

        if now - last_flush >= a.flush:
            spool.push(agg.drain())
            queued = spool.read()
            if queued and send_events(server, key, user, device, queued):
                spool.drop(len(queued))
            last_flush = now
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
