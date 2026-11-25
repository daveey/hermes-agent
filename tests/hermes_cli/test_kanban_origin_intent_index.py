"""Owner-local marker lookup must not scan unrelated courier history."""
import sqlite3
import json

import pytest

from hermes_cli import kanban_origin_intent_carry as carry


def board():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY)')
    conn.execute(carry.INTENTS_DDL)
    conn.executemany('INSERT INTO origin_creation_intents VALUES(?,?,?,?,?)',
                     [(f'r{i}', '{}', f't{i}', f'{{"i":{i}}}', f'c{i}') for i in range(4000)])
    # Preserve table-scan order for several markers on the same task.
    conn.executemany('INSERT INTO origin_creation_intents VALUES(?,?,?,?,?)',
                     [(f'z{i}', '{}', 'target', str(i), f'zc{i}') for i in (7, 2, 9)])
    return conn


def steps(conn, sql, params=()):
    count = 0
    def tick():
        nonlocal count
        count += 1
        return 0
    conn.set_progress_handler(tick, 1)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.set_progress_handler(None, 0)
    return rows, count


def test_marker_lookup_bounded_and_literal_order():
    with board() as conn:
        sql = 'SELECT binding_json FROM origin_creation_intents WHERE task_id=?'
        expected, old_work = steps(conn, sql, ('target',))
        carry.ensure_task_index(conn)
        for analyze in (False, True):
            if analyze:
                conn.execute('ANALYZE')
            actual, work = steps(conn, sql, ('target',))
            assert actual == expected == [('7',), ('2',), ('9',)]
            assert old_work > 10000
            assert work < 100
            assert steps(conn, sql, ('absent',))[1] < 100


def test_settled_install_is_schema_noop_and_absence_stays_absent():
    with sqlite3.connect(':memory:') as conn:
        carry.ensure_task_index(conn)
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='origin_creation_intents'").fetchall()
    with board() as conn:
        carry.ensure_task_index(conn)
        before = conn.execute('PRAGMA schema_version').fetchone()
        carry.ensure_task_index(conn)
        assert conn.execute('PRAGMA schema_version').fetchone() == before


def test_real_board_reopen_installs_index(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    path = tmp_path / 'kanban.db'
    with kbc.connect_closing(path) as conn:
        conn.execute(carry.INTENTS_DDL)
    kbc.init_db(path)
    with kbc.connect_closing(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_origin_intents_task'").fetchone()


from tests.hermes_cli import test_kanban_sync_restore_origin_intents as restored_support
kanban_home = restored_support.kanban_home
svc = restored_support.svc
live = restored_support.live


@pytest.mark.parametrize('how', ['create', 'adopt'])
def test_lazy_creation_paths_install_index(live, how):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_creation import adopt_with_origin
    intent = restored_support._intent()
    if how == 'create':
        kb.create_task(live, title='courier', assignee='worker', origin_intent=intent)
    else:
        tid = kb.create_task(live, title='existing', assignee='worker')
        adopt_with_origin(live, {**intent, 'owner': {**intent['owner'], 'task_id': tid}},
                          board=restored_support.BOARD)
    assert live.execute("SELECT 1 FROM sqlite_master WHERE name='idx_origin_intents_task'").fetchone()


def test_restoring_missing_table_installs_index():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY)')
        conn.execute('CREATE TABLE kanban_restored_cards(task_id TEXT)')
        conn.execute("INSERT INTO kanban_restored_cards VALUES('card')")
        binding = {'binding_id': 'binding-test', 'version': 1,
                   'owner': {'gateway': 'owner', 'profile': 'default', 'board': 'default', 'task_id': 'card'},
                   'origin': {'gateway': 'origin', 'profile': 'default', 'session': 'session'},
                   'task_record_key': 'default/default/card'}
        payload = json.dumps({'v': 1, 'request_id': 'request-test', 'intent_json': '{}',
                              'binding_json': json.dumps(binding), 'child_request_id': 'child-test'})
        assert carry.restore_from_event(conn, {'kind': carry.EVENT_KIND, 'task_id': 'card',
                                               'payload': payload}, 'owner')
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_origin_intents_task'").fetchone()

