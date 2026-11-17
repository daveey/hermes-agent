"""Project lanes: classification and the creation gate, soft shares in dispatch order, the usage report."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli.kanban_db_dispatch_queries import lane_rows
from tests.hermes_cli import test_kanban_parent_first_dispatch as helpers

conn = helpers.conn

LANES = {
    "babybear": {"share": 35, "describe": "Baby Bear"},
    "hermes": {"share": 20, "gated": True, "admit_per_day": 2,
               "match": {"creators": ["self-heal"], "key_prefixes": ["ci-main-watch:"], "repos": ["hermes-agent"],
                         "self_work": True},
               "gate_exempt": ["independent review"], "never_admit": ["lifecycle guard"]},
    "other": {"share": 10},
}


@pytest.fixture
def configured(conn, monkeypatch):
    def write(mapping=None, **admission):
        from hermes_constants import get_default_hermes_root

        doc = {"kanban": {"admission": {"lanes": LANES if mapping is None else mapping,
                                        "agent_priority_cap": 100, **admission}}}
        (Path(get_default_hermes_root()) / "config.yaml").write_text(json.dumps(doc))  # JSON is YAML
    write()
    return write


def _task(conn, tid):
    return conn.execute("SELECT status, tenant, priority, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def _hermes_parent(conn):
    """An admitted Hermes card the user filed: the creating card for agent-filed children."""
    return kb.create_task(conn, title="core work", assignee="default", created_by="user",
                          workspace_path="/src/hermes-agent")


def test_no_lanes_configured_changes_nothing(conn):
    parent = kb.create_task(conn, title="p", assignee="default", priority=900)
    child = kb.create_task(conn, title="c", assignee="default", priority=900, creator_task_id=parent,
                           created_by="self-heal", workspace_path="/src/hermes-agent")
    row = _task(conn, child)
    assert (row["status"], row["tenant"], row["priority"], row["block_kind"]) == ("ready", None, 900, None)
    assert not [k for k in _kinds(conn, child) if k.startswith("lane_")]


def test_agent_filed_card_in_gated_lane_waits_and_is_capped(conn, configured):
    parent = _hermes_parent(conn)
    assert _task(conn, parent)["status"] == "ready"  # the user's own card is never gated
    child = kb.create_task(conn, title="follow-up", assignee="default", priority=1000, creator_task_id=parent,
                           created_by="default", workspace_path="/src/hermes-agent")
    row = _task(conn, child)
    assert (row["status"], row["tenant"], row["block_kind"]) == ("triage", "hermes", lanes.GATE_KIND)
    assert row["priority"] == 100
    assert {lanes.ASSIGNED, lanes.GATED} <= set(_kinds(conn, child))


def test_user_card_keeps_status_and_priority(conn, configured):
    tid = kb.create_task(conn, title="mine", assignee="default", priority=1000, created_by="user",
                         workspace_path="/src/hermes-agent")
    row = _task(conn, tid)
    assert (row["status"], row["tenant"], row["priority"], row["block_kind"]) == ("ready", "hermes", 1000, None)


def test_rule_lane_wins_over_the_creating_cards_lane(conn, configured):
    bb = kb.create_task(conn, title="bb", assignee="default", created_by="user", tenant="babybear")
    child = kb.create_task(conn, title="fix the watcher", assignee="default", creator_task_id=bb,
                           created_by="self-heal")
    assert _task(conn, child)["tenant"] == "hermes"
    plain = kb.create_task(conn, title="bb follow-up", assignee="default", creator_task_id=bb)
    row = _task(conn, plain)
    assert (row["tenant"], row["status"]) == ("babybear", "ready")


def test_unplaced_card_is_default_lane_with_unclassified_event(conn, configured):
    tid = kb.create_task(conn, title="something", assignee="default")
    assert _task(conn, tid)["tenant"] == "other"
    assert lanes.UNCLASSIFIED in _kinds(conn, tid)


@pytest.mark.parametrize("extra", [{"idempotency_key": "pickup-stall:x"}, {"admission_class": "urgent",
                                                                           "admission_frees": "frees a slot"}])
def test_alarm_and_urgent_cards_skip_the_gate(conn, configured, extra):
    parent = _hermes_parent(conn)
    if extra.get("admission_class"):
        extra = {"admission_class": "urgent"}
    tid = kb.create_task(conn, title="now", assignee="default", creator_task_id=parent,
                         workspace_path="/src/hermes-agent", **extra)
    assert _task(conn, tid)["status"] == "ready"


def test_gated_card_another_lane_waits_on_is_admitted(conn, configured):
    parent = _hermes_parent(conn)
    held = kb.create_task(conn, title="infra", assignee="default", creator_task_id=parent,
                          workspace_path="/src/hermes-agent")
    assert _task(conn, held)["status"] == "triage"
    kb.create_task(conn, title="bb feature", assignee="default", tenant="babybear", parents=[held])
    row = _task(conn, held)
    assert (row["status"], row["block_kind"]) == ("ready", None)
    assert lanes.admitted_today(conn, "hermes") == 0  # outside the budget


def test_review_card_of_admitted_creator_skips_gate_but_not_of_held_one(conn, configured):
    parent = _hermes_parent(conn)
    review = kb.create_task(conn, title="Independent review of core work", assignee="default",
                            creator_task_id=parent, workspace_path="/src/hermes-agent")
    assert _task(conn, review)["status"] == "ready"
    held = kb.create_task(conn, title="more", assignee="default", creator_task_id=parent,
                          workspace_path="/src/hermes-agent")
    review2 = kb.create_task(conn, title="Independent review of more", assignee="default",
                             creator_task_id=held, workspace_path="/src/hermes-agent")
    assert _task(conn, review2)["status"] == "triage"


def test_triage_sweep_leaves_gate_held_card(conn, configured):
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage

    parent = _hermes_parent(conn)
    held = kb.create_task(conn, title="infra", assignee="default", creator_task_id=parent,
                          workspace_path="/src/hermes-agent")
    plain = kb.create_task(conn, title="needs a spec", assignee="default", triage=True, tenant="babybear")
    conn.execute("UPDATE tasks SET created_at = created_at - 100000")
    conn.execute("UPDATE task_events SET created_at = created_at - 100000")
    conn.commit()
    swept = sweep_stale_triage(conn)
    assert plain in swept and held not in swept
    assert _task(conn, held)["status"] == "triage"


def test_admit_releases_and_counts_against_budget(conn, configured):
    parent = _hermes_parent(conn)
    held = kb.create_task(conn, title="infra", assignee="default", creator_task_id=parent,
                          workspace_path="/src/hermes-agent")
    assert [r["id"] for r in lanes.held(conn, "hermes")] == [held]
    assert lanes.admit(conn, held, actor="user", reason="needed")
    assert _task(conn, held)["status"] == "ready"
    assert lanes.admitted_today(conn, "hermes") == 1
    assert not lanes.admit(conn, held, actor="user", reason="again")


def _ready_in(conn, lane, title, priority=0):
    return kb.create_task(conn, title=title, assignee="default", priority=priority, tenant=lane, created_by="user")


def _order(conn):
    return [r["id"] for r in lane_rows(conn, "ready")]


def test_under_share_lane_gets_the_next_slot(conn, configured):
    hermes = [_ready_in(conn, "hermes", f"h{i}", priority=1000) for i in range(10)]
    running = [_ready_in(conn, "hermes", f"r{i}") for i in range(4)]
    conn.execute(f"UPDATE tasks SET status='running' WHERE id IN ({','.join('?' * 4)})", running)
    conn.commit()
    assert _order(conn)[0] in hermes  # nothing else ready: Hermes keeps the slot
    bb = _ready_in(conn, "babybear", "bb")
    assert _order(conn)[0] == bb
    assert set(_order(conn)[1:]) == set(hermes)


def test_equal_shares_alternate_and_keep_score_order_inside_a_lane(conn, configured):
    configured({"a": {"share": 1}, "b": {"share": 1}, "other": {"share": 1}})
    a = [_ready_in(conn, "a", f"a{i}", priority=900 - i) for i in range(3)]
    b = [_ready_in(conn, "b", f"b{i}", priority=10 - i) for i in range(3)]
    order = _order(conn)
    assert [t for t in order if t in a] == a and [t for t in order if t in b] == b
    assert {order[0], order[1]} == {a[0], b[0]} and {order[2], order[3]} == {a[1], b[1]}


def test_marked_card_in_over_share_lane_still_first(conn, configured):
    running = [_ready_in(conn, "hermes", f"r{i}") for i in range(4)]
    conn.execute(f"UPDATE tasks SET status='running' WHERE id IN ({','.join('?' * 4)})", running)
    conn.commit()
    bb = _ready_in(conn, "babybear", "bb", priority=1000)
    urgent = kb.create_task(conn, title="down", assignee="default", tenant="hermes", created_by="user",
                            admission_class="urgent")
    assert _order(conn)[:2] == [urgent, bb]


def test_turn_owner_in_over_share_lane_still_first(conn, configured, monkeypatch):
    from hermes_cli import kanban_host_counts, kanban_turn_urgency

    running = [_ready_in(conn, "hermes", f"r{i}") for i in range(4)]
    conn.execute(f"UPDATE tasks SET status='running' WHERE id IN ({','.join('?' * 4)})", running)
    conn.commit()
    bb = _ready_in(conn, "babybear", "bb", priority=1000)
    owner = _ready_in(conn, "hermes", "landing")
    monkeypatch.setattr(kanban_host_counts, "board_of", lambda _conn: "default")
    monkeypatch.setattr(kanban_turn_urgency, "read_owners", lambda **_kw: {owner})
    assert _order(conn)[:2] == [owner, bb]


def test_paused_lane_is_not_dispatched(conn, configured):
    mapping = json.loads(json.dumps(LANES))
    mapping["babybear"]["paused"] = True
    bb = _ready_in(conn, "babybear", "bb")
    other = _ready_in(conn, "other", "o")
    configured(mapping)
    assert _order(conn) == [other]
    configured()
    assert set(_order(conn)) == {bb, other}


def test_malformed_lanes_do_not_break_creation(conn, configured):
    configured({"Bad Name": {"share": 1}})
    with pytest.raises(ValueError):
        lanes.policy()
    tid = kb.create_task(conn, title="still works", assignee="default")
    assert _task(conn, tid)["status"] == "ready"


def test_usage_reports_target_actual_and_backlog(conn, configured):
    parent = _hermes_parent(conn)
    kb.create_task(conn, title="infra", assignee="default", creator_task_id=parent,
                   workspace_path="/src/hermes-agent")
    _ready_in(conn, "babybear", "bb")
    now = 1_000_000
    conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at) VALUES (?, 'done', ?, ?)",
                 (parent, now - 7200, now - 3600))
    conn.commit()
    out = lanes.usage(conn, lanes.policy(), window_s=6 * 3600, now=now)
    assert out["hermes"]["run_seconds"] == 3600 and out["hermes"]["actual"] == 1.0
    assert out["hermes"]["held"] == 1 and out["babybear"]["ready"] == 1
    assert round(out["babybear"]["target"], 3) == round(35 / 65, 3)
