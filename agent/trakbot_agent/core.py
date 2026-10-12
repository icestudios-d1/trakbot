"""Sampling + aggregation. Pure logic so it can be unit tested."""
import json
import time
from dataclasses import dataclass, field

BROWSERS = ("chrome", "chromium", "firefox", "msedge", "edge", "brave", "opera", "vivaldi", "safari", "arc")


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
