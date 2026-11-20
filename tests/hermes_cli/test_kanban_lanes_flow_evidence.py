"""Persisted evidence paths, rather than classifier-only dictionaries."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes_flow as flow
from hermes_cli import kanban_lanes_flow_pr as prs
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person
URL = "https://github.com/example/repo/pull/1"


def card(conn, tid):
    return next(c for c in flow.snapshot(conn)["cards"] if c["id"] == tid)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(prs.Reader, "get", lambda self, url: {})


@pytest.mark.parametrize("kind,step", [("ci", "ci"), ("deploy", "deploy")])
def test_actual_scheduled_event_supplies_awaiting(conn, configured, kind, step):
    tid = base._ready_in(conn, "babybear", kind)
    conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
    kb._append_event(conn, tid, "scheduled", {"awaiting": {"kind": kind, "ref": URL}})
    conn.commit()
    assert card(conn, tid)["step"] == step


def test_changes_requested_invalidates_old_review(conn, configured):
    tid = base._ready_in(conn, "babybear", "rework")
    kb._append_event(conn, tid, "review_requested", {})
    kb._append_event(conn, tid, "changes_requested", {})
    conn.commit()
    assert card(conn, tid)["step"] == "ready"
    assert card(conn, tid)["reason"] == "slot"


def test_creator_task_without_created_by_is_automation(conn, configured):
    parent = base._ready_in(conn, "babybear", "owner")
    tid = kb.create_task(conn, title="child", assignee="default", creator_task_id=parent)
    assert card(conn, tid)["source"] == {"kind": "automation", "id": parent, "project": None}


@pytest.mark.parametrize("observation,step", [
    ({"merged": False, "approved": True, "checks": "green"}, "merge"),
    ({"merged": False, "checks": "pending"}, "ci"),
    ({"merged": True}, "deploy"),
    ({}, "landing_unclassified"),
])
def test_report_uses_pr_observation(conn, configured, monkeypatch, observation, step):
    tid = base._ready_in(conn, "babybear", "landing")
    kb._append_event(conn, tid, "review_requested", {"metadata": {"pr": URL}})
    kb._append_event(conn, tid, "changes_requested", {})
    monkeypatch.setattr(prs.Reader, "get", lambda self, url: observation)
    conn.commit()
    assert card(conn, tid)["step"] == step


def test_live_verification_requires_explicit_deploy_readback(conn, configured):
    tid = base._ready_in(conn, "babybear", "verify")
    conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
    kb._append_event(conn, tid, "scheduled", {"awaiting": {"kind": "deploy", "ref": URL},
                                           "metadata": {"flow_pr": {"url": URL, "merged": True,
                                                                     "deployed": True, "verified": False}}})
    conn.commit()
    assert card(conn, tid)["step"] == "test"


def test_old_limit_reason_not_carried_after_admit(conn, configured):
    from tests.hermes_cli.test_kanban_lane_daily_limits import limits
    limits(configured, "hermes", cards_per_day=0)
    tid = kb.create_task(conn, title="gate", assignee="default", created_by="self-heal")
    kb._append_event(conn, tid, "lane_gated", {"reason": "limit:cards_per_day"})
    from hermes_cli import kanban_lanes as lanes
    assert lanes.admit(conn, tid, actor="user", reason="approved", budget=False)
    assert card(conn, tid)["reason"] == "slot"


def test_no_lane_policy_still_reports_open_cards(conn, monkeypatch):
    from hermes_cli import kanban_lanes as lanes
    monkeypatch.setattr(lanes, "safe_policy", lambda: None)
    tid = kb.create_task(conn, title="unconfigured", assignee="default")
    assert card(conn, tid)["lane"] == "other"


def test_expired_limit_on_ordinary_gated_lane_reports_gate(conn, configured):
    from tests.hermes_cli.test_kanban_lane_daily_limits import limits
    limits(configured, "hermes", cards_per_day=0)
    tid = kb.create_task(conn, title="held", assignee="default", created_by="self-heal")
    assert card(conn, tid)["reason"] == "limit:cards_per_day"
    configured()
    assert card(conn, tid)["reason"] == "gate"
