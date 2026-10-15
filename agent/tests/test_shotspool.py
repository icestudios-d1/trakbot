import json
import os
import time

from cyril_agent.core import ShotSpool


def shot(ts, n=100):
    return {"user": "u", "device": "d", "ts": ts, "app": "a", "title": "t", "image_b64": "x" * n}


def test_queued_shots_survive_and_upload_oldest_first(tmp_path):
    sp = ShotSpool(str(tmp_path / "q"))
    now = time.time()
    for ts in (now - 30, now - 90, now - 60):
        sp.push(shot(ts))
    assert sp.count() == 3
    seen = []
    assert sp.drain(lambda pl: seen.append(pl["ts"]) or 200) == 3
    assert seen == sorted(seen) and sp.count() == 0          # oldest first, original capture times preserved


def test_a_down_server_keeps_everything_and_stops_early(tmp_path):
    sp = ShotSpool(str(tmp_path / "q"))
    now = time.time()
    for i in range(4):
        sp.push(shot(now - 100 + i))
    calls = []
    assert sp.drain(lambda pl: calls.append(1) or None) == 0     # unreachable
    assert len(calls) == 1 and sp.count() == 4                   # did not hammer the dead server, kept all
    assert sp.drain(lambda pl: 502) == 0 and sp.count() == 4     # storage broken: keep for later


def test_unacceptable_shots_are_dropped_so_they_cannot_block_the_queue(tmp_path):
    sp = ShotSpool(str(tmp_path / "q"))
    now = time.time()
    sp.push(shot(now - 20))
    sp.push(shot(now - 10))
    statuses = iter([400, 200])
    assert sp.drain(lambda pl: next(statuses)) == 1 and sp.count() == 0


def test_bounded_by_size_and_age(tmp_path):
    sp = ShotSpool(str(tmp_path / "q"), max_bytes=1500, max_age=3600)
    now = time.time()
    sp.push(shot(now - 7200))                       # older than max_age: dropped on the next trim
    for i in range(6):
        sp.push(shot(now - 600 + i, n=400))         # ~450 bytes each: only the newest few fit under the cap
    left = [json.load(open(os.path.join(sp.folder, f)))["ts"] for f in sorted(os.listdir(sp.folder))]
    assert all(t > now - 3600 for t in left) and 1 <= len(left) <= 4
    assert left == sorted(left) and left[-1] == now - 600 + 5          # the newest is always kept


def test_corrupt_files_do_not_wedge_the_queue(tmp_path):
    sp = ShotSpool(str(tmp_path / "q"))
    os.makedirs(sp.folder)
    open(os.path.join(sp.folder, "0000000000000001.json"), "w").write("{not json")
    sp.push(shot(time.time()))
    assert sp.drain(lambda pl: 200) == 1 and sp.count() == 0
