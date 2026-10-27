"""Review regressions: lock scope, same-operation races and failed dispatch."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.tui_gateway.test_prompt_admission import call
pytest_plugins = ['tests.tui_gateway.test_prompt_admission']
from tui_gateway import server, methods_prompt_admission as admission


def test_db_and_slot_work_never_hold_global_or_history_locks(owner, monkeypatch):
    _, session, params, _ = owner
    original = admission.admit_prompt
    ensure = server._ensure_active_session_slot

    def check():
        with ThreadPoolExecutor(1) as pool:
            def probe():
                for lock in (server._session_resume_lock, session["history_lock"]):
                    assert lock.acquire(timeout=2), "database work holds admission lock"
                    lock.release()
            pool.submit(probe).result(timeout=5)
        assert session["running"]

    def write(*args, **kwargs):
        check()
        return original(*args, **kwargs)

    def slot(*args, **kwargs):
        check()
        return ensure(*args, **kwargs)

    monkeypatch.setattr(admission, "admit_prompt", write)
    monkeypatch.setattr(server, "_ensure_active_session_slot", slot)
    assert "result" in call("prompt.admit", params)


def test_same_operation_race_returns_one_receipt(owner, monkeypatch):
    db, _, params, started = owner
    original = admission._owner_home
    barrier = threading.Barrier(2)
    seen = threading.local()

    def read(*args):
        result = original(*args)
        if not getattr(seen, "first", False):
            seen.first = True
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(admission, "_owner_home", read)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(call, "prompt.admit", params) for _ in range(2)]
        replies = [future.result(timeout=10) for future in futures]
    assert all("result" in r for r in replies), replies
    assert replies[0]["result"] == replies[1]["result"]
    assert len(started) == len(db.get_messages(params["stored_session_id"])) == 1


def test_failed_dispatch_releases_lease_retains_terminal_error(owner, monkeypatch):
    _, session, params, _ = owner
    released = []
    release = server._release_active_session_slot
    monkeypatch.setattr(server, "_release_active_session_slot", lambda s: (released.append(s), release(s)))
    monkeypatch.setattr(server, "_start_session_work", lambda *a, **kw: None)
    reply = call("prompt.admit", params)
    assert reply["result"].get("dispatch") == "cancelled"
    assert released and not session["running"]
    assert session["inflight_turn"]["error"]
    assert call("prompt.admission.get", {k: v for k, v in params.items() if k != "text"})["result"]["dispatch"] == "cancelled"


@pytest.mark.parametrize("state", ["settling", "detached"])
def test_admission_refuses_disconnecting_owner(owner, state):
    db, session, params, started = owner
    if state == "settling":
        session["_client_gone_interrupt_requested"] = True
    else:
        session["transport"] = server._detached_ws_transport
    assert "error" in call("prompt.admit", params)
    assert not started and not db.get_messages(params["stored_session_id"])


def test_admission_refreshes_activity(owner):
    _, session, params, _ = owner
    session["last_active"] = 1
    assert "result" in call("prompt.admit", params)
    assert session["last_active"] > 1


def test_normal_submit_lockin_race_queues(owner, monkeypatch):
    _, session, params, _ = owner
    sid = next(sid for sid, s in server._sessions.items() if s is session)
    original = server._lock_in_submit_turn
    first = True

    def lockin(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            assert "result" in call("prompt.admit", params)
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "_lock_in_submit_turn", lockin)
    reply = call("prompt.submit", {"session_id": sid, "text": "follow up", "queued": True})
    assert reply.get("result", {}).get("status") == "queued", reply
    assert session["_submit_user_row"]["content"] == params["text"]


def test_storage_failure_undoes_claim_and_releases_slot(owner, monkeypatch):
    _, session, params, _ = owner
    released = []
    release = server._release_active_session_slot
    monkeypatch.setattr(server, "_release_active_session_slot", lambda s: (released.append(s), release(s)))
    def fail(*args, **kwargs):
        raise RuntimeError("storage unavailable")
    monkeypatch.setattr(admission, "admit_prompt", fail)
    assert "error" in call("prompt.admit", params)
    assert not session["running"] and released
    assert not session.get("inflight_turn")


def test_cancel_during_acceptance_never_dispatches(owner, monkeypatch):
    db, session, params, started = owner
    original = admission.admit_prompt
    def cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        session["_turn_cancel_requested"] = True
        return result
    monkeypatch.setattr(admission, "admit_prompt", cancel)
    reply = call("prompt.admit", params)
    assert reply["result"]["dispatch"] == "cancelled"
    assert not started and not session["running"]
    assert db.get_messages(params["stored_session_id"]) == []


def test_failed_dispatch_status_write_failure_keeps_acceptance(owner, monkeypatch):
    _, session, params, started = owner
    monkeypatch.setattr(server, "_start_session_work", lambda *a, **kw: None)
    def fail(*args):
        raise RuntimeError("status write failed")
    monkeypatch.setattr(admission, "mark_dispatch_failed", fail)
    assert 'error' in call("prompt.admit", params)
    assert 'error' in call("prompt.admit", params)
    assert not started and session["running"]
    assert session["inflight_turn"]["error"]
