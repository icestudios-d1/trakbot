"""TrakBot agent: samples the foreground app/window + idle state and reports
to the TrakBot server. Config via env vars or flags."""
import argparse
import getpass
import json
import os
import platform
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import Aggregator, Spool, browser_url
from .platform.active_window import get_active_window, idle_seconds

ext_report = None  # latest {"url","title","at"} from the browser extension


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


def send(server: str, key: str, user: str, device: str, events: list) -> bool:
    req = urllib.request.Request(
        server.rstrip("/") + "/api/ingest",
        json.dumps({"user": user, "device": device, "events": events}).encode(),
        {"Content-Type": "application/json", "X-Api-Key": key})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("TRAKBOT_SERVER", "http://localhost:8000"))
    ap.add_argument("--key", default=os.environ.get("TRAKBOT_AGENT_KEY", ""))
    ap.add_argument("--user", default=os.environ.get("TRAKBOT_USER", getpass.getuser()))
    ap.add_argument("--device", default=platform.node())
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--idle-after", type=float, default=120.0)
    ap.add_argument("--flush", type=float, default=60.0)
    ap.add_argument("--ext-port", type=int, default=7077)
    ap.add_argument("--spool", default=os.path.expanduser("~/.trakbot-spool.jsonl"))
    a = ap.parse_args()

    srv = HTTPServer(("127.0.0.1", a.ext_port), ExtHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    agg, spool, last_flush = Aggregator(), Spool(a.spool), time.time()
    while True:
        now = time.time()
        w = get_active_window() or {"app": "", "title": ""}
        idle = idle_seconds() >= a.idle_after
        url = browser_url(w["app"], ext_report, now)
        title = ext_report["title"] if url and ext_report and not w["title"] else w["title"]
        agg.add(now, a.interval, "" if idle else w["app"], "" if idle else title, "" if idle else url, idle)
        if now - last_flush >= a.flush:
            spool.push(agg.drain())
            queued = spool.read()
            if queued and send(a.server, a.key, a.user, a.device, queued):
                spool.drop(len(queued))
            last_flush = now
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
