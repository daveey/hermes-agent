"""Only the creator maintains old blocks; a worker still owns execution transitions.

Exercise two real gateway databases, sync clients, pickup and leases against the
shared in-memory Board service. Pending writes deliberately retain the copy.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_block_policy as policy
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, MACHINES, WORKER, Gateway


@pytest.fixture
def gateways(tmp_path, monkeypatch):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    svc = FakeService(lease_runs=True, machines=MACHINES)
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    try:
        yield hub, worker, svc
    finally:
        hub.conn.close()
        worker.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)


def test_retained_worker_block_is_escalated_only_by_creator(gateways):
    hub, worker, svc = gateways
    hub.enter()
    tid = kb.create_task(hub.conn, title="remote work", assignee="default",
                         runs_on=["machine:worker-1"])
    hub.round()
    worker.round()
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER
    worker.enter()
    run_id = kb.get_task(worker.conn, tid).current_run_id
    assert run_id is not None
    assert kb.block_task(worker.conn, tid, kind="transient", reason="retry needed",
                         expected_run_id=run_id)
    assert kb.get_task(worker.conn, tid).status == "blocked"
    # Execution's block and run end have not reached the Board: real outbox
    # capture prevents eviction, even though the worker has released its claim.
    assert worker.conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()
    assert tid not in wn.evict(worker.conn)
    later = int(time.time()) + policy.AGENT_BLOCK_GRACE_SECONDS + 10
    assert policy.sweep_agent_owned_blocks(worker.conn, now=later) == []
    assert kb.get_task(worker.conn, tid).status == "blocked"
    assert not worker.conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'agent_block_escalated'", (tid,)).fetchone()
    worker.round()
    hub.round()
    hub.enter()
    assert kb.get_task(hub.conn, tid).status == "blocked"
    assert policy.sweep_agent_owned_blocks(hub.conn, now=later) == [tid]
    assert policy.sweep_agent_owned_blocks(hub.conn, now=later) == []
    hub.round()
    worker.round()
    events = [e for e in svc.board(BOARD)["rows"]["task_events"].values()
              if e["task_id"] == tid and e["kind"] == "agent_block_escalated"]
    assert len(events) == 1
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == "triage"


@pytest.mark.parametrize("synced", [False, True])
def test_legacy_unattributed_block_still_escalates(tmp_path, monkeypatch, synced):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_sync_client as ksc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if synced:
        (tmp_path / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        tid = kb.create_task(conn, title="legacy local block", assignee="default")
        assert kb.block_task(conn, tid, kind="transient", reason="retry needed")
        if synced:
            ksc.ensure_identity(conn, BOARD, HUB)
            sync.set_mode(conn, "primary")
            # Pre-identity rows remain locally owned.
            assert conn.execute("SELECT created_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is None
        later = int(time.time()) + policy.AGENT_BLOCK_GRACE_SECONDS + 10
        assert policy.sweep_agent_owned_blocks(conn, now=later) == [tid]
        assert policy.sweep_agent_owned_blocks(conn, now=later) == []
    finally:
        conn.close()
