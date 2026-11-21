"""A Kanban notice refused only by the core update's admission hold wakes within seconds of the hold lifting. [daveey]

Hub 2026-11-21: notices refused "busy" while "Hermes update admission is paused or busy" were written as passive rows
and the notice guarantee offered them again only after its 300 s grace, then ``BUSY_RETRY_S`` doubling. Now such a
wake is stamped ``hold_wait``, charges no attempt, is due the moment the hold lifts, and a watcher runs the pass
within ``POLL_S`` of that; every other busy refusal keeps the backoff.
"""

from __future__ import annotations

import contextlib
import json
import time

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB
from tui_gateway import kanban_unloaded as ku
from tui_gateway import notice_guarantee as ng
from tui_gateway import notice_hold_wait as hw

CHAT = "20261121_050000_holdwt"


@pytest.fixture
def hold(monkeypatch):
    state = {"held": True, "noted": 0}
    monkeypatch.setattr(hw, "update_hold_active", lambda home=None: state["held"])

    def note(**kw):
        state["noted"] += 1
        return True

    monkeypatch.setattr(hw, "note_waiting", note)
    return state


@pytest.fixture
def db(tmp_path):
    so._reset_hooks_for_tests()
    ksa._reset_cache_for_tests()
    ng._reset_refreshed_for_tests()
    handle = SessionDB(tmp_path / "state.db")
    handle.create_session(CHAT, "desktop")
    handle.append_message(CHAT, "user", "run the deploy card")
    handle.append_message(CHAT, "assistant", "Started the deploy card.")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


def _notice(db, text, *, age=600.0, **meta):
    row = db.append_kanban_notice(CHAT, text, {"notice_id": f"n:{text}:{time.time()}", "display_text": text, **meta})
    db._write_rowcount("UPDATE messages SET timestamp = ? WHERE id = ?", (time.time() - age, row))
    return row


def _run(db, verdict="started", now=None):
    calls = []

    def wake(sid, msg_id):
        calls.append((sid, msg_id))
        return verdict

    @contextlib.contextmanager
    def opener(chat_id):
        yield db, db.get_compression_tip(chat_id) or chat_id

    out = ng.guarantee_pass(now=now, stores=lambda: iter([db]), holders=list, session_db=opener, wake=wake,
                            spawn=lambda job: job())
    return out, calls


def _closeout(db, row):
    raw = db._read_one("SELECT display_metadata FROM messages WHERE id = ?", (row,))[0]
    return json.loads(raw).get("closeout") or {}


def test_a_hold_refused_closeout_charges_nothing_and_wakes_when_the_hold_lifts(db, hold):
    row = _notice(db, "✔ Kanban t_aaa done — deploy card")
    now = time.time()

    _out, calls = _run(db, verdict="busy", now=now)

    closeout = _closeout(db, row)
    assert calls == [(CHAT, row)] and closeout["state"] == "deferred" and closeout["hold_wait"] is True
    assert closeout.get("attempts", 0) == 0 and closeout.get("busy_attempts", 0) == 0  # nothing charged
    assert hold["noted"] == 1  # the watcher was started
    assert _run(db, now=now + 30) == ([], [])  # the hold is still on: not due

    hold["held"] = False
    _out, calls = _run(db, verdict="started", now=now + 30)  # long before retry_at / BUSY_RETRY_S

    assert calls == [(CHAT, row)] and _closeout(db, row)["state"] == "started"


def test_a_young_hold_refused_row_skips_the_grace_window(db, hold):
    row = _notice(db, "✔ Kanban t_bbb done", age=5.0, closeout=hw.hold_wait_closeout())
    assert _run(db) == ([], [])  # held

    hold["held"] = False
    _out, calls = _run(db)
    assert calls == [(CHAT, row)]


def test_an_ordinary_busy_wake_keeps_its_backoff(db, hold):
    hold["held"] = False
    row = _notice(db, "✔ Kanban t_ccc done")
    now = time.time()

    _run(db, verdict="busy", now=now)

    closeout = _closeout(db, row)
    assert closeout["state"] == "deferred" and not closeout.get("hold_wait")
    assert closeout["busy_attempts"] == 1 and closeout["retry_at"] >= now + ng.BUSY_RETRY_S
    assert hold["noted"] == 0
    assert _run(db, now=now + 30) == ([], [])


def test_the_sweeps_passive_row_carries_the_hold_wait_stamp(hold):
    meta = {"notice_id": "n1"}
    stamped = ku._hold_wait_metadata("busy", meta)
    assert hw.is_hold_wait(stamped) and "closeout" not in meta

    assert ku._hold_wait_metadata("refused", meta) is meta
    assert ku._hold_wait_metadata("busy", {"closeout": {"state": "self"}}) == {"closeout": {"state": "self"}}
    hold["held"] = False
    assert ku._hold_wait_metadata("busy", meta) is meta


def test_the_watcher_runs_passes_after_the_lift_until_no_wake_waits_for_a_slot(monkeypatch):
    hw._reset_for_tests()
    held = iter([True, True, False])
    results = iter([[{"result": "dispatched"}] * 8 + [{"result": "skipped:slots"}] * 3,
                    [{"result": "dispatched"}] * 3])
    sleeps, passes = [], []

    def run_pass():
        passes.append(1)
        return next(results)

    monkeypatch.setattr(hw, "update_hold_active", lambda home=None: next(held, False))
    jobs = []
    assert hw.note_waiting(run_pass=run_pass, held=lambda: next(held, False), sleep=sleeps.append,
                           spawn=jobs.append) is True
    assert hw.note_waiting(run_pass=run_pass, spawn=jobs.append) is False  # one watcher at a time
    jobs[0]()

    assert len(passes) == 2
    assert sleeps == [hw.POLL_S, hw.POLL_S, hw.PASS_EVERY_S]  # polled the hold, then one slot-wait pass gap
    assert hw.note_waiting(run_pass=run_pass, held=lambda: False, sleep=sleeps.append, spawn=jobs.append) is True


def test_the_drain_stops_after_its_window():
    hw._reset_for_tests()
    clock = iter(range(0, 10_000, 50))
    passes = hw.watch(run_pass=lambda: [{"result": "skipped:slots"}], held=lambda: False, sleep=lambda s: None,
                      monotonic=lambda: next(clock))
    assert 1 < passes <= hw.DRAIN_S / 50 + 2


def test_a_hold_wait_followed_by_an_ordinary_busy_keeps_the_normal_backoff(db, hold):
    row = _notice(db, "✔ Kanban t_ddd done")
    now = time.time()
    _run(db, verdict="busy", now=now)  # refused by the hold
    assert _closeout(db, row)["hold_wait"] is True

    hold["held"] = False
    _out, calls = _run(db, verdict="busy", now=now + 30)  # the hold lifted, the chat is busy for another reason

    closeout = _closeout(db, row)
    assert calls == [(CHAT, row)] and closeout["state"] == "deferred" and not closeout.get("hold_wait")
    assert closeout["busy_attempts"] == 1 and closeout["retry_at"] >= now + ng.BUSY_RETRY_S  # stamped off the wall clock
    assert _run(db, now=now + 90) == ([], [])  # not due again before its backoff


def test_a_busy_wake_held_by_a_retiring_backend_retries_soon_and_charges_nothing(db, hold, monkeypatch):
    from tui_gateway import gateway_background_holder as gbh
    hold["held"] = False
    row = _notice(db, "✔ Kanban t_eee done")
    now = time.time()

    def wake(sid, msg_id):
        with gbh._retiring_mu:
            gbh._retiring_busy[sid] = now + gbh.RETIRING_OWNER_RETRY_S
        return "busy"

    @contextlib.contextmanager
    def opener(chat_id):
        yield db, chat_id

    ng.guarantee_pass(now=now, stores=lambda: iter([db]), holders=list, session_db=opener, wake=wake,
                      spawn=lambda job: job())

    closeout = _closeout(db, row)
    assert closeout["state"] == "deferred" and closeout["retry_at"] == now + gbh.RETIRING_OWNER_RETRY_S
    assert closeout.get("attempts", 0) == 0 and closeout.get("busy_attempts", 0) == 0
    _out, calls = _run(db, now=now + gbh.RETIRING_OWNER_RETRY_S + 1)
    assert calls == [(CHAT, row)]


def test_a_notice_refused_while_the_watcher_decides_to_stop_is_not_lost():
    """``note_waiting`` during the watcher's last pass makes it watch again; once it returns, ``thread`` is already
    clear (in the same locked section), so the next ``note_waiting`` starts a new watcher."""
    hw._reset_for_tests()
    jobs, passes = [], []

    def run_pass():
        passes.append(1)
        if len(passes) == 1:
            assert hw.note_waiting(run_pass=run_pass, spawn=jobs.append) is False  # the live watcher takes it
        return []

    assert hw.note_waiting(run_pass=run_pass, held=lambda: False, sleep=lambda s: None, spawn=jobs.append)
    jobs[0]()

    assert len(passes) == 2 and len(jobs) == 1  # watched again for the late notice, no second thread
    assert hw._state["thread"] is None
    assert hw.note_waiting(run_pass=run_pass, held=lambda: False, sleep=lambda s: None, spawn=jobs.append) is True
