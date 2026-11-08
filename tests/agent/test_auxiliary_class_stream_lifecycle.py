"""Class streams retry only before delivery and own every created client."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import auxiliary_class_routing as routing
from agent import auxiliary_client as aux


@pytest.fixture
def stream_boundary(monkeypatch):
    clients = []
    failures = []
    closed = []

    def prepare(task, options, request, failed):
        failures.append(list(failed))
        client = object()
        clients.append(client)
        runtime = {"provider": "anthropic", "model": f"deep-{len(clients)}", "api_mode": "anthropic_messages"}
        return runtime, client, {}

    monkeypatch.setattr(routing, "_class_request", lambda *args: ("class:deep", "owner", {}))
    monkeypatch.setattr(routing, "_prepare", prepare)
    # A TimeoutError is retryable on upstream's real verdict (fallback label "request timed out").
    monkeypatch.setattr(aux, "_client_streams_internally", lambda client: False)
    monkeypatch.setattr(aux, "_close_cached_client", lambda client, **kwargs: closed.append(client))
    return SimpleNamespace(clients=clients, failures=failures, closed=closed)


def test_stream_defers_client_allocation_until_consumed(stream_boundary):
    stream = routing.class_routed_sync(Mock(side_effect=AssertionError("legacy")))(stream=True)
    assert stream_boundary.clients == []
    stream.close()
    assert stream_boundary.closed == []


def test_stream_retries_same_class_before_first_chunk(stream_boundary, monkeypatch):
    def relay(client, kwargs, **route):
        if len(stream_boundary.clients) == 1:
            raise TimeoutError("before first chunk")
        yield "output"

    monkeypatch.setattr(aux, "_relay_sync_stream", relay)
    stream = routing.class_routed_sync(Mock(side_effect=AssertionError("legacy")))(stream=True)
    assert list(stream) == ["output"]
    assert stream_boundary.failures == [[], [("anthropic", "deep-1")]]
    assert stream_boundary.closed == stream_boundary.clients


def test_stream_never_replays_after_delivery(stream_boundary, monkeypatch):
    def relay(*args, **kwargs):
        yield "output"
        raise TimeoutError("after first chunk")

    monkeypatch.setattr(aux, "_relay_sync_stream", relay)
    stream = routing.class_routed_sync(Mock(side_effect=AssertionError("legacy")))(stream=True)
    assert next(stream) == "output"
    with pytest.raises(TimeoutError, match="after first chunk"):
        next(stream)
    assert len(stream_boundary.clients) == 1
    assert stream_boundary.closed == stream_boundary.clients


def test_stream_close_releases_client(stream_boundary, monkeypatch):
    monkeypatch.setattr(aux, "_relay_sync_stream", lambda *args, **kwargs: iter(["one", "two"]))
    stream = routing.class_routed_sync(Mock(side_effect=AssertionError("legacy")))(stream=True)
    assert next(stream) == "one"
    stream.close()
    assert stream_boundary.closed == stream_boundary.clients
