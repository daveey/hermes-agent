"""Lane valve contracts through persisted config, dispatch/claims and flow."""
import copy

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli import kanban_lanes_cli as cli
from hermes_cli import kanban_lanes_flow as flow
from hermes_cli.kanban_db_dispatch_queries import lane_rows
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


@pytest.mark.parametrize("value", [-1, True, 1.5, "2"])
def test_worker_limit_validation(value):
    with pytest.raises(ValueError, match="max_workers"):
        lanes.parse({"lanes": {"other": {"max_workers": value}}})


def test_set_preserves_omitted_and_clears_explicit_null(conn, configured):
    cli.set_lane("babybear", max_workers=2, cards_per_day=4, usd_per_day=8)
    cli.set_lane("babybear", share=42)
    spec = lanes.policy().lanes["babybear"]
    assert (spec.max_workers, spec.cards_per_day, spec.usd_per_day) == (2, 4, 8)
    cli.set_lane("babybear", max_workers=None, cards_per_day=None, usd_per_day=None)
    spec = lanes.policy().lanes["babybear"]
    assert (spec.max_workers, spec.cards_per_day, spec.usd_per_day) == (None, None, None)


@pytest.mark.parametrize("review", [False, True])
def test_cap_filters_and_atomically_rechecks_claim(conn, configured, review):
    mapping = copy.deepcopy(base.LANES)
    mapping["babybear"]["max_workers"] = 1
    configured(mapping)
    first = base._ready_in(conn, "babybear", "first")
    second = base._ready_in(conn, "babybear", "second")
    other = base._ready_in(conn, "other", "other")
    status = "review" if review else "ready"
    if review:
        conn.execute("UPDATE tasks SET status='review' WHERE id IN (?, ?)", (first, second))
        conn.commit()
    claim = kb.claim_review_task if review else kb.claim_task
    assert first in [row["id"] for row in lane_rows(conn, status)]
    assert claim(conn, first) is not None
    assert second not in [row["id"] for row in lane_rows(conn, status)]
    assert claim(conn, second) is None
    assert base._task(conn, second)["status"] == status
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (second,)).fetchone()[0] == 0
    assert kb.claim_task(conn, other) is not None
    conn.execute("UPDATE tasks SET status='done', claim_lock=NULL WHERE id=?", (first,))
    conn.commit()
    assert claim(conn, second) is not None


@pytest.mark.parametrize("shadow,limit,allowed", [(False, 0, False), (False, None, True), (True, 0, True)])
def test_zero_unlimited_shadow_and_default_lane(conn, configured, shadow, limit, allowed):
    mapping = copy.deepcopy(base.LANES)
    mapping["other"]["max_workers"] = limit
    configured(mapping, lane_shadow=shadow)
    tid = base._ready_in(conn, "legacy-name", "unknown tenant")
    assert (kb.claim_task(conn, tid) is not None) is allowed


def test_cli_unset_and_omission(conn, configured):
    from hermes_cli.kanban_parser import build_parser
    import argparse
    parser = build_parser(argparse.ArgumentParser().add_subparsers())
    cli._set(parser.parse_args(["lanes", "set", "babybear", "--cards-per-day", "2", "--usd-per-day", "3", "--max-workers", "1"]))
    cli._set(parser.parse_args(["lanes", "set", "babybear", "--share", "40"]))
    assert lanes.policy().lanes["babybear"].cards_per_day == 2
    cli._set(parser.parse_args(["lanes", "set", "babybear", "--unset", "cards_per_day", "--unset", "usd_per_day", "--unset", "max_workers"]))
    spec = lanes.policy().lanes["babybear"]
    assert (spec.cards_per_day, spec.usd_per_day, spec.max_workers) == (None, None, None)
    with pytest.raises(ValueError, match="set and unset"):
        cli._set(parser.parse_args(["lanes", "set", "babybear", "--max-workers", "1", "--unset", "max_workers"]))


@pytest.mark.parametrize("marked", ["urgent", "capacity", "regular"])
def test_zero_cap_has_no_priority_or_alarm_exception(conn, configured, marked):
    mapping = copy.deepcopy(base.LANES)
    mapping["babybear"]["max_workers"] = 0
    configured(mapping)
    tid = base._ready_in(conn, "babybear", "alarm")
    conn.execute("UPDATE tasks SET admission_class=?, idempotency_key='alarm:example' WHERE id=?", (marked, tid))
    conn.commit()
    assert tid not in [row["id"] for row in lane_rows(conn, "ready")]
    assert kb.claim_task(conn, tid) is None


def test_flow_limits_and_all_open_blockers(conn, configured, monkeypatch):
    from hermes_cli import kanban_lanes_flow_pr
    monkeypatch.setattr(kanban_lanes_flow_pr.Reader, "prefetch", lambda *_: None)
    cli.set_lane("babybear", max_workers=3)
    parent = base._ready_in(conn, "babybear", "parent")
    parent2 = base._ready_in(conn, "other", "parent2")
    conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", ("https://github.com/example/repo/pull/42", parent))
    conn.commit()
    child = kb.create_task(conn, title="child", assignee="default", parents=[parent, parent2], tenant="babybear")
    data = flow.snapshot(conn)
    assert data["lanes"]["babybear"]["limits"]["max_workers"] == 3
    card = next(c for c in data["cards"] if c["id"] == child)
    assert card["reason"] == "dep"
    assert {b["id"] for b in card["blockers"]} == {parent, parent2}
    assert next(b["pr"] for b in card["blockers"] if b["id"] == parent) == "https://github.com/example/repo/pull/42"
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    conn.commit()
    card = next(c for c in flow.snapshot(conn)["cards"] if c["id"] == child)
    assert card["blockers"] == [{"id": parent2, "pr": None}]
