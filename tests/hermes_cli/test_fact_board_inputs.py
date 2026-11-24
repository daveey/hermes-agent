"""The board's native evidence worker is demanded, bounded and fail-closed."""
import threading
import time

import pytest

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore


@pytest.fixture
def source():
    from hermes_cli.fact_board_inputs import BoardInputSource, CapturedInput
    scope = Scope('installation', 'default', 'main')
    engine = ReducerEngine(FactStore(), scope)
    calls = []
    woke = threading.Event()

    def capture(view, selector, budget):
        calls.append((threading.get_ident(), view, selector))
        return CapturedInput({'local': {'answer': selector['n']}}, ((source_id, 'native-token'),), time.time())

    source_id = SourceId(scope, 'board.input', 'local')
    adapter = BoardInputSource(engine, source_id, capture, max_selectors=2)
    adapter.watch(woke.set)
    yield adapter, engine, calls, woke
    adapter.close()


def settle(adapter, woke):
    for _ in range(10):
        woke.clear()
        result = adapter.capture(None, CaptureBudget(max_seconds=2))
        assert result.next_deadline is not None
        assert woke.wait(2), 'native capture worker did not finish'
        adapter._worker.join(2)
        assert not adapter._worker.is_alive()
        result = adapter.capture(None, CaptureBudget(max_seconds=2))
        adapter.commit(result)
        if result.next_deadline > time.monotonic():
            return result
    pytest.fail('selector cycle did not settle')


def test_want_has_no_capture_and_worker_publishes_vector(source):
    adapter, engine, calls, woke = source
    assert adapter.want('board', {'n': 1})
    assert calls == []
    assert adapter.want('board', {'n': 1})
    assert adapter.want('board', {'n': 2})
    assert not adapter.want('board', {'n': 3})
    result = settle(adapter, woke)
    assert len(calls) == 2
    assert all(thread != threading.get_ident() for thread, _, _ in calls)
    fact = engine.store.snapshot(engine.scope).get(adapter.input_key('board', {'n': 1}))
    assert fact.health == 'fresh'
    assert fact.value['local']['answer'] == 1
    assert fact.source_vector == ((adapter.source, 'native-token'),)
    assert fact.valid_until > result.observed_at


def test_capture_failure_retains_stale_value_and_recovers(source):
    adapter, engine, _calls, woke = source
    adapter.want('board', {'n': 1})
    settle(adapter, woke)
    key = adapter.input_key('board', {'n': 1})
    old = engine.store.snapshot(engine.scope).get(key)
    original = adapter._capture

    def fail(*args):
        raise OSError('private source path must not be published')

    adapter._capture = fail
    settle(adapter, woke)
    stale = engine.store.snapshot(engine.scope).get(key)
    assert stale.value == old.value
    assert stale.health == 'stale'
    assert stale.error_code == 'board_capture_failed'
    assert 'private' not in repr(stale)
    adapter._capture = original
    settle(adapter, woke)
    assert engine.store.snapshot(engine.scope).get(key).health == 'fresh'


@pytest.mark.parametrize('warm', [False, True])
def test_repeated_capture_failure_preserves_evidence_health(source, warm):
    adapter, engine, _calls, woke = source
    adapter.want('board', {'n': 1})
    original = adapter._capture
    key = adapter.input_key('board', {'n': 1})
    if warm:
        settle(adapter, woke)
    previous = engine.store.snapshot(engine.scope).get(key)

    def fail(*args):
        raise OSError('controlled repeated failure')

    adapter._capture = fail
    for _ in range(3):
        settle(adapter, woke)
        # Both a failed detached capture and the scheduler failure callback
        # must preserve unknown until there has actually been a good capture.
        adapter.failure()
        fact = engine.store.snapshot(engine.scope).get(key)
        assert fact.health == ('stale' if warm else 'unknown')
        assert fact.error_code == 'board_capture_failed'
        if warm:
            assert fact.value == previous.value
            assert fact.observed_at == previous.observed_at
            assert fact.source_vector == previous.source_vector
    adapter._capture = original
    settle(adapter, woke)
    assert engine.store.snapshot(engine.scope).get(key).health == 'fresh'


def test_source_close_forbids_new_demand(source):
    adapter, _, _, _ = source
    adapter.close()
    assert not adapter.want('board', {'n': 1})


def test_busy_capture_is_not_replaced_and_close_retains_custody(source):
    adapter, engine, _calls, _woke = source
    entered, release = threading.Event(), threading.Event()
    original = adapter._capture

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    adapter._capture = blocked
    adapter.want('board', {'n': 1})
    adapter.capture(None, CaptureBudget(max_seconds=2))
    try:
        assert entered.wait(2)
        worker = adapter._worker
        adapter._deadline = time.monotonic() - 1
        failed = adapter.capture(None, CaptureBudget(max_seconds=2))
        assert failed.failed
        adapter.commit(failed)
        assert adapter.capture(None, CaptureBudget(max_seconds=2)).next_deadline is None
        assert adapter._worker is worker
        fact = engine.store.snapshot(engine.scope).get(adapter.input_key('board', {'n': 1}))
        assert fact.health == 'unknown'
        with pytest.raises(TimeoutError, match='custody retained'):
            adapter.close(timeout=0)
        assert worker.is_alive()
    finally:
        release.set()
        adapter.close(timeout=2)


def test_failed_selector_does_not_suppress_healthy_peer(source):
    adapter, engine, _calls, woke = source
    original = adapter._capture

    def capture(view, selector, budget):
        if selector['n'] == 1:
            raise OSError('one broken source')
        return original(view, selector, budget)

    adapter._capture = capture
    adapter.want('board', {'n': 1})
    adapter.want('board', {'n': 2})
    settle(adapter, woke)
    snapshot = engine.store.snapshot(engine.scope)
    assert snapshot.get(adapter.input_key('board', {'n': 1})).health == 'unknown'
    assert snapshot.get(adapter.input_key('board', {'n': 2})).health == 'fresh'


def test_oversize_native_evidence_never_publishes_truncated_success(source):
    adapter, engine, _calls, woke = source
    adapter.want('board', {'n': 1})
    woke.clear()
    adapter.capture(None, CaptureBudget(max_seconds=2, max_bytes=1))
    assert woke.wait(2)
    adapter._worker.join(2)
    result = adapter.capture(None, CaptureBudget(max_seconds=2, max_bytes=1))
    adapter.commit(result)
    fact = engine.store.snapshot(engine.scope).get(adapter.input_key('board', {'n': 1}))
    assert fact.health == 'unknown'
    assert fact.error_code == 'board_capture_failed'


@pytest.mark.parametrize('options', [{'max_selectors': 0}, {'refresh_seconds': 0},
                                    {'refresh_seconds': 60, 'valid_seconds': 60}])
def test_invalid_source_bounds_refused(source, options):
    from hermes_cli.fact_board_inputs import BoardInputSource
    adapter, engine, _calls, _woke = source
    with pytest.raises(ValueError, match='bounds'):
        BoardInputSource(engine, adapter.source, adapter._capture, **options)
