"""Durable request recovery across connections; no live Board writes."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_caused_review as caused
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli import kanban_spend_review_request as journal
from tests.hermes_cli import test_kanban_spend_caused_review as helpers

kanban_home = helpers.kanban_home
board = helpers.board


def started(conn):
    tid, ep = helpers.stop(conn)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, reviewer.QUEUED, {"episode": ep})
        kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep, "deadline": 0})
    return tid, ep


@pytest.mark.parametrize("failure", ["lost_reply", "malformed", "deferred", "bad_receipt", "lost_readback"])
def test_restart_recovers_original_choice(kanban_home, spend_reviewer, board, monkeypatch, failure):
    hub, calls = board
    real_ops, real_read = hub.ops, hub.row_evidence
    def uncertain(board_id, ops):
        result = real_ops(board_id, ops)
        if failure == "lost_reply":
            raise OSError("reply lost after commit")
        if failure == "malformed":
            return {"results": [None]}
        if failure == "deferred":
            return {"results": [{"client_op_id": ops[0]["client_op_id"], "status": "deferred"}]}
        return result
    hub.ops = uncertain
    if failure == "bad_receipt":
        hub.mutate = lambda row: row.update(payload="{}")
    if failure == "lost_readback":
        def lost(*args):
            raise OSError("readback unavailable")
        hub.row_evidence = lost
    monkeypatch.setattr(reviewer, "judge", lambda ctx: {
        "decision": "release", "reason": "original decision", "remaining_quote": helpers.QUOTE})
    with kbc.connect_closing() as conn:
        tid, ep = started(conn)
        assert reviewer.review(conn, tid, ep) == "pending"
        frozen = journal.get(conn, tid, ep)
        assert frozen["wire"] == calls[0]
        assert not conn.in_transaction
    # New connection simulates dispatcher restart; new config/context cannot choose again.
    hub.ops, hub.row_evidence = real_ops, real_read
    hub.mutate = lambda row: None
    monkeypatch.setattr(reviewer, "judge", lambda ctx: pytest.fail("second judge choice"))
    monkeypatch.setattr(reviewer, "settings", lambda: (True, 1))
    with kbc.connect_closing() as conn:
        assert reviewer.review(conn, tid, ep) == "settled"
        assert journal.get(conn, tid, ep) == frozen
        assert reviewer.review(conn, tid, ep) == "moot"
        assert len(calls) == 1
        assert kb.get_task(conn, tid).status == "triage"


def test_unknown_absence_resubmits_exact_bytes_not_new_verdict(kanban_home, spend_reviewer, board, monkeypatch):
    hub, calls = board
    real_ops, real_read = hub.ops, hub.row_evidence
    def not_received(board_id, ops):
        calls.append(ops[0])
        raise OSError("connection lost before receipt")
    hub.ops = not_received
    monkeypatch.setattr(reviewer, "judge", lambda ctx: {
        "decision": "release", "reason": "frozen", "remaining_quote": helpers.QUOTE})
    with kbc.connect_closing() as conn:
        tid, ep = started(conn)
        assert reviewer.review(conn, tid, ep) == "pending"
        original = json.dumps(calls[0], sort_keys=True)
        hub.ops = real_ops
        reads = []
        def read(*args):
            reads.append(args)
            return {"state": "unknown"} if len(reads) == 1 else real_read(*args)
        hub.row_evidence = read
        monkeypatch.setattr(reviewer, "judge", lambda ctx: pytest.fail("second judge"))
        monkeypatch.setattr(reviewer, "settings", lambda: (True, 1))
        assert reviewer.review(conn, tid, ep) == "settled"
        assert len(calls) == 2
        assert json.dumps(calls[1], sort_keys=True) == original


@pytest.mark.parametrize("fence", ["safety", "human", "containment", "cap", "episode", "projected"])
def test_unknown_with_new_fence_read_only(kanban_home, spend_reviewer, board, monkeypatch, fence):
    hub, calls = board
    def uncertain(board_id, ops):
        calls.append(ops[0])
        raise OSError("unknown")
    hub.ops = uncertain
    monkeypatch.setattr(reviewer, "judge", lambda ctx: {
        "decision": "release", "reason": "frozen", "remaining_quote": helpers.QUOTE})
    with kbc.connect_closing() as conn:
        tid, ep = started(conn)
        assert reviewer.review(conn, tid, ep) == "pending"
        hub.row_evidence = lambda *a: {"state": "unknown"}
        if fence == "human":
            kb.add_comment(conn, tid, "operator", "Wait for me", writer_kind="human")
        elif fence == "containment":
            monkeypatch.setattr(caused, "fenced", lambda *a: True)
        elif fence == "cap":
            monkeypatch.setattr(caused, "grant_count", lambda *a: 2)
        else:
            with kb.write_txn(conn):
                if fence == "safety":
                    kb._append_event(conn, tid, "decision_hold_restricted", {"episode": ep, "cause": "safety_refusal"})
                elif fence == "projected":
                    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
                else:
                    kb._append_event(conn, tid, "spend_budget_triaged", {"cause": "scope_stopped"})
        assert reviewer.review(conn, tid, ep) == "pending"
        assert len(calls) == 1
        assert reviewer.state(conn, tid, ep) == reviewer.STARTED
        # Recovery remains schedulable even after the canonical feed moved the task.
        assert (tid, ep, 1) in reviewer._due(conn, 9999999999, set())


def test_journal_rejects_outer_transaction_and_keeps_first_request(kanban_home):
    with kbc.connect_closing() as conn:
        wire = {"task": "t", "episode": "e", "reason": "first"}
        with kb.write_txn(conn), pytest.raises(ValueError, match="another transaction"):
            journal.freeze(conn, "b", "g", wire)
        assert journal.freeze(conn, "b", "g", wire)["wire"] == wire
        assert journal.freeze(conn, "b", "g", {**wire, "reason": "replacement"})["wire"] == wire


def test_refusal_survives_crash_before_escalation(kanban_home, spend_reviewer, board, monkeypatch):
    hub, calls = board
    def reject(board_id, ops):
        calls.append(ops[0])
        return {"results": [{"client_op_id": ops[0]["client_op_id"], "status": "rejected",
                             "reason": "capability_unattested"}]}
    hub.ops = reject
    with kbc.connect_closing() as conn:
        tid, ep = started(conn)
        with pytest.raises(caused.Refused):
            caused.canonical(conn, tid, ep, {"verdict": "release", "reason": "finish", "quote": helpers.QUOTE}, 2)
    monkeypatch.setattr(reviewer, "judge", lambda ctx: pytest.fail("second choice"))
    monkeypatch.setattr(helpers.decision, "safe_ensure_producer", lambda *a: None)
    with kbc.connect_closing() as conn:
        assert reviewer.review(conn, tid, ep) == "escalated"
        assert reviewer.state(conn, tid, ep) == reviewer.ESCALATED
        assert len(calls) == 1


def test_frozen_recovery_survives_judge_attempt_cap(kanban_home, spend_reviewer, board, monkeypatch):
    hub, calls = board
    real_read = hub.row_evidence
    hub.row_evidence = lambda *a: {"state": "unknown"}
    monkeypatch.setattr(reviewer, "judge", lambda ctx: {
        "decision": "release", "reason": "finish", "remaining_quote": helpers.QUOTE})
    with kbc.connect_closing() as conn:
        tid, ep = started(conn)
        assert reviewer.review(conn, tid, ep) == "pending"
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep, "deadline": 0})
        hub.row_evidence = real_read
        monkeypatch.setattr(reviewer, "judge", lambda ctx: pytest.fail("second choice"))
        assert reviewer.start_due(conn, now=9999999999) == [tid]
        reviewer.wait_for_reviews()
        assert reviewer.state(conn, tid, ep) == reviewer.SETTLED
        assert len(calls) == 1


def test_target_contract_excludes_run_only_names():
    verdict = {"decision": "wait_on", "reason": "wait", "target": "t_other"}
    ctx = {"task_id": "t_source", "evidence": ["run mentioned t_other"], "target_evidence": []}
    with pytest.raises(ValueError, match="not named"):
        caused.validate(verdict, ctx)
    ctx["target_evidence"] = ["stop names t_other"]
    assert caused.validate(verdict, ctx)["target"] == "t_other"
