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


def make_classifier(rules: list[dict]):
    """A fast `classify` for one pass over many events (a report). It gives exactly the same answers as classify()
    (the tests compare them), but a day has thousands of events and only a few hundred distinct programs and sites,
    so each rule kind is looked up once per distinct value instead of once per event:
      - app rules are tried once per distinct program name,
      - domain rules once per distinct website,
      - title rules (regexes) once per distinct window title, and only the title rules.
    The first match by priority among the three wins, with the browser exceptions described in _applicable()."""
    app_rules = [(i, r, r["pattern"].lower()) for i, r in enumerate(rules) if r["kind"] == "app"]
    dom_rules = [(i, r, r["pattern"].lower()) for i, r in enumerate(rules) if r["kind"] == "domain"]
    title_rules = []
    for i, r in enumerate(rules):
        if r["kind"] == "title":
            try:
                title_rules.append((i, r, re.compile(r["pattern"].lower(), re.I)))
            except re.error:
                pass                       # a broken pattern matches nothing, as in matches()
    app_c: dict = {}
    dom_c: dict = {}
    title_c: dict = {}

    def by_app(app):
        try:
            return app_c[app]
        except KeyError:
            a = (app or "").lower()
            app_c[app] = hit = next(((i, r) for i, r, pat in app_rules if fnmatch(a, pat)), None)
            return hit

    def by_domain(domain):
        try:
            return dom_c[domain]
        except KeyError:
            d = (domain or "").lower()
            dom_c[domain] = hit = (next(((i, r) for i, r, pat in dom_rules if d == pat or d.endswith("." + pat)), None) if d else None)
            return hit

    def by_title(title):
        try:
            return title_c[title]
        except KeyError:
            if len(title_c) > 200_000:
                title_c.clear()
            t = title or ""
            title_c[title] = hit = next(((i, r) for i, r, rx in title_rules if rx.search(t)), None)
            return hit

    def rule_for(ev: dict):
        if ev.get("idle"):
            return None
        domain = ev.get("domain")
        a, d, t = by_app(ev.get("app")), by_domain(domain), by_title(ev.get("title")) if title_rules else None
        if is_browser(ev.get("app")):
            if domain:
                found = [x for x in (d, t) if x]          # a browser rule never decides when the site is known
            else:
                found = [t] if t else [x for x in (a, d) if x]   # title rules (private browsing) before the browser rule
        else:
            found = [x for x in (a, d, t) if x]
        return min(found, key=lambda x: x[0])[1] if found else None

    def clf(ev: dict) -> tuple[str, str]:
        if ev.get("idle"):
            return "Idle", "idle"
        r = rule_for(ev)
        return (r["category"], r["label"]) if r else ("Uncategorized", "neutral")

    clf.rule_for = rule_for      # the matching rule itself (None = nothing matches)
    return clf
