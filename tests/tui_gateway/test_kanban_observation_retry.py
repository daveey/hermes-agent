"""Bound leader stalls and coalesce recovery without holding publication locks."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.tui_gateway.test_kanban_remote_observations_lock import NOW, _world
from tests.waiting import wait_until
from tui_gateway import kanban_remote_observations as obs


def test_wait_timeout_persists_without_waiting_for_stalled_leader(tmp_path, monkeypatch):
    replica, ledger, _ = _world(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = obs._persist
    calls, waits = [], []
    pending_init = obs._Pending.__init__

    class ExpiredEvent(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            assert timeout is not None and 10 <= timeout <= 120
            return False  # Deterministically expire the wait, leaving the leader running.

    def init(self, token):
        pending_init(self, token)
        self.done = ExpiredEvent()

    def persist(*args):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            assert release.wait(20)
        return original(*args)

    monkeypatch.setattr(obs._Pending, "__init__", init)
    monkeypatch.setattr(obs, "_persist", persist)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(obs.observe, replica, ledger, NOW)
        try:
            assert entered.wait(20)
            result = pool.submit(obs.observe, replica, ledger, NOW).result(20)
            assert len(result) == 20
            assert not leader.done()
            assert len(calls) == 2 and len(waits) == 1
        finally:
            release.set()
        assert leader.result(20) == result
    assert not obs._pending


def test_failed_leader_waiters_share_one_retry_pass(tmp_path, monkeypatch):
    replica, ledger, _ = _world(tmp_path)
    entered, fail, finish_retry = threading.Event(), threading.Event(), threading.Event()
    original, pending_init = obs._persist, obs._Pending.__init__
    calls, waiting = [], []
    followers = 5

    class CountedEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.append(self)
            return super().wait(timeout)

    def init(self, token):
        pending_init(self, token)
        self.done = CountedEvent()

    def persist(*args):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            assert fail.wait(20)
            raise RuntimeError("leader failed")
        assert finish_retry.wait(20)
        return original(*args)

    monkeypatch.setattr(obs._Pending, "__init__", init)
    monkeypatch.setattr(obs, "_persist", persist)
    with ThreadPoolExecutor(max_workers=followers + 1) as pool:
        leader = pool.submit(obs.observe, replica, ledger, NOW)
        try:
            assert entered.wait(20)
            futures = [pool.submit(obs.observe, replica, ledger, NOW) for _ in range(followers)]
            wait_until(lambda: len(waiting) == followers, what="all initial followers")
            fail.set()
            with pytest.raises(RuntimeError, match="leader failed"):
                leader.result(20)
            # Each follower either became a writer or joined the retry flight.
            wait_until(lambda: len(calls) + len(waiting) >= followers * 2 + 1,
                       what="all followers joined retry or persisted")
            assert len(calls) == 2
            assert len(waiting) == followers * 2 - 1
        finally:
            fail.set()
            finish_retry.set()
        results = [future.result(20) for future in futures]
        assert len(results[0]) == 20 and all(result == results[0] for result in results)
    assert not obs._pending
