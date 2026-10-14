"""Covers the "Name or service not known" startup crash: a DNS/connection
failure reaching SeaweedFS must turn into ObjectStoreError, not an uncaught
urllib.error.URLError that takes the whole app down at import time."""
from cyril_server.objstore import ObjectStoreError, S3Store

UNREACHABLE = "this-host-does-not-exist.invalid:9999"


def test_unreachable_host_raises_objectstoreerror_not_urlerror():
    s = S3Store(endpoint=UNREACHABLE, access_key="a", secret_key="b", bucket="x")
    try:
        s.put("k", b"d")
        assert False, "expected ObjectStoreError"
    except ObjectStoreError:
        pass


def test_ensure_bucket_does_not_raise_when_store_is_unreachable():
    s = S3Store(endpoint=UNREACHABLE, access_key="a", secret_key="b", bucket="x")
    s.ensure_bucket(retries=1, delay=0)  # must return quietly, not raise
