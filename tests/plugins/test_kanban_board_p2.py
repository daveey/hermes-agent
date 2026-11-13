"""Exact endpoint reads: P2 invalidation, volatile overlay and legacy FULL fallback."""
from contextlib import closing
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_task_change as changes
from plugins.kanban.dashboard import board_counts, board_rows
from tests.plugins import test_kanban_board_read_cache as helpers

board = helpers.board


@pytest.fixture(autouse=True)
def reset():
    board_rows.clear()
    board_counts.clear()
    yield
    board_rows.clear()
    board_counts.clear()


def get(client):
    response = client.get('/api/plugins/kanban/board?done_limit=2')
    assert response.status_code == 200
    body = response.json()
    return body, {t['id']: t for col in body['columns'] for t in col['tasks']}


def seed():
    with closing(kbc.connect()) as conn:
        parent = kb.create_task(conn, title='parent', assignee='w')
        child = kb.create_task(conn, title='child', assignee='w')
        return parent, child


@pytest.mark.parametrize('mutation', ['heartbeat', 'child', 'link', 'done'])
def test_next_get_is_exact_without_a_whole_board_read(board, mutation):
    _mod, client, _reads = board
    parent, child = seed()
    with closing(kbc.connect()) as conn:
        if mutation == 'child':
            kb.link_tasks(conn, parent, child)
        if mutation == 'done':
            for i in range(4):
                tid = kb.create_task(conn, title=f'done-{i}', assignee='w')
                conn.execute("UPDATE tasks SET status='done',completed_at=? WHERE id=?", (100+i, tid))
            conn.commit()
    get(client)
    with mock.patch.object(kb, 'list_tasks', wraps=kb.list_tasks) as full:
        with closing(kbc.connect()) as conn:
            if mutation == 'heartbeat':
                conn.execute('UPDATE tasks SET last_heartbeat_at=123,claim_expires=456,worker_pid=789 WHERE id=?', (child,))
            elif mutation == 'link':
                kb.link_tasks(conn, parent, child)
            else:
                conn.execute("UPDATE tasks SET status='done',completed_at=1000 WHERE id=?", (child,))
            conn.commit()
        body, cards = get(client)
        assert not full.called
    if mutation == 'heartbeat':
        assert (cards[child]['last_heartbeat_at'], cards[child]['claim_expires'], cards[child]['worker_pid']) == (123, 456, 789)
    elif mutation == 'child':
        assert cards[parent]['progress'] == {'done': 1, 'total': 1}
    elif mutation == 'link':
        assert cards[parent]['link_counts']['children'] == cards[child]['link_counts']['parents'] == 1
    else:
        done = next(c for c in body['columns'] if c['name'] == 'done')
        assert (done['total'], done['omitted']) == (5, 3)
        assert [t['title'] for t in done['tasks']] == ['child', 'done-3']


def test_untrusted_cursor_falls_back_and_rearms(board):
    _mod, client, _reads = board
    _, child = seed()
    get(client)
    with closing(kbc.connect()) as conn:
        conn.execute('DROP TRIGGER kanban_activity_rev_tasks_update')
        conn.execute("UPDATE tasks SET title='unmarked' WHERE id=?", (child,))
        conn.commit()
    with mock.patch.object(kb, 'list_tasks', wraps=kb.list_tasks) as full:
        assert get(client)[1][child]['title'] == 'unmarked'
        assert full.call_count == 1


def test_cursor_and_rows_share_a_snapshot(board, monkeypatch):
    _mod, client, _reads = board
    _, child = seed()
    get(client)
    real = changes.changed_since
    committed = False

    def racing(conn, cursor, **kwargs):
        nonlocal committed
        result = real(conn, cursor, **kwargs)
        assert conn.in_transaction
        if not committed:
            committed = True
            with closing(kbc.connect()) as writer:
                writer.execute("UPDATE tasks SET title='raced' WHERE id=?", (child,))
                writer.commit()
        return result

    monkeypatch.setattr(changes, 'changed_since', racing)
    assert get(client)[1][child]['title'] == 'child'
    assert get(client)[1][child]['title'] == 'raced'


def test_switcher_ignores_heartbeat_but_not_status(board):
    mod, _client, _reads = board
    _, child = seed()
    mod.list_boards(include_archived=False)
    with mock.patch.object(board_counts, '_query', wraps=board_counts._query) as query:
        with closing(kbc.connect()) as conn:
            conn.execute('UPDATE tasks SET last_heartbeat_at=321 WHERE id=?', (child,))
            conn.commit()
        mod.list_boards(include_archived=False)
        assert not query.called
        with closing(kbc.connect()) as conn:
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child,))
            conn.commit()
        result = mod.list_boards(include_archived=False)
        assert query.call_count == 1
        assert result['boards'][0]['counts']['blocked'] == 1
