"""Adversarial handoff races: real stores/registries, deterministic interleavings."""
import contextlib
import threading
from types import SimpleNamespace

import pytest

from agent.turn_facade_lease import DurableTurnLease, admit_durable_turn_lease
from agent import turn_facade_lease
from hermes_cli import active_sessions
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    home = tmp_path / "owner-profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(home / "state.db")
    db.create_session("chat", source="cli")
    assert db.try_acquire_session_turn_lease("chat", "original-cli")
    session = {"session_key": "chat", "profile_home": str(home), "source": "desktop",
               "agent": None, "history": [], "history_lock": threading.Lock(), "running": False}
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_reload_history_after_takeover", lambda s: True)
    monkeypatch.setattr(server, "_load_cfg", dict)
    yield home, db, session
    server._release_active_session_slot(session)
    db.close()


def test_handoff_binds_new_registry_to_inspected_db_not_ambient_profile(handoff, monkeypatch, tmp_path):
    home, db, session = handoff
    other = tmp_path / "ambient-profile"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    session.pop("profile_home")
    original_request = active_sessions.request_turn_handoff

    def request(lease, holder):
        assert lease.state_path == active_sessions._state_path(home)
        ok = original_request(lease, holder)
        db.release_session_turn_lease("chat", holder)
        return ok

    monkeypatch.setattr(active_sessions, "request_turn_handoff", request)
    result = server._take_over_durable_writer("r", "ui", session, db.session_turn_lease_owner("chat"))
    assert result["result"]["acknowledged"] is True
    assert not active_sessions._state_path(other).exists()


def test_handoff_rejects_cached_registry_from_wrong_profile(handoff, tmp_path):
    _home, db, session = handoff
    wrong = tmp_path / "wrong-profile"
    lease, _, refusal = active_sessions.take_over_active_session(
        session_id="chat", surface="desktop", registry_home=wrong, config={})
    assert refusal is None
    session["active_session_lease"] = lease
    result = server._take_over_durable_writer("r", "ui", session, db.session_turn_lease_owner("chat"))
    assert result["error"]["data"]["reason"] == "SESSION_COORDINATION_UNAVAILABLE"
    assert active_sessions.find_takeover_of("original-cli", registry_home=wrong) is None
    assert db.session_turn_lease_owner("chat")["holder"] == "original-cli"


def test_replaced_requester_cannot_report_success_when_cli_releases(handoff, monkeypatch):
    home, db, session = handoff
    original_request = active_sessions.request_turn_handoff
    replacement = []

    def request(lease, holder):
        assert original_request(lease, holder)
        successor, _, refusal = active_sessions.take_over_active_session(
            session_id="chat", surface="desktop", registry_home=home,
            metadata={"live_session_id": "other-ui"}, config={})
        assert refusal is None
        replacement.append(successor)
        db.release_session_turn_lease("chat", holder)
        return True

    monkeypatch.setattr(active_sessions, "request_turn_handoff", request)
    try:
        result = server._take_over_durable_writer("r", "ui", session, db.session_turn_lease_owner("chat"))
        assert result["error"]["data"]["reason"] == "SESSION_NOT_OWNED"
        assert not active_sessions.turn_handoff_is_current(session["active_session_lease"], "original-cli")
    finally:
        for lease in replacement:
            lease.release()


def test_replacement_durable_writer_is_not_implicitly_taken_over(handoff, monkeypatch):
    _, db, session = handoff
    original_request = active_sessions.request_turn_handoff

    def request(lease, holder):
        assert original_request(lease, holder)
        db.release_session_turn_lease("chat", holder)
        assert db.try_acquire_session_turn_lease("chat", "replacement-cli")
        return True

    monkeypatch.setattr(active_sessions, "request_turn_handoff", request)
    # Any wait on the replacement is a bug: the user targeted the original generation.
    monkeypatch.setattr(server.time, "sleep", lambda *_: pytest.fail("waited on replacement writer"))
    result = server._take_over_durable_writer("r", "ui", session, db.session_turn_lease_owner("chat"))
    assert result["error"]["data"]["reason"] == "SESSION_NOT_OWNED"
    assert db.session_turn_lease_owner("chat")["holder"] == "replacement-cli"
    assert active_sessions.find_takeover_of("replacement-cli", registry_home=session["profile_home"]) is None


def test_latch_follows_compression_after_interrupt_but_not_other_profile(handoff, tmp_path):
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    _, db, _ = handoff
    agent = SimpleNamespace(session_id="chat", _session_db=db, interrupt=lambda *a, **k: None,
                            _liveness_activity_lock=lambda: threading.Lock())
    lease = DurableTurnLease(agent, db, "chat", "original-cli")
    lease.turn_active = True
    lease._interrupt_turn("taken over", taken_over=True)
    # Compression can finish after the interrupt latches the old id.
    db.end_session("chat", "compression")
    db.create_session("compressed", source="cli", parent_session_id="chat")
    agent.session_id = "compressed"
    lease.release()
    admission = admit_durable_turn_lease(
        agent, session_id="compressed", relay_turn_id="auto", task_context={"platform": "cli"},
        conversation_history=[])
    assert admission.lease is None
    assert admission.early_result["failure_reason"] == "session_busy"
    assert admission.early_result["api_calls"] == 0
    assert db.session_turn_lease_owner("compressed") is None
    cli = SimpleNamespace(agent=agent)
    pending, _ = CLIChatTurnMixin._chat_resolve_interrupt(
        cli, SimpleNamespace(result={"interrupted": True, "interrupt_message": "taken over"}),
        threading.current_thread(), None, "")
    assert pending is None
    assert cli._last_turn_interrupted
    db.create_session("unrelated", source="cli")
    assert not turn_facade_lease.session_was_taken_over(agent, "unrelated")
    other = SessionDB(tmp_path / "another-profile" / "state.db")
    try:
        other.create_session("chat", source="cli")
        agent._session_db = other
        assert not turn_facade_lease.session_was_taken_over(agent, "chat")
    finally:
        other.close()
