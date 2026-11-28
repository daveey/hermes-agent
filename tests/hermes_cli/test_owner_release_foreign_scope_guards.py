"""Observation custody, bounds and transactional race controls."""
import copy
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_release_scope as scope
from hermes_cli import kanban_release_certificate as cert
from tests.hermes_cli import test_owner_release_foreign_scope as helpers

board = helpers.board
proof = helpers.proof
assembly = helpers.assembly
foreign = helpers.foreign


def test_capture_forbids_network_under_writer(foreign):
    a = foreign
    with kb.write_txn(a.conn), pytest.raises(ValueError, match='no writer'):
        with scope.observe(a.conn, a.hub, a.proof):
            pytest.fail('writer-held capture admitted')


@pytest.mark.parametrize('change', ['missing', 'different', 'gateway'])
def test_private_observation_authority_is_not_inferred(foreign, monkeypatch, change):
    a = foreign
    if change == 'gateway':
        a.hub.ident.gateway = 'different-home'
    else:
        saved = copy.deepcopy(a.proof) if change == 'different' else None
        if saved is not None:
            saved['intent']['note'] = 'not the winning private receipt'
        monkeypatch.setattr(scope.replica, 'verified_consumption', lambda *args: saved)
    with pytest.raises(ValueError):
        with scope.observe(a.conn, a.hub, a.proof):
            pytest.fail('uncertified observation admitted')


@pytest.mark.parametrize('change', ['proof', 'connection', 'disappear', 'bytes', 'unbound'])
def test_observation_recheck_binds_owner_and_immutable_bytes(foreign, change):
    a = foreign
    with scope.observe(a.conn, a.hub, a.proof):
        proof = copy.deepcopy(a.proof)
        with kb.write_txn(a.conn):
            original = scope._OBSERVED.get()
            def change_bytes():
                p = json.loads(a.canonical['payload'])
                p['disposition']['summary'] = 'new bytes in writer'
                a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (cert.encode(p),))
            actions = {
                'proof': lambda: proof['intent'].update(note='different request'),
                'connection': lambda: scope._OBSERVED.set(scope._Observation(object(), original.proof, original.rows)),
                'disappear': lambda: a.conn.execute("DELETE FROM task_events WHERE uid='restriction-1'"),
                'bytes': change_bytes,
                'unbound': lambda: scope._OBSERVED.set(None),
            }
            actions[change]()
            with pytest.raises(ValueError):
                scope.evidence(a.conn, proof)
    assert scope._OBSERVED.get() is None


@pytest.mark.parametrize('damage', ['inventory', 'bytes', 'nontext'])
def test_inventory_and_actual_decoded_bytes_are_bounded(foreign, damage):
    a = foreign
    with kb.write_txn(a.conn):
        if damage == 'inventory':
            a.conn.executemany("INSERT INTO task_events(uid,task_id,kind,payload,created_at) "
                               "VALUES (?,?,'decision_retained','{}',90)",
                               [(f'bounded-{i}', a.tid) for i in range(4097)])
        else:
            value = ' ' * (cert.MAX_BYTES + 1) if damage == 'bytes' else None
            a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (value,))
    with pytest.raises(ValueError):
        helpers.process(a)
    assert not a.hub.posts


@pytest.mark.parametrize('field,value', [('version', 2), ('request_uid', []), ('request_sha256', None),
                                        ('request_sha256', 'a' * 63), ('run_uid', []), ('actor_gateway', [])])
def test_typed_local_receipt_validation(foreign, field, value):
    a = foreign
    p = json.loads(a.canonical['payload'])
    p[field] = value
    a.canonical['payload'] = cert.encode(p)
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (cert.encode(p),))
    with pytest.raises(ValueError):
        helpers.process(a)
    assert not a.hub.posts


def test_completed_application_replay_needs_no_mutable_observation(foreign):
    a = foreign
    first = helpers.process(a)
    def retired(*args, **kwargs):
        pytest.fail('completed replay performed mutable scope observation')
    a.hub.row_evidence = retired
    assert helpers.process(a) == first
    assert len(a.hub.posts) == 1
