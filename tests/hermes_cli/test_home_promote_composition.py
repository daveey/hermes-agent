"""Transfer custody survives the newer composable promotion entrypoints."""
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb, kanban_db_promote as promote
from tests.hermes_cli import test_home_release_candidates as support

board = support.board
card = support.card
freeze = support.freeze


@pytest.mark.parametrize("phase", ("prepared", "pending", "malformed"))
@pytest.mark.parametrize("entry", ("public", "dry", "body", "admitted"))
def test_frozen_promotion_refuses_before_effects(board, phase, entry):
    task = card(board)
    freeze(board, task, phase)
    before = board.total_changes
    if entry in ("public", "dry"):
        result = promote.promote_task(board, task, actor="owner", dry_run=entry == "dry")
    else:
        with kb.write_txn(board):
            if entry == "admitted":
                result = promote._promote_in_txn(board, task, actor="owner", admission=object())
            else:
                result = promote.promote_in_txn(board, task, actor="owner")
    assert result == (False, "home transfer owns this card; promotion deferred")
    assert board.total_changes == before
    assert kb.get_task(board, task).status == "triage"


@pytest.mark.parametrize("phase", ("prepared", "pending", "malformed"))
def test_public_promotion_rechecks_custody_at_writer_entry(board, monkeypatch, phase):
    task = card(board)
    original = kb.write_txn
    observed = []

    @contextmanager
    def acquire(conn, **kwargs):
        freeze(conn, task, phase)
        observed.append(conn.total_changes)
        with original(conn, **kwargs):
            yield

    monkeypatch.setattr(kb, "write_txn", acquire)
    assert promote.promote_task(board, task, actor="owner") == (
        False, "home transfer owns this card; promotion deferred")
    assert board.total_changes == observed[0]
    assert kb.get_task(board, task).status == "triage"


@pytest.mark.parametrize("entry", ("public", "body"))
def test_ordinary_promotion_still_progresses(board, entry):
    task = card(board)
    if entry == "public":
        result = promote.promote_task(board, task, actor="owner")
    else:
        with kb.write_txn(board):
            result = promote.promote_in_txn(board, task, actor="owner")
    assert result == (True, None)
    assert kb.get_task(board, task).status == "ready"
