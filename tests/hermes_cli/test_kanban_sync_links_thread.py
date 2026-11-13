"""A slow chat-link pass never holds up sync rounds (hub 2026-11-13, t_01a1d2cc49634a78). [daveey]

Inline in the loop, one ~10 min links pass (04:14-04:24Z) held the replica pull, the remote-parent mirror and the
outbox push, so the replica went 13 min stale and hub cards waiting on worker cards did not promote.
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import kanban_sync_client as ksc


def _run_loop(monkeypatch, *, reconcile, sync_all_once, wait, keep_running):
    from hermes_cli import machine_metrics
    monkeypatch.setattr(ksc, "load_identity", lambda: SimpleNamespace(gateway="here", url="fake", key="k"))
    monkeypatch.setattr(ksc, "Hub", lambda ident: SimpleNamespace(report_metrics=lambda *a: None))
    monkeypatch.setattr(ksc.sync, "set_lease_client", lambda value: None)
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", Path("/nonexistent/kanban.db"))])
    monkeypatch.setattr(ksc, "_schema_ahead_boards", lambda boards: [])
    monkeypatch.setattr(ksc, "FeedWatcher", lambda *a, **k: SimpleNamespace(run=lambda: None))
    monkeypatch.setattr(ksc, "sync_all_once", sync_all_once)
    monkeypatch.setattr(ksc, "LINKS_EVERY_S", 0.0)
    monkeypatch.setattr(ksc, "reconcile_links", reconcile)
    monkeypatch.setattr(ksc, "wait_for_work", wait)
    monkeypatch.setattr(machine_metrics, "Reporter",
                        lambda *a: SimpleNamespace(report_due=lambda: None, pending_wake_in=lambda: None))
    ksc.run_forever(keep_running, interval=1)


def test_rounds_keep_running_while_a_links_pass_is_slow(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    calls: list[str] = []
    rounds: list[int] = []
    rounds_when_pass_returned: list[int] = []
    iterations = {"n": 0}

    def slow_reconcile(boards, gateway, **kwargs):
        calls.append(gateway)
        release.wait(5)  # base: the loop itself blocks here for the whole pass
        rounds_when_pass_returned.append(len(rounds))
        finished.set()

    def sync_all_once(*a, **k):
        rounds.append(1)
        return []

    def wait(*args):  # runs after the iteration asked for its links pass: no request follows the release
        iterations["n"] += 1
        if iterations["n"] == 6:
            release.set()

    _run_loop(monkeypatch, reconcile=slow_reconcile, sync_all_once=sync_all_once, wait=wait,
              keep_running=lambda: iterations["n"] < 6)
    assert finished.wait(10), "the links pass never finished"
    assert len(rounds) == 6, rounds
    assert rounds_when_pass_returned == [6], \
        f"rounds waited for the links pass: {rounds_when_pass_returned[0]} of 6 ran before it returned"
    assert calls == ["here"], f"passes piled up behind a running one: {calls}"


def test_a_failed_links_pass_does_not_stop_later_passes(monkeypatch):
    calls: list[int] = []
    iterations = {"n": 0}

    def flaky(boards, gateway, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    def wait(*args):
        iterations["n"] += 1
        assert ksc._LINKS_RUNNER is not None and ksc._LINKS_RUNNER.wait_idle(10)

    _run_loop(monkeypatch, reconcile=flaky, sync_all_once=lambda *a, **k: [], wait=wait,
              keep_running=lambda: iterations["n"] < 3)
    assert len(calls) == 3, calls
