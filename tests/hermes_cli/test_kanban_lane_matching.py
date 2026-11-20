"""Lane provenance rules and non-admitting accounting repair."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli.kanban_lane_reclassify import reclassify
from tests.hermes_cli import test_kanban_lanes as helpers

conn = helpers.conn
person = helpers.person
configured = helpers.configured


@pytest.mark.parametrize("opts", [
    {"project_repo": "https://github.com/example/hermes-agent.git"},
    {"workspace_path": "/src/hermes-agent/.worktrees/card"},
    {"workspace_path": "/src/hermes-agent"},
])
def test_repo_identity(opts):
    pol = lanes.parse({"lanes": helpers.LANES})
    assert lanes._rule_lane(pol, opts) == "hermes"


@pytest.mark.parametrize("raw", [["core-dev"], '["core-dev"]'])
def test_exact_skill_rule(raw):
    pol = lanes.parse({"lanes": {"hermes": {"match": {"skills": ["core-dev"]}}, "other": {}}})
    assert lanes._rule_lane(pol, {"skills": raw}) == "hermes"


def test_title_rule_and_other_regressions():
    pol = lanes.parse({"lanes": {"hermes": {"match": {"title_patterns": [r"^Desktop:"]}}, "other": {}}})
    assert lanes._rule_lane(pol, {"title": "Desktop: sidebar dot", "created_by": "default"}) == "hermes"
    for opts in ({"title": "Ask Eve admin", "created_by": "default"},
                 {"title": "Buy a desktop computer"}, {"skills": '["core-dev-extra"]'},
                 {"skills": '{"core-dev":true}'}, {"skills": "malformed"}):
        assert lanes._rule_lane(pol, opts) is None


def test_no_arbitrary_repo_ancestor_match():
    pol = lanes.parse({"lanes": helpers.LANES})
    assert lanes._rule_lane(pol, {"workspace_path": "/src/hermes-agent/ask-eve"}) is None


def test_bad_pattern_rejected():
    with pytest.raises(ValueError, match="title_patterns"):
        lanes.parse({"lanes": {"other": {"match": {"title_patterns": ["["]}}}})


@pytest.mark.parametrize("status,block_kind", [
    ("running", None), ("blocked", "needs_input"), ("scheduled", "time"),
    ("triage", "containment"), ("triage", lanes.GATE_KIND), ("done", None), ("archived", None),
])
def test_reclassification_never_changes_execution_state(conn, configured, status, block_kind):
    tid = kb.create_task(conn, title="Desktop: state", assignee="default", created_by="user")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=?, block_kind=? WHERE id=?", (status, block_kind, tid))
    mapping = {**helpers.LANES, "hermes": {"gated": True, "match": {"title_patterns": ["^Desktop:"]}}}
    configured(mapping)
    changes = reclassify(conn, apply=True)
    assert bool(changes) == (status not in ("done", "archived"))
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
    assert tuple(row) == (status, block_kind)


def test_new_rules_still_honor_creation_gate_and_explicit_user_lane(conn, configured):
    mapping = {**helpers.LANES, "hermes": {"gated": True, "match": {"title_patterns": ["^Desktop:"]}}}
    configured(mapping)
    parent = kb.create_task(conn, title="parent", assignee="default", created_by="user")
    tid = kb.create_task(conn, title="Desktop: state", assignee="default", created_by="default", creator_task_id=parent)
    assert tuple(conn.execute("SELECT tenant,status FROM tasks WHERE id=?", (tid,)).fetchone()) == ("hermes", "triage")
    explicit = kb.create_task(conn, title="Desktop: state", assignee="default", created_by="user", tenant="other")
    assert conn.execute("SELECT tenant FROM tasks WHERE id=?", (explicit,)).fetchone()[0] == "other"


def test_reclassification_preserves_custody_and_manual_labels(conn, configured):
    ids = [kb.create_task(conn, title="Desktop: dot", assignee="default", created_by="user") for _ in range(3)]
    lanes.assign(conn, ids[1], "other", source="given", reason="explicit")
    lanes.assign(conn, ids[2], "other", source="llm", reason="planner")
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (ids[0],)).fetchone())
    mapping = dict(helpers.LANES)
    mapping["hermes"] = {**mapping["hermes"], "match": {"title_patterns": ["^Desktop:"]}}
    configured(mapping)
    changes = reclassify(conn)
    assert [c["id"] for c in changes] == [ids[0]]
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (ids[0],)).fetchone()) == before
    assert reclassify(conn, apply=True) == changes
    after = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (ids[0],)).fetchone())
    assert after.pop("tenant") == "hermes"
    before.pop("tenant")
    # Synced write bookkeeping may advance, but execution state must not change.
    for key in ("status", "priority", "block_kind", "claim_lock", "claim_expires", "worker_pid", "current_run_id"):
        assert after[key] == before[key]
    assert reclassify(conn, apply=True) == []
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN (?, ?)",
                            (ids[0], lanes.GATED, lanes.ADMITTED)).fetchone()
