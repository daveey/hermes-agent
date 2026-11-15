"""Journal migration and concurrent immutable source identity controls."""
from dataclasses import replace

import pytest

from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


def test_populated_legacy_journal_migrates_without_inventing_identity(setup):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    run = helpers.api().register_initial(conn, tid, str(source))
    conn.execute("ALTER TABLE kanban_source_runs DROP COLUMN source_identity")
    conn.commit()
    legacy = board.attached_run(conn, run.run_id)
    assert legacy == replace(run, source_identity=None)
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        helpers.api().recover_local(conn, tid, {"task_id": tid, "run_uid": run.run_uid, "machine": run.machine})
    following = replace(run, run_id=run.run_id + 100, run_uid="another")
    board.record_run(conn, following)
    board.record_run(conn, following)
    assert board.attached_run(conn, run.run_id) == legacy
    assert board.attached_run(conn, following.run_id) == following


def test_identity_change_racing_first_observation_preserves_winner(setup, monkeypatch):
    from hermes_cli import kanban_db_connect as connect
    conn, b, tid, source, _scratch, home, _clone = setup
    reg = helpers.api()
    run_id, uid, machine = reg._current(conn, tid)
    identity = reg.source_identity(str(source), "scratch_external")
    winner = board.AttachedRun(run_id, tid, uid, "initial", 0, f"wt/{tid}", str(source),
                              helpers.git(source, "rev-parse", "HEAD"), None, "pending", machine, 2,
                              {**identity, "repository_id": 2})
    original = board.attached_run
    fired = False

    def interleave(db, rid):
        nonlocal fired
        result = original(db, rid)
        if not fired and db is conn and rid == run_id:
            fired = True
            other = connect.connect(home / "kanban.db")
            try:
                board.record_run(other, winner)
            finally:
                other.close()
        return result

    monkeypatch.setattr(board, "attached_run", interleave)
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        reg.register_initial(conn, tid, str(source))
    assert original(conn, run_id) == winner
    assert b.actions("source_attach") == []
