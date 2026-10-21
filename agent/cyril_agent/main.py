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
import urllib.error
import urllib.request
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import (Aggregator, ShotSpool, Spool, browser_url, classify_status, data_dir, deliver_events, is_chromium_browser,
                   is_private, load_config, normalize_url, read_build, sanitize_event, setup_logging, write_health)
from .platform.active_window import get_active_window, idle_seconds
from .shots import ScreenshotThrottle, capture_jpeg_b64
from .update import VERSION, check_and_apply

ext_report = None  # latest {"url","title","at"} from the browser extension
log = logging.getLogger("cyril-agent")
HEALTH_PATH: str | None = None   # set in main(): read by the machine-level updater, which fetches a fix if this stays red
SHOT_SPOOL: ShotSpool | None = None   # set in main(): screenshots waiting for the server to come back
LATEST = {"app": "", "title": "", "idle": False}   # what is in front right now (for "screenshot now")

# Health counters sent to the server after every flush, so the dashboard can show WHY websites or screenshots are
# missing on a given PC without anyone reading logs on it.
STATUS = {"version": VERSION, "platform": {"win32": "Windows", "darwin": "Mac"}.get(sys.platform, sys.platform), "session": None, "browser_samples": 0, "url_extension": 0, "url_uia": 0,
          "uia_not_found": 0, "uia_error": "", "shots_ok": 0, "shots_failed": 0, "shots_queued": 0, "last_error": "",
          "events_ok": 0, "events_dropped": 0, "events_queued": 0, "events_last_ok": 0, "events_error": "", "events_failing_since": 0}


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


def _post_status(server: str, path: str, key: str, payload: dict) -> int | None:
    """POST JSON; returns the HTTP status, or None if the server could not be reached at all."""
    url = server.rstrip("/") + path
    req = urllib.request.Request(
        url, json.dumps(payload).encode(),
        {"Content-Type": "application/json", "X-Api-Key": key, "User-Agent": USER_AGENT})
    try:
        status = urllib.request.urlopen(req, timeout=20).status
        if STATUS["last_error"].startswith(path + ":"):   # that problem is over: do not keep reporting it
            STATUS["last_error"] = ""
        return status
    except Exception as e:
        detail = ""
        status = None
        if isinstance(e, urllib.error.HTTPError):   # the server says WHY in the body ("image store error: ...")
            status = e.code
            try:
                detail = " " + e.read().decode(errors="replace")[:260]
            except Exception:
                pass
        STATUS["last_error"] = f"{path}: {e!r}{detail}"[:400]
        log.error("POST %s failed: %r%s", url, e, detail)
        return status if status is not None else None


def _post(server: str, path: str, key: str, payload: dict) -> bool:
    status = _post_status(server, path, key, payload)
    return status is not None and 200 <= status < 300


def send_events(server: str, key: str, user: str, device: str, events: list) -> int | None:
    """HTTP status of the upload, or None if the server could not be reached."""
    return _post_status(server, "/api/ingest", key, {"user": user, "device": device, "events": events})


def flush_events(server: str, key: str, user: str, device: str, agg: Aggregator, spool: Spool, spool_path: str,
                 sent_ok: str | None) -> str | None:
    """Queue what was collected, then upload as much of the queue as the server takes. Never raises: a problem here
    must not stop the sampling loop. Returns the outcome label so the caller logs changes, not every minute."""
    try:
        fresh = [e for e in (sanitize_event(e) for e in agg.drain(keep_open=False)) if e]
        spool.push(fresh)
        res = deliver_events(spool, lambda evs: send_events(server, key, user, device, evs))
    except Exception as e:   # disk full, permissions, ...
        log.exception("could not queue or upload events")
        STATUS["events_error"] = f"{e!r}"[:300]
        return "error"
    STATUS["events_ok"] += res["sent"]
    STATUS["events_dropped"] += res["dropped"]
    STATUS["events_queued"] = spool.count()
    if res["sent"]:
        STATUS["events_last_ok"] = time.time()
    kind = classify_status(res["status"])
    if res["outcome"] == "failed" or (res["outcome"] == "partial" and STATUS["events_queued"]):
        STATUS["events_error"] = f"HTTP {res['status']}" if res["status"] else "server unreachable"
    else:
        STATUS["events_error"] = ""
    if res["dropped"]:
        log.warning("discarded %d event(s) the server keeps refusing (HTTP %s) so the rest are not blocked",
                    res["dropped"], res["status"])
    if STATUS["events_error"]:
        STATUS["events_failing_since"] = STATUS["events_failing_since"] or time.time()
    else:
        STATUS["events_failing_since"] = 0
    if HEALTH_PATH:
        write_health(HEALTH_PATH, time.time(), STATUS["events_failing_since"], STATUS["events_error"])
    label = ("ok" if not STATUS["events_error"] else
             "unreachable" if kind == "transient" else "rejected" if kind in ("permanent", "ambiguous") else "config")
    if label != sent_ok:
        if label == "ok":
            log.info("events: uploading works (%d sent, %d still queued)", res["sent"], STATUS["events_queued"])
        elif label == "unreachable":
            log.warning("events: server not reachable (HTTP %s); %d event(s) queued on disk at %s, retrying every minute",
                        res["status"], STATUS["events_queued"], spool_path)
        elif label == "rejected":
            log.error("events: server refused them (HTTP %s); %d queued at %s", res["status"], STATUS["events_queued"], spool_path)
        else:
            log.error("events: server answered HTTP %s - check the agent key / server address; %d queued",
                      res["status"], STATUS["events_queued"])
    return label


def send_screenshot(server: str, key: str, user: str, device: str, ts: float, app: str, title: str,
                    command_id: int | None = None) -> None:
    try:
        img = capture_jpeg_b64()
    except Exception as e:
        STATUS["shots_failed"] += 1
        STATUS["last_error"] = f"screenshot capture: {e!r}"[:300]
        log.error("screenshot capture failed: %r", e)
        if command_id is not None:
            _post(server, "/api/agent/command-result", key,
                  {"device": device, "id": command_id, "ok": False, "error": f"capture failed: {e!r}"[:400]})
        return
    payload = {"user": user, "device": device, "ts": ts, "app": app, "title": title, "image_b64": img}
    if command_id is not None:
        payload["command_id"] = command_id
    status = _post_status(server, "/api/screenshot", key, payload)
    ok = status is not None and 200 <= status < 300
    STATUS["shots_ok" if ok else "shots_failed"] += 1
    if not ok and command_id is None and SHOT_SPOOL is not None and status not in ShotSpool.DROP_STATUSES:
        # Server down / storage broken: keep the picture and retry, instead of losing that stretch of the day.
        try:
            SHOT_SPOOL.push(payload)
            STATUS["shots_queued"] = SHOT_SPOOL.count()
        except OSError as e:
            log.error("could not queue screenshot on disk: %r", e)


def command_loop(server: str, key: str, user: str, device: str, throttle: ScreenshotThrottle, shots_enabled: bool,
                 every: float = 10.0) -> None:
    """Background thread: ask the server every few seconds whether there is a "screenshot now" request, and pick up
    the screenshot intervals set in the dashboard. Runs apart from the sampling loop so a slow or unreachable server
    can never delay activity tracking."""
    url = server.rstrip("/") + "/api/agent/poll?device=" + quote(device, safe="")
    last_err = None
    while True:
        try:
            req = urllib.request.Request(url, headers={"X-Api-Key": key, "User-Agent": USER_AGENT})
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            if last_err:
                log.info("server poll working again")
                last_err = None
            cfg = data.get("config") or {}
            if cfg.get("screenshot_every"):
                throttle.every = float(cfg["screenshot_every"])
            if cfg.get("screenshot_stable_every"):
                throttle.stable_every = float(cfg["screenshot_stable_every"])
            if SHOT_SPOOL is not None and SHOT_SPOOL.count():   # the server answered: resend what is waiting
                sent = SHOT_SPOOL.drain(lambda pl: _post_status(server, "/api/screenshot", key, pl))
                STATUS["shots_queued"] = SHOT_SPOOL.count()
                if sent:
                    STATUS["shots_ok"] += sent
                    log.info("uploaded %d queued screenshot(s), %d still waiting", sent, STATUS["shots_queued"])
            for cmd in data.get("commands", []):
                if cmd.get("kind") != "screenshot":
                    continue
                if not shots_enabled:
                    _post(server, "/api/agent/command-result", key,
                          {"device": device, "id": cmd["id"], "ok": False, "error": "screenshots are disabled on this PC"})
                    continue
                log.info("screenshot requested from the dashboard (id %s)", cmd["id"])
                send_screenshot(server, key, user, device, time.time(), LATEST["app"], LATEST["title"], cmd["id"])
        except Exception as e:
            if repr(e) != last_err:   # log a change, not every 10 seconds
                log.warning("server poll failed: %r", e)
                last_err = repr(e)
        time.sleep(every)


def read_browser_url(w: dict) -> tuple[str, bool]:
    """(address, is_private) of the page in the foreground browser, read from the browser itself: Windows via UI
    Automation on the address bar, macOS via AppleScript. No browser extension needed."""
    try:
        if sys.platform == "win32":
            if not w.get("hwnd") or not is_chromium_browser(w["app"]):
                return "", False
            from .platform.browser_url import read_address_bar
            raw = read_address_bar(w["hwnd"])
            if raw is None:
                STATUS["uia_not_found"] += 1
                return "", False
            return normalize_url(raw), False
        if sys.platform == "darwin":
            from .platform import macos
            if not macos.is_scriptable_browser(w["app"]):
                return "", False
            url, private = macos.browser_address(w["app"])
            if not url and not private:
                STATUS["uia_not_found"] += 1
            return normalize_url(url), private
    except Exception as e:  # missing dependency, COM/Apple-event trouble, browser hung ...: never stop the agent
        if STATUS["uia_error"] != repr(e)[:200]:
            log.error("reading the browser address failed: %r", e)
        STATUS["uia_error"] = repr(e)[:200]
    return "", False


def is_url_browser(app: str) -> bool:
    """Browsers whose address the agent can read itself."""
    if sys.platform == "darwin":
        from .platform import macos
        return macos.is_scriptable_browser(app)
    return is_chromium_browser(app)


def refresh_permissions() -> None:
    """macOS: report which permissions are granted, so the dashboard can say what to enable."""
    if sys.platform != "darwin":
        return
    from .platform import macos
    STATUS["permissions"] = {"screen_recording": macos.screen_recording_granted()}


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
    ap.add_argument("--screenshot-every", type=float, default=60.0, help="seconds between shots while the app in front keeps changing (the server can override this)")
    ap.add_argument("--screenshot-stable-every", type=float, default=180.0, help="seconds between shots while the same app stays in front (the server can override this)")
    ap.add_argument("--no-screenshots", action="store_true")
    ap.add_argument("--spool", default=None, help="default: <data dir>/spool.jsonl")
    ap.add_argument("--log", default=None, help="default: <data dir>/agent.log")
    ap.add_argument("--selftest", action="store_true", help="exit 0 right away if the program starts (used by windows/build.ps1)")
    ap.add_argument("--update-check-every", type=float, default=3600.0, help="seconds between self-update checks; 0 disables")
    a = ap.parse_args()
    if a.selftest:
        if sys.platform == "darwin":
            try:
                import AppKit, Quartz  # noqa: F401, E401
            except Exception:
                sys.exit(3)
        if sys.platform == "win32":   # the address-bar reader must be inside the build, or websites go missing silently
            try:
                import uiautomation  # noqa: F401
            except Exception:
                sys.exit(3)
        return  # reaching here means every import above worked: that is all a build check needs

    cfg = load_config(a.config)
    server = cfg.get("server", a.server)
    key = cfg.get("key", a.key)
    user = cfg.get("user", a.user)
    device = cfg.get("device") or a.device or device_id()

    ddir = data_dir(a.config)
    spool_path = a.spool or os.path.join(ddir, "spool.jsonl")
    global SHOT_SPOOL, HEALTH_PATH
    HEALTH_PATH = os.path.join(ddir, "health.json")
    SHOT_SPOOL = ShotSpool(os.path.join(ddir, "shots-pending"))
    STATUS["shots_queued"] = SHOT_SPOOL.count()
    setup_logging(a.log or os.path.join(ddir, "agent.log"))

    srv = HTTPServer(("127.0.0.1", a.ext_port), ExtHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    sess = windows_session_id()
    STATUS["session"] = sess
    STATUS["build"] = read_build(a.config)
    log.info("starting: user=%r device=%r server=%r flush_every=%ss session=%s pid=%s",
             user, device, server, a.flush, sess, os.getpid())
    if sess == 0:
        log.warning("running in Windows session 0 (services): this cannot see any user's windows, screen or input. "
                    "The agent must run in the logged-in user's session (scheduled task 'At log on').")
    if not key:
        log.error("no agent key configured: the server will reject every report")
    if sys.platform == "darwin" and not a.selftest:
        from .platform import macos
        refresh_permissions()
        if STATUS.get("permissions", {}).get("screen_recording") is False:
            log.warning("macOS Screen Recording is not granted: screenshots and window titles will be blank until it is "
                        "(System Settings > Privacy & Security > Screen Recording > cyril). Asking now.")
            macos.request_screen_recording()

    agg, spool, last_flush = Aggregator(), Spool(spool_path), time.time()
    no_window = 0
    sent_ok = None
    STATUS["events_queued"] = spool.count()
    shot_throttle = ScreenshotThrottle(a.screenshot_every, a.screenshot_stable_every)
    threading.Thread(target=command_loop, args=(server, key, user, device, shot_throttle, not a.no_screenshots), daemon=True).start()
    last_update_check = time.time()
    while True:
        now = time.time()

        if a.update_check_every and now - last_update_check >= a.update_check_every:
            last_update_check = now
            try:
                check_and_apply(server, key, user, device)  # returns only if no update was applied
            except Exception:
                pass
        try:
            w = get_active_window()
            idle = idle_seconds() >= a.idle_after
        except Exception as e:   # a locked/switching desktop can make the OS calls fail briefly: skip this sample only
            if STATUS["last_error"] != f"sampling: {e!r}"[:400]:
                log.error("sampling failed: %r", e)
            STATUS["last_error"] = f"sampling: {e!r}"[:400]
            time.sleep(a.interval)
            continue
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
        url = browser_url(w["app"], ext_report, now)
        mac_private = False
        if is_url_browser(w["app"]) and not idle and not is_private(w["title"]):
            STATUS["browser_samples"] += 1
            if url:
                STATUS["url_extension"] += 1
            else:
                url, mac_private = read_browser_url(w)   # no extension needed
                if url:
                    STATUS["url_uia"] += 1
        title = ext_report["title"] if url and ext_report and not w["title"] else w["title"]

        private = is_private(title) or mac_private
        if private:
            # We can see a private-browsing window is open, but not its content
            # (that's the browser's own privacy boundary) -- don't store the title/URL.
            title, url = "(private/incognito browsing)", ""

        LATEST.update(app=w["app"], title=title, idle=idle)
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
            sent_ok = flush_events(server, key, user, device, agg, spool, spool_path, sent_ok)
            try:
                refresh_permissions()
            except Exception:
                pass
            _post(server, "/api/agent/status", key, {"user": user, "device": device, "info": dict(STATUS)})
            last_flush = now
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
