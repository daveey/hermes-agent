"""Every headless cold resume leaves a turn paused for an update to the restarting backend. [daveey]

``server._wake_session_turn`` yields a fresh update pause to the dead writer's successor
(test_wake_yields_paused_turn.py). Two more producers resume an unloaded chat in-process without a viewer and so
took the paused turn's continuation the same way: the producer-admission sweeper (every ``hermes serve`` /
dashboard process) and the re-adopted-exit waker (each Desktop-owned backend sharing the home). Both now read the
same ``update_pause.successor_owes_continuation``.

The yield window also has to cover the hand-off it waits for. On hermes-hub (gui.log, 2026-10-30..11-12, 81
continuations of a dead writer's pause) the successor scheduled 42-198 s after the checkout moved, which was up
to 2781 s after the pause stamp; with no checkout move, up to 545 s after the stamp. 135 s from the stamp ended
before 50 of them.
"""
import json
import threading
import time

import pytest

import hermes_state_producer_admission as pa
from tests.tui_gateway import test_update_pause_recovery as _recovery
from tests.waiting import wait_until
from tui_gateway import producer_admission as delivery
from tui_gateway import server, update_pause as up
from tui_gateway.turn_marker import read_turn_marker

recovery = _recovery.recovery  # the real session.resume wiring over a dead writer's durable pause
assert_continued = _recovery.assert_continued

KEY = "paused"
# Past the stamp-counted window (the parent commit had one window, SUCCESSOR_YIELD_S).
PAST_WINDOW = getattr(up, "SUCCESSOR_YIELD_UNKNOWN_S", up.SUCCESSOR_YIELD_S) + 1
PRODUCER = json.dumps(["user-terminal", "job-1", "final"], separators=(",", ":"))
TEXT = "[User-terminal completion - tool result, not user input]\njob_id: job-1\nstatus: exited\nexit_code: 0"


def _loaded() -> bool:
    return server._find_live_session_by_key(KEY) is not None


def _age_pause(home, seconds: float) -> None:
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    stamp = time.time() - seconds
    entries[KEY].update(started_at=stamp, paused_at=stamp)
    path.write_text(json.dumps(entries))


@pytest.fixture
def home(recovery, monkeypatch):
    """The recovery store as this process's launch home, as the sweepers resolve it."""
    monkeypatch.setenv("HERMES_HOME", str(recovery.home))
    monkeypatch.setattr(up, "_process_started_at", lambda: 0.0, raising=False)  # this process predates the pause
    return recovery.home


# ── producer-admission sweeper ───────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def completion(home):
    receipt = pa.admit_producer_completion(producer=PRODUCER, profile_home=home, stored_session_id=KEY, text=TEXT)
    assert receipt["status"] == "queued", receipt
    return receipt


def test_producer_sweep_leaves_a_fresh_paused_turn_to_its_successor(recovery, home, completion):
    before = read_turn_marker(home, KEY)
    [rec] = delivery.sweep_once()
    assert not recovery.done.wait(0.5) and recovery.calls == [], "the sweep took the paused turn's continuation"
    assert not _loaded(), "the sweep resumed the paused chat in a process without its viewers"
    assert rec["verdict"] == "paused_turn"
    assert read_turn_marker(home, KEY) == before
    db = pa.open_profile_db(home)
    try:
        assert pa.pending_owners(db) == [KEY]  # still queued: the next pass delivers it
    finally:
        db.close()


def test_producer_sweep_wakes_a_pause_no_successor_picked_up(recovery, home, completion):
    _age_pause(home, PAST_WINDOW)
    [rec] = delivery.sweep_once()
    assert rec["verdict"] != "paused_turn"
    assert _loaded()
    assert_continued(recovery)


# ── re-adopted-exit waker ────────────────────────────────────────────────────────────────────────────────────

def test_readopt_wake_waits_out_a_fresh_pause_then_resumes(recovery, home, monkeypatch):
    monkeypatch.setattr(up, "SUCCESSOR_POLL_S", 0.02, raising=False)
    out = []
    waker = threading.Thread(target=lambda: out.append(server._wake_chat_for_readopted_exit(KEY)), daemon=True)
    waker.start()
    assert not recovery.done.wait(0.5) and recovery.calls == [], "the wake took the paused turn's continuation"
    assert not _loaded(), "the wake resumed the paused chat in a process without its viewers"
    assert waker.is_alive()  # it waits; the parked exit is not dropped
    _age_pause(home, PAST_WINDOW)  # no successor came
    waker.join(5)
    assert not waker.is_alive() and out == [None]  # resumed under the launch profile
    assert _loaded()
    assert_continued(recovery)


def test_readopt_wake_finds_the_chat_its_successor_loaded_here(recovery, home, monkeypatch):
    """The startup sweep of this same backend continues the turn while the waker yields: the waker then has
    nothing to resume (the chat's poller takes the parked exit)."""
    monkeypatch.setattr(up, "SUCCESSOR_POLL_S", 0.02, raising=False)
    resumes = []
    real = server._methods["session.resume"]
    monkeypatch.setitem(server._methods, "session.resume", lambda rid, p: (resumes.append(rid), real(rid, p))[1])
    waker = threading.Thread(target=server._wake_chat_for_readopted_exit, args=(KEY,), daemon=True)
    waker.start()
    assert not recovery.done.wait(0.3)
    _recovery.mount()  # the successor's resume
    assert_continued(recovery)
    waker.join(5)
    assert not waker.is_alive()
    assert resumes == ["resume"]  # the waker resumed nothing
    assert len(recovery.calls) == 1


# ── the window covers the measured hand-off ──────────────────────────────────────────────────────────────────

def _wake() -> str:
    return server._wake_session_turn(KEY, "a card finished", display_kind="kanban_notice", label="kanban-sweep-wake")


def test_a_waker_restarted_with_the_writer_counts_from_its_own_start(recovery, home, monkeypatch):
    """Hub 2026-11-10 19:46: schedules 2760-2781 s after the pause stamp, 197-198 s after the checkout moved."""
    _age_pause(home, 2781)
    monkeypatch.setattr(up, "_process_started_at", lambda: time.time() - 197, raising=False)
    assert _wake() == "busy"
    assert not recovery.done.wait(0.5) and recovery.calls == [] and not _loaded()
    monkeypatch.setattr(up, "_process_started_at", lambda: time.time() - up.SUCCESSOR_YIELD_S - 1)
    _wake()
    assert _loaded()
    assert_continued(recovery)


def test_a_waker_that_did_not_restart_counts_from_the_pause_stamp(recovery, home):
    """Hub 2026-11-12 03:23: a schedule 545 s after the stamp with no checkout move (longest of that kind)."""
    _age_pause(home, 544)
    assert _wake() == "busy"
    assert not recovery.done.wait(0.5) and recovery.calls == [] and not _loaded()
    _age_pause(home, PAST_WINDOW)
    _wake()
    assert _loaded()
    assert_continued(recovery)


def test_process_start_is_this_process_birth():
    started = up._process_started_at()
    assert 0 < started <= time.time()
    assert wait_until(lambda: up._process_started_at() == started, timeout=1)  # stable across calls
