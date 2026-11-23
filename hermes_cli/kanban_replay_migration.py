"""Dormant, offline-verified migration gate; no live installer or caller. [daveey]

The receiver owns authoritative inventory/barriers. Integration must provision the
Board-scoped pinned public key and independent expectation; receipt keys/booleans
are never authentication. Fixture acceptance is not real drainage readiness.
"""
import base64
import binascii
import json

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_DOMAIN = b'hermes.sender_migration_evidence1\x00'
_EXPECTED = frozenset(('protocol', 'authority', 'key_id', 'board', 'board_incarnation',
                       'gateway_principal', 'journal_id', 'migration_id', 'challenge', 'inventory'))
_DYNAMIC = frozenset(('issued_at_ms', 'expires_at_ms', 'readback_position', 'barriers'))


def _number(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def _inventory(expected):
    if not isinstance(expected, dict) or set(expected) != _EXPECTED:
        raise ValueError('migration expectation incomplete')
    if expected['protocol'] != 'sender_migration_evidence1' or any(
            not isinstance(expected[k], str) or not expected[k] for k in _EXPECTED - {'inventory'}):
        raise ValueError('migration identity incomplete')
    inventory = expected['inventory']
    if not isinstance(inventory, dict) or set(inventory) != {'revision', 'coverage', 'principals'}:
        raise ValueError('migration inventory incomplete')
    principals = inventory['principals']
    if (inventory['coverage'] != 'complete_current_and_former' or
            not _number(inventory['revision']) or inventory['revision'] == 0 or
            not isinstance(principals, list) or not principals or
            any(not isinstance(p, str) or not p for p in principals) or
            principals != sorted(set(principals))):
        raise ValueError('migration inventory is not complete canonical principal set')
    return principals


def verify(receipt, *, expectation, public_key, now_ms):
    """Verify canonical signed readback against independently pinned context.

    No network or activation. The public key is trusted configuration scoped to
    expectation's authority/Board incarnation, never taken from the envelope.
    """
    principals = _inventory(expectation)
    if not isinstance(receipt, bytes) or len(receipt) > 262144:
        raise ValueError('migration receipt missing or oversized')
    try:
        envelope = json.loads(receipt)
        if (not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'} or
                rfc8785.dumps(envelope) != receipt):
            raise ValueError('migration receipt is not canonical')
        payload = envelope['payload']
        signature = base64.b64decode(envelope['signature'], validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, _DOMAIN + rfc8785.dumps(payload))
    except (InvalidSignature, ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise ValueError('migration receipt unauthenticated or malformed') from exc
    if (not isinstance(payload, dict) or set(payload) != _EXPECTED | _DYNAMIC or
            rfc8785.dumps({k: payload.get(k) for k in _EXPECTED}) != rfc8785.dumps(expectation)):
        raise ValueError('migration receipt identity or inventory mismatch')
    issued, expires = payload['issued_at_ms'], payload['expires_at_ms']
    if (not all(_number(n) for n in (now_ms, issued, expires)) or
            not issued <= now_ms < expires or expires - issued > 60000):
        raise ValueError('migration receipt stale')
    barriers = payload['barriers']
    position = payload['readback_position']
    if not isinstance(barriers, list) or not _number(position):
        raise ValueError('migration barrier readback malformed')
    covered = []
    for barrier in barriers:
        if (not isinstance(barrier, dict) or set(barrier) != {'principal', 'barrier_id', 'position'} or
                not isinstance(barrier['barrier_id'], str) or not barrier['barrier_id'] or
                not _number(barrier['position']) or not 0 < barrier['position'] <= position):
            raise ValueError('migration barrier not established before readback')
        covered.append(barrier['principal'])
    if covered != principals:
        raise ValueError('migration barrier coverage incomplete')
    return payload


def push(conn, hub, board, gateway, *, expectation, public_key, receipt, now_ms,
         primary, max_batches=25, before_head=False, seen=None):
    """Explicit dormant entry: verify before even the first capability/inspect I/O.

    Normal push is intentionally unchanged. There is no production migration
    caller, trust-root installer, certification or activation in this module.
    """
    from hermes_cli import kanban_replay_admission as admission
    from hermes_cli import kanban_replay_journal as journal
    from hermes_cli import kanban_replay_sender as sender
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_sync_guard import sender_lock
    path = _main_db_file(conn)
    if path is None:
        raise ValueError('migration sender requires on-disk custody')
    with sender_lock(path):
        admission.guard(conn)
        local = journal.snapshot(conn)  # A missing anchor cannot select legacy here.
        verify(receipt, expectation=expectation, public_key=public_key, now_ms=now_ms)
        if (expectation['board'] != board or
                local['gateway_principal'] not in expectation['inventory']['principals'] or
                any(expectation[k] != local[k] for k in ('board_incarnation', 'gateway_principal', 'journal_id'))):
            raise ValueError('migration local binding mismatch')
        return sender._push(conn, hub, board, gateway, primary=primary, max_batches=max_batches,
                            before_head=before_head, seen=seen)
