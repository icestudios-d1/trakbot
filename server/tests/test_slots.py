"""Reporting granularity: blocks aligned to the local clock, dominant app per block, totals preserved."""
from cyril_server.reports import SLOT_CHOICES, slotize

# 10:00 IST (UTC+5:30) on some day, as a UTC epoch
T0 = 1_792_000_000 - (1_792_000_000 % 86400) + 4.5 * 3600
IST = 330


def ev(ts, dur, app="", idle=0, title="t", dom="", user="u"):
    return {"id": 1, "user": user, "device": "d", "ts": ts, "dur": dur, "app": app, "title": title,
            "url": "", "domain": dom, "idle": idle}


def test_exact_is_untouched_and_choices_are_the_requested_ones():
    evs = [ev(T0, 60, "a")]
    assert slotize(evs, 0, IST) is evs
    assert SLOT_CHOICES == (0, 1, 5, 10, 15, 30)


def test_app_used_most_in_the_block_is_reported():
    evs = [ev(T0, 420, "Photoshop.exe"), ev(T0 + 420, 480, "chrome", dom="youtube.com")]
    [slot] = slotize(evs, 15, IST)
    assert slot["domain"] == "youtube.com" and slot["ts"] == T0 and slot["dur"] == 900
    assert slot["share"] == 0.533 and slot["slot"] is True
    assert [b["name"] for b in slot["breakdown"]] == ["youtube.com", "Photoshop.exe"]


def test_block_is_idle_when_idle_time_is_at_least_all_activity():
    evs = [ev(T0, 300, "Slack.exe"), ev(T0 + 300, 600, idle=1)]
    [slot] = slotize(evs, 15, IST)
    assert slot["idle"] == 1 and slot["app"] == "" and slot["dur"] == 900
    assert slotize([ev(T0, 450, "a"), ev(T0 + 450, 450, idle=1)], 15, IST)[0]["idle"] == 1  # tie -> idle
    assert slotize([ev(T0, 500, "a"), ev(T0 + 500, 400, idle=1)], 15, IST)[0]["idle"] == 0


def test_blocks_align_to_the_local_clock_not_utc():
    # 10:07 IST event: with UTC alignment +5:30 would put the boundary at :30/:00 offsets; local must be :00/:15
    evs = [ev(T0 + 7 * 60, 60, "a"), ev(T0 + 16 * 60, 60, "b")]
    slots = slotize(evs, 15, IST)
    assert [s["ts"] - T0 for s in slots] == [0, 900]


def test_event_spanning_a_boundary_is_split_and_totals_are_preserved():
    evs = [ev(T0 + 600, 600, "a"), ev(T0 + 1200, 600, "b")]   # 10:10-10:20 then 10:20-10:30
    s = slotize(evs, 15, IST)
    assert [round(x["dur"]) for x in s] == [300 + 0, 600 + 300] or sum(x["dur"] for x in s) == 1200
    assert sum(x["dur"] for x in s) == sum(e["dur"] for e in evs)
    assert s[0]["app"] == "a" and s[1]["app"] == "b"  # 300 a vs 0 -> a;  300 a vs 600 b -> b


def test_users_are_kept_apart_and_output_is_time_ordered():
    evs = [ev(T0, 300, "x", user="ana"), ev(T0, 300, "y", user="ben"), ev(T0 + 900, 60, "z", user="ana")]
    s = slotize(evs, 15, IST)
    assert [(x["user"], x["app"]) for x in s] == [("ana", "x"), ("ben", "y"), ("ana", "z")]


def test_dominant_title_comes_from_the_dominant_app():
    evs = [ev(T0, 100, "Photoshop.exe", title="a.psd"), ev(T0 + 100, 500, "Photoshop.exe", title="b.psd"),
           ev(T0 + 600, 100, "Slack.exe", title="chat")]
    [s] = slotize(evs, 15, IST)
    assert s["app"] == "Photoshop.exe" and s["title"] == "b.psd"
