from cyril_server.classify import classify, matching_rule

R = [  # already in priority order, highest first: the user's own "Edge = neutral" outranks the built-in site rules
    {"id": 1, "kind": "app", "pattern": "msedge*", "category": "Browsing", "label": "neutral", "alert": 0, "priority": 100},
    {"id": 2, "kind": "domain", "pattern": "freepik.com", "category": "Stock & Assets", "label": "productive", "alert": 0, "priority": 10},
    {"id": 3, "kind": "domain", "pattern": "facebook.com", "category": "Social Media", "label": "unproductive", "alert": 1, "priority": 0},
    {"id": 4, "kind": "title", "pattern": r"\(incognito\)|private browsing", "category": "Private browsing", "label": "unproductive", "alert": 1, "priority": 0},
    {"id": 5, "kind": "app", "pattern": "photoshop*", "category": "Design", "label": "productive", "alert": 0, "priority": 0},
]


def ev(app, domain="", title="", idle=False):
    return {"app": app, "domain": domain, "title": title, "idle": idle}


def test_a_known_website_is_judged_by_its_own_rule_not_the_browsers():
    assert classify(ev("msedge.exe", "www.freepik.com"), R) == ("Stock & Assets", "productive")
    assert classify(ev("msedge.exe", "facebook.com"), R) == ("Social Media", "unproductive")
    assert matching_rule(ev("msedge.exe", "facebook.com"), R)["alert"] == 1      # alerts follow the site too


def test_the_browsers_rule_is_the_fallback_when_the_address_is_unreadable():
    assert classify(ev("msedge.exe", ""), R) == ("Browsing", "neutral")           # e.g. an incognito window


def test_a_site_with_no_rule_does_not_inherit_the_browsers_label():
    assert classify(ev("msedge.exe", "some-unknown-site.io"), R) == ("Uncategorized", "neutral")
    only_browser = [R[0]]
    assert classify(ev("msedge.exe", "claude.ai"), only_browser) == ("Uncategorized", "neutral")
    assert matching_rule(ev("msedge.exe", "claude.ai"), only_browser) is None     # shows up under "Needs a label"


def test_incognito_stays_flagged_even_though_the_browser_has_a_higher_priority_rule():
    # address unreadable -> the title is the only evidence, so the private-browsing rule is tried before "Edge = neutral"
    assert classify(ev("msedge.exe", "", "x (InCognito)"), R) == ("Private browsing", "unproductive")
    assert matching_rule(ev("msedge.exe", "", "x (InCognito)"), R)["alert"] == 1
    assert classify(ev("msedge.exe", "", "just a normal page"), R) == ("Browsing", "neutral")   # fallback unchanged


def test_rules_for_other_programs_are_unaffected():
    assert classify(ev("Photoshop.exe"), R) == ("Design", "productive")
    assert classify(ev("Photoshop.exe", "", "", True), R) == ("Idle", "idle")
