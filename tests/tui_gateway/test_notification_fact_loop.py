"""Fact scheduling through the production loaded-session executor."""
import queue

import pytest

from types import SimpleNamespace

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_fake import FakeStore


@pytest.mark.parametrize("fail_first", [False, True])
def test_real_loop_quiet_facts_stop_polling_and_rollback_resumes(monkeypatch, fail_first):
    from tools import async_delegation, process_registry_awaiting
    from tui_gateway import server, notification_facts, notification_park, producer_admission
    from tui_gateway.notification_wait import PollerStop

    scope = Scope("install", "loaded")
    store = FakeStore()
    owner = notification_facts.bind(store, scope, "/canonical/loaded")
    for name in ("sessions", "panel", "annotations", "board"):
        store.bind_consumer(name, scope, lambda *_: None)
        store.report_ready(name, scope)
    owner.report_ready()
    store.activate(scope)
    store.drain()
    clock = [100.0]
    q = queue.Queue()
    stop = PollerStop(q)
    registry = SimpleNamespace(completion_queue=q, has_parked_events=lambda: False)
    session = {"profile_home": "/canonical/loaded", "session_key": "loaded"}
    calls, producers = [], []
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(async_delegation, "maybe_sweep_orphaned_completions", lambda *_: None)
    monkeypatch.setattr(notification_park, "has_parked", lambda: False)
    monkeypatch.setattr(producer_admission, "deliver_pending", lambda *_: producers.append(clock[0]))
    for name in ("_maybe_fire_tui_loop_tick", "_maybe_fire_tui_heartbeat_tick", "_notif_take_parked"):
        monkeypatch.setattr(server, name, lambda *_: None)
    def poll(*_):
        calls.append(clock[0])
        if fail_first and len(calls) == 1:
            raise RuntimeError("source temporarily unavailable")
    monkeypatch.setattr(server, "_notif_poll_kanban", poll)
    expected = [100.0, 105.0] if fail_first else [100.0]
    turns = []

    def take(q, timeout, **kwargs):
        turns.append(clock[0])
        assert len(turns) <= 5
        if len(turns) == 1:
            clock[0] = 105.0
        elif len(turns) == 2:
            assert calls == expected  # producer cadence must not read the notice source
            store.publish([Fact(FactKey(scope, "SourceState", "notices"), 1)])
            store.drain()
            clock[0] = 106.0
        elif len(turns) == 3:
            assert calls == expected + [106.0]
            store.rollback(scope)
            clock[0] = 111.0
        else:
            stop.set()
        return []

    monkeypatch.setattr(process_registry_awaiting, "take_ready", take)
    try:
        server._notification_poller_run(stop, "sid", session, registry, lambda *_: None)
        assert calls == expected + [106.0, 111.0]
        assert producers == [100.0, 105.0, 111.0]
        assert not owner.actions  # teardown removes the session's callback
    finally:
        owner.close()


def test_missing_profile_binding_cannot_resume_legacy_in_facts(monkeypatch):
    from tui_gateway import notification_facts
    scope = Scope("install", "bound")
    store = FakeStore()
    owner = notification_facts.bind(store, scope, "/canonical/bound")
    for name in ("sessions", "panel", "annotations", "board"):
        store.bind_consumer(name, scope, lambda *_: None)
        store.report_ready(name, scope)
    owner.report_ready()
    try:
        assert notification_facts.for_home("/missing") is None
        store.activate(scope)
        with pytest.raises(RuntimeError, match="binding unavailable"):
            notification_facts.for_home("/missing")
        store.rollback(scope)
        assert notification_facts.for_home("/missing") is None
    finally:
        owner.close()
