"""Real session-store boundaries for retirement replay admission."""
import sqlite3
import threading
from contextlib import nullcontext
from types import SimpleNamespace

from tests.tui_gateway import test_prompt_start_lock_order as prompt_helpers

turn_env = prompt_helpers.turn_env

import pytest

from hermes_state import SessionDB
from tui_gateway.automation_replay_handoff import Journal
from tests.tui_gateway.test_automation_replay_handoff import item


@pytest.fixture
def store(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat", source="desktop")
    journal = Journal(tmp_path)
    journal.publish("chat", [item(persisted=False)])
    batch = journal.claim("chat", 100, "dead", lambda _: False)
    yield db, journal, batch
    db.close()


def delivery(journal, batch):
    from tui_gateway.automation_replay_dispatch import Delivery
    return Delivery(journal, batch)


def stage(db, journal, batch, text="original"):
    session = {"session_key": "chat", "agent": SimpleNamespace(session_id="chat")}
    pending = delivery(journal, batch)
    accepted = pending.stage(db, session, text, "process_complete", {"automation_replay": {"batch_id": batch["id"]}})
    assert accepted == session["_submit_user_row"]["content"]
    return pending, session


def test_commit_recovery_restages_same_real_row_and_original_content(store):
    db, journal, batch = store
    first, session = stage(db, journal, batch)
    recovered = journal.claim("chat", 100, "successor", lambda _: False)
    second, resumed = stage(db, journal, recovered, "new timestamp note")
    assert resumed["_submit_user_row"]["content"] == "original"
    assert resumed["_submit_user_row"]["_row_id"] == session["_submit_user_row"]["_row_id"]
    assert len(db.get_messages("chat")) == 1
    assert not first.admit()
    assert second.admit()
    assert not second.admit()


def test_admitted_receipt_recovers_without_assistant_or_second_dispatch(store):
    db, journal, batch = store
    pending, _ = stage(db, journal, batch)
    assert pending.admit()
    # Simulate death after durable worker admission but before settlement/model API.
    recovered = journal.claim("chat", 100, "successor", lambda _: False)
    assert recovered["dispatch"] == "admitted"
    assert not delivery(journal, recovered).admit()
    assert journal.settle(recovered)
    assert len(db.get_messages("chat")) == 1
    assert journal.owners() == []


def test_stop_between_row_commit_and_worker_entry_refuses_dispatch(store):
    db, journal, batch = store
    pending, _ = stage(db, journal, batch)
    journal.cancel("chat", 1e20, stop=True)
    assert not pending.admit()
    assert journal.owners() == []


def test_commit_receipt_rolls_back_real_message_and_counters(store, monkeypatch):
    db, journal, batch = store
    original = db._bump_session_counters
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash in transaction")
    monkeypatch.setattr(db, "_bump_session_counters", fail)
    with pytest.raises(RuntimeError, match="crash"):
        stage(db, journal, batch)
    assert db.get_messages("chat") == []
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT message_row_id FROM automation_replay_batches").fetchone()[0] is None
        assert conn.execute("SELECT message_count FROM sessions WHERE id='chat'").fetchone()[0] == 0


def test_wrong_profile_store_cannot_write_replay(store, tmp_path):
    _, journal, batch = store
    other = SessionDB(tmp_path / "foreign.db")
    try:
        other.create_session("chat", source="desktop")
        with pytest.raises(ValueError, match="profile"):
            stage(other, journal, batch)
        assert other.get_messages("chat") == []
    finally:
        other.close()


@pytest.mark.parametrize("stop", [False, True])
def test_real_wake_worker_entry_and_live_pending_claim(store, turn_env, monkeypatch, stop):
    from tui_gateway import server, automation_replay as ar
    from tui_gateway import wake_profiles
    from agent import memory_provider
    db, journal, batch = store
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    sid = "dispatch-worker-proof"
    session = prompt_helpers._registered_session(sid, threading.RLock(), completed)
    session.update(profile_home=str(journal.path.parent), session_key="chat", running=False)
    session["agent"].session_id = "chat"
    session["agent"]._session_db = db
    monkeypatch.setattr(server, "_session_db", lambda _: nullcontext(db))
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _: True)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_routing_provenance_db", lambda _: nullcontext(None))
    monkeypatch.setattr(server, "_reopen_routed_session_row", lambda *a: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: None)
    monkeypatch.setattr(wake_profiles, "live_for_profiles", lambda *a: (sid, session))
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    monkeypatch.setattr(ar, "retiring_backend_refuses", lambda *a: False)
    original_spawn = memory_provider.spawn_context_thread
    def delayed(target, **kwargs):
        def wait_then_run():
            entered.set()
            assert release.wait(10)
            target()
        return original_spawn(wait_then_run, **kwargs)
    monkeypatch.setattr(memory_provider, "spawn_context_thread", delayed)
    journal.release(batch)
    try:
        assert ar.consume_handoffs([journal.path.parent], now=100) == 1
        assert entered.wait(10)
        # Wake returned, but the actual PreparedWork has not entered its body.
        assert ar.consume_handoffs([journal.path.parent], now=100) == 0
        if stop:
            ar.cancel_durable(journal.path.parent, ("chat",), 1e20, stop=True)
        release.set()
        session["_run_thread"].join(10)
        assert not session["_run_thread"].is_alive()
        assert completed.is_set() is (not stop)
        assert len(db.get_messages("chat")) >= 1
        assert not session["running"]
        if not stop:
            # Worker admission is permanent even without relying on its assistant rows.
            ar.consume_handoffs([journal.path.parent], now=100)
        assert journal.owners() == []
        assert "_submit_user_row" not in session
        assert "inflight_turn" not in session or session["inflight_turn"] is None
    finally:
        release.set()
        if session.get("_run_thread") is not None:
            session["_run_thread"].join(10)
        with server._sessions_lock:
            server._sessions.pop(sid, None)
