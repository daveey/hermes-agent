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
    assert f'--{flag} {suggested}' in out and 'at once' in out, out
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


def test_every_tripped_field_is_named_including_the_board_fallback(kanban_home, monkeypatch):
    tid = _card_with_spend(7)
    out = cli.run_slash(f'spend-budget {tid} --runs 3 --cache-read-tokens 500 --reason x')
    assert '--runs 10' in out and '--cache-read-tokens 1200' in out, out
    # The override is replaced whole: a field not given falls back to the board's limit.
    monkeypatch.setattr(budget, 'limits', lambda cfg=None: budget.Limits(5, helpers.LIMITS.cache_read_tokens))
    out = cli.run_slash(f'spend-budget {tid} --cache-read-tokens 100000 --reason x')
    assert "the board's runs limit 5" in out and '--runs 12' in out, out
    with kbc.connect_closing() as conn:
        assert helpers._events(conn, tid, budget.OVERRIDE_EVENT) == []


@pytest.mark.parametrize('cause', ['scope_stopped', 'safety_refusal'])
def test_a_restricted_stop_keeps_its_history_so_a_spent_limit_is_refused(kanban_home, cause):
    tid = _card_with_spend(8)
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, budget.EVENT, {'cause': cause, 'accounting_boundary': False})
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        assert budget.limit_window(conn, tid) == 'current'
    assert '--runs 13' in cli.run_slash(f'spend-budget {tid} --runs 5 --reason x')
    cli.run_slash(f'spend-budget {tid} --runs 9 --reason x')
    with kbc.connect_closing() as conn:
        assert budget.card_limits(conn, tid, helpers.LIMITS).runs == 9


def test_clearing_back_to_a_board_limit_already_spent_is_refused(kanban_home, monkeypatch):
    tid = _card_with_spend(7)
    cli.run_slash(f'spend-budget {tid} --exempt --reason standing')
    monkeypatch.setattr(budget, 'limits', lambda cfg=None: budget.Limits(5, helpers.LIMITS.cache_read_tokens))
    out = cli.run_slash(f'spend-budget {tid} --clear --reason done')
    assert "the board's runs limit 5" in out and '--runs 12' in out and '--exempt' in out, out
    with kbc.connect_closing() as conn:
        assert budget.card_limits(conn, tid, budget.limits()) == (0, 0)
