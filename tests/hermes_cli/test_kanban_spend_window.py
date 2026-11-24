"""A pre-promote limit promises a fresh allowance, not a cumulative old-window cap."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('token_limited', [False, True])
def test_set_before_promote_reports_and_enforces_fresh_allowance(kanban_home, monkeypatch, token_limited):
    now = 2_000_000_000
    monkeypatch.setattr(budget.time, 'time', lambda: now)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Bounded retry', assignee='default')
        helpers._runs(conn, tid, 8, usage={'cache_read_tokens': 100})
        # Historical stops can retain the spent window until promotion (the synced incident shape).
        with kb.write_txn(conn):
            kb._append_event(conn, tid, budget.EVENT, {'reason': 'eight spent', 'accounting_boundary': False})
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        assert budget.spend(conn, tid).runs == 8
    args = '--runs 0 --cache-read-tokens 200' if token_limited else '--runs 2 --cache-read-tokens 0'
    shown = cli.run_slash(f'spend-budget {tid} {args} --reason bounded')
    assert 'this limit applies to a fresh window starting at promote' in shown
    assert 'historical spend: 8 runs' in shown
    before = json.loads(cli.run_slash(f'spend-budget {tid} --json'))
    assert before['window'] == 'next_promote'
    assert before['spent']['runs'] == 8  # never fabricate zero historical spend
    with kbc.connect_closing() as conn:
        budget.hold(conn, tid, lim=helpers.LIMITS)
        counts = {kind: len(helpers._events(conn, tid, kind)) for kind in (budget.EVENT, 'spend_budget_decision_ready')}
        budget.hold(conn, tid, lim=helpers.LIMITS)
        assert counts == {kind: len(helpers._events(conn, tid, kind)) for kind in counts}
        assert counts[budget.EVENT] == 1  # no new hold against the old window/limit
        assert kb.get_task(conn, tid).status == 'triage'  # setting is not release
        assert 'fresh window starting at promote' in kb.list_comments(conn, tid)[0].body
        now += 10
        assert kb.promote_task(conn, tid, actor='operator', reason='bounded retry') == (True, None)
        assert budget.spend(conn, tid).runs == 0
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 0
        assert budget.spend(conn, tid).since == now
        for i in range(2):
            assert budget.hold(conn, tid, lim=helpers.LIMITS) is None
            now += 100
            with kb.write_txn(conn):
                rid = conn.execute("INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome) "
                                   "VALUES (?,'default','continued',?,?,'continued')",
                                   (tid, now - 90, now)).lastrowid
                kb._append_event(conn, tid, 'worker_result', {'usage': {'cache_read_tokens': 100}}, run_id=rid)
        assert budget.hold(conn, tid, lim=helpers.LIMITS) == 'triage'
        last = helpers._events(conn, tid, budget.EVENT)[-1].payload
        assert last['runs'] == 2 and last['tokens']['cache_read_tokens'] == 200
    assert json.loads(cli.run_slash(f'spend-budget {tid} --json'))['window'] == 'next_promote'


def test_ordinary_limit_counts_current_window_and_help_explains_both(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Ordinary', assignee='default')
        helpers._runs(conn, tid, 1)
    out = cli.run_slash(f'spend-budget {tid} --runs 2 --reason bounded')
    assert 'limit counts the current window' in out
    assert 'spent: 1 runs' in out
    assert json.loads(cli.run_slash(f'spend-budget {tid} --json'))['window'] == 'current'
    help_text = cli.run_slash('spend-budget --help')
    assert 'fresh window starting at promote' in ' '.join(help_text.split())
    assert 'current window' in ' '.join(help_text.split())


@pytest.mark.parametrize('cause', ['scope_stopped', 'safety_refusal'])
def test_restricted_stop_does_not_promise_or_open_fresh_window(kanban_home, cause):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Restricted', assignee='default')
        helpers._runs(conn, tid, 8)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, budget.EVENT, {'cause': cause, 'accounting_boundary': False})
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    out = cli.run_slash(f'spend-budget {tid} --runs 10 --reason bounded')
    assert 'limit counts the current window' in out
    assert 'fresh window' not in out
    with kbc.connect_closing() as conn:
        assert budget.spend(conn, tid).runs == 8
        assert not kb.promote_task(conn, tid, actor='operator', reason='budget only')[0]
        assert not helpers._events(conn, tid, 'promoted_manual')


def test_real_spend_stop_keeps_notice_until_promote(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Eight-run stop', assignee='default')
        helpers._runs(conn, tid, 8)
        assert budget.hold(conn, tid, lim=budget.Limits(8, 0)) == 'triage'
        before = {kind: len(helpers._events(conn, tid, kind))
                  for kind in (budget.EVENT, 'spend_budget_decision_ready')}
    out = cli.run_slash(f'spend-budget {tid} --runs 2 --reason bounded')
    assert 'fresh window starting at promote' in out
    with kbc.connect_closing() as conn:
        assert budget.hold(conn, tid, lim=helpers.LIMITS) == 'triage'
        assert before == {kind: len(helpers._events(conn, tid, kind)) for kind in before}
        assert kb.promote_task(conn, tid, actor='operator', reason='bounded retry')[0]
        assert budget.card_limits(conn, tid, helpers.LIMITS).runs == 2
        assert budget.spend(conn, tid).runs == 0
