"""Consumed delegation results resolve retained losses without requiring an empty board."""
from __future__ import annotations

import json
import time

import pytest

from tests.hermes_state import test_session_status_refresh as fixtures
from tools import async_delegation as ad

board, db, receipts = fixtures.board, fixtures.db, fixtures.receipts
add_card, refresh, turn = fixtures.add_card, fixtures.refresh, fixtures.turn


def child(db, sid="child", reason="delegation_unknown"):
    db.create_session(sid, source="subagent", parent_session_id="chat")
    db.append_message(sid, role="user", content="build")
    db.append_message(sid, role="assistant", content="working", tool_calls=[
        {"id": "call", "type": "function", "function": {"name": "process_manage", "arguments": "{}"}}])
    db.end_session(sid, reason)


def delivered(db, parent="chat", sid="child"):
    # Real ledger write and delivery acknowledgement, not an injected reducer flag.
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("INSERT INTO async_delegations (delegation_id, origin_session, parent_session_id, state,"
                     " dispatched_at, updated_at, delivery_state, task_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     ("deleg-test", parent, parent, "unknown", time.time(), time.time(), "pending",
                      json.dumps({"task_session_ids": {"0": sid}})))
    assert ad.mark_completion_delivered("deleg-test")


@pytest.fixture(autouse=True)
def ledger(db, monkeypatch):
    ad._reset_for_tests()
    monkeypatch.setattr(ad, "_db_path", lambda: db.db_path)
    yield
    ad._reset_for_tests()


@pytest.mark.parametrize("ack", [False, True])
def test_retained_loss_successor_open_cards(db, board, ack):
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    turn(db, "independently verified work")
    add_card(board, "t_wait", "todo")
    if ack:
        delivered(db)
    status = refresh(db)
    assert bool(status["failures"]) is not ack
    assert status["kind"] == ("waiting" if ack else "unresolved")


def test_successful_successor_alone_cannot_acknowledge_loss(db):
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    turn(db, "unrelated successful reply")
    outcome = refresh(db)
    assert outcome["failures"]
    assert outcome["kind"] == "unresolved"


def test_late_terminal_transcript_reconciles_retained_loss(db, board):
    turn(db)
    child(db, reason="startup_orphan_reap")
    assert refresh(db)["failures"]
    db.append_message("child", role="tool", content="finished", tool_call_id="call", tool_name="process_manage")
    db.append_message("child", role="assistant", content='{"pr_url":"https://example.test/1","verified":true}')
    db._write_sql("UPDATE sessions SET end_reason='agent_close' WHERE id='child'")
    turn(db)
    add_card(board, "t_wait", "todo")
    assert refresh(db)["failures"] == []


def test_foreign_delivery_cannot_acknowledge_loss(db):
    turn(db)
    child(db)
    delivered(db, parent="foreign")
    assert refresh(db)["failures"]


def test_pending_delivery_does_not_acknowledge(db):
    turn(db)
    child(db)
    delivered(db)
    import queue
    db._write_sql("UPDATE async_delegations SET event_json=?", (json.dumps({"type": "async_delegation", "delegation_id": "deleg-test"}),))
    assert ad.requeue_admitted_completion("deleg-test", queue.Queue())
    assert refresh(db)["failures"]


def test_acknowledgement_survives_ledger_pruning(db):
    turn(db)
    child(db)
    delivered(db)
    db._write_sql("DELETE FROM async_delegations")
    assert refresh(db)["failures"] == []


def test_old_delivery_does_not_cover_continued_child(db):
    turn(db)
    child(db)
    delivered(db)
    later = time.time() + 60
    db._write_sql("UPDATE sessions SET last_activity_at=? WHERE id='child'", (later,))
    assert refresh(db)["failures"]


def test_explicit_acknowledgement_reaches_cli_and_persists(db, board, monkeypatch, capsys):
    from types import SimpleNamespace
    from hermes_cli import sessions_cmd_outcome as cli
    turn(db)
    child(db)
    assert refresh(db)["failures"]
    add_card(board, "t_wait", "todo")
    monkeypatch.setattr(cli, "ensure_outcome_hooks", lambda: None)
    args = SimpleNamespace(session_id="chat", refresh=False, json=True, ack_subagent=["child"],
                           evidence="Independent PR merge and installed proof verified")
    cli.cmd_outcome(db, args)
    assert json.loads(capsys.readouterr().out)["outcome"]["status"]["failures"] == []
    assert refresh(db)["kind"] == "waiting"


@pytest.mark.parametrize("target,evidence", [("foreign", "verified"), ("child", ""), ("live", "verified")])
def test_explicit_acknowledgement_refuses_unowned_live_or_unexplained(db, target, evidence):
    from hermes_state_subagent_resolution import acknowledge_subagents
    turn(db)
    child(db)
    db.create_session("live", source="subagent", parent_session_id="chat")
    with pytest.raises(ValueError):
        acknowledge_subagents(db, "chat", [target], evidence)
    assert db._read_one("SELECT value FROM state_meta WHERE key='subagent_ack:child'") is None
