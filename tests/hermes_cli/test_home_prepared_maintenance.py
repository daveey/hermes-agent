"""Prepared source candidates must not abort unrelated native maintenance."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as transfer
from tests.hermes_cli import test_home_transfer_capture as support

board = support.board


def prepared(conn, status="todo"):
    task = kb.create_task(conn, title="source preparation candidate")
    neighbour = kb.create_task(conn, title="independent candidate")
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (neighbour,))
        conn.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (task,))
        conn.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3'")
        conn.execute("DELETE FROM kanban_sync_outbox")
        transfer.prepare(conn, task, source="worker-a3", destination="hub", nonce="maintenance-test-0001", expected_version=7)
    return task, neighbour


def test_real_recompute_skips_prepared_source_and_promotes_independent_neighbour(board):
    task, neighbour = prepared(board)
    before = [tuple(r) for r in board.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task,))]
    assert kb.recompute_ready(board) == 1
    assert kb.get_task(board, task).status == "todo"
    assert kb.get_task(board, neighbour).status == "ready"
    assert [tuple(r) for r in board.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task,))] == before


def test_real_wake_skip_is_write_free_for_prepared_source(board):
    from hermes_cli.kanban_terminal_guard import skip_wake
    task, _ = prepared(board, "scheduled")
    with dbc.write_txn(board):
        before = board.total_changes
        assert skip_wake(board, task, "scheduled") is True
        assert board.total_changes == before


@pytest.mark.parametrize("review", [False, True])
def test_prepared_claim_refuses_before_requesting_any_lease(board, monkeypatch, review):
    from hermes_cli import kanban_db_sync as sync
    task, _ = prepared(board, "review" if review else "ready")
    def no_lease(*args, **kwargs):
        pytest.fail("prepared source attempted a Board lease")
    monkeypatch.setattr(sync, "claim_gate", no_lease)
    claim = kb.claim_review_task if review else kb.claim_task
    assert claim(board, task) is None


def test_malformed_marker_presence_is_not_release_authority(board):
    task, neighbour = prepared(board)
    # The existing SQLite fence remains; malformed non-key custody cannot make
    # maintenance try an effect and abort the independent neighbour's promotion.
    with dbc.write_txn(board):
        board.execute("DROP TRIGGER home_prepared1_marker_update")
        board.execute("UPDATE kanban_home_prepared1 SET nonce='invalid',source='' WHERE task_id=?", (task,))
    assert kb.recompute_ready(board) == 1
    assert kb.get_task(board, task).status == "todo"
    assert kb.get_task(board, neighbour).status == "ready"
