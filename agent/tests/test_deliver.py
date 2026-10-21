import json

from cyril_agent.core import Spool, classify_status, deliver_events, sanitize_event


def ev(i, **kw):
    return {"ts": 1000.0 + i, "dur": 5, "app": "x", "title": f"t{i}", "url": "", "idle": False, **kw}


class FakeServer:
    """Accepts events unless a batch contains a title in `poison` (answering `poison_status`), or `down` is set."""

    def __init__(self, poison=(), poison_status=422, down=None):
        self.stored, self.calls, self.poison, self.poison_status, self.down = [], 0, set(poison), poison_status, down

    def __call__(self, events):
        self.calls += 1
        if self.down is not None:
            return self.down if self.down != "none" else None
        if any(e["title"] in self.poison for e in events):
            return self.poison_status
        self.stored += [e["title"] for e in events]
        return 200


def spooled(tmp_path, n):
    s = Spool(str(tmp_path / "spool.jsonl"))
    s.push([ev(i) for i in range(n)])
    return s


def test_backlog_is_delivered_in_order_after_an_outage(tmp_path):
    s = spooled(tmp_path, 1200)
    srv = FakeServer(down=530)                    # the Cloudflare tunnel is down
    r = deliver_events(s, srv)
    assert r["outcome"] == "failed" and s.count() == 1200 and srv.calls == 1   # nothing lost, no hammering
    srv.down = None
    r = deliver_events(s, srv)
    assert r["sent"] == 1200 and s.count() == 0 and srv.stored == [f"t{i}" for i in range(1200)]


def test_unreachable_and_gateway_errors_keep_everything(tmp_path):
    for down in ("none", 502, 503, 504, 524, 530, 429, 408):
        s = spooled(tmp_path, 3)
        assert deliver_events(s, FakeServer(down=down))["outcome"] == "failed" and s.count() == 3
        s.drop(3)


def test_wrong_key_or_address_keeps_everything(tmp_path):
    for status in (401, 403, 404):
        s = spooled(tmp_path, 3)
        assert deliver_events(s, FakeServer(down=status))["outcome"] == "failed" and s.count() == 3
        s.drop(3)


def test_one_poison_event_does_not_block_the_rest(tmp_path):
    for status in (400, 413, 422):
        s = spooled(tmp_path, 50)
        srv = FakeServer(poison={"t17"}, poison_status=status)
        r = deliver_events(s, srv)
        assert r["dropped"] == 1 and r["sent"] == 49 and s.count() == 0
        assert "t17" not in srv.stored and len(srv.stored) == 49 and srv.stored == sorted(srv.stored, key=lambda t: int(t[1:]))


def test_server_500_on_one_event_is_isolated_while_siblings_succeed(tmp_path):
    s = spooled(tmp_path, 20)
    srv = FakeServer(poison={"t3"}, poison_status=500)
    r = deliver_events(s, srv)
    assert r["dropped"] == 1 and r["sent"] == 19 and s.count() == 0


def test_server_500_on_everything_is_kept_not_discarded(tmp_path):
    s = spooled(tmp_path, 20)
    srv = FakeServer(poison={f"t{i}" for i in range(20)}, poison_status=500)   # a broken server, not bad events
    r = deliver_events(s, srv)
    assert r["dropped"] == 0 and s.count() == 20 and r["outcome"] == "failed"


def test_several_poison_events_across_batches(tmp_path):
    s = spooled(tmp_path, 700)
    srv = FakeServer(poison={"t5", "t400", "t650"})
    deliver_events(s, srv)
    deliver_events(s, srv)                         # the probe budget may spread this over two flushes
    assert s.count() == 0 and len(srv.stored) == 697


def test_outage_in_the_middle_of_finding_the_culprit_does_not_duplicate(tmp_path):
    s = spooled(tmp_path, 8)

    class Flaky(FakeServer):
        def __call__(self, events):
            self.calls += 1
            if any(e["title"] == "t6" for e in events):
                return 422 if len(events) > 1 and self.calls < 4 else 530
            self.stored += [e["title"] for e in events]
            return 200
    srv = Flaky()
    deliver_events(s, srv)
    assert len(srv.stored) == len(set(srv.stored))             # never uploaded twice
    assert s.count() + len(srv.stored) == 8                    # and none lost


def test_corrupt_lines_are_cleared(tmp_path):
    s = spooled(tmp_path, 5)
    with open(s.path, "a") as f:
        f.write('{"ts": 1, "dur"')                          # torn write after a crash
    s.push([ev(5)])
    srv = FakeServer()
    r = deliver_events(s, srv)
    assert srv.stored == [f"t{i}" for i in range(6)] and s.count() == 0 and r["sent"] == 6


def test_spool_is_bounded(tmp_path):
    s = Spool(str(tmp_path / "s"), max_bytes=5000)
    for i in range(200):
        s.push([ev(i)])
    assert (tmp_path / "s").stat().st_size <= 5000
    lines = open(s.path).readlines()
    assert s.count() == len(lines) and json.loads(lines[0])["title"] != "t0"        # no blank lines; oldest went first
    assert json.loads(lines[-1])["title"] == "t199"


def test_sanitize_makes_awkward_values_safe():
    e = sanitize_event({"ts": 5.0, "dur": 99999, "app": "a\x00b", "title": "bad\ud83d end", "url": "u" * 5000, "idle": 1})
    assert e["dur"] == 3600 and e["app"] == "ab" and "\ud83d" not in e["title"] and len(e["url"]) == 2048 and e["idle"] is True
    json.dumps(e, allow_nan=False).encode("utf-8")             # strict JSON the server will parse
    assert sanitize_event({"ts": float("nan"), "dur": 1}) is None
    assert sanitize_event({"ts": 5, "dur": float("inf")})["dur"] == 0
    assert sanitize_event({"dur": 1}) is None
    assert sanitize_event({"ts": 5, "dur": -3})["dur"] == 0


def test_classify_status():
    assert [classify_status(s) for s in (200, None, 530, 502, 500, 422, 401)] == \
        ["ok", "transient", "transient", "transient", "ambiguous", "permanent", "config"]


def test_health_file_tracks_continuous_failure(tmp_path):
    import time
    from cyril_agent import main
    from cyril_agent.core import Aggregator
    main.HEALTH_PATH = str(tmp_path / "health.json")
    main.STATUS["events_failing_since"] = 0
    state = {"code": 530}
    main._post_status = lambda srv, path, key, pl: state["code"] if path == "/api/ingest" else 200
    sp, agg = Spool(str(tmp_path / "s.jsonl")), Aggregator()

    def flush():
        agg.add(time.time(), 5, "code", "t", "", False)
        main.flush_events("http://x", "k", "u", "d", agg, sp, "p", "ok")
        return json.load(open(main.HEALTH_PATH))

    first = flush()["failing_since"]
    assert first > 0
    time.sleep(0.01)
    assert flush()["failing_since"] == first            # same outage, same start time
    state["code"] = 200
    h = flush()
    assert h["failing_since"] == 0 and h["reason"] == ""    # recovered: cleared
