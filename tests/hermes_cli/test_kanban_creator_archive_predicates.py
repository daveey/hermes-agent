"""Unmasked boundaries for creator cleanup predicates."""
import pytest

from hermes_cli import kanban_creator_archive as archive
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_triage_cleanup as cleanup
from tests.hermes_cli import test_kanban_creator_archive_adversarial as helpers

fleet = helpers.fleet


def test_creator_only_action_refuses_general_capability_scope(fleet):
    from hermes_cli.kanban_archive_scope import archive_scope_for
    f = fleet
    tid = f.card(f.h, "capability owner", idempotency_key=f"capability-owner:{f.caller}:test")
    assert archive_scope_for(f.h, tid, f.caller)[0] is None
    assert archive.refusal(f.h, tid, f.caller) is not None
    assert kb.get_task(f.h, tid).status != "archived"


@pytest.mark.parametrize("requester", [None, ""])
def test_missing_requester_without_creator_cannot_gain_unscoped_authority(fleet, requester):
    f = fleet
    tid = f.card(f.h, "not creator owned")
    helpers.set_creator(f.h, tid, requester)
    assert archive.refusal(f.h, tid, requester) is not None


def test_missing_target_is_ineligible(fleet):
    assert cleanup.eligible(fleet.h, "t_0000000000000000") is False


@pytest.mark.parametrize("status", ["todo", "ready", "done", "archived"])
def test_only_triage_status_is_eligible(fleet, status):
    f = fleet
    _, tid = helpers.triage_fixture(f)
    assert cleanup.eligible(f.h, tid)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    assert cleanup.eligible(f.h, tid) is False
