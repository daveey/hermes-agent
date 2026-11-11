"""Commit keys are comparable across readers, including a cold concurrent open."""
import concurrent.futures
import sqlite3
import threading

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_session_activity as activity


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_concurrent_legacy_keys_share_commit_observer(tmp_path, monkeypatch, journal):
    path = tmp_path / "legacy.db"
    activity._reset_cache_for_tests()
    writer = sqlite3.connect(path)
    try:
        writer.execute(f"PRAGMA journal_mode={journal}")
        writer.executescript("CREATE TABLE tasks (title TEXT); INSERT INTO tasks VALUES ('one');")
        frozen = rev.file_stat(path)
        monkeypatch.setattr(rev, "file_stat", lambda _: frozen)

        def batch():
            barrier = threading.Barrier(8)

            def read():
                barrier.wait(timeout=5)
                return activity._board_content_key(path)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                keys = list(pool.map(lambda _: read(), range(8)))
            assert all(key == keys[0] for key in keys), keys
            return keys[0]

        first = batch()
        writer.execute("UPDATE tasks SET title='two'")
        writer.commit()
        second = batch()
        assert first != second
        assert batch() == second
        # Reopen only the reader; keep content caches and the frozen stat intact.
        # A new connection can start at the same data_version as its predecessor.
        from hermes_cli.kanban_generation import evict_readers
        evict_readers(str(path))
        writer.execute("UPDATE tasks SET title='new'")
        writer.commit()
        third = batch()
        assert third != second
        assert third != first  # reopened data_version can equal the first batch's
        assert batch() == third
    finally:
        writer.close()
        activity._reset_cache_for_tests()


@pytest.mark.parametrize("legacy", [False, True])
def test_concurrent_hub_signatures_share_one_memo(tmp_path, monkeypatch, legacy):
    from hermes_cli import kanban_session_activity_hub as hub
    from hermes_cli.kanban_generation import evict_readers

    path = tmp_path / "hub.db"
    activity._reset_cache_for_tests()
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE meta (k TEXT, v TEXT)")
        writer.execute("CREATE TABLE replica (coll TEXT, seq INTEGER)" if not legacy
                       else "CREATE TABLE replica (coll TEXT)")
        writer.commit()
        frozen = hub.file_stat(path)
        monkeypatch.setattr(hub, "file_stat", lambda _: frozen)
        queries = []
        with activity.cached_ro(path) as conn:
            conn.set_trace_callback(lambda sql: queries.append(sql) if sql.startswith("SELECT coll,") else None)

        def batch():
            barrier = threading.Barrier(8)

            def read(_):
                barrier.wait(timeout=5)
                return activity._hub_content_signature(path)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                signatures = list(pool.map(read, range(8)))
            assert all(sig == signatures[0] for sig in signatures), signatures
            return signatures[0]

        first = batch()
        assert batch() == first
        # SQLite does not trace statements which fail to prepare (old schema).
        if not legacy:
            assert len(queries) == 1
        writer.execute("INSERT INTO replica VALUES ('tasks', 1)" if not legacy
                       else "INSERT INTO replica VALUES ('tasks')")
        writer.commit()
        second = batch()
        assert second != first
        assert batch() == second
        if not legacy:
            assert len(queries) == 2
        evict_readers(str(path))  # memo survives a reader replacement
        writer.execute("INSERT INTO replica VALUES ('tasks', 2)" if not legacy
                       else "INSERT INTO replica VALUES ('tasks')")
        writer.commit()
        third = batch()
        assert third != second
        assert third != first  # reopened data_version can equal the first batch's
        assert batch() == third
    finally:
        writer.close()
        activity._reset_cache_for_tests()
