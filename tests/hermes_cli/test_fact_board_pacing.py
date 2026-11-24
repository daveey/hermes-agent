"""Board evidence capture pacing and budgets (hub 2026-11-24, ``hermes_cli.fact_board_inputs``). [daveey]

Every write to the board file re-queued every view, the one shared capture worker never idled, and in the
contended dashboard backend each capture overran the owner's 10 s budget (also while still queued), so the board
route answered ``503 dependency_unavailable`` instead of the board.
"""
import threading
import time

import pytest

from hermes_cli import fact_capture_queue
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore


class _Clock:
    """The board source's monotonic clock, advanced by the test: the delays are the behaviour under test."""

    def __init__(self):
        self.now, self.wall = 1000.0, time.time()

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds

    def time(self):
        return self.wall


@pytest.fixture
def clock(monkeypatch):
    from hermes_cli import fact_board_inputs
    fake = _Clock()
    monkeypatch.setattr(fact_board_inputs, 'time', fake)
    return fake


def _adapter(capture, **options):
    from hermes_cli.fact_board_inputs import BoardInputSource
    scope = Scope('installation', 'default', 'main')
    engine = ReducerEngine(FactStore(), scope)
    woke = threading.Event()
    adapter = BoardInputSource(engine, SourceId(scope, 'board.input', 'local'), capture, max_selectors=4, **options)
    adapter.watch(woke.set)
    return adapter, engine, woke


def _captured(selector):
    from hermes_cli.fact_board_inputs import CapturedInput
    return CapturedInput({'local': {'answer': selector['n']}}, (), time.time())


def _run_once(adapter, woke, budget):
    """Start one capture, let it finish, commit its result; returns the capture that started it."""
    woke.clear()
    started = adapter.capture(None, budget)
    assert woke.wait(5), 'capture did not finish'
    adapter._worker.join(5)
    adapter.commit(adapter.capture(None, budget))
    return started


def test_writes_inside_the_gap_coalesce_into_one_capture(clock):
    calls = []

    def capture(view, selector, budget):
        calls.append(clock.monotonic())
        return _captured(selector)

    adapter, _engine, woke = _adapter(capture, min_gap_seconds=0.5)
    try:
        adapter.want('board', {'n': 1})
        _run_once(adapter, woke, CaptureBudget(max_seconds=2))
        assert len(calls) == 1
        for _ in range(5):  # a burst of board writes
            adapter._invalidate()
        held = adapter.capture(None, CaptureBudget(max_seconds=2))
        assert held.key is None and held.next_deadline == calls[0] + 0.5  # due after the gap, nothing started
        assert len(calls) == 1
        clock.advance(0.4)
        assert adapter.capture(None, CaptureBudget(max_seconds=2)).key is None and len(calls) == 1
        clock.advance(0.1)
        _run_once(adapter, woke, CaptureBudget(max_seconds=2))
        assert len(calls) == 2 and calls[1] - calls[0] >= 0.5  # one capture for the whole burst
        assert adapter.capture(None, CaptureBudget(max_seconds=2)).next_deadline > clock.monotonic()
        assert len(calls) == 2
    finally:
        adapter.close()


def test_a_throttled_selector_does_not_hold_back_a_due_peer(clock):
    calls = []

    def capture(view, selector, budget):
        calls.append(selector['n'])
        return _captured(selector)

    adapter, _engine, woke = _adapter(capture, min_gap_seconds=30 - 1)
    try:
        adapter.want('board', {'n': 1})
        _run_once(adapter, woke, CaptureBudget(max_seconds=2))
        adapter.want('board', {'n': 2})  # new demand behind a just-captured selector
        adapter._invalidate()
        _run_once(adapter, woke, CaptureBudget(max_seconds=2))
        assert calls == [1, 2]
    finally:
        adapter.close()


def test_a_capture_queued_behind_another_board_does_not_spend_its_budget(clock):
    """The budget runs from the capture's own start, not from submission to the shared worker."""
    release = threading.Event()
    blocker = fact_capture_queue.submit(lambda: release.wait(5))  # another board's capture holds the worker
    adapter, engine, woke = _adapter(lambda view, selector, budget: _captured(selector))
    try:
        adapter.want('board', {'n': 1})
        woke.clear()  # want() wakes the owner; wait for the capture's own wake below
        budget = CaptureBudget(max_seconds=0.1)
        adapter.capture(None, budget)
        clock.advance(0.3)  # longer than the budget, all of it queued
        queued = adapter.capture(None, budget)
        assert not queued.failed and queued.key is None
        assert queued.next_deadline == clock.monotonic() + 1  # polled again: its deadline arms when it starts
        release.set()
        blocker.join(5)
        assert woke.wait(5)
        adapter._worker.join(5)
        result = adapter.capture(None, budget)
        assert not result.failed
        adapter.commit(result)
        fact = engine.store.snapshot(engine.scope).get(adapter.input_key('board', {'n': 1}))
        assert fact.health == 'fresh'
    finally:
        release.set()
        adapter.close()


def test_a_capture_slower_than_the_owner_budget_still_publishes_fresh_evidence(clock):
    """The owner's 10 s budget failed every contended board capture, even finished ones; the board has its own."""
    def slow(view, selector, budget):
        clock.advance(0.3)  # the capture's own duration
        return _captured(selector)

    adapter, engine, woke = _adapter(slow, max_capture_seconds=5)
    try:
        adapter.want('board', {'n': 1})
        _run_once(adapter, woke, CaptureBudget(max_seconds=0.1))
        fact = engine.store.snapshot(engine.scope).get(adapter.input_key('board', {'n': 1}))
        assert fact.health == 'fresh' and fact.value['local']['answer'] == 1
    finally:
        adapter.close()


def test_a_capture_over_its_own_limit_still_fails_closed(clock):
    def slow(view, selector, budget):
        clock.advance(0.3)  # the capture's own duration
        return _captured(selector)

    adapter, engine, woke = _adapter(slow, max_capture_seconds=0.1)
    try:
        adapter.want('board', {'n': 1})
        _run_once(adapter, woke, CaptureBudget(max_seconds=10))
        fact = engine.store.snapshot(engine.scope).get(adapter.input_key('board', {'n': 1}))
        assert fact.error_code == 'board_capture_failed'
    finally:
        adapter.close()


def test_a_capture_still_running_past_its_own_limit_is_reported_failed(clock):
    """The deadline is armed when the capture starts: past it, the owner hears a failure while the worker runs."""
    entered, release = threading.Event(), threading.Event()

    def blocked(view, selector, budget):
        entered.set()
        release.wait(5)
        return _captured(selector)

    adapter, engine, _woke = _adapter(blocked, max_capture_seconds=0.2)
    try:
        adapter.want('board', {'n': 1})
        adapter.capture(None, CaptureBudget(max_seconds=10))
        assert entered.wait(5)
        clock.advance(0.3)
        result = adapter.capture(None, CaptureBudget(max_seconds=10))
        assert result.failed and adapter._worker.is_alive()
        adapter.commit(result)
        assert engine.store.snapshot(engine.scope).get(adapter.input_key('board', {'n': 1})).error_code == \
            'board_capture_failed'
    finally:
        release.set()
        adapter.close(timeout=5)


def test_a_finished_capture_disarms_its_deadline_with_its_result(clock):
    """The queue marks the job done after _build returns; a capture() in between must not see a running deadline."""
    adapter, _engine, woke = _adapter(lambda view, selector, budget: _captured(selector))
    try:
        adapter.want('board', {'n': 1})
        woke.clear()
        adapter.capture(None, CaptureBudget(max_seconds=2))
        assert woke.wait(5)
        assert adapter._deadline is None and adapter._began is None
    finally:
        adapter.close()


def test_the_next_refresh_is_timed_from_the_evidence_observation(clock):
    """Slow captures: refresh by the evidence's age, so its validity cannot lapse before the next one lands."""
    from hermes_cli.fact_board_inputs import CapturedInput

    def aged(view, selector, budget):
        return CapturedInput({'local': {'answer': 1}}, (), clock.time() - 20)  # observed 20 s before it landed

    adapter, _engine, woke = _adapter(aged)
    try:
        adapter.want('board', {'n': 1})
        woke.clear()
        adapter.capture(None, CaptureBudget(max_seconds=2))
        assert woke.wait(5)
        adapter._worker.join(5)
        result = adapter.capture(None, CaptureBudget(max_seconds=2))
        assert result.next_deadline == pytest.approx(clock.monotonic() + 30 - 20)
    finally:
        adapter.close()


def test_the_gap_runs_from_the_capture_start_not_its_queueing(clock):
    release = threading.Event()
    blocker = fact_capture_queue.submit(lambda: release.wait(5))
    adapter, _engine, woke = _adapter(lambda view, selector, budget: _captured(selector), min_gap_seconds=5)
    try:
        adapter.want('board', {'n': 1})
        woke.clear()
        adapter.capture(None, CaptureBudget(max_seconds=2))
        clock.advance(3)  # queued for 3 s
        release.set()
        blocker.join(5)
        assert woke.wait(5)
        adapter._worker.join(5)
        adapter.commit(adapter.capture(None, CaptureBudget(max_seconds=2)))
        adapter._invalidate()
        assert adapter.capture(None, CaptureBudget(max_seconds=2)).next_deadline == pytest.approx(1000 + 3 + 5)
    finally:
        release.set()
        adapter.close()


def test_every_view_owner_paces_its_views():
    """The project tree's views run on the same source as the board's."""
    from hermes_cli.fact_view_owner import ViewOwner
    owner = ViewOwner(Scope('installation', 'default', 'tree'), lambda *a: None, lambda *a: None, ('tree',))
    assert owner.source._gap == 5 and owner.source._capture_seconds == 30


@pytest.mark.parametrize('options', [{'min_gap_seconds': -1}, {'min_gap_seconds': 30}, {'max_capture_seconds': 0},
                                     {'max_capture_seconds': 60}])
def test_invalid_pacing_bounds_refused(options):
    with pytest.raises(ValueError, match='bounds'):
        _adapter(lambda *a: None, **options)
