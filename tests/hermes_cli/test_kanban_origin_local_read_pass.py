"""Projection-local owner handles never retain a transaction or outlive the call."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin as origin, kanban_origin_local as local, kanban_origin_ledger as ledger
from tests.hermes_cli import test_kanban_local_origin_projection as support

world = support.world
board = support.board


def seed(world):
    path, replica, template = world
    template['origin']['gateway'] = 'owner'
    tasks = []
    for i in range(3):
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title='Build', assignee='default', completion_contract='local-only',
                                 origin_intent={'request_id': f'projection-create-{i}',
                                                'owner': {'gateway': 'owner', 'profile': 'default', 'board': 'default'},
                                                'origin': template['origin']})
            binding = json.loads(conn.execute('SELECT binding_json FROM origin_creation_intents WHERE task_id=?',
                                               (tid,)).fetchone()[0])
        binding['publication_version'] = 3
        support.publish(replica, binding)
        support.accept((path, replica, binding), gateway='owner', request_id=f'projection-accept-{i}')
        tasks.append(tid)
    return tasks


@pytest.mark.parametrize('flag', ['0', '1'])
@pytest.mark.parametrize('change', ['blocked', 'missing_marker'])
def test_projection_shares_opens_but_not_snapshots(world, board, monkeypatch, flag, change):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setenv('HERMES_ORIGIN_BATCH_READS', flag)
    tasks = seed(world)
    opened = []
    original_open = origin._open_ro
    def opener(path):
        conn = original_open(path)
        if path == kb.kanban_db_path('default'):
            opened.append(conn)
        return conn
    monkeypatch.setattr(origin, '_open_ro', opener)
    original = local.observe_local_binding
    visited = []
    def observe(binding, **kw):
        result = original(binding, **kw)
        visited.append(binding['owner']['task_id'])
        if len(visited) == 1:
            # The previous borrow must release its snapshot before this commit.
            with kbc.connect_closing() as conn:
                if change == 'blocked':
                    conn.execute("UPDATE tasks SET status='blocked' WHERE id != ?", (visited[0],))
                else:
                    conn.execute('DELETE FROM origin_creation_intents WHERE task_id != ?', (visited[0],))
        return result
    monkeypatch.setattr(local, 'observe_local_binding', observe)
    rows = ledger.project(world[0], replica=None, gateway='owner', profile='default')
    assert len(rows) == len(tasks) == 3
    by_id = {row['id']: row for row in rows}
    assert by_id[visited[0]]['status'] == 'ready'
    assert {by_id[tid]['status'] for tid in visited[1:]} == ({'blocked'} if change == 'blocked' else {'unknown'})
    assert len(opened) == (1 if flag == '1' else 3)
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
    # The next call cannot reuse the prior call's connection or observations.
    monkeypatch.setattr(local, 'observe_local_binding', original)
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (visited[0],))
    again = ledger.project(world[0], replica=None, gateway='owner', profile='default')
    assert next(row for row in again if row['id'] == visited[0])['status'] == 'running'
    assert len(opened) == (2 if flag == '1' else 6)


def test_projection_closes_shared_handles_after_failure(world, board, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    seed(world)
    opened = []
    original_open = origin._open_ro
    def opener(path):
        conn = original_open(path)
        opened.append(conn)
        return conn
    monkeypatch.setattr(origin, '_open_ro', opener)
    original = local.observe_local_binding
    def fail_after_borrow(binding, **kw):
        original(binding, **kw)
        raise ValueError('projection_failure')
    monkeypatch.setattr(local, 'observe_local_binding', fail_after_borrow)
    with pytest.raises(ValueError, match='projection_failure'):
        ledger.project(world[0], replica=None, gateway='owner', profile='default')
    assert len(opened) == 2
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
