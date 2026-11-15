"""Completion hands off the selected snapshot, never a second copy of its bytes."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_registration as registration
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


def test_completion_snapshot_survives_retry_and_ending_rollback(setup, tmp_path, monkeypatch):
    from tools.kanban_tools_source import stage_completion
    from hermes_cli.kanban_db_workspace import _managed_scratch_path_info
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    workspace = tmp_path / "workspaces" / tid
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(workspace.parent))
    conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(workspace), tid))
    conn.commit()
    assert _managed_scratch_path_info(workspace)[0]
    src = workspace / "report.txt"
    src.write_bytes(b"selected report")
    original = {"artifacts": [str(src)], "other": "kept"}
    result = stage_completion(conn, tid, original, expected_run_id=run.run_id)
    assert original["artifacts"] == [str(src)]
    stored = Path(result["artifacts"][0])
    assert stored != src and stored.read_bytes() == b"selected report"
    assert len(kb.list_attachments(conn, tid)) == 1
    assert stage_completion(conn, tid, original, expected_run_id=run.run_id) == result
    src.write_bytes(b"changed after snapshot")
    result["report"] = str(src)
    result["completion_receipt"] = {"version": 1, "items": [{
        "scope": "report", "disposition": "delivered", "remaining": [],
        "evidence": [{"ref": str(src), "observed_at": "2026-11-14T00:00:00+00:00",
                      "verification": "independently_checked"}],
    }]}
    result = kb._merge_completion_prose_artifacts(conn, tid, result, summary=str(src), result=None)
    assert result["artifacts"] == [str(stored)]
    with pytest.raises(RuntimeError, match="rollback"):
        with kb.write_txn(conn):
            assert kb._stage_completion_artifacts(conn, tid, result, 1) == []
            assert result["report"] == str(stored)
            assert result["attached_from_workspace"][str(src)] == str(stored)
            raise RuntimeError("rollback")
    assert stored.read_bytes() == b"selected report"
    assert len(kb.list_attachments(conn, tid)) == 1
    assert artifacts.selected(conn, tid, run.run_uid)
    assert stage_completion(conn, tid, {"artifacts": []}, expected_run_id=run.run_id) == {"artifacts": []}
    assert artifacts.selected(conn, tid, run.run_uid) == []
    assert stored.read_bytes() == b"selected report"


@pytest.mark.parametrize("run_kind", ["absent", "stale"])
def test_completion_never_infers_caller_run(setup, tmp_path, run_kind):
    from tools.kanban_tools_source import stage_completion
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    src = tmp_path / "report.txt"
    src.write_bytes(b"report")
    metadata = {"artifacts": [str(src)]}
    if run_kind == "absent":
        assert stage_completion(conn, tid, metadata, expected_run_id=None) == metadata
    else:
        with pytest.raises(kb.ArtifactPreservationError):
            stage_completion(conn, tid, metadata, expected_run_id=run.run_id + 1)
    assert artifacts.selected(conn, tid, run.run_uid) is None
    assert kb.list_attachments(conn, tid) == []
