"""Optional execution-observation adapter failures never break legacy output or erase human asks.

Regression for the 29b07d review (M1-M3): one malformed synced event, or an adapter exception, must
degrade only that card to an explicit unknown observation. Isolated temp-DB fixtures only.
"""
import json
import sqlite3
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_execution_observations as observations
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_execution_source as source
from hermes_cli import kanban_queue_position as queue
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_capacity_harness as harness
from tests.hermes_cli import test_kanban_session_activity as fixtures

kanban_root = fixtures.kanban_root
capacity_board = harness.capacity_board

REQUEST = {"action": "Approve", "why_human": "Owner needed", "interaction": "approval",
           "scope": "fixture", "evidence": "fixture", "resolved_check": "private command"}


def _meta(identity, predecessors=()):
    return {"status": "ready", "_recovery_history_v1": {"identity": identity, "predecessors": predecessors}}


@pytest.fixture
def two_cards(kanban_root, monkeypatch):
    """`card` (session) carries a malformed event; `other` (session2) holds an unrelated outstanding ask."""
    monkeypatch.setattr(observations, "local_scope", lambda: ("fixture-gateway", "default"))
    monkeypatch.setattr(activity.time, "time", lambda: 1000)
    path = kanban_root / "kanban.db"
    fixtures._make_full_board(path, [("card", "Work", "ready", "agent", "session", 10, None, None),
                                     ("other", "Other", "ready", "agent", "session2", 10, None, None)])
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
        conn.execute("ALTER TABLE tasks ADD COLUMN human_request TEXT")
        conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input', human_request=? WHERE id='other'",
                     (json.dumps(REQUEST),))
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, "
                     "created_at INTEGER, payload TEXT, uid TEXT)")
    return path


def _bad_event(path, payload):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO task_events VALUES (1, 'card', NULL, 'promoted', 10, ?, 'u1')", (json.dumps(payload),))


MALFORMED = [
    _meta(["x"]), _meta({"a": 1}), _meta(7), _meta(None),
    _meta("ok", [["nested"]]), _meta("ok", "not-a-list"), _meta("ok", [{"a": 1}]), _meta("ok", [3]),
]


@pytest.mark.parametrize("payload", MALFORMED, ids=range(len(MALFORMED)))
def test_malformed_event_identity_is_unknown_and_leaves_other_session_ask(two_cards, payload):
    _bad_event(two_cards, payload)
    other, bad = [{"id": "session2"}], [{"id": "session"}]
    activity.annotate_kanban_activity(other)
    activity.annotate_kanban_activity(bad)
    assert not other[0].get("kanban_unknown") and other[0]["kanban_blocked"] == 1
    assert not bad[0].get("kanban_unknown") and bad[0]["kanban_active"] == 1
    states = {(v["source"], v["state"]) for v in bad[0]["execution_observations"]}
    assert ("kanban.card", "unknown") in states
    assert not any(v["state"] in {"running", "done"} for v in bad[0]["execution_observations"])


def test_malformed_event_counts_snapshot_does_not_raise(two_cards):
    _bad_event(two_cards, MALFORMED[0])
    assert activity._all_counts(include_snapshot=True) is not None


def test_source_raw_failure_is_per_card_unknown(two_cards, monkeypatch):
    real = source.raw

    def flaky(row, src):
        if row["id"] == "card":
            raise ValueError("fixture adapter failure")
        return real(row, src)
    monkeypatch.setattr(source, "raw", flaky)
    other, bad = [{"id": "session2"}], [{"id": "session"}]
    activity.annotate_kanban_activity(other)
    activity.annotate_kanban_activity(bad)
    assert not other[0].get("kanban_unknown") and other[0]["kanban_blocked"] == 1
    assert not bad[0].get("kanban_unknown") and bad[0]["kanban_active"] == 1
    unknown = [v for v in bad[0]["execution_observations"] if v["source"] == "kanban.card"]
    assert unknown and unknown[0]["state"] == "unknown" and unknown[0]["unknown_reason"] == "adapter_error"


def test_assemble_failure_keeps_legacy_counts_and_marks_cards_unknown(two_cards, monkeypatch):
    def boom(*a, **kw):
        raise sqlite3.OperationalError("fixture assemble failure")
    monkeypatch.setattr(source, "assemble", boom)
    other = [{"id": "session2"}]
    activity.annotate_kanban_activity(other)
    assert not other[0].get("kanban_unknown") and other[0]["kanban_blocked"] == 1
    assert activity._all_counts(include_snapshot=True) is not None


@pytest.mark.parametrize("fault", ["malformed", "raw", "assemble"])
def test_session_tasks_card_failure_is_explicit_unknown_with_ask(two_cards, monkeypatch, fault):
    if fault == "malformed":
        with sqlite3.connect(two_cards) as conn:
            conn.execute("UPDATE tasks SET session_id='session' WHERE id='other'")
        _bad_event(two_cards, MALFORMED[0])
    elif fault == "raw":
        monkeypatch.setattr(source, "raw", lambda *a, **kw: (_ for _ in ()).throw(TypeError("fixture")))
    else:
        def boom(*a, **kw):
            raise sqlite3.OperationalError("fixture assemble failure")
        monkeypatch.setattr(source, "assemble", boom)
    rows = {r["id"]: r for r in activity.session_tasks(["session", "session2"], statuses=("ready", "blocked"))}
    assert set(rows) == {"card", "other"}
    for tid in ("card", "other"):
        work = [v for v in rows[tid].get("execution_observations", []) if v["source"] == "kanban.card"]
        if tid == "card" or fault != "malformed":
            assert len(work) == 1 and work[0]["state"] == "unknown", (tid, work)
    other = next(v for v in rows["other"]["execution_observations"] if v["source"] == "kanban.card")
    if fault != "malformed":
        assert other["human_request"]["action"] == "Approve"
        assert "resolved_check" not in other["human_request"]
        assert other["unknown_reason"] == "adapter_error"


def test_add_cards_adapter_failure_attaches_unknown_not_nothing(monkeypatch):
    monkeypatch.setattr(source, "raw", lambda *a, **kw: (_ for _ in ()).throw(KeyError("fixture")))
    row = {"id": "t1", "board": "b", "status": "blocked", "human_request": json.dumps(REQUEST),
           "_execution_source": {"task": {}}}
    [out] = observations.add_cards([row], now=5)
    [value] = out["execution_observations"]
    assert value["source"] == "kanban.card" and value["state"] == "unknown"
    assert value["unknown_reason"] == "adapter_error" and value["human_request"]["action"] == "Approve"
    assert "_execution_source" not in out


@contextmanager
def _faults(monkeypatch, fault):
    if fault == "raw":
        monkeypatch.setattr(source, "raw", lambda *a, **kw: (_ for _ in ()).throw(ValueError("fixture")))
    elif fault == "assemble":
        monkeypatch.setattr(source, "assemble",
                            lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("fixture")))
    else:
        monkeypatch.setattr(observations, "local_scope",
                            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("fixture")))
    yield


@pytest.mark.parametrize("fault", ["raw", "assemble", "scope"])
def test_detail_fields_survives_adapter_failure(capacity_board, monkeypatch, fault):
    conn, ids, _ = capacity_board
    monkeypatch.setattr(queue, "_running_and_limit", lambda *a: (0, 4))
    with _faults(monkeypatch, fault):
        out = queue.detail_fields(conn, kb.get_task(conn, ids[0]), "default")
    assert "capacity_evidence" in out and "capacity_note" in out and "queue" in out
    work = [v for v in out["execution_observations"] if v["source"] == "kanban.card"]
    assert len(work) == 1 and work[0]["state"] == "unknown"


@pytest.mark.parametrize("fault", ["raw", "assemble"])
def test_tool_cli_rest_detail_survive_adapter_failure(capacity_board, monkeypatch, capsys, fault):
    from argparse import Namespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import kanban as cli
    from tests.plugins.test_kanban_foreign_card_api import _load_plugin_router
    from tools.kanban_show import show

    _, ids, _ = capacity_board
    monkeypatch.setattr(queue, "_running_and_limit", lambda *a: (0, 4))
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    with _faults(monkeypatch, fault), TestClient(app) as client:
        tool = json.loads(show({"task_id": ids[0]}))
        assert cli._cmd_show(Namespace(task_id=ids[0], json=True, board=None)) == 0
        command = json.loads(capsys.readouterr().out)
        response = client.get(f"/api/plugins/kanban/tasks/{ids[0]}")
        assert response.status_code == 200
        for payload in (tool, command, response.json()):
            assert payload["task"]["id"] == ids[0] and "capacity_evidence" in payload
            assert "comments" in payload and "events" in payload
            work = [v for v in payload["execution_observations"] if v["source"] == "kanban.card"]
            assert len(work) == 1 and work[0]["state"] == "unknown"
        assert cli._cmd_show(Namespace(task_id=ids[0], json=False, board=None)) == 0
