"""Observer lifetime and failure semantics on disposable SQLite stores."""
import sqlite3

import pytest

from hermes_cli.profile_session_scan_cache import ProfileSessionScanCache


@pytest.fixture
def cache():
    cache = ProfileSessionScanCache(max_profiles=1, max_queries=2)
    yield cache
    cache.clear()


def store(home):
    home.mkdir(exist_ok=True)
    conn = sqlite3.connect(home / 'state.db', isolation_level=None)
    conn.execute('CREATE TABLE item(value)')
    conn.execute('INSERT INTO item VALUES (1)')
    return conn


@pytest.mark.parametrize('journal', ['wal', 'delete'])
def test_commits_not_rollbacks_invalidate(cache, tmp_path, journal):
    with store(tmp_path) as writer:
        writer.execute(f'PRAGMA journal_mode={journal}')
        calls = []

        def scan():
            calls.append(True)
            return [writer.execute('SELECT value FROM item').fetchone()[0]]

        assert cache.read(tmp_path, (), scan) == [1]
        writer.execute('BEGIN')
        writer.execute('UPDATE item SET value=2')
        writer.execute('ROLLBACK')
        assert cache.read(tmp_path, (), scan) == [1]
        assert len(calls) == 1
        writer.execute('UPDATE item SET value=3')
        assert cache.read(tmp_path, (), scan) == [3]
        assert len(calls) == 2
    writer.close()


def test_missing_error_recovery_and_replacement(cache, tmp_path):
    calls = []

    def scan():
        calls.append(True)
        path = tmp_path / 'state.db'
        if not path.exists():
            return None
        with sqlite3.connect(path) as conn:
            value = conn.execute('SELECT value FROM item').fetchone()[0]
        conn.close()
        return [value]

    assert cache.read(tmp_path, (), scan) is None
    writer = store(tmp_path)
    writer.close()
    assert cache.read(tmp_path, (), scan) == [1]
    assert cache.read(tmp_path, (), scan) == [1]
    replacement = tmp_path / 'replacement'
    writer = store(replacement)
    writer.execute('UPDATE item SET value=2')
    writer.close()
    (replacement / 'state.db').replace(tmp_path / 'state.db')
    assert cache.read(tmp_path, (), scan) == [2]
    (tmp_path / 'state.db').unlink()
    assert cache.read(tmp_path, (), scan) is None
    assert len(calls) == 4


def test_lru_eviction_closes_observers_and_does_not_reuse_versions(cache, tmp_path):
    one, two = tmp_path / 'one', tmp_path / 'two'
    for home in (one, two):
        store(home).close()
    calls = []

    def scan():
        calls.append(True)
        return [{'value': len(calls)}]

    first = cache.read(one, 'a', scan)
    first[0]['value'] = -1
    assert cache.read(one, 'a', scan) == [{'value': 1}]
    conn = next(iter(cache._profiles.values()))[1]
    cache.read(two, 'a', scan)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    assert cache.read(one, 'a', scan) == [{'value': 3}]
    cache.read(one, 'b', scan)
    cache.read(one, 'c', scan)
    assert cache.read(one, 'a', scan) == [{'value': 6}]
