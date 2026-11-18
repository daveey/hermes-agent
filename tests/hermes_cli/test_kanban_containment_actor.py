"""Runtime admission for CLI/tool containment, never caller-supplied identity."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_containment_actor as actors
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from tests.hermes_cli import test_kanban_containment_lifecycle as fixtures

board = fixtures.board


def bind(conn, monkeypatch):
    relay, _, _, _, _ = fixtures.setup(conn)
    task = kb.get_task(conn, relay)
    assert task is not None
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    session = "fixture-contained-owner"
    for key, value in {"HERMES_KANBAN_DB": path, "HERMES_KANBAN_TASK": relay,
                       "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
                       "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock, "HERMES_SESSION_ID": session}.items():
        monkeypatch.setenv(key, value)
    from gateway.session_context import scoped_current_session_id
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    with scoped_current_session_id(session):
        kb._append_event(conn, relay, "owner_admitted", {"session_id": session}, run_id=task.current_run_id)
    return relay, task.current_run_id, session


def test_exact_runtime_claim_and_owner_session_are_required(board, monkeypatch):
    relay, _, session = bind(board, monkeypatch)
    from gateway.session_context import scoped_current_session_id
    with scoped_current_session_id(session):
        actor = actors.current_actor(board)
    assert actor.task == relay
    assert actor.run_uid == fixtures.run_uid(board, relay)


@pytest.mark.parametrize("key,value", [
    ("HERMES_KANBAN_TASK", ""), ("HERMES_KANBAN_TASK", "t_0123456789abcdef"),
    ("HERMES_KANBAN_DB", ""), ("HERMES_KANBAN_DB", "/does-not-exist/fixture.db"),
    ("HERMES_KANBAN_RUN_ID", ""), ("HERMES_KANBAN_RUN_ID", "bad"),
    ("HERMES_KANBAN_RUN_ID", "١"), ("HERMES_KANBAN_RUN_ID", "9999999"),
    ("HERMES_KANBAN_CLAIM_LOCK", ""), ("HERMES_KANBAN_CLAIM_LOCK", "other"),
])
def test_missing_or_changed_runtime_pins_refuse(board, monkeypatch, key, value):
    _, _, session = bind(board, monkeypatch)
    monkeypatch.setenv(key, value)
    from gateway.session_context import scoped_current_session_id
    with scoped_current_session_id(session), pytest.raises(ContainmentRefused):
        actors.current_actor(board)


@pytest.mark.parametrize("scope", ["delegate", "cron"])
def test_inprocess_child_cannot_borrow_workers_claim(board, monkeypatch, scope):
    _, _, session = bind(board, monkeypatch)
    from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
    from gateway.session_context import scoped_current_session_id
    context = delegated_child_context() if scope == "delegate" else non_dispatcher_owned_context()
    with context, scoped_current_session_id(session), pytest.raises(ContainmentRefused):
        actors.current_actor(board)


def test_informational_worker_task_never_grants_admission(board, monkeypatch):
    relay, _, session = bind(board, monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_TASK", relay)
    from gateway.session_context import scoped_current_session_id
    with scoped_current_session_id(session), pytest.raises(ContainmentRefused):
        actors.current_actor(board)


@pytest.mark.parametrize("case", ["missing", "unrelated_run", "wrong_session", "malformed", "nonobject", "empty_context"])
def test_owner_adoption_must_bind_exact_session_and_current_run(board, monkeypatch, case):
    relay, rid, session = bind(board, monkeypatch)
    if case in ("missing", "unrelated_run"):
        board.execute("UPDATE task_events SET run_id=? WHERE task_id=? AND kind='owner_admitted'",
                      (None if case == "missing" else rid + 1, relay))
    elif case in ("malformed", "nonobject"):
        board.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='owner_admitted'",
                      ("{bad" if case == "malformed" else json.dumps([session]), relay))
    elif case == "wrong_session":
        session = "different-runtime-session"
    else:
        session = ""  # Context-local empty must NOT fall back to an ambient owner session.
    from gateway.session_context import scoped_current_session_id
    with scoped_current_session_id(session), pytest.raises(ContainmentRefused):
        actors.current_actor(board)


def test_ended_owner_cannot_start_new_containment(board, monkeypatch):
    relay, rid, session = bind(board, monkeypatch)
    board.execute("UPDATE task_runs SET ended_at=1 WHERE id=?", (rid,))
    from gateway.session_context import scoped_current_session_id
    with scoped_current_session_id(session), pytest.raises(ContainmentRefused):
        actors.current_actor(board)
    assert kb.get_task(board, relay).status == "running"
