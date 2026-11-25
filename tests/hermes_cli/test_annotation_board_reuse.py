"""Content wakes must not invalidate every independently unchanged board."""
import sqlite3
import time
from collections import Counter

import pytest

from hermes_cli import kanban_activity_revision as legacy
from hermes_cli import kanban_annotation_revision as revision
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_parts as part_helpers

world = part_helpers.world
SCOPE = ('board-reuse-test', 'default')


@pytest.fixture
def boards(tmp_path, monkeypatch):
    paths = [tmp_path / name / 'kanban.db' for name in ('a', 'b')]
    writers = []
    for path in paths:
        path.parent.mkdir()
        conn = sqlite3.connect(path, isolation_level=None)
        conn.executescript('''
            CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT, session_id TEXT);
            CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT);
            CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT, last_heartbeat_at INTEGER);
            INSERT INTO tasks VALUES ('t1','running','s1');
            INSERT INTO task_runs VALUES (1,'t1',1);
        ''')
        legacy.ensure_revision(conn, legacy.LOCAL_TABLES)
        revision.ensure_revisions(conn)
        writers.append(conn)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: SCOPE)
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: paths)
    monkeypatch.setattr(ksa, '_board_memo', {})
    monkeypatch.setattr(ksa._revalidate.force_read, 'scope', SCOPE, raising=False)
    monkeypatch.setattr(ksa._revalidate.force_read, 'parts', {'counts'}, raising=False)
    monkeypatch.setattr(ksa._revalidate.force_read, 'content_only_boards', True, raising=False)
    calls = Counter()

    def read(path, *, parked, stalled):
        calls[path] += 1
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as conn:
            rows = conn.execute('SELECT session_id FROM tasks WHERE status="running"').fetchall()
            heartbeat = conn.execute('SELECT last_heartbeat_at FROM task_runs').fetchone()[0]
        return {'active': dict(Counter(row[0] for row in rows)), 'blocked': {}, 'unseen': {},
                'pairs': set(), 'source_read_at': time.time(), 'heartbeat': heartbeat}, True

    monkeypatch.setattr(ksa, '_read_board', read)
    yield paths, writers, calls
    for conn in writers:
        conn.close()
    ksa._reset_cache_for_tests()


def _read():
    active = {}
    ksa._read_local_boards(active, {}, {})
    return active


def test_content_wake_reads_changed_board_only(boards):
    paths, writers, calls = boards
    assert _read() == {'s1': 2}
    assert calls == dict.fromkeys(paths, 1)
    old_legacy = ksa._board_content_key(paths[0])
    writers[0].execute('UPDATE task_runs SET last_heartbeat_at=2')
    assert ksa._board_content_key(paths[0]) == old_legacy, 'exercise the legacy-invisible liveness write'
    assert _read() == {'s1': 2}
    assert ksa._board_memo[str(paths[0])][1]['heartbeat'] == 2
    assert calls == {paths[0]: 2, paths[1]: 1}, 'unchanged board was fully read on its neighbour heartbeat'


def test_event_content_refresh_enables_board_reuse_only_during_read(world, monkeypatch):
    part_helpers._refresh()
    world.conn.execute("UPDATE tasks SET status='blocked'")
    seen = []
    original = ksa._refresh_annotation_locked

    def refresh(scope):
        seen.append(getattr(ksa._revalidate.force_read, 'content_only_boards', None))
        return original(scope)

    monkeypatch.setattr(ksa, '_refresh_annotation_locked', refresh)
    part_helpers._refresh()
    assert seen == [True]
    assert not getattr(ksa._revalidate.force_read, 'content_only_boards', False)


@pytest.mark.parametrize('reason', ['renew', 'clock', 'loss'])
def test_event_noncontent_refresh_keeps_full_board_reads(world, monkeypatch, reason):
    from hermes_cli import kanban_annotation_host as host

    part_helpers._refresh()
    if reason == 'renew':
        monkeypatch.setattr(ksa._count_scopes, 'read_at_locked', lambda scope: 0)
    elif reason == 'clock':
        host._clock_deadlines[part_helpers.SCOPE] = [time.time() - 1]
    seen = []
    original = ksa._refresh_annotation_locked

    def refresh(scope):
        seen.append(getattr(ksa._revalidate.force_read, 'content_only_boards', None))
        return original(scope)

    monkeypatch.setattr(ksa, '_refresh_annotation_locked', refresh)
    world.conn.execute("UPDATE tasks SET status='blocked'")
    from types import SimpleNamespace
    host._event_refresh(SimpleNamespace(replace=lambda *a, **kw: None, control_only=False,
                                        coverage_lost=reason == 'loss'))
    assert seen == [False]
    assert not getattr(ksa._revalidate.force_read, 'content_only_boards', False)


def test_forced_renewal_reads_every_board(boards, monkeypatch):
    paths, _writers, calls = boards
    _read()
    monkeypatch.setattr(ksa._revalidate.force_read, 'content_only_boards', False)
    _read()
    assert calls == dict.fromkeys(paths, 2)


def test_content_reuse_preserves_actual_source_time_and_age_bound(boards):
    paths, _writers, calls = boards
    _read()
    before = {str(p): ksa._board_memo[str(p)][1]['source_read_at'] for p in paths}
    _read()
    assert calls == dict.fromkeys(paths, 1)
    assert {str(p): ksa._board_memo[str(p)][1]['source_read_at'] for p in paths} == before
    for path in paths:
        ksa._board_memo[str(path)][1]['source_read_at'] = time.time() - ksa._COUNTS_MAX_AGE_S
    _read()
    assert calls == dict.fromkeys(paths, 2)


def test_unknown_counts_coverage_never_reuses_content_board(boards, monkeypatch):
    paths, writers, calls = boards
    _read()
    writers[0].execute(f'DROP TRIGGER {revision.COUNTS.prefix}task_runs_update')
    monkeypatch.setattr(ksa, '_board_content_key', lambda path: ('legacy-fixed',))
    _read()
    _read()
    assert calls[paths[0]] == 3
    assert calls[paths[1]] == 2, 'certified neighbour should survive the second unknown-board read'
