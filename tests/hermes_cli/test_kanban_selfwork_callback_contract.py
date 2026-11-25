"""Callback race contracts; mocked transition seams, not live authorization proof."""
import copy

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_selfwork_exception as sx
from hermes_cli import kanban_selfwork_local as local
from hermes_cli import kanban_selfwork_local_source as source
from tests.hermes_cli import test_kanban_selfwork_local as h

world = h.world
nochat_envelope = h.nochat_envelope
request_envelope = h.request_envelope
home = h.home
operation = h.operation


@pytest.mark.parametrize('case', ['invalid_stamp', 'conflict', 'replay', 'delivered', 'transition_refused'])
def test_delivery_entry_contract(home, operation, monkeypatch, case):
    with source.custody(local.RECOVERY_OPERATION) as actual, kbc.connect_closing() as conn:
        evidence = copy.deepcopy(actual)
        stamp = evidence['snapshot']['ask'][sx.STAMP]
        grant = local._grant(evidence, stamp, None)
        old = None
        if case == 'invalid_stamp':
            evidence['snapshot']['ask'][sx.STAMP] = {}
        elif case in ('conflict', 'replay'):
            old = dict(grant, operationId='different') if case == 'conflict' else grant
        elif case == 'transition_refused':
            evidence['operation']['dispatchState'] = 'recorded'
        monkeypatch.setattr(local, 'recorded', lambda *_: old)
        monkeypatch.setattr(local, 'eligible', lambda *_: stamp)
        calls = []
        monkeypatch.setattr(h.kb, 'unblock_task', lambda *a, **kw: calls.append(kw) or False)
        if case == 'replay':
            assert local._deliver(conn, evidence) == evidence['receipt']
        else:
            reason = {'invalid_stamp': 'local_stamp_invalid', 'conflict': 'local_operation_conflict',
                      'delivered': 'local_delivered_requires_recovery',
                      'transition_refused': 'local_transition_refused'}[case]
            with pytest.raises(sx.Refused, match='^' + reason + '$'):
                local._deliver(conn, evidence)
        assert len(calls) == (1 if case == 'transition_refused' else 0)


@pytest.mark.parametrize('case', ['conflict', 'replay', 'already_granted', 'status', 'ask', 'null_ask', 'generation', 'valid'])
def test_before_insert_rechecks_race_contract(home, operation, monkeypatch, case):
    with source.custody(local.RECOVERY_OPERATION) as actual, kbc.connect_closing() as conn:
        evidence = copy.deepcopy(actual)
        evidence['operation']['dispatchState'] = 'recorded'
        stamp = evidence['snapshot']['ask'][sx.STAMP]
        grant = local._grant(evidence, stamp, None)
        states = iter([None, dict(grant, fingerprint='conflict') if case == 'conflict'
                       else grant if case == 'replay' else None])
        monkeypatch.setattr(local, 'recorded', lambda *_: next(states))
        checks = []
        monkeypatch.setattr(local, 'eligible', lambda *_: checks.append(True) or stamp)
        monkeypatch.setattr(sx, '_grant', lambda *_: grant if case == 'already_granted' else None)
        generation = conn.execute('SELECT MAX(id) FROM task_events WHERE task_id=? AND kind IN '
                                  "('created','blocked','block_loop_detected','unblocked','archived','completed')",
                                  (home['task'],)).fetchone()[0]
        evidence['snapshot']['generation'] = generation + (1 if case == 'generation' else 0)
        conn.execute('UPDATE tasks SET status=?,human_request=? WHERE id=?',
                     ('ready' if case == 'status' else 'blocked',
                      None if case == 'null_ask' else source.encoded({} if case == 'ask' else evidence['snapshot']['ask']), home['task']))
        def transition(c, task, **kwargs):
            kwargs['precondition'](c)
            return True
        monkeypatch.setattr(h.kb, 'unblock_task', transition)
        if case in ('replay', 'valid'):
            assert local._deliver(conn, evidence) == evidence['receipt']
        else:
            reason = ('local_operation_conflict' if case == 'conflict' else
                      'local_offer_already_granted' if case == 'already_granted' else 'local_ask_changed')
            with pytest.raises(sx.Refused, match='^' + reason + '$'):
                local._deliver(conn, evidence)
        assert len(checks) == (1 if case in ('conflict', 'replay') else 2)


@pytest.mark.parametrize('case', ['invalid_stamp', 'conflict', 'replay', 'already_granted'])
def test_recovery_entry_contract(home, operation, monkeypatch, case):
    from contextlib import contextmanager
    with source.custody(local.RECOVERY_OPERATION) as actual:
        evidence = copy.deepcopy(actual)
    stamp = evidence['snapshot']['ask'][sx.STAMP]
    grant = local._grant(evidence, stamp, None)
    if case == 'invalid_stamp':
        evidence['snapshot']['ask'][sx.STAMP] = {}
    @contextmanager
    def custody(oid, *, delivered):
        assert oid == local.RECOVERY_OPERATION and delivered is True
        yield evidence
    monkeypatch.setattr(source, 'custody', custody)
    old = dict(grant, operationId='different') if case == 'conflict' else grant if case == 'replay' else None
    monkeypatch.setattr(local, 'recorded', lambda *_: old)
    monkeypatch.setattr(local, 'eligible', lambda *_: stamp)
    monkeypatch.setattr(sx, '_grant', lambda *_: grant)
    with kbc.connect_closing() as conn:
        before = sx._own(conn, local.RECOVERED)
        if case == 'replay':
            assert local.recover(conn, home['hub'], local.RECOVERY_OPERATION) == grant
        else:
            reason = {'invalid_stamp': 'local_recovery_stamp_invalid', 'conflict': 'local_grant_operation_conflict',
                      'already_granted': 'local_offer_already_granted'}[case]
            with pytest.raises(sx.Refused, match='^' + reason + '$'):
                local.recover(conn, home['hub'], local.RECOVERY_OPERATION)
        assert sx._own(conn, local.RECOVERED) == before
