"""Elapsed-time grace for human-ask probes; transient outages are not permanent."""
import json

from hermes_cli import kanban_ask_resolved as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_ask_resolved_check as helpers

board = helpers.board


def result(conn, tid, at, rc=255):
    state = kwc.get_state(conn, tid)
    return kar.apply_result(conn, tid, state['cmd'], rc, 'connection closed',
                            generation=state['generation'], now=at)


def test_three_failures_within_hour_keep_probe(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, 'exit 255')
        for at in (1000, 1900, 2800):
            assert result(conn, tid, at) is None
        assert kwc.get_state(conn, tid)['fails'] == 3
        assert not helpers._events(conn, tid, kar.BROKEN_EVENT)
        assert conn.execute('SELECT first_failure_at FROM task_wake_checks WHERE task_id=?', (tid,)).fetchone()[0] == 1000


def test_three_failures_spanning_six_hours_drop_once_and_mark(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, 'exit 255')
        state = kwc.get_state(conn, tid)
        assert result(conn, tid, 1000) is None
        assert result(conn, tid, 22599) is None
        assert result(conn, tid, 22600) == kar.BROKEN_EVENT
        assert kwc.get_state(conn, tid) is None
        request = kb.get_task(conn, tid).human_request
        if isinstance(request, str):
            request = json.loads(request)
        assert request['resolved_check_dropped_at'] == 22600
        assert request['resolved_check_drop_reason'] == kar.BROKEN_EVENT
        assert kar.apply_result(conn, tid, state['cmd'], 255, '', generation=state['generation'], now=23000) is None
        assert len(helpers._events(conn, tid, kar.BROKEN_EVENT)) == 1
        assert not kar.reconcile(conn, now=24000)['restored']


def test_not_yet_resets_failure_time_and_counter(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, 'exit 255')
        for at, rc in ((1000,255), (2000,255), (21000,1), (22000,255), (23000,255), (24000,255)):
            assert result(conn, tid, at, rc) is None
        assert kwc.get_state(conn, tid)['fails'] == 3
        assert conn.execute('SELECT first_failure_at FROM task_wake_checks WHERE task_id=?', (tid,)).fetchone()[0] == 22000
        assert not helpers._events(conn, tid, kar.BROKEN_EVENT)


def test_elapsed_time_alone_is_not_broken(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, 'exit 255')
        assert result(conn, tid, 0) is None
        assert result(conn, tid, 21600) is None
        assert kwc.get_state(conn, tid)['fails'] == 2
        assert result(conn, tid, 21601) == kar.BROKEN_EVENT


def test_lost_probe_marks_and_refiling_clears_mark(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, 'exit 1')
        for at in (1000, 2000):
            with kb.write_txn(conn):
                conn.execute('DELETE FROM task_wake_checks WHERE task_id=?', (tid,))
            kar.reconcile(conn, now=at)
        request = kb.get_task(conn, tid).human_request
        if isinstance(request, str):
            request = json.loads(request)
        assert request['resolved_check_dropped_at'] == 2000
        assert request['resolved_check_drop_reason'] == kar.LOST_EVENT
        assert kar.reconcile(conn, now=3000)['lost'] == []
        with kb.write_txn(conn):
            kar.store_request(conn, tid, request)
        request = kb.get_task(conn, tid).human_request
        if isinstance(request, str):
            request = json.loads(request)
        assert 'resolved_check_dropped_at' not in request
        assert kwc.get_state(conn, tid)['fails'] == 0


def test_legacy_failure_count_starts_new_grace(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, 'exit 255')
        with kb.write_txn(conn):
            conn.execute('UPDATE task_wake_checks SET fails=5, last_at=1 WHERE task_id=?', (tid,))
        assert result(conn, tid, 100000) is None
        assert kwc.get_state(conn, tid)['fails'] == 6
