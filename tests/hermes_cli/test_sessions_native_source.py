"""Fairness, bounded inventories, failure isolation and expiry controls."""
from types import SimpleNamespace

import pytest

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Ready, Scope, SourceId, Unavailable
from hermes_cli.fact_store import FactStore, view_key
from hermes_cli.sessions_native_source import SessionsSource


@pytest.fixture
def source(monkeypatch):
    from hermes_cli import sessions_native_source as module
    clock = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    store = FactStore()
    scope = Scope('install', 'scope')
    calls = []
    def read(name, selector):
        calls.append((name, selector['id']))
        if selector['id'] == 'bad':
            raise RuntimeError('annotation unavailable')
        return selector['id'].encode()
    result = SessionsSource(store, SourceId(scope, 'sessions.display', 'test'), [], read, max_views=2)
    result._watch = SimpleNamespace(check=lambda: None, close=lambda: None)
    result._wake = lambda: None
    return result, clock, calls


def step(source, clock):
    capture = source.capture(None, CaptureBudget(max_bytes=4096, max_seconds=5))
    source.commit(capture)
    clock[0] += 1
    return capture


def test_hot_hints_do_not_starve_and_failure_is_per_selector(source):
    owner, clock, calls = source
    owner.demand('sessions', {'id': 'bad'})
    owner.demand('sessions', {'id': 'good'})
    step(owner, clock)
    owner.invalidate()  # Continuous writer re-dirties the failed first key.
    step(owner, clock)
    assert calls == [('sessions', 'bad'), ('sessions', 'good')]
    snap = owner.store.snapshot(owner.source.scope)
    assert isinstance(snap.view('sessions', {'id': 'bad'}), Unavailable)
    assert snap.view('sessions', {'id': 'good'}).encoded == b'good'
    owner.invalidate()
    step(owner, clock)
    assert calls[-1] == ('sessions', 'good')  # bad is in per-key backoff
    clock[0] += 5
    owner.reader = lambda *_: b'recovered'
    step(owner, clock)
    assert owner.store.snapshot(owner.source.scope).view('sessions', {'id': 'bad'}).encoded == b'recovered'


def test_lru_and_per_endpoint_capacity(source):
    owner, clock, _ = source
    for sid in ('one', 'two'):
        owner.demand('sessions', {'id': sid})
        step(owner, clock)
    owner.touch('sessions', {'id': 'one'})
    owner.demand('sessions', {'id': 'three'})
    step(owner, clock)
    snap = owner.store.snapshot(owner.source.scope)
    assert isinstance(snap.view('sessions', {'id': 'one'}), Ready)
    assert snap.get(view_key(owner.source.scope, 'sessions', {'id': 'two'})) is None
    for sid in ('a', 'b', 'c'):
        owner.demand('sessions_stamps', {'id': sid})
        step(owner, clock)
    assert owner.store.snapshot(owner.source.scope).view('sessions', {'id': 'one'}).encoded == b'one'
    assert len(owner._selectors) == 4


def test_cooldown_and_quiet_reconciliation(source):
    owner, clock, calls = source
    owner.demand('sessions', {'id': 'one'})
    capture = owner.capture(None, CaptureBudget())
    owner.commit(capture)
    owner.invalidate()
    assert owner.capture(None, CaptureBudget()).fact is None
    assert len(calls) == 1
    clock[0] += 31
    observed = []
    owner.reconcile = lambda: observed.append('roster')
    step(owner, clock)
    assert observed == ['roster']
    assert len(calls) == 2


def test_over_budget_and_eviction_during_capture(source):
    owner, clock, _ = source
    owner.reader = lambda *_: b'x' * 5000
    owner.demand('sessions', {'id': 'large'})
    step(owner, clock)
    assert isinstance(owner.store.snapshot(owner.source.scope).view('sessions', {'id': 'large'}), Unavailable)
    owner.reader = lambda *_: b'good'
    owner.demand('sessions', {'id': 'ok'})
    capture = owner.capture(None, CaptureBudget())
    owner.demand('sessions', {'id': 'third'})
    owner.demand('sessions', {'id': 'fourth'})
    owner.commit(capture)
    assert owner.store.snapshot(owner.source.scope).get(capture.fact.key) is None


def test_time_boundary_and_total_bytes(source):
    owner, clock, _ = source
    owner.refresh_at(clock[0] + 2)
    assert owner._refresh == clock[0] + 2
    owner.max_bytes = 6
    owner.max_views = 4
    for sid in ('one', 'two', 'abc'):
        owner.demand('sessions', {'id': sid})
        step(owner, clock)
    assert sum(owner._sizes.values()) <= 6
    assert owner.store._fact_bytes <= owner.store._fact_limit


def test_elapsed_display_deadline_does_not_spin_idle_minute(source):
    owner, clock, calls = source
    original = owner.reader
    def read(name, selector):
        owner.refresh_at(clock[0] - 1)  # canonical annotation can still say active after its boundary
        return original(name, selector)
    owner.reader = read
    owner.demand('sessions', {'id': 'one'})
    captures = 0
    end = clock[0] + 60
    while clock[0] <= end:
        capture = owner.capture(None, CaptureBudget())
        owner.commit(capture)
        captures += 1
        assert capture.next_deadline > clock[0], 'expired display boundary must not rearm itself'
        clock[0] = capture.next_deadline
    assert captures <= 3
    assert len(calls) == captures


@pytest.mark.parametrize('reason', ['reader_error', 'invalid_type', 'bytes', 'elapsed'])
def test_capture_failure_reason_is_safe_bounded_and_recovers(source, caplog, reason):
    owner, clock, _ = source
    secret = 'private-selector-and-exception'
    selector = {'id': secret}
    def fail(*_):
        if reason == 'reader_error':
            raise RuntimeError(secret)
        if reason == 'invalid_type':
            return secret
        if reason == 'elapsed':
            clock[0] += 6
            return b'valid'
        return b'x' * 5000
    owner.reader = fail
    owner.demand('sessions', selector)
    with caplog.at_level('WARNING', logger='hermes_cli.sessions_native_source'):
        step(owner, clock)
        assert isinstance(owner.store.snapshot(owner.source.scope).view('sessions', selector), Unavailable)
        clock[0] += 40
        step(owner, clock)
        messages = [r.getMessage() for r in caplog.records if r.name == 'hermes_cli.sessions_native_source']
        assert len(messages) == 1
        assert f'reason={reason}' in messages[0]
        assert 'elapsed_seconds=' in messages[0] and 'encoded_bytes=' in messages[0]
        assert secret not in messages[0]
        clock[0] += 40
        owner.reader = lambda *_: b'recovered'
        step(owner, clock)
        assert isinstance(owner.store.snapshot(owner.source.scope).view('sessions', selector), Ready)
        owner.reader = fail
        owner.invalidate()
        clock[0] += 40
        step(owner, clock)
        messages = [r.getMessage() for r in caplog.records if r.name == 'hermes_cli.sessions_native_source']
        assert len(messages) == 2


def test_evicted_failed_selector_reports_new_failure_episode(source, caplog):
    owner, clock, calls = source
    owner.max_views = 1
    selector = {'id': 'bad'}
    key = view_key(owner.source.scope, 'sessions', selector)
    with caplog.at_level('WARNING', logger='hermes_cli.sessions_native_source'):
        owner.demand('sessions', selector)
        step(owner, clock)
        owner.demand('sessions', {'id': 'good'})
        step(owner, clock)
        assert owner.store.snapshot(owner.source.scope).get(key) is None
        owner.demand('sessions', selector)
        step(owner, clock)
    messages = [r.getMessage() for r in caplog.records if r.name == 'hermes_cli.sessions_native_source']
    assert calls == [('sessions', 'bad'), ('sessions', 'good'), ('sessions', 'bad')]
    assert len(messages) == 2
    assert all('reason=reader_error' in message for message in messages)
    assert isinstance(owner.store.snapshot(owner.source.scope).view('sessions', selector), Unavailable)


def test_expiry_watch_failure_and_close(source):
    owner, clock, _ = source
    owner.demand('sessions', {'id': 'one'})
    step(owner, clock)
    owner.failure()
    fact = owner.store.snapshot(owner.source.scope).get(view_key(owner.source.scope, 'sessions', {'id': 'one'}))
    assert fact.health == 'stale' and fact.value['encoded'] == b'one'
    owner.close()
    with pytest.raises(RuntimeError, match='closed'):
        owner.demand('sessions', {'id': 'two'})
    with pytest.raises(RuntimeError, match='closed'):
        owner.watch(lambda: None)
