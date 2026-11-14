"""Exercise updater receipt recognition through actual PR continuation consumers."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from tests.hermes_cli import test_kanban_core_update_requeue_handoff as helpers

board = helpers.board


@pytest.mark.parametrize("setup", [helpers._approved, helpers._ordering])
def test_matching_term_keeps_handoff_and_real_successor_fault_consumes_it(board, setup):
    task = setup(board)
    helpers._reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    run = helpers._latest_run(board, task)
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND run_id = ? "
                        "AND kind = 'reclaimed'", (task, run["id"])).fetchone()
    payload = json.loads(row["payload"])
    payload.update(prev_pid=42, prev_lock="worker:99", host_local=True,
                   termination_attempted=True, terminated=True, sigkill=False)
    board.execute("UPDATE task_events SET payload = ?, created_at = ? WHERE id = ?",
                  (json.dumps(payload), run["ended_at"], row["id"]))
    kb._append_event(board, task, "crashed", dict(pid=42, claimer="worker:99",
                     exit_kind="signaled", exit_code=15), run_id=run["id"])
    board.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND run_id = ? "
                  "AND kind = 'crashed'", (run["ended_at"], task, run["id"]))
    assert dispatch.check_respawn_guard(board, task) is None
    helpers._reclaim(board, task, helpers.CRASH)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
