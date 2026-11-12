"""Warm linked-card reads must not enter blocking fingerprint/projection work."""
import threading

from hermes_cli import kanban_session_activity as activity
from tests.waiting import wait_until


def test_scope_a_survives_scope_b_projection_lock(monkeypatch):
    from contextvars import ContextVar

    scope = ContextVar('test_links_scope', default='B')
    monkeypatch.setattr(activity, '_origin_scope', lambda: (scope.get(),))
    monkeypatch.setattr(activity, '_links_fingerprint', lambda: ('revision',))
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    held = [False]
    results = []
    projected = []

    def project():
        projected.append(scope.get())
        if held[0] and scope.get() == 'B':
            entered.set()
            assert release.wait(5)
        return [{'id': scope.get()}]

    monkeypatch.setattr(activity, '_project_remote_links', project)
    activity._reset_cache_for_tests()
    scope.set('A')
    expected = activity._remote_links()
    scope.set('B')
    activity._remote_links()
    monkeypatch.setattr(activity, '_links_fingerprint', lambda: ('changed',))
    held[0] = True
    activity._remote_links(stale_ok=True)

    def read_a():
        scope.set('A')
        results.append(activity._remote_links(stale_ok=True))
        returned.set()

    reader = threading.Thread(target=read_a)
    try:
        assert entered.wait(2)
        reader.start()
        assert returned.wait(2), 'scope B evicted the warm scope A fast path'
        assert results == [expected]
    finally:
        release.set()
        if reader.ident is not None:
            reader.join(5)
        wait_until(lambda: not activity._links_refresher.busy, what='scope refresh cleanup')
        activity._reset_cache_for_tests()
    assert projected == ['A', 'B', 'B', 'A'], 'queued A refresh must not be lost behind B'


def test_refresh_captures_nondefault_context(monkeypatch):
    from contextvars import ContextVar

    scope = ContextVar('refresh_scope', default='wrong')
    scope.set('named-profile')
    revision = [0]
    observed = []
    monkeypatch.setattr(activity, '_origin_scope', lambda: (scope.get(),))
    monkeypatch.setattr(activity, '_links_fingerprint', lambda: (revision[0],))

    def project():
        observed.append(scope.get())
        return [{'id': scope.get(), 'revision': revision[0]}]

    monkeypatch.setattr(activity, '_project_remote_links', project)
    activity._reset_cache_for_tests()
    first = activity._remote_links()
    revision[0] = 1
    try:
        assert activity._remote_links(stale_ok=True) == first
        wait_until(lambda: not activity._links_refresher.busy, what='captured refresh')
        assert observed == ['named-profile', 'named-profile']
        assert activity._remote_links()[0]['revision'] == 1
    finally:
        activity._reset_cache_for_tests()


def test_warm_stale_returns_while_fingerprint_is_held(monkeypatch):
    activity._reset_cache_for_tests()
    monkeypatch.setattr(activity, '_origin_scope', lambda: ('A',))
    monkeypatch.setattr(activity, '_links_fingerprint', lambda: ('first',))
    monkeypatch.setattr(activity, '_project_remote_links', lambda: [{'id': 'A'}])
    expected = activity._remote_links()
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    results = []

    def fingerprint():
        entered.set()
        assert release.wait(5)
        return ('next',)

    def read():
        results.append(activity._remote_links(stale_ok=True))
        returned.set()

    monkeypatch.setattr(activity, '_links_fingerprint', fingerprint)
    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert entered.wait(2)
        assert returned.wait(2), 'warm stale reader waited for fingerprint'
        assert results == [expected]
    finally:
        release.set()
        reader.join(5)
        wait_until(lambda: not activity._links_refresher.busy, what='refresh cleanup')
        activity._reset_cache_for_tests()
