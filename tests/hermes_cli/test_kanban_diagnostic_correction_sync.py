"""Real capture/translate/replica with isolated Python Board stand-in."""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_diagnostic_correction import correct_diagnostics
from tests.hermes_cli import test_kanban_sync_terminal_regress as fixtures

kanban_home = fixtures.kanban_home
svc = fixtures.svc
live = fixtures.live


def test_correction_converges_body_comment_and_audit(live, kanban_home, svc):
    body = "Diagnostic error. Acceptance: preserve all requirements."
    comment = "Observed error."
    tid = kb.create_task(live, title="Diagnostic transport", body=body)
    kb.add_comment(live, tid, "original-author", comment)
    original = dict(live.execute("SELECT * FROM task_comments WHERE task_id = ?", (tid,)).fetchone())
    uid = original["uid"]
    assert uid
    fixtures._round(kanban_home, svc)
    request = dict(comment_uid=uid, expected_body=body, expected_comment=comment,
                   body_diagnostic="error", comment_diagnostic="error",
                   reason="Diagnosed prose, not a destination", request_id="transport-correction")
    result = correct_diagnostics(live, tid, **request)
    fixtures._round(kanban_home, svc)
    rows = svc.board(fixtures.BOARD)["rows"]
    assert rows["tasks"][tid]["body"] == result["after"]["body"]
    assert rows["task_comments"][uid]["body"] == result["after"]["comment"]
    assert rows["task_comments"][uid]["author"] == original["author"]
    with kbc.connect_closing(ksc.shadow_path(fixtures.BOARD)) as replica:
        assert replica.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == result["after"]["body"]
        copied = dict(replica.execute("SELECT * FROM task_comments WHERE uid = ?", (uid,)).fetchone())
        for key in ("uid", "task_id", "author", "created_at"):
            assert copied[key] == original[key]
        assert copied["body"] == result["after"]["comment"]
        audit = replica.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                                (tid, "diagnostic_corrected")).fetchall()
        assert len(audit) == 1
        assert json.loads(audit[0][0]) == result
    before = list(live.iterdump())
    assert correct_diagnostics(live, tid, **request) == result
    assert list(live.iterdump()) == before
    assert result["before"] == {"body": body, "comment": comment}
    assert result["after"]["body"].split("Acceptance:")[1] == body.split("Acceptance:")[1]
