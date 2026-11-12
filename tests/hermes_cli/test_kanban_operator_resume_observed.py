"""A current worker-observed hold is evidence even when home releases."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_operator_resume as resume
from tests.hermes_cli import test_kanban_operator_resume as helpers

kanban_home = helpers.kanban_home
clock = helpers.clock


def test_current_observed_hold_allows_home_resume(kanban_home, clock, monkeypatch):
    with kbc.connect_closing() as conn:
        task = helpers._held(conn)
        clock[0] += 10
        with kb.write_txn(conn):
            kb._append_event(conn, task, "promoted")
            kb._append_event(conn, task, "respawn_guarded", {"reason": "active_pr"})
        monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *_: None)
        receipt = resume.resume(conn, task, reason="worker observed hold", actor="operator")
        assert receipt["scope"] == "one_continuation"
        assert resume.resumed_after(conn, task, helpers.T0)
        assert kb.claim_task(conn, task) is not None
        assert not resume.resumed_after(conn, task, helpers.T0)


@pytest.mark.parametrize("mode", ["absent", "old", "other", "claimed"])
def test_observation_must_be_in_current_ready_episode(kanban_home, clock, monkeypatch, mode):
    with kbc.connect_closing() as conn:
        task = helpers._held(conn)
        clock[0] += 10
        with kb.write_txn(conn):
            if mode == "old":
                kb._append_event(conn, task, "respawn_guarded", {"reason": "active_pr"})
            kb._append_event(conn, task, "promoted")
            if mode == "other":
                kb._append_event(conn, task, "respawn_guarded", {"reason": "shared_blocker"})
            if mode == "claimed":
                kb._append_event(conn, task, "respawn_guarded", {"reason": "active_pr"})
                kb._append_event(conn, task, "claimed")
        monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *_: None)
        with pytest.raises(resume.ResumeRefused, match="active_pr"):
            resume.resume(conn, task, reason="no current hold", actor="operator")


def test_observation_does_not_override_another_local_guard(kanban_home, clock, monkeypatch):
    # An imported active_pr observation widens only a local "no hold" verdict:
    # it never resumes a card this gateway holds for a different reason.
    with kbc.connect_closing() as conn:
        task = helpers._held(conn)
        clock[0] += 10
        with kb.write_txn(conn):
            kb._append_event(conn, task, "promoted")
            kb._append_event(conn, task, "respawn_guarded", {"reason": "active_pr"})
        monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *_: "shared_blocker")
        with pytest.raises(resume.ResumeRefused, match="guard: shared_blocker"):
            resume.resume(conn, task, reason="other hold", actor="operator")
