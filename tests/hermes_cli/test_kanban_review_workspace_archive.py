"""Disk-pressure cleanup of an old review must not deadlock confirmation."""
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_foreign_card as foreign
from tests.hermes_cli import test_kanban_dependency_review_history as history

board = history.board


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_archived_historical_review_and_reaped_park_release_once(board, monkeypatch, tmp_path, remote, legacy):
    task, historical = history.mixed_park(board, monkeypatch, legacy=legacy)
    fresh = next(p for p, _ in kb.parent_states(board, task) if p != historical)
    now = time.time()
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET title = 'Exact-head confirmation review' WHERE id = ?", (fresh,))
        kb._append_event(board, task, "terminal_worker_reaped", {"terminated": True})
        kb._append_event(board, historical, "workspace_archived", {"reason": "disk_pressure"})
    # The owner parked orderly; a later reap is not a new failed run.
    assert board.execute("SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                         (task,)).fetchone()[0] == "blocked"
    replica = None
    if remote:
        board.executescript(sync.SYNC_SCHEMA_SQL)
        board.execute("ALTER TABLE kanban_sync_state ADD COLUMN pulled_at REAL")
        board.execute("INSERT INTO kanban_sync_state (board_id, gateway_id, hlc_last, mode, pulled_at) "
                      "VALUES ('board-test', 'home-test', '', 'primary', ?)", (now,))
        board.commit()
        path = tmp_path / "replica.db"
        replica = sqlite3.connect(path)
        board.backup(replica)
        with kb.write_txn(board):
            for parent in (historical, fresh):
                board.execute("DELETE FROM task_runs WHERE task_id = ?", (parent,))
                board.execute("DELETE FROM task_events WHERE task_id = ?", (parent,))
                board.execute("DELETE FROM tasks WHERE id = ?", (parent,))
                board.execute("INSERT INTO kanban_sync_remote_parents VALUES (?, 'done', ?)", (parent, now))
        monkeypatch.setattr(foreign, "replica_path", lambda _: path)
    monkeypatch.setattr(prs, "pr_state", lambda url: "CLOSED" if url == history.OLD_PR else "OPEN")
    try:
        assert dispatch.check_respawn_guard(board, task) is None
        assert dispatch.check_respawn_guard(board, task) is None  # reads do not consume
        assert kb.claim_task(board, task) is not None
        assert dispatch.check_respawn_guard(board, task) == "active_pr"
        assert kb.reclaim_task(board, task, reason="crash", signal_fn=lambda *_: None)
        assert dispatch.check_respawn_guard(board, task) == "active_pr"
    finally:
        if replica is not None:
            replica.close()
