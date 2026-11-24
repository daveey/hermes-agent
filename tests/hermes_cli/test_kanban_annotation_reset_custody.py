"""Cache teardown must not close SQLite readers still owned by a bounded refresh."""

from concurrent.futures import ThreadPoolExecutor
import threading

from hermes_cli import kanban_annotation_refresh as bounded
from hermes_cli import kanban_session_activity as activity
from tests.waiting import wait_until


def test_reset_waits_for_bounded_refresh_before_closing_reader(monkeypatch):
    activity._reset_cache_for_tests()
    entered = threading.Event()
    release = threading.Event()
    reset_waiting = threading.Event()
    reset_done = threading.Event()
    finished = threading.Event()
    closed = threading.Event()
    lock = threading.Lock()

    class ObservedLock:
        def acquire(self):
            reset_waiting.set()
            assert lock.acquire(timeout=10)
            return True

        def release(self):
            lock.release()

    class Reader:
        def close(self):
            closed.set()
            assert finished.is_set(), "reader closed during refresh"

    monkeypatch.setattr(activity, "_annotate_refresh_lock", ObservedLock())
    with activity._RO_CACHE_LOCK:
        activity._RO_CACHE["reset-custody"] = (Reader(),)

    def refresh():
        entered.set()
        assert release.wait(10)
        assert not closed.is_set()
        finished.set()
        return ()

    def reset():
        try:
            activity._reset_cache_for_tests()
        finally:
            reset_done.set()

    assert lock.acquire(timeout=10)
    assert bounded.refresh_bounded(refresh, lock, None, 0) is None
    assert entered.wait(10)
    with ThreadPoolExecutor(max_workers=1) as pool:
        resetting = pool.submit(reset)
        try:
            assert wait_until(lambda: reset_waiting.is_set() or reset_done.is_set(), timeout=10)
            assert reset_waiting.is_set(), "reset bypassed the refresh ownership lock"
            assert not closed.is_set()
        finally:
            release.set()
        resetting.result(timeout=10)
    assert finished.is_set()
    assert closed.is_set()
    assert not activity._RO_CACHE
