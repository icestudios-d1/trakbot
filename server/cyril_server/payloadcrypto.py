"""Opens what agents seal (see agent/cyril_agent/seal.py). The private key lives only here.

Key storage: the file `payload.key` next to the database (the `cyril_db` volume, so it survives rebuilds), created on
first start; or set CYRIL_PAYLOAD_KEY (base64 of the 32 raw bytes) to manage it yourself. Back it up with the database.
Replacing the key means agents must receive the new public key (the daily updater rewrites their config) and anything
still queued under the old key is refused."""
import base64
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"cyril-seal-v1"
_AAD = b"cyril1"
_RAW = (serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


class BadEnvelope(ValueError):
    pass


def _load(path: str) -> X25519PrivateKey:
    env = os.environ.get("CYRIL_PAYLOAD_KEY", "").strip()
    if env:
        return X25519PrivateKey.from_private_bytes(base64.b64decode(env))
    try:
        with open(path, "rb") as f:
            return X25519PrivateKey.from_private_bytes(base64.b64decode(f.read().strip()))
    except FileNotFoundError:
        k = X25519PrivateKey.generate()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64encode(k.private_bytes(*_RAW)))
        return k


class Opener:
    def __init__(self, key_path: str):
        self._priv = _load(key_path)
        self._pub_raw = self._priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_b64 = base64.b64encode(self._pub_raw).decode()

    def open(self, wrapper: dict) -> dict:
        """The dict that was sealed, from {"enc": {...}}. Raises BadEnvelope for anything that does not open."""
        try:
            e = wrapper["enc"]
            epub = base64.b64decode(e["k"])
            shared = self._priv.exchange(X25519PublicKey.from_public_bytes(epub))
            key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO + epub + self._pub_raw).derive(shared)
            out = json.loads(AESGCM(key).decrypt(base64.b64decode(e["n"]), base64.b64decode(e["c"]), _AAD))
        except Exception as err:
            raise BadEnvelope("cannot open the sealed payload (wrong or stale server key, or damaged)") from err
        if not isinstance(out, dict):
            raise BadEnvelope("sealed payload is not an object")
        return out
