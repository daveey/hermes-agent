"""Only the home gateway maintains readiness on retained worker-node copies."""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_cross_gateway_parents import (
    _events, kanban_home, live, svc,
)
from tests.hermes_cli.test_kanban_sync_copy_links import (
    _foreign_child_of_done_parent, _import,
)

__all__ = ["kanban_home", "live", "svc"]


@pytest.mark.parametrize("status", ["todo", "blocked", "scheduled", "triage"])
def test_retained_foreign_copy_is_not_promoted(live, svc, kanban_home, status):
    _foreign_child_of_done_parent(svc, kanban_home, status=status)
    _import(live, "t_kid")
    if status == "triage":
        with kbc.write_txn(live):
            live.execute("UPDATE tasks SET block_kind = 'transient' WHERE id = 't_kid'")
            kb._append_event(live, "t_kid", "gave_up", None)
    assert kb.parent_states(live, "t_kid") == [("t_done", "done")]
    assert kb.recompute_ready(live) == 0
    assert kb.get_task(live, "t_kid").status == status
    assert not _events(live, "t_kid", "promoted")


def test_retained_foreign_copy_does_not_maintain_missing_parent(live, svc, kanban_home):
    _foreign_child_of_done_parent(svc, kanban_home, status="todo")
    _import(live, "t_kid")
    with kbc.write_txn(live):
        live.execute("UPDATE task_links SET parent_id = 't_missing' WHERE child_id = 't_kid'")
    kb.recompute_ready(live)
    assert not _events(live, "t_kid", "parent_missing")
    # A streak imported from the owner must not be expired on the worker either.
    with kbc.write_txn(live):
        kb._append_event(live, "t_kid", "parent_missing", {"parents": ["t_missing"]})
        live.execute("UPDATE task_events SET created_at = 1 WHERE task_id = 't_kid' AND kind = 'parent_missing'")
    kb.recompute_ready(live)
    assert kb.get_task(live, "t_kid").status == "todo"
    assert not _events(live, "t_kid", "blocked")


@pytest.mark.parametrize("parent_status", [None, "running", "done"])
def test_foreign_missing_streak_is_neither_expired_nor_cleared(live, svc, kanban_home, parent_status):
    _foreign_child_of_done_parent(svc, kanban_home, status="todo")
    _import(live, "t_kid")
    with kbc.write_txn(live):
        if parent_status is None:
            live.execute("UPDATE task_links SET parent_id = 't_missing' WHERE child_id = 't_kid'")
        elif parent_status == "running":
            live.execute("UPDATE kanban_sync_remote_parents SET status = 'running'")
        kb._append_event(live, "t_kid", "parent_missing", {"parents": ["t_done"]})
        live.execute("UPDATE task_events SET created_at = 1 WHERE task_id = 't_kid' AND kind = 'parent_missing'")
    before = [tuple(r) for r in live.execute("SELECT * FROM task_events WHERE task_id = 't_kid'")]
    kb.recompute_ready(live)
    assert kb.get_task(live, "t_kid").status == "todo"
    assert [tuple(r) for r in live.execute("SELECT * FROM task_events WHERE task_id = 't_kid'")] == before


@pytest.mark.parametrize("created_on", [None, "macbook"])
def test_home_and_legacy_cards_still_promote(live, created_on):
    child = kb.create_task(live, title="local readiness control", assignee="worker")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'todo', created_on = ? WHERE id = ?", (created_on, child))
    assert kb.recompute_ready(live) == 1
    assert kb.get_task(live, child).status == "ready"
    assert len(_events(live, child, "promoted")) == 1


def test_owner_promotes_retained_copy_then_worker_can_claim(live, svc, kanban_home):
    from hermes_cli import kanban_db_sync as sync, kanban_sync_client as sc
    from tests.hermes_cli.test_kanban_cross_gateway_parents import BOARD, ME, OTHER, _round

    owner = kbc.connect(kanban_home / "owner.db")
    try:
        sc.ensure_identity(owner, BOARD, OTHER)
        sync.set_mode(owner, "primary")
        parent = kb.create_task(owner, title="owner parent", assignee="worker")
        child = kb.create_task(owner, title="owner child", assignee="worker", parents=[parent])
        with kbc.write_txn(owner):
            owner.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        sc.push(owner, svc.hub(OTHER), BOARD, OTHER, primary=True)
        _round(kanban_home, svc)
        _import(live, child)
        assert kb.recompute_ready(live) == 0
        assert kb.recompute_ready(owner) == 1
        sc.push(owner, svc.hub(OTHER), BOARD, OTHER, primary=True)
        sc.pull(live, svc.hub(ME), BOARD, observers=False)
        assert kb.get_task(live, child).status == "ready"
        assert len(_events(live, child, "promoted")) == 1
        claimed = kb.claim_task(live, child)
        assert claimed is not None, sync.pop_claim_deferral(child)
        assert claimed.status == "running"
    finally:
        owner.close()
