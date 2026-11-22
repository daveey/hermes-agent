"""Synthetic authenticated-route fixtures; ACK alone never settles a retained actor."""
import copy
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_retain_transport as transport
from tests.hermes_cli import test_kanban_retain_transport as support

live = support.live


def fixture(conn):
    tid, rid = support.support._running(conn)
    owned = support.reserve(conn, tid, rid)
    wire = json.loads(owned['request_json'])
    episode = 'fixture-stop'
    gateway = 'fixture-gateway'
    receipt = {'version': 1, 'task_uid': tid, 'run_uid': owned['run_uid'], 'request_uid': owned['request_uid'],
               'event_uid': owned['event_uid'], 'request_sha256': owned['request_sha256'],
               'episode_uid': episode, 'actor_gateway': gateway, 'operation_kind': 'retain',
               'superseded_by_safety_stop': False, 'disposition': wire['payload'], 'next_action': {'owner': 'fixture'}}
    rows = {('task_events', owned['event_uid']): {'uid': owned['event_uid'], 'task_id': tid, 'run_uid': owned['run_uid'],
            'kind': 'decision_retained', 'payload': json.dumps(receipt), 'origin_gateway': gateway, 'version': 1, 'seq': 42},
            ('task_events', episode): {'uid': episode, 'task_id': tid, 'kind': 'spend_budget_triaged'},
            ('task_runs', owned['run_uid']): {'uid': owned['run_uid'], 'task_id': tid, 'ended_at': 123, 'outcome': 'retained', 'status': 'retained'},
            ('tasks', tid): {'uid': tid, 'current_run_uid': None, 'status': 'triage'}}
    class Hub:
        ident = SimpleNamespace(gateway=gateway)
        verdict = 'applied'
        calls = []
        def decision_retain_capability(self, board):
            return {'decision_retain': 1, 'frozen_replay': 1, 'hlc': '0000000000001-0000-server'}
        def ops(self, board, ops):
            self.calls.append(copy.deepcopy(ops))
            assert ops == [wire]
            return {'results': [{'client_op_id': owned['request_uid'], 'status': self.verdict}]}
        def row_evidence(self, board, table, uid):
            return {'board': board, 'table': table, 'uid': uid, 'state': 'live', 'row': rows[table, uid]}
    return owned, receipt, rows, Hub()


def test_confirm_requires_replayed_canonical_receipt_and_independent_ended_run(live):
    owned, receipt, _rows, hub = fixture(live)
    result = transport.confirm(live, hub, 'default', owned['run_uid'])
    assert result['terminal_for_run'] is True and result['dependency_satisfied'] is False
    assert result['next_action'] == receipt['next_action'] and result['episode_uid'] == receipt['episode_uid']
    confirmed = transport.command(live, owned['run_uid'])
    assert confirmed['delivery_state'] == 'confirmed'
    assert json.loads(confirmed['canonical_receipt_json']) == receipt
    assert hub.calls == [[json.loads(owned['request_json'])]]


@pytest.mark.parametrize('damage', ['ack_only', 'changed_hash', 'live_run', 'wrong_outcome', 'same_current_run',
    'wrong_gateway', 'rewritten_row', 'wrong_board', 'missing_stop', 'replay_refused', 'changed_disposition'])
def test_confirmation_fails_closed_without_canonical_terminal_proof(live, damage):
    owned, receipt, rows, hub = fixture(live)
    event = rows['task_events', owned['event_uid']]
    changes = {
        'ack_only': (event, 'kind', 'diagnostic'),
        'changed_hash': (receipt, 'request_sha256', '0' * 64),
        'changed_disposition': (receipt, 'disposition', {'version': 1}),
        'live_run': (rows['task_runs', owned['run_uid']], 'ended_at', None),
        'wrong_outcome': (rows['task_runs', owned['run_uid']], 'outcome', 'completed'),
        'same_current_run': (rows['tasks', owned['task_id']], 'current_run_uid', owned['run_uid']),
        'wrong_gateway': (event, 'origin_gateway', 'different'),
        'rewritten_row': (event, 'version', 2),
        'missing_stop': (rows['task_events', receipt['episode_uid']], 'kind', 'diagnostic')}
    if damage in changes:
        target, key, value = changes[damage]
        target[key] = value
    if damage == 'replay_refused':
        hub.verdict = 'rejected'
    if damage == 'wrong_board':
        read = hub.row_evidence
        hub.row_evidence = lambda *args: {**read(*args), 'board': 'foreign'}
    event['payload'] = json.dumps(receipt)
    with pytest.raises(ValueError):
        transport.confirm(live, hub, 'default', owned['run_uid'])
    assert transport.command(live, owned['run_uid'])['canonical_receipt_json'] is None


def test_safety_supersession_reports_canonical_safety_not_requested_work(live):
    owned, receipt, rows, hub = fixture(live)
    receipt['superseded_by_safety_stop'] = True
    rows['task_events', owned['event_uid']]['payload'] = json.dumps(receipt)
    rows['task_runs', owned['run_uid']]['outcome'] = 'safety_refused'
    result = transport.confirm(live, hub, 'default', owned['run_uid'])
    assert result['disposition'] == 'superseded_by_safety_stop'
    assert result['terminal_for_run'] is True
