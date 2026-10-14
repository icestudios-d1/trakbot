"""Cyril agent: samples the foreground app/window + idle state, reports to
the Cyril server, and takes periodic screenshots of long-running apps.
Config via env vars, a --config JSON file (used by the Windows installer), or flags."""
import argparse
import base64
import getpass
import json
import logging
import os
import platform
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import Aggregator, Spool, browser_url, data_dir, is_private, load_config, setup_logging
from .platform.active_window import get_active_window, idle_seconds
from .shots import ScreenshotThrottle, capture_jpeg_b64
from .update import check_and_apply

ext_report = None  # latest {"url","title","at"} from the browser extension
log = logging.getLogger("cyril-agent")


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
            ext_report = {"url": str(body["url"])[:2048], "title": str(body.get("title", ""))[:512], "at": time.time()}
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


USER_AGENT = "CyrilAgent/1.0"


def _post(server: str, path: str, key: str, payload: dict) -> bool:
    url = server.rstrip("/") + path
    req = urllib.request.Request(
        url, json.dumps(payload).encode(),
        {"Content-Type": "application/json", "X-Api-Key": key, "User-Agent": USER_AGENT})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:
        log.error("POST %s failed: %r", url, e)
        return False


def send_events(server: str, key: str, user: str, device: str, events: list) -> bool:
    return _post(server, "/api/ingest", key, {"user": user, "device": device, "events": events})


def send_screenshot(server: str, key: str, user: str, device: str, ts: float, app: str, title: str) -> None:
    try:
        img = capture_jpeg_b64()
    except Exception as e:
        log.error("screenshot capture failed: %r", e)
        return
    _post(server, "/api/screenshot", key,
          {"user": user, "device": device, "ts": ts, "app": app, "title": title, "image_b64": img})


def windows_session_id() -> int | None:
    """Windows session this process runs in. 0 = the non-interactive services session, which has no desktop:
    no foreground window, no screen to capture, no keyboard/mouse input to measure."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        sid = ctypes.c_ulong()
        ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
        return sid.value
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="JSON file with server/key/user (written by the Windows installer)")
    ap.add_argument("--server", default=os.environ.get("CYRIL_SERVER", "http://localhost:8000"))
    ap.add_argument("--key", default=os.environ.get("CYRIL_AGENT_KEY", ""))
    ap.add_argument("--user", default=os.environ.get("CYRIL_USER", getpass.getuser()))
    ap.add_argument("--device", default=None)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--idle-after", type=float, default=120.0)
    ap.add_argument("--flush", type=float, default=60.0)
    ap.add_argument("--ext-port", type=int, default=7077)
    ap.add_argument("--screenshot-after", type=float, default=60.0, help="seconds an app must hold the foreground before the first shot")
    ap.add_argument("--screenshot-every", type=float, default=180.0, help="seconds between shots while that app stays foreground")
    ap.add_argument("--no-screenshots", action="store_true")
    ap.add_argument("--spool", default=None, help="default: <data dir>/spool.jsonl")
    ap.add_argument("--log", default=None, help="default: <data dir>/agent.log")
    ap.add_argument("--update-check-every", type=float, default=3600.0, help="seconds between self-update checks; 0 disables")
    a = ap.parse_args()

    cfg = load_config(a.config)
    server = cfg.get("server", a.server)
    key = cfg.get("key", a.key)
    user = cfg.get("user", a.user)
    device = cfg.get("device") or a.device or device_id()

    ddir = data_dir(a.config)
    spool_path = a.spool or os.path.join(ddir, "spool.jsonl")
    setup_logging(a.log or os.path.join(ddir, "agent.log"))

    srv = HTTPServer(("127.0.0.1", a.ext_port), ExtHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    sess = windows_session_id()
    log.info("starting: user=%r device=%r server=%r flush_every=%ss session=%s pid=%s",
             user, device, server, a.flush, sess, os.getpid())
    if sess == 0:
        log.warning("running in Windows session 0 (services): this cannot see any user's windows, screen or input. "
                    "The agent must run in the logged-in user's session (scheduled task 'At log on').")
    if not key:
        log.error("no agent key configured: the server will reject every report")

    agg, spool, last_flush = Aggregator(), Spool(spool_path), time.time()
    no_window = 0
    sent_ok = None
    shot_throttle = ScreenshotThrottle(a.screenshot_after, a.screenshot_every)
    last_update_check = time.time()
    while True:
        now = time.time()

        if a.update_check_every and now - last_update_check >= a.update_check_every:
            last_update_check = now
            try:
                check_and_apply(server, key, user, device)  # returns only if no update was applied
            except Exception:
                pass
        w = get_active_window()
        if w is None:
            no_window += 1
            if no_window == 12:  # a minute of samples with no foreground window
                log.warning("no foreground window visible for 1 minute (session=%s). Normal if the screen is locked or "
                            "nobody is logged in; otherwise the agent is not running inside the user's desktop.", sess)
        else:
            if no_window >= 12:
                log.info("foreground window visible again")
            no_window = 0
        w = w or {"app": "", "title": ""}
        idle = idle_seconds() >= a.idle_after
        url = browser_url(w["app"], ext_report, now)
        title = ext_report["title"] if url and ext_report and not w["title"] else w["title"]

        private = is_private(title)
        if private:
            # We can see a private-browsing window is open, but not its content
            # (that's the browser's own privacy boundary) -- don't store the title/URL.
            title, url = "(private/incognito browsing)", ""

        agg.add(now, a.interval, "" if idle else w["app"], "" if idle else title, "" if idle else url, idle)

        if not a.no_screenshots and shot_throttle.tick(now, w["app"], idle, private):
            threading.Thread(target=send_screenshot, args=(server, key, user, device, now, w["app"], title), daemon=True).start()

        if now - last_flush >= a.flush:
            # keep_open=False: force-close whatever's currently open too, not
            # just already-completed events. Without this, staying on one
            # window (or staying idle) for longer than --flush means nothing
            # is ever sent -- the open event just keeps growing in memory
            # until it eventually closes (a window switch or idle toggle),
            # which may be hours away, or never, for a single long session.
            # The next add() simply starts a fresh segment of the same
            # activity, so a long session is reported in --flush-sized
            # chunks instead of only once it ends.
            spool.push(agg.drain(keep_open=False))
            queued = spool.read()
            if queued:
                ok = send_events(server, key, user, device, queued)
                if ok:
                    spool.drop(len(queued))
                if ok != sent_ok:  # log changes (first success, outage start, recovery), not every minute
                    log.info("report %s: %d event(s)%s", "sent" if ok else "FAILED, will retry (spooled)", len(queued),
                             "" if ok else f" queued on disk at {spool_path}")
                    sent_ok = ok
            last_flush = now
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
