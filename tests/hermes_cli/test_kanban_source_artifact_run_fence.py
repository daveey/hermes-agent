"""Run-fenced staging checks the attached holder before and after snapshot I/O."""
from dataclasses import replace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_registration as registration
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


@pytest.mark.parametrize("fault", [None, "run", "lease", "ended", "observation", "run_ended", "outcome", "infinite", "mode", "uid", "gateway"])
@pytest.mark.parametrize("phase", ["before", "during"])
def test_stage_requires_same_live_attached_run(setup, tmp_path, monkeypatch, fault, phase):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    src = tmp_path / "evidence.txt"
    src.write_bytes(b"evidence")

    from hermes_cli import kanban_db_sync as sync

    faults = {
        "mode": lambda: sync.set_mode(conn, "shadow"),
        "uid": lambda: conn.execute("UPDATE task_runs SET uid='other' WHERE id=?", (run.run_id,)),
        "gateway": lambda: conn.execute("UPDATE kanban_sync_state SET gateway_id='elsewhere'"),
        "run": lambda: conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (tid,)),
        "lease": lambda: conn.execute("UPDATE tasks SET lease_expires=1 WHERE id=?", (tid,)),
        "ended": lambda: conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,)),
        "observation": lambda: board.record_run(conn, replace(run, state="pending")),
        "run_ended": lambda: conn.execute("UPDATE task_runs SET ended_at=1 WHERE id=?", (run.run_id,)),
        "outcome": lambda: conn.execute("UPDATE task_runs SET outcome='continued' WHERE id=?", (run.run_id,)),
        "infinite": lambda: conn.execute("UPDATE tasks SET lease_expires=? WHERE id=?", (float("inf"), tid)),
    }

    def change():
        if fault is not None:
            faults[fault]()
        conn.commit()

    if phase == "before":
        change()
    else:
        original = artifacts._snapshot

        def snapshot(path):
            result = original(path)
            change()
            return result

        monkeypatch.setattr(artifacts, "_snapshot", snapshot)
    if fault:
        with pytest.raises(artifacts.ArtifactRefused, match="live attached run"):
            artifacts.stage_for_run(conn, tid, [str(src)], board="default", expected_run_id=run.run_id)
        assert artifacts.selected(conn, tid, run.run_uid) is None
        assert kb.list_attachments(conn, tid) == []
    else:
        result = artifacts.stage_for_run(conn, tid, [str(src)], board="default", expected_run_id=run.run_id)
        assert artifacts.selected(conn, tid, run.run_uid) == result
        assert len(kb.list_attachments(conn, tid)) == 1
