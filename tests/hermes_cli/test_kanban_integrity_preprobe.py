"""Per-connect full probes stay mandatory; repair remains under the init turn."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import threading
import os
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_db_lock_safety import _board


def observe_lock(monkeypatch, before=None):
    held = []
    original = kbc._cross_process_init_lock

    @contextmanager
    def locked(path):
        if before:
            before(path)
        with original(path):
            held.append(True)
            try:
                yield
            finally:
                held.pop()
    monkeypatch.setattr(kbc, "_cross_process_init_lock", locked)
    return held


def test_full_check_outside_init_turn_then_schema_inside(tmp_path, monkeypatch):
    db = _board(tmp_path)
    held = observe_lock(monkeypatch)
    calls = []
    real = kbc._probe_integrity
    schema = kbc.apply_schema_steps

    def probe(path):
        calls.append(bool(held))
        return real(path)

    def initialize(conn):
        assert held
        return schema(conn)
    monkeypatch.setattr(kbc, "_probe_integrity", probe)
    monkeypatch.setattr(kbc, "apply_schema_steps", initialize)
    with kbc.connect_closing(db):
        pass
    assert calls == [False]
    with kbc.connect_closing(db):
        pass
    assert calls == [False], "existing process fast path remains unchanged"
    kbc._INITIALIZED_PATHS.discard(str(db.resolve()))
    with kbc.connect_closing(db):
        pass
    assert calls == [False, False], "new process must never trust a durable health stamp"


def test_cold_threads_share_one_full_probe(tmp_path, monkeypatch):
    db = _board(tmp_path)
    barrier = threading.Barrier(4)
    calls = []
    real = kbc._probe_integrity

    def probe(path):
        calls.append(path)
        return real(path)

    def opened(_):
        barrier.wait(timeout=10)
        with kbc.connect_closing(db) as conn:
            return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    monkeypatch.setattr(kbc, "_probe_integrity", probe)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(opened, range(4))) == [8] * 4
    assert calls == [db.resolve()]


@pytest.mark.parametrize("replacement", ["corrupt", "empty", "healthy"])
def test_replaced_file_after_preprobe_is_checked_under_lock(tmp_path, monkeypatch, replacement):
    db = _board(tmp_path)
    other = tmp_path / "other.db"
    if replacement == "corrupt":
        other.write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)
    elif replacement == "empty":
        other.touch()
    else:
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE preserved(value)")
            conn.execute("INSERT INTO preserved VALUES (42)")
    held = observe_lock(monkeypatch, before=lambda path: os.replace(other, db))
    calls = []
    guard = kbc._guard_existing_db_is_healthy

    def guarded(path):
        assert held
        calls.append(path)
        return guard(path)
    monkeypatch.setattr(kbc, "_guard_existing_db_is_healthy", guarded)
    if replacement == "corrupt":
        with pytest.raises(kbc.KanbanDbCorruptError) as caught:
            kbc.connect(db)
        assert caught.value.backup_path.exists()
    else:
        with kbc.connect_closing(db) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            if replacement == "healthy":
                assert conn.execute("SELECT value FROM preserved").fetchone()[0] == 42
    assert calls == [db]


def test_corruption_only_confirmed_and_backed_up_under_lock(tmp_path, monkeypatch):
    db = tmp_path / "bad.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)
    held = observe_lock(monkeypatch)
    real_probe = kbc._probe_integrity
    confirm = kbc._confirm_corruption
    backup = kbc._backup_corrupt_db
    calls = []

    def probe(path):
        calls.append(bool(held))
        return real_probe(path)

    def confirmed(*args):
        assert held
        return confirm(*args)

    def backed_up(*args):
        assert held
        return backup(*args)
    monkeypatch.setattr(kbc, "_probe_integrity", probe)
    monkeypatch.setattr(kbc, "_confirm_corruption", confirmed)
    monkeypatch.setattr(kbc, "_backup_corrupt_db", backed_up)
    with pytest.raises(kbc.KanbanDbCorruptError):
        kbc.connect(db)
    assert calls == [False, True]
