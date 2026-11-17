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


def test_flat_frame_is_blank_and_real_content_is_not():
    from PIL import Image, ImageDraw
    from cyril_agent.shots import looks_blank
    assert looks_blank(Image.new("RGB", (1440, 900), (40, 40, 44)))      # what macOS returns without Screen Recording
    img = Image.new("RGB", (1440, 900), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((100, 100, 700, 500), fill=(20, 60, 200))
    assert not looks_blank(img)


def _no_permission_frame(w=2560, h=1440, bar=34):
    """What macOS actually returns without Screen Recording: a wallpaper-tinted menu bar strip on top, the rest
    flat (40,39,44). Seen on a Mac after the Cyril->NetMon hand-over gave it a program it had not allowed yet."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (40, 39, 44))
    d = ImageDraw.Draw(img)
    for x in range(0, w, 40):
        d.rectangle((x, 0, x + 39, bar), fill=(0, 140 + x % 80, 210 - x % 60))   # blue/cyan circles of the wallpaper
    return img


def test_menu_bar_only_frame_is_blank():
    from cyril_agent.shots import looks_blank
    assert looks_blank(_no_permission_frame())
    assert looks_blank(_no_permission_frame(1440, 900, 24))


def test_menu_bar_only_frame_on_two_displays_is_blank():
    from PIL import Image
    from cyril_agent.shots import looks_blank
    img = Image.new("RGB", (2560 + 1920, 1440), (0, 0, 0))   # second display shorter: black filler below it
    img.paste(_no_permission_frame(), (0, 0))
    img.paste(_no_permission_frame(1920, 1080, 25), (2560, 0))
    assert looks_blank(img, [(0, 0, 2560, 1440), (2560, 0, 1920, 1080)])
    img.paste(Image.new("RGB", (500, 300), (250, 250, 250)), (3000, 500))   # a real window on the second display
    assert not looks_blank(img, [(0, 0, 2560, 1440), (2560, 0, 1920, 1080)])


def test_real_screen_with_a_menu_bar_is_not_blank():
    from PIL import ImageDraw
    from cyril_agent.shots import looks_blank
    img = _no_permission_frame()
    ImageDraw.Draw(img).rectangle((200, 300, 1800, 1200), fill=(250, 250, 250))   # any window at all
    assert not looks_blank(img)
    img = _no_permission_frame()
    ImageDraw.Draw(img).rectangle((900, 1360, 1660, 1430), fill=(90, 90, 100))    # just the Dock
    assert not looks_blank(img)
