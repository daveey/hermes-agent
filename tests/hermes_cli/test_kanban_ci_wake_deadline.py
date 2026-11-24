"""A CI awaiting label without a probe still has a timer deadline.

This guards the timed/gated path from the observed late-wake payload. It does not
reproduce historical dispatcher starvation or claim an external CI subscription.
"""
from hermes_cli import kanban_continuation as continuation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_sweep_gate as helpers

home = helpers.home


def test_ci_timer_wakes_without_owner_comment_at_deadline(home):
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title='CI deadline', assignee='worker')
        other = kb.create_task(conn, title='unrelated', assignee='worker')
        kb.claim_task(conn, task)
        continuation.continue_task(
            conn, task, checkpoint='CI pending', resume_after_seconds=600,
            awaiting={'kind': 'ci', 'ref': 'https://github.com/o/r/actions/runs/45181400536',
                      'why': 'pending checks'})
        park = continuation.pending_wake(conn, task)
        assert park is not None
        wake_at = park['wake_at']
        for now in (wake_at - 500, wake_at - 100, wake_at - 1):
            assert continuation.wake_due_continuations(conn, now=now) == []
        # A board-content revision before the deadline must retain the timer.
        kb.add_comment(conn, other, 'test', 'unrelated revision')
        assert continuation.wake_due_continuations(conn, now=wake_at - 1) == []
        assert continuation.wake_due_continuations(conn, now=wake_at + 1) == [task]
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'ready'
        assert continuation.wake_due_continuations(conn, now=wake_at + 2) == []
