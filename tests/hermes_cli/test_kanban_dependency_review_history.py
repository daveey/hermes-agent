"""Mixed historical dependencies must not require repeated reviews of old PRs."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli.kanban_guard_helpers import worker_comment
from tests.hermes_cli.test_kanban_dependency_review import HEAD, PR, finish_reviews, parked

OLD_PR = "https://github.com/example/repo/pull/9"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def mixed_park(conn, monkeypatch, *, legacy=False):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    historical = kb.create_task(conn, title="old design ruling", assignee="reviewer")
    finish_reviews(conn, [historical], metadata={"verdict": "approved", "head": HEAD, "pr": OLD_PR})
    task = kb.create_task(conn, title="sequential implementation", assignee="dev")
    kb.claim_task(conn, task)
    run = kb.get_task(conn, task).current_run_id
    worker_comment(conn, task, "dev", OLD_PR + " and " + PR)
    fresh = kb.create_task(conn, title="new review", assignee="reviewer")
    for parent in [historical, fresh]:
        kb.link_tasks(conn, parent, task, expected_child_run_id=run)
    monkeypatch.setattr(time, "time", lambda: now + 10)
    assert kb.block_task(conn, task, kind="dependency", reason="review", expected_run_id=run)
    if legacy:
        row = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'", (task,)).fetchone()
        payload = json.loads(row["payload"])
        payload["review_checkpoint"].pop("awaited", None)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    monkeypatch.setattr(time, "time", lambda: now + 20)
    finish_reviews(conn, [fresh], metadata={"verdict": "APPROVE", "head": HEAD, "pr": 10})
    return task, historical


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("old_state", ["MERGED", "CLOSED", "REJECTED"])
def test_historical_parent_and_discharged_pr_allow_one_continuation(board, monkeypatch, legacy, old_state):
    task, _ = mixed_park(board, monkeypatch, legacy=legacy)
    monkeypatch.setattr(prs, "pr_state", lambda url: old_state if url == OLD_PR else "OPEN")
    monkeypatch.setitem(prs._rejections, OLD_PR, "size judge: SPLIT")
    assert dispatch.check_respawn_guard(board, task) is None
    if old_state == "REJECTED":
        notes = [r["body"] for r in board.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND author = 'respawn-guard'", (task,),
        )]
        assert len(notes) == 1
        assert "size judge: SPLIT" in notes[0]
        assert "superseding scope ruling" in notes[0]
        assert "do not blindly resubmit" in notes[0]
        assert dispatch.check_respawn_guard(board, task) is None
        assert board.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id = ? AND author = 'respawn-guard'", (task,),
        ).fetchone()[0] == 1
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="crash", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("state", [None, "OPEN"])
def test_integer_review_does_not_cover_other_open_or_unknown_pr(board, monkeypatch, state):
    task, _ = mixed_park(board, monkeypatch)
    monkeypatch.setattr(prs, "pr_state", lambda url: state if url == OLD_PR else "OPEN")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("named", [10, "#10", "example/repo#10", PR])
def test_present_pr_reference_resolves_only_its_captured_url(board, named):
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={"verdict": "APPROVE", "head": HEAD, "pr": named})
    assert dispatch.check_respawn_guard(board, task) is None


@pytest.mark.parametrize("named", [None, True, 99, "#99", "other/repo#10", "", {}, []])
@pytest.mark.parametrize("verdict", ["APPROVE", "changes_required"])
def test_present_invalid_reference_is_not_missing_reference(board, named, verdict):
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={"verdict": verdict, "reviewed_sha": HEAD, "pr": named})
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("legacy", [False, True])
def test_historical_parent_lifecycle_activity_invalidates_exemption(board, monkeypatch, legacy):
    task, historical = mixed_park(board, monkeypatch, legacy=legacy)
    with kb.write_txn(board):
        kb._synthesize_ended_run(board, historical, outcome="completed", summary="Superseding ruling", metadata={})
    monkeypatch.setattr(prs, "pr_state", lambda url: "MERGED" if url == OLD_PR else "OPEN")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("event", ["archived", "workspace_archived", "worker_result", "wake_episode_evidence"])
def test_historical_parent_bookkeeping_does_not_rearm_hold(board, monkeypatch, legacy, event):
    task, historical = mixed_park(board, monkeypatch, legacy=legacy)
    with kb.write_txn(board):
        if event == "archived":
            board.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (historical,))
        kb._append_event(board, historical, event, {})
    monkeypatch.setattr(prs, "pr_state", lambda url: "MERGED" if url == OLD_PR else "OPEN")
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="crash", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("event", ["unblocked", "claimed", "completed", "unknown_housekeeping"])
def test_archive_does_not_hide_lifecycle_or_unknown_events(board, monkeypatch, legacy, event):
    task, historical = mixed_park(board, monkeypatch, legacy=legacy)
    with kb.write_txn(board):
        kb._append_event(board, historical, "workspace_archived", {"reason": "disk_pressure"})
        kb._append_event(board, historical, event, {})
    monkeypatch.setattr(prs, "pr_state", lambda url: "CLOSED" if url == OLD_PR else "OPEN")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_comment_on_finished_historical_parent_does_not_rearm_hold(board, monkeypatch):
    # A verifier's comment on a long-done parent appends `commented` and, from outside the
    # wake corridor, a `wake_episode_evidence` snapshot. Neither reopens the parent. The
    # snapshot is appended when this DB has not carried the parent's exit reset itself
    # (it completed on another gateway and arrived by sync): drop the local exit row.
    task, historical = mixed_park(board, monkeypatch)
    with kb.write_txn(board):
        board.execute("DELETE FROM task_wake_episode_exit WHERE task_id = ?", (historical,))
    kb.add_comment(board, historical, "worker-1/t_verifier", "independent verification")
    kinds = {r[0] for r in board.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND created_at >= ?", (historical, int(time.time())))}
    assert "wake_episode_evidence" in kinds
    monkeypatch.setattr(prs, "pr_state", lambda url: "MERGED" if url == OLD_PR else "OPEN")
    assert dispatch.check_respawn_guard(board, task) is None


def test_ambiguous_numeric_reference_does_not_cover_two_repositories(board):
    from hermes_cli.kanban_dependency_review import _named_prs
    assert _named_prs({"pr": 10}, {PR, "https://github.com/other/repo/pull/10"}) == set()


def test_synced_nonlocal_parent_can_be_checkpointed(board):
    # A nonlocal parent is one the board sync resolved from another partition. A parent with no
    # state anywhere (a dangling edge) is refused by block_task; test_kanban_missing_parent_refusal
    # covers that path.
    task = kb.create_task(board, title="implementation", assignee="dev")
    kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    worker_comment(board, task, "dev", PR)
    board.executescript(sync.SYNC_SCHEMA_SQL)
    with kb.write_txn(board):
        board.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", ("t_remote_missing", task))
        board.execute("INSERT INTO kanban_sync_remote_parents (parent_id, status, refreshed_at) "
                      "VALUES ('t_remote_missing', 'running', 1)")
    assert kb.get_task(board, "t_remote_missing") is None
    assert kb.parent_states(board, task) == [("t_remote_missing", "running")]
    assert kb.block_task(board, task, kind="dependency", reason="remote review", expected_run_id=run)
    row = board.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'", (task,)).fetchone()
    assert json.loads(row["payload"])["review_checkpoint"]["awaited"] == ["t_remote_missing"]
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("awaited", [None, {}, "parent", [42], ["unknown"], []])
def test_malformed_or_empty_awaited_checkpoint_cannot_authorize(board, awaited):
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={"verdict": "APPROVE", "head": HEAD})
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'", (task,)).fetchone()
    payload = json.loads(row["payload"])
    payload["review_checkpoint"]["awaited"] = awaited
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    assert dispatch.check_respawn_guard(board, task) == "active_pr"

