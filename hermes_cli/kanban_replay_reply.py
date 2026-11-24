"""Authenticated consuming replies avoid a redundant inspect on new sends.

The caller already owns the serialized sender session. This adapter is only for
an exact newly consumed H+1 reply, not historical coverage, duplicates, recovery,
or current-row evidence. Every other shape requires authoritative inspection.
"""
import json

from hermes_cli.kanban_replay_reconcile import reconcile


_TRANSPORT = frozenset({'consumed', 'sequence', 'payload_sha256', 'h', 'dup',
                        'row', 'source_predecessor'})


def mapping_envelope(payload, label):
    """Refuse a non-object Board payload as a typed board-local fault."""
    if not isinstance(payload, dict):
        raise ValueError(f'replay {label} envelope malformed')
    return payload


def capability_envelope(payload):
    """Refuse a non-object capability as a board-local stream fault."""
    if not isinstance(payload, dict):
        raise ValueError('replay capability unavailable')
    return payload


def slot_envelope(payload):
    """Refuse a missing or non-object serving slot as a board-local stream fault."""
    mapping_envelope(payload, 'serving')
    slot = payload.get('slot')
    if not isinstance(slot, dict):
        raise ValueError('replay serving envelope malformed')
    return slot


def row_evidence_envelope(payload):
    """Refuse a non-object current-row evidence payload before dereference."""
    return mapping_envelope(payload, 'current-row')


def consumed_slot(reply, wire, previous, local):
    """Return a validated final snapshot, or require inspect without settlement.

    Board settleReplay returns its slim retained verdict plus consumption fields.
    A successful HTTP status alone, empty results or an unbound final-looking
    status never authorize source pruning. Readback is still required for CAS,
    uncertain replies and standalone ACK controls.
    """
    if (not isinstance(reply, dict) or reply.get('protocol') != 'processed_prefix1'
            or not isinstance(reply.get('results'), list) or len(reply['results']) != 1):
        return None
    result = reply['results'][0]
    replay = wire['replay']
    if (not isinstance(result, dict) or result.get('consumed') is not True
            or result.get('outcome_unknown') or result.get('dup')
            or result.get('client_op_id') != wire['client_op_id']
            or result.get('sequence') != replay['sequence'] or result.get('h') != replay['sequence']
            or result.get('payload_sha256') != replay['payload_sha256']
            or int(replay['sequence']) != int(previous['h']) + 1):
        return None
    verdict = {key: value for key, value in result.items() if key not in _TRANSPORT}
    try:
        candidate = dict(previous, a=previous['h'], h=replay['sequence'],
                         last_op_id=wire['client_op_id'], last_payload_sha256=replay['payload_sha256'],
                         last_verdict=json.dumps(verdict, ensure_ascii=False, allow_nan=False))
        if reconcile(local, candidate)['action'] != 'record':
            return None
    except (ValueError, TypeError, RecursionError):
        return None
    return candidate


def same_slot(left, right):
    """Compare exact counters/bindings and JSON-equivalent retained verdicts.

    An ops reply carries the verdict object, not the Board's JSON.stringify bytes.
    Only this representation difference is allowed in standalone ACK readback;
    canonical JSON keeps booleans distinct from numbers and rejects nonfinite data.
    """
    import rfc8785
    if not isinstance(right, dict) or left.keys() != right.keys():
        return False
    if {k: v for k, v in left.items() if k != 'last_verdict'} != {
            k: v for k, v in right.items() if k != 'last_verdict'}:
        return False
    if left['last_verdict'] == right['last_verdict']:
        return True
    try:
        return rfc8785.dumps(json.loads(left['last_verdict'])) == rfc8785.dumps(json.loads(right['last_verdict']))
    except (ValueError, TypeError, RecursionError):
        return False
