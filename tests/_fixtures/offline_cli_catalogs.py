"""Offline transport defaults for CLI manifests and provider model listings."""
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import pytest

from tests._fixtures.offline_catalogs import _is_loopback


@pytest.fixture(autouse=True)
def _offline_cli_catalog_transport(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return
    from hermes_cli import model_catalog, models

    real_urlopen = urllib.request.urlopen
    real_credentials = models.open_credentialed_url
    manifest_open = model_catalog._open_manifest_request
    provider_open = models._urlopen_model_catalog_request

    def remote(req):
        return not _is_loopback(urlsplit(req.full_url).hostname or "")

    def manifest(req, **kwargs):
        if remote(req) and urllib.request.urlopen is real_urlopen:
            raise urllib.error.URLError("offline CLI catalog fixture: stub the manifest transport")
        return manifest_open(req, **kwargs)

    def provider(req, **kwargs):
        if remote(req) and models.open_credentialed_url is real_credentials:
            raise urllib.error.URLError("offline CLI catalog fixture: stub the provider transport")
        return provider_open(req, **kwargs)

    monkeypatch.setattr(model_catalog, "_open_manifest_request", manifest)
    monkeypatch.setattr(models, "_urlopen_model_catalog_request", provider)
