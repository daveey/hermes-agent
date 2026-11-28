"""Invocation-local authenticated foreign scope observations, never clearance.

A consumed certificate attests UIDs, not receipt bytes. Read canonical bytes
before the proposal writer, then compare them during both real assembly checks.
Do not invent another executor's private retain-command custody on HOME.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass

from hermes_cli import kanban_release_certificate as cert, kanban_release_replica as replica
from hermes_cli import kanban_scope_renewal as renewal

_OBSERVED = contextvars.ContextVar('owner_release_foreign_scope', default=None)
_FIELDS = ('uid', 'task_id', 'kind', 'payload', 'created_at', 'origin_gateway', 'run_uid')
_REQUIRED = {'version', 'task_uid', 'run_uid', 'request_uid', 'event_uid', 'episode_uid',
             'request_sha256', 'operation_kind', 'actor_gateway', 'superseded_by_safety_stop', 'disposition'}


def _rows(conn, proof):
    body = proof['intent']
    rows = conn.execute("SELECT e.*,r.uid AS run_uid,r.task_id AS run_task,r.ended_at AS run_end,"
                        "r.outcome AS run_outcome FROM task_events e LEFT JOIN task_runs r ON r.id=e.run_id "
                        "WHERE e.task_id=? AND e.kind='decision_retained' ORDER BY e.uid LIMIT 4097",
                        (body['task_id'],)).fetchall()
    if len(rows) > 4096:
        raise ValueError('HOME foreign scope inventory exceeds bound')
    result, size = {}, 0
    for row in rows:
        event = dict(row)
        raw = event['payload']
        size += len(raw.encode('utf-8')) if isinstance(raw, str) else cert.MAX_BYTES + 1
        if size > cert.MAX_BYTES:
            raise ValueError('HOME foreign scope bytes exceed bound')
        p = cert.parse(raw)
        if p.get('superseded_by_safety_stop') is not False:
            raise ValueError('HOME retained safety or unknown receipt remains')
        disposition = p.get('disposition')
        if not isinstance(disposition, dict):
            raise ValueError('HOME retained disposition is malformed')
        if p.get('episode_uid') != body['episode'] or disposition.get('reason') != 'scope_stopped':
            continue
        uid = event['uid']
        if (set(p) not in (_REQUIRED, _REQUIRED | {'next_action'})
                or type(p['version']) is not int or p['version'] != 1 or p['operation_kind'] != 'retain'
                or p['event_uid'] != uid or p['task_uid'] != body['task_id']
                or not cert.intent._coordinate(uid) or not cert.intent._coordinate(p['request_uid'])
                or not isinstance(p['request_sha256'], str) or len(p['request_sha256']) != 64
                or any(c not in '0123456789abcdef' for c in p['request_sha256'])
                or p['actor_gateway'] != event['origin_gateway']
                or not cert.intent._coordinate(event['origin_gateway'])
                or p['run_uid'] != event['run_uid'] or not cert.intent._coordinate(event['run_uid'])
                or event['run_task'] != body['task_id'] or event['run_end'] is None
                or event['run_outcome'] != 'retained' or uid in result):
            raise ValueError('HOME retained scope identity differs')
        result[uid] = {k: event[k] for k in _FIELDS}
    return result


@dataclass(frozen=True, slots=True)
class _Observation:
    conn: object
    proof: str
    rows: str


@contextmanager
def observe(conn, hub, proof):
    """Only current authenticated canonical bytes; no I/O while a writer is held."""
    if conn.in_transaction:
        raise ValueError('HOME foreign scope capture requires no writer')
    saved = replica.verified_consumption(conn, proof['consume']['client_op_id'])
    if saved is None or cert.encode(saved) != cert.encode(proof):
        raise ValueError('HOME foreign scope lacks exact private consumption')
    if hub.ident.gateway != proof['renewal']['origin_gateway']:
        raise ValueError('HOME foreign scope observer identity differs')
    local = _rows(conn, proof)
    ordinary = {renewal.key(e) for e in renewal.scope_evidence(conn, proof['intent']['task_id'], proof['intent']['episode'])}
    captured = {}
    for uid, image in local.items():
        if uid in ordinary:
            continue  # Existing locally confirmed custody retains its own verifier.
        if uid not in cert.coverage(proof) or image['origin_gateway'] == hub.ident.gateway:
            raise ValueError('HOME foreign scope is uncovered or lacks local custody')
        ev = hub.row_evidence(proof['intent']['board'], 'task_events', uid, max_response_bytes=cert.MAX_BYTES)
        if (not isinstance(ev, dict) or ev.get('board') != proof['intent']['board']
                or ev.get('table') != 'task_events' or ev.get('uid') != uid or ev.get('state') != 'live'
                or not isinstance(ev.get('row'), dict) or ev['row'].get('deleted_at') is not None
                or type(ev['row'].get('version')) is not int or ev['row']['version'] != 1
                or cert.encode({k: ev['row'].get(k) for k in _FIELDS}) != cert.encode(image)):
            raise ValueError('HOME foreign scope differs from authenticated canonical receipt')
        captured[uid] = image
    token = _OBSERVED.set(_Observation(conn, cert.encode(proof), cert.encode(captured)))
    try:
        yield
    finally:
        _OBSERVED.reset(token)


def evidence(conn, proof):
    """SQL-only recheck inside the original unique application writer."""
    ordinary = renewal.scope_evidence(conn, proof['intent']['task_id'], proof['intent']['episode'])
    keys = {renewal.key(e) for e in ordinary}
    local = _rows(conn, proof)
    extra = {uid: image for uid, image in local.items() if uid not in keys}
    observed = _OBSERVED.get()
    if extra:
        if (type(observed) is not _Observation or observed.conn is not conn
                or observed.proof != cert.encode(proof) or observed.rows != cert.encode(extra)):
            raise ValueError('HOME foreign scope observation missing or changed')
        keys.update(extra)
    elif type(observed) is _Observation and observed.rows != cert.encode({}):
        raise ValueError('HOME foreign scope receipt disappeared')
    if not keys or frozenset(keys) != cert.coverage(proof):
        raise ValueError('HOME current scope evidence differs from winning consumption')
    return frozenset(keys)
