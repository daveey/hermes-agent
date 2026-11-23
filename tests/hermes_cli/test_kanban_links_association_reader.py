"""Links dedup reads associations, never full sidebar execution or history."""
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_session_activity_busy as busy
from tests.hermes_cli import test_kanban_session_outcome_tasks as fixtures


def test_dedup_does_not_enrich_while_holding_links_lock(monkeypatch, tmp_path):
    board = tmp_path / 'kanban.db'
    conn = fixtures._connect(board)
    fixtures._insert_task(conn, 'open', 'running', 'tip')
    conn.commit()
    conn.close()
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [board])

    def forbidden(*args, **kwargs):
        pytest.fail('dedup invoked full sidebar enrichment under links lock')

    monkeypatch.setattr(activity, '_read_session_tasks', forbidden)
    row = dict(gateway='gw', profile='default', board='default', id='open',
               key='gw/default/default/open', session='tip', origin_session='tip')
    busy.reset()
    try:
        with activity._links_lock:
            assert activity._without_local_open_duplicates([row], gateway='gw', profile='default') == []
    finally:
        busy.reset()


def test_associations_preserve_creator_subscriber_and_status_selection(tmp_path):
    from hermes_cli.kanban_links_associations import read
    from hermes_cli.kanban_session_outcome_tasks import select_listed_local_tasks

    board = tmp_path / 'kanban.db'
    conn = fixtures._connect(board)
    for tid, status, sid in [('open', 'running', 'tip'), ('done', 'done', 'tip'),
                             ('sub', 'blocked', 'elsewhere'), ('other', 'ready', 'elsewhere')]:
        fixtures._insert_task(conn, tid, status, sid)
    fixtures._sub(conn, 'sub', 'tip', 'created')
    conn.commit()
    conn.row_factory = sqlite3.Row
    expected = select_listed_local_tasks(conn, ['tip'], activity.LISTED_STATUSES)
    conn.close()
    assert set(expected) == {'open', 'sub'}
    assert read(board, ['tip'], activity.LISTED_STATUSES) == list(expected.values())


def test_association_reader_owns_and_closes_connection_even_on_failure(tmp_path, monkeypatch):
    from hermes_cli import kanban_links_associations as associations
    from hermes_cli import kanban_session_outcome_tasks as selection
    from hermes_cli.kanban_reader_lifecycle import cached_entry

    board = tmp_path / 'kanban.db'
    fixtures._connect(board).close()
    readers = []
    real_open = activity._open_ro

    def capture(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        readers.append(conn)
        return conn

    _key, cached = cached_entry(board)
    monkeypatch.setattr(activity, '_open_ro', capture)
    with cached[2]:  # full sidebar borrower cannot delay the lock holder's association read
        assert associations.read(board, ['tip'], activity.LISTED_STATUSES) == []

    def fail(*args):
        raise ValueError('selection failed')

    monkeypatch.setattr(selection, 'select_listed_local_tasks', fail)
    with pytest.raises(ValueError, match='selection failed'):
        associations.read(board, ['tip'], activity.LISTED_STATUSES)
    assert len(readers) == 2
    for conn in readers:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            conn.execute('SELECT 1')


def test_busy_associations_never_poison_sidebar_or_outcome_memos(tmp_path):
    board = tmp_path / 'kanban.db'

    def read(*args):
        return [{'id': 'open'}]

    def unavailable(*args):
        from hermes_cli.kanban_generation import BoardLifecycleBusy
        raise BoardLifecycleBusy()

    busy.reset()
    try:
        assert busy.session_tasks(board, ['tip'], ('running',), read, memo_namespace='links') == [{'id': 'open'}]
        assert busy.session_tasks(board, ['tip'], ('running',), unavailable,
                                  memo_namespace='links') == [{'id': 'open', 'stale': True}]
        for namespace in ('', 'lean'):
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                busy.session_tasks(board, ['tip'], ('running',), unavailable, memo_namespace=namespace)
        busy.reset()
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            busy.session_tasks(board, ['tip'], ('running',), unavailable, memo_namespace='links')
    finally:
        busy.reset()
