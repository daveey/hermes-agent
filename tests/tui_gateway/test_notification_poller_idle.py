"""Exercise the production poller wait and claim-custody boundaries. [daveey]"""
import contextlib
import queue
import threading

import pytest


@pytest.fixture
def poller(monkeypatch):
    from tools import async_delegation, process_registry, process_registry_awaiting
    from tui_gateway import server, producer_admission, notification_park
    from tui_gateway.notification_wait import PollerStop
    from types import SimpleNamespace

    q = queue.Queue()
    registry = SimpleNamespace(completion_queue=q, restore_completions=lambda: None,
                               has_parked_events=lambda: False)
    monkeypatch.setattr(process_registry, "process_registry", registry)
    monkeypatch.setattr(async_delegation, "maybe_sweep_orphaned_completions", lambda q: 0)
    monkeypatch.setattr(producer_admission, "deliver_pending", lambda *a: "none")
    def forbidden(owns):
        raise AssertionError("empty park scanned")
    real_claim = notification_park.claim
    monkeypatch.setattr(notification_park, "claim", forbidden)
    for name in ("_maybe_fire_tui_loop_tick", "_maybe_fire_tui_heartbeat_tick", "_notif_poll_kanban",
                 "_notif_take_parked", "_kb_release_claims"):
        monkeypatch.setattr(server, name, lambda *a: None)
    session = {"session_key": "idle", "history_lock": threading.RLock(), "_test_real_claim": real_claim}
    stop = PollerStop(q)
    waiting = threading.Event()
    delivered = threading.Event()
    timeouts = []
    received = []
    real_take = process_registry_awaiting.take_ready

    def take(q, timeout, **kwargs):
        if timeout:
            timeouts.append(timeout)
            waiting.set()
        return real_take(q, timeout, **kwargs)

    def handle(sid, session, events, *args):
        received.extend(events)
        if events:
            delivered.set()

    monkeypatch.setattr(process_registry_awaiting, "take_ready", take)
    monkeypatch.setattr(server, "_notif_handle_ready", handle)
    thread = threading.Thread(target=server._notification_poller_scoped_loop, args=(stop, "idle", session))
    thread.start()
    try:
        assert waiting.wait(2)
        yield q, stop, thread, session, timeouts, delivered, received
    finally:
        stop.set()
        thread.join(2)
        assert not thread.is_alive()


def test_idle_production_poller_waits_until_due_and_stops(poller):
    _q, stop, thread, _session, timeouts, _delivered, _received = poller
    assert timeouts[0] > 4
    stop.set()
    thread.join(1)
    assert not thread.is_alive()


def test_production_poller_queue_put_delivers_within_half_second(poller):
    q, _stop, _thread, _session, _timeouts, delivered, received = poller
    event = {"type": "completion", "session_key": "idle"}
    q.put(event)
    assert delivered.wait(0.5)
    assert received == [event]


def test_production_poller_park_publication_delivers_within_two_seconds(poller, monkeypatch):
    from tui_gateway import notification_park, server
    _q, _stop, _thread, session, _timeouts, delivered, received = poller
    monkeypatch.setattr(notification_park, "claim", session["_test_real_claim"])
    monkeypatch.setattr(server, "_session_owns_notification_event", lambda sid, s, event: True)
    event = {"type": "completion", "session_key": "idle"}
    try:
        assert notification_park.park(event)
        assert delivered.wait(2)
        assert received == [event]
    finally:
        notification_park.clear()


def test_self_rehold_does_not_invalidate_wait_generation():
    from tui_gateway import server
    from tui_gateway.notification_wait import generation
    from tools.process_registry_awaiting import release
    q = queue.Queue()
    session = {"_notif_held": [], "_notif_poller_ident": threading.get_ident()}
    before = generation(q)
    event = {"type": "completion"}
    try:
        assert server._notif_hold_for(session, event, q)
        assert generation(q) == before
    finally:
        release(server._notif_take_held(session))
        server._notif_unhold([event])


def test_collected_claims_release_on_provider_binding_failure(monkeypatch):
    from tui_gateway import server
    session = {}
    claims = [("board", "task", 1, 2)]
    monkeypatch.setattr(server, "_notif_collect_kanban", lambda s: (["notice"], claims))
    def fail(s):
        raise RuntimeError("provider binding failed")
    monkeypatch.setattr(server, "_session_profile_runtime_scope", fail)
    released = []
    def release(s):
        released.extend(s.pop("_kanban_claims"))
        s.pop("_kanban_pending")
    monkeypatch.setattr(server, "_kb_release_claims", release)
    server._notif_poll_kanban("idle", session)
    assert released == claims
    assert not session.get("_kanban_claims")
    assert not session.get("_kanban_pending")


def test_finalize_during_provider_binding_releases_prepared_claims(monkeypatch):
    from tui_gateway import server
    session = {}
    claims = [("board", "task", 1, 2)]
    monkeypatch.setattr(server, "_notif_collect_kanban", lambda s: (["notice"], claims))
    @contextlib.contextmanager
    def finalize(s):
        s["_finalized"] = True
        yield
    released = []
    monkeypatch.setattr(server, "_session_profile_runtime_scope", finalize)
    monkeypatch.setattr(server, "_kb_release_claims", lambda s: released.extend(s["_kanban_claims"]))
    server._notif_poll_kanban("idle", session)
    assert released == claims
