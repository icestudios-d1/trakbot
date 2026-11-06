"""Seal what the agent collects so that nobody on the PC can read it back: not the employee, not a proxy.

The agent is configured with the SERVER'S PUBLIC KEY only (written by the installer into the admin-only config).
Everything it queues on disk and sends is encrypted to that key (X25519 key agreement with a fresh key per item,
HKDF-SHA256, AES-256-GCM). A public key can encrypt but cannot decrypt, so the agent itself has no means of reading
its own queue: the files in ProgramData\\Cyril\\data and the bytes on the wire are ciphertext, and only the server
(which holds the private key) can open them. Reading the config, the queue, the process or the network traffic of
the PC reveals nothing but who is reporting and when.

Without a public key in the config (a PC installed before this existed), or without the `cryptography` package, the
functions return the data unchanged, so an old install keeps reporting until the daily updater rewrites its config.
"""
import base64
import json
import os

_INFO = b"cyril-seal-v1"
_AAD = b"cyril1"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def seal(obj: dict, server_pub_b64: str) -> dict:
    """{"enc": {"v":1,"k":<ephemeral public key>,"n":<nonce>,"c":<ciphertext>}} for a JSON-able dict."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    spub_raw = base64.b64decode(server_pub_b64)
    eph = X25519PrivateKey.generate()
    epub_raw = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(spub_raw))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO + epub_raw + spub_raw).derive(shared)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(obj, separators=(",", ":")).encode("utf-8"), _AAD)
    return {"enc": {"v": 1, "k": _b64(epub_raw), "n": _b64(nonce), "c": _b64(ct)}}


def make_sealer(server_pub_b64: str | None):
    """A function dict -> dict that seals, or one that returns its input unchanged when sealing is not available."""
    if not server_pub_b64:
        return lambda obj: obj
    try:
        seal({"probe": 1}, server_pub_b64)          # the key is valid and the library is present
    except Exception:
        return lambda obj: obj
    return lambda obj: seal(obj, server_pub_b64)
