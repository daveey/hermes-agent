"""Quota maintenance has one owner; real gateway stores/sync over an in-memory Board transport."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_quota as quota
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_quota_guard import LEGACY_BLOCK, QUOTA_ERR
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, MACHINES, WORKER, Gateway


@pytest.fixture
def world(tmp_path, monkeypatch):
    svc = FakeService(lease_runs=True, machines=MACHINES)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    home = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    executor = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield home, executor, svc
    home.conn.close()
    executor.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _retained_block(home, executor, svc, kind):
    home.enter()
    tid = kb.create_task(home.conn, title="legacy quota hold", assignee="default",
                         runs_on=["machine:worker-1"])
    home.round()
    executor.round()
    executor.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER
    assert kb.block_task(executor.conn, tid, reason=LEGACY_BLOCK, kind=kind, human_request={
        "action": "Resolve access; unblock resumes, archive abandons.",
        "why_human": "Legacy ask retained for local probe custody.",
        "interaction": "external", "scope": "test", "evidence": "legacy hold",
        "resolved_check": "exit 1", "resolved_check_every": 900,
    })
    executor.round()
    home.round()
    assert tid not in svc.board(BOARD)["leases"]
    for gateway in (home, executor):
        assert kb.get_task(gateway.conn, tid).status == "blocked"
    assert executor.conn.execute(
        "SELECT purpose FROM task_wake_checks WHERE task_id=?", (tid,)).fetchone()[0] == "ask_resolved"
    return tid


def _completion(gateway, monkeypatch, at):
    gateway.enter()
    monkeypatch.setattr(kb.time, "time", lambda: at)
    tid = kb.create_task(gateway.conn, title="provider recovered", assignee="default")
    assert kb.claim_task(gateway.conn, tid)
    assert kb.complete_task(gateway.conn, tid, result="ok")


@pytest.mark.parametrize("kind", ["capability", "needs_input"])
@pytest.mark.parametrize("order", [(0, 1), (1, 0), "concurrent"])
def test_only_home_releases_retained_copy_before_sync(world, monkeypatch, kind, order):
    home, executor, svc = world
    tid = _retained_block(home, executor, svc, kind)
    blocked_at = home.conn.execute(
        "SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()[0]
    # Both stores have independent recovery evidence, but neither has seen the other's release.
    for gateway in (home, executor):
        _completion(gateway, monkeypatch, blocked_at + 60)
    results = {}
    if order == "concurrent":
        barrier = Barrier(2)

        def release(gateway):
            conn = kbc.connect(gateway.home / "kanban.db")
            try:
                barrier.wait(timeout=30)
                return quota.release_recovered_quota_blocks(conn)
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(release, gateway) for gateway in (home, executor)]
            results = {i: future.result(timeout=60) for i, future in enumerate(futures)}
    else:
        for i in order:
            gateway = (home, executor)[i]
            gateway.enter()
            results[i] = quota.release_recovered_quota_blocks(gateway.conn)
    # Sync only after both sweeps: models concurrent disconnected maintenance in both orders.
    for gateway in (executor, home, executor, home):
        gateway.round()
    notes = [r for r in svc.board(BOARD)["rows"].get("task_comments", {}).values()
             if r.get("task_id") == tid and r.get("body", "").startswith(quota.RELEASE_NOTE_MARKER)]
    assert results == {0: [tid], 1: []}, (results, notes)
    assert len(notes) == 1, notes
    assert kb.get_task(home.conn, tid).status == "ready"
    home.enter()
    assert quota.release_recovered_quota_blocks(home.conn) == []


def test_executor_quota_stamp_still_clears_after_cooldown(world, monkeypatch):
    home, executor, svc = world
    home.enter()
    tid = kb.create_task(home.conn, title="executor cooldown", assignee="default",
                         runs_on=["machine:worker-1"])
    home.round()
    executor.round()
    executor.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER
    run_id = kb.get_task(executor.conn, tid).current_run_id
    now = int(kb.time.time())
    executor.conn.execute("UPDATE task_runs SET outcome='rate_limited', ended_at=? WHERE id=?", (now, run_id))
    executor.conn.execute("UPDATE tasks SET status='ready', last_failure_error=? WHERE id=?", (QUOTA_ERR, tid))
    executor.conn.commit()
    # Execution handling is deliberately NOT fenced by the creator-only maintenance rule.
    monkeypatch.setattr(kb.time, "time", lambda: now + 400)
    assert kbd.check_respawn_guard(executor.conn, tid) is None
    assert executor.conn.execute("SELECT last_failure_error FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None
