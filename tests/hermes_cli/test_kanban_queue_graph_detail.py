"""Real task-detail wire parity and bounded graph transfer on large-body boards."""
from contextlib import contextmanager
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_label_backlog as backlog
from hermes_cli import kanban_queue_read_rows as transfer
from tests.hermes_cli import test_kanban_label_backlog_pool_ahead as helpers
from tests.hermes_cli.test_kanban_queue_schema_reads import CountConnection

hub = helpers.hub


@pytest.mark.parametrize('status', ['ready', 'running', 'done'])
def test_real_detail_wire_and_backlog_match_literal_inventory(hub, monkeypatch, status):
    from plugins.kanban.dashboard import plugin_api as api
    conn = hub.conn
    now = int(time.time())
    monkeypatch.setattr(time, 'time', lambda: now)
    tid = helpers._card(conn, 'target', now - 1900, helpers.LABELS)
    predecessor = helpers._card(conn, 'predecessor', now - 2000, helpers.LABELS)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=?,body=? WHERE id=?', (status, 'target body\n' * 8192, tid))
        conn.executemany("INSERT INTO tasks(id,title,body,status,priority,created_at,assignee) VALUES (?,'history',?,'done',0,1,'default')",
                         [(f'history-{i}', 'x' * 8192) for i in range(1024)])
        kb._append_event(conn, tid, 'commented', {'preserved': 'history' * 8192})
    from hermes_cli import kanban_db_connect as kbc
    import sqlite3
    path = kbc._main_db_file(conn)
    reads = []

    @contextmanager
    def board_conn(_board):
        reader = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        reader.row_factory = sqlite3.Row
        counted = CountConnection(reader)
        reads.append(counted)
        try:
            yield 'default', counted
        finally:
            reader.close()

    monkeypatch.setattr(api, '_board_conn', board_conn)
    app = FastAPI()
    app.include_router(api.router, prefix='/api/plugins/kanban')
    with TestClient(app) as client:
        response = client.get(f'/api/plugins/kanban/tasks/{tid}')
        assert response.status_code == 200, response.text
        actual = response.json()
        transferred = sum(reader.rows for reader in reads)
        assert transferred < 600, transferred
        assert actual['task']['body'] == 'target body\n' * 8192
        assert any(e['payload'] == {'preserved': 'history' * 8192} for e in actual['events'])
        before = backlog.read(conn, tid, helpers.LABELS, machines=helpers.WORKERS, machine_labels=helpers.CARRIED)
        with monkeypatch.context() as literal:
            literal.setattr(transfer, 'column_names', lambda c, table: {r[1] for r in c.execute(f'PRAGMA table_info({table})')})
            expected = client.get(f'/api/plugins/kanban/tasks/{tid}')
            assert expected.status_code == 200, expected.text
            assert actual == expected.json()
            if status == 'ready':
                assert sum(reader.rows for reader in reads) - transferred > transferred
            assert before == backlog.read(conn, tid, helpers.LABELS, machines=helpers.WORKERS, machine_labels=helpers.CARRIED)
        # No retained result: fresh priority/status changes affect the next real queue read.
        if status == 'ready':
            assert before['ahead'] == 1 and actual['queue']['lane'] == 'machine'
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (predecessor,))
            assert backlog.read(conn, tid, helpers.LABELS)['ahead'] == 0
