"""Pickup watcher firings retain one open subject and append new evidence."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pickup_stall as pickup
from tests.hermes_cli import test_kanban_pickup_stall as support

svc = support.svc
gws = support.gws
alarm_worker_pool = support.alarm_worker_pool


def fire(worker, subject="queue-full"):
    worker.enter()
    return pickup._create(
        worker.conn, key=f"pickup-stall:{support.WORKER}:{subject}:",
        title=f"Pickup incident {subject}", body="Still stalled: fresh observation",
        tenant=None, own=support.WORKER, now_ms=pickup._now_ms(),
    )


def test_repeat_appends_one_comment(gws):
    _, worker = gws
    first = fire(worker)
    assert fire(worker) == first
    assert len(support._repairs(worker.conn)) == 1
    comments = kb.list_comments(worker.conn, first)
    assert len(comments) == 1
    assert "fresh observation" in comments[0].body
    assert fire(worker) == first
    assert len(kb.list_comments(worker.conn, first)) == 1
    from hermes_cli.kanban_pickup_alarm_identity import refusal
    task = kb.get_task(worker.conn, first)
    identity = {"idempotency_key": task.idempotency_key, "created_by": task.created_by,
                "created_on": support.WORKER}
    assert refusal(identity, support.WORKER) == "pickup_stall_source"
    assert refusal(identity, "worker-2") is None


def test_distinct_subjects_have_distinct_incidents(gws):
    _, worker = gws
    assert fire(worker) != fire(worker, "hold")
    assert len(support._repairs(worker.conn)) == 2


@pytest.mark.parametrize("status", ["done", "archived"])
def test_closed_incident_is_replaced_immediately(gws, status):
    _, worker = gws
    first = fire(worker)
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, first))
    assert fire(worker) != first
    assert len(support._repairs(worker.conn)) == 2
    assert not kb.list_comments(worker.conn, first)
