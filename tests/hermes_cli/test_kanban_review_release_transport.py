"""Real gateway stores and feed/pickup, with an in-memory Board service."""
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


@pytest.mark.parametrize("transport", ["feed", "snapshot"])
@pytest.mark.parametrize("missing", ["parent", "github", "foreign_origin"])
def test_home_review_receipt_crosses_feed_and_pickup(gws, clock, monkeypatch, missing, transport):
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
    kb.recompute_ready(hub.conn)
    owner_uid = hub.conn.execute("SELECT uid FROM task_runs WHERE id=?", (owner_run,)).fetchone()[0]
    assert owner_uid
    assert dispatch.check_respawn_guard(hub.conn, task) is None
    hub.round()
    worker.enter()
    if transport == "snapshot":
        from hermes_cli import kanban_sync_client as client
        replica = client.open_shadow("default", worker.name)
        try:
            client.resync(replica, worker.svc.hub(worker.name), "default", observers=False)
        finally:
            replica.close()
    else:
        worker.round()
    worker.enter()
    # Occupy local integer ids before pickup; portable identity must be used.
    dummy = kb.create_task(worker.conn, title="local unrelated", assignee="default")
    assert kb.claim_task(worker.conn, dummy)
    from hermes_cli import kanban_worker_node as wn
    wn.pickup(worker.conn, budget=10)
    row = worker.conn.execute("SELECT id,uid FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    assert row and row["id"] != owner_run
    assert row["uid"] == owner_uid
    if missing == "github":
        monkeypatch.setattr(prs, "pr_state", lambda _: None)
    else:
        assert worker.conn.execute("SELECT 1 FROM task_runs WHERE task_id=?", (parent,)).fetchone() is None
        from hermes_cli import kanban_dependency_review as review
        monkeypatch.setattr(review, "_completed_review_run", lambda *a: None)
    if missing == "foreign_origin":
        from hermes_cli import kanban_db_sync as sync
        from hermes_cli import kanban_sync_client as client
        from hermes_cli import kanban_review_release as release
        event = worker.conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='promoted' ORDER BY id DESC LIMIT 1", (task,)).fetchone()
        fields = client._row_fields(worker.conn, "task_events", event)
        assert "origin_gateway" not in fields
        fields["origin_gateway"] = "hub"  # Wire fields cannot override transport provenance.
        forged = sync.RemoteChange(0, "task_events", event["uid"], "upsert",
                                   fields, 0, "", "foreign")
        sync._upsert_row(worker.conn, forged, "worker", sync.ApplyResult(cursor=0))
        assert not release.released(worker.conn, task, {PR, OLD})
        return
    assert dispatch.check_respawn_guard(worker.conn, task) is None
    assert kb.claim_task(worker.conn, task)
    assert dispatch.check_respawn_guard(worker.conn, task) == "active_pr"
