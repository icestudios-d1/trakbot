"""Report maths for the dashboard: date spans and aggregation."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .classify import LABELS, classify

ALL_LABELS = (*LABELS, "idle")
MAX_SPAN_DAYS = 366

# Categories that count as "hands-on creative tool time" for a design studio.
CREATIVE_PREFIXES = ("Design", "Motion", "Animation", "Video Editing", "Photo", "3D", "UI/UX",
                     "Illustration", "Audio", "CAD", "Colour", "Fonts")


def is_creative(category: str) -> bool:
    return category.startswith(CREATIVE_PREFIXES)


def tzinfo_for(tz: int | None):
    """`tz` is minutes east of UTC as reported by the browser; None = server-local time."""
    return timezone(timedelta(minutes=tz)) if tz is not None else None


def parse_day(s: str | None, tz: int | None, default: datetime | None = None) -> datetime:
    if s:
        d = datetime.strptime(s, "%Y-%m-%d")
    elif default is not None:
        return default
    else:
        d = datetime.now(tzinfo_for(tz)).replace(tzinfo=None)
        d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return d


def span(start: str | None, end: str | None, tz: int | None):
    """Inclusive local-date range -> (start_ts, end_ts, [day strings]). Defaults to today."""
    s = parse_day(start, tz)
    e = parse_day(end, tz, default=s)
    if e < s:
        s, e = e, s
    n = (e - s).days + 1
    if n > MAX_SPAN_DAYS:
        raise ValueError(f"range too long (max {MAX_SPAN_DAYS} days)")
    zi = tzinfo_for(tz)
    start_ts = s.replace(tzinfo=zi).timestamp()
    end_ts = (e + timedelta(days=1)).replace(tzinfo=zi).timestamp()
    return start_ts, end_ts, [(s + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def previous_span(days: list[str]):
    s = datetime.strptime(days[0], "%Y-%m-%d")
    n = len(days)
    return (s - timedelta(days=n)).strftime("%Y-%m-%d"), (s - timedelta(days=1)).strftime("%Y-%m-%d")


def _top(d: dict, n: int, meta: dict | None = None):
    out = []
    for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]:
        row = {"name": k, "seconds": round(v)}
        if meta and k in meta:
            row["category"], row["label"] = meta[k]
        out.append(row)
    return out


def aggregate(events: list[dict], rules: list[dict], tz: int | None, days: list[str], start_ts: float,
              top_n: int = 12):
    zi = tzinfo_for(tz)
    labels, cats = defaultdict(float), defaultdict(float)
    apps, sites, creative = defaultdict(float), defaultdict(float), defaultdict(float)
    meta_app, meta_site = {}, {}
    hourly = [defaultdict(float) for _ in range(24)]
    heat = [[0.0] * 24 for _ in range(7)]
    idx = {d: i for i, d in enumerate(days)}
    daily = [{"day": d, **{k: 0.0 for k in ALL_LABELS}, "first": None, "last": None} for d in days]
    per_user = defaultdict(lambda: {"seconds": defaultdict(float), "apps": defaultdict(float), "last": 0})
    for e in events:
        cat, lab = classify(e, rules)
        dur = e["dur"]
        dt = datetime.fromtimestamp(e["ts"], zi)
        labels[lab] += dur
        cats[cat] += dur
        hourly[dt.hour][lab] += dur
        pu = per_user[e["user"]]
        pu["seconds"][lab] += dur
        pu["last"] = max(pu["last"], e["ts"] + dur)
        i = idx.get(dt.strftime("%Y-%m-%d"))
        if i is not None:
            daily[i][lab] += dur
        if lab != "idle":
            heat[dt.weekday()][dt.hour] += dur
            name = e["app"] or "unknown"
            apps[name] += dur
            meta_app.setdefault(name, (cat, lab))
            pu["apps"][name] += dur
            if e["domain"]:
                sites[e["domain"]] += dur
                meta_site.setdefault(e["domain"], (cat, lab))
            if is_creative(cat):
                creative[name] += dur
            if i is not None:
                d = daily[i]
                d["first"] = e["ts"] if d["first"] is None else min(d["first"], e["ts"])
                d["last"] = max(d["last"] or 0, e["ts"] + dur)
    active = sum(labels.get(k, 0) for k in LABELS)
    prod = labels.get("productive", 0)
    for d in daily:
        a = d["productive"] + d["unproductive"] + d["neutral"]
        d["pct"] = round(100 * d["productive"] / a, 1) if a else None
        for k in ALL_LABELS:
            d[k] = round(d[k])
    return {
        "seconds": {k: round(labels.get(k, 0)) for k in ALL_LABELS},
        "active_seconds": round(active),
        "productivity_pct": round(100 * prod / active, 1) if active else None,
        "creative_seconds": round(sum(creative.values())),
        "creative_apps": _top(creative, 8),
        "categories": _top(cats, 15),
        "apps": _top(apps, top_n, meta_app),
        "sites": _top(sites, top_n, meta_site),
        "hourly": [{k: round(h.get(k, 0)) for k in ALL_LABELS} for h in hourly],
        "heatmap": [[round(v) for v in row] for row in heat],
        "daily": daily,
        "by_user": {u: {
            **{k: round(m["seconds"].get(k, 0)) for k in ALL_LABELS},
            "top_app": max(m["apps"], key=m["apps"].get) if m["apps"] else None,
            "last_seen": m["last"]} for u, m in per_user.items()},
    }
