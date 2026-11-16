"""H4: consume a holder publication before the ordinary five-second poll. [daveey]"""
import contextlib

import pytest
from types import SimpleNamespace

from tests.tui_gateway import test_kanban_remote_index_defer as fixtures
from tui_gateway import kanban_remote_index as kri
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import server as sn

own = fixtures.own
_fresh_memos = fixtures._fresh_memos


def test_deferred_reader_consumes_publication_before_next_churn(own, monkeypatch):
    replica, ledger = own
    fixtures._lease()
    assert kri.refresh(replica, ledger, now=fixtures.NOW)
    fixtures._poll()
    clock = [100.0]
    monkeypatch.setattr(kri, "_clock", lambda: clock[0])
    monkeypatch.setattr(sn, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(krn.time, "time", lambda: fixtures.NOW)
    fixtures._lease()  # heartbeat and reader wall clocks must agree
    monkeypatch.setattr(sn, "_session_lineage", lambda key: ())
    monkeypatch.setattr(sn, "_kb_gateway_owns", lambda session: False)
    from hermes_cli import kanban_db
    monkeypatch.setattr(kanban_db, "list_boards", lambda **kw: [])
    monkeypatch.setattr(sn, "_kb_stray_delegate_keys", lambda *a: ())
    session = {"session_key": fixtures.CHAT}
    fixtures._add(replica, fixtures._done("t_new"), 50)
    computes = fixtures._count_computes(monkeypatch)
    assert sn._collect_kanban_notifications(session, []) == []
    assert session.get("_kanban_remote_retry_at") == 101.0
    clock[0] = 101.0
    assert kri.refresh(replica, ledger, now=fixtures.NOW)
    computes.clear()  # holder's work is not reader work
    clock[0] = 102.0
    monkeypatch.setattr(kanban_db, "list_boards", lambda **kw: (_ for _ in ()).throw(AssertionError("local scan")))
    claims = []
    texts = sn._collect_kanban_notifications(session, claims, remote_only=True)
    assert [c["remote"]["ref"] for c in claims] == ["hub/default/default/t_new"]
    assert len(texts) == 1 and "t_new" in texts[0]
    assert not computes
    clock[0] = 105.0
    fixtures._add(replica, fixtures._done("t_newer"), 51)
    assert sn._collect_kanban_notifications(session, [], remote_only=True) == []
    assert not computes  # the genuine retry hit ended the previous miss episode


def test_remote_collector_failure_cancels_callback_retry(own, monkeypatch):
    session = {"session_key": fixtures.CHAT}
    failures = []
    monkeypatch.setattr(sn, "_session_lineage", lambda key: ())
    monkeypatch.setattr(sn, "_notif_log_failure", lambda *args: failures.append(args))

    def collect(*args, on_defer, **kwargs):
        on_defer()
        assert "_kanban_remote_retry_at" in session
        raise RuntimeError("collector failed after deferring")

    monkeypatch.setattr(krn, "collect", collect)
    assert sn._collect_remote_kanban_notifications(fixtures.CHAT, [], session=session) == []
    assert "_kanban_remote_retry_at" not in session
    assert len(failures) == 1 and "collector failed" in str(failures[0][1])


@pytest.mark.parametrize("outcome", ["empty_hit", "defer", "mixed_ok", "binding", "release_error",
                                    "refused", "_closing", "_finalized", "_lease_taken_over", "exception"])
def test_poll_attempt_keeps_only_valid_deferral(monkeypatch, outcome):
    from tui_gateway import notification_idle, kanban_claim_custody
    session = {"session_key": fixtures.CHAT, "_kanban_remote_retry_at": 99.0}
    monkeypatch.setattr(sn, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(notification_idle, "collection_home", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(kanban_claim_custody, "release_expired", lambda *a: False)
    released = []

    def collect(session):
        assert "_kanban_remote_retry_at" not in session  # consume before entering collection
        if outcome == "empty_hit":
            return [], []
        session["_kanban_remote_retry_at"] = 101.0
        return ([], []) if outcome == "defer" else (["local"], [{"local": True}])

    def binding(session):
        if outcome in ("binding", "release_error"):
            raise RuntimeError("binding failed")
        return contextlib.nullcontext()

    def release(session):
        released.extend(session.pop("_kanban_claims", []))
        if outcome == "release_error":
            raise RuntimeError("release failed")

    def dispatch(sid, session, prepared):
        if outcome == "refused":
            session["_kanban_refused_until"] = 115.0
        elif outcome.startswith("_"):
            session[outcome] = True
        elif outcome == "exception":
            raise RuntimeError("dispatch failed")

    monkeypatch.setattr(sn, "_notif_collect_kanban", collect)
    monkeypatch.setattr(sn, "_session_profile_runtime_scope", binding)
    monkeypatch.setattr(sn, "_kb_release_claims", release)
    monkeypatch.setattr(sn, "_notif_poll_kanban_scoped", dispatch)
    if outcome in ("exception", "release_error"):
        with pytest.raises(RuntimeError):
            sn._notif_poll_kanban("sid", session)
    else:
        sn._notif_poll_kanban("sid", session)
    assert ("_kanban_remote_retry_at" in session) == (outcome in ("defer", "mixed_ok"))
    if outcome in ("binding", "release_error"):
        assert released == [{"local": True}]


@pytest.mark.parametrize("stop_kind", ["stop", "exception"])
def test_scheduler_remote_retry_does_not_shift_normal_work(monkeypatch, stop_kind):
    import queue
    from tui_gateway.notification_wait import PollerStop
    from tools import async_delegation, process_registry, process_registry_awaiting
    from tui_gateway import producer_admission, notification_park
    clock = [100.0]
    q = queue.Queue()
    stop = PollerStop(q)
    session = {"session_key": "idle"}
    registry = SimpleNamespace(completion_queue=q, restore_completions=lambda: None,
                               has_parked_events=lambda: False)
    monkeypatch.setattr(process_registry, "process_registry", registry)
    monkeypatch.setattr(async_delegation, "maybe_sweep_orphaned_completions", lambda q: 0)
    monkeypatch.setattr(notification_park, "has_parked", lambda: False)
    monkeypatch.setattr(sn, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls, producers, loops, waits, releases = [], [], [], [], []
    monkeypatch.setattr(producer_admission, "deliver_pending", lambda *a: producers.append(clock[0]))
    monkeypatch.setattr(sn, "_maybe_fire_tui_loop_tick", lambda *a: loops.append(clock[0]))
    for name in ("_maybe_fire_tui_heartbeat_tick", "_notif_take_parked", "_notif_handle_ready"):
        monkeypatch.setattr(sn, name, lambda *a: None)
    monkeypatch.setattr(sn, "_kb_release_claims", lambda *a: releases.append(clock[0]))

    def poll(sid, session, *, remote_only=False):
        calls.append((clock[0], remote_only))
        if clock[0] == 100.0:
            session["_kanban_remote_retry_at"] = 101.0
        elif remote_only:
            assert "_kanban_remote_retry_at" not in session
            # Also exercise an equal retry + regular deadline: regular must win.
            session["_kanban_remote_retry_at"] = 105.0
            if stop_kind == "exception":
                raise RuntimeError("retry failed")
        else:
            stop.set()

    def take(q, timeout, **kw):
        if not stop.is_set():
            waits.append(timeout)
            if len(waits) >= 5:
                stop.set()
                pytest.fail("retry spin")
            clock[0] += timeout
        return []

    monkeypatch.setattr(sn, "_notif_poll_kanban", poll)
    monkeypatch.setattr(process_registry_awaiting, "take_ready", take)
    if stop_kind == "exception":
        with pytest.raises(RuntimeError, match="retry failed"):
            sn._notification_poller_scoped_loop(stop, "sid", session)
        assert calls == [(100.0, False), (101.0, True)]
    else:
        sn._notification_poller_scoped_loop(stop, "sid", session)
        assert calls == [(100.0, False), (101.0, True), (105.0, False)]
        assert producers == [100.0, 105.0] and loops == [100.0, 105.0]
    assert waits[0] == 1.0
    assert "_kanban_remote_retry_at" not in session
    assert releases  # teardown still releases claims on exceptional exit
