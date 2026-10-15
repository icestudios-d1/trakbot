import os


def test_ca_bundle_is_configured():
    os.environ.pop("SSL_CERT_FILE", None)
    import importlib, cyril_agent
    importlib.reload(cyril_agent)
    path = os.environ.get("SSL_CERT_FILE", "")
    assert path.endswith(".pem") and os.path.exists(path)
