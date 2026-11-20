"""Release provenance, independent budgets and known red CI regressions."""
import json
import time

import pytest

from agent import delegation_context
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli.kanban_lanes_flow import classify_step
from tests.hermes_cli import test_kanban_lane_daily_limits as daily_tests
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


@pytest.mark.parametrize("env", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
@pytest.mark.parametrize("cap", ["cards_per_day", "usd_per_day"])
@pytest.mark.parametrize("room", ["midnight", "raise_limit"])
def test_freed_cap_preserves_creatorless_detector_gate(conn, configured, monkeypatch, env, cap, room):
    clock = [lanes.day_start() + 3600]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    daily_tests.limits(configured, "hermes", **{cap: 1})
    parent = base._hermes_parent(conn)
    if cap == "usd_per_day":
        kb._append_event(conn, parent, "worker_result", {"usage": {"cost_usd": 1}})
        conn.commit()
    tid = kb.create_task(conn, title="Independent review", assignee="default", created_by="self-heal")
    assert base._task(conn, tid)["status"] == "triage"
    if room == "midnight":
        clock[0] += 86400
    else:
        daily_tests.limits(configured, "hermes", **{cap: 10})
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    monkeypatch.setenv(env, parent)
    monkeypatch.setattr(delegation_context, "is_dispatcher_owned_worker_context", lambda: True)
    kb.recompute_ready(conn)
    assert base._task(conn, tid)["status"] == "triage"
    assert lanes.admitted_today(conn, "hermes") == 0


def test_automatic_release_does_not_spend_planner_budget(conn, configured):
    daily_tests.limits(configured, cards_per_day=0)
    tid = base._ready_in(conn, "babybear", "daily held")
    daily_tests.limits(configured, cards_per_day=10)
    kb.recompute_ready(conn)
    assert base._task(conn, tid)["status"] == "ready"
    assert lanes.cards_today(conn, "babybear") == 1
    assert lanes.admitted_today(conn, "babybear") == 0
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                         (tid, lanes.ADMITTED)).fetchone()
    assert json.loads(event[0])["budget"] == 0


@pytest.mark.parametrize("approved", [False, True])
def test_known_red_required_checks_are_ci(approved):
    assert classify_step({"status": "scheduled", "pr": {
        "merged": False, "approved": approved, "checks": "red"}}) == "ci"
