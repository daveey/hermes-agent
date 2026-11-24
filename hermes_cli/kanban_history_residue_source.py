"""Strict parsing of host-reported historical rejection bytes, never identity certification.

The returned source retains unverified extra columns. Nothing here admits, retires,
replays or modifies transport, events, holds or execution authority. [daveey]
"""
from __future__ import annotations

import json
import math

MAX_BYTES = 131072
MAX_DEPTH = 64
CONTAINMENT = 'containment_hold: use scoped resume-contained; generic writes cannot satisfy containment'
SPEND = 'spend_decision_pending: restricted episode has no execution release'


class Refused(ValueError):
    """No audit classification is established by these bytes."""


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise Refused(reason)


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate_key')
        result[key] = value
    return result


def _constant(_value: str):
    raise Refused('nonfinite_number')


def strict_object(raw: object, *, max_bytes: int = MAX_BYTES) -> dict:
    """Bounded UTF-8 JSON object; reject duplicates, nonfinite values and surrogates."""
    if not isinstance(raw, str):
        raise Refused('json_required')
    try:
        size = len(raw.encode('utf-8'))
    except UnicodeError as exc:
        raise Refused('invalid_unicode') from exc
    _require(size <= max_bytes, 'json_size')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except Refused:
        raise
    except (ValueError, RecursionError) as exc:
        raise Refused('invalid_json') from exc
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        _require(depth <= MAX_DEPTH, 'json_depth')
        if isinstance(item, str):
            try:
                item.encode('utf-8')
            except UnicodeError as exc:
                raise Refused('invalid_unicode') from exc
        elif isinstance(item, dict):
            stack.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif isinstance(item, list):
            stack.extend((part, depth + 1) for part in item)
        elif isinstance(item, float):
            _require(math.isfinite(item), 'nonfinite_number')
    _require(isinstance(value, dict), 'object_required')
    return value


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


def _positive(value: object) -> bool:
    return type(value) is int and 0 < value < 2**53


def decode_source(raw: str) -> dict:
    """Validate the narrow frozen event shape without repairing or normalizing it."""
    row = strict_object(raw)
    fields = strict_object(row.get('fields'))
    payload = strict_object(fields.get('payload'))
    wire = strict_object(row.get('wire_json'))
    _require(set(fields) == {'task_id', 'run_uid', 'kind', 'payload', 'created_at'}, 'source_fields')
    _require(_text(row.get('op_id')) and _text(row.get('row_uid')) and _text(row.get('hlc'))
             and row.get('table_name') == 'task_events' and row.get('op') == 'upsert'
             and row.get('state') == 'rejected' and _positive(row.get('attempts'))
             and _text(fields['task_id']) and fields['run_uid'] is None
             and _positive(fields['created_at']), 'source_shape')
    expected = {'client_op_id': row['op_id'], 'table': row['table_name'], 'uid': row['row_uid'],
                'op': row['op'], 'fields': fields, 'hlc': row['hlc'], 'frozen_replay': 1}
    # Python equality conflates True with 1. JSON byte equality after key ordering does not.
    _require(json.dumps(wire, sort_keys=True, ensure_ascii=True) ==
             json.dumps(expected, sort_keys=True, ensure_ascii=True), 'frozen_wire_mismatch')
    allowed = (row.get('last_error') == CONTAINMENT and fields['kind'] == 'blocked'
               and payload.get('metadata_only') is True and payload.get('classified_in_place') is True)
    allowed = allowed or (row.get('last_error') == SPEND and fields['kind'] == 'promoted_manual')
    _require(allowed, 'unsupported_rejection')
    return row


def _absence(value: object, *, board: str, event: str, head: int) -> None:
    if not isinstance(value, dict):
        raise Refused('absence_object')
    _require((value.get('board'), value.get('table'), value.get('uid')) == (board, 'task_events', event)
             and type(value.get('head')) is int and value['head'] == head
             and value.get('state') == 'never_existed' and type(value.get('floor')) is int
             and value['floor'] == 0 and 'row' in value and value['row'] is None
             and 'tombstone' in value and value['tombstone'] is None, 'absence_unproven')


def validate_current(raw: str, exact_source: str, *, gateway: str, board: str, head: int) -> dict:
    """Validate response structure only, NOT HTTP origin, local custody, freshness or admission.

    Callers must independently bind this result to a successful nonbusy refresh,
    an empty authenticated same-head feed cut, exact immutable custody and matching
    local/replica rows under read reservations. A saved result is not fresh proof.
    """
    row = decode_source(exact_source)
    fields = strict_object(row['fields'])
    # A source string is JSON-escaped again and two full parent snapshots are returned.
    # Keep a finite response budget separate from the unchanged 128 KiB source cap.
    data = strict_object(raw, max_bytes=2 * 1024 * 1024)
    _require(set(data) == {'version', 'gateway', 'receipt', 'current'} and type(data['version']) is int
             and data['version'] == 1 and data['gateway'] == gateway, 'proof_identity')
    receipt, current = data['receipt'], data['current']
    _require(isinstance(receipt, dict) and isinstance(current, dict), 'proof_objects')
    _require(set(receipt) == {'version', 'gateway', 'source_json', 'reason', 'disposition',
             'canonical_acceptance', 'historical_identity_certified', 'execution_authority',
             'reported_rejection_only', 'snapshot_only', 'observed_at_ms', 'observed_head', 'parent', 'absence'},
             'receipt_keys')
    _require(set(current) == {'observed_at_ms', 'observed_head', 'parent', 'absence', 'no_lease',
             'no_running_run', 'no_retained_op_receipt', 'op_history_certified', 'retained_receipt_floor1',
             'execution_authority'}, 'current_keys')
    _require(type(receipt.get('version')) is int and receipt['version'] == 1
             and receipt.get('gateway') == gateway and receipt.get('source_json') == exact_source
             and receipt.get('disposition') == 'retained_unreplayed'
             and _text(receipt.get('reason')), 'receipt_identity')
    for key, expected in {'canonical_acceptance': False, 'historical_identity_certified': False,
                          'execution_authority': False, 'reported_rejection_only': True, 'snapshot_only': True}.items():
        _require(receipt.get(key) is expected, 'receipt_authority')
    _require(_positive(receipt.get('observed_at_ms')) and type(receipt.get('observed_head')) is int
             and receipt['observed_head'] >= 0 and isinstance(receipt.get('parent'), dict)
             and receipt['parent'].get('uid') == fields['task_id']
             and receipt['parent'].get('created_on') == gateway, 'historical_snapshot')
    _absence(receipt.get('absence'), board=board, event=row['row_uid'], head=receipt['observed_head'])
    _require(type(head) is int and head >= 0 and type(current.get('observed_head')) is int
             and current['observed_head'] == head and _positive(current.get('observed_at_ms')),
             'current_cut')
    for key, expected in {'no_lease': True, 'no_running_run': True, 'no_retained_op_receipt': True,
                          'op_history_certified': False, 'execution_authority': False}.items():
        _require(current.get(key) is expected, 'current_authority')
    # This is raw retention metadata, not a certified never-seen-operation proof.
    _require('retained_receipt_floor1' in current and (current['retained_receipt_floor1'] is None
             or isinstance(current['retained_receipt_floor1'], str)), 'receipt_retention_metadata')
    parent = current.get('parent')
    if not isinstance(parent, dict):
        raise Refused('current_parent')
    _require(parent.get('uid') == fields['task_id']
             and parent.get('id') == fields['task_id'] and parent.get('created_on') == gateway
             and _positive(parent.get('version')) and parent.get('status') in ('triage', 'archived')
             and parent.get('delete_pending') == 0, 'current_parent')
    for key in ('current_run_uid', 'lease_holder', 'lease_run_uid', 'lease_expires', 'deleted_at'):
        _require(key in parent and parent[key] is None, 'current_parent_active')
    _absence(current.get('absence'), board=board, event=row['row_uid'], head=head)
    return current
