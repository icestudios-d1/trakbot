from cyril_agent.shots import ScreenshotThrottle


def run(t, plan):
    """plan: list of (second, app). Returns the seconds at which a shot is taken."""
    return [sec for sec, app in plan if t.tick(sec, app, False)]


def test_first_shot_is_immediate():
    assert ScreenshotThrottle().tick(0, "ps", False)


def test_same_app_in_front_slows_to_every_three_minutes():
    t = ScreenshotThrottle(every=60, stable_every=180)
    shots = run(t, [(s, "Photoshop.exe") for s in range(0, 901, 5)])
    assert shots == [0, 180, 360, 540, 720, 900]


def test_switching_apps_keeps_it_at_once_a_minute():
    t = ScreenshotThrottle(every=60, stable_every=180)
    plan = [(s, "edge" if (s // 20) % 2 else "photoshop") for s in range(0, 301, 5)]   # switch every 20 s
    assert run(t, plan) == [0, 60, 120, 180, 240, 300]


def test_one_switch_gets_the_minute_cadence_once_then_it_settles():
    t = ScreenshotThrottle(every=60, stable_every=180)
    plan = [(s, "a") for s in range(0, 100, 5)] + [(s, "b") for s in range(100, 700, 5)]
    shots = run(t, plan)
    assert shots[0] == 0 and 100 <= shots[1] <= 180   # the switch is noticed within the next minute, not 3
    assert [b - a for a, b in zip(shots[1:], shots[2:])] == [180] * (len(shots) - 2)   # then every 3 minutes


def test_private_window_gets_quicker_first_shot_and_tighter_cadence_even_when_stable():
    t = ScreenshotThrottle(every=60, stable_every=180, private_after=10, private_every=60)
    assert not t.tick(0, "chrome", False, True) and t.tick(10, "chrome", False, True)
    assert not t.tick(40, "chrome", False, True) and t.tick(70, "chrome", False, True) and t.tick(130, "chrome", False, True)


def test_idle_or_no_app_never_shoots_and_coming_back_counts_as_a_change():
    t = ScreenshotThrottle(every=60, stable_every=180)
    assert t.tick(0, "ps", False)
    assert not t.tick(100, "ps", True) and not t.tick(150, "", False)
    assert t.tick(200, "ps", False)          # back from idle: a change and >1 min since the last shot -> shoot now
    assert not t.tick(260, "ps", False)      # same app again: now on the 3-minute cadence
    assert t.tick(380, "ps", False)


def test_settings_can_be_changed_live():
    t = ScreenshotThrottle(every=60, stable_every=180)
    t.tick(0, "ps", False)
    t.stable_every = 90      # the server can tell agents to use different intervals
    assert t.tick(90, "ps", False)
