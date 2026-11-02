"""Catalog lookup defaults use transport errors, not real remote DNS."""
import socket

import httpx
import pytest

from agent import model_metadata, model_metadata_http


@pytest.fixture
def no_dns(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("incidental metadata must not reach DNS")
    monkeypatch.setattr(socket, "getaddrinfo", unexpected)


def test_metadata_get_is_offline_before_dns(no_dns):
    with pytest.raises(httpx.ConnectError, match="offline metadata"):
        model_metadata_http.get("https://catalog.example.test/models")


def test_ollama_probe_uses_offline_transport(no_dns):
    assert model_metadata._ollama_show("http://ollama.example.test", "fake", "model") is None


def test_explicit_metadata_client_fake_still_observes_real_request(no_dns, monkeypatch):
    seen = []
    real_client = httpx.Client

    def handle(request):
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json={"data": [{"id": "test/model"}]})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handle)))
    response = model_metadata_http.get("https://catalog.example.test/models")
    assert response.json() == {"data": [{"id": "test/model"}]}
    assert seen == [("GET", "https://catalog.example.test/models")]
