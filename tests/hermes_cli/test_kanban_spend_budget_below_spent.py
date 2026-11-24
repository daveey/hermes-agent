"""A card limit at or below this window's recorded spend is refused, not applied as an instant stop.

Regression for t_01a205c88aff2581 (2026-11-24): its creator set ``--runs 3`` meaning "3 more runs" on a card
that had already spent 7 in the window, and the dispatcher sent it to triage at the next tick.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


def _card_with_spend(runs: int, cache: int = 100) -> str:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Phase sample', assignee='default')
        helpers._runs(conn, tid, runs, usage={'cache_read_tokens': cache})
    return tid


@pytest.mark.parametrize('flag,limit,suggested', [('runs', 3, 10), ('runs', 7, 14),
                                                  ('cache-read-tokens', 500, 1200)])
def test_a_limit_already_spent_is_refused_and_names_the_limit_that_grants_more(kanban_home, flag, limit, suggested):
    tid = _card_with_spend(7)
    out = cli.run_slash(f'spend-budget {tid} --{flag} {limit} --reason "sample phase"')
    assert f'--{flag} {suggested}' in out and 'triage' in out, out
    with kbc.connect_closing() as conn:
        assert helpers._events(conn, tid, budget.OVERRIDE_EVENT) == []
        assert budget.hold(conn, tid, lim=helpers.LIMITS) is None
        assert kb.get_task(conn, tid).status != 'triage'


def test_a_limit_above_the_spend_and_exempt_or_clear_still_apply(kanban_home):
    tid = _card_with_spend(7)
    cli.run_slash(f'spend-budget {tid} --runs 10 --reason "three more"')
    shown = json.loads(cli.run_slash(f'spend-budget {tid} --json'))
    assert shown['limits']['runs'] == 10 and shown['spent']['runs'] == 7
    cli.run_slash(f'spend-budget {tid} --exempt --reason standing')
    cli.run_slash(f'spend-budget {tid} --clear --reason done')
    with kbc.connect_closing() as conn:
        assert len(helpers._events(conn, tid, budget.OVERRIDE_EVENT)) == 3


def test_a_pending_stop_counts_a_fresh_window_so_a_small_limit_is_accepted(kanban_home):
    tid = _card_with_spend(8)
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, budget.EVENT, {'reason': 'eight spent', 'accounting_boundary': False})
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        assert budget.limit_window(conn, tid) == 'next_promote'
    out = cli.run_slash(f'spend-budget {tid} --runs 2 --reason bounded')
    assert 'fresh window starting at promote' in out, out
    with kbc.connect_closing() as conn:
        assert budget.card_limits(conn, tid, helpers.LIMITS).runs == 2
