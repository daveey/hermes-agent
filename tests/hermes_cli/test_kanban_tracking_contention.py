"""Unrelated offline I/O must not pin a board's reader cleanup lock."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from hermes_cli import kanban_dispatch_owner as owner
from hermes_cli import kanban_generation as generation
from hermes_cli import kanban_lockguard as guard
from hermes_cli import sqlite_safe_read as tracking


@pytest.mark.parametrize("operation", ["close", "failed_open", "recovery"])
def test_tracking_wait_releases_board_for_idle_reader(tmp_path, monkeypatch, operation):
    path = tmp_path / "board.db"
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.close()
    reader = guard.connect(path, check_same_thread=False)
    ready, proceed, attempted = (threading.Event() for _ in range(3))
    real_lock = tracking._live_lock
    worker_ident = []

    class ObservedTrackingLock:
        def acquire(self, *args, **kwargs):
            if worker_ident and threading.get_ident() == worker_ident[0] and proceed.is_set():
                attempted.set()
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_):
            self.release()

    monkeypatch.setattr(tracking, "_live_lock", ObservedTrackingLock())
    original_prepare = generation.prepare_connection

    def prepare(conn, timeout):
        original_prepare(conn, timeout)
        if operation == "failed_open":
            ready.set()
            assert proceed.wait(5)
            raise ValueError("injected preparation failure")

    monkeypatch.setattr(generation, "prepare_connection", prepare)

    def work():
        worker_ident.append(threading.get_ident())
        if operation == "failed_open":
            with pytest.raises(ValueError, match="injected"):
                owner.connect_with_identity(path, timeout=0.1)
            return
        conn = owner.connect_with_identity(path, timeout=0.1)
        ready.set()
        assert proceed.wait(5)
        try:
            if operation == "close":
                conn.close()
            else:
                conn._hermes_generation_shm = (-1, -1)
                try:
                    replacement = guard.connect(path, timeout=0.1)
                except sqlite3.OperationalError as exc:
                    assert generation.is_busy(exc)
                else:
                    replacement.close()
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(work)
        assert ready.wait(5)
        with tracking.offline_file_access(tmp_path / "unrelated.db"):
            proceed.set()
            assert attempted.wait(5)
            closing = pool.submit(reader.close)
            # Record before releasing offline I/O; always release it before assertions/cleanup.
            try:
                closing.result(timeout=0.5)
                closed = True
            except TimeoutError:
                closed = False
        pending.result(timeout=5)
        closing.result(timeout=5)
    assert closed, f"{operation} propagated global tracking wait to idle reader close"
    assert not tracking.has_live_connection(path)


def test_lifecycle_busy_does_not_retry_readwrite(tmp_path, monkeypatch):
    calls = []

    def busy(database, **kwargs):
        calls.append(database)
        raise generation.BoardLifecycleBusy()

    monkeypatch.setattr(guard, "connect", busy)
    with pytest.raises(generation.BoardLifecycleBusy) as raised:
        generation.open_ro(tmp_path / "board.db")
    assert raised.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    assert len(calls) == 1
    assert calls[0].endswith("?mode=ro")
