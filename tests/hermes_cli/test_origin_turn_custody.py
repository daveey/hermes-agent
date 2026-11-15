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


def _holder(agent, db, lease, mp):
    db.release_session_turn_lease("chat-1", lease.holder)
    db.acquire_session_turn_lease("chat-1", "other", ttl_seconds=60)


def _session(agent, db, lease, mp):
    db.create_session("other", source="desktop")
    agent.session_id = "other"


def _profile(name):
    def mutate(agent, db, lease, mp):
        from hermes_cli import profiles
        mp.setattr(profiles, "get_active_profile_name", lambda: name)
    return mutate


def _gateway(agent, db, lease, mp):
    from hermes_cli import kanban_origin
    mp.setattr(kanban_origin, "gateway_names", lambda: ("other", []))


def _pid(agent, db, lease, mp):
    from agent import origin_turn_custody as custody
    mp.setattr(custody.os, "getpid", lambda: -1)


def _set(obj_name, attr, value):
    def mutate(agent, db, lease, mp):
        setattr({"agent": agent, "lease": lease}[obj_name], attr, value)
    return mutate


def _patch_db(attr, value):
    def mutate(agent, db, lease, mp):
        mp.setattr(db, attr, value)
    return mutate


_MUTATIONS = {
    "expired": _patch_db("session_turn_lease_expires_at", lambda *a: 0),
    "holder": _holder,
    "session": _session,
    "db": _set("agent", "_session_db", object()),
    "home": lambda agent, db, lease, mp: mp.setenv("HERMES_HOME", str(db.db_path.parent / "other-home")),
    "profile": _profile("other"),
    "custom_profile": _profile("custom"),
    "gateway": _gateway,
    "persist_disabled": _set("agent", "_persist_disabled", True),
    "source": _patch_db("get_session", lambda sid: {"source": "kanban"}),
    "interrupt": _set("lease", "interrupt_message", "revoked"),
    "nonfinite_expiry": _patch_db("session_turn_lease_expires_at", lambda *a: float("nan")),
    "released": lambda agent, db, lease, mp: lease.release(),
    "pid": _pid,
}


@pytest.mark.parametrize("change", [*_MUTATIONS, "noncanonical_db", "delegated"])
def test_custody_revalidated(admitted, monkeypatch, change):
    from agent import origin_turn_custody as custody
    from agent.delegation_context import delegated_child_context
    agent, db, lease = admitted
    with custody.scope(lease):
        assert native.capture(None) is not None
        if change == "noncanonical_db":
            with monkeypatch.context() as patch:
                patch.setattr(db, "db_path", db.db_path.parent / "shadow.db")
                assert native.capture(None) is None
            return
        if change == "delegated":
            with delegated_child_context():
                assert native.capture(None) is None
            assert native.capture(None) is not None
            return
        _MUTATIONS[change](agent, db, lease, monkeypatch)
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

