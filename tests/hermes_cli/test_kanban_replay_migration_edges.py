"""Dormant migration binding and malformed-context controls; no real drainage claim."""
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_admission as admission
from hermes_cli import kanban_replay_migration as migration
from hermes_cli.kanban_sync_guard import SyncRoundBusy, sender_lock
from tests.hermes_cli import test_kanban_replay_migration as contract
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live


def test_memory_image_is_not_migration_custody(live):
    expected, payload, key = contract.fixtures(live)
    with sqlite3.connect(':memory:') as memory:
        live.backup(memory)
        with pytest.raises(ValueError, match='on-disk custody'):
            contract.send(memory, expected, payload, key)


@pytest.mark.parametrize('anchor', ['held', None, 'recreated-journal'])
def test_admission_refuses_before_receipt_or_peer_io(live, anchor):
    expected, _payload, _key = contract.fixtures(live)
    with kbc.write_txn(live):
        if anchor == 'held':
            live.execute('UPDATE kanban_sync_state SET replay_restore_required=1')
        else:
            live.execute('UPDATE kanban_sync_state SET journal_id=?', (anchor,))
    source = [tuple(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')]
    peer = sender.Receiver()
    # Deliberately invalid receipt: RestoreRequired proves admission has precedence.
    with pytest.raises(admission.RestoreRequired, match='restore reconciliation'):
        migration.push(live, peer, 'default', 'macbook', primary=False,
                       expectation=expected, receipt=None, public_key=b'', now_ms=2000)
    assert peer.calls == []
    assert live.execute('SELECT replay_restore_required FROM kanban_sync_state').fetchone()[0] == 1
    assert [tuple(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')] == source


def test_dormant_migration_uses_the_existing_sender_fence(live):
    expected, payload, key = contract.fixtures(live)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    before = list(live.iterdump())
    with sender_lock(path):
        with pytest.raises(SyncRoundBusy):
            contract.send(live, expected, payload, key)
    assert list(live.iterdump()) == before
    peer, _ = contract.send(live, expected, payload, key)
    assert peer.calls[:3] == ['capability', 'inspect', 'session']


@pytest.mark.parametrize('defect', [
    'coverage', 'revision_zero', 'revision_bool', 'unsorted_principals', 'empty_principal',
    'nonstring_principal', 'missing_expectation', 'extra_expectation', 'empty_identity',
    'barriers_not_list', 'readback_bool', 'readback_negative', 'extra_barrier',
    'empty_barrier_id', 'zero_barrier_position', 'reversed_barriers', 'boolean_now',
])
def test_authentic_but_structurally_invalid_contract_refuses(live, defect):
    expected, payload, key = contract.fixtures(live)
    mutations = {
        'coverage': lambda: expected['inventory'].update(coverage='current_only'),
        'revision_zero': lambda: expected['inventory'].update(revision=0),
        'revision_bool': lambda: expected['inventory'].update(revision=True),
        'unsorted_principals': lambda: expected['inventory']['principals'].reverse(),
        'empty_principal': lambda: expected['inventory']['principals'].append(''),
        'nonstring_principal': lambda: expected['inventory']['principals'].append(3),
        'missing_expectation': lambda: expected.pop('challenge'),
        'extra_expectation': lambda: expected.update(authenticated=True),
        'empty_identity': lambda: expected.update(migration_id=''),
        'barriers_not_list': lambda: payload.update(barriers={}),
        'readback_bool': lambda: payload.update(readback_position=True),
        'readback_negative': lambda: payload.update(readback_position=-1),
        'extra_barrier': lambda: payload['barriers'][0].update(authenticated=True),
        'empty_barrier_id': lambda: payload['barriers'][0].update(barrier_id=''),
        'zero_barrier_position': lambda: payload['barriers'][0].update(position=0),
        'reversed_barriers': lambda: payload['barriers'].reverse(),
        'boolean_now': lambda: None,
    }
    mutations[defect]()
    before = list(live.iterdump())
    with pytest.raises(ValueError, match='migration'):
        contract.send(live, expected, payload, key, now_ms=True if defect == 'boolean_now' else 2000)
    assert list(live.iterdump()) == before
