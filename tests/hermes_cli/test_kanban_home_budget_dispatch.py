"""Home budgets must run even when placement rejects the home as an executor."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_no_progress as progress
from hermes_cli import kanban_spend_budget as spend
from hermes_cli import kanban_worker_node as worker
from tests.hermes_cli import test_kanban_no_progress as progress_helpers
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


def remote_only(monkeypatch, conn, tid):
    # Use the real placement predicate, with a home lacking pool:worker.
    from hermes_cli import kanban_db_sync as sync

    sync.apply_sync_schema_steps(conn)
    conn.execute("INSERT INTO kanban_sync_state (board_id, gateway_id, hlc_last, mode) "
                 "VALUES ('default', 'hub', '', 'primary')")
    worker.set_runs_on(conn, tid, ["pool:worker"])
    conn.execute("UPDATE tasks SET created_on='hub' WHERE id=?", (tid,))
    monkeypatch.setattr(sync, "board_mode", lambda c: "primary")
    monkeypatch.setattr(worker, "registry", lambda: {"me": "hub", "machines": {
        "hub": {"labels": ["linux"], "state": "active"},
        "worker": {"labels": ["linux", "pool:worker"], "state": "active", "pickup": "all"},
    }})
    assert worker.dispatch_refusals(conn, "ready")[tid] == "labels_mismatch"


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_home_holds_remote_spend_once(kanban_home, monkeypatch, lane):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Remote spending loop", assignee="default")
        helpers._runs(conn, tid, spend.DEFAULT_RUNS)
        remote_only(monkeypatch, conn, tid)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
        spawns = helpers.Spawns()
        result = dispatch.dispatch_once(conn, spawn_fn=spawns)
        assert result.no_progress_held == [(tid, "spend_budget:triage")]
        assert kb.get_task(conn, tid).status == "triage"
        assert tid not in spawns.ids
        (event,) = helpers._events(conn, tid, spend.EVENT)
        assert event.payload["runs"] == 15
        assert any("dispatcher moved this card to triage" in c.body for c in kb.list_comments(conn, tid))
        dispatch.dispatch_once(conn, spawn_fn=spawns)
        assert len(helpers._events(conn, tid, spend.EVENT)) == 1


def test_home_holds_remote_no_progress(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, _ = progress_helpers._shepherd(conn, progress.DEFAULT_LIMIT)
        remote_only(monkeypatch, conn, tid)
        result = dispatch.dispatch_once(conn, spawn_fn=helpers.Spawns())
        assert (tid, "parked") in result.no_progress_held
        assert kb.get_task(conn, tid).status == "scheduled"
        assert len(helpers._events(conn, tid, progress.PARKED_EVENT)) == 1


@pytest.mark.parametrize("control", ["dry_run", "foreign"])
def test_refused_no_progress_controls_write_no_hold(kanban_home, monkeypatch, control):
    with kbc.connect_closing() as conn:
        tid, _ = progress_helpers._shepherd(conn, progress.DEFAULT_LIMIT)
        remote_only(monkeypatch, conn, tid)
        if control == "foreign":
            conn.execute("UPDATE tasks SET created_on='other-home' WHERE id=?", (tid,))
        result = dispatch.dispatch_once(conn, spawn_fn=helpers.Spawns(), dry_run=control == "dry_run")
        assert result.no_progress_held == []
        assert kb.get_task(conn, tid).status == "ready"
        assert helpers._events(conn, tid, progress.PARKED_EVENT) == []


@pytest.mark.parametrize("control", ["dry_run", "exempt", "foreign"])
def test_refused_budget_controls_write_no_hold(kanban_home, monkeypatch, control):
    with kbc.connect_closing() as conn:
        # max_retries keeps the auto-replan threshold above this spend-control fixture.
        tid = kb.create_task(conn, title="Remote control", assignee="default", max_retries=100)
        helpers._runs(conn, tid, spend.DEFAULT_RUNS)
        remote_only(monkeypatch, conn, tid)
        if control == "exempt":
            spend.set_override(conn, tid, runs=0, cache_read_tokens=0, by="operator", reason="standing work")
        if control == "foreign":
            conn.execute("UPDATE tasks SET created_on='other-home' WHERE id=?", (tid,))
        result = dispatch.dispatch_once(conn, spawn_fn=helpers.Spawns(), dry_run=control == "dry_run")
        assert result.no_progress_held == []
        assert kb.get_task(conn, tid).status == "ready"
        assert helpers._events(conn, tid, spend.EVENT) == []
        assert helpers._events(conn, tid, progress.PARKED_EVENT) == []
