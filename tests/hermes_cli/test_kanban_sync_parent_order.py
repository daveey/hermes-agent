"""Dependency selection must not mint competing envelopes or starve other cards."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_sync_parent_order import parent_first
from hermes_cli.kanban_sync_quarantine import pending_rows
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    db = kbc.connect(home / 'kanban.db')
    client.ensure_identity(db, 'default', 'sender')
    yield db
    db.close()


def test_backoff_prefix_does_not_hide_independent_card(conn):
    now = int(client.time.time())
    tid = kb.create_task(conn, title='backoff', assignee='worker')
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_outbox SET retry_after=? WHERE table_name='tasks'", (now + 600,))
    for i in range(client.PUSH_BATCH + 1):
        kb.add_comment(conn, tid, 'author', str(i))
    other = kb.create_task(conn, title='unrelated', assignee='worker')
    selected = parent_first(conn, pending_rows(conn, now), now, client.PUSH_BATCH)
    assert selected and all(r['row_uid'] == other or json.loads(r['fields']).get('task_id') == other for r in selected)
    assert conn.execute("SELECT MAX(attempts) FROM kanban_sync_outbox").fetchone()[0] == 0


def test_before_head_filter_cannot_reintroduce_missing_parent(conn):
    tid = kb.create_task(conn, title='parent', assignee='worker')
    kb.add_comment(conn, tid, 'author', 'child')
    now = int(client.time.time())
    selected = parent_first(conn, pending_rows(conn, now), now, client.PUSH_BATCH)
    filtered = [r for r in selected if r['table_name'] != 'tasks']
    assert filtered
    assert parent_first(conn, filtered, now, client.PUSH_BATCH,
                        allowed={r['op_id'] for r in filtered}) == []


def test_promoting_materialized_parent_keeps_frozen_bytes(conn):
    tid = kb.create_task(conn, title='parent', assignee='worker')
    kb.add_comment(conn, tid, 'author', 'child')
    with kbc.write_txn(conn):
        parent = conn.execute("SELECT op_id FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()[0]
        wire = json.dumps({'client_op_id': parent, 'hlc': '0000000000001-0000-sender', 'frozen_replay': 1})
        conn.execute("UPDATE kanban_sync_outbox SET created_at=created_at+9999,wire_json=? WHERE op_id=?", (wire, parent))
    now = int(client.time.time())
    selected = parent_first(conn, pending_rows(conn, now), now, client.PUSH_BATCH)
    assert selected[0]['op_id'] == parent and selected[0]['wire_json'] == wire
    assert len({r['op_id'] for r in selected}) == len(selected)
    assert conn.execute("SELECT wire_json FROM kanban_sync_outbox WHERE op_id=?", (parent,)).fetchone()[0] == wire
