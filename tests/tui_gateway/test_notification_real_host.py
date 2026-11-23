"""Real IPC data plane; shared fake lifecycle is a test-only release authority."""
import contextlib
import queue
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_fake_lifecycle import FakeLifecycle
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_replica import Endpoint
from tests.waiting import wait_until
from tests.tui_gateway import test_session_outcome_notices as helpers

# Alias fixture to preserve the canonical disposable action store setup.
db = helpers.db


@pytest.fixture
def real_notifications(tmp_path_factory):
    from tui_gateway import notification_facts
    scope = Scope("fixture-install", "loaded")
    token = "fixture-notification-capability-0001"
    tmp_path = tmp_path_factory.mktemp("nf")
    host = FactHost(tmp_path, {token: scope}).start()
    handle = acquire(scope, endpoint=Endpoint(str(host.directory / "facts.sock"), token))
    lifecycle = FakeLifecycle()
    owner = None
    try:
        wait_until(lambda: handle._online, what="initial scoped IPC snapshot")
        owner = notification_facts.bind(handle.v1, scope, str(tmp_path), lifecycle=lifecycle)
        for name in ("sessions", "panel", "annotations", "board"):
            lifecycle.bind_consumer(name, scope, lambda *_: None)
            lifecycle.report_ready(name, scope)
        owner.report_ready()
        yield host, handle, lifecycle, owner, scope, str(tmp_path)
    finally:
        if owner is not None:
            owner.close()
        handle.close()
        host.close()


def test_loaded_loop_uses_real_scoped_replica_and_same_executor(real_notifications, monkeypatch):
    from tools import async_delegation, process_registry_awaiting
    from tui_gateway import server, notification_park, producer_admission
    from tui_gateway.notification_wait import PollerStop
    host, replica, lifecycle, owner, scope, home = real_notifications
    signalled = threading.Event()
    observer = owner.add_action(lambda: None)
    signal = owner._signal

    def delivered(token, delta):
        signal(token, delta)
        signalled.set()

    monkeypatch.setattr(owner, "_signal", delivered)
    lifecycle.activate(scope)
    assert signalled.wait(5), "initial notification subscription catchup"
    clock = [100.0]
    q = queue.Queue()
    stop = PollerStop(q)
    registry = SimpleNamespace(completion_queue=q, has_parked_events=lambda: False)
    session = {"profile_home": home, "session_key": "loaded"}
    calls = []
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(async_delegation, "maybe_sweep_orphaned_completions", lambda *_: None)
    monkeypatch.setattr(notification_park, "has_parked", lambda: False)
    monkeypatch.setattr(producer_admission, "deliver_pending", lambda *_: None)
    for name in ("_maybe_fire_tui_loop_tick", "_maybe_fire_tui_heartbeat_tick", "_notif_take_parked"):
        monkeypatch.setattr(server, name, lambda *_: None)
    monkeypatch.setattr(server, "_notif_poll_kanban", lambda *_: calls.append(clock[0]))
    turns = []

    def take(*args, **kwargs):
        turns.append(clock[0])
        assert len(turns) <= 4
        if len(turns) == 1:
            clock[0] = 105.0
        elif len(turns) == 2:
            assert calls == [100.0]
            signalled.clear()
            host.store.publish([Fact(FactKey(scope, "SourceState", "notices"), 1)])
            assert signalled.wait(5), "real IPC delta did not wake notification subscriber"
            assert replica.v1.snapshot(scope).get(FactKey(scope, "SourceState", "notices")).value == 1
            clock[0] = 106.0
        elif len(turns) == 3:
            assert calls == [100.0, 106.0]
            lifecycle.rollback(scope)
            assert owner.subscription is None
            clock[0] = 111.0
        else:
            stop.set()
        return []

    monkeypatch.setattr(process_registry_awaiting, "take_ready", take)
    try:
        server._notification_poller_run(stop, "sid", session, registry, lambda *_: None)
        assert calls == [100.0, 106.0, 111.0]
        assert owner.actions == {observer}
    finally:
        observer.close()


def test_real_replica_does_not_supply_release_authority(real_notifications):
    from tui_gateway import notification_facts
    _, replica, _, _, scope, home = real_notifications
    with pytest.raises(ValueError, match="notification lifecycle authority required"):
        notification_facts.bind(replica.v1, scope, home + "-unbound")
    assert notification_facts.for_home(home + "-unbound") is None


def test_real_replica_reset_preserves_durable_ack_boundary(real_notifications, db, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from tui_gateway import notice_fanout as fanout, notice_fanout_state as state
    from tui_gateway import notification_facts
    host, replica, lifecycle, owner, scope, home = real_notifications
    signalled = threading.Event()
    action = owner.add_action(lambda: None)
    delivered_deltas = []
    signal = owner._signal

    def delivered(token, delta):
        signal(token, delta)
        delivered_deltas.append(delta)
        signalled.set()

    monkeypatch.setattr(owner, "_signal", delivered)
    opener = helpers._opener(db)
    tid = helpers._card(done=False)
    with contextlib.closing(kbc.connect()) as conn:
        kb.block_task(conn, tid, reason="registry unavailable")
    helpers.ku.sweep_once(set(), now=helpers.LATER, session_db=opener)
    [notice] = helpers._notices(db)
    fanout.card_change_pass(session_db=opener)
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    assert before is not None
    lifecycle.activate(scope)
    assert signalled.wait(5), "initial catchup callback not delivered"
    assert action.due(legacy_due=False)
    delivered_deltas.clear()
    signalled.clear()
    key = FactKey(scope, "SourceState", "board/events")
    # A real oversized IPC reset, not FakeStore.drain or a direct callback.
    host.store.publish([Fact(key, "x" * (5 * 1024 * 1024))])
    assert signalled.wait(10)
    assert any(delta.reset and any(f.key == key for f in delta.upserts) for delta in delivered_deltas)
    wait_until(lambda: replica.v1.snapshot(scope).get(key) is not None, what="reset applied")
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before
    assert helpers._notices(db) == [notice]
    with monkeypatch.context() as m:
        def no_io(*args, **kwargs):
            pytest.fail("notification lookup performed I/O")
        m.setattr("builtins.open", no_io)
        m.setattr(sqlite3, "connect", no_io)
        assert notification_facts.for_home(home) is owner
        assert replica.v1.snapshot(scope).get(key).value.startswith("x")
        assert action.due(legacy_due=False)
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="done")
    with monkeypatch.context() as m:
        m.setattr(state, "set_cursor", lambda *args: False)
        assert fanout.card_change_pass(session_db=opener).incomplete
    [committed] = helpers._notices(db)
    assert committed["display_metadata"]["stale"] is True
    assert state.get_cursor(ident) == before
    lifecycle.rollback(scope)
    assert owner.subscription is None
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before
    assert action.due(legacy_due=True)
    fanout.card_change_pass(session_db=opener)
    assert helpers._notices(db) == [committed]
    assert state.get_cursor(ident) > before
