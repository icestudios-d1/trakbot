"""Sampling + aggregation. Pure logic so it can be unit tested."""
import json
import logging
import logging.handlers
import os
import re
import time
from dataclasses import dataclass, field

BROWSERS = ("chrome", "chromium", "firefox", "msedge", "edge", "brave", "opera", "vivaldi", "safari", "arc")

# Browsers put this in the window title in private mode; the extension never
# gets a URL out of such a window (that's the browser's own privacy boundary,
# and we don't try to work around it — see shots.py / README).
PRIVATE_TITLE_RE = re.compile(r"\(incognito\)|\(inprivate\)|private browsing|private window|private/incognito", re.I)


def is_private(title: str) -> bool:
    return bool(PRIVATE_TITLE_RE.search(title or ""))


@dataclass
class Aggregator:
    """Merges consecutive identical samples into single events."""
    gap: float = 30.0
    events: list = field(default_factory=list)

    def add(self, now: float, interval: float, app: str, title: str, url: str, idle: bool):
        key = (app, title, url, idle)
        last = self.events[-1] if self.events else None
        if last and last["_k"] == key and now - (last["ts"] + last["dur"]) <= self.gap:
            last["dur"] = now + interval - last["ts"]
        else:
            self.events.append({"_k": key, "ts": now, "dur": interval, "app": app,
                                "title": title, "url": url, "idle": idle})

    def drain(self, keep_open: bool = True) -> list:
        """Return completed events; keep the newest open for further merging."""
        out = self.events[:-1] if keep_open else self.events
        self.events = self.events[-1:] if keep_open else []
        return [{k: v for k, v in e.items() if k != "_k"} for e in out]


def browser_url(app: str, ext_report: dict | None, now: float, max_age: float = 20.0) -> str:
    if ext_report and any(b in app.lower() for b in BROWSERS) and now - ext_report["at"] <= max_age:
        return ext_report["url"]
    return ""


class Spool:
    """Append-only on-disk queue so events survive offline periods/restarts."""
    def __init__(self, path: str):
        self.path = path

    def push(self, events: list):
        if events:
            with open(self.path, "a") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")

    def read(self, limit: int = 1000) -> list:
        try:
            with open(self.path) as f:
                return [json.loads(l) for l in f.readlines()[:limit]]
        except FileNotFoundError:
            return []

    def drop(self, n: int):
        try:
            with open(self.path) as f:
                rest = f.readlines()[n:]
            with open(self.path, "w") as f:
                f.writelines(rest)
        except FileNotFoundError:
            pass


def load_config(path: str | None) -> dict:
    """Read the installer's JSON config. `utf-8-sig` because Windows PowerShell 5.1 writes a BOM
    with `Set-Content -Encoding UTF8`, which plain json.load rejects (the agent then never started)."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except OSError:
        return {}


def data_dir(config_path: str | None) -> str:
    """Where spool + log live. Next to the installer's config (ProgramData\\Cyril\\data, writable by the
    logged-in user the agent runs as); otherwise the home directory (running by hand)."""
    if config_path:
        d = os.path.join(os.path.dirname(os.path.abspath(config_path)), "data")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            pass
    return os.path.expanduser("~")


def setup_logging(log_path: str) -> logging.Logger:
    """Rotating file log (the Windows build has no console, so without this every failure is invisible)."""
    log = logging.getLogger("cyril-agent")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        try:
            fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            pass
        import sys
        if sys.stderr:  # None in a --noconsole build
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            log.addHandler(sh)
    return log
