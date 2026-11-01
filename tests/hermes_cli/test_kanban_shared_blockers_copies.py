"""Shared-wall sweeps have home authority; executor wake probes keep custody.

Real stores, pickup, leases and sync, with the in-memory Board transport.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_timed_park as ktp
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_wake_check_home as wake_home
from tests.hermes_cli.test_kanban_wake_check_home import BUILD, _probe, _status
from tests.hermes_cli.test_kanban_worker_node import BOARD

BID = "copy-test-wall"


@pytest.fixture
def gws(tmp_path, monkeypatch):
    yield from wake_home.gws.__wrapped__(tmp_path, monkeypatch)


def _park(home, worker, flag):
    home.enter()
    tid = kb.create_task(home.conn, title="shared wall", assignee="default", runs_on=["linux"])
    home.round()
    worker.round()
    worker.tick()
    assert _status(worker.conn, tid) == "running"
    with kb.write_txn(worker.conn):
        assert ktp.park(
            worker.conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z wall",
            requested_kind="time", source_status="running", wake_check=_probe(flag),
            check_every=300, awaiting=BUILD, blocker_id=BID)
    worker.round()
    home.round()
    assert _status(home.conn, tid) == _status(worker.conn, tid) == "scheduled"
    return tid


def _board_events(home, tid, kind):
    return [e for e in home.svc.board(BOARD)["rows"]["task_events"].values()
            if e["task_id"] == tid and e["kind"] == kind]


@pytest.mark.parametrize("home_first", [False, True])
@pytest.mark.parametrize("lane", ["scheduled", "blocked", "ready", "review"])
def test_only_home_releases_shared_clear_before_sync(gws, tmp_path, home_first, lane):
    home, worker = gws
    tid = _park(home, worker, tmp_path / "waiting")
    # The executor's real host-local probe provides clear evidence, not a fake run outcome.
    flag = tmp_path / "clear"
    clear_tid = _park(home, worker, flag)
    flag.write_text("wall cleared\n")
    worker.enter()
    kwc.run_due_wake_checks(worker.conn, now=int(time.time()) + 301, wait=True)
    assert _status(worker.conn, clear_tid) == "ready"
    worker.round()
    home.round()
    # Exercise retained legacy blocked copies and queued hold retirement as well as scheduled copies.
    for gateway in (home, worker):
        with kb.write_txn(gateway.conn):
            gateway.conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    order = (home, worker) if home_first else (worker, home)
    results = {}
    for gateway in order:
        gateway.enter()
        results[gateway.name] = ksb.release_cleared_blockers(gateway.conn)
    assert results[worker.name] == []
    assert not [e for e in kb.list_events(worker.conn, tid) if e.kind == ksb.CLEARED_EVENT]
    assert results[home.name] == ([tid] if lane in ("scheduled", "blocked") else [])
    worker.round()
    home.round()
    assert len(_board_events(home, tid, ksb.CLEARED_EVENT)) == 1
    comments = [c for c in home.svc.board(BOARD)["rows"].get("task_comments", {}).values()
                if c["task_id"] == tid and "shared blocker" in c["body"]]
    assert len(comments) == (1 if lane in ("scheduled", "blocked") else 0)


@pytest.mark.parametrize("home_first", [False, True])
def test_only_home_elects_stranded_probe_before_sync(gws, tmp_path, home_first):
    home, worker = gws
    tid = _park(home, worker, tmp_path / "waiting")
    for gateway in (home, worker):
        with kb.write_txn(gateway.conn):
            gateway.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    results = {}
    for gateway in ((home, worker) if home_first else (worker, home)):
        gateway.enter()
        results[gateway.name] = ksb.probe_stranded_blockers(gateway.conn, interval=0)
    assert results == {home.name: [tid], worker.name: []}
    worker.round()
    home.round()
    assert len(_board_events(home, tid, ksb.PROBE_RELEASED_EVENT)) == 1


def test_foreign_winner_is_not_filtered_before_election(gws, tmp_path):
    from tests.hermes_cli.test_kanban_shared_blockers import _blocked_behind

    home, worker = gws
    tid = _park(home, worker, tmp_path / "waiting")
    worker.enter()
    local = _blocked_behind(worker.conn, "later local candidate", blocker_id=BID)
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET status='blocked' WHERE id IN (?, ?)", (tid, local))
        worker.conn.execute("UPDATE task_runs SET ended_at=ended_at-60 WHERE task_id=?", (tid,))
    assert ksb.probe_stranded_blockers(worker.conn, interval=0) == []
    assert _status(worker.conn, local) == "blocked"
