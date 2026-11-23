"""Signed fixture receipts prove consumer refusal, not real Board drainage."""
import base64
from copy import deepcopy

import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_migration as migration
from tests.hermes_cli import test_kanban_replay_sender as sender_tests

live = sender_tests.live
DOMAIN = b'hermes.sender_migration_evidence1\x00'


def fixtures(conn):
    state = journal.snapshot(conn)
    expected = dict(protocol='sender_migration_evidence1', authority='fixture-board-authority',
                    key_id='fixture-key', board='default',
                    board_incarnation=state['board_incarnation'], gateway_principal=state['gateway_principal'],
                    journal_id=state['journal_id'], migration_id='fixture-migration', challenge='fixture-nonce',
                    inventory=dict(revision=3, coverage='complete_current_and_former',
                                   principals=sorted([state['gateway_principal'], 'former-epoch'])))
    payload = dict(deepcopy(expected), issued_at_ms=1000, expires_at_ms=61000,
                   readback_position=12,
                   barriers=[dict(principal=p, barrier_id='barrier-' + p, position=i + 10)
                             for i, p in enumerate(expected['inventory']['principals'])])
    key = Ed25519PrivateKey.generate()
    return expected, payload, key


def signed(payload, key):
    signature = key.sign(DOMAIN + rfc8785.dumps(payload))
    return rfc8785.dumps(dict(payload=payload, signature=base64.b64encode(signature).decode('ascii')))


def send(conn, expected, payload, key, *, receipt=None, now_ms=2000):
    receiver = sender_tests.Receiver()
    encoded = signed(payload, key) if receipt is None else receipt
    result = migration.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1,
                            expectation=expected, receipt=encoded, now_ms=now_ms,
                            public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    return receiver, result


@pytest.mark.parametrize('defect', [
    'missing', 'partial', 'unauthenticated', 'expired', 'future', 'challenge',
    'inventory_revision', 'inventory_principals', 'board', 'journal', 'authority',
    'readback_before_barrier', 'duplicate_barrier', 'duplicate_principal', 'unknown_field',
    'key_id', 'gateway_principal', 'board_incarnation', 'migration_id', 'protocol',
    'outside_principal', 'boolean_position', 'long_lifetime', 'at_expiry',
])
def test_invalid_receipts_refuse_before_any_peer_io(live, defect):
    kb.create_task(live, title='migration custody', assignee='worker')
    expected, payload, key = fixtures(live)
    mutations = {
        'partial': lambda: payload['barriers'].pop(),
        'expired': lambda: payload.update(expires_at_ms=1999),
        'future': lambda: payload.update(issued_at_ms=2001),
        'challenge': lambda: payload.update(challenge='wrong'),
        'board': lambda: payload.update(board='wrong'),
        'authority': lambda: payload.update(authority='wrong'),
        'journal': lambda: payload.update(journal_id='wrong'),
        'inventory_revision': lambda: payload['inventory'].update(revision=4),
        'inventory_principals': lambda: payload['inventory']['principals'].pop(),
        'readback_before_barrier': lambda: payload.update(readback_position=9),
        'duplicate_barrier': lambda: payload['barriers'].append(payload['barriers'][0]),
        'duplicate_principal': lambda: payload['inventory']['principals'].append('former-epoch'),
        'unknown_field': lambda: payload.update(authenticated=True),
        'key_id': lambda: payload.update(key_id='foreign-key'),
        'gateway_principal': lambda: payload.update(gateway_principal='foreign-principal'),
        'board_incarnation': lambda: payload.update(board_incarnation='foreign-board'),
        'migration_id': lambda: payload.update(migration_id='foreign-migration'),
        'protocol': lambda: payload.update(protocol='foreign-protocol'),
        'outside_principal': lambda: payload['barriers'][0].update(principal='unlisted'),
        'boolean_position': lambda: payload['barriers'][0].update(position=True),
        'long_lifetime': lambda: payload.update(expires_at_ms=61001),
        'at_expiry': lambda: payload.update(expires_at_ms=2000),
    }
    mutations.get(defect, lambda: None)()
    encoded = None if defect == 'missing' else signed(payload, key)
    if defect == 'unauthenticated':
        encoded = signed(payload, Ed25519PrivateKey.generate())
    receiver = sender_tests.Receiver()
    before = list(live.iterdump())
    with pytest.raises(ValueError, match='migration'):
        migration.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1,
                       expectation=expected, receipt=encoded, now_ms=2000,
                       public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    assert receiver.calls == []
    assert list(live.iterdump()) == before


def test_complete_signed_fixture_reaches_existing_sender(live):
    kb.create_task(live, title='complete fixture', assignee='worker')
    expected, payload, key = fixtures(live)
    receiver, result = send(live, expected, payload, key)
    assert result.applied == 1
    assert receiver.calls == ['capability', 'inspect', 'session', 'ops', 'inspect', 'ack']
    assert journal.snapshot(live)['settled'] == '1'


@pytest.mark.parametrize('field', ['board', 'board_incarnation', 'gateway_principal', 'journal_id'])
def test_local_binding_is_independent_of_signed_expectation(live, field):
    expected, payload, key = fixtures(live)
    expected[field] = payload[field] = 'foreign-binding'
    # Keep the foreign receipt internally complete and authentically signed: only
    # the independently read local custody / board argument can reject it.
    if field == 'gateway_principal':
        expected['inventory']['principals'] = payload['inventory']['principals'] = sorted(
            [journal.snapshot(live)['gateway_principal'], 'foreign-binding', 'former-epoch'])
        payload['barriers'] = [dict(principal=p, barrier_id='barrier-' + p, position=10)
                               for p in expected['inventory']['principals']]
    receipt = signed(payload, key)
    public_key = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert migration.verify(receipt, expectation=expected, public_key=public_key, now_ms=2000) == payload
    receiver = sender_tests.Receiver()
    before = list(live.iterdump())
    with pytest.raises(ValueError, match='migration local binding mismatch'):
        migration.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1,
                       expectation=expected, receipt=receipt, now_ms=2000, public_key=public_key)
    assert receiver.calls == []
    assert list(live.iterdump()) == before


def test_empty_or_duplicate_expected_inventory_never_certifies_completeness(live):
    expected, payload, key = fixtures(live)
    expected['inventory']['principals'] = payload['inventory']['principals'] = []
    payload['barriers'] = []
    with pytest.raises(ValueError, match='migration'):
        send(live, expected, payload, key)


def test_boolean_revision_cannot_equal_integer_inventory(live):
    expected, payload, key = fixtures(live)
    expected['inventory']['revision'] = 1
    payload['inventory']['revision'] = True
    with pytest.raises(ValueError, match='migration'):
        send(live, expected, payload, key)


@pytest.mark.parametrize('defect', ['current_principal_missing', 'noncanonical', 'oversized'])
def test_independent_context_and_envelope_boundaries(live, defect):
    expected, payload, key = fixtures(live)
    receipt = signed(payload, key)
    if defect == 'current_principal_missing':
        expected['inventory']['principals'] = ['former-epoch']
        payload['inventory']['principals'] = ['former-epoch']
        payload['barriers'] = [dict(principal='former-epoch', barrier_id='former', position=10)]
        receipt = signed(payload, key)
    if defect == 'noncanonical':
        receipt += b' '
    if defect == 'oversized':
        receipt += b' ' * 262144
    receiver = sender_tests.Receiver()
    with pytest.raises(ValueError, match='migration'):
        migration.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1,
                       expectation=expected, receipt=receipt, now_ms=2000,
                       public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    assert receiver.calls == []
