"""Pass-wide storage cancellation must not become per-item maintenance failures."""
from contextlib import closing
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dispatch_storage as storage
from hermes_cli import kanban_hold_notices as notices
from hermes_cli import kanban_parent_held as parents
from hermes_cli import kanban_suspend_ledger as ledger
from hermes_cli.kanban_db_dispatch import DispatchResult
from tests.hermes_cli import test_kanban_hold_failure_isolation as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('operation', ['stamp', 'holds', 'parents'])
def test_maintenance_stops_on_real_writer_contention(kanban_home, caplog, operation):
    with kbc.connect_closing() as conn:
        first = helpers._held(conn, 'runs')
        second = helpers._held(conn, 'runs')
        parent = None
        if operation == 'parents':
            parent = helpers._held(conn, 'runs')
            kb.link_tasks(conn, parent, first)
            kb.link_tasks(conn, parent, second)
        before = [kb.list_events(conn, tid) for tid in (first, second)]
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        after = []

        def tick():
            if operation == 'stamp':
                ledger.record_dispatch_pass(conn, now=1000)
            elif operation == 'parents':
                parents.sweep(conn)
            else:
                notices.sweep(conn)
            after.append('continued after exhausted storage budget')
            return DispatchResult()

        with closing(sqlite3.connect(path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                result = storage.run(conn, tick, board='default')
                assert result.storage_skip_reason == storage.REASON
                assert after == []
                assert [kb.list_events(conn, tid) for tid in (first, second)] == before
                assert not conn.in_transaction
                assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == timeout
                warnings = [r for r in caplog.records if r.levelname == 'WARNING']
                assert len(warnings) == 1
                assert 'reason=storage_write_contended' in warnings[0].getMessage()
                assert warnings[0].exc_info is None
            finally:
                blocker.rollback()
        caplog.clear()
        recovered = storage.run(conn, tick, board='default')
        assert recovered.storage_skip_reason is None
        assert after == ['continued after exhausted storage budget']
        if operation == 'stamp':
            assert conn.execute('SELECT at FROM kanban_dispatch_stamp').fetchone()[0] == 1000
        elif operation == 'parents':
            for tid in (first, second):
                assert len([e for e in kb.list_events(conn, tid) if e.kind == 'parent_held']) == 1
                assert kb.parent_states(conn, tid) == [(parent, 'triage')]
        else:
            assert len(helpers._decisions(conn, first)) == len(helpers._decisions(conn, second)) == 1
        assert not caplog.records
