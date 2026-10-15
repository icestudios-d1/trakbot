"""Rule engine: maps (app, domain, title) -> category + productivity label.

Classification happens at query time, so editing a rule re-labels history
(same behaviour as ActivTrak's "productivity labels").
"""
import re
from fnmatch import fnmatch

from .names import is_browser

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


def _applicable(rules: list[dict], ev: dict):
    """The rules that may apply to this event, in the order they are tried.

    Browser windows are special, because the browser itself says nothing about what is being looked at:
    - the website is known (the event has a domain): a rule about the BROWSER ("Edge = neutral") must not decide;
      each site is judged by its own domain/title rules, so one browser rule cannot flatten every site;
    - the address is unreadable (private/incognito windows, or a browser the agent cannot read): the window title is
      the only evidence, so title rules (the private-browsing alert) are tried BEFORE the browser's own rule, and a
      blanket browser rule can never switch incognito flagging off. The browser rule is then the fallback.
    Rules for programs other than a browser are tried in plain priority order."""
    if is_browser(ev.get("app")):
        if ev.get("domain"):
            return [r for r in rules if r["kind"] != "app"]
        return sorted(rules, key=lambda r: r["kind"] != "title")   # stable: priority order kept within each group
    return rules


def classify(ev: dict, rules: list[dict]) -> tuple[str, str]:
    """Return (category, label). `rules` must be sorted by priority desc."""
    if ev.get("idle"):
        return "Idle", "idle"
    for r in _applicable(rules, ev):
        if matches(r, ev):
            return r["category"], r["label"]
    return "Uncategorized", "neutral"


def matching_rule(ev: dict, rules: list[dict]) -> dict | None:
    if ev.get("idle"):
        return None
    for r in _applicable(rules, ev):
        if matches(r, ev):
            return r
    return None
