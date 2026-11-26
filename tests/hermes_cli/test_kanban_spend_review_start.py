"""Startup failure must not strand reviewer capacity or release live custody."""
from __future__ import annotations

import contextvars
import threading
import types
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli.kanban_dispatch_storage import DispatchWriteContended
from tests.hermes_cli import test_kanban_spend_reviewer as helpers

kanban_home = helpers.kanban_home


@pytest.fixture(autouse=True)
def isolate_registry(monkeypatch):
    monkeypatch.setattr(reviewer, "_INFLIGHT", set())
    monkeypatch.setattr(reviewer, "_THREADS", [])


@pytest.mark.parametrize("failure", ["write", "construct", "start", "late_start"])
def test_failed_start_releases_slot_and_can_retry(kanban_home, spend_reviewer, monkeypatch, failure):
    calls = []
    delayed = []
    real_txn = kb.write_txn

    @contextmanager
    def contended(conn):
        raise DispatchWriteContended()
        yield  # pragma: no cover

    class Broken:
        def __init__(self, target, args, **kwargs):
            if failure == "construct":
                raise RuntimeError("construction failed")
            self.call = lambda: target(*args)

        def start(self):
            if failure == "late_start":
                delayed.append(self.call)
            raise RuntimeError("start failed")

        def is_alive(self):
            return False

    with kbc.connect_closing() as conn:
        tid, episode = helpers._stopped(conn)
        assert reviewer.state(conn, tid, episode) == reviewer.QUEUED
        now = int(reviewer.time.time())
        assert reviewer._due(conn, now, set()) == [(tid, episode, 0)]
        monkeypatch.setattr(reviewer, "queue_backlog", lambda *a, **kw: None)
        monkeypatch.setattr(reviewer, "_run", lambda *a: calls.append(a))
        monkeypatch.setattr(reviewer, "threading", types.SimpleNamespace(Thread=Broken))
        if failure == "write":
            monkeypatch.setattr(kb, "write_txn", contended)
        with pytest.raises((DispatchWriteContended, RuntimeError)):
            reviewer.start_due(conn, now=now)
        key = (reviewer._db_path(conn), tid)
        assert key not in reviewer._INFLIGHT
        assert not reviewer._THREADS
        assert not calls
        monkeypatch.setattr(kb, "write_txn", real_txn)
        monkeypatch.setattr(reviewer, "threading", types.SimpleNamespace(Thread=helpers._Inline))
        assert reviewer.start_due(conn, now=now + reviewer.LEASE_SECONDS + 1) == [tid]
        assert len(calls) == 1
        for call in delayed:
            call()  # an uncertain native start must not run after cancellation
        assert len(calls) == 1
        assert key in reviewer._INFLIGHT  # late canceled target cannot discard successor custody


def test_exception_after_target_entry_retains_live_custody(kanban_home, spend_reviewer, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    threads = []
    marker = contextvars.ContextVar("review-start-test", default="missing")
    observed = []

    def run(db, tid, episode):
        observed.append(marker.get())
        entered.set()
        try:
            assert finish.wait(5)
        finally:
            with reviewer._LOCK:
                reviewer._INFLIGHT.discard((db, tid))

    class StartedThenRaised(threading.Thread):
        def start(self):
            super().start()
            threads.append(self)
            assert entered.wait(5)
            raise RuntimeError("interrupted after target entered")

    monkeypatch.setattr(reviewer, "_run", run)
    monkeypatch.setattr(reviewer, "threading", types.SimpleNamespace(Thread=StartedThenRaised))
    with kbc.connect_closing() as conn:
        tid, _ = helpers._stopped(conn)
        key = (reviewer._db_path(conn), tid)
        token = marker.set("owning profile")
        try:
            with pytest.raises(RuntimeError):
                reviewer.start_due(conn)
            assert key in reviewer._INFLIGHT
            assert threads[0] in reviewer._THREADS
            assert observed == ["owning profile"]
            assert reviewer.start_due(conn, now=int(reviewer.time.time()) + reviewer.LEASE_SECONDS + 1) == []
        finally:
            marker.reset(token)
            finish.set()
            for thread in threads:
                thread.join(5)
        assert key not in reviewer._INFLIGHT
        assert not threads[0].is_alive()
