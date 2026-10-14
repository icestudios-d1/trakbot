from cyril_agent.shots import ScreenshotThrottle


def test_normal_app_waits_then_repeats():
    t = ScreenshotThrottle(after=60, every=180)
    assert not t.tick(0, "ps", False) and not t.tick(59, "ps", False)
    assert t.tick(60, "ps", False) and not t.tick(120, "ps", False) and t.tick(240, "ps", False)


def test_private_window_gets_quicker_first_shot_and_tighter_cadence():
    t = ScreenshotThrottle(after=60, every=180, private_after=10, private_every=60)
    assert not t.tick(0, "chrome", False, True) and t.tick(10, "chrome", False, True)
    assert not t.tick(40, "chrome", False, True) and t.tick(70, "chrome", False, True)


def test_switching_between_normal_and_private_restarts_the_clock():
    t = ScreenshotThrottle(after=60, every=180, private_after=10)
    assert not t.tick(0, "chrome", False, False)
    assert not t.tick(5, "chrome", False, True)    # private window opened in the same browser
    assert t.tick(15, "chrome", False, True)


def test_idle_or_no_app_never_shoots():
    t = ScreenshotThrottle(after=1, every=1)
    assert not t.tick(100, "ps", True) and not t.tick(200, "", False)
