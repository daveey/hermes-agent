"""Deadline-driven refresh admission, with no wall-clock sleeps."""
from contextvars import ContextVar
import threading

from hermes_cli.kanban_links_refresh import ScopeRefresher
from tests.waiting import wait_until


class ClockCondition(threading.Condition):
    """Expose timed waits; tests advance the clock instead of waiting three seconds."""

    def __init__(self):
        super().__init__()
        self.now = 100.0
        self.waits = []
        self.waiting = threading.Event()

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.waiting.set()
        return super().wait()  # advance() delivers the deadline wake deterministically

    def advance(self, seconds):
        with self:
            self.now += seconds
            self.waiting.clear()
            self.notify_all()
        assert self.waiting.wait(5)


def owner_for(refresh, capacity=4):
    condition = ClockCondition()
    owner = ScopeRefresher(refresh, capacity, clock=lambda: condition.now)
    owner._ready = condition
    return owner, condition


def settle(owner, condition):
    wait_until(lambda: not owner.busy, timeout=5)
    assert condition.waiting.wait(5)


def test_completed_polls_coalesce_until_floor_with_first_context_and_no_poll_to_wake():
    origin = ContextVar('coalescing_origin', default='outside')
    calls = []
    owner, condition = owner_for(lambda scope: calls.append((scope, condition.now, origin.get())))
    scope = ('A',)
    owner.submit(scope)
    settle(owner, condition)
    origin.set('trailing')
    owner.submit(scope)
    with owner._ready:
        for _ in range(50):
            origin.set('later')
            owner.submit(scope)
    condition.advance(2.999)
    assert len(calls) == 1
    assert owner.busy
    assert 0 < condition.waits[-1] < 0.002
    condition.advance(0.001)
    settle(owner, condition)
    assert calls == [(scope, 100.0, 'outside'), (scope, 103.0, 'trailing')]
    assert condition.waits[-1] is None


def test_unrelated_scope_is_not_blocked_behind_cooling_scope():
    calls = []
    owner, condition = owner_for(calls.append)
    owner.submit(('A',))
    settle(owner, condition)
    owner.submit(('A',))
    owner.submit(('B',))
    wait_until(lambda: len(calls) == 2, timeout=5)
    assert calls == [('A',), ('B',)]
    owner.drain(('A',), timeout=0)
    settle(owner, condition)


def test_cooldown_state_is_bounded_without_eviction_bypassing_floor():
    calls = []
    owner, condition = owner_for(calls.append, capacity=2)
    for scope in [('A',), ('B',)]:
        owner.submit(scope)
        settle(owner, condition)
    owner.submit(('C',))
    owner.submit(('A',))
    owner.submit(('overflow',))
    condition.advance(2)
    assert calls == [('A',), ('B',)]
    assert len(owner._next_start) == len(owner._pending) == 2
    condition.advance(1)
    settle(owner, condition)
    assert calls == [('A',), ('B',), ('C',), ('A',)]
    assert len(owner._next_start) <= 2
    condition.advance(3)
    assert owner._next_start == {}


def test_drain_cancels_trailing_context_and_resume_preserves_cooldown():
    calls = []
    owner, condition = owner_for(calls.append)
    scope = ('A',)
    owner.submit(scope)
    settle(owner, condition)
    owner.submit(scope)
    owner.drain(scope, timeout=0)
    owner.submit(scope)
    assert not owner.busy
    owner.resume(scope)
    owner.submit(scope)
    condition.advance(2)
    assert calls == [scope]
    condition.advance(1)
    settle(owner, condition)
    assert calls == [scope, scope]


def test_failure_retries_only_at_floor_and_worker_survives():
    calls = []

    def refresh(scope):
        calls.append(scope)
        if len(calls) == 1:
            raise RuntimeError('controlled refresh failure')

    owner, condition = owner_for(refresh)
    owner.submit(('A',))
    settle(owner, condition)
    worker = owner._worker
    owner.submit(('A',))
    condition.advance(2)
    assert calls == [('A',)]
    condition.advance(1)
    settle(owner, condition)
    assert calls == [('A',), ('A',)]
    assert owner._worker is worker


def test_floor_is_from_start_not_completion_and_active_polls_still_coalesce():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def refresh(scope):
        calls.append(scope)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)

    owner, condition = owner_for(refresh)
    owner.submit(('A',))
    assert entered.wait(5)
    try:
        for _ in range(20):
            owner.submit(('A',))
        with condition:
            condition.now += 4
    finally:
        release.set()
    settle(owner, condition)
    assert calls == [('A',)]
    owner.submit(('A',))
    settle(owner, condition)
    assert calls == [('A',), ('A',)]
