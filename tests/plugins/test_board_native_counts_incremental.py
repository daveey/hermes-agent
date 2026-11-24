"""Native catalog capture keeps each board's status counts and reads only what moved. [daveey]"""
from contextlib import closing
from types import SimpleNamespace

import pytest

from hermes_cli import fact_board_native as native
from hermes_cli import kanban_db, kanban_db_connect
from plugins.kanban.dashboard import board_fact_views
from tests.plugins import test_kanban_board_shadow as helpers

shadow_board = helpers.shadow_board


@pytest.fixture
def prevs(monkeypatch):
    """Every board_counts._query call's ``prev`` argument; retained state is isolated per test."""
    monkeypatch.setattr(native, '_COUNTS', type(native._COUNTS)())
    seen = []
    return seen


def _record(api, monkeypatch, seen):
    real = api.board_counts._query

    def recording(path, prev=None):
        seen.append(prev)
        return real(path, prev)

    monkeypatch.setattr(api.board_counts, '_query', recording)


def _counts(api):
    selector = board_fact_views.board_selector()
    return native.capture(api, None, 'boards', selector)['value']['counts']


def _add(title='written between captures'):
    with closing(kanban_db_connect.connect(kanban_db.kanban_db_path())) as conn:
        return kanban_db.create_task(conn, title=title, assignee='default')


def test_repeated_capture_passes_the_prior_and_matches_a_full_read(shadow_board, prevs, monkeypatch):
    api = shadow_board['mod']
    _record(api, monkeypatch, prevs)
    statements = []
    real_open = api.board_counts.open_ro

    def traced(path):
        conn = real_open(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(api.board_counts, 'open_ro', traced)
    first = _counts(api)
    assert any(sql == 'SELECT id, status FROM tasks' for sql in statements)
    statements.clear()
    second = _counts(api)
    assert not any(sql == 'SELECT id, status FROM tasks' for sql in statements)
    assert prevs[0] is None and all(p is not None for p in prevs[1:]) and len(prevs) >= 2
    native._COUNTS.clear()
    assert _counts(api) == first == second


def test_a_write_between_captures_updates_the_counts(shadow_board, prevs, monkeypatch):
    api = shadow_board['mod']
    _record(api, monkeypatch, prevs)
    before = _counts(api)
    _add()
    after = _counts(api)
    assert prevs[-1] is not None  # read from retained state
    slug = next(iter(before))
    assert sum(after[slug].values()) == sum(before[slug].values()) + 1
    native._COUNTS.clear()
    assert _counts(api) == after


def test_returned_counts_are_copies(shadow_board, prevs):
    api = shadow_board['mod']
    first = _counts(api)
    slug = next(iter(first))
    first[slug]['ready'] = 999
    first[slug]['bogus'] = 1
    assert _counts(api) == _counts(api)
    assert 'bogus' not in _counts(api)[slug]
    assert _counts(api)[slug].get('ready') != 999


def test_a_failed_query_propagates_and_evicts(shadow_board, prevs, monkeypatch):
    api = shadow_board['mod']
    _counts(api)
    assert native._COUNTS

    def broken(*_a, **_k):
        raise OSError('disk')

    real = api.board_counts._query
    monkeypatch.setattr(api.board_counts, '_query', broken)
    with pytest.raises(OSError):
        _counts(api)
    assert not native._COUNTS
    monkeypatch.setattr(api.board_counts, '_query', real)
    _record(api, monkeypatch, prevs)
    _counts(api)
    assert prevs[0] is None  # the next read after a failure is full


def test_a_replaced_board_file_starts_from_a_full_read(shadow_board, prevs, monkeypatch):
    api = shadow_board['mod']
    _counts(api)
    _record(api, monkeypatch, prevs)
    real = native.os.stat
    board = str(kanban_db.kanban_db_path())

    class Replaced:  # the same path, a new file: only its inode differs
        def __init__(self, stat):
            self._stat = stat

        def __getattr__(self, name):
            value = getattr(self._stat, name)
            return value + 1 if name == 'st_ino' else value

    monkeypatch.setattr(native.os, 'stat',
                        lambda path, *a, **k: Replaced(real(path, *a, **k)) if str(path) == board else real(path, *a, **k))
    _counts(api)
    assert prevs[0] is None


def test_retained_counts_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(native, '_COUNTS', type(native._COUNTS)())
    api = SimpleNamespace(board_counts=SimpleNamespace(_query=lambda path, prev=None: ({'ready': 1}, ('cursor', {'t': 'ready'}))))
    for i in range(native._COUNTS_MAX + 5):
        path = tmp_path / f'{i}.db'
        path.write_bytes(b'')
        native._counts(api, path)
    assert len(native._COUNTS) == native._COUNTS_MAX
    assert str(tmp_path / '0.db') not in native._COUNTS  # oldest evicted


def test_unobservable_boards_retain_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(native, '_COUNTS', type(native._COUNTS)())
    api = SimpleNamespace(board_counts=SimpleNamespace(_query=lambda path, prev=None: ({'ready': 1}, None)))
    path = tmp_path / 'legacy.db'
    path.write_bytes(b'')
    assert native._counts(api, path) == {'ready': 1}
    assert not native._COUNTS
    assert native._counts(api, tmp_path / 'missing.db') == {'ready': 1}
    assert not native._COUNTS


def test_retire_and_runtime_close_drop_retained_counts(shadow_board, prevs):
    from hermes_cli.fact_board_startup import BoardRuntime
    api = shadow_board['mod']
    _counts(api)
    assert native._COUNTS
    runtime = BoardRuntime(api, boards=(kanban_db.get_current_board(),)).start()
    runtime.close()
    assert not native._COUNTS


def test_retire_only_named_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(native, '_COUNTS', type(native._COUNTS)())
    api = SimpleNamespace(board_counts=SimpleNamespace(_query=lambda path, prev=None: ({}, ('c', {}))))
    a, b = tmp_path / 'a.db', tmp_path / 'b.db'
    for p in (a, b):
        p.write_bytes(b'')
        native._counts(api, p)
    native.retire([a])
    assert list(native._COUNTS) == [str(b)]
