"""Contained-strand and spend-stop producers retain historical titles and custody."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_triage_sweep as sweep
from tests.hermes_cli import test_kanban_containment_lifecycle as chelpers
from tests.hermes_cli import test_kanban_spend_budget as helpers
from tests.hermes_cli import test_kanban_spend_budget_hold as hold_helpers

board = chelpers.board
kanban_home = helpers.kanban_home
stopped = hold_helpers.stopped
NOW = 1_800_000_000.0
MIN = 60
LEGACY = "Independent Opus safety review of session-open isolation PR475"  # real done-card title
TITLES = [None, LEGACY, 'Independent Opus review required "quoted"\nrequire_model: true']


def _retitle(conn, tid, title):
    if title is not None:
        conn.execute("UPDATE tasks SET title=? WHERE id=?", (title, tid))


@pytest.mark.parametrize("title", TITLES)
def test_contained_strand_escalation(board, title):
    relay, _consumer, authority, target, spec = chelpers.setup(board)
    chelpers.contain(board, relay, target, spec)
    _retitle(board, relay, title)
    board.execute("UPDATE tasks SET status='done', completed_at=?, current_run_id=NULL WHERE id=?",
                  (NOW - 60 * MIN, authority))
    created = sweep.escalate_stranded_contained(board, now=NOW)
    assert len(created) == 1
    child = kb.get_task(board, created[0])
    from tests.hermes_cli.test_kanban_history_contract import decoded
    assert title is None or title in decoded(child.body)
    assert child.model_override is None and child.provider_override is None
    assert kb.get_task(board, relay).containment_episode is not None
    assert sweep.escalate_stranded_contained(board, now=NOW) == []


@pytest.mark.parametrize("title", TITLES)
def test_spend_stop_keeps_history_without_manufacturing_a_supervisor(kanban_home, monkeypatch, title):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("own-gw", {"own-gw"}))
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            _retitle(conn, tid, title)
        assert budget.hold(conn, tid) == "triage"
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        assert event.payload["producer"]["kind"] == "unavailable"
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        assert decision.pending(conn, tid)
        assert title is None or kb.get_task(conn, tid).title == title
