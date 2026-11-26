"""Throughput refreshes keep parsed schemas, never a SQLite read transaction. [daveey]"""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import machine_throughput_metrics as mt
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def clear():
    mt.reset_cache()
    yield
    mt.reset_cache()


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    store = SessionDB(db_path=path)
    store.close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO sessions(id,source,started_at) VALUES ('fixture','test',1)")
    db.execute("INSERT INTO session_model_usage(session_id,model,input_tokens,output_tokens,"
               "cache_read_tokens,cache_write_tokens) VALUES ('fixture','m',1,2,3,4)")
    db.commit()
    return db


def observe(monkeypatch):
    original = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(mt.sqlite3, "connect", connect)
    return opened


def test_refresh_keeps_connection_but_sees_commit_and_releases_snapshot(tmp_path, monkeypatch):
    writer = database(tmp_path / "state.db")
    opened = observe(monkeypatch)
    try:
        first = mt.cached_counters(tmp_path, clock=lambda: 0)
        writer.execute("UPDATE session_model_usage SET input_tokens=9")
        writer.commit()
        second = mt.cached_counters(tmp_path, clock=lambda: 60)
        assert second["tokens_io_total"] == first["tokens_io_total"] + 8
        assert len(opened) == 1  # exact forced-fresh-open mutant anchor
        assert not opened[0].in_transaction  # exact missing-rollback mutant anchor
        # Even unchanged refreshes execute the snapshot read, not an answer memo.
        statements = []
        opened[0].set_trace_callback(statements.append)
        assert mt.cached_counters(tmp_path, clock=lambda: 120) == second
        assert any(s == "BEGIN" for s in statements)
        assert any(s == "ROLLBACK" for s in statements)
        assert not opened[0].in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            opened[0].execute("UPDATE session_model_usage SET input_tokens=99")
        mt.reset_cache()
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
    finally:
        writer.close()


def test_wal_checkpoint_and_replacement_revalidate(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    writer = database(path)
    opened = observe(monkeypatch)
    try:
        first = mt.counters(tmp_path)
        writer.execute("UPDATE session_model_usage SET input_tokens=20")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        assert mt.counters(tmp_path)["tokens_io_total"] == 22
        assert all(not db.in_transaction for db in opened)
        # Close the writer before replacing the fixture's complete image.
        writer.close()
        replacement = tmp_path / "new.db"
        other = database(replacement)
        other.execute("UPDATE session_model_usage SET input_tokens=30")
        other.commit()
        other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        other.close()
        # Existing kept reader must not pin the old inode as the current path.
        os.replace(replacement, path)
        assert mt.counters(tmp_path)["tokens_io_total"] == 32
        assert first["tokens_io_total"] == 3
    finally:
        writer.close()


def test_profile_removal_closes_reader_and_readdition_is_fresh(tmp_path, monkeypatch):
    root = database(tmp_path / "state.db")
    profile_path = tmp_path / "profiles" / "extra" / "state.db"
    profile = database(profile_path)
    opened = observe(monkeypatch)
    try:
        assert mt.counters(tmp_path)["tokens_io_total"] == 6
        assert len(opened) == 2
        profile.close()
        profile_path.unlink()
        assert mt.counters(tmp_path)["tokens_io_total"] == 3
        # The old profile reader is closed even though it was not queried again.
        with pytest.raises(sqlite3.ProgrammingError):
            opened[1].execute("SELECT 1")
        profile = database(profile_path)
        assert mt.counters(tmp_path)["tokens_io_total"] == 6
    finally:
        root.close()
        profile.close()


def test_two_threads_share_only_serialized_connection_not_snapshots(tmp_path, monkeypatch):
    writer = database(tmp_path / "state.db")
    opened = observe(monkeypatch)
    try:
        expected = mt.counters(tmp_path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(mt.counters, [tmp_path] * 20))
        assert all(result == expected for result in results)
        assert len(opened) == 1
        assert not opened[0].in_transaction
    finally:
        writer.close()


def test_read_failure_omits_whole_tuple_then_recovers(tmp_path, monkeypatch):
    writer = database(tmp_path / "state.db")
    try:
        expected = mt.counters(tmp_path)
        original = mt.incremental.read

        def failed(db, *args):
            assert db.in_transaction
            raise sqlite3.OperationalError("fixture")

        monkeypatch.setattr(mt.incremental, "read", failed)
        assert mt.counters(tmp_path) == {}
        monkeypatch.setattr(mt.incremental, "read", original)
        assert mt.counters(tmp_path) == expected
    finally:
        writer.close()
