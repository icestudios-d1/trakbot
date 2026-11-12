"""The agent's local listener (127.0.0.1:7077): only "OMS Ping" - nothing can be sent to the agent through it."""
import http.client
import json
import threading
from http.server import HTTPServer

import pytest

from cyril_agent import main as m


@pytest.fixture
def door():
    srv = HTTPServer(("127.0.0.1", 0), m.LocalDoor)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    m.OMS_ORIGINS.clear()
    m.OMS_ORIGINS.add("https://oms.example.com")
    m.IDENT.update(user="priya", device="DEV-1")
    yield srv.server_address[1]
    srv.shutdown()


def call(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    raw = r.read()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:      # the standard library's HTML error page (e.g. 501 for a method the door does not have)
        body = None
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


def test_oms_ping_answers_the_allowed_site_with_details(door):
    st, h, body = call(door, "GET", "/oms/ping?nonce=abc123", {"Origin": "https://oms.example.com"})
    assert st == 200 and body["ok"] and body["app"] == "NetMon" and body["nonce"] == "abc123"
    assert body["device"] == "DEV-1" and body["user"] == "priya" and body["version"]
    assert h["access-control-allow-origin"] == "https://oms.example.com" and h["access-control-allow-private-network"] == "true"


def test_oms_ping_preflight_for_the_allowed_site_only(door):
    st, h, _ = call(door, "OPTIONS", "/oms/ping", {"Origin": "https://oms.example.com", "Access-Control-Request-Private-Network": "true"})
    assert st == 204 and h["access-control-allow-origin"] == "https://oms.example.com" and h["access-control-allow-private-network"] == "true"
    st, h, _ = call(door, "OPTIONS", "/oms/ping", {"Origin": "https://evil.example"})
    assert st == 204 and "access-control-allow-origin" not in h


def test_other_websites_learn_nothing_from_the_ping(door):
    st, h, body = call(door, "GET", "/oms/ping", {"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in h and body == {"ok": True, "app": "NetMon"}      # unreadable and minimal


def test_ping_without_origin_is_for_local_tools_and_checked_for_host(door):
    st, _, body = call(door, "GET", "/oms/ping")
    assert st == 200 and body["device"] == "DEV-1"
    assert call(door, "GET", "/oms/ping", {"Host": "attacker.example:7077"})[0] == 403         # DNS rebinding
    assert call(door, "GET", "/nope")[0] == 404


def test_nothing_can_be_sent_to_the_agent_through_the_door(door):
    """There is no browser extension any more, so there is no POST endpoint: a page address can only come from the
    browser window itself. Every POST is refused, whoever sends it."""
    pl = json.dumps({"url": "https://fake.example/", "title": "x"})
    js = {"Content-Type": "application/json"}
    for h in ({**js, "Origin": "https://evil.example"}, {**js, "Origin": "chrome-extension://abcdef"}, js,
              {"Content-Type": "text/plain"}):
        assert call(door, "POST", "/", h, pl)[0] == 501
    assert not hasattr(m, "ext_report")
    # a preflight from a website is not granted either
    st, h, _ = call(door, "OPTIONS", "/", {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in h


def test_company_oms_can_read_the_ping_with_no_setting(door):
    m.OMS_ORIGINS.clear()
    m.OMS_ORIGINS.update(m.DEFAULT_OMS_ORIGINS)          # what the agent starts with, and keeps after every poll
    o = "https://office.icestudios.in"
    st, h, _ = call(door, "OPTIONS", "/oms/ping", {"Origin": o, "Access-Control-Request-Private-Network": "true"})
    assert st == 204 and h["access-control-allow-origin"] == o and h["access-control-allow-private-network"] == "true"
    st, h, body = call(door, "GET", "/oms/ping", {"Origin": o})
    assert h["access-control-allow-origin"] == o and body["ping"] == "pong"
