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


@pytest.mark.parametrize("entry", [None, {"hold": False, "metrics": {"active_card_runs": 1}}])
def test_registry_evidence_changes_only_when_fields_change(monkeypatch, entry):
    clock = [1201000]
    registry = {"machines": {support.WORKER: entry}}
    monkeypatch.setattr(pickup, "_now_ms", lambda now=None: clock[0])
    monkeypatch.setattr(support.wn, "registry", lambda: registry)
    before = pickup._registry_line(support.WORKER)
    clock[0] += 1000
    assert pickup._registry_line(support.WORKER) == before
    registry["machines"][support.WORKER] = {"hold": True, "metrics": {"active_card_runs": 2}}
    after = pickup._registry_line(support.WORKER)
    assert after != before
    assert "hold=True" in after and "active_card_runs=2" in after


def test_ticks_ignore_elapsed_but_keep_material_changes(gws):
    _, worker = gws
    worker.enter()
    card = {"id": "t_0123456789abcdef", "title": "Pending task", "tenant": None,
            "since_ms": 1000, "skip": {"reason": "other", "detail": "not eligible",
                                      "first_at": 1000, "last_at": 1000, "passes": 1}}
    first = pickup.file_repairs(worker.conn, support.WORKER, [card], now=1200)[0]
    for tick in range(1, 20):
        card["skip"].update(last_at=(1200 + tick * 60) * 1000, passes=tick + 1)
        assert pickup.file_repairs(worker.conn, support.WORKER, [card], now=1200 + tick * 60) == [first]
    assert len(kb.list_comments(worker.conn, first)) == 1
    card["skip"]["detail"] = "capability changed"
    pickup.file_repairs(worker.conn, support.WORKER, [card], now=2500)
    assert len(kb.list_comments(worker.conn, first)) == 2
    card["skip"]["detail"] = "not eligible"
    pickup.file_repairs(worker.conn, support.WORKER, [card], now=2560)
    assert len(kb.list_comments(worker.conn, first)) == 3


def test_host_ticks_ignore_registry_clock_and_report_new_targets(gws, monkeypatch):
    _, worker = gws
    worker.enter()
    card = {"id": "t_0123456789abcdef", "title": "Pending task", "tenant": None,
            "since_ms": 1000, "skip": {"reason": "budget_exhausted", "detail": "no slot",
                                      "first_at": 1000, "last_at": 1000, "passes": 1}}
    first = pickup.file_repairs(worker.conn, support.WORKER, [card], now=1200)[0]
    for tick in range(1, 20):
        monkeypatch.setattr(pickup, "_now_ms", lambda now=None, tick=tick: (1200 + tick * 60) * 1000)
        card["skip"].update(last_at=(1200 + tick * 60) * 1000, passes=tick + 1)
        assert pickup.file_repairs(worker.conn, support.WORKER, [card], now=1200 + tick * 60) == [first]
    assert len(kb.list_comments(worker.conn, first)) == 1
    changed = {**card, "id": "t_fedcba9876543210"}
    pickup.file_repairs(worker.conn, support.WORKER, [changed], now=2500)
    assert len(kb.list_comments(worker.conn, first)) == 2
    assert "t_fedcba9876543210" in kb.list_comments(worker.conn, first)[-1].body


def test_count_fluctuations_only_report_cap_crossings(gws, monkeypatch):
    _, worker = gws
    worker.enter()
    registry = support.wn.registry()
    entry = registry["machines"][support.WORKER]
    entry.update(hold=False, max_card_runs=14, metrics={"active_card_runs": 1})
    monkeypatch.setattr(support.wn, "registry", lambda: registry)
    card = {"id": "t_0123456789abcdef", "title": "Pending task", "tenant": None,
            "since_ms": 1000, "skip": {"reason": "budget_exhausted", "detail": "no slot",
                                      "first_at": 1000, "last_at": 1000, "passes": 1}}
    first = pickup.file_repairs(worker.conn, support.WORKER, [card], now=1200)[0]
    for tick in range(20):
        entry["metrics"]["active_card_runs"] = tick % 14
        pickup.file_repairs(worker.conn, support.WORKER, [card], now=1260 + tick * 60)
    assert len(kb.list_comments(worker.conn, first)) == 1
    for count, expected in [(14, 2), (15, 2), (20, 2), (13, 3), (0, 3)]:
        entry["metrics"]["active_card_runs"] = count
        pickup.file_repairs(worker.conn, support.WORKER, [card], now=2500)
        assert len(kb.list_comments(worker.conn, first)) == expected
    card["id"] = "t_fedcba9876543210"
    pickup.file_repairs(worker.conn, support.WORKER, [card], now=2560)
    assert len(kb.list_comments(worker.conn, first)) == 4
    entry["hold"] = True
    pickup.file_repairs(worker.conn, support.WORKER, [card], now=2620)
    assert len(kb.list_comments(worker.conn, first)) == 5
    entry["max_card_runs"] = 15
    pickup.file_repairs(worker.conn, support.WORKER, [card], now=2680)
    assert len(kb.list_comments(worker.conn, first)) == 6


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
