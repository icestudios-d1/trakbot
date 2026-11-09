from cyril_agent import main as m


def _setup(monkeypatch, granted, seen):
    monkeypatch.setattr(m.sys, "platform", "darwin")
    monkeypatch.setitem(m.SCREEN, "content_seen", seen)
    monkeypatch.setitem(m.STATUS, "permissions", {"screen_recording": granted})


def test_restarts_only_when_installed_permission_off_and_long_enough(monkeypatch):
    cfg = "/Library/Application Support/Cyril/config.json"
    _setup(monkeypatch, False, False)
    assert m.should_restart_for_permission(cfg, 0, 601)
    assert not m.should_restart_for_permission(cfg, 0, 100)                # give the person time first
    assert not m.should_restart_for_permission("/tmp/x/config.json", 0, 601)   # run by hand: never
    assert not m.should_restart_for_permission(None, 0, 601)
    _setup(monkeypatch, True, False)
    assert not m.should_restart_for_permission(cfg, 0, 601)
    _setup(monkeypatch, False, True)                                       # a real screenshot came through
    assert not m.should_restart_for_permission(cfg, 0, 601)


def test_real_screenshot_overrides_a_stale_preflight(monkeypatch):
    from cyril_agent.platform import macos
    monkeypatch.setattr(m.sys, "platform", "darwin")
    monkeypatch.setattr(macos, "screen_recording_granted", lambda: False)
    monkeypatch.setitem(m.SCREEN, "content_seen", False)
    m.refresh_permissions()
    assert m.STATUS["permissions"]["screen_recording"] is False
    monkeypatch.setitem(m.SCREEN, "content_seen", True)
    m.refresh_permissions()
    assert m.STATUS["permissions"]["screen_recording"] is True
