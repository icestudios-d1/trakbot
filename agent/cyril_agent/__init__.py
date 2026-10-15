"""Cyril agent."""
import os as _os
import sys as _sys

# A frozen (PyInstaller) Python on macOS ships no CA certificates, and Python does not fetch a missing intermediate
# certificate the way browsers and curl do, so HTTPS fails with CERTIFICATE_VERIFY_FAILED. On macOS use the system
# trust store (the Keychain, same as Safari/curl); otherwise, or if that is unavailable, use certifi's bundle.
_done = False
if _sys.platform == "darwin":
    try:
        import truststore as _truststore
        _truststore.inject_into_ssl()
        _done = True
    except Exception:
        pass
if not _done:
    try:
        import certifi as _certifi
        _os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    except Exception:
        pass
