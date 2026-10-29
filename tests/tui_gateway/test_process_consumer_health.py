"""Process receipts come from the real queue consumer, not thread existence."""
import sys
import threading
from types import SimpleNamespace

import pytest

from gateway.session_context import scoped_current_session_id
from tools.process_registry import ProcessRegistry
from tui_gateway import server
from tests.waiting import wait_until


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    from tui_gateway import process_consumer_health as health
    registry = ProcessRegistry()
    new_session = registry._new_session

    def notifying_session(*args, **kwargs):
        process = new_session(*args, **kwargs)
        # Arm before the reader starts: a fast child can finish before spawn returns.
        process.notify_on_complete = True
        return process

    monkeypatch.setattr(registry, "_new_session", notifying_session)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    # Fork: upstream retired the bot-mailbox claim from the notification poller (c8ae25cd112, kept by
    # the merge), so there is no _poll_bot_live_delivery_guarded pass left to stub out.
    for name in ("_maybe_fire_tui_loop_tick",
                 "_maybe_fire_tui_heartbeat_tick", "_notif_poll_kanban", "_notif_take_parked"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    lease, error = server._claim_active_session_slot("origin", live_session_id="ui",
        surface="desktop", profile_home=tmp_path)
    assert error is None
    session = dict(profile_home=str(tmp_path), session_key="origin", history=[],
        history_lock=threading.RLock(), running=False, active_session_lease=lease,
        agent=SimpleNamespace(session_id="origin"))
    monkeypatch.setitem(server._sessions, "ui", session)
    state = {"accept": True, "submitted": threading.Event()}
    def submit(*args, **kwargs):
        session["running"] = False
        state["submitted"].set()
        return state["accept"]
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    delivered = threading.Event()
    real_delivered = health.delivered

    def observe_delivery(*args, **kwargs):
        try:
            return real_delivered(*args, **kwargs)
        finally:
            delivered.set()

    monkeypatch.setattr(health, "delivered", observe_delivery)
    stop = server._start_notification_poller("ui", session)
    def complete(*, wait_for_delivery=True):
        delivered.clear()
        with scoped_current_session_id("origin"):
            p = registry.spawn_local(f"exec {sys.executable} -c 'pass'", session_key="origin")
        assert p._completion_event.wait(60)
        assert state["submitted"].wait(60)
        # submit() wakes before its return value is consumed. A negative
        # receipt assertion must not race ahead of health.delivered().
        if wait_for_delivery:
            assert delivered.wait(60), "poller never processed the admission result"
    try:
        yield health, registry, session, state, stop, complete
    finally:
        stop.set()
        for event, thread in server._notification_pollers:
            if event is stop:
                thread.join(60)
                assert not thread.is_alive()
        lease.release()


@pytest.mark.parametrize("accepted", [True, False, None])
def test_completion_before_spawn_returns_still_reaches_consumer(consumer, tmp_path, monkeypatch, accepted):
    registry = consumer[1]
    consumer[3]["accept"] = accepted
    spawn = registry.spawn_local

    def completed_spawn(*args, **kwargs):
        process = spawn(*args, **kwargs)
        assert process._completion_event.wait(60)
        return process

    monkeypatch.setattr(registry, "spawn_local", completed_spawn)
    consumer[-1]()
    if accepted is True:
        wait_until(lambda: available(consumer, tmp_path), timeout=60)
    assert available(consumer, tmp_path) is (accepted is True)


def available(c, home):
    health, registry, *_ = c
    return health.available(home, "origin", registry.ownership_generation)


def test_only_successful_process_consumption_certifies(consumer, tmp_path):
    assert not available(consumer, tmp_path)
    consumer[-1]()
    wait_until(lambda: available(consumer, tmp_path), timeout=60)
    assert available(consumer, tmp_path)
    assert not available(consumer, tmp_path / "other")
    assert not consumer[0].available(tmp_path, "other", consumer[1].ownership_generation)
    assert not consumer[0].available(tmp_path, "origin", ProcessRegistry().ownership_generation)
    consumer[4].set()
    assert not available(consumer, tmp_path)


@pytest.mark.parametrize("accepted", [False, None])
def test_refused_turn_never_certifies(consumer, tmp_path, accepted):
    consumer[3]["accept"] = accepted
    consumer[-1]()
    assert not available(consumer, tmp_path)


@pytest.mark.parametrize("flag", ["_turn_cancel_requested", "_closing", "_finalized", "_lease_taken_over"])
def test_stop_or_teardown_invalidates(consumer, tmp_path, flag):
    consumer[-1]()
    wait_until(lambda: available(consumer, tmp_path), timeout=60)
    consumer[2][flag] = True
    assert not available(consumer, tmp_path)


def test_stale_receipt_and_notifications_off(consumer, tmp_path, monkeypatch):
    consumer[-1]()
    wait_until(lambda: available(consumer, tmp_path), timeout=60)
    (tmp_path / "config.yaml").write_text("display:\n  background_process_notifications: off\n")
    assert not available(consumer, tmp_path)
    (tmp_path / "config.yaml").unlink()
    monkeypatch.setattr(consumer[0], "MAX_AGE", -1)
    assert not available(consumer, tmp_path)


def test_released_or_missing_native_lease_invalidates(consumer, tmp_path):
    consumer[-1]()
    wait_until(lambda: available(consumer, tmp_path), timeout=60)
    consumer[2]["active_session_lease"].release()
    assert not available(consumer, tmp_path)


def test_native_stop_destroys_receipt_even_after_cancel_latch_clears(consumer, tmp_path):
    consumer[-1]()
    wait_until(lambda: available(consumer, tmp_path), timeout=60)
    server._interrupt_session_turn("ui", consumer[2])
    consumer[2]["_turn_cancel_requested"] = False
    assert not available(consumer, tmp_path)


def test_dispatch_exception_revokes_previous_success(consumer, tmp_path, monkeypatch):
    consumer[-1]()
    wait_until(lambda: available(consumer, tmp_path), timeout=60)
    def fail(*a, **k):
        consumer[3]["submitted"].set()
        raise RuntimeError("fixture admission unavailable")
    monkeypatch.setattr(server, "_run_prompt_submit", fail)
    consumer[3]["submitted"].clear()
    # Submission raises before delivered() is called. begin_delivery() must
    # already have revoked the old receipt when the failing submit is entered.
    consumer[-1](wait_for_delivery=False)
    wait_until(lambda: not available(consumer, tmp_path), timeout=60)
    assert not available(consumer, tmp_path)


def test_late_success_after_stop_cannot_resurrect_receipt(consumer, tmp_path, monkeypatch):
    def submit(*a, **k):
        consumer[2]["running"] = False
        server._interrupt_session_turn("ui", consumer[2])
        consumer[2]["_turn_cancel_requested"] = False
        consumer[3]["submitted"].set()
        return True
    done = threading.Event()
    real_delivered = consumer[0].delivered
    def observed(*a, **k):
        try:
            return real_delivered(*a, **k)
        finally:
            done.set()
    monkeypatch.setattr(consumer[0], "delivered", observed)
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    consumer[-1]()
    assert done.wait(60)
    assert not available(consumer, tmp_path)


def test_readback_does_not_race_past_revocation(consumer, tmp_path, monkeypatch):
    consumer[-1]()
    wait_until(lambda: available(consumer, tmp_path), timeout=60)
    original = consumer[0]._lease_live
    def revoke(entry):
        result = original(entry)
        consumer[0].invalidate(entry.session)
        return result
    monkeypatch.setattr(consumer[0], "_lease_live", revoke)
    assert not available(consumer, tmp_path)
