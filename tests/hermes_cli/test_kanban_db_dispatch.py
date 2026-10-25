"""Healthy worker progress supersedes stale dispatch failure diagnostics."""
import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture(params=["No Anthropic credentials", "authentication failed: No Anthropic credentials"])
def running(tmp_path, request):
    conn = kbc.connect(tmp_path / "board.db")
    tid = kb.create_task(conn, title="Recovered worker", assignee="default")
    assert kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_failure_error = ?, consecutive_failures = 1 WHERE id = ?",
                     (request.param, tid))
    yield conn, tid, run_id
    conn.close()


@pytest.mark.parametrize("action", ["checkpoint", "scheduled_checkpoint", "heartbeat", "completion"])
def test_successful_progress_clears_stale_failure(running, action):
    conn, tid, run_id = running
    error = kb.get_task(conn, tid).last_failure_error
    if "authentication" in error:
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"
    if action in ("checkpoint", "scheduled_checkpoint"):
        result = kc.continue_task(conn, tid, checkpoint="Normal progress saved",
                                  resume_after_seconds=60 if action == "scheduled_checkpoint" else 0,
                                  expected_run_id=run_id)
        assert result["ok"]
    elif action == "heartbeat":
        assert kbd.heartbeat_worker(conn, tid, expected_run_id=run_id)
    else:
        assert kb.complete_task(conn, tid, summary="Finished", expected_run_id=run_id,
                                fire_lifecycle_hook=False)
    assert kb.get_task(conn, tid).last_failure_error is None
    assert kbd.check_respawn_guard(conn, tid) != "blocker_auth"
    if action != "completion":
        assert conn.execute("SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == 1


@pytest.mark.parametrize("action", ["checkpoint", "heartbeat", "completion"])
def test_stale_worker_cannot_clear_failure(running, action):
    conn, tid, run_id = running
    error = kb.get_task(conn, tid).last_failure_error
    if action == "checkpoint":
        assert not kc.continue_task(conn, tid, checkpoint="Old worker", expected_run_id=run_id + 1)["ok"]
    elif action == "heartbeat":
        assert not kbd.heartbeat_worker(conn, tid, expected_run_id=run_id + 1)
    else:
        assert not kb.complete_task(conn, tid, summary="Old worker done", expected_run_id=run_id + 1, fire_lifecycle_hook=False)
    assert kb.get_task(conn, tid).last_failure_error == error
