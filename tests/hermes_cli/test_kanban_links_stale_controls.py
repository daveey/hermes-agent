"""Bounds and failure controls for last-good linked-card projections."""
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity
from tests.waiting import wait_until


@pytest.fixture
def cache(monkeypatch):
    scope, revision = ['A'], [0]
    calls = []
    monkeypatch.setattr(activity, '_origin_scope', lambda: (scope[0],))
    monkeypatch.setattr(activity, '_links_fingerprint', lambda: (revision[0],))

    def project():
        calls.append(scope[0])
        return [{'id': scope[0], 'revision': revision[0]}]

    monkeypatch.setattr(activity, '_project_remote_links', project)
    activity._reset_cache_for_tests()
    yield scope, revision, calls
    wait_until(lambda: not activity._links_refresher.busy, what='refresh cleanup')
    activity._reset_cache_for_tests()


def test_worker_start_failure_can_retry(monkeypatch):
    import threading
    from hermes_cli.kanban_links_refresh import ScopeRefresher

    called = []
    refresher = ScopeRefresher(called.append, 16)

    def fail_start(thread):
        raise RuntimeError('no thread available')

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, 'start', fail_start)
        refresher.submit(('A',))
        assert not refresher.busy
        assert refresher._worker is None
    refresher.submit(('A',))
    wait_until(lambda: not refresher.busy, what='thread creation retry')
    assert called == [('A',)]


def test_sequential_refreshes_reuse_one_worker(cache, monkeypatch):
    import threading

    _, revision, _ = cache
    activity._remote_links()
    starts = []
    original = threading.Thread.start

    def start(thread):
        if thread.name == 'kanban-remote-links':
            starts.append(thread)
        return original(thread)

    monkeypatch.setattr(threading.Thread, 'start', start)
    for i in range(1, 4):
        revision[0] = i
        activity._remote_links(stale_ok=True)
        wait_until(lambda: not activity._links_refresher.busy, what='sequential refresh')
        assert activity._links_cache[('A',)][3][0]['revision'] == i
    assert len(starts) <= 1, 'one new thread per poll is not a bounded worker lifecycle'


def test_queued_scopes_are_bounded_and_coalesced(cache, monkeypatch):
    import threading
    from contextvars import ContextVar

    scope = ContextVar('bounded_refresh_scope', default='0')
    monkeypatch.setattr(activity, '_origin_scope', lambda: (scope.get(),))
    monkeypatch.setattr(activity, '_project_remote_links', lambda: [{'id': scope.get()}])
    entered, release = threading.Event(), threading.Event()
    checked = []

    def fingerprint():
        checked.append(scope.get())
        if scope.get() == '0':
            entered.set()
            assert release.wait(5)
        return ('changed',)

    for i in range(16):
        scope.set(str(i))
        activity._remote_links()
    monkeypatch.setattr(activity, '_links_fingerprint', fingerprint)
    scope.set('0')
    try:
        activity._remote_links(stale_ok=True)
        assert entered.wait(2)
        for _ in range(3):
            for i in range(16):
                scope.set(str(i))
                assert activity._remote_links(stale_ok=True)[0]['id'] == str(i)
        scope.set('overflow')
        activity._refresh_links_in_background(('overflow',))
        assert len(activity._links_refresher._pending) == 16
    finally:
        release.set()
        wait_until(lambda: not activity._links_refresher.busy, what='bounded queue drained')
    assert checked == [str(i) for i in range(16)]
    activity._refresh_links_in_background(('overflow',))
    wait_until(lambda: not activity._links_refresher.busy, what='overflow retries later')
    assert checked[-1] == 'overflow'


def test_changed_ambient_scope_does_not_publish_wrong_origin(cache, monkeypatch):
    scope, revision, _ = cache
    first = activity._remote_links()
    revision[0] = 1
    scope[0] = 'different'
    activity._refresh_links_in_background(('A',))
    wait_until(lambda: not activity._links_refresher.busy, what='mismatched origin skipped')
    assert activity._links_cache[('A',)][3] == first
    assert ('different',) not in activity._links_cache


def test_scope_capacity_and_cold_eviction(cache):
    scope, _, calls = cache
    for i in range(17):
        scope[0] = str(i)
        assert activity._remote_links()[0]['id'] == str(i)
    assert len(activity._links_cache) == 16
    scope[0] = '0'
    assert activity._remote_links(stale_ok=True)[0]['id'] == '0'
    assert len(calls) == 18  # genuine cold miss remains synchronous, not successful empty


def test_background_failure_retains_good_but_strict_failure_raises(cache, monkeypatch):
    _, revision, _ = cache
    first = activity._remote_links()
    revision[0] = 1

    def fail():
        raise sqlite3.OperationalError('projection unavailable')

    monkeypatch.setattr(activity, '_project_remote_links', fail)
    assert activity._remote_links(stale_ok=True) == first
    wait_until(lambda: not activity._links_refresher.busy, what='failed refresh finished')
    with pytest.raises(sqlite3.OperationalError, match='projection unavailable'):
        activity._remote_links()
    assert activity._links_cache[('A',)][3] == first
    activity._reset_cache_for_tests()
    with pytest.raises(sqlite3.OperationalError, match='projection unavailable'):
        activity._remote_links(stale_ok=True)
    assert not activity._links_cache


def test_strict_read_your_writes_after_stale_read(cache):
    _, revision, _ = cache
    first = activity._remote_links()
    assert activity._remote_links(stale_ok=True) == first
    wait_until(lambda: not activity._links_refresher.busy, what='fresh check finished')
    revision[0] = 1
    assert activity._remote_links()[0]['revision'] == 1


def test_annotation_pins_cards_and_origin_rows(cache):
    _, revision, _ = cache
    with activity.one_annotation_snapshot():
        first = activity.remote_links_and_origin_rows()
        revision[0] = 1
        assert activity.remote_links_and_origin_rows() == first
    assert activity.remote_links_and_origin_rows()[0][0]['revision'] == 1
