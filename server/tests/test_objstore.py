"""S3Store behaviour against a fake HTTP layer (no SeaweedFS needed)."""
import io
import urllib.error
from unittest import mock

import pytest

from cyril_server.objstore import ObjectStoreError, S3Store


def http_error(code, body=b"boom"):
    return urllib.error.HTTPError("http://s3/x", code, "err", {}, io.BytesIO(body))


class Resp:
    def __init__(self, body=b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def store():
    return S3Store("seaweedfs:8333", "ak", "sk", "shots")


def test_put_creates_a_missing_bucket_and_retries():
    calls = []

    def fake(req, timeout=0):
        calls.append((req.get_method(), req.full_url.split("seaweedfs:8333")[1]))
        if len(calls) == 1:
            raise http_error(404, b"NoSuchBucket")
        return Resp()

    with mock.patch("urllib.request.urlopen", fake):
        store().put("a/b.jpg", b"x")
    assert calls == [("PUT", "/shots/a/b.jpg"), ("PUT", "/shots/"), ("PUT", "/shots/a/b.jpg")]


def test_put_failure_carries_the_stores_own_message():
    with mock.patch("urllib.request.urlopen", side_effect=http_error(500, b"no free volumes left")):
        with pytest.raises(ObjectStoreError) as e:
            store().put("a.jpg", b"x")
    assert e.value.code == 500 and "no free volumes left" in str(e.value)


def test_delete_ignores_already_gone_but_not_real_failures():
    with mock.patch("urllib.request.urlopen", side_effect=http_error(404)):
        store().delete("gone.jpg")                      # fine: already deleted
    with mock.patch("urllib.request.urlopen", side_effect=http_error(500)):
        with pytest.raises(ObjectStoreError):
            store().delete("x.jpg")                     # must surface so callers keep the row and can retry
    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(ObjectStoreError):
            store().delete("x.jpg")
