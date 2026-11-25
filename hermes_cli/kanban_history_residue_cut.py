"""Ephemeral same-head history audit cut; no cached exemptions or source writes.

Network happens without SQLite writer locks. Short final reservations then CAS
both snapshots, so claims cannot race the report's construction. Board feed cuts
are observations, not a distributed lock or past-identity certification. [daveey]
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import time

from hermes_cli import kanban_history_residue_custody as custody
from hermes_cli import kanban_history_residue_http as transport
from hermes_cli import kanban_history_residue_local as local
from hermes_cli.kanban_history_residue_source import _require, strict_object, validate_current

MAX_AGE = 30.0
MAX_OPS = 20


def _fresh(started):
    _require(0 <= time.monotonic() - started <= MAX_AGE, 'evidence_expired')


@contextmanager
def _transaction(conn, *, reserve=False):
    _require(not conn.in_transaction, 'nested_cut_transaction')
    old = conn.execute('PRAGMA busy_timeout').fetchone()[0]
    try:
        # Never stall an executor behind a diagnostic. No writes occur even when reserving.
        conn.execute('PRAGMA busy_timeout=0')
        conn.execute('BEGIN IMMEDIATE' if reserve else 'BEGIN')
        yield
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.execute(f'PRAGMA busy_timeout={old}')


def _empty(hub, board, head):
    data = hub.changes(board, head)
    _require(isinstance(data, dict) and data.get('changes') == [] and data.get('more') is False
             and type(data.get('head')) is int and type(data.get('next')) is int
             and data['head'] == head == data['next'], 'authoritative_feed_changed')
    return data


def _capture(live, shadow, raw, board, gateway):
    return [local.snapshot(conn, raw, board=board, gateway=gateway, replica=replica)
            for conn, replica in ((live, False), (shadow, True))]


def verify(live, shadow, ledger, hub, board, op_ids, *, started):
    """Internal cut assembly. Caller MUST own board lock after a nonbusy diagnostic refresh.

    This function returns evidence only. Ordinary sync.diff cannot consume a saved
    result as an exemption. A future diagnostic wrapper must build its report in
    the final reservations, not apply this evidence to a later diff.
    """
    _require(all(not c.in_transaction for c in (live, shadow, ledger)), 'nested_cut_transaction')
    _require(0 < len(op_ids) <= MAX_OPS and len(set(op_ids)) == len(op_ids), 'cut_ops_bound')
    identity = transport.scope(hub, board)
    from hermes_cli import kanban_sync_refusal_audit as refusal
    generations = [refusal._generation(c) for c in (live, shadow)]
    _fresh(started)
    records, snapshots = [], []
    with ExitStack() as stack:
        for conn in (live, shadow, ledger):
            stack.enter_context(_transaction(conn))
        for op_id in op_ids:
            saved = custody.lookup(ledger, identity, op_id)
            _require(saved is not None and saved['receipt_json'] is not None, 'sealed_custody_missing')
            request, source = custody._request(saved['request_json'])
            _require(source['op_id'] == op_id, 'custody_op_identity')
            snaps = _capture(live, shadow, request['source_json'], board, identity[2])
            _require(snaps[0]['portable'] == snaps[1]['portable'], 'portable_parent_changed')
            records.append((op_id, saved, request))
            snapshots.append(snaps)
    head = snapshots[0][1]['state'][0]['cursor']
    _require(type(head) is int and head >= 0
             and all(s[1]['state'][0]['cursor'] == head for s in snapshots), 'replica_cursor_changed')
    begin = _empty(hub, board, head)
    evidence = []
    for (op_id, saved, request), snaps in zip(records, snapshots):
        _fresh(started)
        raw = transport.current(hub, board, op_id)
        current = validate_current(raw, request['source_json'], gateway=identity[2], board=board, head=head)
        data = strict_object(raw, max_bytes=transport.MAX_RESPONSE)
        # Exact immutable receipt bytes are retained; compare the parsed response
        # structurally without Python bool/int equivalence.
        _require(json.dumps(data['receipt'], sort_keys=True) ==
                 json.dumps(strict_object(saved['receipt_json'], max_bytes=transport.MAX_RESPONSE), sort_keys=True),
                 'current_receipt_conflict')
        for key, value in snaps[1]['portable'].items():
            _require(key in current['parent'] and json.dumps(current['parent'][key], sort_keys=True) ==
                     json.dumps(value, sort_keys=True), 'canonical_parent_changed')
        evidence.append({'op_id': op_id, 'source_json': request['source_json'], 'proof_json': raw})
    # Re-read server verdicts inside this cut, never union a saved earlier report.
    server = None
    if any(r['op_id'] not in op_ids and isinstance(r.get('last_error'), str)
           and r['last_error'].partition(':')[0] in refusal.POLICY_REFUSALS for r in refusal._rows(live)):
        server = refusal.report(live, shadow, hub, board, started=started)
        _require(server['server_refusal_evidence']['head'] == head, 'replica_cursor_changed')
    end = _empty(hub, board, head)
    with ExitStack() as stack:
        for conn in (live, shadow, ledger):
            stack.enter_context(_transaction(conn, reserve=True))
        _fresh(started)
        for (op_id, saved, request), old in zip(records, snapshots):
            _require(custody.lookup(ledger, identity, op_id) == saved, 'custody_changed')
            _require(_capture(live, shadow, request['source_json'], board, identity[2]) == old,
                     'local_cut_inputs_changed')
        _require([refusal._generation(c) for c in (live, shadow)] == generations, 'local_cut_inputs_changed')
        _fresh(started)
        from hermes_cli.kanban_sync_client import diff
        report = diff(live, shadow, identity[2])
        if server is not None:
            report.update({key: server[key] for key in ('audited_server_refusals', 'server_refusal_evidence')})
        audits = set(report['audited_retain_rejections']) | set(op_ids) | set(report.get('audited_server_refusals', []))
        report['audited_history_rejections'] = list(op_ids)
        report['clean'] = not (report['missing_in_shadow'] or report['extra_in_shadow']
                              or report['field_mismatch'] or report['child_mismatch']
                              or report['rejected_ops'] != len(audits))
        _fresh(started)
        return {'head': head, 'audited_history_rejections': list(op_ids), 'evidence': evidence, 'diff': report,
                'begin': begin, 'end': end, 'snapshot_only': True, 'execution_authority': False,
                'historical_identity_certified': False}
