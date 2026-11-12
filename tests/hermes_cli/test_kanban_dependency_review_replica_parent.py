"""A review parent that ran on another gateway is read from the Board replica.

Shape observed on the hub: the owner card is local and parked on its exact-head review
parent; the review card and its run exist only in the board replica (created on a
worker node). An exact-head approval there must release the same one continuation a
local approval would, and every fail-closed rule must still hold.
"""
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_foreign_card as foreign
from tests.hermes_cli import test_kanban_dependency_review_history as history
from tests.hermes_cli.test_kanban_dependency_review import HEAD, PR, finish_reviews, parked

board = history.board


def _replicate(conn, monkeypatch, tmp_path, task, reviews, *, metadata, before_park=False):
    """Finish ``reviews`` with ``metadata``, then leave them only in a fresh replica."""
    with pytest.MonkeyPatch.context() as clock:
        if before_park:
            ended = time.time() - 30
            clock.setattr(time, "time", lambda: ended)
        finish_reviews(conn, reviews, metadata=metadata)
    conn.executescript(sync.SYNC_SCHEMA_SQL)
    conn.execute("ALTER TABLE kanban_sync_state ADD COLUMN pulled_at REAL")
    conn.execute("INSERT INTO kanban_sync_state (board_id, gateway_id, hlc_last, mode, pulled_at) "
                 "VALUES ('board-test', 'hub-test', '', 'primary', ?)", (time.time(),))
    conn.commit()
    path = tmp_path / "replica.db"
    replica = sqlite3.connect(path)
    conn.backup(replica)
    # The review parents were created on a worker node: the hub file only knows their
    # status through the remote-parent table, never their runs or events.
    with kb.write_txn(conn):
        for review in reviews:
            conn.execute("DELETE FROM task_runs WHERE task_id = ?", (review,))
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (review,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (review,))
            conn.execute("INSERT INTO kanban_sync_remote_parents VALUES (?, 'done', ?)", (review, time.time()))
    monkeypatch.setattr(foreign, "replica_path", lambda _: path)
    for review in reviews:
        assert kb.get_task(conn, review) is None
    assert kb.unsatisfied_parents(conn, task) == []
    return replica


APPROVE = {"verdict": "approve", "reviewed_sha": HEAD, "reviewed_pr": PR}


def test_replica_only_exact_head_approval_releases_one_continuation(board, monkeypatch, tmp_path):
    task, reviews = parked(board)
    replica = _replicate(board, monkeypatch, tmp_path, task, reviews, metadata=APPROVE)
    try:
        assert dispatch.check_respawn_guard(board, task) is None
        assert kb.claim_task(board, task) is not None
        assert dispatch.check_respawn_guard(board, task) == "active_pr"
    finally:
        replica.close()


@pytest.mark.parametrize("case", ["no_head", "short_head", "changes_other_pr", "other_pr", "before_park"])
def test_replica_only_review_keeps_fail_closed_rules(board, monkeypatch, tmp_path, case):
    metadata = {
        "no_head": {"verdict": "approve", "reviewed_pr": PR},
        "short_head": {"verdict": "approve", "head": HEAD[:10], "reviewed_pr": PR},
        "changes_other_pr": {"verdict": "request_changes", "reviewed_sha": HEAD,
                    "reviewed_pr": "https://github.com/example/repo/pull/11"},
        "other_pr": {"verdict": "approve", "reviewed_sha": HEAD,
                     "reviewed_pr": "https://github.com/example/repo/pull/11"},
        "before_park": APPROVE,
    }[case]
    task, reviews = parked(board)
    replica = _replicate(board, monkeypatch, tmp_path, task, reviews, metadata=metadata,
                         before_park=case == "before_park")
    try:
        assert dispatch.check_respawn_guard(board, task) == "active_pr"
    finally:
        replica.close()


def test_replica_change_request_on_captured_pr_hands_back_repair(board, monkeypatch, tmp_path):
    task, reviews = parked(board)
    replica = _replicate(board, monkeypatch, tmp_path, task, reviews,
                         metadata={"verdict": "request_changes", "reviewed_sha": HEAD, "reviewed_pr": PR})
    try:
        # Changes requested is a repair continuation of the same PR, as with a local review.
        assert dispatch.check_respawn_guard(board, task) is None
    finally:
        replica.close()


@pytest.mark.parametrize("fault", ["unclaimed", "not_done", "not_completed", "stale", "wrong_board",
                                   "sync_off", "missing_replica", "local_row"])
def test_unverified_replica_review_keeps_hold(board, monkeypatch, tmp_path, fault):
    task, reviews = parked(board)
    replica = _replicate(board, monkeypatch, tmp_path, task, reviews, metadata=APPROVE)
    try:
        statements = {
            "unclaimed": ("DELETE FROM task_events WHERE kind = 'claimed' AND task_id IN (?, ?)", tuple(reviews)),
            "not_done": ("UPDATE tasks SET status = 'ready' WHERE id IN (?, ?)", tuple(reviews)),
            "not_completed": ("UPDATE task_runs SET outcome = 'blocked' WHERE task_id IN (?, ?)", tuple(reviews)),
            "stale": ("UPDATE kanban_sync_state SET pulled_at = ?", (time.time() - 86400,)),
            "wrong_board": ("UPDATE kanban_sync_state SET board_id = 'other-board'", ()),
        }
        if fault in statements:
            replica.execute(*statements[fault])
            replica.commit()
        elif fault == "sync_off":
            board.execute("UPDATE kanban_sync_state SET mode = 'off'")
            board.commit()
        elif fault == "missing_replica":
            monkeypatch.setattr(foreign, "replica_path", lambda _: None)
        else:
            # A local row (done, but with no run of its own) always decides; no replica fallback.
            for review in reviews:
                board.execute("INSERT INTO tasks (id, title, assignee, status, created_at) "
                              "VALUES (?, 'local', 'reviewer', 'done', 1)", (review,))
            board.commit()
        assert dispatch.check_respawn_guard(board, task) == "active_pr"
    finally:
        replica.close()
