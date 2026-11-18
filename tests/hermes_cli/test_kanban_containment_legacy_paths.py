"""Executable legacy-path evidence: none is a non-human containment operation.

These controls intentionally retain the old semantics. The protected containment
suite must supply a different transition, not weaken any of these boundaries.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_assignment_routing as assignment
from hermes_cli import kanban_awaiting as awaiting
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_RUN_ID",
                "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    from tests.hermes_cli.kanban_sync_support import write_sync_config
    write_sync_config(home, False)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _relay(conn):
    relay = kb.create_task(conn, title="Lifecycle relay fixture", assignee="default", created_by="default")
    consumer = kb.create_task(conn, title="Dependent consumer fixture", assignee="default", parents=[relay])
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, relay, claimer="default") is not None
    return relay, consumer


def _edges(conn):
    return {(row[0], row[1]) for row in conn.execute("SELECT parent_id, child_id FROM task_links")}


@pytest.mark.parametrize("status", ["triage", "archived"])
def test_remote_inactive_producer_cannot_be_a_scheduled_containment(board, monkeypatch, status):
    """Use the real tool/confirmation seam; fake only the remote read transport."""
    from hermes_cli import kanban_wait_check
    from tools import kanban_tools
    relay, consumer = _relay(board)
    before = _edges(board)
    producer = "t_aabbccdd11223344"
    monkeypatch.setattr(kanban_wait_check, "check_remote", lambda *a, **kw: (1, f"{producer} {status}"))
    monkeypatch.setenv("HERMES_KANBAN_TASK", relay)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(board, relay).current_run_id))
    result = json.loads(kanban_tools._handle_schedule({
        "task_id": relay, "reason": "Wait for the named producer",
        "awaiting": {"kind": "card", "ref": producer, "gateway": "fixture-peer"},
    }))
    assert awaiting.AGENT_ACTION_WAIT in json.dumps(result), result
    assert kb.get_task(board, relay).status == "running"
    assert kb.get_task(board, consumer).status == "todo"
    assert _edges(board) == before


def test_gate_only_creator_is_not_an_assignment_supervisor(board, monkeypatch):
    from hermes_cli import config
    from hermes_cli import kanban_profile_runnable
    from hermes_cli import profiles
    from hermes_cli.config_backend import config_exists as real_config_exists
    import hermes_cli.config_backend as backend

    relay, consumer = _relay(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET created_by='fixture-gate' WHERE id=?", (relay,))
    monkeypatch.setattr(config, "load_config_readonly", dict)
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda name: Path.home() / name)
    monkeypatch.setattr(backend, "config_exists", lambda p: p.parent.name == "fixture-gate" or real_config_exists(p))
    # Only the configured admission gate can run this creator. Model the actual
    # registry/profile-read boundary, not an invented gate-only supervisor API.
    monkeypatch.setattr(kanban_profile_runnable, "runnable", lambda name: name != "fixture-gate")
    task = kb.get_task(board, relay)
    with pytest.raises(assignment.AssignmentRoutingError, match="no_capable_supervisor"):
        with kb.write_txn(board):
            assignment.route(board, task, {"reason": "profile capability mismatch"})
    assert kb.get_task(board, relay).status == "running"
    assert kb.get_task(board, consumer).status == "todo"


def test_review_transition_is_dispatchable_not_containment(board):
    relay, consumer = _relay(board)
    before = _edges(board)
    assert kb.request_review(board, relay, summary="Review fixture")
    assert kb.get_task(board, relay).status == "review"
    # Direct review claim is the actual dispatch admission primitive, no model launch.
    assert kb.claim_review_task(board, relay, claimer="independent-reviewer") is not None
    assert kb.get_task(board, relay).status == "running"
    assert kb.get_task(board, consumer).status == "todo"
    assert _edges(board) == before


def test_completion_releases_consumer_and_is_not_containment(board):
    relay, consumer = _relay(board)
    before = _edges(board)
    assert kb.complete_task(board, relay, summary="Completed fixture")
    kb.recompute_ready(board)
    assert kb.get_task(board, relay).status == "done"
    assert kb.get_task(board, consumer).status == "ready"
    assert kb.claim_task(board, consumer, claimer="consumer") is not None
    assert _edges(board) == before
