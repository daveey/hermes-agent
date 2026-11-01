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


def test_guarded_custom_factory_close_waits_outside_tracking_lock(tmp_path):
    from hermes_cli.kanban_dispatch_owner import connect_with_identity
    from hermes_cli.sqlite_safe_read import _live_lock

    entered = threading.Event()
    path = tmp_path / "board.db"
    conn = connect_with_identity(path, factory=guard.GuardedConnection, check_same_thread=False)

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
