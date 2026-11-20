"""Persisted effective-lane and dispatch-episode regressions."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli import kanban_lanes_flow as flow
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


@pytest.mark.parametrize("legacy", [False, True])
def test_default_cap_counts_unknown_and_explicit_lanes(conn, configured, monkeypatch, legacy):
    clock = [lanes.day_start() + 3600]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    mapping = json.loads(json.dumps(base.LANES))
    mapping["other"]["cards_per_day"] = 2
    configured(mapping)
    first = kb.create_task(conn, title="legacy", assignee="default", tenant="legacy-project", created_by="user")
    if legacy:
        conn.execute("UPDATE task_events SET payload=json_remove(payload, '$.admitted') WHERE task_id=? AND kind=?",
                     (first, lanes.ASSIGNED))
        conn.commit()
    second = kb.create_task(conn, title="explicit", assignee="default", tenant="other", created_by="user")
    held = kb.create_task(conn, title="unknown", assignee="default", tenant="another-project", created_by="user")
    assert base._task(conn, second)["status"] == "ready"
    assert base._task(conn, held)["status"] == "triage"
    report = flow.snapshot(conn)
    assert report["lanes"]["other"]["usage"]["cards_today"] == 2
    assert next(c for c in report["cards"] if c["id"] == held)["reason"] == "limit:cards_per_day"
    clock[0] += 86400
    kb.recompute_ready(conn)
    assert base._task(conn, held)["status"] == "ready"
    assert lanes.cards_today(conn, "other") == 1


@pytest.mark.parametrize("boundary", ["claimed", "spawned", "worker_bound", "completed", "reopened", "dispatch_skipped"])
def test_old_quota_skip_does_not_cross_episode(conn, configured, boundary):
    configured()
    tid = base._ready_in(conn, "babybear", "work")
    kb._append_event(conn, tid, "dispatch_skipped", {"reason": "llm_paced"})
    kb._append_event(conn, tid, boundary, {"reason": "unrelated"})
    conn.commit()
    card = next(c for c in flow.snapshot(conn)["cards"] if c["id"] == tid)
    assert card["reason"] == "slot"


@pytest.mark.parametrize("skip", ["llm_paced", "waiting_for_worker_capacity"])
def test_running_precedes_stale_skip(conn, configured, skip):
    configured()
    tid = base._ready_in(conn, "babybear", "work")
    kb._append_event(conn, tid, "dispatch_skipped", {"reason": skip})
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    conn.commit()
    card = next(c for c in flow.snapshot(conn)["cards"] if c["id"] == tid)
    assert (card["step"], card["reason"]) == ("running", "run")


def test_unknown_tenant_consumes_default_lane_cap(conn, configured):
    mapping = json.loads(json.dumps(base.LANES))
    mapping["other"]["cards_per_day"] = 1
    configured(mapping)
    first = kb.create_task(conn, title="first", assignee="default", tenant="legacy-project", created_by="user")
    second = kb.create_task(conn, title="second", assignee="default", tenant="legacy-project", created_by="user")
    assert base._task(conn, first)["status"] == "ready"
    assert base._task(conn, second)["status"] == "triage"


def test_running_card_does_not_keep_historical_quota_reason(conn, configured):
    configured()
    tid = base._ready_in(conn, "babybear", "running")
    kb._append_event(conn, tid, "dispatch_skipped", {"reason": "llm_paced"})
    kb._append_event(conn, tid, "claimed", {})
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    conn.commit()
    card = next(c for c in flow.snapshot(conn)["cards"] if c["id"] == tid)
    assert card["step"] == "running"
    assert card["reason"] == "run"


@pytest.mark.parametrize("skip,expected", [("llm_paced", "quota"), ("waiting_for_worker_capacity", "slot")])
def test_current_episode_retains_skip(conn, configured, skip, expected):
    configured()
    tid = base._ready_in(conn, "babybear", "work")
    kb._append_event(conn, tid, "claimed", {})
    kb._append_event(conn, tid, "dispatch_skipped", {"reason": skip})
    conn.commit()
    card = next(c for c in flow.snapshot(conn)["cards"] if c["id"] == tid)
    assert card["reason"] == expected
