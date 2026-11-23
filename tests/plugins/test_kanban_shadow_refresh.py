"""Parity against the frozen pre-repair shadow reader, plus changing-snapshot boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import threading
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from plugins.kanban.dashboard import board_read, board_rows, board_shadow
from tests.plugins import _kanban_shadow_oracle as oracle
from tests.plugins.test_kanban_board_shadow import shadow_board as _shadow_board, _read, _cards, _column


@pytest.fixture
def shadow_board(tmp_path, monkeypatch):
    yield from _shadow_board.__wrapped__(tmp_path, monkeypatch)


def _write(env, sql, args=()):
    with closing(kbc.connect(env['shadow'])) as conn, conn:
        conn.execute(sql, args)


def _compare(env, monkeypatch, **kw):
    current = board_shadow.merge
    oracle.clear()
    with monkeypatch.context() as patch:
        patch.setattr(board_shadow, 'merge', oracle.merge)
        expected = _read(env, **kw)
    assert board_shadow.merge is current
    actual = _read(env, **kw)
    assert actual == expected
    return actual


@pytest.mark.parametrize('cap', [0, 1, 3])
def test_legacy_blob_fields_follow_task_parsing(shadow_board, monkeypatch, cap):
    env = shadow_board
    monkeypatch.setattr(board_read.time, 'time', lambda: 1800000000)
    _write(env, 'UPDATE tasks SET status=? WHERE id=?', (sqlite3.Binary(b'done'), env['done']))
    _write(env, "INSERT INTO tasks(id,title,status,created_at,created_on) VALUES (?, 'blob id', 'ready',1,'mini')",
           (sqlite3.Binary(b'blob-id'),))
    with closing(kbc.connect()) as live, live:
        live.execute("INSERT INTO tasks(id,title,status,created_at) VALUES (?, 'live bytes', 'ready',1)",
                     (sqlite3.Binary(env['ready'].encode()),))
    _compare(env, monkeypatch, done_limit=cap)


@pytest.mark.parametrize('cap', [0, 1, 3, 80])
@pytest.mark.parametrize('archived', [False, True])
def test_payload_parity_with_legacy_reader(shadow_board, monkeypatch, cap, archived):
    env = shadow_board
    monkeypatch.setattr(board_read.time, 'time', lambda: 1800000000)
    _write(env, "INSERT INTO tasks(id,title,status,created_at,completed_at,created_on,tenant,assignee) "
                "VALUES ('old','old','done',1,'2020-01-01T00:00:00Z','mini','other','someone')")
    _write(env, "INSERT INTO tasks(id,title,status,created_at,created_on) VALUES ('archive','archive','archived',1,'mini')")
    _write(env, "UPDATE tasks SET status='done', completed_at=9999999999 WHERE id=?", (env['live_id'],))
    _compare(env, monkeypatch, done_limit=cap, archived=archived)
    _write(env, "UPDATE tasks SET title='new', body='changed content' WHERE id=?", (env['ready'],))
    _compare(env, monkeypatch, done_limit=cap, archived=archived)
    _write(env, 'DELETE FROM tasks WHERE id=?', (env['done'],))
    _compare(env, monkeypatch, done_limit=cap, archived=archived)


def test_live_collision_changes_without_replica_commit(shadow_board):
    env = shadow_board
    assert env['done'] in _cards(_read(env))
    with closing(kbc.connect()) as live, live:
        live.execute("INSERT INTO tasks(id,title,status,created_at) VALUES (?, 'local archived', 'archived',1)",
                     (env['done'],))
    assert env['done'] not in _cards(_read(env))
    assert _column(_read(env), 'done')['total'] == 0
    with closing(kbc.connect()) as live, live:
        live.execute('DELETE FROM tasks WHERE id=?', (env['done'],))
    assert env['done'] in _cards(_read(env))


def test_all_replica_rows_collide_leaves_no_remote_cards(shadow_board):
    env = shadow_board
    with closing(kbc.connect()) as live, live:
        for status in ('ready', 'running', 'done'):
            live.execute("INSERT INTO tasks(id,title,status,created_at) VALUES (?, 'owned', 'archived',1)",
                         (env[status],))
    assert set(_cards(_read(env))) == {env['live_id']}
    assert _column(_read(env), 'done')['total'] == 0


def test_bookkeeping_and_exact_history_updates_stay_fresh(shadow_board, monkeypatch):
    env = shadow_board
    inputs = []
    original = env['mod']._diagnostics_from_inputs

    def capture(value):
        inputs.append(value)
        return original(value)

    monkeypatch.setattr(env['mod'], '_diagnostics_from_inputs', capture)
    tid = env['running']
    _write(env, "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'test','{}',1)", (tid,))
    _write(env, "INSERT INTO task_runs(task_id,profile,status,started_at) VALUES (?,'default','running',1)", (tid,))
    _read(env)
    first = inputs[-1][1][tid]
    _write(env, 'UPDATE tasks SET last_heartbeat_at=12345, claim_expires=54321 WHERE id=?', (tid,))
    body = _read(env)
    assert _cards(body)[tid]['last_heartbeat_at'] == 12345
    assert _cards(body)[tid]['claim_expires'] == 54321
    assert inputs[-1][1][tid] is first  # bookkeeping does not rematerialize history
    _write(env, "UPDATE task_events SET payload='{\"new\":true}' WHERE task_id=? AND kind='test'", (tid,))
    _read(env)
    assert inputs[-1][1][tid][-1]['payload'] == '{"new":true}'
    _write(env, 'UPDATE task_runs SET last_heartbeat_at=23456 WHERE task_id=?', (tid,))
    _read(env)
    assert inputs[-1][2][tid][-1]['last_heartbeat_at'] == 23456
    _write(env, "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'heartbeat','{}',2)", (tid,))
    _read(env)
    assert inputs[-1][1][tid][-1]['kind'] == 'heartbeat'
    _write(env, "DELETE FROM task_events WHERE task_id=? AND kind='test'", (tid,))
    _read(env)
    assert all(e['kind'] != 'test' for e in inputs[-1][1][tid])
    _write(env, 'DELETE FROM task_runs WHERE task_id=?', (tid,))
    _read(env)
    assert not inputs[-1][2][tid]


@pytest.mark.parametrize('fallback', ['observer', 'cursor'])
def test_untrusted_reader_is_bounded_and_current(shadow_board, monkeypatch, fallback):
    from hermes_cli import kanban_task_change
    env = shadow_board
    if fallback == 'observer':
        monkeypatch.setattr(board_rows, 'version', lambda path: None)
    else:
        monkeypatch.setattr(kanban_task_change, 'changed_since', lambda *a: (None, None))
    assert env['ready'] in _cards(_read(env))
    _write(env, "UPDATE tasks SET title='fresh fallback' WHERE id=?", (env['ready'],))
    assert _cards(_read(env))[env['ready']]['title'] == 'fresh fallback'
    _write(env, 'DELETE FROM tasks WHERE id=?', (env['ready'],))
    assert env['ready'] not in _cards(_read(env))


def test_projection_is_fresh_and_payload_mutation_cannot_poison_cache(shadow_board, monkeypatch):
    env = shadow_board
    state = {'clock': 1800000000}
    monkeypatch.setattr(board_read.time, 'time', lambda: state['clock'])
    body = _read(env)
    original_age = _cards(body)[env['ready']]['age']['created_age_seconds']
    _cards(body)[env['ready']]['title'] = 'poison'
    state['clock'] += 7
    next_body = _read(env)
    assert _cards(next_body)[env['ready']]['title'] == 'Mini ready'
    assert _cards(next_body)[env['ready']]['age']['created_age_seconds'] == original_age + 7
    assert next_body['now'] == state['clock']


@pytest.mark.parametrize('fallback', [False, True])
def test_concurrent_readers_do_not_publish_stale_build(shadow_board, monkeypatch, fallback):
    env = shadow_board
    if fallback:
        monkeypatch.setattr(board_rows, 'version', lambda path: None)
    entered, release = threading.Event(), threading.Event()
    original = board_shadow._selection
    first = True

    def paused(conn, *args):
        nonlocal first
        result = original(conn, *args)  # snapshot established before the concurrent commit
        if first:
            first = False
            entered.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(board_shadow, '_selection', paused)
    with ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(_read, env)
        try:
            assert entered.wait(10)
            _write(env, "UPDATE tasks SET title='concurrent commit' WHERE id=?", (env['ready'],))
            new = pool.submit(_read, env)
            other_query = pool.submit(_read, env, done_limit=1)
        finally:
            release.set()
        assert _cards(old.result(timeout=15))[env['ready']]['title'] == 'Mini ready'
        assert _cards(new.result(timeout=15))[env['ready']]['title'] == 'concurrent commit'
        assert _cards(other_query.result(timeout=15))[env['ready']]['title'] == 'concurrent commit'
    assert _cards(_read(env))[env['ready']]['title'] == 'concurrent commit'


def test_clear_during_build_cannot_republish_old_rows(shadow_board, monkeypatch):
    env = shadow_board
    original, retire = board_shadow._selection, board_rows._retire
    invalidated = threading.Event()
    threads = []

    def retiring(*observers):
        if threading.current_thread().name == 'shadow-clear':
            invalidated.set()  # registry already unpublished; close waits for borrower
        return retire(*observers)

    def selection(conn, *args):
        result = original(conn, *args)
        if not threads:
            thread = threading.Thread(target=board_rows.clear, name='shadow-clear')
            threads.append(thread)
            thread.start()
            assert invalidated.wait(10)
        return result

    monkeypatch.setattr(board_rows, '_retire', retiring)
    monkeypatch.setattr(board_shadow, '_selection', selection)
    try:
        _read(env)
    finally:
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()
    assert board_rows.stats()['entries'] == 0
    _write(env, "UPDATE tasks SET title='after build' WHERE id=?", (env['ready'],))
    assert _cards(_read(env))[env['ready']]['title'] == 'after build'


def test_filtered_queries_retain_exact_legacy_payload(shadow_board, monkeypatch):
    env = shadow_board
    monkeypatch.setattr(board_read.time, 'time', lambda: 1800000000)
    _write(env, "UPDATE tasks SET tenant='one', workflow_template_id='flow', current_step_key='step' WHERE id=?",
           (env['ready'],))
    current = board_shadow.merge
    for tenant, template, step in [('one', None, None), (None, 'flow', 'step'), ('missing', None, None)]:
        args = (tenant, False, None, template, step, 1)
        oracle.clear()
        with monkeypatch.context() as patch:
            patch.setattr(board_shadow, 'merge', oracle.merge)
            expected = env['mod']._get_board_uncached(*args)
        assert board_shadow.merge is current
        assert env['mod']._get_board_uncached(*args) == expected


def test_different_boards_have_independent_replica_cache(shadow_board, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from hermes_cli import kanban_shadow_view
    env = shadow_board
    second = tmp_path / 'second.db'
    with closing(kbc.connect(env['shadow'])) as source, closing(kbc.connect(second)) as target:
        source.backup(target)
    with closing(kbc.connect(second)) as conn, conn:
        conn.execute("UPDATE tasks SET title='other board' WHERE id=?", (env['ready'],))
    original = kanban_shadow_view.open_shadow_replica
    entered, release = threading.Event(), threading.Event()
    select = board_shadow._selection

    @contextmanager
    def replicas(live):
        if threading.current_thread().name.startswith('other-board'):
            with closing(board_rows._open_reader(str(second))) as conn:
                conn.row_factory = sqlite3.Row
                yield conn
        else:
            with original(live) as conn:
                yield conn

    def paused(conn, *args):
        result = select(conn, *args)
        if kanban_shadow_view.db_file(conn) == str(env['shadow']):
            entered.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(kanban_shadow_view, 'open_shadow_replica', replicas)
    monkeypatch.setattr(board_shadow, '_selection', paused)
    with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1, thread_name_prefix='other-board') as other:
        blocked = first.submit(_read, env)
        try:
            assert entered.wait(10)
            independent = other.submit(_read, env)
            assert _cards(independent.result(timeout=10))[env['ready']]['title'] == 'other board'
        finally:
            release.set()
        assert _cards(blocked.result(timeout=15))[env['ready']]['title'] == 'Mini ready'
