"""Healthy WAL generation transitions stay atomic against same-board opens."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from hermes_cli import kanban_db_connect as kb
from hermes_cli import kanban_lockguard as guard
from hermes_cli.sqlite_safe_read import has_live_connection


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("mixed", [False, True])
def test_same_board_open_close_churn(tmp_path, mixed):
    path = tmp_path / "board.db"
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE marker(value)")
    seed.execute("INSERT INTO marker VALUES (42)")
    seed.commit()
    seed.close()
    alias = tmp_path / "alias.db"
    alias.symlink_to(path)
    start = threading.Barrier(4)

    def churn(worker):
        start.wait(timeout=10)
        errors = []
        for _ in range(500):
            try:
                if mixed and worker % 2:
                    conn = guard.connect(alias.as_uri() + "?mode=rw", uri=True)
                else:
                    conn = kb._sqlite_connect(path)
                try:
                    assert conn.execute("SELECT value FROM marker").fetchone() == (42,)
                finally:
                    conn.close()
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        return errors

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(churn, range(4)))
    errors = [error for batch in batches for error in batch]
    assert not errors, (len(errors), errors[:3])
    assert not has_live_connection(path)


@pytest.mark.parametrize("reused_factory", [False, True])
def test_guarded_custom_factory_close_waits_outside_tracking_lock(tmp_path, reused_factory):
    from hermes_cli.kanban_dispatch_owner import connect_with_identity
    from hermes_cli.sqlite_safe_read import _live_lock

    entered = threading.Event()
    path = tmp_path / "board.db"
    factory = guard.GuardedConnection
    if reused_factory:
        prototype = guard.connect(":memory:")
        factory = type(prototype)
        prototype.close()
    conn = connect_with_identity(path, factory=factory, check_same_thread=False)

    class ObservedLock:
        def __enter__(self):
            entered.set()
            conn_lock.acquire()

        def __exit__(self, *args):
            conn_lock.release()

    conn_lock = conn._hermes_lifecycle_lock
    conn._hermes_lifecycle_lock = ObservedLock()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with conn_lock:
            pending = pool.submit(conn.close)
            assert entered.wait(10)
            acquired = _live_lock.acquire(timeout=2)
            if acquired:
                _live_lock.release()
            assert acquired, "close waits for a board while holding the global tracking lock"
        pending.result(timeout=10)
    assert not has_live_connection(path)


@pytest.mark.parametrize("opener", ["native", "guarded", "cached"])
def test_board_lock_wait_honors_reader_budget(tmp_path, opener):
    from hermes_cli.kanban_dispatch_owner import connect_with_identity
    from hermes_cli.kanban_generation import lifecycle_lock
    from hermes_cli.kanban_session_activity import cached_ro

    path = tmp_path / "board.db"
    seed = sqlite3.connect(path)
    seed.close()

    def read():
        if opener == "cached":
            with cached_ro(path):
                pass
        else:
            connect = connect_with_identity if opener == "native" else guard.connect
            connect(path, timeout=0.1).close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with lifecycle_lock(path):
            pending = pool.submit(read)
            with pytest.raises(sqlite3.OperationalError, match="lifecycle is busy"):
                pending.result(timeout=3)
