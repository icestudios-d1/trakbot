"""Rule engine: maps (app, domain, title) -> category + productivity label.

Classification happens at query time, so editing a rule re-labels history
(same behaviour as ActivTrak's "productivity labels").
"""
import re
from fnmatch import fnmatch

LABELS = ("productive", "unproductive", "neutral")


def matches(rule: dict, ev: dict) -> bool:
    pat = rule["pattern"].lower()
    kind = rule["kind"]
    if kind == "domain":
        d = (ev.get("domain") or "").lower()
        return bool(d) and (d == pat or d.endswith("." + pat))
    if kind == "app":
        return fnmatch((ev.get("app") or "").lower(), pat)
    if kind == "title":
        try:
            return re.search(pat, (ev.get("title") or ""), re.I) is not None
        except re.error:
            return False
    return False


def classify(ev: dict, rules: list[dict]) -> tuple[str, str]:
    """Return (category, label). `rules` must be sorted by priority desc."""
    if ev.get("idle"):
        return "Idle", "idle"
    for r in rules:
        if matches(r, ev):
            return r["category"], r["label"]
    return "Uncategorized", "neutral"


def matching_rule(ev: dict, rules: list[dict]) -> dict | None:
    if ev.get("idle"):
        return None
    for r in rules:
        if matches(r, ev):
            return r
    return None
