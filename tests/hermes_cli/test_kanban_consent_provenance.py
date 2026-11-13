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
        old_worker.setattr(consent, "enforce", lambda *args, **kw: None)
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


def _stopped_home_call(f, monkeypatch, tmp_path, name):
    from hermes_cli import kanban_consent as consent
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / f"{name}-origin"))
    approval_context._fire_approval_hook(
        "post_approval_response", session_id=f"{name}-chat",
        command=f"hermes kanban comment {f.target} 'probe payload'", choice="timeout", surface="gateway")
    caller = f.card(f.w, f"{name} child", session_id=f"{name}-chat")
    kb.claim_task(f.w, caller)
    run = kb._current_run_id(f.w, caller)
    f.run_uid(f.w, caller)
    f.board()
    with monkeypatch.context() as old_worker:
        old_worker.setattr(consent, "enforce", lambda *args, **kw: None)
        result = requests.submit(f.w, env_task=caller, env_run=str(run), ops=[{
            "op": "operator", "action": "comment", "task_id": f.target, "text": "probe payload"}])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / f"{name}-home"))
    return caller, result["request_id"]


def _hide_created(f, monkeypatch, caller):
    """Model replica sync lag: the caller's created event has not reached the home's replica."""
    real = f.board

    def lagging():
        real()
        with kb.write_txn(f.rep):
            f.rep.execute("DELETE FROM task_events WHERE task_id=? AND kind='created'", (caller,))
    monkeypatch.setattr(f, "board", lagging)
    lagging()
    return real


def test_home_executor_retries_while_creation_provenance_is_unsynced(fleet, monkeypatch, tmp_path):
    from hermes_cli import kanban_operator_call as kox
    f = fleet
    caller, rid = _stopped_home_call(f, monkeypatch, tmp_path, "lag")
    real = _hide_created(f, monkeypatch, caller)
    helpers._round(f)
    assert not f.h.execute("SELECT 1 FROM operator_calls WHERE side='execute'").fetchone(), \
        "the home applied a call whose consent provenance it could not read"
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id=?", (f.target,)).fetchone()
    # Once the created event syncs, the home sees the stop and refuses.
    monkeypatch.setattr(f, "board", real)
    kox._MARK.clear()
    helpers._round(f)
    request = requests.get_request(f.w, rid)
    assert request["result"][0]["outcome"] == "refused", request
    assert "consent_stop" in request["result"][0]["detail"]
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id=?", (f.target,)).fetchone()


def test_home_executor_refuses_when_creation_provenance_never_syncs(fleet, monkeypatch, tmp_path):
    from hermes_cli import kanban_consent as consent
    f = fleet
    caller, rid = _stopped_home_call(f, monkeypatch, tmp_path, "never")
    _hide_created(f, monkeypatch, caller)
    monkeypatch.setattr(consent, "CREATION_SYNC_GRACE_S", -1)
    helpers._round(f)
    request = requests.get_request(f.w, rid)
    assert request["result"][0]["outcome"] == "refused", request
    assert "consent_unproven" in request["result"][0]["detail"]
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id=?", (f.target,)).fetchone()


def test_unsynced_caller_defers_only_its_own_call(fleet, monkeypatch, tmp_path):
    """One caller's sync lag must not hold back a later call on the board; its own call still fails closed."""
    from hermes_cli import kanban_consent as consent
    f = fleet
    caller, rid = _stopped_home_call(f, monkeypatch, tmp_path, "lagging")
    _hide_created(f, monkeypatch, caller)
    later = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "second"})
    helpers._round(f)
    bodies = [r[0] for r in f.h.execute("SELECT body FROM task_comments WHERE task_id=?", (f.target,))]
    assert bodies == ["second"], "the synced caller's call waited behind the unsynced one"
    assert requests.get_request(f.w, later["request_id"])["result"][0]["outcome"] == "applied"
    assert requests.get_request(f.w, rid)["status"] != "done", "the unsynced call was not deferred"
    # The deferred call is retried on a later tick (same watermark) and refused once the grace has passed.
    monkeypatch.setattr(consent, "CREATION_SYNC_GRACE_S", -1)
    helpers._round(f)
    request = requests.get_request(f.w, rid)
    assert request["result"][0]["outcome"] == "refused", request
    assert "consent_unproven" in request["result"][0]["detail"]
    assert [r[0] for r in f.h.execute("SELECT body FROM task_comments WHERE task_id=?", (f.target,))] == ["second"]


def _outcome(f, rid):
    result = requests.get_request(f.w, rid).get("result")
    return result[0].get("outcome") if isinstance(result, list) and result else None


def test_deferred_calls_take_no_tick_slot(fleet, monkeypatch, tmp_path):
    """MAX_PER_TICK+1 lagging callers ahead of a synced one: the synced call applies in the same tick."""
    from hermes_cli import kanban_consent as consent
    from hermes_cli import kanban_operator_call as kox
    f = fleet
    lagging = [_stopped_home_call(f, monkeypatch, tmp_path, f"lag{i}") for i in range(kox.MAX_PER_TICK + 1)]
    for caller, _rid in lagging:
        _hide_created(f, monkeypatch, caller)
    later = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "synced"})
    helpers._round(f)
    assert _outcome(f, later["request_id"]) == "applied", "deferred calls used up the tick's slots"
    assert all(requests.get_request(f.w, rid)["status"] != "done" for _c, rid in lagging)
    monkeypatch.setattr(consent, "CREATION_SYNC_GRACE_S", -1)
    for _ in range(2):  # MAX_PER_TICK refusals per tick
        helpers._round(f)
    for _caller, rid in lagging:
        request = requests.get_request(f.w, rid)
        assert request["result"][0]["outcome"] == "refused", request
        assert "consent_unproven" in request["result"][0]["detail"]
    assert [r[0] for r in f.h.execute("SELECT body FROM task_comments WHERE task_id=?", (f.target,))] == ["synced"]


def test_scan_continues_past_a_full_batch_behind_a_deferral(fleet, monkeypatch, tmp_path):
    """A held watermark must not hide calls more than SCAN_BATCH request rows past it."""
    from hermes_cli import kanban_operator_call as kox
    f = fleet
    monkeypatch.setattr(kox, "SCAN_BATCH", 2)
    caller, rid = _stopped_home_call(f, monkeypatch, tmp_path, "lagbatch")
    _hide_created(f, monkeypatch, caller)
    later = [helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": f"s{i}"})
             for i in range(3)]
    for _ in range(2):  # the watermark stays held below the deferred call across ticks
        helpers._round(f)
    assert [_outcome(f, r["request_id"]) for r in later] == ["applied"] * 3, "calls past the batch stalled"
    assert requests.get_request(f.w, rid)["status"] != "done"
    assert sorted(r[0] for r in f.h.execute("SELECT body FROM task_comments WHERE task_id=?", (f.target,))) == \
        ["s0", "s1", "s2"]


def test_unreachable_board_stops_paging_behind_a_deferral(fleet, monkeypatch, tmp_path):
    """An unreachable Board ends the tick even while a held watermark pages on: no later call jumps the retry."""
    from hermes_cli import kanban_operator_call as kox
    f = fleet
    monkeypatch.setattr(kox, "SCAN_BATCH", 2)
    caller, rid = _stopped_home_call(f, monkeypatch, tmp_path, "lagretry")
    _hide_created(f, monkeypatch, caller)
    later = [helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": f"s{i}"})
             for i in range(3)]
    real, down = kox._caller_refusal, {"on": True}

    def flaky(rep, board, event, payload, *replica):
        if down["on"] and (payload.get("args") or {}).get("text") == "s0":
            return None, True
        return real(rep, board, event, payload, *replica)
    monkeypatch.setattr(kox, "_caller_refusal", flaky)
    helpers._round(f)
    assert [_outcome(f, r["request_id"]) for r in later] == [None] * 3, "a call after the retry was applied first"
    down["on"] = False
    helpers._round(f)
    assert [_outcome(f, r["request_id"]) for r in later] == ["applied"] * 3
    assert requests.get_request(f.w, rid)["status"] != "done"


def test_caller_refusal_wins_over_a_deferral_refusal(fleet, monkeypatch, tmp_path):
    """A call refused for its caller keeps that reason even when its created event also never synced."""
    from hermes_cli import kanban_consent as consent
    from hermes_cli import kanban_operator_call as kox
    f = fleet
    caller, rid = _stopped_home_call(f, monkeypatch, tmp_path, "lagcaller")
    _hide_created(f, monkeypatch, caller)
    monkeypatch.setattr(consent, "CREATION_SYNC_GRACE_S", -1)
    monkeypatch.setattr(kox, "_caller_refusal", lambda *a: ("caller_unproven: isolated probe", False))
    helpers._round(f)
    request = requests.get_request(f.w, rid)
    assert request["result"][0]["outcome"] == "refused", request
    assert "caller_unproven: isolated probe" in request["result"][0]["detail"]
    assert "consent_unproven" not in request["result"][0]["detail"]


def test_replica_caller_checks_run_once_per_call(fleet, monkeypatch):
    """The deferral check and the caller refusal share one replica caller check (task_runs + claimed scan)."""
    from hermes_cli import kanban_operator_call as kox
    f = fleet
    real, calls = kox._caller_replica_refusal, []

    def counted(rep, event, payload):
        calls.append(payload.get("request_uid"))
        return real(rep, event, payload)
    monkeypatch.setattr(kox, "_caller_replica_refusal", counted)
    later = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "once"})
    helpers._round(f)
    assert _outcome(f, later["request_id"]) == "applied"
    assert calls and len(calls) == len(set(calls)), f"replica caller checks repeated per call: {calls}"


@pytest.mark.parametrize("ahead", [0, 3600])
def test_unproven_caller_is_refused_not_deferred(fleet, monkeypatch, ahead):
    """A forged call whose caller is unproven and whose created event is missing is refused at once (a future
    requested_at too): it must never be deferred (that would pin the watermark and page the tail every tick)."""
    import time

    from hermes_cli import kanban_operator_call as kox
    f = fleet
    other = f.card(f.h, "unrelated hub card")
    ghost = "t_" + "9" * 16  # no created event anywhere: home_refusal alone would defer it
    with kb.write_txn(f.h):
        kb._append_event(f.h, other, kox.REQUEST_KIND, {
            "request_uid": "future-forged", "board": "default", "action": "comment", "target_task_id": other,
            "args": {"text": "spoofed"}, "caller": {"gateway": "worker-5", "task_id": ghost, "run_id": 1,
                                                     "run_uid": "x"}, "requested_at": int(time.time()) + ahead})
    f.board()
    kox.apply_incoming(f.h)
    row = next((r for r in helpers._log(f.h, "execute") if r["request_uid"] == "future-forged"), None)
    assert row is not None, "the unproven call was deferred instead of refused"
    assert row["outcome"] == "refused" and "caller_unproven" in row["detail"], row
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id = ?", (other,)).fetchone()


def test_future_requested_at_never_defers_a_home_refusal():
    """home_refusal defers only within the grace window: a future time beyond skew is refused, not deferred."""
    import sqlite3
    import time

    from hermes_cli import kanban_consent as consent
    rep = sqlite3.connect(":memory:")
    rep.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, uid TEXT)")
    payload = {"action": "comment", "caller": {"task_id": "t_" + "9" * 16}}
    now = time.time()
    assert consent.home_refusal(rep, {**payload, "requested_at": now}, "t_x")[1] is True
    refusal, defer = consent.home_refusal(rep, {**payload, "requested_at": now + 3600}, "t_x")
    assert defer is False and "consent_unproven" in refusal
    from hermes_cli import kanban_operator_call as kox
    caller = {"gateway": "worker-5", "task_id": "t_" + "9" * 16, "run_uid": "x"}
    assert "in the future" in kox._caller_replica_refusal(
        rep, {"task_id": caller["task_id"]}, {"caller": caller, "requested_at": now + 3600})
    # Called without a precomputed replica result, _caller_refusal still runs the replica checks itself.
    refusal, retry = kox._caller_refusal(rep, "default", {"task_id": caller["task_id"]},
                                         {"caller": caller, "requested_at": now + 3600})
    assert retry is False and "in the future" in (refusal or "")


def test_without_a_deferral_one_tick_reads_one_batch(fleet, monkeypatch):
    """Paging past a full batch is only for a held watermark; otherwise the next tick resumes after it."""
    from hermes_cli import kanban_operator_call as kox
    f = fleet
    monkeypatch.setattr(kox, "SCAN_BATCH", 2)
    later = [helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": f"s{i}"})
             for i in range(3)]
    requests.drain(f.w)
    f.board()
    assert len(kox.apply_incoming(f.h)) == 2
    assert len(kox.apply_incoming(f.h)) == 1
    helpers._round(f)
    assert [_outcome(f, r["request_id"]) for r in later] == ["applied"] * 3


def test_submitting_session_stop_is_enforced_and_inherited(fleet, monkeypatch, tmp_path):
    """A worker run's own timed-out approval binds its submissions and the cards it creates."""
    from gateway.session_context import scoped_current_session_id
    f = fleet
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "worker-run-home"))
    approval_context._fire_approval_hook(
        "post_approval_response", session_id="worker-run-session",
        command=f"hermes kanban comment {f.target} 'probe payload'", choice="timeout", surface="gateway")
    op = {"op": "operator", "action": "comment", "task_id": f.target, "text": "probe payload"}
    with scoped_current_session_id("worker-run-session"):
        with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
            requests.submit(f.w, env_task=f.caller, env_run=str(f.run_id), ops=[op])
        child = f.card(f.w, "child of the stopped run", creator_task_id=f.caller, session_id="origin-chat")
    from hermes_cli import kanban_consent as consent
    with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
        consent.enforce(f.w, child, [{"op": "operator", "action": "comment", "task_id": f.target}])
    # Control: another session's submission is not bound by this run's stop.
    with scoped_current_session_id("other-session"):
        assert requests.submit(f.w, env_task=f.caller, env_run=str(f.run_id), ops=[op])["status"] == "pending"


def test_snapshot_uses_request_scoped_session_not_process_env(fleet, monkeypatch, tmp_path):
    """Two chats in one gateway: os.environ holds whichever bound last; the card follows its own request."""
    import contextvars
    from gateway.session_context import scoped_current_session_id
    from hermes_cli import kanban_consent as consent
    f = fleet
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "multi-chat-home"))
    _decide(f.target, "deny", session="chat-a")
    monkeypatch.setenv("HERMES_SESSION_ID", "chat-a")  # chat A set the process env last

    def create(session):
        with scoped_current_session_id(session):
            return f.card(f.w, f"card from {session}")
    from_b = contextvars.copy_context().run(create, "chat-b")
    from_a = contextvars.copy_context().run(create, "chat-a")
    op = [{"op": "operator", "action": "comment", "task_id": f.target}]
    consent.enforce(f.w, from_b, op)
    with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
        consent.enforce(f.w, from_a, op)


def _sessions(home, *chain):
    """Compression lineage root -> ... -> tip in the throwaway state.db."""
    from hermes_state import SessionDB
    db = SessionDB(home / "state.db")
    try:
        parent = None
        for sid in chain:
            db.create_session(session_id=sid, source="cli", model="test", parent_session_id=parent)
            parent = sid
    finally:
        db.close()


def test_stop_survives_compression_rotation(fleet, monkeypatch, tmp_path):
    """Compression rebinds the conversation to a child session id; its stops come along."""
    from hermes_cli import kanban_consent as consent
    f = fleet
    home = tmp_path / "compressed-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _decide(f.target, "timeout", session="pre-compression")
    _sessions(home, "pre-compression", "after-first", "after-second")
    assert [s["targets"] for s in consent.session_stops("after-second")] == [[f.target]]
    card = f.card(f.w, "created after compression", session_id="after-second")
    with pytest.raises(requests.SupervisorRequestError, match="consent_stop"):
        consent.enforce(f.w, card, [{"op": "operator", "action": "comment", "task_id": f.target}])
    # An explicit approval later in the lineage still supersedes the inherited stop.
    _decide(f.target, "once", session="after-second")
    assert consent.session_stops("after-second") == []
    # Control: a sibling lineage is not bound by this conversation's stop.
    _sessions(home, "unrelated-root", "unrelated-tip")
    assert consent.session_stops("unrelated-tip") == []


def test_worker_kanban_comment_tool_honours_creation_stop(fleet, monkeypatch, tmp_path):
    """A same-board child cannot comment the stopped target directly through kanban_comment."""
    import json as _json
    from hermes_cli import kanban_consent as consent
    f = fleet
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "local-comment-home"))
    sibling = f.card(f.w, "stopped same-board target")
    _decide(sibling, "deny", session="local-chat")
    child = f.card(f.w, "child of the stopped chat", session_id="local-chat")
    assert consent.comment_refusal(f.w, child, None, sibling)
    assert consent.comment_refusal(f.w, child, None, f.target) is None  # other target: no stop
    assert consent.comment_refusal(f.w, None, "local-chat", sibling)    # the stopped session itself

    import tools.kanban_tools as kt
    from contextlib import contextmanager

    @contextmanager
    def board(_slug=None):
        yield kb, f.w
    monkeypatch.setattr(kt, "_board", board)
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    out = _json.loads(kt._handle_comment({"task_id": sibling, "body": "must not land"}))
    assert out.get("ok") is not True and "consent_stop" in _json.dumps(out), out
    assert not f.w.execute("SELECT 1 FROM task_comments WHERE task_id=?", (sibling,)).fetchone()
    # Control: the same worker may still comment an unstopped card.
    other = f.card(f.w, "unstopped sibling")
    assert _json.loads(kt._handle_comment({"task_id": other, "body": "fine"}))["ok"] is True
