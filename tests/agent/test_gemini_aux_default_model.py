"""[daveey] Gemini's auxiliary default (title generation etc.) must be a model new AI Studio keys can
use: gemini-2.5-* returns 404 "no longer available to new users"."""

from agent.auxiliary_client import _get_aux_model_for_provider, resolve_provider_client
from agent.gemini_native_adapter import GeminiNativeClient


def test_gemini_aux_default_is_current_flash():
    assert _get_aux_model_for_provider("gemini") == "gemini-3.8-flash"


def test_title_generation_on_gemini_without_model_uses_current_flash(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)

    client, model = resolve_provider_client("gemini", None, task="title_generation")

    assert isinstance(client, GeminiNativeClient)
    assert model == "gemini-3.8-flash"
