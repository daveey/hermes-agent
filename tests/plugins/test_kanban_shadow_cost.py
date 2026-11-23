"""Shadow history must scale with displayed cards, including under replica churn."""
from contextlib import closing
import json
import time

import pytest

from hermes_cli import kanban_db_connect as kbc
from plugins.kanban.dashboard import board_read
from tests.plugins.test_kanban_board_shadow import shadow_board as _shadow_board, _read, _cards, _column


@pytest.fixture
def shadow_board(tmp_path, monkeypatch):
    yield from _shadow_board.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def large_shadow(shadow_board):
    env = shadow_board
    with closing(kbc.connect(env['shadow'])) as conn, conn:
        conn.execute('BEGIN')
        ids = [f'history_{i:05d}' for i in range(1000)]
        conn.executemany("INSERT INTO tasks (id,title,status,created_at,completed_at,created_on) "
                         "VALUES (?,?,'done',1,?,'mini')", [(tid, tid, i) for i, tid in enumerate(ids)])
        conn.executemany("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'test','{}',1)",
                         [(tid,) for tid in ids for _ in range(20)])
        conn.executemany("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES (?,'default','done',1)",
                         [(tid,) for tid in ids for _ in range(3)])
    return env


@pytest.mark.parametrize('mode', ['p2', 'legacy', 'unobservable'])
def test_changing_replica_history_reads_are_display_bounded(large_shadow, monkeypatch, mode):
    from plugins.kanban.dashboard import board_rows
    env = large_shadow
    if mode == 'legacy':
        with closing(kbc.connect(env['shadow'])) as conn, conn:
            conn.execute('DROP TRIGGER kanban_activity_rev_task_events_update')
    elif mode == 'unobservable':
        monkeypatch.setattr(board_rows, 'version', lambda path: None)
    original = board_read._rows_by_task
    reads, trace, cpu = [], [], []

    def measured(conn, table, ids):
        if str(env['shadow']) == str(conn.execute('PRAGMA database_list').fetchone()[2]):
            conn.set_trace_callback(trace.append)
            reads.append((table, tuple(ids)))
        return original(conn, table, ids)

    monkeypatch.setattr(board_read, '_rows_by_task', measured)
    for i in range(3):
        with closing(kbc.connect(env['shadow'])) as conn, conn:
            conn.execute('UPDATE tasks SET title=? WHERE id=?', (f'changed {i}', env['ready']))
        start = time.thread_time()
        body = _read(env, done_limit=3)
        cpu.append(time.thread_time() - start)
        assert _cards(body)[env['ready']]['title'] == f'changed {i}'
        assert _column(body, 'done')['total'] == 1001
    print(json.dumps({'cpu_seconds': cpu, 'history_queries': len(reads),
                      'history_requested_ids': [len(ids) for _, ids in reads],
                      'sql_statements': len(trace)}))
    # Two open cards + three done, never 1000 hidden historical cards or the collision.
    assert reads and max(len(ids) for _, ids in reads) <= 5
    if mode == 'p2':
        event_reads = [ids for table, ids in reads if table == 'task_events']
        assert len(event_reads) == 1  # task-content edits leave all event histories reusable


def test_live_done_window_bounds_replica_diagnostics(shadow_board, monkeypatch):
    env = shadow_board
    with closing(kbc.connect()) as live, live:
        live.execute("UPDATE tasks SET status='done', completed_at=9999999999 WHERE id=?", (env['live_id'],))
    calls = []
    original = board_read._rows_by_task
    def capture(conn, table, ids):
        if str(conn.execute('PRAGMA database_list').fetchone()[2]) == str(env['shadow']):
            calls.extend(ids)
        return original(conn, table, ids)
    monkeypatch.setattr(board_read, '_rows_by_task', capture)
    body = _read(env, done_limit=1)
    assert [t['id'] for t in _column(body, 'done')['tasks']] == [env['live_id']]
    assert _column(body, 'done')['total'] == 2
    assert env['done'] not in calls


def test_large_history_paired_cpu_and_payload_parity(large_shadow, monkeypatch):
    from plugins.kanban.dashboard import board_shadow
    from tests.plugins import _kanban_shadow_oracle as oracle
    env = large_shadow
    monkeypatch.setattr(board_read.time, 'time', lambda: 1800000000)
    current = board_shadow.merge
    samples = []
    for i in range(4):
        with closing(kbc.connect(env['shadow'])) as conn, conn:
            conn.execute('UPDATE tasks SET last_heartbeat_at=? WHERE id=?', (i, env['ready']))
        pair, payloads = {}, []
        for name in (('old', 'new') if i % 2 == 0 else ('new', 'old')):
            with monkeypatch.context() as patch:
                patch.setattr(board_shadow, 'merge', oracle.merge if name == 'old' else current)
                start = time.thread_time()
                payloads.append(_read(env, done_limit=3))
                pair[name] = time.thread_time() - start
        assert payloads[0] == payloads[1]
        samples.append(pair)
    print(json.dumps({'paired_thread_cpu_seconds': samples, 'payload_parity': True}))
