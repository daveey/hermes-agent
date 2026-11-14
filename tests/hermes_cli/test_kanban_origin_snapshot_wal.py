"""The origin-work snapshot is shared by every process on a host: a write must never lock out a reader.

kanban-ec72354e6b (hub, 2026-11-14): ``origin-work.db`` stayed in the rollback journal, so while any process
committed its multi-MB snapshot the remote-links rebuild's cold read failed at ``PRAGMA journal_mode`` with
``database is locked`` and the whole rebuild was dropped ("background rebuild failed; serving the previous
projection"); the converse (a reader's SHARED lock) failed the writers' saves ("origin-work snapshot not saved"). [daveey]
"""
import sqlite3

import pytest

import hermes_state_wal
from hermes_cli import kanban_db, profiles
from hermes_cli import kanban_session_activity as activity


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(kanban_db, 'kanban_home', lambda: tmp_path)
    activity._reset_cache_for_tests()
    yield tmp_path
    activity._reset_cache_for_tests()


def card(n=0):
    return {'session': f's{n}', 'id': f't{n}', 'key': f'owner/work/default/t{n}',
            'status': 'running', 'gateway': 'owner', 'profile': 'work'}


def journal_mode(path):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        return db.execute('PRAGMA journal_mode').fetchone()[0]


def test_cold_read_succeeds_while_another_process_commits(home):
    activity._remote_snapshot([card()])
    writer = sqlite3.connect(home / 'origin-work.db', isolation_level=None)
    try:
        # A foreign process mid-commit of its own snapshot (EXCLUSIVE in a rollback journal).
        writer.execute('BEGIN EXCLUSIVE')
        writer.execute('INSERT OR REPLACE INTO scoped_snapshot VALUES (?, ?)', ('other', '[]'))
        assert activity._remote_snapshot() == [card()]
    finally:
        writer.execute('ROLLBACK')
        writer.close()


def test_save_succeeds_while_another_process_reads(home):
    activity._remote_snapshot([card()])
    reader = sqlite3.connect(home / 'origin-work.db', isolation_level=None)
    try:
        reader.execute('BEGIN')
        assert reader.execute('SELECT count(*) FROM scoped_snapshot').fetchone()[0] == 1
        assert activity._remote_snapshot([card(1)]) == [card(1)]
    finally:
        reader.execute('COMMIT')
        reader.close()
    assert activity._remote_snapshot() == [card(1)]


def test_existing_rollback_journal_snapshot_is_upgraded_on_the_next_save(home):
    path = home / 'origin-work.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE scoped_snapshot (scope TEXT PRIMARY KEY, data TEXT NOT NULL)')
    db.close()
    assert journal_mode(path) == 'delete'
    activity._remote_snapshot([card()])
    assert journal_mode(path) == 'wal'
    assert activity._remote_snapshot() == [card()]


def guard_spy(monkeypatch):
    from hermes_cli import kanban_lockguard
    seen = []
    real = kanban_lockguard.guard

    def spy(conn, **kw):
        seen.append((conn.execute('PRAGMA journal_mode').fetchone()[0],
                     conn.execute('PRAGMA journal_size_limit').fetchone()[0], kw))
        return real(conn, **kw)
    monkeypatch.setattr(kanban_lockguard, 'guard', spy)
    return seen


def test_save_guards_the_wal_connection_with_a_bounded_wal(home, monkeypatch):
    seen = guard_spy(monkeypatch)
    activity._remote_snapshot([card()])
    assert ('wal', 8388608, {}) in seen


def test_wal_reset_vulnerable_sqlite_keeps_the_rollback_journal(home, monkeypatch):
    monkeypatch.setattr(hermes_state_wal, 'is_sqlite_wal_reset_vulnerable', lambda: True)
    seen = guard_spy(monkeypatch)
    activity._remote_snapshot([card()])
    assert journal_mode(home / 'origin-work.db') == 'delete'
    assert not [s for s in seen if s[2] == {}]  # the post-upgrade guard runs only on WAL
    assert activity._remote_snapshot() == [card()]
