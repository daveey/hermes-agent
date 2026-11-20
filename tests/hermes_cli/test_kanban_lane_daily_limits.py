"""Daily caps exercise persisted admissions and the dispatcher maintenance seam."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli import kanban_lanes_plan as plan
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


def limits(configured, lane="babybear", **values):
    mapping = json.loads(json.dumps(base.LANES))
    mapping[lane].update(values)
    configured(mapping)


@pytest.mark.parametrize("cap", ["cards_per_day", "usd_per_day"])
def test_existing_limit_wait_released_by_recompute_after_midnight(conn, configured, monkeypatch, cap):
    clock = [lanes.day_start() + 3600]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    limits(configured, **{cap: 1})
    first = base._ready_in(conn, "babybear", "first")
    if cap == "usd_per_day":
        kb._append_event(conn, first, "worker_result", {"usage": {"cost_usd": 1}})
        conn.commit()
    held = base._ready_in(conn, "babybear", "held")
    assert base._task(conn, held)["status"] == "triage"
    kb.recompute_ready(conn)
    assert base._task(conn, held)["status"] == "triage"
    clock[0] += 86400
    kb.recompute_ready(conn)
    assert base._task(conn, held)["status"] == "ready"
    assert base._task(conn, held)["block_kind"] is None
    assert lanes.cards_today(conn, "babybear") == 1


@pytest.mark.parametrize("cap", ["cards_per_day", "usd_per_day"])
def test_admitted_parent_review_is_exempt_from_daily_caps(conn, configured, cap):
    parent = base._hermes_parent(conn)
    limits(configured, "hermes", **{cap: 0})
    review = kb.create_task(conn, title="Independent review", assignee="default", creator_task_id=parent)
    assert base._task(conn, review)["status"] == "ready"
    ordinary = kb.create_task(conn, title="new feature", assignee="default", creator_task_id=parent)
    assert base._task(conn, ordinary)["status"] == "triage"


@pytest.mark.parametrize("cap", ["cards_per_day", "usd_per_day"])
def test_planner_cannot_bypass_daily_limit(conn, configured, cap):
    limits(configured, "hermes", **{cap: 0})
    held = kb.create_task(conn, title="new work", assignee="default", created_by="self-heal")
    result = plan.apply(conn, [{"action": "admit", "card": held, "reason": "worth doing"}])
    assert not result["applied"]
    assert f"limit:{cap}" in result["rejected"][0]["why"]
    assert base._task(conn, held)["status"] == "triage"


def test_count_only_admissions_not_relanes_or_parked_creation(conn, configured):
    configured()
    first = base._ready_in(conn, "babybear", "first")
    kb.create_task(conn, title="parked", assignee="default", tenant="babybear", triage=True)
    lanes.assign(conn, first, "other", source="user", reason="move")
    assert lanes.cards_today(conn, "babybear") == 1
    assert lanes.cards_today(conn, "other") == 0
    held = kb.create_task(conn, title="held", assignee="default", created_by="self-heal")
    assert lanes.admit(conn, held, actor="user", reason="manual", budget=False)
    assert lanes.cards_today(conn, "hermes") == 1
    assert lanes.admitted_today(conn, "hermes") == 0


def test_reset_releases_only_available_capacity(conn, configured, monkeypatch):
    clock = [lanes.day_start() + 3600]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    limits(configured, cards_per_day=1)
    base._ready_in(conn, "babybear", "first")
    held = sorted(base._ready_in(conn, "babybear", str(i)) for i in range(2))
    clock[0] += 86400
    kb.recompute_ready(conn)
    assert [base._task(conn, t)["status"] for t in held] == ["ready", "triage"]
    kb.recompute_ready(conn)
    assert [base._task(conn, t)["status"] for t in held] == ["ready", "triage"]


def test_release_preserves_ordinary_gate_foreign_and_containment(conn, configured, monkeypatch):
    from hermes_cli import kanban_lane_daily_limits as daily
    from hermes_cli import kanban_containment_connections as containment

    limits(configured, cards_per_day=0)
    held = base._ready_in(conn, "babybear", "limit held")
    limits(configured, cards_per_day=1)
    with kb.write_txn(conn):
        assert daily.release_due(conn, foreign={held}) == 0
    original = containment.held
    monkeypatch.setattr(containment, "held", lambda c, tid: tid == held or original(c, tid))
    with kb.write_txn(conn):
        assert daily.release_due(conn) == 0
    monkeypatch.setattr(containment, "held", original)
    ordinary = kb.create_task(conn, title="ordinary gate", assignee="default", created_by="self-heal")
    kb.recompute_ready(conn)
    assert base._task(conn, held)["status"] == "ready"
    assert base._task(conn, ordinary)["status"] == "triage"


def test_planner_reserves_each_admission_in_same_batch(conn, configured):
    limits(configured, "hermes", cards_per_day=1)
    ids = [kb.create_task(conn, title=f"work {i}", assignee="default", created_by="self-heal") for i in range(2)]
    result = plan.apply(conn, [{"action": "admit", "card": tid, "reason": "valuable"} for tid in ids])
    assert len(result["applied"]) == 1
    assert result["rejected"][0]["why"] == "limit:cards_per_day"


def test_legacy_admission_event_counted_without_inventing_relanes(conn, configured):
    configured()
    tid = base._ready_in(conn, "babybear", "legacy")
    conn.execute("UPDATE task_events SET payload=json_remove(payload, '$.admitted') WHERE task_id=? AND kind=?",
                 (tid, lanes.ASSIGNED))
    conn.commit()
    assert lanes.cards_today(conn, "babybear") == 1
    lanes.assign(conn, tid, "other", source="user", reason="move")
    assert lanes.cards_today(conn, "other") == 0
