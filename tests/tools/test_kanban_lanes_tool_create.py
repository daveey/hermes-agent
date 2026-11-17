"""The kanban_create tool path: a worker's follow-up card in a gated lane is held and priority-capped."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def worker_card(monkeypatch, tmp_path):
    """A claimed worker card in an isolated HERMES_HOME, as the dispatcher pins it for the kanban tools."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _configure():
    from hermes_constants import get_default_hermes_root

    doc = {"kanban": {"admission": {"agent_priority_cap": 100, "lanes": {
        "hermes": {"share": 1, "gated": True, "match": {"key_prefixes": ["ci-main-watch:"]}},
        "product": {"share": 1}, "other": {"share": 1}}}}}
    (Path(get_default_hermes_root()) / "config.yaml").write_text(json.dumps(doc))


def test_tool_created_follow_up_in_gated_lane_is_held(worker_card):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_lanes as lanes
    from tools import kanban_tools as kt

    _configure()
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET tenant='hermes' WHERE id=?", (worker_card,))
        conn.commit()
    result = json.loads(kt._handle_create({"title": "Tidy the dispatcher", "assignee": "test-worker",
                                           "priority": 1000}))
    assert "error" not in result, result
    with kbc.connect() as conn:
        row = conn.execute("SELECT status, tenant, priority, block_kind FROM tasks WHERE id=?",
                           (result["task_id"],)).fetchone()
    assert tuple(row) == ("triage", "hermes", 100, lanes.GATE_KIND)


def test_tool_created_card_without_lanes_is_unchanged(worker_card):
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    result = json.loads(kt._handle_create({"title": "Tidy the dispatcher", "assignee": "test-worker"}))
    assert "error" not in result, result
    with kbc.connect() as conn:
        row = conn.execute("SELECT status, tenant, block_kind FROM tasks WHERE id=?",
                           (result["task_id"],)).fetchone()
    assert tuple(row) == ("ready", None, None)


def test_worker_in_another_lane_filing_a_rule_matched_card_is_held(worker_card, monkeypatch):
    """Workers pass their own card's lane (HERMES_TENANT) on every create; a match rule must still win."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_lanes as lanes
    from tools import kanban_tools as kt

    _configure()
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET tenant='product' WHERE id=?", (worker_card,))
        conn.commit()
    monkeypatch.setenv("HERMES_TENANT", "product")
    result = json.loads(kt._handle_create({"title": "main is red", "assignee": "test-worker",
                                           "idempotency_key": "ci-main-watch:abc"}))
    assert "error" not in result, result
    with kbc.connect() as conn:
        row = conn.execute("SELECT status, tenant, block_kind FROM tasks WHERE id=?", (result["task_id"],)).fetchone()
    assert tuple(row) == ("triage", "hermes", lanes.GATE_KIND)
    plain = json.loads(kt._handle_create({"title": "product follow-up", "assignee": "test-worker"}))
    with kbc.connect() as conn:
        row = conn.execute("SELECT status, tenant FROM tasks WHERE id=?", (plain["task_id"],)).fetchone()
    assert tuple(row) == ("ready", "product")
