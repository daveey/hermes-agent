"""Protocol-1 (v1 worktree) runs keep legacy artifact behaviour until protocol 2 is advertised.

Review t_01a1e00810ce8281: selection staging must not scan/snapshot v1 worktree
completion artifacts or turn them into a v2 artifact manifest.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_registration as registration
from tests.hermes_cli import test_kanban_source_registration as helpers
from tools.kanban_tools_source import heartbeat_source, stage_completion

setup = helpers.setup


def _as_v1_worktree(conn, tid, run):
    conn.execute("UPDATE kanban_source_runs SET mode='claim', source_protocol=1, source_identity=NULL "
                 "WHERE run_id=?", (run.run_id,))
    conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (tid,))
    conn.commit()


@pytest.mark.parametrize("content", [b"plain report", b"call +14155550100 about it"])
def test_v1_worktree_completion_keeps_legacy_artifacts(setup, tmp_path, content):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    _as_v1_worktree(conn, tid, run)
    assert board.attached_run(conn, run.run_id).source_protocol == 1
    assert kb.get_task(conn, tid).workspace_kind == "worktree"
    src = tmp_path / "report.txt"
    src.write_bytes(content)
    metadata = {"artifacts": [str(src)]}
    assert stage_completion(conn, tid, metadata, expected_run_id=run.run_id) is metadata
    assert artifacts.selected(conn, tid, run.run_uid) is None


def test_v1_worktree_heartbeat_selection_refused(setup, tmp_path):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    _as_v1_worktree(conn, tid, run)
    src = tmp_path / "report.txt"
    src.write_bytes(b"plain report")
    with pytest.raises(ValueError, match="protocol 2"):
        heartbeat_source(conn, tid, {"artifacts": [str(src)]}, worker_task=tid, expected_run_id=run.run_id)
    assert artifacts.selected(conn, tid, run.run_uid) is None


def test_v2_run_still_stages_selection(setup, tmp_path):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    assert board.attached_run(conn, run.run_id).source_protocol == 2
    src = tmp_path / "report.txt"
    src.write_bytes(b"plain report")
    out = stage_completion(conn, tid, {"artifacts": [str(src)]}, expected_run_id=run.run_id)
    assert out["artifacts"][0] != str(src)
    assert artifacts.selected(conn, tid, run.run_uid)
