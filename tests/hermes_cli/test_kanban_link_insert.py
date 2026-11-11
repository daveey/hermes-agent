"""Internal graph writers must not bypass endpoint validation."""
import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_cross_gateway_parents as fixtures

local = fixtures.local


@pytest.mark.parametrize("missing", ["parent", "child", "both"])
def test_internal_link_refuses_unknown_endpoints_atomically(local, missing):
    parent = kb.create_task(local, title="parent", assignee="worker")
    child = kb.create_task(local, title="child", assignee="worker")
    parent_id = "t_missing_parent" if missing in {"parent", "both"} else parent
    child_id = "t_missing_child" if missing in {"child", "both"} else child
    before = [tuple(row) for row in local.execute("SELECT * FROM task_events")]
    with pytest.raises(ValueError, match="unknown task"):
        with kb.write_txn(local):
            kb._link(local, parent_id, child_id)
            kb._append_event(local, child, "linked", {"parent": parent_id})
    assert list(local.execute("SELECT * FROM task_links")) == []
    assert [tuple(row) for row in local.execute("SELECT * FROM task_events")] == before


def test_internal_link_accepts_uncommitted_endpoints_and_exact_retry(local):
    with kb.write_txn(local):
        parent = kb.create_task(local, title="parent", assignee="worker")
        child = kb.create_task(local, title="child", assignee="worker")
        kb._link(local, parent, child)
        kb._link(local, parent, child)
    assert kb.parent_ids(local, child) == [parent]
