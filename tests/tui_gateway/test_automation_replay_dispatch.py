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


def test_admitted_quota_rewall_can_transfer_to_new_attempt(store):
    db, journal, batch = store
    pending, _ = stage(db, journal, batch)
    assert pending.admit()
    child = {**item("rewall", due=9000), "source_batch": batch["id"], "attempt": 2}
    journal.publish("chat", [child])
    assert journal.commit_message(batch, lambda *a: pytest.fail("retired parent wrote again")) is None
    assert not journal.settle(batch)
    assert journal.claim("chat", 100, "successor", lambda _: False) is None
    successor = journal.claim("chat", 9000, "successor", lambda _: False)
    assert successor["id"] != batch["id"]
    assert journal.discard(successor)
    assert journal.owners() == []
    assert len(db.get_messages("chat")) == 1


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


@pytest.mark.parametrize("refusal", ["foreign_lease", "compression_rotation"])
def test_foreign_live_turn_lease_refuses_staging_without_spending_budget(store, monkeypatch, refusal):
    from tui_gateway import server, automation_replay as ar
    db, journal, batch = store
    if refusal == "foreign_lease":
        assert db.try_acquire_session_turn_lease("chat", "foreign-live-turn", ttl_seconds=300)
    else:
        db.end_session("chat", "compression")
    session = {"session_key": "chat", "history_lock": threading.RLock()}
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _: True)
    monkeypatch.setattr(server, "_session_db", lambda _: nullcontext(db))
    monkeypatch.setattr(server, "_notif_release_claim", lambda *a: None)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    def wake(owner, text, **kw):
        accepted = kw["replay_delivery"].submit("rid", "sid", session, text,
                                             kw["display_kind"], kw["display_metadata"], "test")
        return "started" if accepted else "refused"
    for _ in range(ar.HANDOFF_FAILURES_MAX + 2):
        ar._run_batch(journal, batch, "chat", None, wake)
        batch = journal.claim("chat", 100, "successor", lambda _: False)
        assert batch is not None and batch.get("failures", 0) == 0
    assert db.get_messages("chat") == []


def test_real_dispatch_storage_refusal_releases_claim_without_failure_charge(store, monkeypatch):
    from tui_gateway import server, automation_replay as ar
    db, journal, batch = store
    session = {"session_key": "chat", "history_lock": threading.RLock()}
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _: True)
    monkeypatch.setattr(server, "_session_db", lambda _: nullcontext(db))
    monkeypatch.setattr(server, "_notif_release_claim", lambda *a: None)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(journal, "commit_message", unavailable)
    def wake(owner, text, **kw):
        accepted = kw["replay_delivery"].submit("rid", "sid", session, text,
                                             kw["display_kind"], kw["display_metadata"], "test")
        return "started" if accepted else "refused"
    ar._run_batch(journal, batch, "chat", None, wake)
    recovered = journal.claim("chat", 100, "successor", lambda _: True)
    assert recovered is not None and recovered["failures"] == 0


@pytest.mark.parametrize("failure", ["locked", "missing_store"])
def test_submit_storage_refusal_returns_false_and_releases_claim(store, monkeypatch, failure):
    from tui_gateway import server
    db, journal, batch = store
    session = {"session_key": "chat", "agent": SimpleNamespace(session_id="chat"),
               "history_lock": threading.RLock(), "_notif_turn_claim": object()}
    released = []
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _: failure != "missing_store")
    monkeypatch.setattr(server, "_session_db", lambda _: nullcontext(db))
    monkeypatch.setattr(server, "_notif_release_claim", lambda *args: released.append(args))
    def locked(*args, **kw):
        raise sqlite3.OperationalError("storage refused")
    monkeypatch.setattr(journal, "commit_message", locked)
    assert delivery(journal, batch).submit("rid", "sid", session, "report", "process_complete", {}, "test") is False
    assert released == [(session, session["_notif_turn_claim"])]
    assert db.get_messages("chat") == []


def test_cleanup_preserves_newer_staged_row_and_marker(store):
    db, journal, batch = store
    pending, session = stage(db, journal, batch)
    session["history_lock"] = threading.RLock()
    newer = {"role": "user", "content": "newer turn"}
    session["_submit_user_row"] = newer
    pending.cleanup()
    assert session["_submit_user_row"] is newer
    assert session["_marker_until_tool"] is True
    session["_submit_user_row"] = pending.staged
    pending.cleanup()
    assert "_submit_user_row" not in session and "_marker_until_tool" not in session


def test_submit_refusal_cleans_exact_row_and_releases_original_claim(store, monkeypatch):
    from tui_gateway import server
    db, journal, batch = store
    session = {"session_key": "chat", "agent": SimpleNamespace(session_id="chat"),
               "history_lock": threading.RLock(), "_notif_turn_claim": object()}
    claim = session["_notif_turn_claim"]
    released = []
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _: True)
    monkeypatch.setattr(server, "_session_db", lambda _: nullcontext(db))
    monkeypatch.setattr(server, "_notif_submit", lambda *a, **kw: False)
    monkeypatch.setattr(server, "_notif_release_claim", lambda s, c: released.append((s, c)))
    pending = delivery(journal, batch)
    assert not pending.submit("rid", "sid", session, "report", "process_complete", {}, "test")
    assert released == [(session, claim)]
    assert "_submit_user_row" not in session and "_marker_until_tool" not in session
    assert len(db.get_messages("chat")) == 1


@pytest.mark.parametrize("stop", [False, True, "retire", "gate_error", "gate_refused"])
@pytest.mark.parametrize("local", [False, True])
def test_real_wake_worker_entry_and_live_pending_claim(store, turn_env, monkeypatch, stop, local):
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
    from agent.session_persistence import SessionPersistenceMixin
    from agent.turn_context import _stage_turn_user_message
    class FlushingAgent(SessionPersistenceMixin, SimpleNamespace):
        def run_conversation(self, text, *, persist_user_message=None, persist_user_display_kind=None,
                             persist_user_display_metadata=None, **kwargs):
            row, _ = _stage_turn_user_message(self, text, persist_user_message, None, None,
                                              persist_user_display_kind, persist_user_display_metadata)
            messages = [row, {"role": "assistant", "content": "ok"}]
            assert self._flush_messages_to_session_db(messages)
            assert self._flush_messages_to_session_db(messages)
            completed.set()
            return {"final_response": "ok", "messages": messages, "interrupted": False, "completed": True}
    attrs = vars(session["agent"]).copy()
    attrs.pop("run_conversation")
    session["agent"] = FlushingAgent(**attrs, _session_db_created=True)
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
    if local:
        journal.discard(batch)
        monkeypatch.setattr(ar, "ENABLED", True)
        monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: journal.path.parent)
        monkeypatch.setattr(threading, "Timer", lambda *a: SimpleNamespace(
            daemon=False, start=lambda: None, cancel=lambda: None))
        ar.schedule("chat", text="report", display_kind="process_complete", display_metadata={},
                    due=100, cause="quota", persisted=False, now=50, profile_home=journal.path.parent)
    else:
        journal.release(batch)
    try:
        if local:
            ar._fire("chat", profile_home=journal.path.parent)
        else:
            assert ar.consume_handoffs([journal.path.parent], now=100) == 1
        assert entered.wait(10)
        staged_identity = None if local else (session["_submit_user_row"]["_row_id"],
                                               session["_submit_user_row"]["message_uid"])
        if local:
            assert ar.pending("chat", profile_home=journal.path.parent)
        # Wake returned, but the actual PreparedWork has not entered its body.
        assert ar.consume_handoffs([journal.path.parent], now=100) == 0
        if stop in ("gate_error", "gate_refused"):
            from tui_gateway.automation_replay_dispatch import Delivery
            from tui_gateway.automation_replay_local import LocalPermit
            def refuse(_):
                if stop == "gate_error":
                    raise sqlite3.OperationalError("admission unavailable")
                return False
            monkeypatch.setattr(LocalPermit if local else Delivery, "admit", refuse)
        elif stop == "retire":
            monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
            ar.handoff_all()
        elif stop:
            ar.cancel_durable(journal.path.parent, ("chat",), 1e20, stop=True)
        release.set()
        session["_run_thread"].join(10)
        assert not session["_run_thread"].is_alive()
        assert completed.is_set() is (not stop)
        rows = db.get_messages("chat")
        assert len(rows) == ((0 if local else 1) if stop else 2)
        assert sum(row["role"] == "user" for row in rows) == (0 if local and stop else 1)
        if not local:
            assert (rows[0]["id"], rows[0]["message_uid"]) == staged_identity
            assert rows[0]["message_uid"] == "automation-replay-" + batch["id"]
        if not stop:
            assert rows[0]["display_kind"] in ("process_complete", "kanban_notice")
        assert not session["running"]
        if not stop:
            # Worker admission is permanent even without relying on its assistant rows.
            ar.consume_handoffs([journal.path.parent], now=100)
        retained = stop == "retire" or (not local and stop in ("gate_error", "gate_refused"))
        assert journal.owners() == (["chat"] if retained else [])
        if stop == "retire":
            # The predecessor is still alive; refusing its worker must release custody now.
            assert journal.claim("chat", 1e20, "successor", lambda _: True) is not None
        assert "_submit_user_row" not in session
        assert "inflight_turn" not in session or session["inflight_turn"] is None
    finally:
        release.set()
        if session.get("_run_thread") is not None:
            session["_run_thread"].join(10)
        with server._sessions_lock:
            server._sessions.pop(sid, None)
        ar.reset()
