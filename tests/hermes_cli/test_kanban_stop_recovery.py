"""Recovery advice is typed evidence, never execution clearance."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_release as release
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_scope_renewal as scope

board = scope.board


def capability_stop(conn):
    tid = kb.create_task(conn, title='Capability unavailable', assignee='default')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, decision.EVENT,
                         {'cause': 'capability_unavailable', 'accounting_boundary': False})
    return tid


def refused(conn, tid):
    before = list(conn.iterdump())
    out = release.release(conn, tid, writer=('agent', 'foreign-chat', '42:HERMES_SESSION_ID'),
                          env={'HERMES_SESSION_ID': 'foreign-chat'}, ancestry=[{}], runs=10)
    assert out['refusal'] == 'not_scope_authority'
    assert list(conn.iterdump()) == before
    return out


def test_identity_refusal_exposes_capability_stop_and_guarded_promote(board):
    tid = capability_stop(board)
    out = refused(board, tid)
    recovery = out['recovery']
    assert recovery['stop']['cause'] == 'capability_unavailable'
    assert recovery['stop']['accounting_boundary'] is False
    assert recovery['next_operation'] == 'promote'
    assert recovery['authorization'] == 'not_granted'
    assert 'owning_chat' in recovery['expected_actor']
    assert '--dry-run --json' in recovery['instruction']
    assert 'run_in_user_terminal' in out['message']
    assert 'capability_unavailable' in out['message']


@pytest.mark.parametrize('restriction,operation', [
    ('scope', 'renew-scope'), ('safety', 'resolve_safety_boundary'),
    ('administrative', 'resolve_administrative_stop'), ('containment', 'resume-contained'),
])
def test_independent_restriction_dominates_later_capability_stop(board, monkeypatch, restriction, operation):
    from hermes_cli import kanban_stop_recovery as recovery
    tid, _, _ = scope.stopped(board, monkeypatch)
    if restriction == 'safety':
        kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal'})
    elif restriction == 'administrative':
        # Historical stop shape; the production administrative API requires a separate authenticated operator.
        from hermes_cli.kanban_stop_ended import KIND
        kb._append_event(board, tid, KIND, {'reason': 'fixture historical stop'})
    elif restriction == 'containment':
        monkeypatch.setattr('hermes_cli.kanban_containment_guard.pending', lambda *a, **kw: True)
    kb._append_event(board, tid, decision.EVENT, {'cause': 'capability_unavailable', 'accounting_boundary': False})
    board.commit()
    before = list(board.iterdump())
    advice = recovery.describe(board, tid)
    assert advice['next_operation'] == operation
    assert advice['authorization'] == 'not_granted'
    assert list(board.iterdump()) == before


def test_renewed_scope_does_not_hide_ordinary_recovery(board, monkeypatch):
    from hermes_cli import kanban_stop_recovery as recovery
    tid, _, episode = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    scope.renew(board, tid, episode, scope.mandate(board, tid))
    assert not decision.execution_restricted(board, tid)
    assert recovery.describe(board, tid)['next_operation'] == 'promote'


def test_refusal_names_fully_qualified_recorded_owner_not_observers(board):
    tid = capability_stop(board)
    scope.link(board, tid, gateway='owner-gateway', profile='default')
    scope.link(board, tid, gateway='other-gateway', session='observer', kind='shepherd')
    scope.link(board, tid, gateway='released-gateway', session='released', released=1)
    out = refused(board, tid)
    assert out['recovery']['recorded_authority']['chats'] == [
        {'gateway': 'owner-gateway', 'profile': 'default', 'session': scope.CHAT}]
    assert f'@session:default@owner-gateway/{scope.CHAT}' in out['message']
    assert '@session:default@other-gateway/observer' not in out['message']
    assert '@session:default@released-gateway/released' not in out['message']


def test_malformed_stop_never_advises_ordinary_promote(board):
    from hermes_cli import kanban_stop_recovery as recovery
    tid = capability_stop(board)
    board.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind=?",
                  (json.dumps({'cause': {'invalid': True}}), tid, decision.EVENT))
    board.commit()
    assert recovery.describe(board, tid)['next_operation'] == 'inspect_stop'


def test_advertised_preflight_parses(board):
    import argparse
    import shlex
    from hermes_cli.kanban_parser import build_parser
    from hermes_cli.kanban_stop_recovery import describe
    tid = capability_stop(board)
    command = describe(board, tid)['instruction'].split('`')[1]
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    args = parser.parse_args(shlex.split(command)[1:])
    assert args.task_id == tid and args.dry_run and args.json


def test_diagnostic_failure_preserves_original_refusal(board, monkeypatch):
    from hermes_cli import kanban_stop_recovery
    tid = capability_stop(board)
    def unavailable(*args):
        raise ValueError('diagnostic unavailable')
    monkeypatch.setattr(kanban_stop_recovery, 'refusal_context', unavailable)
    before = list(board.iterdump())
    out = release.release(board, tid, runs=0)
    assert out['refusal'] == 'invalid_runs'
    assert out['recovery']['next_operation'] == 'inspect_stop'
    assert list(board.iterdump()) == before


def test_sync_quarantine_has_its_own_recovery(board):
    from hermes_cli import kanban_sync_quarantine as quarantine
    from hermes_cli.kanban_stop_recovery import describe
    tid = capability_stop(board)
    for table, columns in quarantine._TABLES.items():
        board.execute(f'CREATE TABLE IF NOT EXISTS {table} ({columns})')
    board.execute('INSERT INTO containment_sync_quarantine VALUES (?,?,?,?,?,?,?)',
                  ('fixture-receipt', tid, 'fixture', 'contained', 'event_differs', '{}', 1))
    board.execute('INSERT INTO containment_sync_quarantine_tasks VALUES (?,?,?)',
                  (tid, 'fixture-receipt', 'direct'))
    board.commit()
    before = list(board.iterdump())
    advice = describe(board, tid)
    assert advice['next_operation'] == 'resolve_sync_quarantine'
    assert advice['instruction'] == quarantine.refusal(board, tid)
    assert list(board.iterdump()) == before


def test_authenticated_containment_release_is_not_still_held(board):
    from tests.hermes_cli import test_kanban_containment_lifecycle as lifecycle
    from hermes_cli.kanban_stop_recovery import describe
    relay, _consumer, authority, target, spec = lifecycle.setup(board)
    result = lifecycle.contain(board, relay, target, spec)
    assert describe(board, relay)['next_operation'] == 'resume-contained'
    lifecycle.release(board, target, result['episode'], authority)
    assert describe(board, relay)['next_operation'] != 'resume-contained'
