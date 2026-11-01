"""Unresolved dependencies must refuse atomically with a remote-wait remedy."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_cross_gateway_parents import local

__all__ = ["local"]


def test_link_missing_parent_explains_cross_gateway_wait(local):
    child = kb.create_task(local, title="child", assignee="worker")
    before = kb.get_task(local, child)
    with pytest.raises(ValueError, match="awaiting=.*gateway"):
        kb.link_tasks(local, "t_foreign", child)
    assert kb.get_task(local, child) == before
    assert kb.parent_ids(local, child) == []


@pytest.mark.parametrize("state", ["running", "blocked"])
def test_dependency_block_refuses_dangling_edge_without_ending_run(local, state):
    child = kb.create_task(local, title="child", assignee="worker")
    kb.claim_task(local, child)
    with kbc.write_txn(local):
        local.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                      ("t_foreign", child))
        if state == "blocked":
            local.execute("UPDATE tasks SET status='blocked', current_run_id=NULL WHERE id=?", (child,))
    before = kb.get_task(local, child)
    runs = [tuple(r) for r in local.execute("SELECT * FROM task_runs")]
    events = [tuple(r) for r in local.execute("SELECT * FROM task_events")]
    with pytest.raises(ValueError, match="awaiting=.*gateway"):
        kb.block_task(local, child, kind="dependency", reason="Wait for foreign owner",
                      comment=("worker", "Must roll back on refusal"))
    assert kb.get_task(local, child) == before
    assert [tuple(r) for r in local.execute("SELECT * FROM task_runs")] == runs
    assert [tuple(r) for r in local.execute("SELECT * FROM task_events")] == events
    assert not local.execute("SELECT 1 FROM task_comments WHERE task_id=?", (child,)).fetchone()


def test_local_dependency_still_blocks_and_releases(local):
    parent = kb.create_task(local, title="parent", assignee="worker")
    child = kb.create_task(local, title="child", assignee="worker")
    kb.claim_task(local, child)
    run = kb.get_task(local, child).current_run_id
    kb.link_tasks(local, parent, child, expected_child_run_id=run)
    assert kb.block_task(local, child, kind="dependency", expected_run_id=run)
    assert kb.get_task(local, child).status == "todo"
    kb.claim_task(local, parent)
    assert kb.complete_task(local, parent, result="finished")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "ready"


def test_synced_parent_with_known_state_can_still_gate_dependency_block(local):
    from hermes_cli import kanban_db_sync as sync

    child = kb.create_task(local, title="child", assignee="worker")
    kb.claim_task(local, child)
    local.executescript(sync.SYNC_SCHEMA_SQL)
    with kbc.write_txn(local):
        local.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                      ("t_remote", child))
        local.execute("INSERT INTO kanban_sync_remote_parents (parent_id, status, refreshed_at) "
                      "VALUES ('t_remote', 'running', 1)")
    assert kb.get_task(local, "t_remote") is None
    assert kb.block_task(local, child, kind="dependency")
    assert kb.get_task(local, child).status == "todo"
    with kbc.write_txn(local):
        local.execute("UPDATE kanban_sync_remote_parents SET status='done'")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "ready"
