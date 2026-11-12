"""Consent authority must survive delegation to a host without the originating chat."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_supervisor_requests as requests
from tests.hermes_cli import test_kanban_operator_call as helpers
from tools import approval_context

fleet = helpers.fleet


@pytest.mark.parametrize("outcome", ["timeout", "deny", "notify_failed", "cancelled"])
def test_real_gateway_settlement_carries_consent_to_worker(fleet, monkeypatch, tmp_path, outcome):
    from tools import approval as approvals
    from tools import approval_gateway_wait as wait
    from hermes_cli import kanban_consent as consent
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "gateway-home"))
    session = "isolated-consent-gateway"
    command = f"hermes kanban comment {fleet.target} 'isolated probe'"

    def notify(data):
        if outcome == "notify_failed":
            raise OSError("isolated transport unavailable")
        if outcome == "deny":
            assert approvals.resolve_gateway_approval(session, "deny") == 1
        elif outcome == "cancelled":
            approvals.clear_session(session)

    monkeypatch.setattr(wait, "_poll_event", lambda *a, **kw: "timeout" if outcome == "timeout" else "set")
    try:
        wait._await_gateway_decision(session, notify, {
            "command": command, "description": "isolated", "pattern_key": "test", "pattern_keys": ["test"]})
        caller = fleet.card(fleet.w, "settled child", session_id=session)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "gateway-worker-home"))
        op = {"op": "operator", "action": "comment", "task_id": fleet.target}
        if outcome in ("timeout", "deny"):
            with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
                consent.enforce(fleet.w, caller, [op])
        else:
            consent.enforce(fleet.w, caller, [op])
    finally:
        approvals.clear_session(session)


@pytest.mark.parametrize("template,expected", [
    ("hermes kanban comment {target} 'See t_0000000000000000'", "target"),
    ("hermes kanban supervisor-requests list --requester {target}", "none"),
    ("printf '%s' 'hermes kanban comment {target} body'", "none"),
    ("hermes kanban --board default comment {target} 'body'", "target"),
])
def test_consent_binds_command_target_not_body_mentions(fleet, monkeypatch, tmp_path, template, expected):
    from hermes_cli import kanban_consent as consent
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "parser-home"))
    approval_context._fire_approval_hook(
        "post_approval_response", session_id="parser-chat",
        command=template.format(target=fleet.target), choice="deny", surface="gateway")
    stops = consent.session_stops("parser-chat")
    assert [s["targets"] for s in stops] == ([[fleet.target]] if expected == "target" else [])


@pytest.mark.parametrize("choice", ["notify_failed", "transport_error", "cancelled", "smart_deny"])
def test_technical_or_machine_refusal_is_not_human_nonconsent(fleet, monkeypatch, tmp_path, choice):
    from hermes_cli import kanban_consent as consent
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "technical-home"))
    approval_context._fire_approval_hook(
        "post_approval_response", session_id="technical-chat",
        command=f"hermes kanban comment {fleet.target} 'probe'", choice=choice, surface="gateway")
    assert consent.session_stops("technical-chat") == []


def test_user_turn_does_not_clear_stop_and_child_inherits(fleet, monkeypatch, tmp_path):
    from hermes_cli import kanban_consent as consent
    from hermes_state import SessionDB
    home = tmp_path / "persistent-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    approval_context._fire_approval_hook(
        "post_approval_response", session_id="persistent-chat",
        command=f"hermes kanban comment {fleet.target} 'probe'", choice="deny", surface="gateway")
    db = SessionDB(home / "state.db")
    try:
        db.create_session(session_id="persistent-chat", source="cli", model="test")
        db.append_message("persistent-chat", "user", "What is the weather?")
    finally:
        db.close()
    parent = fleet.card(fleet.w, "first child", session_id="persistent-chat")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "grandchild-host"))
    child = fleet.card(fleet.w, "second child", creator_task_id=parent)
    with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
        consent.enforce(fleet.w, child, [{"op": "operator", "action": "comment", "task_id": fleet.target}])


def test_comment_stop_does_not_deny_unrelated_action(fleet, monkeypatch, tmp_path):
    from hermes_cli import kanban_consent as consent
    f = fleet
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "action-home"))
    approval_context._fire_approval_hook(
        "post_approval_response", session_id="action-chat",
        command=f"hermes kanban comment {f.target} 'probe payload'", choice="deny", surface="gateway")
    caller = f.card(f.w, "scoped child", session_id="action-chat")
    consent.enforce(f.w, caller, [{"op": "operator", "action": "move", "task_id": f.target}])
    with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
        consent.enforce(f.w, caller, [{"op": "operator", "action": "comment", "task_id": f.target}])


def test_home_executor_enforces_creation_stop_from_old_worker(fleet, monkeypatch, tmp_path):
    from hermes_cli import kanban_consent as consent
    f = fleet
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "origin-consent"))
    approval_context._fire_approval_hook(
        "post_approval_response", session_id="stopped-chat",
        command=f"hermes kanban comment {f.target} 'probe payload'", choice="timeout", surface="gateway")
    caller = f.card(f.w, "stopped child", session_id="stopped-chat")
    kb.claim_task(f.w, caller)
    run = kb._current_run_id(f.w, caller)
    f.run_uid(f.w, caller)
    f.board()
    # Model an older submitting worker: the home must enforce independently.
    with monkeypatch.context() as old_worker:
        old_worker.setattr(consent, "enforce", lambda *args: None)
        result = requests.submit(f.w, env_task=caller, env_run=str(run), ops=[{
            "op": "operator", "action": "comment", "task_id": f.target, "text": "probe payload"}])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-without-chat"))
    helpers._round(f)
    request = requests.get_request(f.w, result["request_id"])
    assert request["result"][0]["outcome"] == "refused", request
    assert "consent_stop" in request["result"][0]["detail"]
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id=?", (f.target,)).fetchone()


def test_timeout_creation_provenance_refuses_cross_host_relay(fleet, monkeypatch, tmp_path):
    f = fleet
    session = "consent-origin"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "consent-home"))
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    approval_context._fire_approval_hook(
        "post_approval_response", session_id=session, session_key=session,
        command=f"hermes kanban comment {f.target} 'probe payload'", choice="timeout", surface="gateway")
    caller = f.card(f.w, "independent follow-up", session_id=session)
    payload = json.loads(f.w.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='created'", (caller,)).fetchone()[0])
    assert payload.get("consent_stops"), "creation dropped the approval timeout"
    assert payload["consent_stops"][0]["targets"] == [f.target]
    assert payload["consent_stops"][0]["origin_session"] == session
    assert payload["consent_stops"][0]["outcome"] == "timeout"
    kb.claim_task(f.w, caller)
    run = kb._current_run_id(f.w, caller)
    f.run_uid(f.w, caller)
    f.board()
    # A worker on a different host has neither this chat nor its host-local stop cache.
    other_home = tmp_path / "other-host"
    other_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    monkeypatch.setenv("HERMES_SESSION_ID", "worker-session")
    with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
        requests.submit(f.w, env_task=caller, env_run=str(run), ops=[{
            "op": "operator", "action": "comment", "task_id": f.target, "text": "probe payload"}])
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id=?", (f.target,)).fetchone()


def _decide(target, choice, session="regrant-chat"):
    approval_context._fire_approval_hook(
        "post_approval_response", session_id=session,
        command=f"hermes kanban comment {target} 'probe payload'", choice=choice, surface="gateway")


def test_explicit_same_target_approval_supersedes_stop(fleet, monkeypatch, tmp_path):
    from hermes_cli import kanban_consent as consent
    f = fleet
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "regrant-home"))
    _decide(f.target, "timeout")
    stopped = f.card(f.w, "stopped child", session_id="regrant-chat")
    _decide(f.target, "once")
    granted = f.card(f.w, "granted child", session_id="regrant-chat")
    consent.enforce(f.w, granted, [{"op": "operator", "action": "comment", "task_id": f.target}])
    # A card created under the stop keeps its immutable creation provenance.
    with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
        consent.enforce(f.w, stopped, [{"op": "operator", "action": "comment", "task_id": f.target}])


@pytest.mark.parametrize("choice", ["smart_approve", "notify_failed", "cancelled"])
def test_other_target_or_machine_approval_never_grants(fleet, monkeypatch, tmp_path, choice):
    from hermes_cli import kanban_consent as consent
    f = fleet
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nogrant-home"))
    _decide(f.target, "deny")
    _decide("t_" + "1" * 16, "always")
    _decide(f.target, choice)
    child = f.card(f.w, "still stopped", session_id="regrant-chat")
    with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
        consent.enforce(f.w, child, [{"op": "operator", "action": "comment", "task_id": f.target}])


@pytest.mark.parametrize("choice,raises", [("timeout", True), ("deny", True), ("once", False)])
def test_store_failure_fails_closed_without_blocking_an_approved_command(monkeypatch, tmp_path, choice, raises):
    """A lost stop must surface (the denial stays a denial); a lost grant only keeps the stop."""
    import hermes_state
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "broken-store"))

    def broken(self, fn):
        raise hermes_state.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(hermes_state.SessionDB, "_execute_write", broken)
    if raises:
        with pytest.raises(Exception, match="locked"):
            _decide("t_" + "2" * 16, choice)
    else:
        _decide("t_" + "2" * 16, choice)
