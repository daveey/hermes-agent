"""Registration observations must survive competing registrations and lease changes."""

import pytest

from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


def test_competing_observation_is_not_overwritten(setup, monkeypatch):
    conn, b, tid, source, _scratch, home, _clone = setup
    from hermes_cli import kanban_db_connect as connect
    reg = helpers.api()
    original = board.attached_run
    run_id, uid, machine = reg._current(conn, tid)
    winner = board.AttachedRun(run_id, tid, uid, "initial", 0, f"wt/{tid}",
                              str(source) + "-other", "winner-head", None, "pending", machine, 2)
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


def test_lease_changed_during_git_observation_never_records(setup, monkeypatch):
    conn, b, tid, source, _scratch, _home, _clone = setup
    reg = helpers.api()
    run_id, _uid, _machine = reg._current(conn, tid)
    original = reg._local_path

    def expire(*args):
        result = original(*args)
        conn.execute("UPDATE tasks SET lease_expires=1 WHERE id=?", (tid,))
        conn.commit()
        return result

    monkeypatch.setattr(reg, "_local_path", expire)
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        reg.register_initial(conn, tid, str(source))
    assert board.attached_run(conn, run_id) is None
    assert b.actions("source_attach") == []


@pytest.mark.parametrize("matches", [True, False])
def test_registration_uses_assigned_project_branch(setup, matches):
    conn, b, tid, source, _scratch, _home, _clone = setup
    branch = f"project/{tid}"
    helpers.git(source, "branch", "-m", branch)
    conn.execute("UPDATE tasks SET branch_name=? WHERE id=?", (branch if matches else f"other/{tid}", tid))
    conn.commit()
    if matches:
        assert helpers.api().register_initial(conn, tid, str(source)).branch == branch
    else:
        with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
            helpers.api().register_initial(conn, tid, str(source))
        assert b.actions("source_attach") == []
