import hashlib
import http.server
import json
import os
import tempfile
import threading
from unittest import mock

from cyril_agent import update


def _server(manifest: dict, asset_bytes: bytes):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/agent/version":
                body = json.dumps(manifest).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/dist/cyril.exe":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(asset_bytes)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


def test_no_update_when_versions_match():
    srv, url = _server({"version": update.VERSION}, b"")
    try:
        assert update.check_and_apply(url, "k", "u", "d") is False
    finally:
        srv.shutdown()


def test_no_update_when_manifest_missing_asset():
    srv, url = _server({"version": "999.0.0"}, b"")  # no windows_exe/linux/macos key
    try:
        assert update.check_and_apply(url, "k", "u", "d") is False
    finally:
        srv.shutdown()


def test_refuses_apply_on_checksum_mismatch(monkeypatch):
    payload = b"new build bytes"
    manifest = {"version": "999.0.0", "linux": {"url": "/dist/cyril.exe", "sha256": "0" * 64}}
    srv, url = _server(manifest, payload)
    applied = []
    monkeypatch.setattr(update, "_apply", lambda data: applied.append(data))
    try:
        assert update.check_and_apply(url, "k", "u", "d") is False
        assert applied == []  # wrong checksum -> never reaches _apply
    finally:
        srv.shutdown()


def test_applies_on_checksum_match(monkeypatch):
    payload = b"new build bytes"
    sha = hashlib.sha256(payload).hexdigest()
    manifest = {"version": "999.0.0", "linux": {"url": "/dist/cyril.exe", "sha256": sha}}
    srv, url = _server(manifest, payload)
    applied = []
    monkeypatch.setattr(update, "_apply", lambda data: applied.append(data))
    monkeypatch.setattr(update.sys, "platform", "linux")
    try:
        assert update.check_and_apply(url, "k", "u", "d") is True
        assert applied == [payload]
    finally:
        srv.shutdown()


def test_apply_replaces_file_atomically(tmp_path, monkeypatch):
    target = tmp_path / "cyril"
    target.write_bytes(b"old")
    monkeypatch.setattr(update.sys, "argv", [str(target)])
    monkeypatch.setattr(update.sys, "frozen", False, raising=False)
    monkeypatch.setattr(update.platform, "system", lambda: "Linux")
    monkeypatch.setattr(update.os, "_exit", lambda code: None)  # don't actually kill the test process
    update._apply(b"new bytes")
    assert target.read_bytes() == b"new bytes"
