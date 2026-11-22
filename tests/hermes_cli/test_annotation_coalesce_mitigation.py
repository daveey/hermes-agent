"""Deterministic event-time proof of the urgent holder floor."""
import threading
from types import SimpleNamespace

import pytest
from hermes_cli import annotation_event_loop as loop


@pytest.mark.parametrize('flag,gap', [('1', 3.0), ('0', .25)])
def test_flag(monkeypatch, flag, gap):
    monkeypatch.setenv('HERMES_ANNOTATION_COALESCE', flag)
    assert loop.holder_coalesce() == gap


@pytest.mark.parametrize('control', [False, True])
def test_hundred_events_one_second_and_idle(monkeypatch, control):
    clock = [100.0]
    monkeypatch.setattr(loop, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    stop = threading.Event()
    calls = []
    # Initial ownership seeds coverage once. No source work on maintenance ticks.
    schedule = [101 + i / 100 for i in range(100)]
    class Events:
        control_only = False
        def wait(self, timeout):
            end = clock[0] + timeout
            if schedule and schedule[0] <= end:
                clock[0] = schedule.pop(0)
                self.control_only = control
                return True
            clock[0] = end
            if clock[0] >= 110:
                stop.set()
            return False
    loop.run(Events(), stop, SimpleNamespace(held=lambda: True, beat=lambda: None),
             lambda: calls.append(clock[0]), coalesce=3.0, floor_all=True)
    assert calls == [100.0, 103.0]
    assert len([t for t in calls if t >= 101]) <= 2
    assert not [t for t in calls if t > 103], 'no periodic input reads after the burst'


def test_quiet_after_ownership_seed_has_zero_runs(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(loop, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    stop = threading.Event()
    calls = []
    class Events:
        def wait(self, timeout):
            clock[0] += timeout
            if clock[0] >= 120:
                stop.set()
            return False
    loop.run(Events(), stop, SimpleNamespace(held=lambda: True, beat=lambda: None),
             lambda: calls.append(clock[0]), coalesce=3.0, floor_all=True)
    assert calls == [100.0]  # explicit ownership bootstrap, zero event-free refreshes
