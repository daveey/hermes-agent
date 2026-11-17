"""The kanban_create tool path: a worker's follow-up card in a gated lane is held and priority-capped."""
import json
from pathlib import Path

from tests.tools.test_kanban_tools import worker_env  # noqa: F401  (fixture)


def _configure():
    from hermes_constants import get_default_hermes_root

    doc = {"kanban": {"admission": {"agent_priority_cap": 100, "lanes": {
        "hermes": {"share": 1, "gated": True}, "other": {"share": 1}}}}}
    (Path(get_default_hermes_root()) / "config.yaml").write_text(json.dumps(doc))


def test_tool_created_follow_up_in_gated_lane_is_held(worker_env):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_lanes as lanes
    from tools import kanban_tools as kt

    _configure()
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET tenant='hermes' WHERE id=?", (worker_env,))
        conn.commit()
    result = json.loads(kt._handle_create({"title": "Tidy the dispatcher", "assignee": "test-worker",
                                           "priority": 1000}))
    assert "error" not in result, result
    with kbc.connect() as conn:
        row = conn.execute("SELECT status, tenant, priority, block_kind FROM tasks WHERE id=?",
                           (result["task_id"],)).fetchone()
    assert tuple(row) == ("triage", "hermes", 100, lanes.GATE_KIND)


def test_tool_created_card_without_lanes_is_unchanged(worker_env):
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    result = json.loads(kt._handle_create({"title": "Tidy the dispatcher", "assignee": "test-worker"}))
    assert "error" not in result, result
    with kbc.connect() as conn:
        row = conn.execute("SELECT status, tenant, block_kind FROM tasks WHERE id=?",
                           (result["task_id"],)).fetchone()
    assert tuple(row) == ("ready", None, None)
