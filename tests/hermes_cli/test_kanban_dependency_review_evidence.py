"""Adversarial persisted-evidence tests for dependency-review continuation."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.test_kanban_dependency_review import parked, finish_reviews


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


@pytest.mark.parametrize("bad", [None, 4, [{}], "https://github.com/example/repo/pull/10"])
def test_malformed_pr_checkpoint_fails_closed(board, bad):
    task, reviews = parked(board)
    finish_reviews(board, reviews)
    row = board.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'",
        (task,),
    ).fetchone()
    payload = json.loads(row["payload"])
    payload["review_checkpoint"]["prs"] = bad
    # Corrupt only this fixture's temp database, never the operator's board.
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_newer_unclaimed_completion_cannot_fall_back_to_old_review(board):
    task, reviews = parked(board)
    finish_reviews(board, reviews)
    assert dispatch.check_respawn_guard(board, task) is None
    for review in reviews:
        with kb.write_txn(board):
            kb._synthesize_ended_run(board, review, outcome="completed", summary="Superseding approval",
                                     metadata={"verdict": "approved"})
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_changed_dependency_graph_cannot_reuse_checkpoint(board):
    task, reviews = parked(board)
    finish_reviews(board, reviews)
    assert kb.unlink_tasks(board, reviews[0], task)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
