"""Every path that ends or abandons a claim ends that claim's run row, on this gateway and on the Board.

The live failure (2026-10-27, MacBook ``default``): a run that ended within one sync round of an earlier push of
the same row came back ``running``. The pull applied the feed copy of an older state of the row (our own earlier
op, whose outbox row the push had already deleted, or the Board's lease-grant row) over the newer local row, and
the push then sent that clobbered row whole, so the Board went back to ``running`` too. Runs carry no
``current_run_id`` after they end, so nothing ever closed them again.

Service: the in-memory Board fake with Board-faithful lease runs (``kanban_sync_fake_board``).
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "macbook", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def live(kanban_home, svc):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        sync.set_lease_client(previous)
        conn.close()


def _round(kanban_home, svc):
    res = ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert res.error is None and (res.push is None or res.push.rejected == 0)
    return res


def _run(conn, uid):
    return conn.execute("SELECT status, outcome, ended_at FROM task_runs WHERE uid = ?", (uid,)).fetchone()


def _board_run(svc, uid):
    return svc.board(BOARD)["rows"]["task_runs"][uid]


def _claimed(live, kanban_home, svc, title):
    tid = kb.create_task(live, title=title, assignee="worker")
    _round(kanban_home, svc)
    assert kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    uid = live.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?",
                       (tid,)).fetchone()[0]
    return tid, uid


def _assert_ended(live, svc, uid, outcome):
    local = _run(live, uid)
    assert (local["status"], local["outcome"]) == (outcome, outcome) and local["ended_at"], dict(local)
    board = _board_run(svc, uid)
    assert (board["status"], board["outcome"]) == (outcome, outcome) and board["ended_at"], board
    dangling = live.execute(
        "SELECT r.id FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE r.ended_at IS NULL "
        "AND r.status = 'running' AND (t.current_run_id IS NULL OR t.current_run_id != r.id)").fetchall()
    assert not dangling


def test_stale_lock_reclaim_survives_the_echo_of_the_runs_earlier_push(live, kanban_home, svc):
    tid, uid = _claimed(live, kanban_home, svc, "stale lock")
    _round(kanban_home, svc)  # the run's insert reaches the Board; its echo now waits in the feed
    live.execute("UPDATE tasks SET claim_expires = ?, worker_pid = NULL WHERE id = ?", (int(time.time()) - 5, tid))
    assert kb.release_stale_claims(live) == 1
    _round(kanban_home, svc)  # pull (the stale echo) then push (the end)
    _round(kanban_home, svc)
    _assert_ended(live, svc, uid, "reclaimed")


def test_orphan_sweep_survives_the_echo_of_the_runs_earlier_push(live, kanban_home, svc):
    tid, uid = _claimed(live, kanban_home, svc, "orphan")
    _round(kanban_home, svc)
    live.execute("UPDATE tasks SET claim_lock = NULL, worker_pid = NULL WHERE id = ?", (tid,))
    assert kd.reconcile_orphaned_running(live) == [tid]
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    _assert_ended(live, svc, uid, "reclaimed")


def test_a_run_that_ends_before_its_first_push_survives_the_grants_own_run_row(live, kanban_home, svc):
    """The Board's grant wrote the run row before the local claim did; that copy reaching the gateway after the
    run already ended (a slow round) must not reopen it."""
    tid, uid = _claimed(live, kanban_home, svc, "slow round")
    live.execute("UPDATE tasks SET claim_expires = ?, worker_pid = NULL WHERE id = ?", (int(time.time()) - 5, tid))
    assert kb.release_stale_claims(live) == 1
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    _assert_ended(live, svc, uid, "reclaimed")


def test_a_grant_whose_local_claim_lost_ends_its_run_everywhere(live, kanban_home, svc, monkeypatch):
    tid = kb.create_task(live, title="cas lost", assignee="worker")
    _round(kanban_home, svc)
    monkeypatch.setattr(kb, "claim_and_open_run", lambda *a, **k: None)  # another claimer won the local CAS
    assert kb.claim_task(live, tid, ttl_seconds=900) is None
    assert not svc.board(BOARD)["leases"]  # the unused lease was handed back
    (uid,) = svc.board(BOARD)["rows"]["task_runs"]
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    _assert_ended(live, svc, uid, "released")
    assert kb.get_task(live, tid).status == "ready"


def test_a_newer_board_write_to_a_run_still_lands(live, kanban_home, svc):
    """The fence only drops copies we already have: a later Board write (another writer's end) applies."""
    _tid, uid = _claimed(live, kanban_home, svc, "board ends it")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    b = svc.board(BOARD)
    svc._put(b, "task_runs", uid, {"status": "reclaimed", "outcome": "reclaimed", "ended_at": 7}, "server", None,
             f"{9_999_999_999_999:013d}-0000-server")
    _round(kanban_home, svc)
    assert tuple(_run(live, uid)) == ("reclaimed", "reclaimed", 7)
    assert live.execute("SELECT sync_version FROM task_runs WHERE uid = ?", (uid,)).fetchone()[0] == \
        _board_run(svc, uid)["version"]


def test_the_dispatcher_closes_a_dangling_run_and_the_board_follows(live, kanban_home, svc):
    """Backstop for rows left behind before the fence (and any path not found yet): a run still ``running`` that
    is no longer its card's current run is closed once it is old enough to have settled."""
    tid, uid = _claimed(live, kanban_home, svc, "dangling")
    _round(kanban_home, svc)
    # What the race left on every host: the card moved on, the run row did not.
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'blocked', claim_lock = NULL, claim_expires = NULL, "
                     "current_run_id = NULL WHERE id = ?", (tid,))
    _live_tid, live_uid = _claimed(live, kanban_home, svc, "live run")  # a current run must never be touched
    young = kb.create_task(live, title="young", assignee="worker")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)",
                     (young, int(time.time())))
    old = int(time.time()) - kd.DANGLING_RUN_MIN_AGE_SECONDS - 1
    live.execute("UPDATE task_runs SET started_at = ? WHERE uid IN (?, ?)", (old, uid, live_uid))
    closed = kd.close_dangling_runs(live)
    assert [live.execute("SELECT uid FROM task_runs WHERE id = ?", (i,)).fetchone()[0] for i in closed] == [uid]
    assert kd.close_dangling_runs(live) == []  # idempotent
    _round(kanban_home, svc)
    assert _run(live, uid)["status"] == "reclaimed" and _run(live, uid)["ended_at"]
    assert _board_run(svc, uid)["status"] == "reclaimed" and _board_run(svc, uid)["ended_at"]
    assert _run(live, live_uid)["status"] == "running" and _board_run(svc, live_uid)["status"] == "running"
    assert live.execute("SELECT count(*) FROM task_runs WHERE task_id = ? AND status = 'running'",
                        (young,)).fetchone()[0] == 1


def test_dispatch_pass_runs_the_dangling_run_sweep(live, monkeypatch):
    called = []
    monkeypatch.setattr(kd, "close_dangling_runs", lambda conn, **kw: called.append(conn) or [])
    kd.dispatch_once(live, dry_run=True)
    assert called
