"""A park's wake_check keeps exactly one live probe, on the gateway that parked the card, and its wake reaches the
card's home board. [daveey]

t_01a18c12ba6895b8 (created on the MacBook, ``runs_on=[machine:hub]``) was picked up and run by the hub. Its worker
parked it with ``wake_check: hermes kanban wait-check task t_01a18c31ab3e58f7 --gateway macbook``. The probe row
(``task_wake_checks``, machine-local, never synced) was written on the hub beside the hub's copy. The hub evicted
the copy on its next tick (``kanban_worker_node.evict``) and ``run_due_wake_checks``' stale sweep deleted the
orphaned row. The MacBook got the ``scheduled`` status and event through the Board feed but no probe, so the card
slept until an orchestrator woke it 43 min after its producer finished (7 such cards on the MacBook, 2026-10-30).

The probe stays on the parking host: only it is sure to be able to run it (its dry run passed there; a
``wait-check capability-owner`` request id lives only in that host's request store, t_01a18a8d2bd8cf97). The worker
keeps its scheduled copy while the probe is live, and the wake is a status write the Board carries home. The home
board lints every scheduled card whose park carries a wake_check: it re-registers a probe it lost once, and wakes the
card with a comment on a second loss or when a relayed park's probe host has gone silent.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_timed_park as ktp
from hermes_cli import kanban_wake_check as kwc
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, WORKER, Gateway

BUILD = {"kind": "external", "ref": "release build 559", "why": "the build job writes the flag when it lands"}
ALIVE, LOST, RESTORED = "wake_check_alive", "wake_check_lost", "wake_check_restored"


def _probe(flag: Path) -> str:
    return f"test -f {flag} && cat {flag} || exit 1"


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _status(conn, tid):
    task = kb.get_task(conn, tid)
    return task.status if task else None


@pytest.fixture
def gws(tmp_path, monkeypatch):
    svc = FakeService(lease_runs=True)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    sync.consume_dispatcher_wake()
    home = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)       # creates the card (the MacBook's role)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")  # picks it up and runs it (the hub's role)
    getattr(kwc, "_RESTORE_SEEN", {}).clear()
    yield home, worker
    kwc._shutdown_for_tests()
    for g in (home, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _relayed_park(home, worker, flag):
    """Home creates a card, the worker runs it, parks it on a wake_check and pushes the park."""
    home.enter()
    tid = kb.create_task(home.conn, title="runs on the worker", assignee="default", runs_on=["linux"])
    home.round()
    worker.round()
    worker.tick()
    assert _status(worker.conn, tid) == "running"
    run_id = kb.get_task(worker.conn, tid).current_run_id
    with kb.write_txn(worker.conn):
        assert ktp.park(worker.conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for build 559",
                        requested_kind="time", source_status="running", expected_run_id=run_id,
                        wake_check=_probe(flag), check_every=300, awaiting=BUILD)
    worker.round()   # push the park
    return tid


def test_a_relayed_park_keeps_one_live_probe_and_its_wake_reaches_the_home_board(gws, tmp_path):
    home, worker = gws
    flag = tmp_path / "flag"
    tid = _relayed_park(home, worker, flag)
    worker.tick()    # pickup evicts finished copies; the wake-check sweep runs
    worker.tick()
    assert _status(worker.conn, tid) == "scheduled", "the worker evicted its copy, and with it the only probe"
    state = kwc.get_state(worker.conn, tid)
    assert state is not None and state["cmd"] == _probe(flag) and state["every"] == 300

    home.round()     # the home board receives the scheduled status and the park event
    assert _status(home.conn, tid) == "scheduled"
    park = _events(home.conn, tid, "scheduled")[-1].payload
    assert park["wake_check"] == _probe(flag) and park["probe_host"] == WORKER
    home.enter()
    kwc.run_due_wake_checks(home.conn, wait=True)
    assert kwc.get_state(home.conn, tid) is None          # exactly one probe: the worker's
    home.tick()
    assert _status(home.conn, tid) == "scheduled" and _events(home.conn, tid, LOST) == []

    # The producer finishes: the worker's next due probe wakes the card, and the home board sees it.
    deadline = time.monotonic() + 30
    while kwc.in_flight() and time.monotonic() < deadline:   # let the ticks' own probe runs land first
        time.sleep(0.05)
    flag.write_text("build 559 seen\n")
    worker.enter()
    kwc.run_due_wake_checks(worker.conn, now=int(time.time()) + 301, wait=True)
    assert _status(worker.conn, tid) == "ready"
    worker.round()
    home.round()
    assert _status(home.conn, tid) == "ready"
    assert "build 559 seen" in _events(home.conn, tid, "condition_met")[-1].payload["output"]


def test_home_timer_reserves_final_run_for_relayed_probe(gws, tmp_path):
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli.kanban_continuation import _wake_parks
    home, worker = gws
    flag = tmp_path / 'terminal'
    tid = _relayed_park(home, worker, flag)
    home.round()
    home.enter()
    budget.set_override(home.conn, tid, runs=1, by='owner', reason='one settlement run')
    assert kwc.get_state(home.conn, tid) is None
    assert _wake_parks(home.conn, 4070908801)[0] == []
    assert _status(home.conn, tid) == 'scheduled'
    assert len(_events(home.conn, tid, 'terminal_run_reserved')) == 1
    flag.write_text('bb land: exit 0\n')
    worker.enter()
    kwc.run_due_wake_checks(worker.conn, now=int(time.time()) + 301, wait=True)
    worker.round()
    home.round()
    assert _status(home.conn, tid) == 'ready'
    assert _events(home.conn, tid, 'condition_met')[-1].payload['output'] == 'bb land: exit 0'
    assert budget.hold(home.conn, tid) is None


def test_a_relayed_probe_reports_alive_to_the_home_board(gws, tmp_path):
    home, worker = gws
    tid = _relayed_park(home, worker, tmp_path / "flag")
    worker.enter()
    kwc.run_due_wake_checks(worker.conn, wait=True)                 # just parked: no alive note yet
    assert _events(worker.conn, tid, ALIVE) == []
    later = int(time.time()) + kwc.ALIVE_EVERY + 1
    kwc.run_due_wake_checks(worker.conn, now=later, wait=True)
    assert _events(worker.conn, tid, ALIVE)[-1].payload["host"] == WORKER
    worker.round()
    home.round()
    assert _events(home.conn, tid, ALIVE)


def test_the_home_board_wakes_a_relayed_park_whose_probe_host_went_silent(gws, tmp_path):
    home, worker = gws
    flag = tmp_path / "flag"
    tid = _relayed_park(home, worker, flag)   # the worker never ticks again (its probe never reports)
    home.round()
    home.enter()
    now = int(time.time())
    assert kwc.restore_missing_checks(home.conn, now=now + kwc.ALIVE_EVERY) == {"restored": [], "woken": []}
    silent = now + kwc.ALIVE_EVERY + 2 * 300 + kwc.SILENT_GRACE + 5
    assert kwc.restore_missing_checks(home.conn, now=silent) == {"restored": [], "woken": [tid]}
    assert _status(home.conn, tid) == "ready"
    lost = _events(home.conn, tid, LOST)[-1].payload
    assert lost["reason"] == "remote_probe_silent" and lost["probe_host"] == WORKER
    assert any(WORKER in c.body and _probe(flag) in c.body for c in kb.list_comments(home.conn, tid))


def test_silent_remote_probe_is_not_reused_by_no_progress_repark(gws, tmp_path):
    from hermes_cli import kanban_no_progress as no_progress

    home, worker = gws
    flag = tmp_path / "recovered-flag"
    tid = _relayed_park(home, worker, flag)
    home.round()
    home.enter()
    previous = _events(home.conn, tid, "scheduled")[-1].payload
    now = int(time.time()) + kwc.ALIVE_EVERY + 2 * 300 + kwc.SILENT_GRACE + 5
    assert kwc.restore_missing_checks(home.conn, now=now)["woken"] == [tid]
    assert _events(home.conn, tid, LOST)[-1].payload["probe_host"] == WORKER
    assert no_progress.hold(home.conn, tid, now=now, budget=1) == "parked"
    park = _events(home.conn, tid, "scheduled")[-1].payload
    assert park["probe_host"] == HUB, "no-progress re-armed the same silent remote probe host"
    assert park["wake_check"] == _probe(flag)
    assert park["awaiting"] == previous["awaiting"]
    assert kwc.get_state(home.conn, tid)["cmd"] == _probe(flag)
    # Home actually executes the replacement; a label-only reassignment is not recovery.
    flag.write_text("build recovered on home\n")
    kwc.run_due_wake_checks(home.conn, now=now + 1, wait=True)
    assert _status(home.conn, tid) == "ready"
    assert "build recovered on home" in _events(home.conn, tid, "condition_met")[-1].payload["output"]


# --- the lost-probe sweep on one board --------------------------------------------------------------------------

@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    getattr(kwc, "_RESTORE_SEEN", {}).clear()
    with kbc.connect_closing() as conn:
        yield conn
    kwc._shutdown_for_tests()


def _parked(conn, *, wake_check):
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    with kb.write_txn(conn):
        assert ktp.park(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait", requested_kind="time",
                        source_status="running", wake_check=wake_check, check_every=300,
                        awaiting=BUILD if wake_check else {"kind": "clock", "why": "rate-limit cooldown"},
                        clock_only=not wake_check)
    return tid


def _lose(conn, tid):
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))


def test_a_lost_probe_is_restored_once_then_a_second_loss_wakes_the_card(board, tmp_path):
    flag = tmp_path / "flag"
    tid = _parked(board, wake_check=_probe(flag))
    clock = _parked(board, wake_check=None)
    _lose(board, tid)
    assert kwc.restore_missing_checks(board) == {"restored": [tid], "woken": []}
    state = kwc.get_state(board, tid)
    assert state["cmd"] == _probe(flag) and state["every"] == 300
    assert kwc.get_state(board, clock) is None                                  # clock-only: not a lost probe
    assert kwc.restore_missing_checks(board) == {"restored": [], "woken": []}   # idempotent

    _lose(board, tid)
    assert kwc.restore_missing_checks(board) == {"restored": [], "woken": [tid]}
    assert _status(board, tid) == "ready"
    lost = _events(board, tid, LOST)[-1].payload
    assert lost["wake_check"] == _probe(flag) and lost["reason"] == "lost_again"
    assert any(_probe(flag) in c.body for c in kb.list_comments(board, tid))
    assert _status(board, clock) == "scheduled"


def test_the_dispatcher_tick_restores_a_lost_probe_and_runs_it(board, tmp_path):
    tid = _parked(board, wake_check=_probe(tmp_path / "flag"))
    _lose(board, tid)
    kwc.run_due_wake_checks(board, wait=True)
    state = kwc.get_state(board, tid)
    assert state is not None and state["last_rc"] == 1
    assert _events(board, tid, RESTORED)
