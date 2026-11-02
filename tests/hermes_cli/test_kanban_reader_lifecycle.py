"""A board lifecycle transition must not hold the cross-board reader-cache lock."""
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import threading

import pytest

from hermes_cli import kanban_session_activity as activity


def _seed(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker(value)")
    conn.execute("INSERT INTO marker VALUES (42)")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("phase", ["open", "close"])
def test_cache_registry_is_available_during_board_io(tmp_path, monkeypatch, phase):
    path = tmp_path / "board.db"
    _seed(path)
    if phase == "close":
        with activity.cached_ro(path):
            pass
        replacement = tmp_path / "replacement.db"
        _seed(replacement)
        os.replace(replacement, path)
    entered, release = threading.Event(), threading.Event()
    seam = "_open_ro" if phase == "open" else "_close_quietly"
    original = getattr(activity, seam)

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(activity, seam, paused)

    def read():
        with activity.cached_ro(path) as conn:
            return conn.execute("SELECT value FROM marker").fetchone()[0]

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(read)
        try:
            assert entered.wait(10)
            acquired = activity._RO_CACHE_LOCK.acquire(timeout=2)
            if acquired:
                activity._RO_CACHE_LOCK.release()
            assert acquired, "board I/O holds the global reader registry lock"
        finally:
            release.set()
            pending.result(timeout=10)
            activity._reset_cache_for_tests()


def test_recovery_refuses_busy_cache_registry(tmp_path):
    from hermes_cli.kanban_generation import DeletedWalGenerationError, evict_readers

    with ThreadPoolExecutor(max_workers=1) as pool:
        with activity._RO_CACHE_LOCK:
            pending = pool.submit(evict_readers, str(tmp_path / "board.db"))
            with pytest.raises(DeletedWalGenerationError, match="registry is busy"):
                pending.result(timeout=3)


def test_replaced_reader_is_not_closed_under_borrower(tmp_path):
    from hermes_cli.kanban_generation import DeletedWalGenerationError

    path = tmp_path / "board.db"
    _seed(path)
    try:
        with activity.cached_ro(path) as held:
            replacement = tmp_path / "new.db"
            _seed(replacement)
            os.replace(replacement, path)
            with pytest.raises(DeletedWalGenerationError, match="still borrowed"):
                with activity.cached_ro(path):
                    pass
            assert held.execute("SELECT value FROM marker").fetchone() == (42,)
        with activity.cached_ro(path) as fresh:
            assert fresh is not held
            assert fresh.execute("SELECT value FROM marker").fetchone() == (42,)
    finally:
        activity._reset_cache_for_tests()
