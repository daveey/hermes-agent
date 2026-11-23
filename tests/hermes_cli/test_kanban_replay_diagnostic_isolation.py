"""Diagnostic retirement is scoped; trusted readback cannot release stale custody."""
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_notify_reconcile_exec as notices
from hermes_cli import kanban_replay_diagnostics as diagnostics
from tests.hermes_cli import test_kanban_replay_diagnostics as helpers

live = helpers.live
clock = helpers.clock


@pytest.mark.parametrize('other', ['task', 'board'])
def test_retiring_one_episode_preserves_other_scope_receipts(live, clock, monkeypatch, other):
    _, row = helpers.due(live, clock)
    key = row['notice_key']
    read = helpers.bind(live, key)
    sent = helpers.readback(read)
    monkeypatch.setattr(notices, '_sent', lambda ident: sent)
    assert diagnostics.record_delivery(live, key)
    stored = dict(live.execute('SELECT * FROM kanban_replay_diagnostic1').fetchone())
    receipt = json.loads(stored['receipt_json'])
    assert receipt['read'] == read
    assert receipt['evidence']['delivered'] == [key]
    # Unrelated scope has retained custody awaiting its own reconciliation.
    # Synchronizing this task is not authority to retire that other scope.
    board = row['board_incarnation'] + '-other' if other == 'board' else row['board_incarnation']
    task = row['task'] + '-other' if other == 'task' else row['task']
    with kbc.write_txn(live):
        live.execute('INSERT INTO kanban_replay_diagnostic1 '
                     '(board_incarnation,task,origin,evidence_sha256,notice_key,receipt_json) '
                     'VALUES (?,?,?,?,?,?)',
                     (board, task, row['origin'], row['evidence_sha256'], key + '-other', stored['receipt_json']))
        live.execute('DELETE FROM kanban_replay_episode1 WHERE board_incarnation=? AND task=?',
                     (row['board_incarnation'], row['task']))
        diagnostics.synchronize(live, row['board_incarnation'], row['task'])
    remaining = [dict(r) for r in live.execute('SELECT * FROM kanban_replay_diagnostic1')]
    assert len(remaining) == 1
    assert remaining[0]['notice_key'] == key + '-other'
    assert remaining[0]['receipt_json'] == stored['receipt_json']


@pytest.mark.parametrize('field,value', [('gateway', ''), ('profile', ''), ('chat', ''),
                                        ('gateway', None), ('profile', 3), ('chat', False)])
def test_invalid_owner_cannot_poison_immutable_binding(live, clock, field, value):
    _, row = helpers.due(live, clock)
    owner = dict(gateway='fixture-owner-gw', profile='default', chat='fixture-owner-chat')
    owner[field] = value
    before = list(live.iterdump())
    with pytest.raises(ValueError, match='owner identity required'):
        diagnostics.bind_owner(live, row['notice_key'], **owner)
    assert list(live.iterdump()) == before
    assert helpers.bind(live, row['notice_key'])['keys'] == [row['notice_key']]


def test_unbound_delivery_never_reads_owner_or_records_receipt(live, clock, monkeypatch):
    _, row = helpers.due(live, clock)
    reads = []
    monkeypatch.setattr(notices, '_sent', lambda ident: reads.append(ident))
    assert not diagnostics.record_delivery(live, row['notice_key'])
    assert reads == []
    assert diagnostics.pending(live)[0]['receipt_json'] is None


def test_unknown_key_cannot_bind_or_record_delivery(live, clock, monkeypatch):
    _, row = helpers.due(live, clock)
    reads = []
    monkeypatch.setattr(notices, '_sent', lambda ident: reads.append(ident))
    with pytest.raises(ValueError, match='no longer pending'):
        helpers.bind(live, row['notice_key'] + '-unknown')
    assert not diagnostics.record_delivery(live, row['notice_key'] + '-unknown')
    assert reads == []
    assert diagnostics.pending(live)[0]['notice_key'] == row['notice_key']


def test_producer_without_transaction_changes_nothing(live, clock):
    _, row = helpers.due(live, clock)
    before = list(live.iterdump())
    assert not live.in_transaction
    with pytest.raises(ValueError, match='caller transaction'):
        diagnostics.synchronize(live, row['board_incarnation'], row['task'])
    assert list(live.iterdump()) == before
