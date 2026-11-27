"""Captured epochs compose with enrollment-gated confirmation after rebase."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc, kanban_db_sync as sync, kanban_home_generation as epochs
from hermes_cli import kanban_retain_transport as retain
from tests.hermes_cli import test_kanban_failure_transport as failure
from tests.hermes_cli import test_kanban_retain_transport as fixture
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live


@pytest.mark.parametrize('anchor', ['bound', 'never', 'null', 'missing'])
@pytest.mark.parametrize('state', ['pending', 'acknowledged'])
def test_retain_confirmation_preserves_epoch_and_replay_enrollment(live, anchor, state):
    sync.set_mode(live, 'primary')
    task, rid = failure._running(live)
    with kbc.write_txn(live):
        epochs.install(live)
        live.execute('INSERT INTO kanban_home_generations1(task_id,nonce,pending) VALUES (?,?,0)',
                     (task, 'captured-confirmation-epoch'))
    owned = fixture.reserve(live, task, rid)
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_failure_commands SET delivery_state=? WHERE run_uid=?',
                     (state, owned['run_uid']))
        live.execute('UPDATE kanban_home_generations1 SET nonce=? WHERE task_id=?',
                     ('new-mirror-not-capture', task))
        if anchor == 'never':
            live.execute('DROP TABLE kanban_replay_sender1')
            live.execute('UPDATE kanban_sync_state SET journal_id=NULL')
        elif anchor == 'null':
            live.execute('UPDATE kanban_sync_state SET journal_id=NULL')
        elif anchor == 'missing':
            live.execute('DELETE FROM kanban_sync_state')
    before = retain.command(live, owned['run_uid'])
    calls = []
    evidence = []

    class Peer:
        ident = SimpleNamespace(gateway='macbook')

        def decision_retain_capability(self, board):
            return {'decision_retain': 1, 'frozen_replay': 1, 'hlc': '0000000000001-0000-server'}

        def ops(self, board, wires):
            calls.extend(wires)
            return {'results': [{'client_op_id': owned['op_id'], 'status': 'applied'}]}

        def row_evidence(self, board, table, uid):
            evidence.append((table, uid))
            raise LookupError('independent receipt intentionally unavailable')

    with pytest.raises((ValueError, RuntimeError, LookupError)):
        retain.confirm(live, Peer(), 'default', owned['run_uid'])
    expected = {**json.loads(owned['request_json']),
                'home_generations': {task: 'captured-confirmation-epoch'}}
    assert calls == ([expected] if anchor == 'never' else [])
    assert bool(evidence) == (anchor == 'never' or anchor == 'bound' and state == 'acknowledged')
    assert retain.command(live, owned['run_uid']) == before
