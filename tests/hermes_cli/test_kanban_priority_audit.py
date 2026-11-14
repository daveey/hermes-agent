"""Creation and absolute edits preserve priority history for queue replay."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path):
    db = kbc.connect(tmp_path / 'audit.db')
    yield db
    db.close()


def payload(conn, tid, kind):
    row = conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                       (tid, kind)).fetchone()
    return json.loads(row[0])


@pytest.mark.parametrize('requested,stored', [(0, 0), (37, 37), (60000, 1000)])
def test_created_event_records_actual_stored_priority(conn, requested, stored):
    tid = kb.create_task(conn, title='audit creation', priority=requested)
    assert payload(conn, tid, 'created')['priority'] == stored


@pytest.mark.parametrize('priority,key,expected', [(37, None, 37), (0, 'pickup-stall:fixture', 900)])
def test_decomposed_child_inherits_effective_priority_and_audits(conn, priority, key, expected):
    from hermes_cli.kanban_db_graph import _insert_decomposed_child

    root = kb.create_task(conn, title='root', priority=priority, idempotency_key=key)
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (root,)).fetchone()
    child = _insert_decomposed_child(conn, root, row, {'title': 'child'}, 'test', 1800000000)
    assert conn.execute('SELECT priority FROM tasks WHERE id=?', (child,)).fetchone()[0] == expected
    assert payload(conn, child, 'created')['priority'] == expected


@pytest.mark.parametrize('new', [0, 1000])
def test_absolute_edit_records_previous_priority(conn, new):
    tid = kb.create_task(conn, title='audit edit', priority=37)
    assert kb.edit_task(conn, tid, priority=new)
    event = payload(conn, tid, 'reprioritized')
    assert event['priority'] == new
    assert event['previous_priority'] == 37
