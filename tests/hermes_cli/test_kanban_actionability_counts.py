"""Read-side human asks require explicit evidence, never a block kind."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as db
from hermes_cli import kanban_diagnostics as diagnostics
from hermes_cli import kanban_session_activity as activity

REQUEST = dict(action="Approve this deployment", why_human="Approval is reserved to the owner",
               interaction="approval", scope="staging deployment", evidence="Owner approval not recorded")


@pytest.mark.parametrize("status", ["blocked", "triage"])
@pytest.mark.parametrize("kind,evidence,human", [
    (None, None, False), ("capability", None, False), ("needs_input", None, False),
    (None, REQUEST, True), ("needs_input", REQUEST, True), ("capability", REQUEST, True),
    ("transient", REQUEST, False), ("time", REQUEST, False), ("dependency", REQUEST, False),
    ("needs_input", {**REQUEST, "evidence": "\t"}, False),
    ("capability", {**REQUEST, "interaction": "sudo"}, False),
    ("needs_input", [], False), ("needs_input", "bad json", False),
])
def test_local_replica_and_stats_agree(tmp_path, status, kind, evidence, human):
    path = tmp_path / "board.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id TEXT, session_id TEXT, status TEXT, block_kind TEXT, "
                 "human_request TEXT, assignee TEXT, created_at INTEGER)")
    stored = json.dumps(evidence) if evidence is not None else None
    conn.execute("INSERT INTO tasks VALUES ('t_one', 'owner', ?, ?, ?, 'worker', 1)",
                 (status, kind, stored))
    conn.commit()
    task = dict(status=status, block_kind=kind, human_request=evidence)
    assert activity._replica_bucket(task) == ("blocked" if human else "active")
    assert activity._read_counts(path, activity.BLOCKED_STATUSES) == ({"owner": 1} if human else {})
    assert activity._read_counts(path) == ({} if human else {"owner": 1})
    stats = db.board_stats(conn)
    assert stats["waiting_on_user_ids"] == (["t_one"] if human else [])
    assert stats["by_assignee"] == {"worker": {status: 1}}
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == status
    conn.close()


def test_legacy_schema_is_operational_not_hidden(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT, session_id TEXT, status TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?, 'owner', ?)", [("a", "blocked"), ("b", "triage")])
    assert activity._read_counts(path) == {"owner": 2}
    assert activity._read_counts(path, activity.BLOCKED_STATUSES) == {}


@pytest.mark.parametrize("evidence,human", [(None, False), ({"action": "sudo"}, False), (REQUEST, True)])
def test_diagnostics_require_explicit_current_evidence(evidence, human):
    task = dict(id="parent", status="triage", block_kind="capability", human_request=evidence, assignee="worker")
    events = [dict(kind="block_loop_detected", created_at=1, payload={"reason": "sudo authentication failed"})]
    result = diagnostics._rule_escalated_waiting_on_user(task, events, [], 10, {})
    assert bool(result) is human
    if human:
        assert result[0].data["ask"] == REQUEST["action"]
    behind = diagnostics._rule_waiting_behind_user(
        {"status": "todo"}, [], [], 10, {"_graph": {"parents": [task]}})
    assert bool(behind) is human


def test_scheduled_unknown_does_not_claim_human_answer_or_offer_resume():
    task = dict(id="scheduled", status="scheduled", block_kind="capability", assignee="worker")
    result = diagnostics._rule_scheduled_without_wake(task, [], [], 10, {})[0]
    assert "waits on a human answer" not in result.detail
    assert all(action.kind != "unblock" for action in result.actions)


def test_stale_operational_block_remains_visible_and_owned():
    task = dict(id="blocked", status="blocked", block_kind="capability", assignee="worker")
    result = diagnostics._rule_stuck_in_blocked(
        task, [dict(kind="blocked", created_at=1)], [], 100000, {})[0]
    assert "waiting for human input" not in result.detail
    assert "worker" in result.detail


@pytest.mark.parametrize("interaction", ["approval", "reply", "user_terminal", "physical", "external"])
def test_hub_replica_counts_explicit_requests_and_operational_children(tmp_path, interaction):
    path = tmp_path / "hub.db"
    root = dict(id="parent", status="blocked", block_kind="capability",
                human_request={**REQUEST, "interaction": interaction}, children=["child"],
                sessions=[{"session": "owner", "gw": "here", "profile": "default", "rel": "shepherd"}])
    child = dict(id="child", status="blocked", block_kind="capability")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (k TEXT, v TEXT)")
        conn.execute("INSERT INTO meta VALUES ('gateway', 'here')")
        conn.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER)")
        conn.executemany("INSERT INTO replica VALUES ('there', 'tasks', ?, ?, 1000)",
                         [(f"default/main/{card['id']}", json.dumps(card)) for card in [root, child]])
    rows = activity._read_remote_links(path, now=1)
    assert {row["id"]: (row["session"], activity._replica_bucket(row)) for row in rows} == {
        "parent": ("owner", "blocked"), "child": ("owner", "active"),
    }


@pytest.mark.parametrize("missing", list(REQUEST))
def test_each_field_is_required_by_sql_and_replica(missing):
    evidence = {key: value for key, value in REQUEST.items() if key != missing}
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE tasks (status TEXT, block_kind TEXT, human_request TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('blocked', 'needs_input', ?)", (json.dumps(evidence),))
        match, params = activity._status_filter(conn, activity.BLOCKED_STATUSES)
        assert conn.execute(f"SELECT COUNT(*) FROM tasks t WHERE {match}", params).fetchone()[0] == 0
    assert activity._replica_bucket(dict(status="blocked", block_kind="needs_input", human_request=evidence)) == "active"


def test_malformed_storage_and_stale_evidence_are_not_human(tmp_path):
    path = tmp_path / "board.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT, session_id TEXT, status TEXT, block_kind TEXT, human_request TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('bad', 'owner', 'blocked', 'needs_input', '{')")
        conn.execute("INSERT INTO tasks VALUES ('done', 'owner', 'done', 'needs_input', ?)", (json.dumps(REQUEST),))
    assert activity._read_counts(path) == {"owner": 1}
    assert activity._read_counts(path, activity.BLOCKED_STATUSES) == {}
    assert activity._replica_bucket(dict(status="done", human_request=REQUEST)) is None
    assert diagnostics._human_request({"human_request": "{"}) is None
