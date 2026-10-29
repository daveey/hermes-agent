"""Repair foreign-parent edges consumed by the old both-endpoints partition filter."""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli.test_kanban_cross_gateway_parents import (
    BOARD, ME, OTHER, _other, _round, kanban_home, live, svc,
)

__all__ = ["kanban_home", "live", "svc"]


def _consumed_link(live, svc, kanban_home, monkeypatch):
    child = kb.create_task(live, title="child", assignee="worker")
    _round(kanban_home, svc)
    _other(svc, "parent", "tasks", "t_foreign", {"title": "parent", "status": "ready", "created_at": 1})
    _other(svc, "edge", "task_links", f"t_foreign>{child}", {"parent_id": "t_foreign", "child_id": child})
    partition = ksc.own_partition
    with monkeypatch.context() as old:
        old.setattr(ksc, "own_partition", lambda conn, changes, gateway: [
            ch for ch in partition(conn, changes, gateway) if ch.table != "task_links"])
        ksc._pull_replica(BOARD, svc.hub(ME), ME)
        ksc.pull(live, svc.hub(ME), BOARD, observers=False, partition=ME)
    assert not live.execute("SELECT 1 FROM task_links WHERE child_id = ?", (child,)).fetchone()
    return child


def test_next_round_repairs_a_consumed_foreign_edge_without_rewinding_or_echoing(
    live, svc, kanban_home, monkeypatch,
):
    child = _consumed_link(live, svc, kanban_home, monkeypatch)
    cursor = sync.sync_state(live)["cursor"]
    _round(kanban_home, svc)
    assert kb.parent_states(live, child) == [("t_foreign", "ready")]
    assert kb.get_task(live, "t_foreign") is None
    assert sync.sync_state(live)["cursor"] == cursor
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name = 'task_links'").fetchone()
    assert kb.claim_task(live, child) is None
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "todo"
    shadow = ksc.open_shadow(BOARD, ME)
    try:
        assert ksc.diff(live, shadow, ME)["child_mismatch"] == {}
    finally:
        shadow.close()
    _round(kanban_home, svc)
    assert kb.parent_states(live, child) == [("t_foreign", "ready")]


@pytest.mark.parametrize("guard", ["tombstone", "pending", "rejected", "shadow"])
def test_partition_repair_preserves_local_intent(live, svc, kanban_home, monkeypatch, guard):
    child = _consumed_link(live, svc, kanban_home, monkeypatch)
    uid = f"t_foreign>{child}"
    with kbc.write_txn(live):
        if guard == "tombstone":
            live.execute("INSERT INTO kanban_sync_tombstones VALUES ('task_links', ?, 1)", (uid,))
        elif guard in ("pending", "rejected"):
            live.execute("INSERT INTO kanban_sync_outbox "
                         "(op_id, table_name, row_uid, op, fields, hlc, created_at, state) "
                         "VALUES ('local-delete', 'task_links', ?, 'delete', '{}', '', 1, ?)", (uid, guard))
    if guard == "shadow":
        sync.set_mode(live, "shadow")
    # Keep unanswered local intent unanswered while exercising the production round.
    monkeypatch.setattr(ksc, "push", lambda *args, **kwargs: None)
    _round(kanban_home, svc)
    assert not live.execute("SELECT 1 FROM task_links WHERE child_id = ?", (child,)).fetchone()


def test_real_foreign_parent_unlink_is_captured_and_not_resurrected(live, svc, kanban_home, monkeypatch):
    child = _consumed_link(live, svc, kanban_home, monkeypatch)
    sync.apply_remote_changes(live, [sync.RemoteChange(
        0, "task_links", f"t_foreign>{child}", "upsert",
        {"parent_id": "t_foreign", "child_id": child}, 1, "", OTHER,
    )], new_cursor=sync.sync_state(live)["cursor"], observers=False)
    assert kb.unlink_tasks(live, "t_foreign", child)
    assert live.execute("SELECT 1 FROM kanban_sync_tombstones WHERE row_uid = ?",
                        (f"t_foreign>{child}",)).fetchone()
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert not kb.parent_states(live, child)
    assert svc.board(BOARD)["rows"]["task_links"][f"t_foreign>{child}"]["deleted_at"]


def test_replica_readded_edge_overrides_its_historical_remote_tombstone(live, svc, kanban_home, monkeypatch):
    child = _consumed_link(live, svc, kanban_home, monkeypatch)
    uid = f"t_foreign>{child}"
    res = svc.hub(OTHER).ops(BOARD, [{"client_op_id": "delete-edge", "op": "delete",
        "table": "task_links", "uid": uid, "hlc": "9999999999998-0000-worker-1", "fields": {}}])
    assert res["results"][0]["status"] == "applied"
    _other(svc, "readd", "task_links", uid, {"parent_id": "t_foreign", "child_id": child})
    ksc._pull_replica(BOARD, svc.hub(ME), ME)
    shadow = ksc.open_shadow(BOARD, ME)
    try:
        with kbc.write_txn(live):
            live.execute("UPDATE kanban_sync_state SET cursor = ?", (sync.sync_state(shadow)["cursor"],))
    finally:
        shadow.close()
    _round(kanban_home, svc)
    assert kb.parent_states(live, child) == [("t_foreign", "ready")]
