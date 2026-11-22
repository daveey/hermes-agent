"""Sender cursor reconciliation, approved post-collection design sections 3/4. [daveey]"""
from __future__ import annotations

import importlib.util
import json

import pytest


def api():
    assert importlib.util.find_spec('hermes_cli.kanban_replay_reconcile') is not None, 'sender reconciliation is missing'
    from hermes_cli import kanban_replay_reconcile
    return kanban_replay_reconcile


def slot(**changes):
    value = dict(board_incarnation='board-incarnation', gateway_principal='principal-one',
                 journal_id='journal-one', generation='1', session='1', mode='STRICT', h='0', a='0',
                 last_op_id=None, last_payload_sha256=None, last_verdict=None)
    value.update(changes)
    return value


def local(**changes):
    value = dict(board_incarnation='board-incarnation', gateway_principal='principal-one',
                 journal_id='journal-one', generation='1', settled='0',
                 last_op_id=None, last_payload_sha256=None, pending=None)
    value.update(changes)
    return value


def test_empty_matching_journal_is_idle_not_a_cursor_reset():
    assert api().reconcile(local(), slot()) == {'action': 'idle', 'ack': None}


def pending(**changes):
    value = dict(sequence='1', op_id='operation-one', payload_sha256='a' * 64)
    value.update(changes)
    return value


def test_unconsumed_pending_envelope_is_retried_without_allocating():
    state = local(pending=pending())
    assert api().reconcile(state, slot()) == {'action': 'retry', 'ack': None}
    assert state['pending'] == pending()


def test_lost_response_recovers_only_the_exact_held_verdict():
    import json
    result = {'client_op_id': 'operation-one', 'status': 'rejected', 'reason': 'not_found'}
    remote = slot(h='1', last_op_id='operation-one', last_payload_sha256='a' * 64,
                  last_verdict=json.dumps(result))
    assert api().reconcile(local(pending=pending()), remote) == {'action': 'record', 'verdict': result}


def settled_remote(**changes):
    return slot(h='1', a='0', last_op_id='operation-one', last_payload_sha256='a' * 64,
                last_verdict=json.dumps(dict(client_op_id='operation-one', status='applied')), **changes)


def settled_local(**changes):
    return local(settled='1', last_op_id='operation-one', last_payload_sha256='a' * 64, **changes)


@pytest.mark.parametrize('field', ['board_incarnation', 'gateway_principal', 'journal_id', 'generation'])
def test_different_stream_identity_is_not_rebound(field):
    remote = slot(**{field: '2' if field == 'generation' else 'different-identity'})
    with pytest.raises(ValueError, match=f'^stream binding mismatch: {field}$'):
        api().reconcile(local(), remote)


@pytest.mark.parametrize('mode', ['LEGACY', 'DRAINING', 'FAULT', None, 'unknown'])
def test_only_confirmed_strict_state_can_reconcile(mode):
    with pytest.raises(ValueError, match='^serving slot is not STRICT$'):
        api().reconcile(local(), slot(mode=mode))


def test_server_ahead_without_pending_is_not_acknowledged():
    with pytest.raises(ValueError, match='^missing issued envelope$'):
        api().reconcile(local(), settled_remote())


@pytest.mark.parametrize('field,value', [('last_op_id', 'operation-other'), ('last_payload_sha256', 'b' * 64)])
def test_lost_reply_with_different_identity_cannot_settle_our_pending(field, value):
    remote = settled_remote()
    remote[field] = value
    remote['last_verdict'] = json.dumps(dict(client_op_id=remote['last_op_id'], status='applied'))
    with pytest.raises(ValueError, match='^pending identity conflict$'):
        api().reconcile(local(pending=pending()), remote)


def test_server_cannot_regress_behind_a_locally_recorded_verdict():
    with pytest.raises(ValueError, match='^server cursor regressed$'):
        api().reconcile(settled_local(), slot())


def test_server_gap_does_not_fast_forward_allocator():
    remote = settled_remote()
    remote.update(h='2', a='1')
    with pytest.raises(ValueError, match='^server cursor gap$'):
        api().reconcile(local(pending=pending()), remote)


def test_local_recording_retains_ack_until_board_confirms_it():
    ack = dict(sequence='1', op_id='operation-one', payload_sha256='a' * 64)
    assert api().reconcile(settled_local(), settled_remote()) == {'action': 'idle', 'ack': ack}
    remote = settled_remote()
    remote['a'] = '1'
    assert api().reconcile(settled_local(), remote) == {'action': 'idle', 'ack': None}


def test_retry_of_successor_can_carry_the_retained_predecessor_ack():
    ack = dict(sequence='1', op_id='operation-one', payload_sha256='a' * 64)
    state = settled_local(pending=pending(sequence='2', op_id='operation-two', payload_sha256='b' * 64))
    assert api().reconcile(state, settled_remote()) == {'action': 'retry', 'ack': ack}


@pytest.mark.parametrize('field,value', [('last_op_id', 'operation-other'), ('last_payload_sha256', 'b' * 64)])
def test_equal_cursors_with_divergent_history_fault_even_when_board_acknowledged(field, value):
    remote = settled_remote()
    remote.update(a='1', **{field: value})
    remote['last_verdict'] = json.dumps(dict(client_op_id=remote['last_op_id'], status='applied'))
    with pytest.raises(ValueError, match='^settled identity conflict$'):
        api().reconcile(settled_local(), remote)


@pytest.mark.parametrize('key', ['h', 'a', 'generation', 'session'])
@pytest.mark.parametrize('value', [True, 1, '-1', '01', '1.0', '9223372036854775808', None])
def test_noncanonical_or_out_of_range_remote_counters_fault(key, value):
    with pytest.raises(ValueError, match=f'^invalid remote {key}$'):
        api().reconcile(local(), slot(**{key: value}))


@pytest.mark.parametrize('key', ['generation', 'session'])
def test_zero_generation_or_session_is_not_a_strict_session(key):
    with pytest.raises(ValueError, match=f'^invalid remote {key}$'):
        api().reconcile(local(), slot(**{key: '0'}))


@pytest.mark.parametrize('h,a', [('0', '1'), ('2', '0')])
def test_remote_one_outstanding_verdict_bound(h, a):
    with pytest.raises(ValueError, match='^invalid remote ack window$'):
        api().reconcile(local(), slot(h=h, a=a))


@pytest.mark.parametrize('value', ['0', '2', '01', 1, True, None])
def test_pending_must_be_exact_successor(value):
    with pytest.raises(ValueError, match='^invalid pending sequence$'):
        api().reconcile(local(pending=pending(sequence=value)), slot())


@pytest.mark.parametrize('body', ['broken-json', '[]', 'null',
    '{"client_op_id":"other-operation","status":"applied"}',
    '{"client_op_id":"operation-one","status":"retry"}',
    '{"client_op_id":"operation-one","status":"applied","row":{"uid":"t_foo"}}'])
def test_recovered_verdict_is_bounded_final_and_identity_bound(body):
    remote = settled_remote()
    remote['last_verdict'] = body
    with pytest.raises(ValueError, match='^invalid retained verdict$'):
        api().reconcile(local(pending=pending()), remote)


@pytest.mark.parametrize('status', ['applied', 'merged', 'rejected', 'cancelled', 'deferred'])
def test_every_final_transport_disposition_can_be_recorded_without_calling_it_applied(status):
    remote = settled_remote()
    verdict = dict(client_op_id='operation-one', status=status)
    remote['last_verdict'] = json.dumps(verdict)
    assert api().reconcile(local(pending=pending()), remote) == dict(action='record', verdict=verdict)


def test_readback_and_local_custody_remain_unchanged_on_decision_or_fault():
    import copy
    state, remote = local(pending=pending()), settled_remote()
    before = copy.deepcopy((state, remote))
    api().reconcile(state, remote)
    assert (state, remote) == before
    remote['last_payload_sha256'] = 'b' * 64
    before = copy.deepcopy((state, remote))
    with pytest.raises(ValueError):
        api().reconcile(state, remote)
    assert (state, remote) == before


@pytest.mark.parametrize('body', [
    '{"client_op_id":"operation-one","status":"applied","detail":NaN}',
    '{"client_op_id":"operation-one","status":"applied","detail":Infinity}',
    '{"client_op_id":"operation-one","status":"applied","detail":-Infinity}',
])
def test_non_json_constants_cannot_enter_a_recovered_verdict(body):
    remote = settled_remote()
    remote['last_verdict'] = body
    with pytest.raises(ValueError, match='^invalid retained verdict$'):
        api().reconcile(local(pending=pending()), remote)


def test_retained_verdict_bound_matches_receiver_utf16_character_units():
    remote = settled_remote()
    remote['last_verdict'] = json.dumps(dict(client_op_id='operation-one', status='applied',
                                           detail='😀' * 131072), ensure_ascii=False)
    with pytest.raises(ValueError, match='^invalid retained verdict$'):
        api().reconcile(local(pending=pending()), remote)


def test_matching_recoverable_last_verdict_needs_no_new_ack_if_another_copy_acked():
    remote = settled_remote()
    remote['a'] = '1'
    # Spec §3: server ahead by exactly one recovers the matching verdict, never
    # skips history. This decision does not acknowledge any unrecorded result.
    assert api().reconcile(local(pending=pending()), remote) == {
        'action': 'record', 'verdict': dict(client_op_id='operation-one', status='applied')}


@pytest.mark.parametrize('key,value', [('settled', '-1'), ('settled', True), ('settled', None),
    ('generation', '0'), ('generation', '01'), ('generation', None)])
def test_local_durable_counter_corruption_faults(key, value):
    with pytest.raises(ValueError, match=f'^invalid local {key}$'):
        api().reconcile(local(**{key: value}), slot())


@pytest.mark.parametrize('key,value', [('op_id', ''), ('op_id', 42), ('payload_sha256', 'A' * 64),
                                       ('payload_sha256', None)])
def test_invalid_pending_identity_faults(key, value):
    with pytest.raises(ValueError, match='^invalid pending identity$'):
        api().reconcile(local(pending=pending(**{key: value})), slot())


@pytest.mark.parametrize('key', ['last_op_id', 'last_payload_sha256'])
def test_invalid_local_settled_identity_faults(key):
    state = settled_local()
    state[key] = None
    with pytest.raises(ValueError, match='^invalid local identity$'):
        api().reconcile(state, settled_remote())


@pytest.mark.parametrize('key', ['last_op_id', 'last_payload_sha256'])
def test_invalid_remote_settled_identity_faults(key):
    remote = settled_remote()
    remote[key] = None
    with pytest.raises(ValueError, match='^invalid remote identity$'):
        api().reconcile(local(pending=pending()), remote)


@pytest.mark.parametrize('key,value', [('last_op_id', 'unexpected-op'), ('last_payload_sha256', 'a' * 64),
                                       ('last_verdict', '{}')])
def test_zero_prefix_has_no_retained_verdict(key, value):
    with pytest.raises(ValueError, match='^unexpected zero-prefix verdict$'):
        api().reconcile(local(), slot(**{key: value}))


@pytest.mark.parametrize('key', ['last_op_id', 'last_payload_sha256'])
def test_zero_local_prefix_has_no_retained_identity(key):
    with pytest.raises(ValueError, match='^unexpected zero-prefix local identity$'):
        api().reconcile(local(**{key: 'unexpected'}), slot())


@pytest.mark.parametrize('value', [False, [], 'pending-envelope'])
def test_non_mapping_pending_is_a_fault_not_idle(value):
    with pytest.raises(ValueError, match='^invalid pending envelope$'):
        api().reconcile(local(pending=value), slot())


@pytest.mark.parametrize('value', [None, '{}', []])
def test_non_mapping_snapshots_are_not_a_reset(value):
    with pytest.raises(ValueError, match='^invalid reconciliation snapshot$'):
        api().reconcile(value, slot())
    with pytest.raises(ValueError, match='^invalid reconciliation snapshot$'):
        api().reconcile(local(), value)


def test_largest_counter_reconciles_without_wraparound():
    remote = settled_remote()
    remote.update(h='9223372036854775807', a='9223372036854775807',
                  generation='9223372036854775807', session='9223372036854775807')
    state = settled_local()
    state.update(settled='9223372036854775807', generation='9223372036854775807')
    assert api().reconcile(state, remote) == dict(action='idle', ack=None)


def test_unreadable_verdict_shape_and_oversized_ascii_fault():
    for body in [None, {}, ' ' * 262145]:
        remote = settled_remote()
        remote['last_verdict'] = body
        with pytest.raises(ValueError, match='^invalid retained verdict$'):
            api().reconcile(local(pending=pending()), remote)


def test_successor_cannot_reuse_the_locally_settled_operation_id():
    state = settled_local(pending=pending(sequence='2', payload_sha256='b' * 64))
    with pytest.raises(ValueError, match='^pending reuses settled operation identity$'):
        api().reconcile(state, settled_remote())
