"""A turn admitted after a busy-store retry must flush under the lease key it claimed.

Admission claims the row for ``session_id`` and, when the acquire saw a busy store, reloads the
resume target. That target can be a plain child (parent not ended by compression) whose lease key
is its own id. Moving ``agent.session_id`` there under the admitted id's claim made the valid
owner's first flush read a missing row. Real ``SessionDB``, a real busy writer on a second
connection, the real admission and the real guarded append.
"""
import sqlite3
import threading
import uuid

import pytest

from agent.turn_facade_lease import admit_durable_turn_lease
from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError
from run_agent import AIAgent


class Agent:
    platform = "desktop"
    _interrupt_requested = False
    _session_db_created = True
    _persist_disabled = False

    def __init__(self, db, session_id):
        self._session_db, self.session_id = db, session_id
        self.statuses = []
        self._activity_lock = threading.Lock()

    def _emit_status(self, text):
        self.statuses.append(text)

    def _liveness_activity_lock(self):
        return self._activity_lock

    def _touch_activity(self, _label):
        pass


def _store(tmp_path, *, compression: bool):
    home = tmp_path / ("compressed" if compression else "plain")
    home.mkdir()
    db = SessionDB(home / "state.db")
    db.create_session("root", source="desktop")
    db.append_message("root", "user", "first")
    if compression:
        db.end_session("root", "compression")
    # No branch/delegate/reset marker, so resolve_resume_session_id follows the child either way.
    db.create_session("child", source="desktop", parent_session_id="root")
    db.append_message("child", "user", "newer")
    return db, home / "state.db"


@pytest.fixture
def plain(tmp_path):
    db, path = _store(tmp_path, compression=False)
    try:
        yield db, path
    finally:
        db.close()


@pytest.fixture
def compressed(tmp_path):
    db, path = _store(tmp_path, compression=True)
    try:
        yield db, path
    finally:
        db.close()


class busy_writer:
    """Hold the store's write lock until the admission's acquire has reported the busy store, then
    commit. Tied to that report, not to a sleep, so a slow runner cannot admit uncontended and pass
    without reaching the reload; ``contended`` is asserted by every test that depends on it."""

    def __init__(self, path, monkeypatch):
        self.conn = sqlite3.connect(path, timeout=0.1, check_same_thread=False)
        self.conn.isolation_level = None
        self.contended = threading.Event()
        self.held = False
        real = SessionDB.acquire_session_turn_lease

        def acquire(db, session_id, holder, *, on_contended=None, **kwargs):
            def reported():
                self.contended.set()
                self.commit()
                if on_contended is not None:
                    on_contended()
            return real(db, session_id, holder, on_contended=reported, **kwargs)

        monkeypatch.setattr(SessionDB, "acquire_session_turn_lease", acquire)

    def commit(self):
        if self.held:
            self.held = False
            self.conn.execute("COMMIT")

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        self.held = True
        return self

    def __exit__(self, *_exc):
        self.commit()
        self.conn.close()


def admit(agent, session_id, task_context=None):
    context = {"platform": "desktop", "session_id": session_id} if task_context is None else task_context
    admission = admit_durable_turn_lease(
        agent, session_id=session_id, relay_turn_id=f"{session_id}:fixture:{uuid.uuid4().hex[:8]}",
        task_context=context, conversation_history=[])
    assert admission.lease is not None, admission.early_result
    return admission


def lease_rows(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT conversation_id, holder FROM session_turn_leases").fetchall()
    finally:
        conn.close()


def contents(path, session_id):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY id", (session_id,))]
    finally:
        conn.close()


def flush(db, session_id, holder, text="reply"):
    return db.append_messages_batch(
        session_id=session_id, messages=[{"role": "assistant", "content": text}], turn_lease_holder=holder)


def test_fixture_keys(plain, compressed):
    for (db, _path), child_key in ((plain, "child"), (compressed, "root")):
        assert db.resolve_resume_session_id("root") == "child"
        assert db._session_turn_lease_key("root") == "root"
        assert db._session_turn_lease_key("child") == child_key


def test_uncontended_admission_keeps_id_and_persists(plain):
    db, path = plain
    agent = Agent(db, "root")
    lease = admit(agent, "root").lease
    try:
        assert agent.session_id == "root"
        assert lease_rows(path) == [("root", lease.holder)]
        assert flush(db, agent.session_id, lease.holder) == 1
    finally:
        lease.release()
    assert lease_rows(path) == []


def test_contended_admission_stays_on_the_claimed_key(plain, monkeypatch):
    """Was: SessionTurnLeaseLostError (missing lease row) on the admitted owner's first flush."""
    db, path = plain
    agent = Agent(db, "root")
    context = {"platform": "desktop", "session_id": "root"}
    with busy_writer(path, monkeypatch) as writer:
        admission = admit(agent, "root", context)
    assert writer.contended.is_set()
    lease = admission.lease
    try:
        assert lease_rows(path) == [("root", lease.holder)]
        assert (agent.session_id, context["session_id"], lease.session_id) == ("root", "root", "root")
        assert [m["content"] for m in admission.conversation_history] == ["first"]  # the admitted transcript
        assert flush(db, agent.session_id, agent._active_session_turn_lease_holder) == 1
        # The claim never covered the other key: no row was made for it and it still refuses this holder.
        with pytest.raises(SessionTurnLeaseLostError):
            flush(db, "child", lease.holder, "wrong key")
        assert lease_rows(path) == [("root", lease.holder)]
    finally:
        lease.release()
    assert lease_rows(path) == []
    assert contents(path, "root") == ["first", "reply"]  # written once, to the admitted session
    assert contents(path, "child") == ["newer"]
    with pytest.raises(SessionTurnLeaseLostError):
        flush(db, "root", lease.holder, "after release")


def test_contended_admission_follows_a_compression_child(compressed, monkeypatch):
    """Same lease key: the redirect to the continuation still happens and the owner persists there."""
    db, path = compressed
    agent = Agent(db, "root")
    context = {"platform": "desktop", "session_id": "root"}
    with busy_writer(path, monkeypatch) as writer:
        admission = admit(agent, "root", context)
    assert writer.contended.is_set()
    lease = admission.lease
    try:
        assert lease_rows(path) == [("root", lease.holder)]
        assert (agent.session_id, context["session_id"]) == ("child", "child")
        assert [m["content"] for m in admission.conversation_history][-1] == "newer"
        assert flush(db, agent.session_id, lease.holder) == 1
    finally:
        lease.release()
    assert lease_rows(path) == []
    assert contents(path, "child") == ["newer", "reply"]
    with pytest.raises(SessionTurnLeaseLostError):
        flush(db, "child", lease.holder, "after release")


def test_non_owners_still_refuse(plain, monkeypatch):
    db, path = plain
    first = Agent(db, "root")
    with busy_writer(path, monkeypatch) as writer:
        old = admit(first, "root").lease
    assert writer.contended.is_set()
    old.release()
    with pytest.raises(SessionTurnLeaseLostError):  # released
        flush(db, "root", old.holder, "released")
    with pytest.raises(SessionTurnLeaseLostError):  # never acquired
        flush(db, "root", "never-acquired", "never")
    assert lease_rows(path) == []  # a refusal makes no row
    successor = admit(Agent(db, "root"), "root").lease
    try:
        with pytest.raises(SessionTurnLeaseLostError):  # superseded
            flush(db, "root", old.holder, "superseded")
        assert lease_rows(path) == [("root", successor.holder)]
        assert flush(db, "root", successor.holder, "successor") == 1
    finally:
        successor.release()
    assert contents(path, "root") == ["first", "successor"]


def test_unreadable_claim_ends_the_turn_and_releases(plain, monkeypatch):
    """A store that refuses the claim read for the resume target (the scoped worker store answers
    ``permission_denied`` for any id but its own) ends the turn at admission: the admitted row is
    released, the agent is not moved, nothing is written. Modelled on ``SessionDB`` with that one
    refusal; ``RuntimeSessionStore`` itself is not exercised here."""
    db, path = plain
    real = SessionDB.session_turn_lease_expires_at

    def scoped(self, session_id, holder):
        if session_id != "root":
            raise PermissionError("permission_denied")
        return real(self, session_id, holder)

    monkeypatch.setattr(SessionDB, "session_turn_lease_expires_at", scoped)
    agent = Agent(db, "root")
    context = {"platform": "desktop", "session_id": "root"}
    with busy_writer(path, monkeypatch) as writer:
        with pytest.raises(PermissionError, match="permission_denied"):
            admit(agent, "root", context)
    assert writer.contended.is_set()
    assert (agent.session_id, context["session_id"]) == ("root", "root")
    assert lease_rows(path) == []
    assert contents(path, "root") == ["first"] and contents(path, "child") == ["newer"]


def test_managed_turn_persists_after_contended_admission(plain, monkeypatch):
    """Through ``AIAgent.run_conversation``: admission, the loop's guarded flush, release."""
    db, path = plain
    agent = AIAgent.__new__(AIAgent)
    agent.session_id, agent.platform, agent.model = "root", "desktop", "test-model"
    agent._session_db, agent._session_db_created, agent._persist_disabled = db, True, False
    agent._parent_session_id = agent._relay_pending_turn_id = agent.status_callback = None
    agent._reset_activity_labels_after_turn = lambda: None
    agent._conversation_root_id = lambda: "root"
    agent.log_prefix = ""
    agent._vprint = lambda *a, **k: None
    agent._interrupt_requested = agent._interrupt_thread_signal_pending = False
    agent._interrupt_message = agent._pending_redirect = agent._execution_thread_id = None
    seen = {}

    def loop(_agent, _message, _system, history, *_a, **_k):
        seen["session_id"] = _agent.session_id
        seen["rows"] = lease_rows(path)
        seen["holder"] = _agent._active_session_turn_lease_holder
        seen["written"] = flush(db, _agent.session_id, seen["holder"])
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", loop)
    monkeypatch.setattr("agent.relay_cwd.resolve_relay_scope_cwds", lambda *_a, **_k: ("", ""))
    with busy_writer(path, monkeypatch) as writer:
        result = AIAgent.run_conversation(agent, "new message", conversation_history=[])
    assert writer.contended.is_set()  # the reload ran: without the fix this turn moved to "child"
    assert result["final_response"] == "ok"
    assert seen["session_id"] == "root" and seen["written"] == 1
    assert seen["rows"] == [("root", seen["holder"])]
    assert lease_rows(path) == []
    assert contents(path, "root") == ["first", "reply"]
    assert contents(path, "child") == ["newer"]
