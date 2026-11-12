"""Headless notice turns must not monopolize Desktop's writer slot."""
from types import SimpleNamespace
import threading

import pytest

from tui_gateway import server, turn_admission

_REAL_SUBMIT = server._notif_submit
_REAL_TEARDOWN = server._teardown_popped_session


@pytest.fixture
def wake(monkeypatch, tmp_path):
    from hermes_cli.active_sessions import active_session_registry_snapshot
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_load_cfg", lambda: {"max_concurrent_sessions": 100})
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)
    monkeypatch.setattr(server, "_readopt_wake_profiles", lambda: [None])
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda *a: None)
    monkeypatch.setattr(turn_admission, "owns", lambda s: True)
    monkeypatch.setattr(server, "_notif_claim_turn", lambda s: s.update(running=True) or True)
    monkeypatch.setattr(server, "_session_pending_kind", lambda sid: None)
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda *a: False)
    monkeypatch.setattr(server, "_session_owns_live_wakeup_schedule", lambda s: False)
    monkeypatch.setattr(server, "_session_owns_pending_process_notification", lambda s: False)
    monkeypatch.setattr(server, "_session_owns_running_process", lambda s: False)
    ready = threading.Event()
    ready.set()
    session = {"history_lock": threading.Lock(), "running": False, "agent_ready": ready,
               "session_key": "headless-wake", "profile_home": str(tmp_path),
               "transport": server._stdio_transport, "agent": SimpleNamespace()}
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda *a, **k:
                        ("live", session) if "live" in server._sessions else None)
    def resume(*args):
        server._sessions["live"] = session
        return {"result": {"session_id": "live"}}
    monkeypatch.setitem(server._methods, "session.resume", resume)
    def claim(sid, s):
        if s.get("active_session_lease") is None:
            lease, error = server._claim_active_session_slot(s["session_key"], live_session_id=sid,
                                                           profile_home=tmp_path)
            assert error is None
            s["active_session_lease"] = lease
    monkeypatch.setattr(server, "_ensure_active_session_slot", claim)
    monkeypatch.setattr(server, "_notif_submit", lambda *a, **k: True)
    closed = []
    def teardown(s, *, end_reason):
        if s is not None:
            closed.append(end_reason)
            server._release_active_session_slot(s)
        return s is not None
    monkeypatch.setattr(server, "_teardown_popped_session", teardown)
    timers = []
    class Timer:
        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            pass
    # Patch only the cleanup scheduler, not process-global threading.
    from tui_gateway import detached_turn_cleanup
    monkeypatch.setattr(detached_turn_cleanup, "Timer", Timer)
    clock = [100.0]
    monkeypatch.setattr(detached_turn_cleanup, "monotonic", lambda: clock[0])
    def finish():
        turn_admission.settle_prompt_state(session, SimpleNamespace(agent=session["agent"], error_retained=False))
    def snapshot():
        return active_session_registry_snapshot(registry_home=tmp_path)
    yield session, finish, timers, clock, closed, snapshot
    server._release_active_session_slot(session)


def test_headless_stdio_is_dead(monkeypatch):
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)
    assert server._transport_is_dead(server._stdio_transport)
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", True)
    assert not server._transport_is_dead(server._stdio_transport)


def test_wake_releases_registry_after_grace(wake):
    session, finish, timers, clock, closed, snapshot = wake
    assert server._wake_session_turn("headless-wake", "notice") == "started"
    assert session["active_session_lease"] is not None
    finish()
    assert not closed
    clock[0] += 60
    timers[-1].fn()
    assert closed == ["ws_orphan_reap"]
    assert snapshot() == []
    import subprocess
    import sys
    from tools.environments.local import build_subprocess_env
    child = subprocess.run(
        [sys.executable, "-c", "from tui_gateway import server; "
         "server._load_cfg = lambda: {}; "
         "lease, error = server._claim_active_session_slot('headless-wake', "
         "live_session_id='other-process', profile_home=__import__('sys').argv[1]); "
         "assert error is None, error; lease.release()", session["profile_home"]],
        env=build_subprocess_env(), capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert child.returncode == 0, child.stderr


def test_rpc_stdio_keeps_lease(wake, monkeypatch):
    session, finish, timers, _clock, closed, _snapshot = wake
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", True)
    assert server._wake_session_turn("headless-wake", "notice") == "started"
    finish()
    assert not timers and not closed
    assert session["active_session_lease"] is not None


@pytest.mark.parametrize("guard", ["_session_owns_running_process", "_session_has_active_delegations",
                                  "_session_owns_live_wakeup_schedule", "_session_pending_kind"])
def test_live_work_keeps_session_but_releases_idle_slot(wake, monkeypatch, guard):
    session, finish, timers, clock, closed, _snapshot = wake
    monkeypatch.setattr(server, guard, lambda *a: True)
    assert server._wake_session_turn("headless-wake", "notice") == "started"
    finish()
    assert session.get("active_session_lease") is None
    clock[0] += 60
    timers[-1].fn()
    assert not closed and server._sessions["live"] is session


def test_two_notices_ten_seconds_apart_reuse_session(wake):
    session, finish, timers, clock, closed, _snapshot = wake
    assert server._wake_session_turn("headless-wake", "first") == "started"
    finish()
    first = timers[-1]
    clock[0] += 10
    assert server._wake_session_turn("headless-wake", "second") == "started"
    assert server._sessions["live"] is session
    finish()
    clock[0] += 50
    first.fn()
    assert not closed
    clock[0] += 10
    timers[-1].fn()
    assert closed == ["ws_orphan_reap"]


@pytest.mark.parametrize("change", ["running", "reattached", "removed"])
def test_old_timer_cannot_close_changed_owner(wake, change):
    session, finish, timers, clock, closed, _snapshot = wake
    server._wake_session_turn("headless-wake", "first")
    finish()
    if change == "running":
        session["running"] = True
    elif change == "reattached":
        session["transport"] = SimpleNamespace(_closed=False)
    else:
        server._sessions.pop("live")
    clock[0] += 60
    timers[-1].fn()
    assert not closed


def test_early_timer_grants_remaining_grace(wake):
    _session, finish, timers, clock, closed, _snapshot = wake
    server._wake_session_turn("headless-wake", "first")
    finish()
    clock[0] += 10
    timers[-1].fn()
    assert not closed and timers[-1].delay == 50
    clock[0] += 50
    timers[-1].fn()
    assert closed == ["ws_orphan_reap"]


def test_finished_background_work_eventually_reaps(wake, monkeypatch):
    _session, finish, timers, clock, closed, _snapshot = wake
    busy = [True]
    monkeypatch.setattr(server, "_session_owns_running_process", lambda s: busy[0])
    server._wake_session_turn("headless-wake", "first")
    finish()
    clock[0] += 60
    timers[-1].fn()
    assert not closed
    busy[0] = False
    clock[0] += 60
    timers[-1].fn()
    assert closed == ["ws_orphan_reap"]


def test_ws_disconnect_policy_is_not_shortened(wake):
    session, finish, timers, _clock, closed, _snapshot = wake
    session["transport"] = server._detached_ws_transport
    server._wake_session_turn("headless-wake", "first")
    finish()
    assert not timers and not closed


def test_repair_arms_missing_timer_without_resetting_grace(wake):
    from tui_gateway.detached_turn_cleanup import repair
    session, _finish, timers, clock, closed, _snapshot = wake
    server._wake_session_turn("headless-wake", "first")
    session["running"] = False
    repair()
    assert len(timers) == 1
    clock[0] += 10
    repair()
    assert len(timers) == 1
    clock[0] += 50
    timers[-1].fn()
    assert closed == ["ws_orphan_reap"]


def test_real_notice_turn_and_teardown_release_lease(wake, monkeypatch):
    session, _finish, timers, clock, _closed, snapshot = wake
    turns = []
    session.update(history=[], history_version=0, attached_images=[], image_counter=0,
                   cols=80, slash_worker=None, show_reasoning=False, tool_progress_mode="all")
    session["agent"] = SimpleNamespace(
        session_id="headless-wake", clear_interrupt=lambda: None,
        run_conversation=lambda *a, **k: turns.append(a) or {"final_response": "done"})
    monkeypatch.setattr(server, "_notif_submit", _REAL_SUBMIT)
    monkeypatch.setattr(server, "_teardown_popped_session", _REAL_TEARDOWN)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda s: True)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda s: None)
    assert server._wake_session_turn("headless-wake", "notice") == "started"
    session["_run_thread"].join(10)
    assert not session["_run_thread"].is_alive()
    assert turns and not session["running"]
    assert timers
    clock[0] += 60
    timers[-1].fn()
    assert "live" not in server._sessions
    assert snapshot() == []


@pytest.mark.parametrize("callback", ["settle", "timer"])
def test_cleanup_does_not_hold_registry_while_waiting_for_history(wake, callback):
    from tui_gateway.detached_turn_cleanup import settled
    session, finish, timers, clock, _closed, _snapshot = wake
    server._wake_session_turn("headless-wake", "first")
    finish()
    clock[0] += 60
    attempted = threading.Event()
    lock = threading.Lock()
    class ObservedLock:
        def __enter__(self):
            attempted.set()
            lock.acquire()
        def __exit__(self, *args):
            lock.release()
    session["history_lock"] = ObservedLock()
    lock.acquire()
    worker = threading.Thread(target=timers[-1].fn if callback == "timer" else lambda: settled(session))
    worker.start()
    try:
        assert attempted.wait(2)
        acquired = server._sessions_lock.acquire(timeout=2)
        if acquired:
            server._sessions_lock.release()
        assert acquired, "cleanup inverted history -> sessions lock order"
    finally:
        lock.release()
        worker.join(5)
    assert not worker.is_alive()


def test_notif_rejection_settles_clientless_lease(wake):
    session, _finish, timers, clock, closed, _snapshot = wake
    server._wake_session_turn("headless-wake", "first")
    server._notif_release_turn(session)
    clock[0] += 60
    timers[-1].fn()
    assert closed == ["ws_orphan_reap"]
