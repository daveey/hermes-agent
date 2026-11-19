"""Audited diagnostic quotation must not rewrite acceptance or destinations."""
from __future__ import annotations

import importlib
import importlib.util
import sqlite3
from typing import cast

import pytest

from tests.hermes_cli import test_kanban_edit_operator_fields as fixtures

NO_BOARD = cast(sqlite3.Connection, None)
kanban_home = fixtures.kanban_home


def test_atomic_correction_keeps_original_comment_identity_and_audit(kanban_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_diagnostic_correction as correction

    body = "Diagnostic: origin chat @session:example.\nAcceptance: unchanged."
    comment = "Error: origin chat @session:example."
    with kbc.connect_closing() as conn:
        from hermes_cli import kanban_db_sync as sync
        sync.apply_sync_schema_steps(conn)
        conn.commit()
        task_id = kb.create_task(conn, title="Correction fixture", body=body)
        kb.add_comment(conn, task_id, "original-author", comment)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET uid = ? WHERE task_id = ?", ("fixture-comment-1", task_id))
        before = dict(conn.execute("SELECT * FROM task_comments WHERE task_id = ?", (task_id,)).fetchone())
        apply = getattr(correction, "correct_diagnostics", None)
        assert callable(apply), "audited atomic correction entry point is missing"
        result = apply(
            conn, task_id, comment_uid=before["uid"], expected_body=body, expected_comment=comment,
            body_diagnostic="origin chat @session:example", comment_diagnostic="origin chat @session:example",
            reason="The two occurrences reproduce an error; neither is a delivery destination.",
            request_id="fixture-correction-1",
        )
        after = dict(conn.execute("SELECT * FROM task_comments WHERE task_id = ?", (task_id,)).fetchone())
        assert after == {**before, "body": 'Error: "origin chat @session:example".'}
        assert kb.get_task(conn, task_id).body == 'Diagnostic: "origin chat @session:example".\nAcceptance: unchanged.'
        events = [event for event in kb.list_events(conn, task_id) if event.kind == "diagnostic_corrected"]
        assert len(events) == 1
        assert events[0].payload["before"] == {"body": body, "comment": comment}
        assert events[0].payload["comment_uid"] == before["uid"]
        assert result["request_id"] == "fixture-correction-1"
        assert "operator" not in result
        assert result["process_context"]["authority_model"] == "trusted-operator"
        assert result["process_context"]["worker_markers_present"] is False
        changes = conn.total_changes
        replay = apply(
            conn, task_id, comment_uid=before["uid"], expected_body=body, expected_comment=comment,
            body_diagnostic="origin chat @session:example", comment_diagnostic="origin chat @session:example",
            reason="The two occurrences reproduce an error; neither is a delivery destination.",
            request_id="fixture-correction-1",
        )
        assert replay == result
        assert conn.total_changes == changes
        # Unrelated history must not evict the durable replay identity.
        for _ in range(110):
            kb.add_comment(conn, task_id, "fixture-author", "Unrelated history")
        changes = conn.total_changes
        assert apply(
            conn, task_id, comment_uid=before["uid"], expected_body=body, expected_comment=comment,
            body_diagnostic="origin chat @session:example", comment_diagnostic="origin chat @session:example",
            reason="The two occurrences reproduce an error; neither is a delivery destination.",
            request_id="fixture-correction-1",
        ) == result
        assert conn.total_changes == changes
        with pytest.raises(ValueError, match="request_id conflict"):
            apply(
                conn, task_id, comment_uid=before["uid"], expected_body=body, expected_comment=comment,
                body_diagnostic="origin chat @session:example", comment_diagnostic="origin chat @session:example",
                reason="Different authority explanation must not reuse the key.",
                request_id="fixture-correction-1",
            )
        assert conn.total_changes == changes


@pytest.mark.parametrize("worker_key", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_worker_refused_before_reading_board(kanban_home, monkeypatch, worker_key):
    from hermes_cli.kanban_diagnostic_correction import correct_diagnostics

    monkeypatch.setenv(worker_key, "fixture-worker")
    with pytest.raises(PermissionError, match="independent non-worker operator"):
        correct_diagnostics(
            NO_BOARD, "fixture-card", comment_uid="fixture-comment", expected_body="error",
            expected_comment="error", body_diagnostic="error", comment_diagnostic="error",
            reason="test worker exclusion", request_id="fixture-worker-exclusion",
        )


@pytest.mark.parametrize(("field", "value"), [
    ("reason", "x" * 2001), ("request_id", "x" * 129),
    ("reason", None), ("request_id", []), ("expected_body", "x" * 100001),
    ("expected_comment", "x" * 50001), ("comment_uid", "x" * 202),
])
def test_invalid_request_refused_before_board_access(kanban_home, field, value):
    from hermes_cli.kanban_diagnostic_correction import correct_diagnostics

    request = dict(comment_uid="fixture-comment", expected_body="error", expected_comment="error",
                   body_diagnostic="error", comment_diagnostic="error", reason="fixture correction",
                   request_id="fixture-limits")
    request[field] = value
    with pytest.raises(ValueError, match="invalid correction field"):
        correct_diagnostics(NO_BOARD, "fixture-card", **request)


@pytest.mark.parametrize("stale", ["body", "comment"])
def test_stale_compare_preserves_both_fields_and_history(kanban_home, stale):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_diagnostic_correction import correct_diagnostics

    with kbc.connect_closing() as conn:
        sync.apply_sync_schema_steps(conn)
        conn.commit()
        tid = kb.create_task(conn, title="CAS fixture", body="Diagnostic error")
        kb.add_comment(conn, tid, "author", "Diagnostic error")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET uid = ? WHERE task_id = ?", ("fixture-cas", tid))
        before = list(conn.iterdump())
        with pytest.raises(ValueError, match="stale body or comment"):
            correct_diagnostics(
                conn, tid, comment_uid="fixture-cas",
                expected_body="Diagnostic error" + (" stale" if stale == "body" else ""),
                expected_comment="Diagnostic error" + (" stale" if stale == "comment" else ""),
                body_diagnostic="error", comment_diagnostic="error",
                reason="test CAS exclusion", request_id="fixture-cas",
            )
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize(("original", "diagnostic", "reason"), [
    ("No error here", "origin chat @session:example", "missing or ambiguous"),
    ("x x", "x", "missing or ambiguous"),
    ("Acceptance: x", "x", "acceptance"),
    ("x", "", "nonempty single-line"),
    ('a"b', 'a"b', "nonempty single-line"),
    ("a\nb", "a\nb", "nonempty single-line"),
])
def test_quote_plan_refuses_unsafe_spans(original, diagnostic, reason):
    module = importlib.import_module("hermes_cli.kanban_diagnostic_correction")
    with pytest.raises(ValueError, match=reason):
        module.quote_diagnostic(original, diagnostic)


def test_quote_plan_preserves_every_other_byte():
    spec = importlib.util.find_spec("hermes_cli.kanban_diagnostic_correction")
    assert spec is not None, "supported diagnostic correction planner is missing"
    module = importlib.import_module("hermes_cli.kanban_diagnostic_correction")
    original = (
        "Probe reproduces origin chat @session:example.\n\n"
        "Acceptance: deliver to @session:real-destination and retain all gates."
    )
    result = module.quote_diagnostic(original, "origin chat @session:example")
    assert result == (
        'Probe reproduces "origin chat @session:example".\n\n'
        "Acceptance: deliver to @session:real-destination and retain all gates."
    )
