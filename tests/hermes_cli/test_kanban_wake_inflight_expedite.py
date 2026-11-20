"""An expedite that commits while a probe is in flight survives that probe's older "not yet" result. [daveey]

Review t_01a1f71be02bffcd of PR #3729: ``run_due_wake_checks`` claims a probe with next_at = ts + every. A comment (or
awaited-card status change) arriving while the probe runs expedites next_at to now; the probe's result, read before
that change, then ``apply_result`` unconditionally wrote next_at = now + every, so the answer waited a full
``check_every`` (900 s) again. The window is the probe's whole run (3.7-3.8 s measured on a pool host).

Each test commits the change from a second connection (the real writer boundary) after the probe has produced its
result and before ``apply_result`` records it: a deterministic stand-in for the race.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli.test_kanban_comment_wake import EVERY, _next_at, _park, _status
from tests.hermes_cli.test_kanban_comment_wake import live, probes  # fixtures, requested below by name


@pytest.fixture
def board(request):
    return request.getfixturevalue("live")


@pytest.fixture
def wake_probes(request):
    return request.getfixturevalue("probes")


def _inflight(monkeypatch, db_path, change, result=None):
    """After the probe computed its result, ``change`` commits on another connection; the old result is returned."""
    inner = kwc._run_probe

    def run(key, cmd, cwd, env):
        rc, out = inner(key, cmd, cwd, env) if result is None else result
        other = kbc.connect(Path(str(db_path)))
        try:
            change(other)
        finally:
            other.close()
        return rc, out

    monkeypatch.setattr(kwc, "_run_probe", run)
    return inner


def _tick(board, now):
    kwc.run_due_wake_checks(board, now=now, wait=True)


def test_a_comment_during_the_probe_is_not_lost_to_its_older_result(board, wake_probes, monkeypatch):
    tid, t0 = _park(board, wake_probes)
    db = kbc._main_db_file(board)
    # The park's probe is due; its claim sets next_at = t0 + 600 + EVERY, then the answer lands mid-probe.
    with kb.write_txn(board):
        board.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (t0, tid))
    inner = _inflight(monkeypatch, db, lambda c: kb.add_comment(c, tid, "default", "Decision: rotate daily; proceed."))
    wall = int(time.time())
    _tick(board, t0 + 600)
    assert _status(board, tid) == "scheduled"  # the in-flight result was the older, not-yet one
    assert _next_at(board, tid) <= wall + 5  # RED on main: t0 + 600 + EVERY
    monkeypatch.setattr(kwc, "_run_probe", inner)

    _tick(board, t0 + 601)
    assert _status(board, tid) == "ready"
    met = [e for e in kb.list_events(board, tid) if e.kind == "condition_met"]
    assert met and "Decision: rotate daily" in (met[-1].payload or {})["output"]


def test_a_rejected_comment_during_the_probe_costs_one_probe_then_the_cadence(board, wake_probes, monkeypatch):
    tid, t0 = _park(board, wake_probes)
    db = kbc._main_db_file(board)
    with kb.write_txn(board):
        board.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (t0, tid))
    inner = _inflight(monkeypatch, db, lambda c: kb.add_comment(c, tid, "kanban-dispatcher", "note", system=True))
    wall = int(time.time())
    _tick(board, t0 + 600)
    assert _next_at(board, tid) <= wall + 5
    monkeypatch.setattr(kwc, "_run_probe", inner)

    runs = wake_probes[tid]
    _tick(board, t0 + 601)  # the real probe skips the system comment: not yet
    assert wake_probes[tid] == runs + 1 and _status(board, tid) == "scheduled"
    assert _next_at(board, tid) == t0 + 601 + EVERY  # ordinary cadence again, no further expedite


def test_a_status_change_during_the_probe_is_not_lost_either(board, monkeypatch):
    review = kb.create_task(board, title="review", assignee="worker")
    owner = kb.create_task(board, title="owner", assignee="worker")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'running' WHERE id IN (?, ?)", (review, owner))
    flag = os.path.join(os.path.dirname(str(kbc._main_db_file(board))), "never-written")
    assert kb.schedule_task(board, owner, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting",
                            wake_check=f"test -f {flag}", check_every=EVERY,
                            awaiting={"kind": "card", "ref": review, "why": "review"})
    t0 = int(time.time())
    with kb.write_txn(board):
        board.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (t0, owner))

    def finish(c):
        with kb.write_txn(c):
            c.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))

    _inflight(monkeypatch, kbc._main_db_file(board), finish, result=(1, ""))
    _tick(board, t0)
    assert _status(board, owner) == "scheduled"
    assert _next_at(board, owner) <= int(time.time()) + 5  # RED on main: t0 + EVERY


@pytest.mark.parametrize("result", [
    pytest.param((1, "not yet"), id="not-yet"),
    pytest.param((2, "boom"), id="broken-once"),
    pytest.param((3, "stale_age=60s gateway=g1"), id="offline-gateway"),
    pytest.param((3, "source_state=unknown gateway=g1"), id="scope-unknown-already-escalated"),
])
def test_every_non_waking_result_branch_keeps_the_expedite(board, wake_probes, monkeypatch, result):
    from hermes_cli import kanban_offline_backoff

    # scope-unknown: an escalation under 24 h old holds the probe parked (the write this PR changed).
    monkeypatch.setattr(kanban_offline_backoff, "recently_escalated", lambda *a, **k: True)
    tid, t0 = _park(board, wake_probes)
    with kb.write_txn(board):
        board.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (t0, tid))
    _inflight(monkeypatch, kbc._main_db_file(board), lambda c: kb.add_comment(c, tid, "default", "answer"),
              result=result)
    wall = int(time.time())
    _tick(board, t0 + 600)
    assert _status(board, tid) == "scheduled"
    assert _next_at(board, tid) <= wall + 5


def test_no_expedite_keeps_the_ordinary_cadence_through_the_tick(board, wake_probes, monkeypatch):
    tid, t0 = _park(board, wake_probes)
    with kb.write_txn(board):
        board.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (t0, tid))
    monkeypatch.setattr(kwc, "_run_probe", lambda key, cmd, cwd, env: (1, "not yet"))
    _tick(board, t0 + 600)
    assert _next_at(board, tid) == t0 + 600 + EVERY


@pytest.mark.parametrize("result", [
    pytest.param((1, "not yet"), id="not-yet"),
    pytest.param((2, "boom"), id="broken-once"),
    pytest.param((3, "stale_age=60s gateway=g1"), id="offline-gateway"),
    pytest.param((3, "source_state=unknown gateway=g1"), id="scope-unknown-already-escalated"),
])
def test_no_expedite_keeps_exactly_now_plus_every_when_the_probe_ran_long(board, wake_probes, monkeypatch, result):
    """The apply's clock is later than the claim's (the probe took 37 s). Unexpedited, next_at stays exactly
    now + every: neither the claim's slot (always-keep), nor min(slot, now + every), nor a <= comparison."""
    from hermes_cli import kanban_offline_backoff

    monkeypatch.setattr(kanban_offline_backoff, "recently_escalated", lambda *a, **k: True)
    tid, t0 = _park(board, wake_probes)
    ts = t0 + 600
    claimed = ts + EVERY
    with kb.write_txn(board):
        board.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (claimed, tid))
    state = kwc.get_state(board, tid)
    assert state is not None
    rc, out = result
    kwc.apply_result(board, tid, state["cmd"], rc, out, now=ts + 37, generation=state["generation"],
                     claimed_next_at=claimed)
    assert _status(board, tid) == "scheduled"
    assert _next_at(board, tid) == ts + 37 + EVERY


def test_a_repark_during_the_probe_drops_the_old_result_and_keeps_the_new_park(board, wake_probes, monkeypatch):
    tid, t0 = _park(board, wake_probes)
    with kb.write_txn(board):
        board.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (t0, tid))
    later = {}

    def repark(c):
        task = kb.get_task(c, tid)
        assert task is not None
        newer = kb.add_comment(c, tid, "default", "second question")
        later["cmd"] = f"hermes kanban wait-comment {tid} --after-id {newer} --skip-system"
        with kb.write_txn(c):
            c.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        assert kb.schedule_task(c, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting again",
                                wake_check=later["cmd"], check_every=EVERY)

    _inflight(monkeypatch, kbc._main_db_file(board), repark, result=(1, "not yet"))
    _tick(board, t0 + 600)
    state = kwc.get_state(board, tid)
    assert state is not None and state["cmd"] == later["cmd"]
    assert state["last_rc"] is None and state["fails"] == 0  # the old probe's result recorded nothing
    assert state["next_at"] != t0 + 600 + EVERY  # nothing from the old probe's apply: its slot was dropped


def test_a_cross_card_wait_comment_keeps_its_cadence(board, wake_probes):
    """Deferred scope, pinned: only ``wait-comment <the parked card itself>`` is expedited. Matching a park on
    ANOTHER card's comments needs a scan of every park's cmd per comment commit (the self-card case is one indexed
    read), and the standard chat-wait builder always waits on its own card."""
    source = kb.create_task(board, title="source", assignee="worker")
    owner = kb.create_task(board, title="owner", assignee="worker")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (owner,))
    assert kb.schedule_task(board, owner, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on another card",
                            wake_check=f"hermes kanban wait-comment {source} --after-id 0 --skip-system",
                            check_every=EVERY)
    before = _next_at(board, owner)
    kb.add_comment(board, source, "default", "answer on the other card")
    assert _next_at(board, owner) == before
