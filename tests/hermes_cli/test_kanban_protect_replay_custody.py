"""Protect's independent frozen transport retains lost-anchor safety and liveness."""
import json
import time
import uuid

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_protect_transport as transport
from hermes_cli import kanban_replay_journal as journal
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


class Ident:
    gateway = 'macbook'


class FakeHub:
    ident = Ident()

    def __init__(self):
        self.ops_calls = []
        self.events = {}

    def _call(self, method, path, body=None):
        assert path.endswith('/protect-stopped/capability'), path
        return {transport.KIND: 1, 'frozen_replay': 1, 'administrative_only': True}

    def row_evidence(self, board, table, uid):
        row = self.events.get(uid)
        return {'board': board, 'table': table, 'uid': uid, 'state': 'live' if row else 'never', 'head': 1, 'row': row}

    def ops(self, board, wires):
        self.ops_calls.extend(wires)
        return {'results': [{'client_op_id': wires[0]['client_op_id'], 'status': 'applied'}]}


def _reserved(conn, hub, *, captured=False):
    sync.set_mode(conn, 'primary')
    state = sync.sync_state(conn)
    home = state['gateway_id']
    hub.ident.gateway = home
    board = 'default'
    target = kb.create_task(conn, title='target', assignee='default')
    conn.execute('UPDATE tasks SET created_on=? WHERE id=?', (home, target)) if not conn.in_transaction else None
    conn.commit()
    actor, run, source, oldrun, request_event, request_uid = ['p-' + uuid.uuid4().hex for _ in range(6)]
    args = {'source_event_uid': source, 'source_run_uid': oldrun, 'expected_status': 'triage',
            'expected_current_run_uid': None, 'expected_updated_at': 1000, 'expected_episode': None,
            'requested_owner': actor, 'next_step': 'Resolve original administrative authority only',
            'recovery_trigger': 'Explicit supported disposition, never retry original scope',
            'escalate_at': int(time.time()) + 86400, 'evidence': [source]}
    payload = {'version': 1, 'request_uid': request_uid, 'action': 'protect_stopped', 'board': board, 'home': home,
               'target_task_id': target, 'requested_at': int(time.time()),
               'caller': {'gateway': home, 'task_id': actor, 'run_uid': run}, 'args': args}
    with kbc.write_txn(conn):
        if captured:
            from hermes_cli import kanban_home_generation as epochs
            epochs.install(conn)
            conn.execute('INSERT INTO kanban_home_generations1(task_id,nonce,pending) VALUES (?,?,0)',
                         (target, 'protect-captured-epoch'))
        frozen = transport.reserve(conn, board, payload, request_event)
    hub.events[request_event] = {'uid': request_event, 'deleted_at': None, 'kind': 'operator_call_requested',
                                 'version': 1, 'task_id': actor, 'run_uid': run, 'origin_gateway': home,
                                 'payload': json.dumps(payload)}
    return board, request_uid, frozen


@pytest.mark.parametrize('captured', [False, True])
@pytest.mark.parametrize('anchor', ['bound', 'never', 'null', 'missing'])
def test_protect_transport_respects_enrollment_without_starving_pending(live, anchor, captured):
    hub = FakeHub()
    board, request_uid, frozen = _reserved(live, hub, captured=captured)
    assert frozen['delivery_state'] == 'pending'
    assert journal.bound_custody(live) is True
    assert journal.pending_wire(live) is None
    if anchor != 'bound':
        with kbc.write_txn(live):
            if anchor == 'never':
                live.execute('DROP TABLE kanban_replay_sender1')
            if anchor == 'missing':
                live.execute('DELETE FROM kanban_sync_state')
            else:
                live.execute('UPDATE kanban_sync_state SET journal_id=NULL')
    # No canonical receipt is supplied: delivery must not imply confirmation.
    with pytest.raises(ValueError):
        transport.confirm(live, hub, board, request_uid)
    expected = json.loads(frozen['request_json'])
    if captured:
        expected['home_generations'] = {expected['task']: 'protect-captured-epoch'}
    assert hub.ops_calls == ([expected] if anchor in ('bound', 'never') else [])
    assert transport.command(live, request_uid) == frozen

