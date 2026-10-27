"""Real RPC + SQLite acceptance, with only model execution suppressed."""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_prompt_admission import prompt_digest
from tests.tui_gateway.test_submit_time_user_row import _desktop_session
from tui_gateway import server


@pytest.fixture
def owner(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home
    monkeypatch.setattr(server, "_hermes_home", get_hermes_home())
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    server._ensure_session_db_row(session)
    started = []
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *args: False)
    monkeypatch.setattr(server, "_start_session_work", lambda target, **kw: started.append(target) or True)
    params = {"profile": server._current_profile_name(), "stored_session_id": key,
              "operation_id": "decision-1", "text": "approved exact scope",
              "payload_digest": prompt_digest(key, "approved exact scope")}
    yield db, session, params, started
    server._release_active_session_slot(session)
    server._sessions.pop(sid, None)
    db.close()


def call(method, params):
    return server.handle_request({"id": "test", "method": method, "params": params})


def test_external_transport_cannot_admit_or_probe(owner, monkeypatch):
    db, _session, params, started = owner
    monkeypatch.setattr(server, 'current_transport', lambda: object())
    for method in ('prompt.admit', 'prompt.admission.get'):
        payload = params if method == 'prompt.admit' else {k:v for k,v in params.items() if k != 'text'}
        response = call(method, payload)
        assert response['error']['code'] == 4031
    assert not started
    assert db.get_messages(params['stored_session_id']) == []


def test_rpc_accept_retry_and_readback_after_owner_disappears(owner):
    db, session, params, started = owner
    first = call("prompt.admit", params)
    assert "result" in first, (first, session.get("profile_home"), server._hermes_home,
                               server._profile_home(params["profile"]), session.get("session_key"))
    receipt = first["result"]
    assert receipt["status"] == "accepted"
    assert len(started) == 1
    assert call("prompt.admit", params)["result"] == receipt
    assert len(started) == 1
    assert len(db.get_messages(params["stored_session_id"])) == 1
    saved = dict(server._sessions)
    server._sessions.clear()
    try:
        assert call("prompt.admission.get", {k: v for k, v in params.items() if k != "text"})["result"] == receipt
        assert call("prompt.admit", params)["result"] == receipt
        assert len(started) == 1
    finally:
        server._sessions.update(saved)


@pytest.mark.parametrize("state", ["busy", "rotated", "lazy", "missing", "wrong_profile", "queued", "closing", "images", "branch"])
def test_refused_owner_has_no_message(owner, state):
    db, session, params, started = owner
    mutations = {
        'busy': {'running': True},
        'rotated': {'agent': SimpleNamespace(session_id='different')},
        'lazy': {'lazy': True},
        'queued': {'queued_prompt': {'text': 'already waiting'}},
        'closing': {'_closing': True},
        'images': {'attached_images': ['pending.png']},
        'branch': {'seeded': True},
    }
    session.update(mutations.get(state, {}))
    if state == 'missing':
        params['stored_session_id'] = 'absent'
        params['payload_digest'] = prompt_digest('absent', params['text'])
    if state == 'wrong_profile':
        params['profile'] = 'does-not-exist'
    response = call("prompt.admit", params)
    assert response["error"]["code"] in (4092, 4064), response
    assert not started
    assert db.get_messages(session["session_key"]) == []


def test_crash_after_commit_does_not_reexecute(owner, monkeypatch):
    db, _session, params, _started = owner
    def crash(*args, **kwargs):
        raise RuntimeError("simulated scheduler failure after commit")
    monkeypatch.setattr(server, "_start_session_work", crash)
    # The first call can lose its reply. Acceptance remains queryable and no
    # retry is permitted to interpret a missing dispatch as permission to run.
    call("prompt.admit", params)
    again = call("prompt.admit", params)
    assert again["result"]["status"] == "accepted", again
    assert again["result"]["dispatch"] == "cancelled"
    assert db.get_messages(params["stored_session_id"]) == []


def test_turn_adopts_admission_row_without_duplicate_and_disables_recovery(owner, monkeypatch):
    from tests.tui_gateway.test_submit_time_user_row import _flush_agent
    from agent.turn_context import _stage_turn_user_message
    db, session, params, started = owner
    first = call("prompt.admit", params)
    assert "result" in first, first
    recorded = []
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **kw: recorded.append(kw))
    started[0]()
    assert recorded == [{"auto_continue": False}]
    agent = _flush_agent(db, params["stored_session_id"])
    server._adopt_submit_user_row(session, agent, params["text"], params["text"])
    msg, _ = _stage_turn_user_message(agent, params["text"], params["text"], None, None, None, None)
    agent._persist_user_message_idx = 0
    agent._flush_messages_to_session_db([msg, {"role": "assistant", "content": "done"}], [])
    assert [m["role"] for m in db.get_messages(params["stored_session_id"])] == ["user", "assistant"]


def test_normal_submit_claim_cannot_overwrite_admitted_turn(owner):
    _, session, params, _ = owner
    assert "result" in call("prompt.admit", params)
    sid = next(sid for sid, s in server._sessions.items() if s is session)
    error, _fields = server._lock_in_submit_turn("other", sid, session, "race", {}, False, None, None, None)
    assert error["error"]["code"] == 4009
    assert session["_submit_user_row"]["content"] == params["text"]


def test_foreign_writer_lease_refuses_before_acceptance(owner):
    from hermes_cli.active_sessions import try_acquire_active_session
    db, _session, params, started = owner
    lease, refusal = try_acquire_active_session(
        session_id=params["stored_session_id"], surface="desktop", config={},
        metadata={"live_session_id": "another-runtime"})
    assert refusal is None
    try:
        response = call("prompt.admit", params)
        assert response["error"]["code"] == 4090, response
        assert not started
        assert db.get_messages(params["stored_session_id"]) == []
    finally:
        lease.release()


def test_secondary_profile_cannot_read_or_admit_launch_owner(owner, monkeypatch, tmp_path):
    from hermes_cli import profiles
    _db, _session, params, started = owner
    profiles_root = tmp_path / "profiles"
    secondary = profiles_root / "secondary"
    secondary.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    secondary_db = SessionDB(db_path=secondary / "state.db")
    secondary_db.create_session(session_id=params["stored_session_id"], source="desktop")
    secondary_db.close()
    assert "result" in call("prompt.admit", params)
    foreign = {**params, "profile": "secondary"}
    response = call("prompt.admit", foreign)
    assert response["error"]["code"] == 4092, response
    read = call("prompt.admission.get", {k: v for k, v in foreign.items() if k != "text"})
    assert read["result"]["status"] == "absent", read
    assert len(started) == 1

