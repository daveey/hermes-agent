"""Exercise the real-thread handoff used by the consecutive rewind fixture."""

import threading

import pytest

from tests.tui_gateway.test_tui_gateway_server import _join_turn_thread


def test_join_waits_for_published_worker_start(monkeypatch):
    worker = threading.Thread(target=lambda: None)
    sess = {"_run_thread": worker}
    observed = threading.Event()
    original_wait = worker._started.wait

    def observe_wait(timeout=None):
        observed.set()
        return original_wait(timeout)

    monkeypatch.setattr(worker._started, "wait", observe_wait)
    errors = []

    def join():
        try:
            _join_turn_thread(sess)
        except (AssertionError, RuntimeError) as exc:
            errors.append(exc)
        finally:
            observed.set()

    waiter = threading.Thread(target=join)
    waiter.start()
    try:
        assert observed.wait(10), "joiner never observed the published worker"
        assert not errors, errors
    finally:
        worker.start()
        worker.join(10)
        waiter.join(10)
    assert not waiter.is_alive()
    assert not errors, errors


def test_join_rejects_worker_that_never_starts():
    with pytest.raises(AssertionError, match="did not start"):
        _join_turn_thread({"_run_thread": threading.Thread()}, timeout=0)
