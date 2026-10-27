"""An accepted prompt.submit must survive a client socket drop, and a message that never got a turn must not idle
silently (t_ed201037).

Incident: an edit/resend on a Desktop chat persisted the user's row, then the deferred agent build failed (the
session's provider had lost its credentials) while the renderer socket was dropping. The waiter's exits and the
build failure logged nothing, so agent.log read as "the message never ran"; the reply never came and nothing
later flagged the unanswered row. A related path drops a healthy turn outright: once the client is gone past the
orphan-reap grace, a turn still parked on the build has no activity clock, so the reaper interrupted it.
"""

from __future__ import annotations

import logging
import threading

import pytest

from hermes_state import SessionDB
from tui_gateway import entry, server
from tui_gateway.transport import bind_transport, reset_transport


class Sock:
    """A client socket: frames while open, a dead peer once closed."""

    def __init__(self):
        self.frames, self._closed = [], False

    def write(self, obj):
        if self._closed:
            return False
        self.frames.append(obj)
        return True

    def close(self):
        self._closed = True


def _rpc(transport, method, params):
    token = bind_transport(transport)
    try:
        return server.handle_request({"id": method, "method": method, "params": params})
    finally:
        reset_transport(token)


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server, "_wire_session_agent", lambda *a: False)
    monkeypatch.setattr(server, "_announce_built_agent", lambda *a: None)
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: None)
    created: list[str] = []
    yield db, created
    for sid in created:
        server._sessions.pop(sid, None)
    db.close()


def _seeded_desktop_session(gateway, transport):
    """A Desktop chat with two finished exchanges whose agent is not built yet (cold resume shape)."""
    db, created = gateway
    resp = _rpc(transport, "session.create", {"cols": 96, "source": "desktop"})
    sid, key = resp["result"]["session_id"], resp["result"]["stored_session_id"]
    created.append(sid)
    session = server._sessions[sid]
    server._ensure_session_db_row(session)
    for role, text in (("user", "question 1"), ("assistant", "answer 1"),
                       ("user", "are you still working on this"), ("assistant", "yes")):
        db.append_message(key, role, text)
    session["history"] = db.get_messages_as_conversation(key, include_row_ids=True)
    return sid, key, session


def _edit_last_turn(transport, sid, session, text):
    """The Desktop edit/resend of the last user turn: a row-addressed truncation submit."""
    users = [m for m in session["history"] if m.get("role") == "user"]
    return _rpc(transport, "prompt.submit", {
        "session_id": sid, "text": text, "truncate_before_row_id": users[-1]["_row_id"],
        "truncate_before_user_ordinal": 1, "confirm_truncate": True})


class _ManualTimers:
    """threading.Timer stand-in: reap callbacks fire only when the test says so."""

    def __init__(self):
        self.pending = []

    def __call__(self, delay, fn):
        timer = type("Timer", (), {"daemon": False, "start": lambda self: None, "cancel": lambda self: None})()
        self.pending.append((timer, fn))
        return timer

    def fire(self, rounds: int = 3):
        """Fire each armed reap callback; a re-armed one fires again, up to ``rounds`` grace windows."""
        for _ in range(rounds):
            armed, self.pending = self.pending, []
            for _timer, fn in armed:
                fn()


def test_edit_submit_survives_socket_drop_past_reap_grace_during_agent_build(gateway, monkeypatch):
    """Submit (edit with truncation) on a session whose agent must be built; the socket drops and the client
    stays away past the orphan-reap grace while the build runs; it then reconnects. The accepted turn must run."""
    release, ran, interrupted = threading.Event(), [], []
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, **kw: release.wait(10) and type(
        "Agent", (), {"session_id": key})())
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, s, text, **k: ran.append(text) or True)
    real_interrupt = server._interrupt_session_turn
    monkeypatch.setattr(server, "_interrupt_session_turn",
                        lambda *a, **k: interrupted.append(a[0]) or real_interrupt(*a, **k))
    timers = _ManualTimers()
    monkeypatch.setattr(server.threading, "Timer", timers)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 20)
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda *a: False)

    first = Sock()
    sid, key, session = _seeded_desktop_session(gateway, first)
    resp = _edit_last_turn(first, sid, session, "are you still working on this, what the hell")
    assert resp["result"]["status"] == "streaming", resp
    assert session["running"] and not session["agent_ready"].is_set()

    first.close()
    assert server._close_sessions_for_transport(first) == (0, 1)  # parked on the drop sentinel
    timers.fire()  # three grace windows expire with the build still running
    assert interrupted == [], "the orphan reaper interrupted a turn that was only waiting on its agent build"
    assert sid in server._sessions

    second = Sock()
    resumed = _rpc(second, "session.resume", {"session_id": key, "omit_messages": True})["result"]
    assert resumed["session_id"] == sid and resumed["running"] is True

    release.set()
    session["_run_thread"].join(15)
    assert ran == ["are you still working on this, what the hell"]


def test_build_failure_and_pre_agent_exits_are_logged(gateway, monkeypatch, caplog):
    """A failed build ends the waiting turn with a retained error. Both must reach agent.log: without them
    the accepted message reads as one that silently never ran."""
    def _fail(*_a, **_k):
        raise RuntimeError("No Codex credentials stored.")

    monkeypatch.setattr(server, "_make_agent", _fail)
    ran = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: ran.append(a) or True)
    sock = Sock()
    sid, _key, session = _seeded_desktop_session(gateway, sock)
    with caplog.at_level(logging.INFO):
        assert _edit_last_turn(sock, sid, session, "retry this")["result"]["status"] == "streaming"
        session["_run_thread"].join(15)
    assert ran == []
    text = caplog.text
    assert "agent build failed for session" in text and "No Codex credentials stored." in text
    assert "failed before the agent was ready" in text
    assert session["running"] is False
    assert session["inflight_turn"]["status"] == "error"  # retained for the reconnecting client


def test_reattach_flags_an_unanswered_user_row_as_not_sent(gateway):
    """The newest active row is the user's message, nothing runs, nothing is retained: the reattach must say
    so (a retryable failed-turn projection) instead of idling."""
    sock = Sock()
    _sid, key, session = _seeded_desktop_session(gateway, sock)
    db, _ = gateway
    db.append_message(key, "user", "are you still working on this, what the hell")
    session["history"] = db.get_messages_as_conversation(key, include_row_ids=True)

    inflight = _rpc(Sock(), "session.resume", {"session_id": key, "omit_messages": True})["result"]["inflight"]
    assert inflight["user"] == "are you still working on this, what the hell"
    assert inflight["status"] == "error" and inflight["recoverable"] is True
    assert inflight["error_surface"] == {"layer": "runtime", "code": "turn_not_started", "retryable": True}
    assert inflight["error"].startswith("Not sent")


def test_reattach_leaves_answered_running_and_notice_tails_alone(gateway):
    sock = Sock()
    _sid, key, session = _seeded_desktop_session(gateway, sock)
    db, _ = gateway

    def resume_inflight():
        return _rpc(Sock(), "session.resume", {"session_id": key, "omit_messages": True})["result"].get("inflight")

    assert resume_inflight() is None  # answered tail
    db.append_message(key, "user", "a new question")
    session["history"] = db.get_messages_as_conversation(key, include_row_ids=True)
    with session["history_lock"]:
        session["running"] = True
        server._start_inflight_turn(session, "a new question")
    live = resume_inflight()  # a live turn shows its own snapshot, never a not-sent error
    assert live is not None and "error" not in live
    with session["history_lock"]:
        session["running"] = False
        session["inflight_turn"] = None
    db.append_message(key, "user", "Stopped by you", display_kind="turn_stopped")
    session["history"] = db.get_messages_as_conversation(key, include_row_ids=True)
    assert resume_inflight() is None  # a Stop notice tail is not an unsent message


def test_reattach_skips_a_session_another_process_is_answering(gateway, monkeypatch):
    sock = Sock()
    _sid, key, session = _seeded_desktop_session(gateway, sock)
    db, _ = gateway
    db.append_message(key, "user", "answered elsewhere")
    session["history"] = db.get_messages_as_conversation(key, include_row_ids=True)
    monkeypatch.setattr(SessionDB, "session_turn_lease_owner", lambda self, k: {"holder": "pid=1:x"})
    assert _rpc(Sock(), "session.resume", {"session_id": key, "omit_messages": True})["result"].get("inflight") is None


def test_deferred_cold_resume_flags_an_unanswered_user_row_from_the_store(gateway):
    """A backend restart lost the live record: the deferred resume reads the newest durable row."""
    sock = Sock()
    sid, key, _session = _seeded_desktop_session(gateway, sock)
    db, created = gateway
    db.append_message(key, "user", "never answered")
    server._sessions.pop(sid, None)  # the old backend is gone

    resp = _rpc(Sock(), "session.resume", {"session_id": key, "omit_messages": True, "defer_history": True})
    result = resp["result"]
    created.append(result["session_id"])
    assert result["status"] == "resuming"
    assert result["inflight"]["user"] == "never answered"
    assert result["inflight"]["error_surface"]["code"] == "turn_not_started"

    db.append_message(key, "assistant", "here you go")
    server._sessions.pop(result["session_id"], None)
    again = _rpc(Sock(), "session.resume", {"session_id": key, "omit_messages": True, "defer_history": True})
    created.append(again["result"]["session_id"])
    assert again["result"]["inflight"] is None
