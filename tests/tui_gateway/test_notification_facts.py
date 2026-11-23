"""The shared fact ring wakes actions; it never owns their durable progress."""
import builtins
import sqlite3

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_fake import FakeStore


SCOPE = Scope("install", "profile")


def ready_siblings(store):
    for name in ("sessions", "panel", "annotations", "board"):
        store.bind_consumer(name, SCOPE, lambda *_: None)
        store.report_ready(name, SCOPE)


def test_latch_changes_scheduling_not_executor_or_cursor(monkeypatch):
    from tui_gateway.notification_facts import NotificationFacts
    store = FakeStore()
    ready_siblings(store)
    wakes = []
    owner = NotificationFacts(store, SCOPE)
    action = owner.add_action(lambda: wakes.append("wake"))
    durable = {"cursor": 17, "lease": "owner", "receipt": "committed"}
    assert action.due(legacy_due=True)
    owner.report_ready()
    store.activate(SCOPE)
    store.drain()
    assert action.due(legacy_due=False)  # startup drains the SAME durable cursor
    assert not action.due(legacy_due=True)  # old periodic poll is stopped

    def forbidden(*args, **kwargs):
        raise AssertionError("projection wake performed I/O")

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", forbidden)
        m.setattr(builtins, "open", forbidden)
        store.publish([Fact(FactKey(SCOPE, "SourceState", "remote/notice-ledger"), {"revision_token": 2})])
        store.drain()
        assert action.due(legacy_due=False)
        assert not action.due(legacy_due=False)
    store.rollback(SCOPE)
    assert action.due(legacy_due=True)
    assert durable == {"cursor": 17, "lease": "owner", "receipt": "committed"}
    owner.close()


def test_foreign_scope_does_not_wake_and_overflow_coalesces():
    from tui_gateway.notification_facts import NotificationFacts
    store = FakeStore(ring_revisions=1, subscriber_envelopes=1)
    ready_siblings(store)
    owner = NotificationFacts(store, SCOPE)
    action = owner.add_action(lambda: None)
    owner.report_ready()
    store.activate(SCOPE)
    store.drain()
    assert action.due(legacy_due=False)
    foreign = Scope("other-install", "profile")
    store.publish([Fact(FactKey(foreign, "SourceState", "notices"), 1)])
    store.drain()
    assert not action.due(legacy_due=True)
    # Ring/queue overflow coalesces to one wake, not a stream of actions.
    for index in range(3):
        store.publish([Fact(FactKey(SCOPE, "SourceState", "notices"), index)])
    store.drain()
    assert action.due(legacy_due=False)
    assert not action.due(legacy_due=True)
    owner.close()


def test_late_callbacks_cannot_wake_after_rollback_or_close():
    from tui_gateway.notification_facts import NotificationFacts
    store = FakeStore()
    ready_siblings(store)
    owner = NotificationFacts(store, SCOPE)
    wakes = []
    action = owner.add_action(lambda: wakes.append(1))
    owner.report_ready()
    store.activate(SCOPE)
    callback = owner.subscription.callback
    delta = store.changes(SCOPE, None)
    store.rollback(SCOPE)
    before = list(wakes)
    callback(delta)
    assert wakes == before
    assert action.due(legacy_due=True)
    owner.close()
    before = list(wakes)
    callback(delta)
    assert wakes == before
    assert not action.due(legacy_due=True)


def test_profile_binding_lookup_is_memory_only_and_rejects_duplicate(monkeypatch):
    import pytest
    from tui_gateway import notification_facts as nf
    store = FakeStore()
    home = "/already/canonical/profile"
    owner = nf.bind(store, SCOPE, home)
    try:
        def forbidden(*args, **kwargs):
            raise AssertionError("profile lookup performed I/O")
        with monkeypatch.context() as m:
            m.setattr(builtins, "open", forbidden)
            m.setattr(sqlite3, "connect", forbidden)
            assert nf.for_home(home) is owner
            assert nf.for_home("/other/profile") is None
        with pytest.raises(ValueError, match="already bound"):
            nf.bind(store, SCOPE, home)
    finally:
        owner.close()
    assert nf.for_home(home) is None


def test_install_binding_watch_catches_creation_and_close_without_io(monkeypatch):
    from tui_gateway import notification_facts as nf
    wakes = []
    watch = nf.observe_bindings(lambda: wakes.append(1))
    owner = None
    try:
        assert watch.snapshot() == ()
        assert wakes == [1]  # atomic catch-up wakes even when initially empty
        owner = nf.bind(FakeStore(), SCOPE, "/canonical/second")
        assert watch.snapshot() == (("/canonical/second", owner),)
        assert len(wakes) == 2
        def forbidden(*args, **kwargs):
            raise AssertionError("binding census performed I/O")
        with monkeypatch.context() as m:
            m.setattr(builtins, "open", forbidden)
            m.setattr(sqlite3, "connect", forbidden)
            assert watch.snapshot() == (("/canonical/second", owner),)
        owner.close()
        owner = None
        assert watch.snapshot() == ()
        assert len(wakes) == 3
        watch.close()
        owner = nf.bind(FakeStore(), SCOPE, "/canonical/second")
        assert len(wakes) == 3
    finally:
        watch.close()
        if owner is not None:
            owner.close()

