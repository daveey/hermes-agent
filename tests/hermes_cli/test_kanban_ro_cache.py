"""The shared read-only connection cache behind the linked-card reads: one connection per file reused across calls
and threads, fresh commits visible, reopened when the file is replaced or a read fails, never blocking a writer."""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest

from hermes_cli import kanban_session_activity as ksa


@pytest.fixture(autouse=True)
def _clean_cache():
    ksa._reset_cache_for_tests()
    yield
    ksa._reset_cache_for_tests()


def _db(path, value):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.execute("DELETE FROM t")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    return conn


def _read(path):
    with ksa.cached_ro(path) as conn:
        return conn, conn.execute("SELECT v FROM t").fetchone()[0]


def test_reuses_one_connection_and_sees_new_commits(tmp_path):
    path = tmp_path / "b.db"
    writer = _db(path, "one")
    first, value = _read(path)
    assert value == "one"
    writer.execute("UPDATE t SET v = 'two'")
    writer.commit()
    again, value = _read(path)
    assert again is first and value == "two"
    writer.close()


def test_reopens_when_the_file_is_replaced(tmp_path):
    path, fresh = tmp_path / "b.db", tmp_path / "new.db"
    _db(path, "old").close()
    first, _ = _read(path)
    _db(fresh, "rebuilt").close()
    os.replace(fresh, path)
    second, value = _read(path)
    assert second is not first and value == "rebuilt"


def test_a_failed_read_drops_the_connection(tmp_path):
    path = tmp_path / "b.db"
    _db(path, "x").close()
    first, _ = _read(path)
    with pytest.raises(sqlite3.OperationalError):
        with ksa.cached_ro(path) as conn:
            conn.execute("SELECT nope FROM missing")
    second, value = _read(path)
    assert second is not first and value == "x"


def test_missing_file_raises_and_is_never_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        with ksa.cached_ro(path):
            pass
    assert not path.exists()


def test_idle_cached_reader_does_not_block_writers_or_checkpoints(tmp_path):
    path = tmp_path / "b.db"
    writer = _db(path, "a")
    _read(path)
    for i in range(50):
        writer.execute("INSERT INTO t VALUES (?)", (str(i),))
        writer.commit()
    busy, _log, _ckpt = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert busy == 0
    assert os.path.getsize(f"{path}-wal") == 0
    writer.close()


def test_threads_share_the_connection_safely(tmp_path):
    path = tmp_path / "b.db"
    _db(path, "v").close()
    seen, errors = set(), []

    def worker():
        try:
            for _ in range(50):
                conn, value = _read(path)
                assert value == "v"
                seen.add(id(conn))
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(seen) == 1
