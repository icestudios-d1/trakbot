"""Sampling + aggregation. Pure logic so it can be unit tested."""
import json
import logging
import logging.handlers
import math
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
    """Append-only on-disk queue so events survive offline periods/restarts.

    Bounded by size (oldest events are discarded first) so a PC that cannot reach the server for weeks can never
    fill its disk. A half-written or corrupt line (power cut, crash mid-write) is skipped, never allowed to wedge
    the queue."""

    def __init__(self, path: str, max_bytes: int = 50_000_000):
        self.path = path
        self.max_bytes = max_bytes

    def push(self, events: list):
        if events:
            with open(self.path, "a+b") as f:   # a torn last line (crash mid-write) must not swallow the next event
                if f.tell():
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
            with open(self.path, "a", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")
            self._trim()

    def take(self, limit: int = 500) -> tuple[list, int]:
        """The oldest `limit` lines: (events that parse, number of lines consumed). Pass the second value to
        drop() once the events are delivered, so corrupt lines are cleared with them."""
        out, n = [], 0
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    n += 1
                    try:
                        e = json.loads(line)
                        if isinstance(e, dict):
                            out.append(e)
                    except ValueError:
                        pass
                    if n >= limit:
                        break
        except FileNotFoundError:
            pass
        return out, n

    def read(self, limit: int = 1000) -> list:
        return self.take(limit)[0]

    def drop(self, n: int):
        self._rewrite(lambda lines: lines[n:])

    def repair(self) -> None:
        """Remove lines that are not valid events (left by a crash mid-write)."""
        def good(lines):
            out = []
            for line in lines:
                try:
                    if isinstance(json.loads(line), dict):
                        out.append(line)
                except ValueError:
                    pass
            return out
        self._rewrite(good)

    def count(self) -> int:
        try:
            with open(self.path, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _trim(self) -> None:
        try:
            if os.path.getsize(self.path) <= self.max_bytes:
                return
        except OSError:
            return

        def keep_newest(lines):
            total, kept = 0, []
            for line in reversed(lines):
                total += len(line.encode("utf-8", "replace"))
                if total > self.max_bytes * 0.8:
                    break
                kept.append(line)
            return kept[::-1]
        self._rewrite(keep_newest)

    def _rewrite(self, fn) -> None:
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                rest = fn(f.readlines())
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(rest)
            os.replace(tmp, self.path)
        except FileNotFoundError:
            pass


# ---- what the server will take ------------------------------------------------------------------------------
MAX_EVENT_SECONDS = 3600   # mirrors the server's limit
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(v, limit: int) -> str:
    """A string the server's database and JSON layer will certainly accept: no control characters (NUL), no lone
    surrogates (a window title cut in the middle of an emoji), bounded length."""
    s = "" if v is None else str(v)
    s = s.encode("utf-8", "replace").decode("utf-8")
    return _CONTROL_RE.sub("", s)[:limit]


def sanitize_event(e: dict) -> dict | None:
    """Make an event safe to queue, so one odd window title or clock jump can never get a whole batch refused.
    Returns None for an event with no usable time."""
    try:
        ts = float(e["ts"])
        dur = float(e.get("dur", 0))
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    if not math.isfinite(dur):
        dur = 0.0
    return {"ts": ts, "dur": min(max(dur, 0.0), float(MAX_EVENT_SECONDS)),
            "app": clean_text(e.get("app"), 256), "title": clean_text(e.get("title"), 1000),
            "url": clean_text(e.get("url"), 2048), "idle": bool(e.get("idle"))}


def classify_status(status: int | None) -> str:
    """How to treat an upload result:
    ok          stored
    transient   server/tunnel/network trouble (no answer, Cloudflare 52x/530, 502-504, 429): keep and retry later
    config      the key or address is wrong (401/403/404/...): keep, retrying will not hurt and fixing it will help
    permanent   the server will never accept this content (400/413/422)
    ambiguous   a bare 500: could be one bad event, could be a broken server"""
    if status is not None and 200 <= status < 300:
        return "ok"
    if status is None or status in (408, 425, 429) or status >= 501:
        return "transient"
    if status in (400, 413, 422):
        return "permanent"
    if status == 500:
        return "ambiguous"
    return "config"


def deliver_events(spool: "Spool", post, batch: int = 500, max_batches: int = 20, max_probes: int = 40) -> dict:
    """Upload queued events, oldest first. `post(events)` returns the HTTP status or None if unreachable.

    - Delivered events are removed from the queue; nothing is removed that the server did not take, EXCEPT a
      single event the server keeps refusing while it accepts the events around it (a poison event). That one is
      discarded and counted, so it can never block everything queued behind it.
    - When a batch is refused for its content, it is split in halves to find the culprit (at most `max_probes`
      requests per call; the rest is finished on the next flush).
    - Outage / tunnel down / wrong key: stop, keep everything, try again next flush.
    Returns {"sent","dropped","status","outcome"}; outcome is "empty", "ok", "partial" or "failed"."""
    res = {"sent": 0, "dropped": 0, "status": None, "outcome": "empty"}
    probes = [max_probes]
    stopped = [False]

    def attempt(evs):
        probes[0] -= 1
        res["status"] = post(evs)
        return classify_status(res["status"])

    for _ in range(max_batches):
        events, lines = spool.take(batch)
        if not lines:
            break
        if lines != len(events):                        # a damaged line in the middle: clear it, then go on
            spool.repair()
            continue
        kind = attempt(events)
        if kind == "ok":
            spool.drop(lines)
            res["sent"] += len(events)
            res["outcome"] = "ok"
            continue
        if kind not in ("permanent", "ambiguous"):
            res["outcome"] = "failed"
            break
        # Refused for its content: find which events the server takes.
        verdict: list = [None] * len(events)            # "ok" | "bad" | "amb" | None (not tried yet)

        def isolate(lo: int, hi: int):
            if hi - lo == 1:
                return
            mid = (lo + hi) // 2
            for a, b in ((lo, mid), (mid, hi)):
                if probes[0] <= 0 or stopped[0]:
                    return
                if b - a == 1:
                    k = attempt(events[a:b])
                    verdict[a] = {"ok": "ok", "permanent": "bad", "ambiguous": "amb"}.get(k)
                    if verdict[a] is None:
                        stopped[0] = True               # outage in the middle: stop here
                        return
                    continue
                k = attempt(events[a:b])
                if k == "ok":
                    verdict[a:b] = ["ok"] * (b - a)
                elif k in ("permanent", "ambiguous"):
                    isolate(a, b)
                else:
                    stopped[0] = True
                    return
        if len(events) == 1:
            verdict[0] = "bad" if kind == "permanent" else "amb"
        else:
            isolate(0, len(events))
        if "ok" in verdict:                             # the server is healthy, so a refused event is the culprit
            verdict = ["bad" if v == "amb" else v for v in verdict]
        done = 0
        while done < len(verdict) and verdict[done] in ("ok", "bad"):
            done += 1
        res["sent"] += verdict[:done].count("ok")
        res["dropped"] += verdict[:done].count("bad")
        if done:
            spool.drop(done if done < len(events) else lines)
            res["outcome"] = "partial"
        if done < len(events) or probes[0] <= 0:
            res["outcome"] = "partial" if done else "failed"
            break
    if res["outcome"] == "partial" and not spool.count():
        res["outcome"] = "ok"
    return res


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


CHROMIUM_BROWSERS = ("msedge", "microsoft edge", "chrome", "chromium", "brave", "opera", "vivaldi")


def is_chromium_browser(app: str) -> bool:
    a = (app or "").lower()
    return any(b in a for b in CHROMIUM_BROWSERS)


def normalize_url(text: str | None) -> str:
    """Turn what the address box shows into a URL, or "" if it is not an address (typed search words, internal
    pages). Chromium hides the scheme when the box is not focused, so a bare "claude.ai/chat" gets https://."""
    t = (text or "").strip()
    if not t or any(c.isspace() for c in t):
        return ""
    if "://" not in t:
        head = t.split("/")[0]
        if t.split(":")[0].lower() in ("about", "edge", "chrome", "brave", "opera", "vivaldi", "view-source", "javascript", "data"):
            return ""
        if "." not in head and ":" not in head:   # one word: a search, not an address (localhost:3000 has a colon)
            return ""
        t = "https://" + t
    if not t.lower().startswith(("http://", "https://")):
        return ""
    return t[:2048]


def read_build(config_path: str | None) -> str:
    """Build id the installer stamped next to the config (build.txt), or ""."""
    if not config_path:
        return ""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(config_path)), "build.txt"), encoding="utf-8-sig") as f:
            return f.read().strip()[:40]
    except OSError:
        return ""


class ShotSpool:
    """Screenshots that could not be uploaded wait on disk and are retried, like activity events do.

    One JSON file per shot, named by its capture time, so a shot uploaded hours later still lands at the right
    moment on the timeline. Bounded (size and age) so a long outage can never fill the disk."""

    DROP_STATUSES = (400, 413, 422)   # the server will never accept these: retrying forever would block the queue

    def __init__(self, folder: str, max_bytes: int = 300_000_000, max_age: float = 7 * 86400):
        self.folder, self.max_bytes, self.max_age = folder, max_bytes, max_age

    def _files(self) -> list[str]:
        try:
            return sorted(os.path.join(self.folder, f) for f in os.listdir(self.folder) if f.endswith(".json"))
        except OSError:
            return []

    def count(self) -> int:
        return len(self._files())

    def push(self, payload: dict) -> None:
        os.makedirs(self.folder, exist_ok=True)
        name = f"{int(payload['ts'] * 1000):016d}.json"
        tmp = os.path.join(self.folder, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, os.path.join(self.folder, name))
        self._trim()

    def _trim(self) -> None:
        import time
        files = self._files()
        sizes = {p: os.path.getsize(p) for p in files if os.path.exists(p)}
        total = sum(sizes.values())
        for p in files:                                   # oldest first
            too_old = time.time() - int(os.path.basename(p)[:16]) / 1000 > self.max_age
            if too_old or total > self.max_bytes:
                total -= sizes.get(p, 0)
                try:
                    os.remove(p)
                except OSError:
                    pass

    def drain(self, post, limit: int = 5) -> int:
        """Try to upload up to `limit` queued shots, oldest first. `post(payload)` returns the HTTP status, or None if
        the server could not be reached. Stops at the first failure (the server is down; try again next time)."""
        sent = 0
        for p in self._files()[:limit]:
            try:
                with open(p, encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, ValueError):
                self._rm(p)
                continue
            status = post(payload)
            if status is not None and (200 <= status < 300 or status in self.DROP_STATUSES):
                self._rm(p)
                sent += status < 300
            else:
                break
        return sent

    @staticmethod
    def _rm(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass
