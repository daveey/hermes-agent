"""Eviction is not a new review obligation; missing history still fails closed."""
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


@pytest.fixture(params=[False, True], ids=["explicit", "legacy"])
def evicted(board, monkeypatch, tmp_path, request):
    task, old = history.mixed_park(board, monkeypatch, legacy=request.param)
    monkeypatch.setattr(prs, "pr_state", lambda url: "MERGED" if url == history.OLD_PR else "OPEN")
    board.executescript(sync.SYNC_SCHEMA_SQL)
    board.execute("ALTER TABLE kanban_sync_state ADD COLUMN pulled_at REAL")
    board.execute("INSERT INTO kanban_sync_state (board_id, gateway_id, hlc_last, mode, pulled_at) "
                  "VALUES ('board-test', 'worker-test', '', 'primary', ?)", (time.time(),))
    path = tmp_path / "replica.db"
    replica = sqlite3.connect(path)
    replica.row_factory = sqlite3.Row
    board.backup(replica)
    # Model the executor's eviction: links persist, local task and its history do not.
    with kb.write_txn(board):
        board.execute("DELETE FROM task_runs WHERE task_id = ?", (old,))
        board.execute("DELETE FROM task_events WHERE task_id = ?", (old,))
        board.execute("DELETE FROM tasks WHERE id = ?", (old,))
        board.execute("INSERT INTO kanban_sync_remote_parents VALUES (?, 'done', ?)", (old, time.time()))
    monkeypatch.setattr(foreign, "replica_path", lambda _: path)
    assert kb.get_task(board, old) is None
    assert kb.unsatisfied_parents(board, task) == []
    yield task, old, replica
    replica.close()


def test_evicted_historical_parent_releases_exactly_one_continuation(board, evicted):
    task, _, _ = evicted
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("fault", ["reopened", "late_event", "late_run", "missing_run", "missing_task",
                                   "stale", "future", "no_stamp", "wrong_board", "sync_off", "missing_replica"])
def test_unverified_replica_history_keeps_hold(board, evicted, monkeypatch, fault):
    task, old, replica = evicted
    statements = {
        "reopened": ("UPDATE tasks SET status = 'ready' WHERE id = ?", (old,)),
        "late_event": ("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'claimed'", (time.time(), old)),
        "late_run": ("UPDATE task_runs SET ended_at = ? WHERE task_id = ?", (time.time(), old)),
        "missing_run": ("DELETE FROM task_runs WHERE task_id = ?", (old,)),
        "missing_task": ("DELETE FROM tasks WHERE id = ?", (old,)),
        "stale": ("UPDATE kanban_sync_state SET pulled_at = ?", (time.time() - 86400,)),
        "future": ("UPDATE kanban_sync_state SET pulled_at = ?", (time.time() + 86400,)),
        "no_stamp": ("UPDATE kanban_sync_state SET pulled_at = NULL", ()),
        "wrong_board": ("UPDATE kanban_sync_state SET board_id = 'other-board'", ()),
    }
    if fault in statements:
        replica.execute(*statements[fault])
    elif fault == "sync_off":
        board.execute("UPDATE kanban_sync_state SET mode = 'off'")
    else:
        monkeypatch.setattr(foreign, "replica_path", lambda _: None)
    replica.commit()
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("status", ["ready", "done"])
def test_local_row_without_history_overrides_old_replica(board, evicted, status):
    task, old, _ = evicted
    board.execute("INSERT INTO tasks (id, title, assignee, status, created_at) "
                  "VALUES (?, 'reopened', 'reviewer', ?, 1)", (old, status))
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("fault", ["before_park", "no_state", "local_no_state", "no_schema", "no_file"])
def test_replica_proof_requires_current_readable_state(board, evicted, fault, monkeypatch, tmp_path):
    task, _, replica = evicted
    statements = {
        "before_park": ("UPDATE kanban_sync_state SET pulled_at = ?", (time.time() - 11,)),
        "no_state": ("DELETE FROM kanban_sync_state", ()),
        "no_schema": ("DROP TABLE task_runs", ()),
    }
    if fault in statements:
        replica.execute(*statements[fault])
    elif fault == "local_no_state":
        board.execute("DELETE FROM kanban_sync_state")
    else:
        monkeypatch.setattr(foreign, "replica_path", lambda _: tmp_path / "absent.db")
    replica.commit()
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
