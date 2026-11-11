"""Legacy boards gain failure timing without inventing outage age."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ask_resolved as kar
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_ask_resolved_check as helpers

board = helpers.board


def test_legacy_column_migration_preserves_armed_probe(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, 'exit 255')
        with kb.write_txn(conn):
            conn.execute('ALTER TABLE task_wake_checks DROP COLUMN first_failure_at')
            conn.execute('UPDATE task_wake_checks SET fails=2 WHERE task_id=?', (tid,))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        row = conn.execute('SELECT first_failure_at, fails FROM task_wake_checks WHERE task_id=?', (tid,)).fetchone()
        assert tuple(row) == (None, 2)
        state = kwc.get_state(conn, tid)
        assert kar.apply_result(conn, tid, state['cmd'], 255, '', generation=state['generation'], now=100000) is None
        assert kwc.get_state(conn, tid)['fails'] == 3


def test_satisfied_clears_streak_and_refile_starts_fresh(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, 'exit 255')
        state = kwc.get_state(conn, tid)
        assert kar.apply_result(conn, tid, state['cmd'], 255, '', generation=state['generation'], now=1000) is None
        assert kar.apply_result(conn, tid, state['cmd'], 0, '', generation=state['generation'], now=2000) == kar.EVENT
        assert kwc.get_state(conn, tid) is None
        assert kb.get_task(conn, tid).status == 'ready'
