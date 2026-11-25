"""Pure sender reconciliation decisions; no I/O, allocation or activation. [daveey]

The transport must obtain authenticated serving-Board state and the durable local
snapshot in its serialized sender boundary. A decision never authorizes pruning:
recording a recovered verdict and advancing local custody still needs one commit.
This module does not certify provenance, readiness, authentication or wire bytes.
"""
from __future__ import annotations

import json
import re

_MAX = (1 << 63) - 1
_FINAL = frozenset({'applied', 'merged', 'rejected', 'cancelled', 'deferred'})
_BINDING = ('board_incarnation', 'gateway_principal', 'journal_id', 'generation')
_MAX_VERDICT = 256 * 1024  # Same retained character bound as receiver replay_slot.ts.


def _counter(value: object, label: str, *, positive: bool = False) -> int:
    if not isinstance(value, str) or re.fullmatch(r'0|[1-9][0-9]{0,18}', value) is None:
        raise ValueError(f'invalid {label}')
    number = int(value)
    if number > _MAX or (positive and number == 0):
        raise ValueError(f'invalid {label}')
    return number


def _identity(op_id: object, digest: object, label: str) -> None:
    if not isinstance(op_id, str) or re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', op_id) is None:
        raise ValueError(f'invalid {label} identity')
    if not isinstance(digest, str) or re.fullmatch(r'[a-f0-9]{64}', digest) is None:
        raise ValueError(f'invalid {label} identity')


def _invalid_constant(value: str) -> None:
    raise ValueError(f'invalid JSON constant: {value}')


def _verdict(remote: dict, h: int) -> dict | None:
    op_id, digest, encoded = (remote.get(key) for key in ('last_op_id', 'last_payload_sha256', 'last_verdict'))
    if h == 0:
        if op_id is not None or digest is not None or encoded is not None:
            raise ValueError('unexpected zero-prefix verdict')
        return None
    _identity(op_id, digest, 'remote')
    if not isinstance(encoded, str) or len(encoded) > _MAX_VERDICT:
        raise ValueError('invalid retained verdict')
    try:
        # JavaScript strings count UTF-16 code units, not Unicode code points.
        if len(encoded.encode('utf-16-le', errors='surrogatepass')) // 2 > _MAX_VERDICT:
            raise ValueError('verdict exceeds receiver bound')
        verdict = json.loads(encoded, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError('invalid retained verdict') from exc
    if (not isinstance(verdict, dict) or verdict.get('client_op_id') != op_id or
            not isinstance(verdict.get('status'), str) or verdict['status'] not in _FINAL or
            verdict.get('row') is not None or verdict.get('source_predecessor') is not None):
        raise ValueError('invalid retained verdict')
    # Both inspected and consuming replies pass here before settlement binds SQLite.
    if 'version' in verdict:
        version = verdict['version']
        if type(version) is not int or not 0 <= version <= _MAX:
            raise ValueError('invalid retained verdict version')
    for key in ('reason', 'detail'):
        if verdict.get(key) is not None and not isinstance(verdict[key], str):
            raise ValueError(f'invalid retained verdict {key}')
    if verdict.get('deferred') is not None:
        reference = verdict['deferred']
        if (not isinstance(reference, dict) or set(reference) != {'task', 'origin'} or
                any(not isinstance(reference[key], str) or not reference[key] for key in ('task', 'origin'))):
            raise ValueError('invalid retained verdict deferred reference')
    return verdict


def _local_state(local: dict) -> tuple[int, dict | None]:
    settled = _counter(local.get('settled'), 'local settled')
    _counter(local.get('generation'), 'local generation', positive=True)
    if settled:
        _identity(local.get('last_op_id'), local.get('last_payload_sha256'), 'local')
    elif local.get('last_op_id') is not None or local.get('last_payload_sha256') is not None:
        raise ValueError('unexpected zero-prefix local identity')
    pending = local.get('pending')
    if pending is not None:
        if not isinstance(pending, dict):
            raise ValueError('invalid pending envelope')
        sequence = _counter(pending.get('sequence'), 'pending sequence', positive=True)
        if sequence != settled + 1:
            raise ValueError('invalid pending sequence')
        _identity(pending.get('op_id'), pending.get('payload_sha256'), 'pending')
        if settled and pending['op_id'] == local['last_op_id']:
            raise ValueError('pending reuses settled operation identity')
    return settled, pending


def _same(identity: dict, remote: dict, *, pending: bool) -> bool:
    prefix = '' if pending else 'last_'
    return (identity.get(f'{prefix}op_id') == remote.get('last_op_id') and
            identity.get(f'{prefix}payload_sha256') == remote.get('last_payload_sha256'))


def reconcile(local: dict, remote: dict) -> dict:
    """Choose idle, exact retry, or exact verdict recording; inconsistencies fault.

    ``settled`` is the local durably recorded verdict cursor, not next allocation.
    ``pending`` names the one issued successor retained with its immutable bytes.
    An ack returned here is only for a verdict already recorded locally. In
    particular, recovering an unknown reply never acknowledges it in this step.
    On ``retry``, the ack is a custody requirement, not permission to rewrite the
    pending bytes: the store must compare it with the frozen predecessor ack.
    """
    if not isinstance(local, dict) or not isinstance(remote, dict):
        raise ValueError('invalid reconciliation snapshot')
    if remote.get('mode') != 'STRICT':
        raise ValueError('serving slot is not STRICT')
    counters = {key: _counter(remote.get(key), f'remote {key}', positive=key in ('generation', 'session'))
                for key in ('h', 'a', 'generation', 'session')}
    h, a = counters['h'], counters['a']
    if a > h or h - a > 1:
        raise ValueError('invalid remote ack window')
    settled, pending = _local_state(local)
    for key in _BINDING:
        value = local.get(key)
        if not isinstance(value, str) or not value or value != remote.get(key):
            raise ValueError(f'stream binding mismatch: {key}')
    verdict = _verdict(remote, h)
    if h < settled:
        raise ValueError('server cursor regressed')
    if h > settled + 1:
        raise ValueError('server cursor gap')
    if h == settled + 1:
        if pending is None:
            raise ValueError('missing issued envelope')
        if not _same(pending, remote, pending=True):
            raise ValueError('pending identity conflict')
        return {'action': 'record', 'verdict': verdict}
    if settled and not _same(local, remote, pending=False):
        raise ValueError('settled identity conflict')
    ack = ({'sequence': local['settled'], 'op_id': local['last_op_id'],
            'payload_sha256': local['last_payload_sha256']} if a < settled else None)
    return {'action': 'retry' if pending is not None else 'idle', 'ack': ack}
