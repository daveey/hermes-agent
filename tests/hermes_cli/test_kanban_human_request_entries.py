"""CLI and REST evidence entry must not turn classification into authorization."""
import argparse
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban, kanban_db as db
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_parser import build_parser
from plugins.kanban.dashboard import plugin_api as api

REQUEST = dict(action="Approve staging", why_human="Release owner decision required",
               interaction="approval", scope="Staging only", evidence="Release policy")


@pytest.fixture
def client(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "test-dashboard-token")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app, headers={"Authorization": "Bearer test-dashboard-token"})


def cli(*args):
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(["kanban", *args]))


def parked(*, triage=False):
    with connect_closing() as conn:
        return db.create_task(conn, title="Legacy", assignee="owner", triage=triage,
                              initial_status="blocked")


def snapshot(tid):
    with connect_closing() as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())


def test_cli_create_block_and_output(capsys):
    assert cli("create", "Ask", "--initial-status", "blocked", "--human-request",
               json.dumps(REQUEST), "--json") == 0
    task = json.loads(capsys.readouterr().out)
    assert task["human_request"] == REQUEST
    with connect_closing() as conn:
        assert db.unblock_task(conn, task["id"])
    assert cli("block", task["id"], "Policy requires owner", "--kind", "needs_input",
               "--human-request", json.dumps(REQUEST)) == 0
    assert json.loads(snapshot(task["id"])["human_request"]) == REQUEST


@pytest.mark.parametrize("triage", [False, True])
@pytest.mark.parametrize("surface", ["cli", "patch", "bulk"])
def test_classification_is_audited_metadata_only(client, surface, triage):
    tid = parked(triage=triage)
    before = snapshot(tid)
    if surface == "cli":
        assert cli("block", tid, "Reviewed legacy ask", "--metadata-only", "--human-request",
                   json.dumps(REQUEST)) == 0
    else:
        payload = dict(metadata_only=True, block_reason="Reviewed legacy ask", human_request=REQUEST)
        response = (client.patch(f"/tasks/{tid}", json=payload) if surface == "patch" else
                    client.post("/tasks/bulk", json={**payload, "ids": [tid]}))
        assert response.status_code == 200, response.text
        if surface == "bulk":
            assert response.json()["results"][0]["ok"]
    after = snapshot(tid)
    assert json.loads(after.pop("human_request")) == REQUEST
    before.pop("human_request")
    assert after == before
    with connect_closing() as conn:
        events = db.list_events(conn, tid)
        event = events[-1]
        payload = event.payload
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["metadata_only"] is True
        assert payload["actor"]
        assert payload["reason"] == "Reviewed legacy ask"


def test_rest_create_and_block_parity(client):
    response = client.post("/tasks", json=dict(title="Ask", initial_status="blocked", human_request=REQUEST))
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["status"] == "blocked"
    assert task["human_request"] == REQUEST
    for surface in ("patch", "bulk"):
        with connect_closing() as conn:
            assert db.unblock_task(conn, task["id"])
        payload = dict(status="blocked", block_kind="needs_input", human_request=REQUEST)
        response = (client.patch(f"/tasks/{task['id']}", json=payload) if surface == "patch" else
                    client.post("/tasks/bulk", json={**payload, "ids": [task["id"]]}))
        assert response.status_code == 200, response.text
        assert json.loads(snapshot(task["id"])["human_request"]) == REQUEST


@pytest.mark.parametrize("bad", [{}, [], "bad", {**REQUEST, "interaction": "sudo"}, {**REQUEST, "scope": " "}])
def test_invalid_rest_create_has_no_writes(client, bad):
    response = client.post("/tasks", json=dict(title="Bad", initial_status="blocked", human_request=bad))
    assert response.status_code in (400, 422), response.text
    with connect_closing() as conn:
        assert not db.list_tasks(conn)


@pytest.mark.parametrize("surface", ["patch", "bulk"])
@pytest.mark.parametrize("extra", [dict(status="done"), dict(assignee="other"), dict(block_reason=""),
                                    dict(human_request={}), dict(status="scheduled")])
def test_invalid_classification_never_changes_task(client, surface, extra):
    tid = parked()
    before = snapshot(tid)
    payload = dict(metadata_only=True, block_reason="Reviewed", human_request=REQUEST) | extra
    response = (client.patch(f"/tasks/{tid}", json=payload) if surface == "patch" else
                client.post("/tasks/bulk", json={**payload, "ids": [tid]}))
    assert response.status_code in (400, 422), response.text
    assert snapshot(tid) == before


@pytest.mark.parametrize("surface", ["cli", "patch", "bulk"])
def test_clear_typed_legacy_request_without_unblocking(client, surface):
    tid = parked()
    with connect_closing() as conn:
        assert db.block_task(conn, tid, kind="needs_input", human_request=REQUEST)
    before = snapshot(tid)
    if surface == "cli":
        assert cli("block", tid, "No longer a human ask", "--metadata-only", "--human-request", "null") == 0
    else:
        payload = dict(metadata_only=True, human_request=None, block_reason="No longer a human ask")
        response = (client.patch(f"/tasks/{tid}", json=payload) if surface == "patch" else
                    client.post("/tasks/bulk", json={**payload, "ids": [tid]}))
        assert response.status_code == 200
    after = snapshot(tid)
    assert after.pop("human_request") is None
    before.pop("human_request")
    assert before == after


@pytest.mark.parametrize("extra", [dict(status="done"), dict(block_kind="transient"),
                                    dict(human_request={}), dict(status=None)])
@pytest.mark.parametrize("surface", ["patch", "bulk"])
def test_invalid_block_request_rejected_before_assignment(client, extra, surface):
    tid = parked()
    before = snapshot(tid)
    payload = dict(status="blocked", assignee="other", human_request=REQUEST) | extra
    response = (client.patch(f"/tasks/{tid}", json=payload) if surface == "patch" else
                client.post("/tasks/bulk", json={**payload, "ids": [tid]}))
    assert response.status_code in (400, 422)
    assert snapshot(tid) == before


@pytest.mark.parametrize("bad", ["{}", "[]", '"bad"', json.dumps({**REQUEST, "interaction": "sudo"})])
def test_invalid_cli_requests_leave_rows_unchanged(bad):
    tid = parked()
    before = snapshot(tid)
    assert cli("block", tid, "--human-request", bad) != 0
    assert snapshot(tid) == before
    assert cli("create", "Invalid", "--initial-status", "blocked", "--human-request", bad) != 0
    with connect_closing() as conn:
        assert [task.id for task in db.list_tasks(conn)] == [tid]


@pytest.mark.parametrize("state", ["running", "done", "claimed"])
def test_classification_refuses_execution_state(client, state):
    tid = parked()
    with connect_closing() as conn:
        assert db.unblock_task(conn, tid)
        assert db.claim_task(conn, tid) is not None
        if state == "done":
            assert db.complete_task(conn, tid, result="Finished", force=True)
        elif state == "claimed":
            # Corrupt/legacy parked row retaining a real run must also fail closed.
            with db.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    before = snapshot(tid)
    response = client.patch(f"/tasks/{tid}", json=dict(metadata_only=True, human_request=REQUEST,
                                                    block_reason="Review"))
    assert response.status_code == 409
    assert snapshot(tid) == before


def test_worker_cannot_classify_legacy_card(monkeypatch):
    from hermes_cli.kanban_human_request_migration import classify_parked_request

    tid = parked()
    before = snapshot(tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with connect_closing() as conn, pytest.raises(PermissionError, match="orchestrator-only"):
        classify_parked_request(conn, tid, human_request=REQUEST, actor="worker", reason="Review")
    assert snapshot(tid) == before
