"""Bounded batch publication keeps fairness, per-view faults and trailing writes."""
from types import SimpleNamespace

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Ready, Scope, SourceId, Unavailable
from hermes_cli.fact_store import FactStore
from hermes_cli.sessions_capture_process import CaptureUnavailable
from hermes_cli.sessions_native_source import SessionsSource


def test_batch_publishes_together_keeps_trailing_write_and_duty(monkeypatch):
    from hermes_cli import sessions_native_source as module
    clock = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    store = FactStore()
    scope = Scope('batch', 'sessions')
    calls = []
    owner = None

    def batch(views):
        calls.append(views)
        owner.invalidate()  # A genuine write DURING the read must survive commit.
        clock[0] += 2
        return [b'good', CaptureUnavailable('reader_error')][:len(views)]

    owner = SessionsSource(store, SourceId(scope, 'sessions.display', 'test'), [],
                           lambda *_: b'unexpected', capture_many=batch)
    owner._watch = SimpleNamespace(check=lambda: None)
    for name in ('sessions', 'sessions_sidebar'):
        owner.demand(name, {})
    capture = owner.capture(None, CaptureBudget(max_seconds=5))
    assert len(calls) == 1 and len(calls[0]) == 2
    owner.commit(capture)
    snap = store.snapshot(scope)
    assert isinstance(snap.view('sessions', {}), Ready)
    assert isinstance(snap.view('sessions_sidebar', {}), Unavailable)
    assert len(owner._dirty) == 2
    assert owner._next_capture == 104  # Whole two-second batch pays two-second cooldown.
    owner.commit(owner.capture(None, CaptureBudget()))
    assert len(calls) == 1
    clock[0] = 104
    owner.commit(owner.capture(None, CaptureBudget(max_seconds=5)))
    assert len(calls) == 2 and calls[1] == [('sessions', {})]  # failed peer backs off
    assert isinstance(store.snapshot(scope).view('sessions', {}), Ready)


def test_batch_is_bounded_and_failure_does_not_starve_following_views():
    store = FactStore()
    scope = Scope('batch', 'sessions')
    seen = []

    def batch(views):
        seen.append(views)
        raise CaptureUnavailable('timeout')

    owner = SessionsSource(store, SourceId(scope, 'sessions.display', 'test'), [],
                           lambda *_: b'unexpected', capture_many=batch)
    owner._watch = SimpleNamespace(check=lambda: None)
    for index in range(6):
        owner.demand('sessions', {'id': index})
    owner.commit(owner.capture(None, CaptureBudget()))
    assert len(seen[0]) == 4
    assert len(owner._dirty) == 6  # four retries plus two never attempted
    owner._next_capture = 0
    owner.commit(owner.capture(None, CaptureBudget()))
    assert [selector['id'] for _, selector in seen[1]] == [4, 5]
