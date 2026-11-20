"""Card evidence through real SQLite/read composition, without production writes."""
import json
import sqlite3
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_execution_observations as observations
from hermes_cli import kanban_session_activity as activity
from hermes_cli.execution_observations import project
from tests.hermes_cli import test_kanban_session_activity as activity_fixtures

kanban_root = activity_fixtures.kanban_root


@pytest.fixture
def board(kanban_root, monkeypatch):
    monkeypatch.setattr(observations, "local_scope", lambda: ("fixture-gateway", "default"))
    monkeypatch.setenv("HERMES_HOME", str(kanban_root))  # the board is under the root: owner profile default
    path = kanban_root / "kanban.db"
    activity_fixtures._make_full_board(path, [("card", "Work", "running", "agent", "session", 100, 200, 7)])
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_heartbeat_at INTEGER")
        conn.execute("ALTER TABLE tasks ADD COLUMN human_request TEXT")
        conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
        conn.execute("UPDATE tasks SET last_heartbeat_at=900")
        conn.execute("ALTER TABLE task_runs ADD COLUMN last_heartbeat_at INTEGER")
        conn.execute("ALTER TABLE task_runs ADD COLUMN status TEXT")
        conn.execute("ALTER TABLE task_runs ADD COLUMN ended_at INTEGER")
        conn.execute("UPDATE task_runs SET last_heartbeat_at=900, status='running'")
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, "
                     "kind TEXT, created_at INTEGER, payload TEXT)")
        conn.execute("INSERT INTO task_events VALUES (1, 'card', 7, 'comment', 910, '{}')")
    monkeypatch.setattr(activity.time, "time", lambda: 920)
    return path


def test_real_rows_separate_activity_progress_and_do_not_renew_on_fetch(board, monkeypatch):
    row = activity.session_tasks(["session"])[0]
    value = row["execution_observations"][0]
    assert value["identity"] == {"gateway": "fixture-gateway", "profile": "default", "board": "default",
                                 "kind": "card", "work_id": "card", "run_id": 7}
    assert value["state"] == "running" and value["source_revision"] == "task_runs/7/last_heartbeat_at/900"
    assert value["observed_at"] == 900 and value["last_activity"] == 910
    assert value["last_progress"] is None and value["last_progress_ref"] is None
    assert "_execution_event" not in row
    assert project(value, now=1080)["state"] == "unknown"
    assert activity.session_tasks(["session"])[0]["execution_observations"][0]["observed_at"] == 900
    monkeypatch.setattr(activity.time, "time", lambda: 1080)
    fetched = activity.session_tasks(["session"])[0]["execution_observations"][0]
    assert fetched["state"] == "unknown" and fetched["unknown_reason"] == "source_expired"
    assert fetched["observed_at"] == 900


def test_other_attempt_activity_cannot_refresh_current_run(board):
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE task_events SET run_id=6, created_at=919")
    value = activity.session_tasks(["session"])[0]["execution_observations"][0]
    assert value["identity"]["run_id"] == 7
    assert value["observed_at"] == 900 and value["last_activity"] == 919
    assert value["last_progress"] is None


def test_done_and_legacy_missing_attempt_never_invent_verification(board):
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE tasks SET status='done', current_run_id=NULL")
    value = activity.session_tasks(["session"], statuses=("done",))[0]["execution_observations"][0]
    assert value["state"] == "unknown" and value["last_known_state"] is None
    assert "run_id" not in value["identity"] and "delivery_receipt" not in value
    raw = {"id": "card", "board": "default", "status": "done", "current_run_id": 7,
           "_execution_event": {"id": 1, "run_id": 7, "created_at": 910, "kind": "completed"}}
    assert observations.card(raw, gateway="g", profile="p", now=920)["unknown_reason"] == "unverified_completion"


def test_human_ask_survives_unknown_wait_and_expiry_without_private_probe(board):
    request = {"action": "Approve fixture", "why_human": "Only the owner can approve", "interaction": "approval",
               "scope": "fixture", "evidence": "fixture-ref", "resolved_check": "private command",
               "resolved_check_every": 10}
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE tasks SET status='blocked', human_request=?, block_kind='human'", (json.dumps(request),))
        conn.execute("UPDATE task_events SET kind='blocked'")
    row = activity.session_tasks(["session"])[0]
    value = row["execution_observations"][0]
    assert value["state"] == "unknown" and value["unknown_reason"] == "missing_qualified_identity"
    assert value["human_request"]["action"] == request["action"]
    assert "resolved_check" not in value["human_request"]
    assert "resolved_check_every" not in value["human_request"]
    assert project(value, now=2000)["human_request"] == value["human_request"]
    assert row["human_request"] == json.dumps(request)  # legacy request not erased


@pytest.mark.parametrize("clock", [None, float("inf"), float("nan"), 930, True])
def test_invalid_or_future_source_clocks_remain_unknown(clock):
    row = {"id": "card", "board": "default", "status": "running", "current_run_id": 7,
           "last_heartbeat_at": clock}
    assert observations.card(row, gateway="g", profile="p", now=920)["state"] == "unknown"


@pytest.mark.parametrize("with_links", [False, True])
def test_task_queries_remain_constant_for_many_rows(board, monkeypatch, with_links):
    if with_links:
        with sqlite3.connect(board) as conn:
            conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    original = activity.cached_ro
    statements = []

    @contextmanager
    def traced(path, **kwargs):
        with original(path, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(activity, "cached_ro", traced)
    activity._read_session_tasks(board, ["session"], activity.LISTED_STATUSES)
    one = len(statements)
    assert one > 0
    statements.clear()
    with sqlite3.connect(board) as conn:
        conn.executemany("INSERT INTO tasks (id, title, status, session_id, current_run_id, last_heartbeat_at) "
                         "VALUES (?, 'Work', 'running', 'session', 7, 900)", [(f"card-{n}",) for n in range(80)])
    rows = activity._read_session_tasks(board, ["session"], activity.LISTED_STATUSES)
    assert len(rows) == 81 and len(statements) == one


def test_session_counts_project_unknown_without_queries_or_zero_fabrication(board, monkeypatch):
    rows = [{"id": "session"}, {"id": "empty"}]
    activity.annotate_kanban_activity(rows)
    assert rows[0]["kanban_active"] == 1
    assert rows[0]["execution_observations"][0]["state"] == "running"
    assert rows[0]["execution_observations"][0]["observed_at"] == 900
    assert "execution_observations" not in rows[1] and "kanban_active" not in rows[1]
    # A reused snapshot must not read one card per session, or accumulate observations.
    with activity.one_annotation_snapshot():
        activity.annotate_kanban_activity(rows)
        monkeypatch.setattr(activity, "_read_annotation_parts", lambda: pytest.fail("snapshot re-read"))
        activity.annotate_kanban_activity(rows)
    assert len(rows[0]["execution_observations"]) == 1
    capacity = {"source": "capacity", "state": "unknown"}
    rows[0]["execution_observations"].append(capacity)
    activity._mark_kanban_unknown(rows)
    assert rows[0]["kanban_unknown"] is True
    assert capacity in rows[0]["execution_observations"]
    assert all(value["state"] == "unknown" for value in rows[0]["execution_observations"])


def test_task_response_exposes_actual_composed_observation(board):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers.sessions import list_router

    app = FastAPI()
    app.include_router(list_router)
    with TestClient(app) as client:
        response = client.get("/api/session-kanban-tasks", params={"ids": "session"})
    assert response.status_code == 200
    value = response.json()["tasks"][0]["execution_observations"][0]
    assert value["state"] == "running" and value["observed_at"] == 900
    assert value["last_progress"] is None


def test_count_read_failure_retains_previous_request_without_probes():
    request = {"action": "Approve fixture", "why_human": "Owner required", "interaction": "approval",
               "scope": "fixture", "evidence": "fixture-ref", "resolved_check": "private command"}
    previous = observations.card({"id": "card", "board": "default", "status": "blocked",
                                  "current_run_id": 7, "human_request": request},
                                 gateway="g", profile="p", now=920)
    row = {"id": "session", "execution_observations": [previous]}
    activity._mark_kanban_unknown([row])
    retained = [value for value in row["execution_observations"] if value.get("human_request")]
    assert len(retained) == 1 and retained[0]["human_request"]["action"] == request["action"]
    assert "resolved_check" not in retained[0]["human_request"]
    assert retained[0]["state"] == "unknown"


def test_count_projection_preserves_partial_scope_without_inventing_attempts():
    row = {"id": "session"}
    observations.count_snapshot(row, [("g/p/b/card", "active", False), ("g/b/card", "active", False),
                                      ("other/p/b/card", "active", False)], now=920)
    values = row["execution_observations"]
    assert len(values) == 3 and all(value["state"] == "unknown" for value in values)
    identities = [value["identity"] for value in values]
    assert {"gateway": "g", "profile": "p", "board": "b", "kind": "card", "work_id": "card"} in identities
    assert {"gateway": "g", "board": "b", "kind": "card", "work_id": "card"} in identities
    assert {"gateway": "other", "profile": "p", "board": "b", "kind": "card", "work_id": "card"} in identities
    assert all("run_id" not in identity and "generation" not in identity for identity in identities)
