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


# ── only a pause the startup sweep takes is owed ─────────────────────────────────────────────────────────────

def _set_marker(home, **fields) -> None:
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries[KEY].update(fields)
    path.write_text(json.dumps(entries))


def _stale(home) -> None:
    _age_pause(home, _recovery.CFG["desktop"]["auto_continue"]["freshness_minutes"] * 60 + 60)


def _out_of_attempts(home) -> None:
    _set_marker(home, attempts=2)  # desktop.auto_continue.max_attempts defaults to 2


@pytest.mark.parametrize("skipped", [_stale, _out_of_attempts], ids=["stale", "out-of-attempts"])
def test_a_pause_the_startup_sweep_skips_is_owed_to_no_one(recovery, home, monkeypatch, skipped):
    """Review t_01a1cfcca83f9266: a 2 h old pause was owed to a reader started 10 s earlier, so each restart
    delayed the chat's headless deliveries by ``SUCCESSOR_YIELD_S`` though no successor would ever take it."""
    from tui_gateway import startup_resume as sr
    skipped(home)
    assert sr.pending_resumes(home, _recovery.CFG, log_skips=False) == []  # the sweep leaves it alone
    monkeypatch.setattr(up, "_process_started_at", lambda: time.time() - 10)
    assert not up.successor_owes_continuation(home, KEY)
    assert _wake() != "busy"
    assert _loaded()


def test_a_pause_the_startup_sweep_takes_is_owed_to_a_fresh_reader(recovery, home, monkeypatch):
    from tui_gateway import startup_resume as sr
    _age_pause(home, 2 * 3600)
    assert [r["session_key"] for r in sr.pending_resumes(home, _recovery.CFG, log_skips=False)] == [KEY]
    monkeypatch.setattr(up, "_process_started_at", lambda: time.time() - 10)
    assert up.successor_owes_continuation(home, KEY)
    assert _wake() == "busy"
    assert not recovery.done.wait(0.3) and recovery.calls == [] and not _loaded()


# ── an automation replay outlasts the yield window ───────────────────────────────────────────────────────────

def test_a_busy_replay_is_still_pending_when_the_yield_window_ends(recovery, home, monkeypatch):
    """A replay that first fires just after a dead writer's pause stamp, in a process that did not restart and
    with no successor coming, keeps rechecking past ``SUCCESSOR_YIELD_UNKNOWN_S`` and delivers then (10 x 60 s
    gave up at 600 s of the 660 s window)."""
    from tui_gateway import automation_replay as ar
    stamp = time.time()
    clock = [stamp - 30]  # the replay is scheduled 30 s before the pause stamp, so it first fires 30 s after
    timers = []
    monkeypatch.setattr(up, "_now", lambda: clock[0])
    monkeypatch.setattr(ar, "ENABLED", True)
    monkeypatch.setattr(ar, "_arm", lambda key, entry, delay, fire: timers.append((delay, fire, key)))
    verdicts = []
    real_submit = server._run_prompt_submit

    def submit(*a, **kw):
        # The recovery fixture replaces the worker; retain its worker-entry permit.
        permit = kw.get("replay_delivery")
        if permit is not None:
            assert permit.admit()
        return real_submit(*a, **kw)

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    real_wake = server._wake_session_turn
    monkeypatch.setattr(server, "_wake_session_turn",
                        lambda *a, **kw: (verdicts.append(real_wake(*a, **kw)), verdicts[-1])[1])
    ar.reset()
    try:
        assert ar.schedule(KEY, text="a card finished", display_kind="kanban_notice", display_metadata={}, due=0,
                           cause="lease", persisted=True, now=clock[0])
        while timers and clock[0] - stamp < up.SUCCESSOR_YIELD_UNKNOWN_S:
            delay, fire, key = timers.pop()
            clock[0] += delay
            if clock[0] - stamp >= up.SUCCESSOR_YIELD_UNKNOWN_S:
                assert ar.pending(KEY), f"the replay gave up {clock[0] - stamp:.0f} s after the pause stamp"
            fire(key)
        assert clock[0] - stamp >= up.SUCCESSOR_YIELD_UNKNOWN_S, (
            f"the replay gave up {clock[0] - stamp:.0f} s after the pause stamp; nothing more is scheduled")
        assert set(verdicts) == {"busy"}  # the window's last fire resumed the chat; its paused turn went first
        assert _loaded()
        assert_continued(recovery)
        assert ar.pending(KEY) and timers, "the replay gave up behind the paused turn's continuation"
        _, session = server._find_live_session_by_key(KEY)
        session["running"] = False  # the continuation finished
        delay, fire, key = timers.pop()
        clock[0] += delay
        fire(key)
        assert verdicts[-1] == "started" and not ar.pending(KEY)
        assert len(recovery.calls) == 2 and "a card finished" in recovery.calls[1][0]
    finally:
        ar.reset()
