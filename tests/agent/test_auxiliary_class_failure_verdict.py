"""Class routing reads upstream's classify_api_error verdict once and keeps the fork's fences."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import auxiliary_class_routing as routing
from agent import auxiliary_client as aux
from agent import auxiliary_error_policy as aux_errors


class QuotaError(Exception):
    status_code = 429


@pytest.fixture
def boundary(monkeypatch):
    """Class entry paths over a stubbed route; ``state.raises`` lists what each attempt raises."""
    state = SimpleNamespace(prepared=[], raises=[], pool=Mock())
    state.pool.current.return_value = SimpleNamespace(id="account-a")
    state.pool.mark_exhausted_and_rotate.return_value = SimpleNamespace(id="account-b")
    runtime = {"provider": "anthropic", "model": "fast-model", "api_mode": "anthropic_messages",
               "base_url": "https://api.anthropic.com", "credential_pool": state.pool}

    def prepare(task, options, request, failed):
        state.prepared.append(list(failed))
        return runtime, SimpleNamespace(), {}

    def attempt():
        if state.raises:
            raise state.raises.pop(0)
        return "YES"

    async def async_complete(*args, **kwargs):
        return attempt()

    def stream(*args, **kwargs):
        yield attempt()

    monkeypatch.setattr(routing, "_class_request", lambda *args: object())
    monkeypatch.setattr(routing, "_prepare", prepare)
    monkeypatch.setattr(routing, "_complete", lambda *args: attempt())
    monkeypatch.setattr(routing, "_stream", stream)
    monkeypatch.setattr(aux, "_close_cached_client", lambda client, **kw: None)
    monkeypatch.setattr(aux, "_to_async_client", lambda client, model: (client, model))
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)
    monkeypatch.setattr(aux, "_relay_async_completion", async_complete)
    return state


def _run(path):
    if path == "async":
        return asyncio.run(routing.class_routed_async(Mock())())
    result = routing.class_routed_sync(Mock())(stream=path == "stream")
    return list(result) if path == "stream" else result


PATHS = ["sync", "async", "stream"]


@pytest.mark.parametrize("path", PATHS)
def test_terminal_verdict_raises_without_reallocation(boundary, path):
    boundary.raises = [ValueError("boom")]  # unknown, no fallback label: it would follow the request anywhere
    with pytest.raises(ValueError, match="boom"):
        _run(path)
    assert boundary.prepared == [[]]
    boundary.pool.mark_exhausted_and_rotate.assert_not_called()


@pytest.mark.parametrize("path", PATHS)
def test_managed_fence_violation_fails_closed_even_if_classifier_offers_fallback(boundary, monkeypatch, path):
    monkeypatch.setattr(aux_errors, "fallback_label", lambda failure, **kw: "hook says fall back")
    boundary.raises = [routing.ModelRoutingError("Managed auxiliary SDK endpoint changed")]
    with pytest.raises(routing.ModelRoutingError, match="endpoint changed"):
        _run(path)
    assert boundary.prepared == [[]]
    boundary.pool.mark_exhausted_and_rotate.assert_not_called()


@pytest.mark.parametrize("path", PATHS)
def test_failed_request_is_classified_once(boundary, monkeypatch, path):
    calls = []
    real = aux._classify_failure

    def counting(exc, *args, **kwargs):
        calls.append((exc, args))
        return real(exc, *args, **kwargs)

    monkeypatch.setattr(aux, "_classify_failure", counting)
    quota = QuotaError("Rate limit exceeded for this account")
    boundary.raises = [quota]
    assert _run(path) == (["YES"] if path == "stream" else "YES")
    assert [exc for exc, _ in calls] == [quota]
    assert calls[0][1][0] == "anthropic" and calls[0][1][2] == "fast-model"  # the route it was sent to
    assert boundary.pool.mark_exhausted_and_rotate.call_args.kwargs["failure_reason"] == "rate_limit"


@pytest.mark.parametrize("path", PATHS)
def test_judged_attempt_surfaces_the_provider_error(boundary, path):
    error = ValueError("provider said no")
    failure = aux._classify_failure(error, "anthropic", None, "fast-model")
    boundary.raises = [aux._ClassifiedAttempt(failure)]
    with pytest.raises(ValueError, match="provider said no") as raised:
        _run(path)
    assert raised.value is error
    assert boundary.prepared == [[]]


def test_delivered_stream_surfaces_the_provider_error(monkeypatch):
    error = ValueError("mid-stream")
    failure = aux._classify_failure(error, "anthropic", None, "fast-model")
    runtime = {"provider": "anthropic", "model": "fast-model", "credential_pool": Mock()}

    def stream(*args):
        yield "first"
        raise aux._ClassifiedAttempt(failure)

    monkeypatch.setattr(routing, "_class_request", lambda *args: object())
    monkeypatch.setattr(routing, "_prepare", lambda *args: (runtime, object(), {}))
    monkeypatch.setattr(routing, "_stream", stream)
    result = routing.class_routed_sync(Mock())(stream=True)
    assert next(result) == "first"
    with pytest.raises(ValueError) as raised:
        next(result)
    assert raised.value is error
