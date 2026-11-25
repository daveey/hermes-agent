"""The sync loop's wait tick: how often an idle loop looks at its outboxes, and what must not wait for it. [daveey]"""
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_backlog as backlog
from hermes_cli import kanban_sync_client as client


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class _Wake:
    """An event on a simulated clock: each wait records its timeout and sleeps it out, unless told to fire."""

    def __init__(self, clock, fire_on=None):
        self.clock, self.fire_on = clock, fire_on
        self.timeouts, self.cleared = [], 0

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if self.fire_on == len(self.timeouts):
            return True
        self.clock.now += timeout
        return False

    def clear(self):
        self.cleared += 1


@pytest.fixture
def loop(monkeypatch):
    clock = _Clock()
    probes = []
    monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=clock.monotonic))
    state = SimpleNamespace(clock=clock, probes=probes, pending_on=None)

    def pending(paths):
        probes.append(clock.now)
        return state.pending_on == len(probes)

    monkeypatch.setattr(client, "outbox_pending", pending)
    return state


def _limited(n):
    left = [n]

    def keep_running():
        left[0] -= 1
        return left[0] >= 0
    return keep_running


def test_idle_wait_looks_at_the_outboxes_once_a_second(loop):
    wake = _Wake(loop.clock)
    assert client.wait_for_work(wake, [], 60.0, _limited(3)) == "stopped"
    assert wake.timeouts == [1.0, 1.0, 1.0]
    assert loop.probes == [1001.0, 1002.0, 1003.0]


def test_remote_wake_returns_at_once_without_a_probe(loop):
    wake = _Wake(loop.clock, fire_on=1)
    assert client.wait_for_work(wake, [], 60.0, lambda: True) == "remote"
    assert loop.clock.now == 1000.0 and len(wake.timeouts) == 1
    assert wake.cleared == 1 and loop.probes == []


def test_remote_wake_interrupts_a_tick_in_real_time():
    wake = threading.Event()
    timer = threading.Timer(0.05, wake.set)
    timer.start()
    try:
        started = client.time.monotonic()
        assert client.wait_for_work(wake, [], 30.0, lambda: True) == "remote"
        assert client.time.monotonic() - started < 0.9  # well inside one tick: the wake cut the wait short
    finally:
        timer.cancel()
        timer.join()


def test_stopped_loop_does_not_wait(loop):
    wake = _Wake(loop.clock)
    assert client.wait_for_work(wake, [], 60.0, lambda: False) == "stopped"
    assert wake.timeouts == [] and loop.probes == []


def test_stop_is_seen_within_one_tick(loop):
    wake = _Wake(loop.clock)
    assert client.wait_for_work(wake, [], 60.0, _limited(1)) == "stopped"
    assert wake.timeouts == [1.0] and loop.clock.now == 1001.0


def test_local_write_is_picked_up_within_one_tick(loop):
    loop.pending_on = 1
    wake = _Wake(loop.clock)
    assert client.wait_for_work(wake, [], 60.0, lambda: True) == "local"
    assert wake.timeouts == [1.0] and loop.clock.now - 1000.0 <= 1.0


def test_a_shorter_period_is_not_stretched_to_a_tick(loop):
    wake = _Wake(loop.clock)
    assert client.wait_for_work(wake, [], backlog.BUSY_RETRY_S, lambda: True) == "period"
    assert wake.timeouts == [backlog.BUSY_RETRY_S] and loop.clock.now == 1000.0 + backlog.BUSY_RETRY_S
    wake = _Wake(loop.clock)
    assert client.wait_for_work(wake, [], 0.0, lambda: True) == "period"
    assert wake.timeouts == [0.0]
