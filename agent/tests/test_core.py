from cyril_agent.core import Aggregator, Spool, browser_url, is_private


def test_merge_and_drain():
    a = Aggregator()
    for t in (0, 5, 10):
        a.add(t, 5, "code", "f.py", "", False)
    a.add(15, 5, "chrome", "x", "u", False)
    out = a.drain()
    assert len(out) == 1 and out[0]["dur"] == 15 and "_k" not in out[0]
    assert len(a.events) == 1


def test_gap_splits():
    a = Aggregator(gap=10)
    a.add(0, 5, "x", "", "", False)
    a.add(100, 5, "x", "", "", False)
    assert len(a.events) == 2


def test_browser_url():
    r = {"url": "https://a.com", "at": 100}
    assert browser_url("Google-chrome", r, 110) == "https://a.com"
    assert browser_url("code", r, 110) == ""
    assert browser_url("chrome", r, 200) == ""


def test_is_private():
    assert is_private("Reddit - Google Chrome (Incognito)")
    assert is_private("New InPrivate window")
    assert is_private("Private Browsing - Mozilla Firefox")
    assert not is_private("Reddit - Google Chrome")


def test_spool(tmp_path):
    s = Spool(str(tmp_path / "s"))
    s.push([{"a": 1}, {"a": 2}, {"a": 3}])
    assert s.read(2) == [{"a": 1}, {"a": 2}]
    s.drop(2)
    assert s.read() == [{"a": 3}]
