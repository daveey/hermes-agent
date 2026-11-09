"""Codex's reason-less ``HTTP 400 {"detail": "Bad Request"}`` (self-heal cron-4f48467a6f).

The ChatGPT Codex backend once answered a healthy cron turn's request this way and the identical next
request succeeded. It must retry within the normal retry cap (never the post-exhaustion auto-recovery
ladder), stay provider- and envelope-narrow, and log one ``codex bare-400 retry`` line per attempt.
"""

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from tests.agent.test_error_classifier import MockAPIError
from tests.agent.test_run_agent_codex_responses import (  # _no_codex_backoff: autouse fixture
    _build_agent,
    _codex_message_response,
    _no_codex_backoff,
)


@pytest.mark.parametrize("body", [
    {"detail": "Bad Request"},
    {"detail": " bad request "},
    None,  # SDK path with only the wrapped message text
], ids=["dict-body", "normalised-detail", "message-only"])
def test_codex_bare_bad_request_detail_is_retryable(body):
    """cron-4f48467a6f: the ChatGPT Codex backend answered a healthy request with a reason-less
    ``{"detail": "Bad Request"}`` 400 once; the identical next request succeeds. Retry it."""
    e = MockAPIError("Error code: 400 - {'detail': 'Bad Request'}", status_code=400, body=body)
    result = classify_api_error(e, provider="openai-codex", model="gpt-6-astra", approx_tokens=28000)
    assert result.reason == FailoverReason.server_error
    assert result.retryable is True
    assert result.error_context.get("codex_bare_400") is True  # keeps it off the auto-recovery ladder

@pytest.mark.parametrize(("provider", "body"), [
    ("openai", {"detail": "Bad Request"}),  # same envelope, other provider: genuine shape 400
    ("custom", {"detail": "Bad Request"}),
    ("openai-codex", {"detail": "Bad Request: input[3].content must be a string"}),  # descriptive detail
    ("openai-codex", {"error": {"message": "Bad Request", "code": "invalid_value", "param": "input"}}),
], ids=["openai", "custom", "codex-descriptive", "codex-error-object"])
def test_codex_bare_bad_request_retry_stays_narrow(provider, body):
    e = MockAPIError(f"Error code: 400 - {body}", status_code=400, body=body)
    result = classify_api_error(e, provider=provider, model="gpt-6-astra", approx_tokens=28000)
    assert result.reason == FailoverReason.format_error
    assert result.retryable is False


def _codex_bare_bad_request():
    """The reason-less ``HTTP 400 {"detail": "Bad Request"}`` the ChatGPT Codex backend returned (cron-4f48467a6f)."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    response = httpx.Response(400, request=request, json={"detail": "Bad Request"})
    return openai.BadRequestError("Error code: 400 - {'detail': 'Bad Request'}", response=response,
                                  body={"detail": "Bad Request"})


def test_run_conversation_codex_bare_bad_request_400_retries_once_then_completes(monkeypatch, caplog):
    """cron-4f48467a6f: one bare codex 400 used to end an unattended turn; the identical retry succeeds."""
    agent = _build_agent(monkeypatch)
    agent.provider = "openai-codex"
    agent._disable_streaming = True
    calls = {"count": 0}

    def _flaky_api_call(api_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _codex_bare_bad_request()
        return _codex_message_response("Recovered")

    monkeypatch.setattr(agent, "_interruptible_api_call", _flaky_api_call)

    with caplog.at_level("WARNING"):
        result = agent.run_conversation("Say hello")

    assert result["completed"] is True
    assert result["final_response"] == "Recovered"
    assert calls["count"] == 2
    assert sum("codex bare-400 retry" in r.getMessage() for r in caplog.records) == 1


def test_run_conversation_codex_bare_bad_request_400_stops_at_retry_cap(monkeypatch, caplog):
    """A codex endpoint that keeps answering the bare 400 is bounded by the normal retry cap."""
    agent = _build_agent(monkeypatch)
    agent.provider = "openai-codex"
    agent._disable_streaming = True
    calls = {"count": 0}

    def _always_400(api_kwargs):
        calls["count"] += 1
        raise _codex_bare_bad_request()

    monkeypatch.setattr(agent, "_interruptible_api_call", _always_400)

    with caplog.at_level("WARNING"):
        result = agent.run_conversation("Say hello")

    assert result["completed"] is not True
    # Exactly the normal retry cap: the post-exhaustion auto-recovery ladder (5 more 15-60 s cycles
    # of full retry rounds) must not engage for a bare 400 that survives every retry.
    assert agent._api_max_retries >= 2
    assert calls["count"] == agent._api_max_retries
    assert not any("auto-recovery cycle" in r.getMessage() for r in caplog.records)
    assert sum("codex bare-400 retry" in r.getMessage() for r in caplog.records) == calls["count"]
