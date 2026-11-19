"""Audited diagnostic quotation must not rewrite acceptance or destinations."""
from __future__ import annotations

import importlib
import importlib.util

import pytest

from tests.hermes_cli import test_kanban_edit_operator_fields as fixtures

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
