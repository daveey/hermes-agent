"""Cheap shared-wall evidence crosses the in-memory Board with home custody.

Real pickup, leases and row transport; no live Board writes or fabricated foreign IDs.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from hermes_cli import kanban_wake_check as wake
from tests.hermes_cli import test_kanban_shared_blockers_copies as copies
from tests.hermes_cli.test_kanban_worker_node import BOARD

BID = copies.BID
gws = copies.gws


def _cfg(command):
    return {"kanban": {"shared_blocker_checks": {BID: command}}}


def _events(conn, tid):
    return [event for event in kb.list_events(conn, tid) if event.kind == checks.CHECK_EVENT]


def _park_with_remapped_run(home, worker, flag):
    # Home-only completed history occupies integer IDs but is not picked up.
    home.enter()
    for index in range(3):
        seed = kb.create_task(home.conn, title=f"home history {index}", assignee="default")
        assert kb.claim_task(home.conn, seed)
        assert kb.complete_task(home.conn, seed, result="history")
    tid = copies._park(home, worker, flag)
    runs = [gateway.conn.execute(
        "SELECT id, uid FROM task_runs WHERE task_id=? ORDER BY id DESC", (tid,)
    ).fetchone() for gateway in (home, worker)]
    assert runs[0]["uid"] == runs[1]["uid"]
    assert runs[0]["id"] != runs[1]["id"], "fixture must exercise real transport remapping"
    return tid


@pytest.mark.parametrize("home_first", [False, True])
@pytest.mark.parametrize("rc", [0, 1, 2])
def test_only_home_checks_and_portable_evidence_roundtrips(gws, tmp_path, home_first, rc):
    home, worker = gws
    tid = _park_with_remapped_run(home, worker, tmp_path / "waiting")
    now = int(time.time()) + 10
    command = f"printf 'board evidence'; exit {rc}"
    before = wake.get_state(worker.conn, tid)
    assert before is not None
    assert wake.get_state(home.conn, tid) is None
    tokens = [checks._assertion_tokens(gateway.conn, now)[BID] for gateway in (home, worker)]
    assert tokens[0] == tokens[1]

    for gateway in ((home, worker) if home_first else (worker, home)):
        gateway.enter()
        assert (tid in wake.foreign_ids(gateway.conn)) is (gateway is worker)
        assert checks.run_due_checks(gateway.conn, cfg=_cfg(command), now=now) == (
            [BID] if gateway is home and rc == 0 else [])
    assert _events(worker.conn, tid) == []
    emitted = _events(home.conn, tid)
    assert len(emitted) == 1
    payload = emitted[0].payload
    assert payload["assertion_id"] == tokens[0]
    assert payload["rc"] == rc
    assert payload["fails"] == (1 if rc == 2 else 0)
    assert payload["output"] == "board evidence"

    home.round()
    worker.round()
    home.round()
    board_events = copies._board_events(home, tid, checks.CHECK_EVENT)
    assert len(board_events) == 1
    canonical = board_events[0]
    assert canonical["origin_gateway"] == home.name
    assert json.loads(canonical["payload"]) == payload
    for gateway in (home, worker):
        events = _events(gateway.conn, tid)
        assert len(events) == 1
        assert events[0].payload == payload
        uid = gateway.conn.execute("SELECT uid FROM task_events WHERE id=?", (events[0].id,)).fetchone()[0]
        assert uid == canonical["uid"]
        assert blockers.blocker_states(gateway.conn, now=now)[BID].is_open is (rc != 0)
        assert kb.get_task(gateway.conn, tid).status == "scheduled"
    assert _events(home.conn, tid)[0].id != _events(worker.conn, tid)[0].id
    # The cheap checker neither takes nor deletes the executor's host-local probe.
    assert wake.get_state(worker.conn, tid) == before
    assert wake.get_state(home.conn, tid) is None

    worker.enter()
    assert checks.run_due_checks(worker.conn, cfg=_cfg(command), now=now + 120) == []
    assert blockers.release_cleared_blockers(worker.conn, now=now) == []
    home.enter()
    assert blockers.release_cleared_blockers(home.conn, now=now) == ([tid] if rc == 0 else [])
    home.round()
    worker.round()
    assert len(copies._board_events(home, tid, checks.CHECK_EVENT)) == 1
    assert len(copies._board_events(home, tid, blockers.CLEARED_EVENT)) == (1 if rc == 0 else 0)


def test_same_second_reassertion_invalidates_transported_success(gws, tmp_path, monkeypatch):
    home, worker = gws
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    first = _park_with_remapped_run(home, worker, tmp_path / "first-waiting")
    home.enter()
    old_token = checks._assertion_tokens(home.conn, now)[BID]
    assert checks.run_due_checks(home.conn, cfg=_cfg("exit 0"), now=now) == [BID]
    home.round()
    worker.round()
    assert not blockers.blocker_states(worker.conn, now=now)[BID].is_open

    second = copies._park(home, worker, tmp_path / "second-waiting")
    for gateway in (home, worker):
        runs = gateway.conn.execute(
            "SELECT uid, ended_at FROM task_runs WHERE task_id IN (?, ?) ORDER BY id", (first, second)
        ).fetchall()
        assert len(runs) == 2
        assert runs[0]["ended_at"] == runs[1]["ended_at"]
        assert runs[0]["uid"] != runs[1]["uid"]
        assert checks._assertion_tokens(gateway.conn, now)[BID] != old_token
        assert blockers.blocker_states(gateway.conn, now=now)[BID].is_open
    assert checks._assertion_tokens(home.conn, now) == checks._assertion_tokens(worker.conn, now)

    worker.enter()
    assert checks.run_due_checks(worker.conn, cfg=_cfg("exit 0"), now=now) == []
    home.enter()
    assert checks.run_due_checks(home.conn, cfg=_cfg("exit 0"), now=now) == [BID]
    home.round()
    worker.round()
    home.round()
    events = [event for event in home.svc.board(BOARD)["rows"]["task_events"].values()
              if event["kind"] == checks.CHECK_EVENT]
    assert len(events) == 2
    assert {event["origin_gateway"] for event in events} == {home.name}
    assert len({json.loads(event["payload"])["assertion_id"] for event in events}) == 2
    for gateway in (home, worker):
        assert not blockers.blocker_states(gateway.conn, now=now)[BID].is_open
        assert kb.get_task(gateway.conn, first).status == "scheduled"
        assert kb.get_task(gateway.conn, second).status == "scheduled"
