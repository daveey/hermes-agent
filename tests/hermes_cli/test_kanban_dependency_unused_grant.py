"""Unused Board grants must not spend an owner's dependency continuation."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_dependency_pr_handoff as helpers

board = helpers.board


def release_unused(conn, task, clock, *, evidence=None, unused=True):
    clock[0] += 1
    kb.claim_task(conn, task)
    run = kb.get_task(conn, task).current_run_id
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET status = 'released', outcome = 'released', ended_at = ?, "
                     "claim_lock = NULL, metadata = ? WHERE id = ?",
                     (clock[0], '{"unused_grant":true}', run))
        conn.execute("UPDATE task_events SET payload = ? WHERE task_id = ? AND run_id = ? AND kind = 'claimed'",
                     ('{"holder":"hub"}', task, run))
        conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
                     "current_run_id = NULL WHERE id = ?", (task,))
        kb._append_event(conn, task, "released", {"unused_grant": unused, "retry_status": "ready"}, run_id=run)
        if evidence:
            kb._append_event(conn, task, evidence, {}, run_id=run)
    return run


def test_unused_grants_preserve_dependency_dispatch(board, monkeypatch, all_assignees_spawnable):
    task, _, clock = helpers.park(board, monkeypatch)
    release_unused(board, task, clock)
    release_unused(board, task, clock)
    assert prs.active_pr_reason(board, task, int(time.time())) is None
    launched = []
    result = dispatch.dispatch_once(board, spawn_fn=lambda task, workspace, **kwargs: launched.append(task.id) or 123456789)
    assert task in launched, result
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("evidence", ["worker_bound", "worker_result", "spawned", "crashed", "reclaimed", "timed_out",
                                      "owner_admitted", "heartbeat", "unknown_future_execution"])
def test_used_or_failed_grants_remain_guarded(board, monkeypatch, evidence):
    task, _, clock = helpers.park(board, monkeypatch)
    release_unused(board, task, clock, evidence=evidence)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("field,value", [
    ("status", "running"), ("outcome", "crashed"), ("ended_at", None),
    ("worker_pid", 123), ("worker_started_at", "birth"), ("last_heartbeat_at", 123),
    ("claim_lock", "hub:123"), ("summary", "worked"), ("error", "failed"),
    ("metadata", '{}'), ("metadata", '{"unused_grant":1}'),
    ("metadata", '{"unused_grant":true,"worker_result":{}}'),
])
def test_contradictory_run_fields_consume_handoff(board, monkeypatch, field, value):
    task, _, clock = helpers.park(board, monkeypatch)
    run = release_unused(board, task, clock)
    with kb.write_txn(board):
        board.execute(f"UPDATE task_runs SET {field} = ? WHERE id = ?", (value, run))
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_local_claim_or_missing_release_consumes_handoff(board, monkeypatch):
    task, _, clock = helpers.park(board, monkeypatch)
    run = release_unused(board, task, clock)
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE run_id = ? AND kind = 'claimed'",
                      ('{"lock":"local"}', run))
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"
    with kb.write_txn(board):
        board.execute("DELETE FROM task_events WHERE run_id = ?", (run,))
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_release_without_unused_receipt_remains_guarded(board, monkeypatch):
    task, _, clock = helpers.park(board, monkeypatch)
    release_unused(board, task, clock, unused=False)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"
