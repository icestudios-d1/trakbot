"""Cyril agent."""
import os as _os

# A frozen (PyInstaller) Python on macOS ships no CA certificates, so every HTTPS call fails with
# CERTIFICATE_VERIFY_FAILED. Point OpenSSL at certifi's bundle (bundled into the build) unless the
# machine already configures one. Harmless on Windows/Linux, which also have a system store.
try:
    import certifi as _certifi
    _os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
except Exception:
    pass
