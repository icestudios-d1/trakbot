import os
from cyril_agent.core import Aggregator, Spool, browser_url, is_private, load_config, data_dir, setup_logging, normalize_url, is_chromium_browser, read_build


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


def test_drain_keep_open_false_flushes_the_open_event_too():
    """main.py calls drain(keep_open=False) on every periodic flush. Without
    that, a user who never switches windows (or stays idle) longer than
    --flush never has anything sent at all -- the event just keeps growing
    in memory until it happens to close, which may be hours away or never."""
    a = Aggregator()
    for t in (0, 5, 10):
        a.add(t, 5, "code", "f.py", "", False)  # same window the whole time
    out = a.drain(keep_open=False)
    assert len(out) == 1 and out[0]["dur"] == 15
    assert a.events == []  # nothing left open afterwards

    # the same ongoing activity continues normally in a fresh segment
    a.add(15, 5, "code", "f.py", "", False)
    assert len(a.events) == 1 and a.events[0]["dur"] == 5


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


def test_load_config_accepts_bom_and_plain(tmp_path):
    # Windows PowerShell 5.1 writes a UTF-8 BOM; PowerShell 7 / other tools don't.
    bom, plain = tmp_path / "bom.json", tmp_path / "plain.json"
    bom.write_bytes(b'\xef\xbb\xbf{"server": "https://x", "key": "k", "user": "Priya"}')
    plain.write_text('{"server": "https://x", "key": "k"}', encoding="utf-8")
    assert load_config(str(bom)) == {"server": "https://x", "key": "k", "user": "Priya"}
    assert load_config(str(plain))["server"] == "https://x"
    assert load_config(str(tmp_path / "missing.json")) == {} and load_config(None) == {}


def test_data_dir_next_to_config_else_home(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    assert data_dir(str(cfg)) == str(tmp_path / "data") and (tmp_path / "data").is_dir()
    assert data_dir(None) == os.path.expanduser("~")


def test_setup_logging_writes_file(tmp_path):
    import logging
    logging.getLogger("cyril-agent").handlers.clear()
    path = tmp_path / "agent.log"
    setup_logging(str(path)).info("hello %s", "world")
    for h in logging.getLogger("cyril-agent").handlers:
        h.flush()
    assert "hello world" in path.read_text()


def test_normalize_url_from_the_address_box():
    # Chromium hides the scheme when the box is not focused
    assert normalize_url("claude.ai/chat/abc") == "https://claude.ai/chat/abc"
    assert normalize_url("https://www.freepik.com/search?q=a+b") == "https://www.freepik.com/search?q=a+b"
    assert normalize_url("localhost:3000/x") == "https://localhost:3000/x"
    assert normalize_url("  youtube.com  ") == "https://youtube.com"
    # not addresses: typed searches, internal pages, empty
    for bad in ("", None, "how to make a poster", "poster", "edge://settings", "chrome://flags", "about:blank",
                "view-source:https://a.com", "javascript:alert(1)", "ftp://x.com/a"):
        assert normalize_url(bad) == "", bad


def test_chromium_detection():
    for app in ("msedge.exe", "chrome.exe", "Google-chrome", "brave.exe", "opera.exe", "vivaldi.exe"):
        assert is_chromium_browser(app), app
    for app in ("Photoshop.exe", "firefox.exe", "", "code"):
        assert not is_chromium_browser(app), app


def test_read_build_stamp(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    assert read_build(str(cfg)) == "" and read_build(None) == ""
    (tmp_path / "build.txt").write_bytes(b"\xef\xbb\xbfabc1234def\n")   # tolerate a BOM / trailing newline
    assert read_build(str(cfg)) == "abc1234def"


def test_idle_seconds_survives_tick_count_rollover(monkeypatch):
    """GetTickCount comes back as a signed int from ctypes; past 2**31 ms (~24.8 days) the idle time must stay positive."""
    import sys, types
    from cyril_agent.platform import active_window as aw
    tick = {"v": -2_000_000_000}   # what ctypes returns for 2_294_967_296 ms of uptime
    k32 = types.SimpleNamespace(GetTickCount=lambda: tick["v"])
    def last_input(ref):
        ref._obj.dwTime = 2_294_967_296 - 5000   # last input 5 s ago
        return 1
    u32 = types.SimpleNamespace(GetLastInputInfo=last_input)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(aw.ctypes, "windll", types.SimpleNamespace(kernel32=k32, user32=u32), raising=False)
    assert aw.idle_seconds() == 5.0
