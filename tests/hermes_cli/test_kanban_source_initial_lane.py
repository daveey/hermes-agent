"""Initial custody belongs to implementation claims, including replay and I/O races."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_source_registration as reg
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


def review_lane(conn, tid):
    run_id = kb.get_task(conn, tid).current_run_id
    row = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'",
                       (tid, run_id)).fetchone()
    payload = json.loads(row[1])
    payload["source_status"] = "review"
    conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
    conn.commit()


@pytest.mark.parametrize("when", ["before", "replay", "during_proof"])
def test_initial_review_lane_refused_before_attach(setup, monkeypatch, when):
    conn, remote, tid, source, _scratch, _home, _clone = setup
    prior = None
    if when == "replay":
        prior = reg.register_initial(conn, tid, str(source))
    calls = len(remote.actions("source_attach"))
    if when == "during_proof":
        original = reg.source_identity

        def proof(*args):
            result = original(*args)
            review_lane(conn, tid)
            return result

        monkeypatch.setattr(reg, "source_identity", proof)
    else:
        review_lane(conn, tid)
        if when == "before":
            def unexpected_proof(*args):
                pytest.fail("review claim reached Git identity proof")
            monkeypatch.setattr(reg, "source_identity", unexpected_proof)
    with pytest.raises(launch.AttachRefused, match="initial requires an implementation claim"):
        reg.register_initial(conn, tid, str(source))
    assert len(remote.actions("source_attach")) == calls
    assert board.attached_run(conn, kb.get_task(conn, tid).current_run_id) == prior


def test_review_successor_can_resume(setup, monkeypatch):
    original = reg.predecessor_for_grant

    def predecessor(conn, tid, grant, custody):
        result = original(conn, tid, grant, custody)
        review_lane(conn, tid)
        return result

    monkeypatch.setattr(reg, "predecessor_for_grant", predecessor)
    helpers.test_successor_uses_grant_predecessor_after_attached_uid_is_null(setup, None)


def test_implementation_initial_still_replays(setup):
    conn, _remote, tid, source, _scratch, _home, _clone = setup
    first = reg.register_initial(conn, tid, str(source))
    assert reg.register_initial(conn, tid, str(source)) == first
