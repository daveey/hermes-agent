"""Offline defaults for incidental catalogs fetched while building tool schemas.

Provider transport tests can replace requests.get themselves; real loopback HTTP
fixtures are deliberately left intact. This is not a general HTTP/network fence.
"""
from ipaddress import ip_address
from urllib.parse import urlsplit

import pytest


def _is_loopback(host):
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _offline_video_catalog(request, monkeypatch):
    """Unrelated agent tests use the provider's bundled video catalog fallback."""
    if request.node.get_closest_marker("live"):
        return
    import requests

    real_get = requests.get

    def get(url, *args, **kwargs):
        parsed = urlsplit(str(url))
        if parsed.path.rstrip("/").endswith("/videos/models") and not _is_loopback(parsed.hostname or ""):
            raise requests.ConnectionError(
                "offline video catalog fixture: stub requests.get to exercise a catalog response")
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(requests, "get", get)
