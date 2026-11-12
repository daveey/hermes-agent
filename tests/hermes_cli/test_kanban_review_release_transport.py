"""Real gateway stores and feed/pickup, with an in-memory Board service."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_operator_resume_worker_copy as helpers
from tests.hermes_cli import test_kanban_worker_node as support
from tests.hermes_cli.kanban_guard_helpers import worker_comment

svc = support.svc
gws = support.gws
clock = helpers.clock
PR = "https://github.com/example/repo/pull/9003"
OLD = PR + "9"


@pytest.mark.parametrize("missing", ["parent", "github"])
def test_home_review_receipt_crosses_feed_and_pickup(gws, clock, monkeypatch, missing):
    hub, worker = gws
    hub.enter()
    task = kb.create_task(hub.conn, title="PR owner", assignee="default", runs_on=[])
    assert kb.claim_task(hub.conn, task)
    owner_run = kb.get_task(hub.conn, task).current_run_id
    worker_comment(hub.conn, task, "default", PR + " " + OLD)
    parent = kb.create_task(hub.conn, title="review", assignee="default", runs_on=["machine:hub"])
    kb.link_tasks(hub.conn, parent, task, expected_child_run_id=owner_run)
    clock[0] += 10
    assert kb.block_task(hub.conn, task, kind="dependency", reason="review", expected_run_id=owner_run)
    assert kb.claim_task(hub.conn, parent)
    reviewer_run = kb.get_task(hub.conn, parent).current_run_id
    clock[0] += 10
    monkeypatch.setattr(prs, "pr_state", lambda u: "CLOSED" if u == OLD else "OPEN")
    assert kb.complete_task(hub.conn, parent, summary="review", expected_run_id=reviewer_run,
                            metadata={"verdict": "APPROVE", "reviewed_pr": PR, "reviewed_sha": "a" * 40})
    monkeypatch.setattr(prs, "pr_state", lambda u: "CLOSED" if u == OLD else "OPEN")
    kb.recompute_ready(hub.conn)
    event = hub.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='promoted' ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    stamp = json.loads(event[0])["review_release"]
    assert stamp["park"]["run_uid"]
    hub.round()
    worker.round()
    worker.enter()
    # Occupy local integer ids before pickup; portable identity must be used.
    dummy = kb.create_task(worker.conn, title="local unrelated", assignee="default")
    assert kb.claim_task(worker.conn, dummy)
    from hermes_cli import kanban_worker_node as wn
    wn.pickup(worker.conn, budget=10)
    row = worker.conn.execute("SELECT id,uid FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    assert row and row["id"] != owner_run
    assert row["uid"] == stamp["park"]["run_uid"]
    if missing == "github":
        monkeypatch.setattr(prs, "pr_state", lambda _: None)
    else:
        assert worker.conn.execute("SELECT 1 FROM task_runs WHERE task_id=?", (parent,)).fetchone() is None
        from hermes_cli import kanban_dependency_review as review
        monkeypatch.setattr(review, "_completed_review_run", lambda *a: None)
    assert dispatch.check_respawn_guard(worker.conn, task) is None
    assert kb.claim_task(worker.conn, task)
    assert dispatch.check_respawn_guard(worker.conn, task) == "active_pr"
