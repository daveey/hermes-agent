"""kanban_complete shows its own custody warnings; displays never offer a foreign flat path."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

import json

from tests.tools import test_kanban_tools as helpers

worker_env = helpers.worker_env

MISSING = "File missing or not a regular file on executing machine"


def _scratch(worker_env):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_workspace as kbw

    with kbc.connect() as conn:
        ws = kbw.resolve_workspace(kb.get_task(conn, worker_env))
        kbw.set_workspace_path(conn, worker_env, ws)
    return ws


def test_complete_result_surfaces_receipt_warnings(worker_env, tmp_path):
    """The live proof's worker read only the tool result and saw no missing-file warning."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    present = _scratch(worker_env) / "artifact-proof.txt"
    present.write_text("proof")
    missing = tmp_path / "does-not-exist-proof.txt"

    out = json.loads(kt._handle_complete({
        "summary": "done", "artifacts": [str(present), str(missing)]}))

    assert out["ok"] is True, out
    with kbc.connect() as conn:
        stored = kb.latest_run(conn, worker_env).metadata["artifact_receipt"]
    assert out["artifact_receipt"] == stored
    machine = stored[0]["machine"]
    assert out["artifact_warnings"] == [f"{machine}:{missing} — {MISSING}"]


def test_complete_result_has_no_warnings_when_all_files_kept(worker_env):
    from tools import kanban_tools as kt

    present = _scratch(worker_env) / "kept.txt"
    present.write_text("kept")
    out = json.loads(kt._handle_complete({"summary": "done", "artifacts": [str(present)]}))
    assert out["ok"] is True, out
    assert out["artifact_warnings"] == []
    assert [r["path"] for r in out["artifact_receipt"]] == [str(present)]


def test_receipt_resolves_by_uid_and_handoff_display_is_machine_qualified(worker_env, tmp_path):
    """The executor's flat attachment path is not where another replica keeps the bytes."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    report = tmp_path / "artifact-proof.txt"
    report.write_text("proof")
    with kbc.connect() as conn:
        child = kb.create_task(conn, title="downstream", assignee="test-worker", parents=[worker_env])
    out = json.loads(kt._handle_complete({"summary": "done", "artifacts": [str(report)]}))
    assert out["ok"] is True, out

    with kbc.connect() as conn:
        meta = kb.latest_run(conn, worker_env).metadata
        record = meta["artifact_receipt"][0]
        assert "attachment_path" not in record
        (row,) = kb.list_attachments(conn, worker_env)
        assert record["attachment_id"] == row.id
        flat = meta["artifacts"][0]
        assert flat == row.stored_path  # executor-local legacy consumers keep their local copy
        context = kb.build_worker_context(conn, child)

    machine = record["machine"]
    assert json.dumps(f"{machine}:{flat}") in context
    assert json.dumps(flat) not in context
    assert "attachment_path" not in context
