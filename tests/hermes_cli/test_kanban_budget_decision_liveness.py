"""Standalone limits must not masquerade as a decision on a renewed restricted stop."""
import contextlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_budget_cli as cli
from tests.hermes_cli import test_kanban_release as releases
from tests.hermes_cli import test_kanban_scope_renewal as scope

board = scope.board


def renewed(board, monkeypatch):
    tid, _, episode = scope.stopped(board, monkeypatch)
    # The synthetic stopped run made no model call; settle its accounting through the real result event.
    run_id = board.execute('SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC', (tid,)).fetchone()[0]
    with kb.write_txn(board):
        kb._append_event(board, tid, 'worker_result', {'usage_final': True, 'exit_code': 0, 'usage': {
            'input_tokens': 0, 'output_tokens': 0, 'cache_read_tokens': 0, 'cache_write_tokens': 0}}, run_id=run_id)
    scope.link(board, tid)
    comment = scope.mandate(board, tid)
    scope.renew(board, tid, episode, comment)
    assert decision.pending(board, tid)
    assert not decision.execution_restricted(board, tid)
    assert decision.restricted_floor(board, tid) is not None
    return tid


@pytest.mark.parametrize('flags', [('--runs', '59'), ('--exempt',), ('--clear',)])
def test_owner_budget_after_renewal_refuses_without_writes(board, monkeypatch, flags):
    tid = renewed(board, monkeypatch)
    before = releases.written(board, tid)
    rc, error = releases.spend_budget(board, monkeypatch, tid, *flags, '--reason', 'continue goal')
    assert rc == 1
    assert 'stop_decision_required' in error
    assert f'hermes kanban release {tid}' in error
    assert 'additional runs' in error
    assert releases.written(board, tid) == before
    assert budget.card_override(board, tid) is None
    assert decision.pending(board, tid)
    # The owning chat uses the actual validated release composition, not a bare budget append.
    out = releases.as_owner(board, tid, runs=7)
    assert out['ok'], out
    assert out['outcome'] == 'released'
    assert not decision.pending(board, tid)
    from hermes_cli.kanban_actionability_sources import describe
    assert 'private_hold' not in describe(board, tid)['violations']
    assert budget.hold(board, tid) is None
    assert not kb._spend_claim.claim_check(board, tid)()
    assert not kb._containment.held(board, tid)
    assert not kb.admission_hold_set(tid)
    assert kb.claim_task(board, tid, claimer='fixture-claim') is not None, kb.get_task(board, tid)


def test_budget_read_on_renewed_stop_stays_read_only(board, monkeypatch):
    tid = renewed(board, monkeypatch)
    before = releases.written(board, tid)
    rc, error = releases.spend_budget(board, monkeypatch, tid, '--json')
    assert rc == 0, error
    assert releases.written(board, tid) == before
    assert decision.pending(board, tid)


def test_ordinary_stop_and_completed_scope_decision_allow_budget_edits(board, monkeypatch):
    tid = releases.spend_stopped(board)
    rc, error = releases.spend_budget(board, monkeypatch, tid, '--runs', '59', '--reason', 'ordinary')
    assert rc == 0, error
    scoped = renewed(board, monkeypatch)
    out = releases.as_owner(board, scoped, runs=7)
    assert out['ok'], out
    prior = budget.card_override(board, scoped)
    assert prior is not None
    rc, error = releases.spend_budget(board, monkeypatch, scoped, '--runs', '59', '--reason', 'decided',
                                     '--seen', str(prior['decision_comment']['comment_id']))
    assert rc == 0, error


def test_stop_arriving_at_budget_writer_is_refused(board, monkeypatch):
    tid = kb.create_task(board, title='Race with budget writer', assignee='default')
    real = kb.write_txn
    inserted = False

    @contextlib.contextmanager
    def stopping(conn, *args, **kwargs):
        nonlocal inserted
        if not inserted:
            inserted = True
            with real(conn):
                conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))  # the stop's own writer
                kb._append_event(conn, tid, decision.EVENT, {'cause': 'scope_stopped', 'reason': 'scope ended'})
        with real(conn, *args, **kwargs) as transaction:
            yield transaction

    monkeypatch.setattr(kb, 'write_txn', stopping)
    rc, error = releases.spend_budget(board, monkeypatch, tid, '--runs', '59', '--reason', 'racing')
    assert rc == 1 and 'stop_decision_required' in error
    assert budget.card_override(board, tid) is None
    assert not board.execute('SELECT 1 FROM task_comments WHERE task_id=?', (tid,)).fetchone()


def test_json_refusal_is_structured_and_keeps_budget(board, monkeypatch, capsys):
    import argparse
    tid = renewed(board, monkeypatch)
    args = argparse.Namespace(task_id=tid, runs=59, cache_read_tokens=None, exempt=False, clear=False,
                              reason='continue goal', json=True)
    before = releases.written(board, tid)
    assert cli.command(args) == 1
    reply = json.loads(capsys.readouterr().out)
    assert reply['ok'] is False and reply['error'] == 'stop_decision_required'
    assert f'hermes kanban release {tid}' in reply['message']
    assert releases.written(board, tid) == before


@pytest.mark.parametrize('lane', ['scheduled', 'todo', 'ready', 'running'])
def test_refusal_applies_only_where_release_can_record_the_decision(board, monkeypatch, lane):
    """Live hub replay: promoted cards still reading a restricted stop as pending sit in todo/ready/running,
    where release answers not_held. Refusing their standalone budget there would leave no route at all."""
    tid = renewed(board, monkeypatch)
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET status=? WHERE id=?', (lane, tid))
    assert decision.pending(board, tid) and decision.restricted_floor(board, tid) is not None
    rc, error = releases.spend_budget(board, monkeypatch, tid, '--runs', '59', '--reason', 'continue goal')
    if lane == 'scheduled':  # release's held lanes: it is the route, and it is named
        assert rc == 1 and 'stop_decision_required' in error
        assert budget.card_override(board, tid) is None
    else:
        assert rc == 0, error
        assert budget.card_override(board, tid)['runs'] == 59


def test_a_card_that_is_gone_has_no_decision_to_require(board):
    """The writer reports an unknown task itself; the fence only answers for a card it can read."""
    from hermes_cli.kanban_spend_override_fence import require_decision
    assert require_decision(board, 't_missing') is None
