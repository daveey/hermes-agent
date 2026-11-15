"""Native origin custody must not be reconstructible from session environment."""
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_origin_native as native


def test_environment_is_not_custody(monkeypatch):
    from hermes_cli import kanban_origin, kanban_session_provenance, profiles
    monkeypatch.setenv("HERMES_SESSION_ID", "borrowed-chat")
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("macbook", []))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(kanban_session_provenance, "_local_session_sources",
                        lambda ids: {"borrowed-chat": "desktop"})
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert native.capture(None) is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_borrowed")
    assert native.capture("t_borrowed") is None


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from agent.turn_facade_lease import admit_durable_turn_lease
    from hermes_cli import kanban_origin
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("macbook", []))
    monkeypatch.setattr("agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat-1", source="desktop")
    agent = SimpleNamespace(_session_db=db, session_id="chat-1", _persist_disabled=False,
                            _interrupt_requested=False, _emit_status=lambda *a: None,
                            _touch_activity=lambda *a: None)
    admission = admit_durable_turn_lease(agent, session_id="chat-1", relay_turn_id="turn-1",
        task_context={"platform": "desktop", "session_id": "chat-1"}, conversation_history=[])
    lease = admission.lease
    assert lease is not None
    lease.start()
    try:
        yield agent, db, lease
    finally:
        lease.stop_refresher()
        lease.join_threads()
        lease.release()
        db.close()


def test_admitted_capture_and_copied_thread_context(admitted):
    from agent.origin_turn_custody import scope
    from tools.thread_context import propagate_context_to_thread
    from concurrent.futures import ThreadPoolExecutor
    _, _, lease = admitted
    expected = {"origin": {"gateway": "macbook", "profile": "default", "session": "chat-1"}}
    assert native.capture(None) is None
    with scope(lease):
        assert native.capture(None) == expected
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(propagate_context_to_thread(native.capture), None).result() == expected
        copied = copy_context()
        with scope(None):
            assert native.capture(None) is None
        assert native.capture(None) == expected
    assert native.capture(None) is None
    lease.stop_refresher()
    assert copied.run(native.capture, None) is None


@pytest.mark.parametrize("change", ["expired", "holder", "session", "db", "released", "pid", "delegated",
                                    "home", "profile", "gateway", "custom_profile", "persist_disabled",
                                    "noncanonical_db", "source", "interrupt", "nonfinite_expiry"])
def test_custody_revalidated(admitted, monkeypatch, change):
    from agent import origin_turn_custody as custody
    from agent.delegation_context import delegated_child_context
    agent, db, lease = admitted
    with custody.scope(lease):
        assert native.capture(None) is not None
        if change == "expired":
            monkeypatch.setattr(db, "session_turn_lease_expires_at", lambda *a: 0)
        elif change == "holder":
            db.release_session_turn_lease("chat-1", lease.holder)
            db.acquire_session_turn_lease("chat-1", "other", ttl_seconds=60)
        elif change == "session":
            db.create_session("other", source="desktop")
            agent.session_id = "other"
        elif change == "db":
            agent._session_db = object()
        elif change == "home":
            monkeypatch.setenv("HERMES_HOME", str(db.db_path.parent / "other-home"))
        elif change in ("profile", "custom_profile"):
            from hermes_cli import profiles
            monkeypatch.setattr(profiles, "get_active_profile_name",
                                lambda: "custom" if change == "custom_profile" else "other")
        elif change == "gateway":
            from hermes_cli import kanban_origin
            monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("other", []))
        elif change == "persist_disabled":
            agent._persist_disabled = True
        elif change == "noncanonical_db":
            with monkeypatch.context() as patch:
                patch.setattr(db, "db_path", db.db_path.parent / "shadow.db")
                assert native.capture(None) is None
            return
        elif change == "source":
            monkeypatch.setattr(db, "get_session", lambda sid: {"source": "kanban"})
        elif change == "interrupt":
            lease.interrupt_message = "revoked"
        elif change == "nonfinite_expiry":
            monkeypatch.setattr(db, "session_turn_lease_expires_at", lambda *a: float("nan"))
        elif change == "released":
            lease.release()
        elif change == "pid":
            monkeypatch.setattr(custody.os, "getpid", lambda: -1)
        elif change == "delegated":
            with delegated_child_context():
                assert native.capture(None) is None
            assert native.capture(None) is not None
            return
        assert native.capture(None) is None


def test_release_during_source_read_refuses(admitted, monkeypatch):
    from agent.origin_turn_custody import scope
    _, db, lease = admitted
    read = db.get_session
    def releasing(sid):
        row = read(sid)
        lease.release()
        return row
    with scope(lease):
        monkeypatch.setattr(db, "get_session", releasing)
        assert native.capture(None) is None


def test_captured_authority_revoked_before_record(admitted, monkeypatch):
    from agent.origin_turn_custody import scope
    from tests.hermes_cli.test_origin_native import memory_tasks
    _, _, lease = admitted
    conn = memory_tasks()
    monkeypatch.setattr(native, "_primary_scope", lambda conn: ("macbook", "default", "default"))
    with scope(lease):
        captured = native.capture(None)
        assert captured is not None
        lease.release()
        native.record(conn, "t_root", captured, 1)
        assert native._receipt(conn, "t_root") is None
    conn.close()


def test_real_turn_facade_binds_and_releases(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from tests.agent.test_cross_process_turn_lease import _agent_with_db
    from hermes_cli import kanban_origin
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("macbook", []))
    monkeypatch.setattr("agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat-facade", source="desktop")
    agent = _agent_with_db(db, session_id="chat-facade")
    observed = []
    def loop(*args, **kwargs):
        observed.append(native.capture(None))
        return {"final_response": "ok", "messages": [], "failed": False}
    monkeypatch.setattr("agent.conversation_loop.run_conversation", loop)
    monkeypatch.setattr("agent.relay_cwd.resolve_relay_scope_cwds", lambda *a: ("", ""))
    try:
        assert agent.run_conversation("test")["final_response"] == "ok"
        assert observed == [{"origin": {"gateway": "macbook", "profile": "default", "session": "chat-facade"}}]
        assert native.capture(None) is None
        assert observed[0].validated() is None
    finally:
        db.close()

