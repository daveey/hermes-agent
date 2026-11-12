"""Failure-path identity scope and current-producer array presence (review t_01a1cf2ece397385 F3/F5)."""
from hermes_cli import kanban_execution_observations as observations
from hermes_cli import kanban_session_activity as activity
from hermes_cli.execution_observations import identity_key
from hermes_cli.kanban_execution_source import unknown_raw
from tests.hermes_cli import test_kanban_session_activity as activity_fixtures

kanban_root = activity_fixtures.kanban_root


def test_unrooted_board_failure_keeps_unevidenced_profile_not_the_readers(tmp_path):
    path = tmp_path / "elsewhere" / "kanban.db"
    path.parent.mkdir(parents=True)
    path.touch()
    assert observations.board_owner_profile(path) is None
    row = {"id": "t_x", "status": "ready", "board": "default",
           "_execution_scope": {"gateway": "gw", "profile": None, "board": "default"}}
    listed = observations.failed_card(row, gateway="gw", profile="coder", now=1000.0)
    counted = observations._project(unknown_raw(row, gateway="gw", profile=None, board="default"),
                                    now=1000.0, source_class="sidebar")
    assert listed["identity"].get("profile") is None
    assert identity_key(listed["identity"]) == identity_key(counted["identity"])


def test_source_with_null_profile_never_borrows_the_readers_profile():
    row = {"id": "t_y", "status": "running", "board": "default",
           "_execution_source": {"gateway": "gw", "profile": None, "board": "default"}}
    listed = observations.failed_card(row, gateway="gw", profile="coder", now=1000.0)
    assert listed["identity"].get("profile") is None


def test_unscoped_legacy_row_still_uses_reader_scope():
    listed = observations.failed_card({"id": "t_z", "status": "running", "board": "default"},
                                      gateway="gw", profile="coder", now=1000.0)
    assert listed["identity"].get("profile") == "coder"


def test_pending_closeout_only_row_still_carries_an_array(kanban_root):
    activity_fixtures._make_board(kanban_root / "kanban.db", [("t1", "running", "s_a"), ("t2", "done", "s_b")])
    activity.register_waiting_source("closeout", lambda: {"s_b": ["t2"]})
    try:
        rows = [{"id": "s_a"}, {"id": "s_b"}, {"id": "s_c"}]
        activity.annotate_kanban_activity(rows)
    finally:
        activity.register_waiting_source("closeout", None)
    s_a, s_b, s_c = rows
    assert "execution_observations" in s_a
    assert s_b["kanban_active"] == 1 and s_b.get("kanban_parked") == 0
    assert s_b["execution_observations"] == []  # current producer: not misread as a legacy backend
    assert "execution_observations" not in s_c and "kanban_active" not in s_c
