"""Class pool recovery reads upstream's verdict, on the route the request was sent to.

Each entry path (sync, async, stream) runs the real class wrapper, ``_complete`` /
async ``create`` closures and upstream's ``_create_with_progress`` /
``_acreate_with_progress`` against a scripted chat-completions client; only the
allocation (``_prepare``) and the managed relay envelope are stubbed.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import auxiliary_class_routing as routing
from agent import auxiliary_client as aux
from agent.error_classifier import FailoverReason

PATHS = ["sync", "async", "stream"]


class ProviderError(Exception):
    def __init__(self, message, *, status=None, code="", status_attr="status_code"):
        super().__init__(message)
        if status is not None:
            setattr(self, status_attr, status)
        self.body = {"error": {"code": code, "message": message}}
        self.response = SimpleNamespace(headers={})


def _answer():
    message = SimpleNamespace(role="assistant", content="YES", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")], usage=None)


class Wire:
    """A chat-completions client: each create pops the next scripted outcome."""

    def __init__(self, outcomes):
        self.outcomes, self.sent = outcomes, []
        self.chat = SimpleNamespace(completions=self)

    def _next(self, kwargs):
        self.sent.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def create(self, **kwargs):
        return self._next(kwargs)


class AsyncWire(Wire):
    async def create(self, **kwargs):
        return self._next(kwargs)


@pytest.fixture
def route(monkeypatch):
    """One class route over a scripted wire; ``state.outcomes`` is what each request returns."""
    state = SimpleNamespace(prepared=[], outcomes=[], pool=Mock(), runtime={})
    state.pool.current.return_value = SimpleNamespace(id="account-a")
    state.pool.mark_exhausted_and_rotate.return_value = SimpleNamespace(id="account-b")

    def configure(provider, base_url=""):
        state.runtime = {"provider": provider, "model": "fast-model", "api_mode": "chat_completions",
                         "base_url": base_url, "credential_pool": state.pool}

    def prepare(task, options, request, failed):
        state.prepared.append(list(failed))
        if failed:
            raise routing.ModelRoutingError("no other class member")
        return state.runtime, Wire(state.outcomes), {"model": "fast-model", "messages": [{"role": "user", "content": "q"}]}

    def relay_sync(client, kwargs, *, provider=None, api_mode=None, create):
        return create(kwargs)

    async def relay_async(client, kwargs, *, provider=None, api_mode=None, create):
        return await create(kwargs)

    def relay_stream(client, kwargs, *, provider=None, api_mode=None):
        yield client.chat.completions.create(**kwargs)

    monkeypatch.setattr(routing, "_class_request", lambda *args: object())
    monkeypatch.setattr(routing, "_prepare", prepare)
    monkeypatch.setattr(aux, "_relay_sync_completion", relay_sync)
    monkeypatch.setattr(aux, "_relay_async_completion", relay_async)
    monkeypatch.setattr(aux, "_relay_sync_stream", relay_stream)
    monkeypatch.setattr(aux, "_to_async_client", lambda client, model: (AsyncWire(client.outcomes), model))
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)
    monkeypatch.setattr(aux, "_close_cached_client", lambda client, **kw: None)
    state.configure = configure
    return state


def _run(path):
    # An active progress hook streams the request, so upstream classifies a refused
    # negotiation itself (``_ClassifiedAttempt``) on the provider it is handed.
    with aux.aux_progress_hook(lambda: None):
        if path == "async":
            return asyncio.run(routing.class_routed_async(Mock())())
        result = routing.class_routed_sync(Mock())(stream=path == "stream")
        return list(result) if path == "stream" else result


def _rotation(route):
    assert route.pool.mark_exhausted_and_rotate.call_count == 1
    return route.pool.mark_exhausted_and_rotate.call_args.kwargs


def _usage_window():
    return ProviderError("Your included Hermes usage for today is used up and you have no top-up credits.",
                         status=402, code="usage_window_and_credits_exhausted")


@pytest.mark.parametrize("path", PATHS)
def test_usage_window_402_is_judged_on_the_routed_provider_and_benches_only_the_model(route, path):
    route.configure("nous")  # no Nous base URL: only the routed provider names the backend
    route.outcomes[:] = [_usage_window(), _answer()]
    assert _run(path)
    call = _rotation(route)
    assert call["failure_reason"] == "usage_window"
    assert call["status_code"] == 402 and call["error_context"]["status_code"] == 402
    assert call["credential_id"] == "account-a" and call["model"] == "fast-model"
    assert route.prepared == [[], []]  # recovered on the same route, no other member


@pytest.mark.parametrize("path", PATHS)
def test_unverified_billing_is_sized_as_transient(route, path):
    # Anthropic's "out of extra usage" 400 can be a content filter on a healthy account (#82154).
    route.configure("anthropic")
    route.outcomes[:] = [ProviderError("You're out of extra usage. Add more at claude.ai/settings/usage and keep going.",
                                       status=400), _answer()]
    assert _run(path)
    call = _rotation(route)
    assert call["failure_reason"] == "billing_unverified" and call["status_code"] == 400


@pytest.mark.parametrize("path", PATHS)
def test_rotation_status_is_the_verdicts_not_the_reason_default(route, path):
    # The status rides on ``.status`` (no ``.status_code``); the 402's wording makes it a rate limit.
    route.configure("anthropic")
    route.outcomes[:] = [ProviderError("Usage limit reached, try again in 5 minutes.", status=402,
                                       status_attr="status"), _answer()]
    assert _run(path)
    call = _rotation(route)
    assert call["failure_reason"] == "rate_limit"
    assert call["status_code"] == 402 and call["error_context"]["status_code"] == 402


def _hook_verdict(monkeypatch):
    """A provider plugin: rotate the key, never another provider."""
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_error_classification", lambda **kw: {
        "reason": FailoverReason.rate_limit, "should_rotate_credential": True, "should_fallback": False})


@pytest.mark.parametrize("path", PATHS)
def test_rotate_without_fallback_still_rotates_the_pool(route, monkeypatch, path):
    _hook_verdict(monkeypatch)
    route.configure("anthropic")
    route.outcomes[:] = [ProviderError("slow down", status=429), _answer()]
    assert _run(path)
    assert _rotation(route)["failure_reason"] == "rate_limit"
    assert route.prepared == [[], []]


@pytest.mark.parametrize("path", PATHS)
def test_rotate_without_fallback_never_reaches_another_member(route, monkeypatch, path):
    _hook_verdict(monkeypatch)
    route.pool.mark_exhausted_and_rotate.return_value = None  # no other account
    route.configure("anthropic")
    error = ProviderError("slow down", status=429)
    route.outcomes[:] = [error]
    with pytest.raises(ProviderError) as raised:
        _run(path)
    assert raised.value is error
    assert route.prepared == [[]]  # the route was never excluded for another provider


@pytest.mark.parametrize("path", PATHS)
def test_fallback_verdict_reaches_another_member_once_the_account_is_spent(route, path):
    route.configure("anthropic")
    route.pool.mark_exhausted_and_rotate.return_value = None
    route.outcomes[:] = [ProviderError("Rate limit exceeded for this account", status=429)]
    with pytest.raises(routing.ModelRoutingError, match="no other class member"):
        _run(path)
    assert route.prepared == [[], [("anthropic", "fast-model")]]
