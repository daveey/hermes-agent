"""Readiness memo must avoid schema work without hiding repairs."""

import shutil
import sqlite3
from contextlib import closing

import pytest

from gateway import hosted_rooms as rooms
from gateway import hosted_rooms_legacy_import as legacy


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_readiness_exception_closes_open_connection(tmp_path, monkeypatch, error_type):
    db = tmp_path / "store.db"
    rooms._connect(db).close()
    retained = []
    failure = error_type("readiness failed")

    def fail_readiness(conn, path, identity):
        retained.append(conn)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        raise failure

    monkeypatch.setattr(rooms, "_read_store_ready", fail_readiness)
    try:
        with pytest.raises(error_type) as raised:
            rooms._read_connection(db)
        assert raised.value is failure
        assert len(retained) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            retained[0].execute("SELECT 1")
    finally:
        for conn in retained:
            conn.close()


def test_ready_connection_remains_usable_by_caller(tmp_path, monkeypatch):
    db = tmp_path / "store.db"
    rooms._connect(db).close()
    retained = []
    original = rooms._read_store_ready

    def check_readiness(conn, path, identity):
        retained.append(conn)
        return original(conn, path, identity)

    monkeypatch.setattr(rooms, "_read_store_ready", check_readiness)
    with closing(rooms._read_connection(db)) as conn:
        assert retained == [conn]
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def trace_reads(monkeypatch):
    statements = []
    original = rooms.open_sqlite

    def traced(path):
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(rooms, "open_sqlite", traced)
    return statements


def table_probes(statements):
    return [sql for sql in statements if sql.upper().startswith("PRAGMA TABLE_INFO")]


def test_unchanged_second_read_skips_schema_and_import_checks(tmp_path, monkeypatch):
    db = tmp_path / "shared-state.db"
    rooms.list_rooms(db)
    statements = trace_reads(monkeypatch)

    def unexpected(*args):
        raise AssertionError("unchanged store must skip the legacy import check")

    monkeypatch.setattr(legacy, "settled", unexpected)
    assert rooms.list_rooms(db) == []
    assert not table_probes(statements)
    assert sum(sql.upper() == "PRAGMA SCHEMA_VERSION" for sql in statements) == 1


def test_dropped_required_index_rechecks_and_repairs(tmp_path, monkeypatch):
    original = tmp_path / "original.db"
    rooms.list_rooms(original)
    db = tmp_path / "copy.db"
    shutil.copyfile(original, db)
    rooms.list_rooms(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(f"DROP INDEX {rooms._KIND_INDEX}")
        conn.commit()
    statements = trace_reads(monkeypatch)
    assert rooms.list_rooms(db) == []
    assert table_probes(statements)
    with closing(sqlite3.connect(db)) as conn:
        assert rooms._schema_is_current(conn)
    rooms.list_rooms(db)  # memoize the repaired schema
    statements.clear()
    assert rooms.list_rooms(db) == []
    assert not table_probes(statements)


def test_replaced_inode_rechecks_even_with_same_schema_version(tmp_path, monkeypatch):
    db = tmp_path / "store.db"
    rooms.list_rooms(db)
    old_inode = db.stat().st_ino
    with closing(sqlite3.connect(db)) as conn:
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
    replacement = tmp_path / "replacement.db"
    shutil.copyfile(db, replacement)
    with closing(sqlite3.connect(replacement)) as conn:
        conn.execute(f"DROP INDEX {rooms._KIND_INDEX}")
        conn.execute(f"PRAGMA schema_version = {version}")
        conn.commit()
    replacement.replace(db)
    assert db.stat().st_ino != old_inode
    statements = trace_reads(monkeypatch)
    assert rooms.list_rooms(db) == []
    assert table_probes(statements)
    with closing(sqlite3.connect(db)) as conn:
        assert rooms._schema_is_current(conn)


def test_not_ready_is_never_memoized(tmp_path, monkeypatch):
    db = tmp_path / "store.db"
    rooms._connect(db).close()  # initialize without a read memo
    checks = []
    original_open = rooms.open_sqlite

    def not_ready(conn, path):
        checks.append(path)
        return False

    monkeypatch.setattr(rooms, "_store_ready", not_ready)
    # A repair that has not settled yet must be retried by the next read.
    monkeypatch.setattr(rooms, "_connect", original_open)
    assert rooms.list_rooms(db) == []
    assert rooms.list_rooms(db) == []
    assert len(checks) == 2


def test_replacement_during_open_does_not_use_old_memo(tmp_path, monkeypatch):
    db = tmp_path / "store.db"
    rooms.list_rooms(db)
    replacement = tmp_path / "replacement.db"
    shutil.copyfile(db, replacement)
    with closing(sqlite3.connect(replacement)) as conn:
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute(f"DROP INDEX {rooms._KIND_INDEX}")
        conn.execute(f"PRAGMA schema_version = {version}")
        conn.commit()
    original = rooms.open_sqlite

    def replace_then_open(path):
        if replacement.exists():
            replacement.replace(db)
        return original(path)

    monkeypatch.setattr(rooms, "open_sqlite", replace_then_open)
    assert rooms.list_rooms(db) == []
    with closing(sqlite3.connect(db)) as conn:
        assert rooms._schema_is_current(conn)


def test_memo_is_bounded_and_evicted_store_is_rechecked(tmp_path, monkeypatch):
    monkeypatch.setattr(rooms, "_READY_STORES", {})
    monkeypatch.setattr(rooms, "_READY_STORES_LIMIT", 2)
    stores = [tmp_path / f"{i}.db" for i in range(3)]
    for db in stores:
        rooms.list_rooms(db)
    assert len(rooms._READY_STORES) == 2
    statements = trace_reads(monkeypatch)
    rooms.list_rooms(stores[0])
    assert table_probes(statements)


def test_concurrent_reads_share_positive_memo(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "store.db"
    rooms.list_rooms(db)

    def unexpected(*args):
        raise AssertionError("all threads must share the ready memo")

    monkeypatch.setattr(rooms, "_store_ready", unexpected)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(rooms.list_rooms, [db] * 16)) == [[]] * 16
