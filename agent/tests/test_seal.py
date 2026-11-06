import base64
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from cyril_agent.core import ShotSpool, Spool
from cyril_agent.seal import make_sealer, seal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "server"))


def _server_key(tmp_path):
    from cyril_server.payloadcrypto import Opener
    return Opener(str(tmp_path / "payload.key"))


def test_sealed_data_opens_only_with_the_server_key_and_is_unreadable_on_disk(tmp_path):
    op = _server_key(tmp_path)
    ev = {"ts": 1.0, "dur": 5.0, "app": "Photoshop.exe", "title": "SECRET-CLIENT-PITCH.psd", "url": "https://bank.example/acct"}
    sealer = make_sealer(op.public_b64)
    sp = Spool(str(tmp_path / "spool.jsonl"))
    sp.push([sealer(ev), sealer(ev)])
    raw = open(tmp_path / "spool.jsonl", "rb").read()
    for needle in (b"SECRET-CLIENT-PITCH", b"Photoshop", b"bank.example"):
        assert needle not in raw                                   # nothing readable in the queue file
    queued, n = sp.take(10)
    assert n == 2 and op.open(queued[0]) == ev                    # the server reads it
    assert queued[0]["enc"]["c"] != queued[1]["enc"]["c"]          # fresh key per item: no repeated ciphertext


def test_screenshot_queue_holds_no_readable_image_or_title(tmp_path):
    op = _server_key(tmp_path)
    sealer = make_sealer(op.public_b64)
    pl = {"user": "u", "device": "d", "ts": __import__("time").time(), **sealer({"app": "x", "title": "PRIVATE-TITLE", "image_b64": "QUJD" * 1000})}
    q = ShotSpool(str(tmp_path / "q"))
    q.push(pl)
    f = next((tmp_path / "q").glob("*.json"))
    assert b"PRIVATE-TITLE" not in f.read_bytes() and b"QUJD" not in f.read_bytes()
    seen = []
    q.drain(lambda p: seen.append(p) or 200)
    assert op.open(seen[0]) == {"app": "x", "title": "PRIVATE-TITLE", "image_b64": "QUJD" * 1000}


def test_a_different_key_cannot_open_and_no_key_means_plain(tmp_path):
    import pytest
    from cyril_server.payloadcrypto import BadEnvelope, Opener
    a = Opener(str(tmp_path / "a.key"))
    b = Opener(str(tmp_path / "b.key"))
    with pytest.raises(BadEnvelope):
        b.open(seal({"x": 1}, a.public_b64))
    tampered = seal({"x": 1}, a.public_b64)
    c = bytearray(base64.b64decode(tampered["enc"]["c"])); c[0] ^= 1; tampered["enc"]["c"] = base64.b64encode(bytes(c)).decode()
    with pytest.raises(BadEnvelope):
        a.open(tampered)
    assert make_sealer(None)({"x": 1}) == {"x": 1} and make_sealer("not-a-key")({"x": 1}) == {"x": 1}
