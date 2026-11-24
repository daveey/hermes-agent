"""Deterministic trailing-read and source re-arm transitions in the real loop."""
from types import SimpleNamespace

from hermes_cli import annotation_event_loop as loop


class Clock:
    """Advance only at waits, with the same clock for deadlines and coalescing."""

    def __init__(self):
        self.now = 0.0
        self.stopped = False

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def wait(self, timeout):
        self.now += timeout
        return self.stopped

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True


def test_deferred_gap_reads_lone_trailing_write(monkeypatch, tmp_path):
    """No lease beat, provider deadline or later event can rescue the final read."""
    clock = Clock()
    monkeypatch.setattr(loop, 'time', clock)
    path = tmp_path / 'source'
    path.write_text('initial')
    writes = [(1.0, 'first'), (1.05, 'second')]
    reads = []

    class Events:
        control_only = False

        def wait(self, timeout):
            # The half-second observation bound also terminates the mutant that
            # waits until the 30 s lease beat instead of the coalescing deadline.
            until = min(clock.now + timeout, 1.55)
            if writes and writes[0][0] <= until:
                clock.now, value = writes.pop(0)
                path.write_text(value)
                self.control_only = False
                return True
            clock.now = until
            if clock.now >= 1.55:
                clock.set()
            return False

    def refresh():
        reads.append((clock.now, path.read_text()))

    lease = SimpleNamespace(held=lambda: True, beat=lambda: None)
    loop.run(Events(), clock, lease, refresh, interval=30)

    assert reads[1] == (1.0, 'first'), 'first write after quiet must read immediately'
    assert [value for _, value in reads] == ['initial', 'first', 'second']
    assert 1.05 < reads[2][0] <= 1.55, 'lone trailing write must read within 0.5 s'
    assert not writes


def test_ready_source_is_not_redrained_during_coalesce(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(loop, 'time', clock)
    reads, waits, beats = [], [], []

    class Events:
        control_only = False

        def wait(self, timeout):
            if clock.is_set():
                return False
            assert not waits or clock.now > waits[-1], 'ready source redrained without blocking'
            waits.append(clock.now)
            if clock.now == 0:
                clock.now = .1
            return True

    def refresh():
        reads.append(clock.now)
        if len(reads) == 2:
            clock.set()

    loop.run(Events(), clock, SimpleNamespace(held=lambda: True, beat=lambda: beats.append(clock.now)),
             refresh, interval=1, coalesce=3, floor_all=True)
    assert reads == [0, 3]
    assert waits == [0, 1, 2, 3]
    assert beats == [0, 1, 2, 3], 'coalescing must preserve lease maintenance'


def test_control_then_source_during_floor_keeps_source_classification(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(loop, 'time', clock)
    seen = []

    class Events:
        control_only = False

        def wait(self, timeout):
            if clock.is_set():
                return False
            if clock.now == 0:
                clock.now = .1
                self.control_only = True
                return True
            assert clock.now == 1, 'source hint should be drained at the lease boundary'
            self.control_only = False
            return True

    events = Events()

    def refresh():
        seen.append(events.control_only)
        if len(seen) == 2:
            clock.set()

    loop.run(events, clock, SimpleNamespace(held=lambda: True, beat=lambda: None),
             refresh, interval=1, coalesce=1, floor_all=True)
    assert seen == [False, False], 'queued source invalidation must not become a control-only read'


def test_stop_during_floor_does_not_drain_or_refresh(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(loop, 'time', clock)
    reads = []

    def stopped_wait(timeout):
        clock.set()
        return True

    clock.wait = stopped_wait

    class Events:
        control_only = False

        def wait(self, timeout):
            assert clock.now == 0, 'stop must not drain after the coalesced wait'
            clock.now = .1
            return True

    loop.run(Events(), clock, SimpleNamespace(held=lambda: True, beat=lambda: None),
             lambda: reads.append(clock.now), interval=1, coalesce=3, floor_all=True)
    assert reads == [0]


def test_failed_wait_rearms_with_source_refresh(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(loop, 'time', clock)
    seen = []

    class Events:
        control_only = False
        calls = 0

        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                self.control_only = True
                raise OSError('notification descriptor unavailable')
            clock.set()  # bounded even if the recovery refresh is omitted
            return False

    events = Events()

    def refresh():
        seen.append(events.control_only)

    lease = SimpleNamespace(held=lambda: True, beat=lambda: None)
    loop.run(events, clock, lease, refresh, interval=1)

    assert events.calls == 2
    assert seen == [False, False], 'wait failure must reinstall source coverage'


def test_regained_ownership_rearms_with_source_refresh(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(loop, 'time', clock)
    seen, held_calls = [], []

    class Events:
        control_only = False

        def wait(self, timeout):
            self.control_only = True  # no source notification during lease loss
            clock.wait(timeout)
            if clock.now >= 4:
                clock.set()  # bounded even if reacquisition never refreshes
            return False

    events = Events()

    def held():
        held_calls.append(clock.now)
        return len(held_calls) != 2  # owned, lost, regained

    def refresh():
        seen.append((len(held_calls), events.control_only))
        if len(seen) == 2:
            clock.set()

    lease = SimpleNamespace(held=held, beat=lambda: None)
    loop.run(events, clock, lease, refresh, interval=1)

    assert held_calls == [0.0, 1.0, 2.0]
    assert seen == [(1, False), (3, False)], 'new ownership must reinstall source coverage'
