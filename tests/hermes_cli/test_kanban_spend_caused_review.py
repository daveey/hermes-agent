"""Caused-stop integration; fake authenticated Board, never a live Board probe."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_caused_review as caused
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home
QUOTE = "Finish the remaining integration and verify the acceptance tests"


def stop(conn, cause: str | None = "scope_stopped"):
    tid = kb.create_task(conn, title="Finish acceptance", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "spend_budget_triaged", {"cause": cause, "reason": QUOTE})
    episode = reviewer._current_episode(conn, tid)
    assert episode is not None
    with kb.write_txn(conn):
        kb._append_event(conn, tid, decision.NOTICE, {"episode": episode})
    return tid, episode


@pytest.mark.parametrize("cause", ["scope_stopped", "capability_unavailable"])
@pytest.mark.parametrize("delay", [0, 300, 900])
def test_caused_stop_delayed_queue(kanban_home, spend_reviewer, monkeypatch, cause, delay):
    monkeypatch.setattr(reviewer, "review_after_seconds", lambda: delay)
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, cause)
        noticed = reviewer._notice_at(conn, tid, ep)
        with kb.write_txn(conn):
            assert not reviewer.queue(conn, tid, ep)
            assert not reviewer.queue(conn, tid, ep, backlog=True, now=noticed + delay - 1)
        assert reviewer.queue_backlog(conn, now=noticed + delay - 1, cfg=(True, 2)) == []
        assert reviewer.queue_backlog(conn, now=noticed + delay, cfg=(True, 2)) == [tid]
        assert reviewer.queue_backlog(conn, now=noticed + delay, cfg=(True, 2)) == []


@pytest.mark.parametrize("cause", ["scope_stopped", "capability_unavailable", "safety_refusal"])
def test_late_annotation_cannot_inherit_immediate_start(kanban_home, spend_reviewer, cause):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, None)
        noticed = reviewer._notice_at(conn, tid, ep)
        with kb.write_txn(conn):
            assert reviewer.queue(conn, tid, ep)
            kb._append_event(conn, tid, "decision_hold_restricted", {"episode": ep, "cause": cause})
        if cause == "safety_refusal":
            # Due for model-free retirement, not a review thread.
            assert reviewer._due(conn, noticed + 299, set()) == [(tid, ep, 0)]
            assert not reviewer.holds(conn, tid, ep)
        else:
            assert reviewer._due(conn, noticed + 299, set()) == []
            assert reviewer._due(conn, noticed + 300, set()) == [(tid, ep, 0)]


@pytest.mark.parametrize("cause", ["decision_pending", "safety_refusal"])
@pytest.mark.parametrize("capacity", [0, 3])
def test_excluded_queue_retires_and_remains_discoverable(kanban_home, spend_reviewer, monkeypatch, cause, capacity):
    from hermes_cli import kanban_spend_stop_watch as watch
    monkeypatch.setattr(reviewer, "MAX_CONCURRENT", capacity)
    monkeypatch.setattr(reviewer, "judge", lambda ctx: pytest.fail("excluded stop reached judge"))
    routes = []
    monkeypatch.setattr(decision, "safe_ensure_producer", lambda conn, tid: routes.append(tid))
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, None)
        now = reviewer._notice_at(conn, tid, ep)
        assert now is not None
        with kb.write_txn(conn):
            assert reviewer.queue(conn, tid, ep)
            if cause == "decision_pending":
                kb._append_event(conn, tid, "decision_retained", {
                    "episode_uid": ep, "disposition": {"reason": cause}})
            else:
                kb._append_event(conn, tid, "decision_hold_restricted", {"episode": ep, "cause": cause})
        assert not reviewer.holds(conn, tid, ep)
        assert [c['id'] for c in watch.stale_stops(conn, now=now + 3600)] == [tid]
        for elapsed in (3600, 86400):
            assert reviewer.start_due(conn, now=now + elapsed) == []
            reviewer.wait_for_reviews()
            assert reviewer.state(conn, tid, ep) == reviewer.ESCALATED
            assert [c['id'] for c in watch.stale_stops(conn, now=now + elapsed)] == [tid]
        assert routes == [tid]
        assert kb.get_task(conn, tid).status == "triage"
        assert caused.effective_cause(conn, tid, decision.pending_episode(conn, tid)) == cause
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                                "('spend_review_started','scope_renewed','promoted_manual','spend_budget_set')",
                                (tid,)).fetchone()
        kb.create_task(conn, title="Decide held stop", assignee="default", created_by="kanban-spend-budget",
                       idempotency_key=f"spend-budget-decision:{tid}:{ep}")
        assert watch.stale_stops(conn, now=now + 86400) == []


def test_annotation_on_ordinary_episode_and_safety_precedence(kanban_home, spend_reviewer):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, None)
        root = decision.pending_episode(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decision_hold_restricted", {"episode": "other", "cause": "scope_stopped"})
        assert caused.effective_cause(conn, tid, root) is None
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decision_hold_restricted", {"episode": ep, "cause": "scope_stopped"})
        assert caused.effective_cause(conn, tid, root) == "scope_stopped"
        assert reviewer.eligible(conn, tid, ep)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decision_hold_restricted", {"episode": ep, "cause": "safety_refusal"})
        assert caused.effective_cause(conn, tid, root) == "safety_refusal"
        assert not reviewer.eligible(conn, tid, ep)


@pytest.mark.parametrize("fence", ["human", "foreign", "containment", "admin", "cap", "disabled"])
def test_queue_fences(kanban_home, spend_reviewer, monkeypatch, fence):
    from hermes_cli import kanban_wake_check, kanban_containment_guard, kanban_stop_ended
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
        actions = {
            "human": lambda: kb.add_comment(conn, tid, "operator", "Wait for me", writer_kind="human"),
            "foreign": lambda: monkeypatch.setattr(kanban_wake_check, "foreign_copy", lambda *a: True),
            "containment": lambda: monkeypatch.setattr(kanban_containment_guard, "pending", lambda *a: True),
            "admin": lambda: monkeypatch.setattr(kanban_stop_ended, "stopped_at", lambda *a: 1),
            "cap": lambda: monkeypatch.setattr(caused, "grant_count", lambda *a: 2),
            "disabled": lambda: monkeypatch.setattr(reviewer, "settings", lambda: (False, 2)),
        }
        actions[fence]()
        assert not reviewer.eligible(conn, tid, ep)


@pytest.fixture
def board(monkeypatch, kanban_home):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    calls = []
    class Hub:
        ident = SimpleNamespace(gateway="home")
        capability = {"spend_review": 1}
        mutate = staticmethod(lambda row: None)
        def decision_retain_capability(self, board):
            return self.capability
        def ops(self, board, ops):
            calls.append(ops[0])
            return {"results": [{"client_op_id": ops[0]["event_uid"], "status": "applied"}]}
        def row_evidence(self, board, table, uid):
            op = calls[-1]
            payload = {k: op[k] for k in ("task", "episode", "verdict", "reason", "max_grants")}
            payload.update(version=1, runs=10 if op["verdict"] == "release" else None,
                           quote=op.get("quote"), target=op.get("target"), request_uid=uid,
                           request_sha256=hashlib.sha256(json.dumps(op, sort_keys=True, separators=(",", ":"),
                                                                  ensure_ascii=False).encode()).hexdigest(),
                           actor_kind="system", actor_session=None, actor_gateway="home",
                           authority_kind="spend_reviewer", authority_ref="kanban-spend-reviewer")
            row = dict(uid=uid, task_id=op["task"], kind="spend_review", version=1,
                       origin_gateway="home", deleted_at=None, payload=json.dumps(payload))
            self.mutate(row)
            return {"board": board, "table": table, "uid": uid, "state": "live", "row": row}
        def close(self):
            pass
    hub = Hub()
    hub.conn = kbc.connect()
    monkeypatch.setattr(sync, "sync_state", lambda c: {"board_id": "test"})
    monkeypatch.setattr(sync, "board_mode", lambda c: "primary")
    monkeypatch.setattr(client, "load_identity", lambda: None)
    monkeypatch.setattr(client, "Hub", lambda ident, **kw: hub)
    monkeypatch.setattr(client, "push", lambda *a, **kw: None)
    yield hub, calls
    hub.conn.close()


@pytest.mark.parametrize("choice", ["release", "wait_on", "archive", "escalate"])
def test_supported_wire_and_exact_receipt(board, choice):
    hub, calls = board
    fields = {"verdict": choice, "reason": "bounded decision"}
    if choice == "release":
        fields["quote"] = QUOTE
    if choice in {"wait_on", "archive"}:
        fields["target"] = "t_target"
    receipt = caused.canonical(hub.conn, "t_source", "episode", fields, 9)
    assert receipt["verdict"] == choice
    assert calls[0]["max_grants"] == 2
    assert not set(calls[0]) & {"actor_kind", "actor_session", "authority_kind", "payload"}
    caused.canonical(hub.conn, "t_source", "episode", fields, 9)
    assert len(calls) == 1  # receipt reconciles without another submission


@pytest.mark.parametrize("field,value", [("origin_gateway", "foreign"), ("uid", "other"),
                                         ("version", 2), ("task_id", "other"), ("kind", "scope_renewed"),
                                         ("deleted_at", 1), ("payload", "{}")])
def test_bad_readback_is_not_success(board, field, value):
    hub, _calls = board
    hub.mutate = lambda row: row.update({field: value})
    with pytest.raises(ValueError, match="receipt mismatch"):
        caused.canonical(hub.conn, "t_source", "episode", {"verdict": "release", "reason": "ok", "quote": QUOTE}, 2)


@pytest.mark.parametrize("capability", [{}, {"spend_review": True}, {"spend_review": 0}, None])
def test_unknown_capability_never_sends(board, capability):
    hub, calls = board
    hub.capability = capability
    with pytest.raises(ValueError, match="unavailable"):
        caused.canonical(hub.conn, "t_source", "episode", {"verdict": "escalate", "reason": "ok"}, 2)
    assert calls == []


def test_unsynced_has_no_local_fallback(monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(sync, "sync_state", lambda c: None)
    with pytest.raises(ValueError, match="canonical"):
        caused.canonical(None, "t_source", "episode", {}, 2)


def test_capability_unknown_and_fabricated_quote_escalate():
    ctx = {"task_id": "t_source", "cause": "scope_stopped", "evidence": [QUOTE]}
    verdict = {"decision": "release", "reason": "Finish acceptance", "remaining_quote": QUOTE}
    assert caused.validate(verdict, ctx)["quote"] == QUOTE
    with pytest.raises(ValueError, match="verbatim"):
        caused.validate({**verdict, "remaining_quote": QUOTE.lower()}, ctx)
    assert caused.validate(verdict, {**ctx, "cause": "capability_unavailable"})["quote"] == QUOTE
    with pytest.raises(ValueError, match="not named"):
        caused.validate({"decision": "wait_on", "reason": "wait", "target": "t_other"}, ctx)


@pytest.mark.parametrize("runs", [None, 0, 1, 10, True, "10"])
def test_canonical_verdict_cannot_silently_replace_custom_budget(runs):
    with pytest.raises(ValueError, match="custom run budget"):
        caused.validate({"decision": "release", "reason": "Finish acceptance", "remaining_quote": QUOTE,
                         "runs": runs}, {"task_id": "t_source", "evidence": [QUOTE]})


@pytest.mark.parametrize("choice", ["release", "grant"])
def test_primary_ordinary_uses_fixed_canonical_contract(kanban_home, spend_reviewer, board, monkeypatch, choice):
    _hub, calls = board
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, None)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='spend_budget_triaged'",
                         (json.dumps({"reason": QUOTE, "runs": 20, "since": 0}), tid))
            kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep})
        ctx = reviewer.context(conn, tid, ep)
        assert ctx["_canonical"] is True
        assert QUOTE in ctx["evidence"]
        monkeypatch.setattr(reviewer, "judge", lambda ctx: {
            "decision": choice, **({"runs": 1} if choice == "grant" else {}), "reason": "Finish acceptance",
            "remaining_quote": QUOTE, "progress_quote": QUOTE})
        monkeypatch.setattr(decision, "safe_ensure_producer", lambda *a: None)
        assert reviewer.review(conn, tid, ep) == ("settled" if choice == "release" else "escalated")
        assert len(calls) == (1 if choice == "release" else 0)
        assert kb.get_task(conn, tid).status == "triage"
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                                "('scope_renewed','spend_budget_set','promoted_manual')", (tid,)).fetchone()


def test_primary_direct_grant_never_writes_local_budget(kanban_home, spend_reviewer, board):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, None)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep})
        assert reviewer.grant(conn, tid, ep, 10, "wrong route", QUOTE) is not None
        assert kb.get_task(conn, tid).status == "triage"


def test_review_calls_canonical_without_local_renewal(kanban_home, spend_reviewer, board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn)
        noticed = reviewer._notice_at(conn, tid, ep)
        assert reviewer.queue_backlog(conn, now=noticed + 300, cfg=(True, 2)) == [tid]
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep})
        monkeypatch.setattr(reviewer, "judge", lambda ctx: {
            "decision": "release", "reason": "Finish acceptance", "remaining_quote": QUOTE})
        assert reviewer.review(conn, tid, ep) == "settled"
        assert reviewer.state(conn, tid, ep) == reviewer.SETTLED
        assert kb.get_task(conn, tid).status == "triage"  # canonical feed owns projection
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()
        assert caused.grant_count(conn, tid) == 1


@pytest.mark.parametrize("failure", ["unsupported", "unconfirmed", "human_during_judge", "capability"])
def test_review_escalates_fail_closed(kanban_home, spend_reviewer, board, monkeypatch, failure):
    hub, calls = board
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, "capability_unavailable" if failure == "capability" else "scope_stopped")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep})
        if failure == "unsupported":
            hub.capability = {}
        if failure == "unconfirmed":
            hub.mutate = lambda row: row.update(payload="{}")
        if failure == "capability":
            hub.ops = lambda board, ops: {"results": [{"client_op_id": ops[0]["client_op_id"],
                                                     "status": "rejected", "reason": "capability_unattested"}]}
        def judge(ctx):
            if failure == "human_during_judge":
                kb.add_comment(conn, tid, "operator", "Do not continue", writer_kind="human")
            return {"decision": "release", "reason": "Finish acceptance", "remaining_quote": QUOTE}
        monkeypatch.setattr(reviewer, "judge", judge)
        monkeypatch.setattr(decision, "safe_ensure_producer", lambda *a: None)
        assert reviewer.review(conn, tid, ep) == ("pending" if failure == "unconfirmed" else "escalated")
        assert reviewer.state(conn, tid, ep) == (reviewer.STARTED if failure == "unconfirmed" else reviewer.ESCALATED)
        assert kb.get_task(conn, tid).status == "triage"
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()
        if failure != "unconfirmed":
            assert calls == []


@pytest.mark.parametrize("cause", ["safety_refusal", "decision_pending"])
def test_safety_added_after_queue_never_calls_judge(kanban_home, spend_reviewer, monkeypatch, cause):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, None)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep})
            kb._append_event(conn, tid, "decision_hold_restricted", {"episode": ep, "cause": cause})
        monkeypatch.setattr(reviewer, "judge", lambda ctx: pytest.fail("safety must not reach judge"))
        monkeypatch.setattr(decision, "safe_ensure_producer", lambda *a: None)
        assert reviewer.review(conn, tid, ep) == "escalated"
        assert kb.get_task(conn, tid).status == "triage"


def test_ordinary_grant_cannot_clear_capability_stop(kanban_home, spend_reviewer):
    with kbc.connect_closing() as conn:
        tid, ep = stop(conn, "capability_unavailable")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep})
        assert reviewer.grant(conn, tid, ep, 10, "wrong route", QUOTE) is not None
        assert kb.get_task(conn, tid).status == "triage"
