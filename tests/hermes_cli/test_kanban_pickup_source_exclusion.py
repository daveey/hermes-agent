"""Pool eligibility is independent of capacity and excludes the authenticated source."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_pickup_stall as stall
from hermes_cli import kanban_worker_node as wn


def alarm(**changes):
    return {"created_by": "pickup-stall-detector", "created_on": "worker-1",
            "idempotency_key": "pickup-stall:worker-1:queue-full:1794700000000",
            "workspace_kind": "scratch", "runs_on": json.dumps(["pool:worker"]), **changes}


def test_full_pool_free_hub_still_queues_without_machine_pin():
    labels, _ = stall.alarm_placement("worker-1", {
        "worker-1": {"max_card_runs": 1, "metrics": {"active_card_runs": 1}},
        "hub": {"state": "active", "pickup": "explicit", "labels": ["profile:default",
                "capability:terminal", "capability:file", "capability:kanban"],
                "max_card_runs": 7, "metrics": {"active_card_runs": 0}},
    }, assignee="default")
    assert set(labels) == {"pool:worker", "capability:hub-write", "profile:default",
                           "capability:terminal", "capability:file", "capability:kanban"}
    assert not any(label.startswith("machine:") for label in labels)


def test_unreadable_registry_queues_on_worker_pool():
    labels, _ = stall.alarm_placement("worker-1", None, assignee="default")
    assert "pool:worker" in labels
    assert not any(label.startswith("machine:") for label in labels)


@pytest.mark.parametrize("key", ["pickup-stall:worker-1:queue-full:1794700000000",
                                 "worker-resources-unknown:worker-1:1794700000000"])
def test_source_cannot_pick_up_even_after_capacity_returns(key):
    registry = {name: {"labels": ["pool:worker"], "max_card_runs": 2,
                       "metrics": {"active_card_runs": 0}} for name in ("worker-1", "worker-2")}
    assert wn.eligible(alarm(idempotency_key=key), "worker-1", registry) == "pickup_stall_source"
    assert wn.eligible(alarm(idempotency_key=key), "worker-2", registry) is None


@pytest.mark.parametrize("changes", [
    {"created_on": None}, {"created_on": "worker-3"}, {"created_by": "default"},
    {"idempotency_key": None}, {"idempotency_key": "pickup-stall:worker-1:queue-full:bad"},
    {"idempotency_key": "worker-resources-unknown:worker-3:1794700000000"},
    {"created_by": "default", "idempotency_key": "worker-resources-unknown:worker-1:1794700000000"},
])
def test_detector_like_rows_without_trusted_provenance_fail_closed(changes):
    registry = {"worker-2": {"labels": ["pool:worker"]}}
    assert wn.eligible(alarm(**changes), "worker-2", registry) == "pickup_stall_provenance"


def test_normal_cards_have_no_negative_placement_policy():
    registry = {"worker-1": {"labels": ["pool:worker"]}}
    assert wn.eligible(alarm(created_by="default", idempotency_key="ordinary"), "worker-1", registry) is None


@pytest.mark.parametrize("origin,reason", [("worker-1", "pickup_stall_source"),
                                          (None, "pickup_stall_provenance")])
def test_local_dispatch_refuses_without_labels_or_registry(monkeypatch, origin, reason):
    from hermes_cli import kanban_worker_placement as wp
    monkeypatch.setattr(wp, "local_refusals", lambda *a: {})
    monkeypatch.setattr(wn.sync, "board_mode", lambda *a: "primary")
    monkeypatch.setattr(wn, "_own", lambda *a: "worker-1")
    monkeypatch.setattr(wn, "registry", lambda: None)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id, runs_on, created_on, created_by, idempotency_key, "
                     "workspace_kind, status, claim_lock)")
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     ("alarm", None, origin, "pickup-stall-detector",
                      "pickup-stall:worker-1:queue-full:1794700000000", "scratch", "ready", None))
        assert wn.local_refusals(conn, "ready") == {"alarm": reason}

