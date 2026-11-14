"""Real guarded-reader contracts for dispatcher replica reuse (G2b)."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_foreign_card as foreign
from hermes_cli import kanban_lockguard as guard
from hermes_cli import kanban_reader_retention as retention
from hermes_cli import kanban_session_activity as activity


@contextmanager
def borrow(path):
    from hermes_cli.kanban_replica_reader import open_replica
    with open_replica(str(path)) as conn:
        yield conn


@pytest.fixture
def replica(tmp_path, monkeypatch):
    path = tmp_path / 'replica.db'
    writer = guard.connect(str(path))
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('CREATE TABLE sample(value TEXT)')
    writer.execute("INSERT INTO sample VALUES ('before')")
    writer.commit()
    monkeypatch.setattr(foreign, 'replica_path', lambda _: path if path.is_file() else None)
    yield path, writer
    retention.close_all()
    writer.close()


def test_reuses_guarded_open_and_observes_external_commit(replica, monkeypatch):
    path, writer = replica
    calls = []
    original = guard.connect

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(guard, 'connect', counted)
    with borrow(path) as first:
        assert first.execute('SELECT value FROM sample').fetchone()['value'] == 'before'
    writer.execute("UPDATE sample SET value='after'")
    writer.commit()
    with borrow(path) as second:
        assert second.execute('SELECT value FROM sample').fetchone()['value'] == 'after'
        assert first is second
    assert len(calls) == 1


@pytest.mark.parametrize('error', [None, ValueError, sqlite3.OperationalError])
def test_transaction_release_and_error_propagation(replica, error):
    path, writer = replica

    def scope():
        with borrow(path) as conn:
            conn.execute('BEGIN')
            assert conn.execute('SELECT value FROM sample').fetchone()[0] == 'before'
            if error:
                raise error('body sentinel')

    if error:
        with pytest.raises(error, match='body sentinel'):
            scope()
    else:
        scope()
    writer.execute("UPDATE sample SET value='after'")
    writer.commit()
    with borrow(path) as conn:
        assert not conn.in_transaction
        assert conn.execute('SELECT value FROM sample').fetchone()[0] == 'after'


def test_row_readonly_timeout_and_shared_reset(replica):
    path, _ = replica
    with borrow(path) as conn:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE sample SET value='bad'")
    with activity.cached_ro(path) as shared:
        assert shared.row_factory is None
        assert shared.execute('PRAGMA busy_timeout').fetchone()[0] == 500
    with borrow(path) as again:
        assert again is conn
        assert again.row_factory is sqlite3.Row


def test_nested_and_concurrent_borrow_do_not_wait_on_shared_lock(replica):
    path, _ = replica
    with activity.cached_ro(path) as held:
        with borrow(path) as nested:
            assert nested is not held
            assert nested.execute('SELECT value FROM sample').fetchone()[0] == 'before'
        with ThreadPoolExecutor(max_workers=1) as pool:
            def read():
                with borrow(path) as concurrent:
                    return concurrent.execute('SELECT value FROM sample').fetchone()[0]
            assert pool.submit(read).result(timeout=10) == 'before'
        assert held.execute('SELECT value FROM sample').fetchone()[0] == 'before'


def test_bound_and_active_borrower_retirement(replica, monkeypatch):
    path, _ = replica
    monkeypatch.setattr(retention, 'LIMIT', 0)
    with borrow(path) as conn:
        retention.prune(missing=str(path))
        assert conn.execute('SELECT value FROM sample').fetchone()[0] == 'before'
        assert str(path) in activity._RO_CACHE
    assert str(path) not in activity._RO_CACHE
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_path_resolved_each_borrow_and_missing_not_created(replica, monkeypatch, tmp_path):
    path, _ = replica
    with borrow(path) as first:
        assert first is not None
    missing = tmp_path / 'missing.db'
    monkeypatch.setattr(foreign, 'replica_path', lambda _: missing)
    with borrow(path) as absent:
        assert absent is None
    assert not missing.exists()
    with closing(guard.connect(str(missing))) as writer:
        writer.execute('CREATE TABLE other(value)')
        writer.commit()
    with borrow(path) as created:
        assert created is not None
        assert created.execute('SELECT count(*) FROM other').fetchone()[0] == 0


def test_replacement_reopens_without_manual_cache_clear(replica, tmp_path):
    path, writer = replica
    with borrow(path) as old:
        assert old.execute('SELECT value FROM sample').fetchone()[0] == 'before'
    assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    writer.close()
    replacement = tmp_path / 'replacement.db'
    with closing(guard.connect(str(replacement))) as other:
        other.execute('CREATE TABLE sample(value TEXT)')
        other.execute("INSERT INTO sample VALUES ('replacement')")
        other.commit()
    replacement.replace(path)
    with borrow(path) as current:
        assert current is not old
        assert current.execute('SELECT value FROM sample').fetchone()[0] == 'replacement'


def test_deleted_sidecars_recover_before_reuse(replica):
    path, writer = replica
    with borrow(path) as first:
        assert first.execute('SELECT value FROM sample').fetchone()[0] == 'before'
    assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    writer.close()
    Path(str(path) + '-wal').unlink()
    Path(str(path) + '-shm').unlink()
    with borrow(path) as second:
        assert second is not first
        assert second.execute('SELECT value FROM sample').fetchone()[0] == 'before'
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute('SELECT 1')


def test_closed_cached_handle_recovers(replica):
    path, _writer = replica
    with borrow(path) as first:
        first.execute('SELECT value FROM sample').fetchall()
    first.close()  # model a same-thread recovery closing an idle handle
    with borrow(path) as second:
        assert second is not first
        assert second.execute('SELECT value FROM sample').fetchone()[0] == 'before'


def test_evicted_entry_between_prepare_and_lock_is_a_miss(replica, monkeypatch):
    from hermes_cli import kanban_reader_lifecycle as lifecycle
    path, _ = replica
    original = lifecycle.cached_entry

    def evicted(*args, **kwargs):
        key, entry = original(*args, **kwargs)
        retention.prune(missing=key)
        return key, entry

    monkeypatch.setattr(lifecycle, 'cached_entry', evicted)
    with borrow(path) as conn:
        assert conn is None
    monkeypatch.setattr(lifecycle, 'cached_entry', original)
    with borrow(path) as conn:
        assert conn.execute('SELECT value FROM sample').fetchone()[0] == 'before'

