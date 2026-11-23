"""Exercise the actual owner thread, publication and source-watch boundaries."""
import logging
import sqlite3
import threading
from types import SimpleNamespace
import pytest

from hermes_cli.fact_feed import ChangeFeed


class Adapter:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.ready = threading.Event()
        self.capture_hook = lambda cursor: SimpleNamespace(after=len(self.calls))

    def watch(self, wake):
        self.wake = wake
        wake()  # registration-race control, before register returns
        return self

    def close(self):
        self.closed = True

    def capture(self, cursor, budget):
        assert not self.closed
        self.calls.append(cursor)
        self.ready.set()
        return self.capture_hook(cursor)


def test_one_thread_no_idle_poll_and_late_wake():
    source = Adapter()
    entered, release, twice = threading.Event(), threading.Event(), threading.Event()
    commits = []
    def capture(cursor):
        if len(source.calls) == 1:
            entered.set()
            assert release.wait(3)
        return SimpleNamespace(after=len(source.calls))
    source.capture_hook = capture
    def commit(adapter, captured):
        commits.append((captured.after, threading.get_ident()))
        if len(commits) == 2:
            twice.set()
    feed = ChangeFeed(commit, lambda *args: None)
    try:
        registration = feed.register(source)
        assert entered.wait(3)
        source.wake()
        release.set()
        assert twice.wait(3)
        assert source.calls == [None, 1]
        assert len({thread for _, thread in commits}) == 1
        source.ready.clear()
        assert not source.ready.wait(.08), "idle source was polled"
        registration.close()
        source.wake()
        assert not source.ready.wait(.08)
    finally:
        release.set()
        feed.close()
    assert source.closed


def test_failed_publication_retries_same_cursor_without_new_hint():
    source = Adapter()
    recovered, failed = threading.Event(), threading.Event()
    seen = []
    def commit(adapter, capture):
        seen.append(capture.after)
        if len(seen) == 1:
            raise TypeError("publication aborted")
        recovered.set()
    feed = ChangeFeed(commit, lambda *args: failed.set())
    try:
        feed.register(source)
        assert failed.wait(3)
        assert recovered.wait(3)
        assert source.calls == [None, None]
    finally:
        feed.close()


def test_wake_during_publication_does_not_block_native_dispatch():
    source = Adapter()
    entered, release, woken = threading.Event(), threading.Event(), threading.Event()
    def commit(adapter, capture):
        entered.set()
        assert release.wait(3)
    feed = ChangeFeed(commit, lambda *args: None)
    try:
        feed.register(source)
        assert entered.wait(3)
        def wake():
            source.wake()
            woken.set()
        thread = threading.Thread(target=wake)
        thread.start()
        assert woken.wait(1)
        thread.join(1)
        with pytest.raises(TimeoutError):
            feed.close(timeout=.01)
    finally:
        release.set()
        feed.close()


def test_unregister_during_capture_cannot_publish():
    source = Adapter()
    entered, release = threading.Event(), threading.Event()
    commits = []
    def capture(cursor):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(after=1)
    source.capture_hook = capture
    feed = ChangeFeed(lambda *args: commits.append(args), lambda *args: None)
    try:
        registration = feed.register(source)
        assert entered.wait(3)
        registration.close()
        release.set()
    finally:
        release.set()
        feed.close()
    assert not commits


def test_retry_floor_not_bypassed_by_watch_storm():
    source = Adapter()
    failed, recovered = threading.Event(), threading.Event()
    commits = []
    def commit(adapter, capture):
        commits.append(capture)
        if len(commits) == 1:
            raise RuntimeError("retry later")
        recovered.set()
    feed = ChangeFeed(commit, lambda *args: failed.set())
    try:
        feed.register(source)
        assert failed.wait(3)
        source.ready.clear()
        for _ in range(100):
            source.wake()
        assert not source.ready.wait(.03), "hints bypassed retry floor"
        assert recovered.wait(3)
    finally:
        feed.close()


def _failing_feed(error, attempts):
    source = Adapter()
    failures, done = [], threading.Event()
    def capture(cursor):
        raise error()
    source.capture_hook = capture
    def failure(adapter, exc):
        failures.append(exc)
        if len(failures) >= attempts:
            done.set()
    return source, done, ChangeFeed(lambda *args: None, failure)


def _busy():
    exc = sqlite3.OperationalError("database is locked: SQLite tracking registry is busy")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


@pytest.mark.parametrize("error, tracebacks", [(_busy, 0), (lambda: RuntimeError("defect"), 1)])
def test_retry_streak_logs_at_most_one_traceback(caplog, error, tracebacks):
    # Each retry used to format a full traceback under the logging lock; at ~2/s
    # across sources that stalled the gateway event loop past its watchdog.
    caplog.set_level(logging.DEBUG, logger="hermes_cli.fact_feed")
    source, done, feed = _failing_feed(error, 3)
    try:
        feed.register(source)
        assert done.wait(5)
    finally:
        feed.close()
    records = [r for r in caplog.records if r.name == "hermes_cli.fact_feed"]
    assert len(records) >= 3
    assert sum(r.exc_info is not None for r in records) == tracebacks
    assert sum(r.levelno >= logging.WARNING for r in records) <= 2
