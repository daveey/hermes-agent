"""A same-path replacement must not inherit an attached scratch source identity."""
from dataclasses import replace

import pytest

from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


def test_registration_persists_complete_identity_on_reopen(setup):
    from hermes_cli import kanban_db_connect as connect
    conn, _b, tid, source, _scratch, home, clone = setup
    run = helpers.api().register_initial(conn, tid, str(source))
    fresh = connect.connect(home / "kanban.db")
    try:
        identity = board.attached_run(fresh, run.run_id).source_identity
        assert identity == {"repository_id": 1, "full_name": "example/source",
                            "common_dir": str(clone / ".git"), "source_kind": "scratch_external"}
    finally:
        fresh.close()


@pytest.mark.parametrize("entry", ["register", "recover"])
def test_replaced_worktree_same_path_branch_and_head_is_refused(setup, entry):
    conn, b, tid, source, _scratch, _home, clone = setup
    reg = helpers.api()
    run = reg.register_initial(conn, tid, str(source))
    other = source.parent / "other-clone"
    helpers.git(clone, "clone", str(clone), str(other))
    helpers.git(other, "remote", "set-url", "origin", "https://github.com/example/source")
    helpers.git(clone, "worktree", "remove", str(source))
    helpers.git(other, "worktree", "add", str(source), f"wt/{tid}")
    ws.settings().clones.clear()
    ws.settings().clones["example/source"] = str(other)
    calls = len(b.calls)
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        if entry == "register":
            reg.register_initial(conn, tid, str(source))
        else:
            reg.recover_local(conn, tid, {"task_id": tid, "run_uid": run.run_uid, "machine": run.machine})
    assert len(b.calls) == calls
    assert board.attached_run(conn, run.run_id) == run


@pytest.mark.parametrize("field,value", [("repository_id", 2), ("full_name", "example/other"),
                                        ("source_kind", "task_worktree"), ("common_dir", "/other/.git")])
def test_each_persisted_identity_component_is_revalidated(setup, field, value):
    conn, b, tid, source, _scratch, _home, _clone = setup
    reg = helpers.api()
    run = reg.register_initial(conn, tid, str(source))
    board.record_run(conn, replace(run, source_identity={**run.source_identity, field: value}))
    calls = len(b.calls)
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        reg.register_initial(conn, tid, str(source))
    assert len(b.calls) == calls


def test_missing_v2_identity_is_not_silently_adopted(setup):
    conn, b, tid, source, _scratch, _home, _clone = setup
    reg = helpers.api()
    run = reg.register_initial(conn, tid, str(source))
    board.record_run(conn, replace(run, source_identity=None))
    calls = len(b.calls)
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        reg.recover_local(conn, tid, {"task_id": tid, "run_uid": run.run_uid, "machine": run.machine})
    assert len(b.calls) == calls
