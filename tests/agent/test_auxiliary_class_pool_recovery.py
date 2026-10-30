"""Account quota is not route exhaustion; all class entry paths rotate before failover."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import auxiliary_class_routing as routing
from agent import auxiliary_client as aux


class QuotaError(Exception):
    status_code = 429


@pytest.mark.parametrize("path", ["sync", "async", "stream"])
@pytest.mark.parametrize("succeed", [True, False])
def test_class_quota_rotates_native_pool_before_excluding_route(monkeypatch, path, succeed):
    attempts, exclusions, closed = [], [], []
    entry = SimpleNamespace(id="account-a")
    pool = Mock()
    pool.current.return_value = entry
    pool.mark_exhausted_and_rotate.return_value = SimpleNamespace(id="account-b")
    runtime = {"provider": "anthropic", "model": "fast-model", "api_mode": "anthropic_messages",
               "api_key": "test-only", "credential_pool": pool}

    def prepare(task, options, request, failed):
        exclusions.append(list(failed))
        if failed:
            raise routing.ModelRoutingError("exhausted test route")
        return runtime, SimpleNamespace(), {}

    def complete(*args, **kwargs):
        attempts.append(entry.id)
        if len(attempts) == 1 or not succeed:
            raise QuotaError("Rate limit exceeded for this account")
        return "YES"

    monkeypatch.setattr(routing, "_class_request", lambda *args: object())
    monkeypatch.setattr(routing, "_prepare", prepare)
    monkeypatch.setattr(routing, "_complete", complete)
    monkeypatch.setattr(aux, "_close_cached_client", lambda client, **kw: closed.append(client))
    monkeypatch.setattr(aux, "_to_async_client", lambda client, model: (client, model))
    monkeypatch.setattr(aux, "_validate_llm_response", lambda response, task: response)

    async def async_complete(*args, **kwargs):
        return complete()

    def stream(*args, **kwargs):
        yield complete()

    monkeypatch.setattr(aux, "_relay_async_completion", async_complete)
    monkeypatch.setattr(routing, "_stream", stream)

    def run():
        if path == "async":
            return asyncio.run(routing.class_routed_async(Mock())())
        result = routing.class_routed_sync(Mock())(stream=path == "stream")
        return list(result) if path == "stream" else result

    if succeed:
        assert run() == (["YES"] if path == "stream" else "YES")
        assert exclusions == [[], []]
    else:
        with pytest.raises(routing.ModelRoutingError, match="exhausted test route"):
            run()
        assert exclusions == [[], [], [("anthropic", "fast-model")]]
    assert len(attempts) == 2  # a pool that keeps returning the same account cannot loop
    assert pool.mark_exhausted_and_rotate.call_count == 1
    call = pool.mark_exhausted_and_rotate.call_args.kwargs
    assert call["credential_id"] == "account-a"
    assert call["model"] == "fast-model"
    assert call["failure_reason"] == "rate_limit"


def test_delivered_stream_does_not_rotate_or_replay(monkeypatch):
    pool = Mock()
    runtime = {"provider": "anthropic", "model": "fast-model", "credential_pool": pool}
    monkeypatch.setattr(routing, "_class_request", lambda *args: object())
    monkeypatch.setattr(routing, "_prepare", lambda *args: (runtime, object(), {}))

    def stream(*args):
        yield "first"
        raise QuotaError("Rate limit exceeded")

    monkeypatch.setattr(routing, "_stream", stream)
    result = routing.class_routed_sync(Mock())(stream=True)
    assert next(result) == "first"
    with pytest.raises(QuotaError):
        next(result)
    pool.mark_exhausted_and_rotate.assert_not_called()


@pytest.mark.parametrize("exc", [TimeoutError("network timeout"), QuotaError("model temporarily at capacity upstream")])
def test_non_account_failures_do_not_bench_pool(exc):
    pool = Mock()
    assert not routing._recover_class_pool({"credential_pool": pool}, exc, set())
    pool.mark_exhausted_and_rotate.assert_not_called()
