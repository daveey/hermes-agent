"""Archiving a card another machine runs under its live Board lease (kanban_remote_stop). [daveey]

Before: kanban_archive on the creator wrote ``archived`` locally and answered ``{"status": "archived"}``; the Board
rejected the status write (``live_claim``), the card read back ``running`` and the remote worker kept going. Now the
creator sends a stop request (an ``archive_requested`` event) and answers ``archive_pending``; the holder archives
the card at its next dispatcher tick, terminating its worker. Two gateways share the in-memory Board fake.
"""

from __future__ import annotations

import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER, Gateway


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


@pytest.fixture
def kills(monkeypatch):
    """Record worker terminations instead of signalling: the fake spawns report the test's own pid."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(kbd, "_kill_fn", lambda signal_fn: (lambda pid, sig: sent.append((pid, sig))))
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started_at=None: True)
    return sent


def _events(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]


def _running_on_worker(hub, worker, svc):
    """A hub card the worker claims and runs; both sides synced, hub reads it running under worker-1's lease."""
    hub.enter()
    tid = kb.create_task(hub.conn, title="remote work", assignee="default", runs_on=["linux"])
    hub.round()
    worker.round()
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER
    worker.round()
    hub.round()
    row = hub.conn.execute("SELECT status, lease_holder, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["status"], row["lease_holder"], row["claim_lock"]) == ("running", WORKER, None)
    return tid


def _tool(name, args):
    from tools import kanban_tools
    return json.loads(getattr(kanban_tools, name)(args))


def test_tool_archive_under_a_remote_lease_is_pending_and_writes_no_archived_event(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    out = _tool("_handle_archive", {"task_id": tid})
    assert out == {"ok": True, "task_id": tid, "status": "archive_pending", "holder": WORKER}
    assert kb.get_task(hub.conn, tid).status == "running"
    events = _events(hub.conn, tid)
    assert "archived" not in events and "archive_requested" in events
    # Nothing queued for the Board would move the card's status; the request is an event.
    ops = [(r["table_name"], json.loads(r["fields"])) for r in hub.conn.execute(
        "SELECT table_name, fields FROM kanban_sync_outbox WHERE state = 'pending'")]
    assert not [f for t, f in ops if t == "tasks" and "status" in f]
    assert any(t == "task_events" and f.get("kind") == "archive_requested" for t, f in ops)
    hub.round()
    assert not hub.conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state = 'rejected'").fetchone()
    assert kills == []  # the creator never signals a worker it does not run


def test_cli_archive_under_a_remote_lease_is_pending(gws, svc, kills, capsys):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    from hermes_cli import kanban as kcli
    out = kcli.run_slash(f"archive {tid}")
    assert "Archive pending" in out and WORKER in out, out
    assert kb.get_task(hub.conn, tid).status == "running"
    assert "archived" not in _events(hub.conn, tid)


def test_the_holder_stops_its_worker_at_the_next_tick_and_the_card_reads_archived(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    assert _tool("_handle_archive", {"task_id": tid})["status"] == "archive_pending"
    hub.round()  # the stop request reaches the Board
    sync.consume_dispatcher_wake()
    worker.round()  # ... and the holder's copy of the card
    assert sync.consume_dispatcher_wake()  # which wakes its dispatcher now, not at the next poll
    pid = worker.conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    res = worker.tick()
    assert res.remote_archived == [tid]
    assert kills and kills[0][0] == pid  # its worker was terminated
    assert kb.get_task(worker.conn, tid).status == "archived"
    term = json.loads(worker.conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archive_worker_termination'",
        (tid,)).fetchone()[0])
    assert term["host_local"] is True and term["terminated"] is True
    worker.round()
    assert tid not in svc.board(BOARD)["leases"]
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == "archived"
    hub.round()
    assert kb.get_task(hub.conn, tid).status == "archived"
    archived = json.loads(hub.conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived'", (tid,)).fetchone()[0])
    assert archived["via"] == "remote_archive_request"
    # A second tick does nothing more.
    assert worker.tick().remote_archived == []


def test_a_stop_request_for_an_earlier_run_does_not_stop_the_current_one(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    worker.enter()
    with kb.write_txn(worker.conn):
        kb._append_event(worker.conn, tid, "archive_requested", {"holder": WORKER, "run_uid": "old-run"})
    assert worker.tick().remote_archived == []
    assert kb.get_task(worker.conn, tid).status == "running"


def test_archive_of_a_host_local_running_card_behaves_as_before(gws, svc, kills):
    hub, _worker = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="hub's own", assignee="default", runs_on=["machine:hub"])
    hub.tick()
    assert kb.get_task(hub.conn, tid).status == "running"
    out = _tool("_handle_archive", {"task_id": tid})
    assert out == {"ok": True, "task_id": tid, "status": "archived"}
    assert kb.get_task(hub.conn, tid).status == "archived"
    term = json.loads(hub.conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archive_worker_termination'",
        (tid,)).fetchone()[0])
    assert term["host_local"] is True and term["terminated"] is True and kills
    hub.round()
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == "archived"


def test_archive_is_refused_with_the_holder_named_when_the_board_cannot_carry_a_stop_request(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    for (name,) in hub.conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                    "AND tbl_name = 'task_events'").fetchall():
        hub.conn.execute(f"DROP TRIGGER {name}")
    out = _tool("_handle_archive", {"task_id": tid})
    assert out.get("code") == "remote_live_lease" and out.get("holder") == WORKER, out
    assert WORKER in out["error"]
    assert kb.get_task(hub.conn, tid).status == "running"
    assert "archive_requested" not in _events(hub.conn, tid) and "archived" not in _events(hub.conn, tid)


def test_other_lifecycle_tools_on_a_remote_lease_are_refused_with_the_holder(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    out = _tool("_handle_block", {"task_id": tid, "reason": "stop"})
    assert out.get("rejection") == "live_claim" and out.get("holder") == WORKER, out
    assert kb.get_task(hub.conn, tid).status == "running"
    out = _tool("_handle_complete", {"task_id": tid, "summary": "done elsewhere"})
    assert out.get("rejection") == "live_claim" and out.get("holder") == WORKER, out
    assert kb.get_task(hub.conn, tid).status == "running"
    # The holder's own worker is not refused: on worker-1 the lease is its own.
    worker.enter()
    from hermes_cli.kanban_remote_stop import remote_holder
    assert remote_holder(worker.conn, tid) is None


def test_a_status_write_the_board_rejects_live_claim_is_recorded_on_the_card(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    # A path that bypasses the tools (an operator script) writes the status anyway.
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    hub.round()
    rejected = hub.conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'sync_rejected'",
                                (tid,)).fetchone()
    assert rejected is not None
    payload = json.loads(rejected[0])
    assert payload["reason"].startswith("live_claim") and "status" in payload["fields"]
    assert kb.get_task(hub.conn, tid).status == "running"  # the Board's row came back
    assert os.getpid()  # (no worker was signalled on either side)
    assert kills == []


@pytest.mark.parametrize("bulk", [False, True])
def test_dashboard_archive_under_a_remote_lease_is_pending(gws, svc, kills, bulk):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.plugins.test_kanban_dashboard_plugin import _load_plugin_router
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    client = TestClient(app)
    if bulk:
        r = client.post("/api/plugins/kanban/tasks/bulk", json={"ids": [tid], "archive": True})
        assert r.status_code == 200, r.text
        assert r.json()["results"] == [{"id": tid, "ok": True, "archive_pending": WORKER}]
    else:
        r = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"status": "archived"})
        assert r.status_code == 200, r.text
        assert r.json()["archive_pending"] == WORKER and r.json()["task"]["status"] == "running"
    assert "archived" not in _events(hub.conn, tid) and "archive_requested" in _events(hub.conn, tid)
