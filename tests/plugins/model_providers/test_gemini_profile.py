"""Contract tests for the native Google Gemini provider profile."""

from __future__ import annotations

import pytest


@pytest.fixture
def gemini_profile():
    import model_tools
    import providers

    profile = providers.get_provider_profile("gemini")
    assert profile is not None, "gemini provider profile must be registered"
    return profile


def test_native_gemini_auxiliary_default_is_in_curated_catalog(gemini_profile):
    """The profile's default_aux_model must stay in lockstep with the curated
    model picker catalog — whatever model the default points at has to be
    one the picker can actually offer. Deliberately durable against future
    model-generation bumps: it does not pin either side to a frozen
    model-name string, only to the invariant that they never drift apart.
    """
    from hermes_cli.models import _PROVIDER_MODELS

    assert gemini_profile.default_aux_model in _PROVIDER_MODELS["gemini"]


# [daveey] fetch_models: native AI Studio /models takes x-goog-api-key, not Bearer.
import json as _json
from unittest.mock import patch as _patch


class _Resp:
    def __init__(self, payload):
        self._body = _json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(pages):
    seen = []

    def fake_open(req, *, timeout, **_kw):
        seen.append(req)
        return _Resp(pages[len(seen) - 1])

    return seen, fake_open


def test_native_fetch_models_uses_goog_api_key_and_native_shape(gemini_profile):
    pages = [
        {
            "models": [
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            ],
            "nextPageToken": "tok2",
        },
        {"models": [{"name": "models/gemini-3.8-pro", "supportedGenerationMethods": ["generateContent"]}]},
    ]
    seen, fake_open = _capture(pages)
    with _patch("hermes_cli.urllib_security.open_credentialed_url", side_effect=fake_open):
        ids = gemini_profile.fetch_models(api_key="k-test", base_url=gemini_profile.base_url)
    assert ids == ["gemini-3.8-flash", "gemini-3.8-pro"]
    assert len(seen) == 2
    for req in seen:
        assert req.full_url.startswith("https://generativelanguage.googleapis.com/v1beta/models?")
        assert "pageSize=1000" in req.full_url
        assert req.get_header("X-goog-api-key") == "k-test"
        assert req.get_header("Authorization") is None
    assert "pageToken=tok2" in seen[1].full_url


def test_openai_compat_base_keeps_bearer(gemini_profile):
    base = "https://generativelanguage.googleapis.com/v1beta/openai"
    seen, fake_open = _capture([{"data": [{"id": "gemini-3.8-flash"}]}])
    with _patch("hermes_cli.urllib_security.open_credentialed_url", side_effect=fake_open):
        ids = gemini_profile.fetch_models(api_key="k-test", base_url=base)
    assert ids == ["gemini-3.8-flash"]
    assert seen[0].full_url == base + "/models"
    assert seen[0].get_header("Authorization") == "Bearer k-test"
    assert seen[0].get_header("X-goog-api-key") is None


def test_custom_proxy_base_uses_base_models(gemini_profile):
    base = "https://proxy.example.com/v1"
    seen, fake_open = _capture([{"data": [{"id": "gemini-proxy"}]}])
    with _patch("hermes_cli.urllib_security.open_credentialed_url", side_effect=fake_open):
        ids = gemini_profile.fetch_models(api_key="k-test", base_url=base)
    assert ids == ["gemini-proxy"]
    assert seen[0].full_url == base + "/models"
    assert seen[0].get_header("X-goog-api-key") is None


def test_native_fetch_models_401_returns_none_without_logging_key(gemini_profile, caplog):
    import logging
    import urllib.error

    def boom(req, *, timeout, **_kw):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    caplog.set_level(logging.DEBUG)
    with _patch("hermes_cli.urllib_security.open_credentialed_url", side_effect=boom):
        assert gemini_profile.fetch_models(api_key="k-secret-xyz") is None
    assert "k-secret-xyz" not in caplog.text
