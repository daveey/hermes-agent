"""Lane planner actions are validated one by one; the CLI is the user's control surface."""
import argparse
import json

import pytest

from hermes_cli import kanban, kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli import kanban_lanes_plan as plan
from hermes_cli.kanban_parser import build_parser
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


def cli(*args):
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(["kanban", *args]))


def _held(conn, parent, title="infra"):
    return kb.create_task(conn, title=title, assignee="default", creator_task_id=parent,
                          workspace_path="/src/hermes-agent")


def _why(result):
    return [item["why"] for item in result["rejected"]]


def test_admit_stops_at_the_daily_budget_and_never_admits_guard_cards(conn, configured):
    parent = base._hermes_parent(conn)
    cards = [_held(conn, parent, f"infra {i}") for i in range(3)]
    guard = _held(conn, parent, "Lifecycle guard false refusal")
    result = plan.apply(conn, [{"action": "admit", "card": c, "reason": "next"} for c in [guard, *cards]])
    assert [a["card"] for a in result["applied"]] == cards[:2]  # admit_per_day is 2
    assert "never auto-admits" in _why(result)[0] and "budget spent (2/2" in _why(result)[1]
    assert base._task(conn, cards[2])["status"] == "triage" and base._task(conn, guard)["status"] == "triage"


def test_rejects_unknown_actions_missing_reasons_and_archives(conn, configured):
    card = base._ready_in(conn, "babybear", "bb")
    result = plan.apply(conn, [
        {"action": "archive", "card": card, "reason": "tidy"},
        {"action": "reprioritize", "card": card, "priority": 500},
        {"action": "reprioritize", "card": card, "priority": 5000, "reason": "big"},
        {"action": "reprioritize", "card": "t_missing", "priority": 5, "reason": "x"},
        "not an object",
    ])
    assert not result["applied"] and len(result["rejected"]) == 5
    assert base._task(conn, card)["status"] == "ready"


def test_reprioritize_skips_pinned_cards(conn, configured):
    card = base._ready_in(conn, "babybear", "bb", priority=10)
    plan.set_pin(conn, card, True, actor="user")
    result = plan.apply(conn, [{"action": "reprioritize", "card": card, "priority": 700, "reason": "up"}])
    assert _why(result) == ["card is pinned"] and base._task(conn, card)["priority"] == 10
    plan.set_pin(conn, card, False, actor="user")
    assert plan.apply(conn, [{"action": "reprioritize", "card": card, "priority": 700, "reason": "up"}])["applied"]
    assert base._task(conn, card)["priority"] == 700


def test_relane_only_moves_cards_nothing_placed_and_never_through_a_gate(conn, configured):
    loose = kb.create_task(conn, title="something", assignee="default")
    given = base._ready_in(conn, "babybear", "bb")
    result = plan.apply(conn, [
        {"action": "relane", "card": loose, "lane": "babybear", "reason": "it is BB firmware"},
        {"action": "relane", "card": given, "lane": "other", "reason": "guess"},
        {"action": "relane", "card": loose, "lane": "hermes", "reason": "sneak past the gate"},
        {"action": "relane", "card": loose, "lane": "nope", "reason": "typo"},
    ])
    assert len(result["applied"]) == 1 and len(result["rejected"]) == 3
    assert base._task(conn, loose)["tenant"] == "babybear" and base._task(conn, given)["tenant"] == "babybear"


def test_merge_links_and_keeps_the_card_held(conn, configured):
    parent = base._hermes_parent(conn)
    dup = _held(conn, parent, "fix the watcher again")
    other_lane = base._ready_in(conn, "babybear", "bb")
    result = plan.apply(conn, [
        {"action": "merge", "card": dup, "into": other_lane, "reason": "same"},
        {"action": "merge", "card": dup, "into": parent, "reason": "same watcher fix"},
    ])
    assert _why(result) == ["merge target is in another lane"] and len(result["applied"]) == 1
    row = base._task(conn, dup)
    assert (row["status"], row["block_kind"]) == ("triage", lanes.GATE_KIND)
    assert conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?", (parent, dup)).fetchone()


def test_plan_over_the_action_limit_and_notes(conn, configured, monkeypatch):
    monkeypatch.setattr(plan, "MAX_ACTIONS", 2)
    result = plan.apply(conn, [{"action": "note", "lane": "babybear", "text": "no ready work"},
                               {"action": "note", "lane": "nope", "text": "x"},
                               {"action": "note", "lane": "hermes", "text": "late"}])
    assert result["notes"] == [{"lane": "babybear", "text": "no ready work"}] and len(result["rejected"]) == 2


def test_cli_show_set_admit_apply_round_trip(conn, configured, capsys, tmp_path):
    parent = base._hermes_parent(conn)
    held = _held(conn, parent)
    assert cli("lanes", "show", "--json") == 0
    snap = json.loads(capsys.readouterr().out)
    assert snap["lanes"]["hermes"]["held"] == 1 and snap["scope"] == "local"
    before = lanes.policy().lanes["babybear"].share
    assert cli("lanes", "set", "babybear", "--share", "60", "--pause") == 0
    lane = lanes.policy().lanes["babybear"]
    assert (lane.share, lane.paused) == (60.0, True)
    assert cli("lanes", "set", "babybear", "--share", str(before), "--resume") == 0
    lane = lanes.policy().lanes["babybear"]
    assert (lane.share, lane.paused) == (before, False)  # the previous values restore the previous split
    actions = tmp_path / "plan.json"
    actions.write_text(json.dumps([{"action": "admit", "card": held, "reason": "unblocks the hub"},
                                   {"action": "note", "lane": "babybear", "text": "nothing ready"}]))
    capsys.readouterr()
    assert cli("lanes", "apply", str(actions), "--json") == 0
    assert len(json.loads(capsys.readouterr().out)["applied"]) == 1
    assert base._task(conn, held)["status"] == "ready"
    assert plan.read_notes()["notes"] == [{"lane": "babybear", "text": "nothing ready"}]
    assert cli("lanes", "admit", held) == 1  # no longer held
    other = _held(conn, parent, "second")
    assert cli("lanes", "admit", other) == 0 and base._task(conn, other)["status"] == "ready"
    assert lanes.admitted_today(conn, "hermes") == 1  # the planner's admit; the user's own is outside the budget
    assert cli("lanes", "set", "nope", "--share", "1") == 2
    assert cli("lanes", "explain", held) == 0 and "lane hermes" in capsys.readouterr().out


def test_cli_create_is_classified(conn, configured, capsys):
    assert cli("create", "write the BB onboarding", "--assignee", "default", "--tenant", "babybear", "--json") == 0
    made = json.loads(capsys.readouterr().out)
    assert base._task(conn, made["id"])["tenant"] == "babybear"
    assert cli("create", "something loose", "--assignee", "default", "--json") == 0
    loose = json.loads(capsys.readouterr().out)["id"]
    assert base._task(conn, loose)["tenant"] == "other"
    assert cli("lanes", "unclassified", "--json") == 0
    assert [c["id"] for c in json.loads(capsys.readouterr().out)] == [loose]


def test_a_merge_the_graph_refuses_is_one_refusal_and_the_plan_goes_on(conn, configured):
    parent = base._hermes_parent(conn)
    held = _held(conn, parent, "fix the watcher again")
    kb.link_tasks(conn, held, parent)  # parent already waits on the held card: merging into it would be a cycle
    card = base._ready_in(conn, "babybear", "bb", priority=10)
    result = plan.apply(conn, [{"action": "merge", "card": held, "into": parent, "reason": "dup"},
                               {"action": "reprioritize", "card": card, "priority": 300, "reason": "next"}])
    assert len(result["rejected"]) == 1 and [a["card"] for a in result["applied"]] == [card]
    assert base._task(conn, card)["priority"] == 300


def test_a_lane_named_now_keeps_its_own_budget(conn, configured):
    mapping = json.loads(json.dumps(base.LANES))
    mapping["now"] = {"share": 5, "gated": True, "admit_per_day": 1}
    configured(mapping)
    parent = kb.create_task(conn, title="p", assignee="default", created_by="user", tenant="now")
    held = [kb.create_task(conn, title=f"n{i}", assignee="default", creator_task_id=parent) for i in range(2)]
    result = plan.apply(conn, [{"action": "admit", "card": c, "reason": "go"} for c in held])
    assert [a["card"] for a in result["applied"]] == held[:1]
    assert "budget spent (1/1" in _why(result)[0]


def test_relane_never_leaves_a_gated_lane_and_dry_run_says_so(conn, configured):
    parent = base._hermes_parent(conn)
    child = kb.create_task(conn, title="Independent review of core work", assignee="default",
                           creator_task_id=parent, tenant="hermes")
    assert base._task(conn, child)["status"] == "ready"
    result = plan.apply(conn, [{"action": "relane", "card": child, "lane": "other", "reason": "escape"}])
    assert not result["applied"] and base._task(conn, child)["tenant"] == "hermes"
    dry = plan.apply(conn, [{"action": "relane", "card": child, "lane": "other", "reason": "escape"}], dry_run=True)
    assert dry["dry_run"] is True


def test_set_lane_writes_the_root_config_from_a_profile(conn, configured, monkeypatch):
    from hermes_constants import get_default_hermes_root
    from hermes_cli import kanban_lanes_cli

    root = get_default_hermes_root()
    profile = root / "profiles" / "x"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert get_default_hermes_root() == root
    kanban_lanes_cli.set_lane("babybear", paused=True, share=50)
    lane = lanes.policy().lanes["babybear"]
    assert (lane.paused, lane.share) == (True, 50.0)
    assert "lanes" not in (profile / "config.yaml").read_text()
    with pytest.raises(ValueError):
        kanban_lanes_cli.set_lane("babybear", share=-1)
