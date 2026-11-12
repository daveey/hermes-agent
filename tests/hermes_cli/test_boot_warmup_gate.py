"""The always-on dashboard keeps its port closed until the boot prewarms finish (bounded).

Every hub core update restarts the always-on ``serve``. The new process bound its port with the kanban board,
annotation and project-tree prewarms still running, Desktop's reconnect burst landed on cold caches and ~4 s of warm-up
took 39 s under GIL contention, so /kanban/board and session reads hit Desktop's 6 s / 30 s deadlines on every update
(t_01a1d0971db77eb3). uvicorn runs lifespan startup before it binds, so a wait there keeps the port closed. [daveey]
"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from hermes_cli import boot_warmup_gate as gate
from hermes_cli import kanban_annotation_prewarm
from hermes_cli import web_server
from hermes_cli.always_on_dashboard import ENV_FLAG
from plugins.kanban.dashboard import board_prewarm
from tests.hermes_cli.test_serve_unloaded_sweeper import _quiet_boot


def _gated_thread(release: threading.Event, done: threading.Event) -> threading.Thread:
    def body():
        release.wait(10)
        done.set()

    thread = threading.Thread(target=body, daemon=True, name="fake-prewarm")
    thread.start()
    return thread


def test_join_within_shares_one_budget_across_threads():
    release = threading.Event()
    threads = [_gated_thread(release, threading.Event()) for _ in range(3)]
    try:
        started = time.monotonic()
        elapsed, running = gate.join_within(threads + [None], 0.3)
        assert running == 3
        assert 0.25 <= elapsed < 1.5 and time.monotonic() - started < 1.5  # one budget, not 0.3 s per thread
    finally:
        release.set()
    assert gate.join_within(threads, 5.0)[1] == 0


def test_hold_is_a_no_op_outside_the_always_on_backend(monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.setattr(gate, "boot_prewarm_threads", lambda: (_ for _ in ()).throw(AssertionError("read threads")))
    assert gate.hold_bind_for_prewarm() is None


def test_hold_waits_for_the_prewarms_in_the_always_on_backend(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    release, done = threading.Event(), threading.Event()
    thread = _gated_thread(release, done)
    monkeypatch.setattr(gate, "boot_prewarm_threads", lambda: [thread])
    threading.Timer(0.2, release.set).start()
    elapsed, running = gate.hold_bind_for_prewarm(budget_s=5.0)
    assert done.is_set() and running == 0 and elapsed >= 0.15


def test_hold_gives_up_at_the_budget_and_never_raises(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    release = threading.Event()
    thread = _gated_thread(release, threading.Event())
    monkeypatch.setattr(gate, "boot_prewarm_threads", lambda: [thread])
    try:
        elapsed, running = gate.hold_bind_for_prewarm(budget_s=0.2)
        assert running == 1 and elapsed < 2.0
    finally:
        release.set()
    monkeypatch.setattr(gate, "boot_prewarm_threads", lambda: 1 / 0)
    assert gate.hold_bind_for_prewarm(budget_s=0.2) is None


def test_boot_prewarm_threads_are_the_ones_the_prewarms_started(monkeypatch):
    first, second = threading.Thread(target=lambda: None), threading.Thread(target=lambda: None)
    monkeypatch.setattr(kanban_annotation_prewarm, "_boot_thread", [first])
    monkeypatch.setattr(board_prewarm, "_boot_thread", [second])
    assert gate.boot_prewarm_threads() == [first, second]
    monkeypatch.setattr(board_prewarm, "_boot_thread", [None])
    assert gate.boot_prewarm_threads() == [first]


def test_start_records_the_boot_threads(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.setattr(kanban_annotation_prewarm, "_boot_thread", [None])
    monkeypatch.setattr(board_prewarm, "_boot_thread", [None])
    monkeypatch.setattr(kanban_annotation_prewarm, "_boot_prewarm", lambda: None)
    monkeypatch.setattr(board_prewarm, "prewarm_boards", lambda api: None)
    annotation = kanban_annotation_prewarm.start_annotation_prewarm()
    board = board_prewarm.start_board_prewarm(object())
    assert kanban_annotation_prewarm.boot_thread() is annotation and board_prewarm.boot_thread() is board
    annotation.join(5)
    board.join(5)


def _serve_startup(monkeypatch, *, always_on: bool) -> tuple[bool, list]:
    """Run the serve lifespan startup with one fake prewarm thread that finishes ~0.3 s in.

    Returns (prewarm finished by the time startup completed, the thread lists the gate joined)."""
    _quiet_boot(monkeypatch)
    monkeypatch.setattr(kanban_annotation_prewarm, "start_annotation_prewarm", lambda: None)
    if always_on:
        monkeypatch.setenv(ENV_FLAG, "1")
    else:
        monkeypatch.delenv(ENV_FLAG, raising=False)
    release, done = threading.Event(), threading.Event()
    thread = _gated_thread(release, done)
    threading.Timer(0.3, release.set).start()
    monkeypatch.setattr(gate, "boot_prewarm_threads", lambda: [thread])
    joined, real_join = [], gate.join_within
    monkeypatch.setattr(gate, "join_within", lambda threads, budget_s: joined.append(list(threads)) or
                        real_join(threads, budget_s))
    try:
        with TestClient(web_server.app, raise_server_exceptions=False):
            return done.is_set(), joined  # lifespan startup has completed here: uvicorn would bind now
    finally:
        release.set()


def test_always_on_serve_lifespan_holds_startup_until_the_prewarms_finish(monkeypatch):
    finished, joined = _serve_startup(monkeypatch, always_on=True)
    assert len(joined) == 1 and len(joined[0]) == 1
    assert finished


def test_desktop_owned_serve_lifespan_does_not_wait(monkeypatch):
    _finished, joined = _serve_startup(monkeypatch, always_on=False)
    assert joined == []
