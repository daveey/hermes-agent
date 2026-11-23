"""Strict linked-card readers queued behind one projection share the next one instead of each rebuilding in turn.

Hub 2026-11-22: workers wrote a board every few seconds, so a strict reader's fingerprint (taken before
``_links_lock``) never matched the projection it had waited for. Every queued reader rebuilt in series (~6 s each),
the lock was held 98% of the time and Desktop user-steps polls timed out behind it. [daveey]"""
import itertools
import threading

import pytest

from hermes_cli import kanban_session_activity as activity
from tests.waiting import wait_until


class _CountingLock:
    """``_links_lock`` stand-in that counts acquire attempts, so a test knows every reader is queued."""

    def __init__(self):
        self._lock = threading.Lock()
        self._mutex = threading.Lock()
        self.attempts = 0

    def __enter__(self):
        with self._mutex:
            self.attempts += 1
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


@pytest.fixture
def busy_board(monkeypatch):
    """Every fingerprint read sees a new board write; the first projection blocks until released."""
    lock = _CountingLock()
    monkeypatch.setattr(activity, "_links_lock", lock)
    monkeypatch.setattr(activity, "_origin_scope", lambda: ("A",))
    writes = itertools.count()
    monkeypatch.setattr(activity, "_links_fingerprint", lambda: (next(writes),))
    projections = []
    first_started, release_first = threading.Event(), threading.Event()

    def project():
        n = len(projections)
        projections.append(n)
        if n == 0:
            first_started.set()
            assert release_first.wait(10)
        return [{"id": "A", "projection": n}]

    monkeypatch.setattr(activity, "_project_remote_links", project)
    activity._reset_cache_for_tests()
    lock.attempts = 0
    yield lock, projections, first_started, release_first
    release_first.set()
    activity._reset_cache_for_tests()


def _read_in_thread(results, key):
    thread = threading.Thread(target=lambda: results.__setitem__(key, activity._remote_links()), daemon=True)
    thread.start()
    return thread


def test_queued_strict_readers_share_one_rebuild(busy_board):
    lock, projections, first_started, release_first = busy_board
    results = {}
    first = _read_in_thread(results, "first")
    assert first_started.wait(5)
    waiters = [_read_in_thread(results, i) for i in range(4)]
    wait_until(lambda: lock.attempts == 5, what="every reader queued on _links_lock")
    release_first.set()
    for thread in [first, *waiters]:
        thread.join(10)
        assert not thread.is_alive()
    # The first projection started before the waiters arrived, so it cannot answer them (read-your-writes);
    # the second started after all of them arrived, so it answers every one.
    assert projections == [0, 1]
    assert results["first"][0]["projection"] == 0
    assert {results[i][0]["projection"] for i in range(4)} == {1}


def test_reader_never_takes_a_projection_that_started_before_it(busy_board):
    _, projections, _, release_first = busy_board
    release_first.set()
    assert activity._remote_links()[0]["projection"] == 0
    # The board moved since (new fingerprint) and this reader arrived after projection 0 started: it must rebuild.
    assert activity._remote_links()[0]["projection"] == 1
    assert projections == [0, 1]
