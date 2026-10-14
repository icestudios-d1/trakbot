"""Thin S3-compatible object-store client for screenshots -- no AWS SDK
dependency, just the SigV4-signed REST calls that SeaweedFS's S3 gateway
understands (SeaweedFS replaces MinIO here).

Why self-hosted object storage: screenshots are the heaviest thing Cyril
stores, and keeping them on the office server (not a third-party CDN) means
no third party ever holds employee screen captures, and no dependence on an
external plan/SLA limit. SeaweedFS was picked over MinIO because it needs no
cluster-layout bootstrap for a single-node deployment -- one container,
`weed server -s3`, done -- and over other MinIO replacements (e.g. Garage)
for the same reason. It survives office-server restarts via its own Docker
volume, independent of the app container.
"""
import hashlib
import hmac
import os
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

REGION = "us-east-1"  # ignored by SeaweedFS; any value works


class ObjectStoreError(RuntimeError):
    pass


class S3Store:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, secure: bool = False):
        self.endpoint = endpoint.rstrip("/")
        self.scheme = "https" if secure else "http"
        self.access_key, self.secret_key, self.bucket = access_key, secret_key, bucket
        self.host = self.endpoint.split("://")[-1] if "://" in self.endpoint else self.endpoint
        self._base = f"{self.scheme}://{self.host}"

    # -------------------------------------------------------------- signing
    def _sign(self, method: str, path: str, payload: bytes, extra_headers: dict | None = None):
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        headers.update(extra_headers or {})
        signed_keys = sorted(headers)
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_keys)
        signed_header_str = ";".join(signed_keys)
        canonical_request = "\n".join([
            method, quote(path, safe="/"), "", canonical_headers, signed_header_str, payload_hash])
        scope = f"{date_stamp}/{REGION}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])

        def _hmac(key, msg):
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k = _hmac(("AWS4" + self.secret_key).encode(), date_stamp)
        k = _hmac(k, REGION)
        k = _hmac(k, "s3")
        k = _hmac(k, "aws4_request")
        signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
        auth = (f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                f"SignedHeaders={signed_header_str}, Signature={signature}")
        headers["authorization"] = auth
        return headers

    def _request(self, method: str, key: str, payload: bytes = b"") -> bytes:
        path = f"/{self.bucket}/{key}"
        headers = self._sign(method, path, payload)
        req = urllib.request.Request(self._base + path, data=payload or None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise ObjectStoreError(f"{method} {key} -> {e.code}: {body[:300]}") from e

    def ensure_bucket(self) -> None:
        try:
            self._request("HEAD", "")
        except ObjectStoreError:
            try:
                self._request("PUT", "")
            except ObjectStoreError:
                pass  # created concurrently, or already exists

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> None:
        self._request("PUT", key, data)

    def get(self, key: str) -> bytes:
        return self._request("GET", key)

    def delete(self, key: str) -> None:
        try:
            self._request("DELETE", key)
        except ObjectStoreError:
            pass  # idempotent: already gone counts as deleted
