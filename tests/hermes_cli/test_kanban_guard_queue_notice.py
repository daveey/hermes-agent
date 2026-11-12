"""Queue and chat notices describe guard observations, not nonexistent slot waits."""
import time

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_queue_position as qp
from tests.hermes_cli import test_kanban_dependency_pr_handoff as helpers

board = helpers.board


@pytest.mark.parametrize("cleared_by", ["unblocked", "hold_released", "continued", "promoted"])
@pytest.mark.parametrize("reason", ["active_pr", "blocker_auth", "recent_success"])
def test_guarded_queue_and_notice(board, monkeypatch, reason, cleared_by):
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(board, title="waiting owner", assignee="dev", session_id="chat")
    clock[0] += 1801
    with kb.write_txn(board):
        kb._append_event(board, task, "respawn_guarded", {"reason": reason})
    place = qp.position(board, kb.get_task(board, task))
    assert place == {"lane": "respawn_guard", "reason": reason}
    assert cw.sweep(board, now=clock[0]) == 1
    payload = kb._json_dict(board.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (task, cw.NOTICE_KIND),
    ).fetchone()[0])
    assert payload["respawn_guard"] == reason
    for text in (qp.describe(place), cw.notice_text(payload), pu.notice_text(payload),
                 pu.ready_hint(board, kb.get_task(board, task))):
        assert reason in text
        assert "local slot" not in text
        assert "bump" not in text
        assert "last recorded" in text
    assert qp.position(board, kb.get_task(board, task)) == place  # notice doesn't erase hold
    with kb.write_txn(board):
        kb._append_event(board, task, cleared_by, {})
    assert qp.position(board, kb.get_task(board, task))["lane"] != "respawn_guard"
