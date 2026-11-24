"""Tenant-bearing producer cards use the Board pool, not the legacy courier."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_pool_queue as helpers

hub = helpers.hub
svc = helpers.svc

# Every receipt on the prevention card: six original/decision shapes and one review.
RECEIPTS = (
    ("worker-1: cut spot interruptions, reap leftover processes", "other"),
    ("Fix red state CI: daveey/hermes (since f32a275f)", "hermes"),
    ("Resolve unattended upstream merge conflict", "hermes"),
    ("Repair FailedTailProbe clock assertion blocking PR3604 post-merge CI", "other"),
    ("Investigate state.db structural-corruption latch", "other"),
    ("Decide and implement durable unpushed-source custody", "other"),
)


@pytest.mark.parametrize("title,tenant", RECEIPTS)
def test_spend_producer_preserves_tenant_without_a_guard_card(hub, title, tenant):
    helpers._registry()
    root = kb.create_task(hub.conn, title=title, assignee="default", tenant=tenant)
    with kb.write_txn(hub.conn):
        result = decision.producer(hub.conn, root, "Spend budget reached")
    assert result == {'kind': 'unavailable', 'error': 'scope_authority_unavailable'}
    task = kb.get_task(hub.conn, root)
    assert task.tenant == tenant
    assert task.title == title
    assert hub.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    assert helpers._runs_on(hub.conn, root) == [helpers.POOL]
    payload = hub.conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='created'", (root,)).fetchone()[0]
    assert json.loads(payload)["portable_reason"] != "uncarried option"


def test_review_tool_and_cli_preserve_tenant_and_queue(hub):
    from hermes_cli import kanban as cli
    from tools import kanban_tools

    helpers._registry()
    title = "Independent exact-head review PR #3751 @386117f65a (core-wt ship review required)"
    out = json.loads(kanban_tools._handle_create({
        "title": title, "body": "Independent review only; you must not land.",
        "assignee": "default", "tenant": "hermes", "requires": ["terminal", "git"],
    }))
    assert out.get("ok"), out
    tid = out["task_id"]
    assert helpers._runs_on(hub.conn, tid) == [helpers.POOL]
    assert kb.get_task(hub.conn, tid).tenant == "hermes"
    out = json.loads(cli.run_slash(
        'create "Review another published patch" --assignee default --tenant other --json'))
    assert helpers._runs_on(hub.conn, out["id"]) == [helpers.POOL]
    assert kb.get_task(hub.conn, out["id"]).tenant == "other"


@pytest.mark.parametrize("tenant", ["hermes", "other"])
def test_board_transport_carries_tenant_to_worker(hub, tmp_path, monkeypatch, tenant):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from hermes_cli import kanban_worker_node as wn

    helpers._registry()
    tid = kb.create_task(hub.conn, title="Review a published change", assignee="default", tenant=tenant)
    assert helpers._runs_on(hub.conn, tid) == [helpers.POOL]
    hub.round()
    worker = helpers.Gateway(tmp_path, helpers.WORKER, hub.svc, monkeypatch, pickup="true")
    try:
        wn.set_registry_fetch(lambda: {"me": helpers.WORKER,
                                       "machines": helpers.seen_now(helpers._machines().values())})
        result = client.sync_board("default", worker.home / "kanban.db",
                                   hub.svc.hub(helpers.WORKER), helpers.WORKER)
        assert not result.error, result
        replica = wn.open_replica(worker.conn)
        assert replica is not None
        try:
            row = replica.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            assert row is not None and row["tenant"] == tenant
            assert wn.eligible(dict(row), helpers.WORKER, helpers._machines()) is None
            assert wn.import_cards(worker.conn, replica, [tid]) == [tid]
        finally:
            replica.close()
        task = kb.get_task(worker.conn, tid)
        assert task is not None and task.tenant == tenant
        assert helpers._runs_on(worker.conn, tid) == [helpers.POOL]
    finally:
        worker.conn.close()
        sync.set_lease_client(None)
        hub.enter()


def test_tenant_remains_uncarried_by_legacy_courier():
    card = {"title": "Review a published patch", "assignee": "default", "tenant": "hermes"}
    assert kp.structural_reason(card, helpers.CFG) is None
    assert kp._uncarried(card, kp._Cfg({}), carries=True) == ["tenant"]
    for key, value in (("human_request", {"action": "approve"}), ("max_retries", 2),
                       ("workspace_path", "/host/unpublished")):
        assert kp.structural_reason({**card, key: value}, helpers.CFG) == "uncarried option"
