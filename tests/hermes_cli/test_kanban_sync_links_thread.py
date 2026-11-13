"""A slow chat-link pass never holds up sync rounds (hub 2026-11-13, t_01a1d2cc49634a78). [daveey]

Inline in the loop, one ~10 min links pass (04:14-04:24Z) held the replica pull, the remote-parent mirror and the
outbox push, so the replica went 13 min stale and hub cards waiting on worker cards did not promote.
"""
from __future__ import annotations

import logging
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
    assert ksc._LINKS_RUNNER is None, "the stopped loop's runner is still published"


def test_a_restarted_loop_never_runs_a_pass_beside_the_stopped_loops(monkeypatch):
    """``stop`` waits only ``JOIN_S``: a slow pass outlives its loop. A same-process restart must not start a second
    ``reconcile_links`` beside it (both share ``_LINKS_DONE``; review of PR #1882)."""
    from hermes_cli.kanban_sync_links_pass import LinksPass
    monkeypatch.setattr(LinksPass, "JOIN_S", 0.05)
    release, entered = threading.Event(), threading.Event()
    lock = threading.Lock()
    state = {"active": 0, "max": 0}
    calls: list[threading.Thread] = []

    def reconcile(boards, gateway, **kwargs):
        with lock:
            calls.append(threading.current_thread())
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        entered.set()
        try:
            release.wait(10)
        finally:
            with lock:
                state["active"] -= 1

    first = {"n": 0}

    def first_wait(*args):
        first["n"] += 1
        assert entered.wait(10), "the first loop's pass never started"

    _run_loop(monkeypatch, reconcile=reconcile, sync_all_once=lambda *a, **k: [], wait=first_wait,
              keep_running=lambda: first["n"] < 2)
    assert len(calls) == 1 and calls[0].is_alive() and state["active"] == 1, "the first pass must outlive its loop"
    slow = calls[0]

    second = {"n": 0, "calls_while_blocked": None}

    def second_wait(*args):
        second["n"] += 1
        if second["n"] == 3:  # three requests refused while the stopped loop's pass ran
            second["calls_while_blocked"] = len(calls)
            release.set()
            slow.join(10)
        elif second["n"] > 3:
            assert ksc._LINKS_RUNNER.wait_idle(10)

    _run_loop(monkeypatch, reconcile=reconcile, sync_all_once=lambda *a, **k: [], wait=second_wait,
              keep_running=lambda: len(calls) < 2 and second["n"] < 10)
    release.set()
    assert second["calls_while_blocked"] == 1, f"the restarted loop ran a pass beside the stopped one: {calls}"
    assert state["max"] == 1, f"{state['max']} concurrent reconcile_links passes"
    assert len(calls) == 2 and calls[1] is not slow, "the restarted loop never ran its own pass"


def test_a_pass_refused_behind_a_stopped_loops_pass_warns_once(monkeypatch, caplog):
    """A stopped runner's hung ``reconcile_links`` refuses every later pass in the process: say so once per hold
    rather than refusing silently (review nit on PR #2006)."""
    from hermes_cli.kanban_sync_links_pass import LinksPass
    monkeypatch.setattr(LinksPass, "JOIN_S", 0.05)
    release, entered = threading.Event(), threading.Event()
    calls: list[threading.Thread] = []

    def reconcile(boards, gateway, **kwargs):
        calls.append(threading.current_thread())
        entered.set()
        release.wait(10)

    first = {"n": 0}

    def first_wait(*args):
        first["n"] += 1
        assert entered.wait(10), "the first loop's pass never started"

    try:
        _run_loop(monkeypatch, reconcile=reconcile, sync_all_once=lambda *a, **k: [], wait=first_wait,
                  keep_running=lambda: first["n"] < 1)
        assert len(calls) == 1 and calls[0].is_alive(), "the first pass must outlive its loop"
        caplog.clear()
        second = {"n": 0}

        def second_wait(*args):
            second["n"] += 1

        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_sync_client"):
            _run_loop(monkeypatch, reconcile=reconcile, sync_all_once=lambda *a, **k: [], wait=second_wait,
                      keep_running=lambda: second["n"] < 2)  # two iterations: two refused requests
        assert second["n"] == 2 and len(calls) == 1, f"the restarted loop's requests were not refused: {calls}"
        held = [r for r in caplog.records if "held the pass lock" in r.getMessage()]
        assert len(held) == 1, f"expected one warning for the hold, got {[r.getMessage() for r in held]}"
        assert held[0].levelno == logging.WARNING
    finally:
        release.set()
        for thread in calls:
            thread.join(10)


class _HeldPass:
    """A runner whose ``reconcile_links`` blocks until released, so it holds ``_PASS_LOCK`` while a test refuses
    another runner's requests."""

    def __init__(self, monkeypatch):
        self.entered, self.release = threading.Event(), threading.Event()
        monkeypatch.setattr(ksc, "reconcile_links", self._reconcile)
        from hermes_cli.kanban_sync_links_pass import LinksPass
        self.runner = LinksPass("holder")

    def _reconcile(self, boards, gateway, **kwargs):
        self.entered.set()
        self.release.wait(10)

    def hold(self):
        self.entered.clear()
        self.release.clear()
        assert self.runner.request([("default", Path("/nonexistent/kanban.db"))]), "the holder's pass did not start"
        assert self.entered.wait(10), "the holder's pass never entered reconcile_links"

    def drop(self):
        self.release.set()
        assert self.runner.wait_idle(10), "the holder's pass never returned"

    def close(self):
        self.release.set()
        self.runner.stop()


def _held_warnings(caplog):
    return [r.getMessage() for r in caplog.records if "held the pass lock" in r.getMessage()]


def test_a_new_hold_warns_again_after_the_first_one_ends(monkeypatch, caplog):
    """The quiet-after-warning state is per hold: once a wedged pass returns, the next pass that holds the lock
    warns again (review nit on PR #2073)."""
    from hermes_cli.kanban_sync_links_pass import LinksPass
    held, refused = _HeldPass(monkeypatch), LinksPass("refused")
    boards = [("default", Path("/nonexistent/kanban.db"))]
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_sync_client"):
            held.hold()
            assert not refused.request(boards) and not refused.request(boards)
            assert len(_held_warnings(caplog)) == 1, _held_warnings(caplog)
            held.drop()
            held.hold()
            assert not refused.request(boards)
        assert len(_held_warnings(caplog)) == 2, f"the second hold did not warn: {_held_warnings(caplog)}"
    finally:
        held.close()


def test_a_hold_past_the_threshold_warns_again_naming_its_duration(monkeypatch, caplog):
    """One warning per hold logs a pass wedged for hours once, near 0 s: past ``REWARN_S`` the same hold warns
    again, naming how long it has held the lock, then at most once per interval (review nit on PR #2073)."""
    from hermes_cli import kanban_sync_links_pass as lp
    now = {"t": 1000.0}
    monkeypatch.setattr(lp, "_clock", lambda: now["t"], raising=False)  # base has no seam: the real clock barely moves
    held, refused = _HeldPass(monkeypatch), lp.LinksPass("refused")
    boards = [("default", Path("/nonexistent/kanban.db"))]
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_sync_client"):
            held.hold()
            now["t"] += 5
            assert not refused.request(boards)
            now["t"] += 599  # 604 s held, 599 s since the warning: still quiet
            assert not refused.request(boards)
            now["t"] += 1  # 605 s held, 600 s since the warning: warns again
            assert not refused.request(boards)
            now["t"] += 300
            assert not refused.request(boards)
            now["t"] += 300  # 1205 s held, 600 s since the repeat
            assert not refused.request(boards)
        warnings = _held_warnings(caplog)
        assert len(warnings) == 3, f"expected the first warning plus two repeats: {warnings}"
        assert ["for 5s" in warnings[0], "for 605s" in warnings[1], "for 1205s" in warnings[2]] == [True] * 3, \
            warnings
    finally:
        held.close()
