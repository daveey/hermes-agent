"""Non-streaming response identity comes only from executed, string-valued metadata."""
import pytest

from gateway.platforms.api_server_openai_routes import _apply_served_runtime


@pytest.mark.parametrize("result,expected", [
    ({"provider": "anthropic", "model": "claude-concrete"},
     {"provider": "anthropic", "model": "claude-concrete"}),
    ({"provider": "openai-codex", "model": "gpt-concrete"},
     {"provider": "openai-codex", "model": "gpt-concrete"}),
    ({"served_runtime": {"provider": "anthropic", "model": "fallback-concrete"},
      "provider": "openai-codex", "model": "primary-concrete"},
     {"provider": "anthropic", "model": "fallback-concrete"}),
    ({"final_response": "", "completed": False}, {"model": "class:fast"}),
    ({"provider": None, "model": 42}, {"model": "class:fast"}),
    ({"provider": "   ", "model": ""}, {"model": "class:fast"}),
])
def test_served_identity_projection(result, expected):
    response = {"model": "class:fast"}
    _apply_served_runtime(response, result)
    assert response == expected
