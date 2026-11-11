"""Late failures from a re-filed ask must not poison its replacement probe."""
import pytest

from hermes_cli import kanban_ask_resolved as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as kwc
from hermes_cli.kanban_human_request_migration import classify_parked_request
from tests.hermes_cli import test_kanban_ask_resolved_sync as helpers
from tests.hermes_cli.test_kanban_ask_resolved_sync import _ask, _blocked, _events, _stored_request

board = helpers.board


@pytest.mark.parametrize("rc", [2, 255, None], ids=["exit-2", "ssh-255", "timeout"])
def test_stale_failed_result_after_same_command_refile_leaves_new_generation_untouched(board, rc):
    tid = _blocked(board, "exit 1")
    old = kwc.get_state(board, tid)
    for at in (1000, 2000):
        assert kar.apply_result(board, tid, old["cmd"], 255, "old failure",
                                generation=old["generation"], now=at) is None
    assert classify_parked_request(board, tid, human_request=_ask(old["cmd"], old["every"]),
                                   actor="op", reason="retry same probe")
    fresh = kwc.get_state(board, tid)
    assert fresh["generation"] != old["generation"]
    assert fresh["cmd"] == old["cmd"] and fresh["fails"] == 0
    request = _stored_request(board, tid)
    events = kb.list_events(board, tid)
    comments = kb.list_comments(board, tid)
    assert board.execute("SELECT first_failure_at FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()[0] is None

    # Enough elapsed time to exhaust the OLD streak, but its generation no longer owns the row.
    assert kar.apply_result(board, tid, old["cmd"], rc, "late old failure",
                            generation=old["generation"], now=1000 + kar.BROKEN_MIN_SECONDS) is None
    assert kwc.get_state(board, tid) == fresh
    assert board.execute("SELECT first_failure_at FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()[0] is None
    assert _stored_request(board, tid) == request
    assert kb.list_events(board, tid) == events
    assert kb.list_comments(board, tid) == comments
    assert kb.get_task(board, tid).status == "blocked"
    assert not _events(board, tid, kar.BROKEN_EVENT)

    # The replacement generation still accepts genuine failures and starts its own grace period.
    assert kar.apply_result(board, tid, fresh["cmd"], 255, "new failure",
                            generation=fresh["generation"], now=50_000) is None
    assert kwc.get_state(board, tid)["fails"] == 1
    assert board.execute("SELECT first_failure_at FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()[0] == 50_000
