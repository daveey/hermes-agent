"""A sticky exact pin whose own credential is briefly unselectable is re-checked, not refused.

Mini bb-aux, 2026-11-05 04:42 PT (self-heal agent-36e9ac084a): an api_server session pinned to gpt-6-luna got
a second request 40 ms after the first. The fresh agent build re-resolved the pin while the pool had no
selectable entry (``select()`` returned None: a concurrent single-use-token refresh fails silently), so the
pin's own route left the candidate list and the allocator refused with the generic "Exact pinned route cannot
fail over". The pool was healthy again a moment later. [daveey]
"""
import logging

import pytest

from agent.model_routing import ModelRoutingError
from agent.model_routing_runtime import resolve_class_runtime
from tests.hermes_cli import test_model_routing_runtime

routing_home = test_model_routing_runtime.routing_home


def _allocate_pin(cfg, session):
    """First build: the session's exact pin to the class's only route, written to the routing store."""
    rt = resolve_class_runtime("class:deep", "auto", config=cfg, session_id=session, exact_pin=True,
                               pin_source="explicit")
    assert rt["model_routing"]["exact_pin"] is True
    return rt


def _flaky(monkeypatch, method, misses):
    """``CredentialPool.<method>`` misses ``misses`` times across all pools, then behaves normally."""
    from agent.credential_pool import CredentialPool
    real = getattr(CredentialPool, method)
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= misses:
            return False if method == "has_available" else None
        return real(self, *args, **kwargs)

    monkeypatch.setattr(CredentialPool, method, flaky)
    return calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


@pytest.mark.parametrize("method", ["select", "has_available"])
def test_pinned_credential_briefly_unselectable_succeeds_on_recheck(routing_home, monkeypatch, method):
    first = _allocate_pin(routing_home, "api-session")
    calls = _flaky(monkeypatch, method, misses=1)
    second = resolve_class_runtime("class:deep", "auto", config=routing_home, session_id="api-session",
                                   exact_pin=True, pin_source="explicit")
    assert calls["n"] >= 2  # the miss happened, then the reloaded pool was asked again
    assert (second["provider"], second["model"]) == (first["provider"], first["model"])
    assert second["model_routing"]["exact_pin"] is True


@pytest.mark.parametrize("method,reason", [("select", "select() returned no credential"),
                                           ("has_available", "no available credential")])
def test_pinned_credential_still_missing_raises_typed_reason(routing_home, monkeypatch, caplog, method, reason):
    from agent.model_routing_runtime import PinnedRouteUnavailable
    _allocate_pin(routing_home, "api-session")
    _flaky(monkeypatch, method, misses=10_000)
    with caplog.at_level(logging.WARNING, logger="agent.model_routing_runtime"):
        with pytest.raises(PinnedRouteUnavailable) as caught:
            resolve_class_runtime("class:deep", "auto", config=routing_home, session_id="api-session",
                                  exact_pin=True, pin_source="explicit")
    error = caught.value
    assert isinstance(error, ModelRoutingError)
    assert "cannot fail over" not in str(error)
    assert "anthropic/anthropic-deep" in str(error) and reason in str(error)
    assert (error.provider, error.model, error.reason) == ("anthropic", "anthropic-deep", reason)
    assert any("api-session" in r.getMessage() and "anthropic/anthropic-deep" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


def test_recheck_is_bounded_to_one_retry(routing_home, monkeypatch):
    from agent.model_routing_runtime import PinnedRouteUnavailable
    _allocate_pin(routing_home, "api-session")
    calls = _flaky(monkeypatch, "select", misses=10_000)
    with pytest.raises(PinnedRouteUnavailable):
        resolve_class_runtime("class:deep", "auto", config=routing_home, session_id="api-session",
                              exact_pin=True, pin_source="explicit")
    assert calls["n"] == 2  # the first build's miss and exactly one re-check
    from agent import model_routing_runtime
    assert 0 < model_routing_runtime.PIN_RECHECK_DELAY_SECONDS <= 2


def test_unpinned_class_route_is_not_rechecked(routing_home, monkeypatch):
    """A class selection that loses its only route still refuses as before: the re-check is the pin's alone."""
    resolve_class_runtime("class:deep", "auto", config=routing_home, session_id="class-session")
    calls = _flaky(monkeypatch, "select", misses=10_000)
    with pytest.raises(ModelRoutingError) as caught:
        resolve_class_runtime("class:deep", "auto", config=routing_home, session_id="class-session")
    assert calls["n"] == 1
    assert type(caught.value) is ModelRoutingError
