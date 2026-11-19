"""Negative controls for trusted-operator diagnostic correction."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_diagnostic_correction import correct_diagnostics
from tests.hermes_cli import test_kanban_edit_operator_fields as fixtures

kanban_home = fixtures.kanban_home


@pytest.fixture
def correction_case(kanban_home):
    with kbc.connect_closing() as conn:
        sync.apply_sync_schema_steps(conn)
        conn.commit()
        tid = kb.create_task(conn, title="Correction control", body="Diagnostic error")
        kb.add_comment(conn, tid, "original-author", "Diagnostic error")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET uid = ? WHERE task_id = ?", ("control-comment", tid))
        request = dict(comment_uid="control-comment", expected_body="Diagnostic error",
                       expected_comment="Diagnostic error", body_diagnostic="error",
                       comment_diagnostic="error", reason="Diagnostic only", request_id="control-request")
        yield conn, tid, request


@pytest.mark.parametrize("payload", [None, [], "malformed", 42])
def test_malformed_audit_fails_closed(correction_case, payload):
    conn, tid, request = correction_case
    with kb.write_txn(conn):
        # Corrupt imported audit fixture; ordinary append only emits objects.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (tid, "diagnostic_corrected", json.dumps(payload), 1),
        )
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="malformed correction audit"):
        correct_diagnostics(conn, tid, **request)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("field", ["body", "comment"])
def test_retry_refuses_drift(correction_case, field):
    conn, tid, request = correction_case
    correct_diagnostics(conn, tid, **request)
    with kb.write_txn(conn):
        if field == "body":
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Drift", tid))
        else:
            conn.execute("UPDATE task_comments SET body = ? WHERE uid = ?", ("Drift", request["comment_uid"]))
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="state drifted"):
        correct_diagnostics(conn, tid, **request)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("identity", ["task", "comment", "membership"])
def test_wrong_identity_preserves_every_row(correction_case, identity):
    conn, tid, request = correction_case
    if identity == "task":
        tid = "missing-task"
    elif identity == "comment":
        request["comment_uid"] = "missing-comment"
    else:
        tid = kb.create_task(conn, title="Different task", body=request["expected_body"])
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="identity mismatch"):
        correct_diagnostics(conn, tid, **request)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("status", ["running", "review", "done", "archived"])
def test_active_or_closed_task_refused(correction_case, status):
    conn, tid, request = correction_case
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="inactive open task"):
        correct_diagnostics(conn, tid, **request)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("state", [
    {"mode": "shadow", "gateway_id": "owner"},
    {"mode": "primary", "gateway_id": ""},
    {"mode": "primary", "gateway_id": "foreign"},
])
def test_primary_owner_required(correction_case, monkeypatch, state):
    conn, tid, request = correction_case
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", ("owner", tid))
    monkeypatch.setattr(sync, "sync_state", lambda _conn: state)
    before = list(conn.iterdump())
    with pytest.raises(PermissionError, match="owning primary gateway"):
        correct_diagnostics(conn, tid, **request)
    assert list(conn.iterdump()) == before
