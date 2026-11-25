"""Fresh read-only server-refusal classification; no cached exemptions. [daveey]

A retained op-ID/gateway verdict proves refused transport intent, not historical
payload/enrollment identity or execution authority. Row convergence is still
checked independently by the ordinary diff on the final local cut.
"""
from __future__ import annotations

from contextlib import ExitStack
import time
import urllib.parse

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_history_residue_cut as cut
from hermes_cli import kanban_history_residue_http as transport
from hermes_cli.kanban_history_residue_source import _require, strict_object

POLICY_REFUSALS = frozenset({'containment_hold', 'spend_decision_pending', 'missing_decision_episode'})
MAX_OPS = 20


def _generation(conn):
    return conn.execute('PRAGMA data_version').fetchone()[0], conn.total_changes


def _rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE state='rejected' ORDER BY op_id LIMIT ?", (MAX_OPS + 1,))]


def _receipt(hub, board, op_id):
    path = '/v2/boards/' + urllib.parse.quote(board, safe='') + '/operation-receipt?'
    raw = transport._request(hub, 'GET', path + urllib.parse.urlencode({'op_id': op_id}), None)
    return strict_object(raw.decode('utf-8', 'strict'), max_bytes=transport.MAX_RESPONSE)


def _proven(data, board, gateway, row):
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or data.get('board') != board or data.get('gateway') != gateway
            or data.get('execution_authority') is not False
            or data.get('historical_identity_certified') is not False):
        return False
    receipt = data.get('receipt')
    if not isinstance(receipt, dict):
        return False
    reason = receipt.get('reason')
    detail = receipt.get('detail')
    if not isinstance(reason, str) or reason not in POLICY_REFUSALS or not (detail is None or isinstance(detail, str)):
        return False
    error = f'{reason}: {detail or ""}'.strip(': ')
    return (receipt.get('client_op_id') == row['op_id'] and receipt.get('status') == 'rejected'
            and error == row.get('last_error') and row.get('state') == 'rejected')


def report(live, shadow, hub, board, *, started=None):
    """Caller owns normal board sync lock after successful diagnostic refresh.

    No SQLite reservation spans HTTP. Generation CAS covers every live/replica
    change, not just rejected rows. Final zero-wait reservations build the actual
    ordinary diff; a saved result is never an exemption in a later report.
    """
    from hermes_cli.kanban_sync_client import diff

    started = time.monotonic() if started is None else started
    identity = transport.scope(hub, board)
    generations = [_generation(c) for c in (live, shadow)]
    rows = _rows(live)
    _require(len(rows) <= MAX_OPS, 'refusal_inventory_bound')
    state = sync.sync_state(shadow) or {}
    _require(state.get('board_id') == board and state.get('gateway_id') == identity[2], 'replica_identity_changed')
    head = state.get('cursor')
    _require(type(head) is int and head >= 0, 'replica_cursor_changed')
    _require([_generation(c) for c in (live, shadow)] == generations, 'local_cut_inputs_changed')
    cut._fresh(started)
    begin = cut._empty(hub, board, head)
    proofs = []
    for row in rows:
        error = row.get('last_error')
        if not isinstance(error, str) or error.partition(':')[0] not in POLICY_REFUSALS:
            continue  # Unknown/local/transport failures remain counted, never inferred settled.
        cut._fresh(started)
        proof = _receipt(hub, board, row['op_id'])
        if _proven(proof, board, identity[2], row):
            proofs.append({'op_id': row['op_id'], 'binding': 'op_id_gateway_verdict_only', 'proof': proof})
    end = cut._empty(hub, board, head)
    with ExitStack() as stack:
        for conn in (live, shadow):
            stack.enter_context(cut._transaction(conn, reserve=True))
        cut._fresh(started)
        _require([_generation(c) for c in (live, shadow)] == generations and _rows(live) == rows,
                 'local_cut_inputs_changed')
        result = diff(live, shadow, identity[2])
        result['audited_server_refusals'] = [p['op_id'] for p in proofs]
        audits = set(result['audited_server_refusals']) | set(result['audited_retain_rejections'])
        result['clean'] = not (result['missing_in_shadow'] or result['extra_in_shadow']
                               or result['field_mismatch'] or result['child_mismatch']
                               or result['rejected_ops'] != len(audits))
        result['server_refusal_evidence'] = {'head': head, 'begin': begin, 'end': end, 'rows': proofs,
                                            'execution_authority': False, 'historical_identity_certified': False}
        cut._fresh(started)
        return result
