"""Local closure custody checks not implied by the status/marker fence."""
import pytest

from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_containment_lifecycle as life
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as fixtures

board = fixtures.board


def test_archived_local_card_cannot_gain_another_closure(board):
    relay, _, authority, target, spec = fixtures.setup(board)
    fixtures.contain(board, relay, target, spec)
    assert kb.archive_task(board, authority)
    assert kb.archive_task(board, relay)
    before = board.total_changes
    with pytest.raises(life.ContainmentRefused, match="not an unclaimed triage task"):
        kb.archive_task(board, relay, comment=("owner", "again"))
    assert board.total_changes == before
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='containment_closed'", (relay,)).fetchone()[0] == 1


def test_authority_on_another_gateway_cannot_be_closed_locally(board):
    relay, _, authority, target, spec = fixtures.setup(board)
    spec["authority"]["gateway"] = "another-home"
    fixtures.contain(board, relay, target, spec)
    assert kb.archive_task(board, authority)
    with pytest.raises(life.ContainmentRefused, match="local authority identity differs"):
        kb.archive_task(board, relay)
    assert kb.get_task(board, relay).status == "triage"
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='containment_closed'", (relay,)).fetchone()


def test_valid_stop_does_not_override_a_second_unresolved_stop(board):
    relay, _, authority, target, spec = fixtures.setup(board)
    fixtures.contain(board, relay, target, spec)
    with kb.write_txn(board), guard.authorized_write(board, relay):
        life._append(board, relay, "contained", {})
    assert kb.archive_task(board, authority)
    with pytest.raises(life.ContainmentRefused, match="not an unclaimed triage task"):
        kb.archive_task(board, relay)
    assert kb.get_task(board, relay).status == "triage"
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='containment_closed'", (relay,)).fetchone()
