"""Opt-in bounded owner reassessment; never evidence of producer health.

The schedule/continue lifecycle creates a pending journal record here, but no
scanner path reads it as an exemption until canonical proof is installed.
Shape validation alone grants no authority; the record has execution_authority false.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
import sqlite3
import time

_UID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
_REQUEST = {'version', 'reassess_at', 'target', 'evidence_refs'}


class ReassessmentRefused(ValueError):
    """No effective deferral may be recorded for this request."""


def _keys(value, required, optional=()):
    return isinstance(value, dict) and required <= value.keys() <= required | set(optional)


def _text(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _uid(value):
    return isinstance(value, str) and _UID.fullmatch(value) is not None


def request(value):
    """Validate opt-in input without accepting caller-supplied authority fields."""
    if not _keys(value, _REQUEST) or type(value['version']) is not int or value['version'] != 1:
        raise ReassessmentRefused('invalid owner reassessment request')
    target, refs = value['target'], value['evidence_refs']
    if (type(value['reassess_at']) is not int or value['reassess_at'] <= 0
            or not _keys(target, {'kind', 'identity', 'revision'})
            or target['kind'] not in ('queue', 'update', 'external')
            or not _text(target['identity'], 512) or not _text(target['revision'], 256)
            or not isinstance(refs, list) or not 1 <= len(refs) <= 16
            or not all(_keys(r, {'table', 'uid'}) and r['table'] == 'task_events' and _uid(r['uid']) for r in refs)
            or len({r['uid'] for r in refs}) != len(refs)):
        raise ReassessmentRefused('invalid target, deadline or portable evidence')
    return deepcopy(value)


def issuer(captured, conn, task_id):
    """Revalidate actual private owner custody in the caller's park transaction.

    Environment pins, dictionaries and injected projection objects are not issuers.
    The portable event references must already exist; legacy prose is not upgraded.
    """
    from agent.origin_worker_custody import CapturedWorkerOrigin

    if not conn.in_transaction or type(captured) is not CapturedWorkerOrigin:
        raise ReassessmentRefused('reassessment requires private admitted owner custody')
    # The capability rechecks both task/run claims and the actual facade turn lease.
    if captured.validated(conn) != {'creator': task_id}:
        raise ReassessmentRefused('owner custody is no longer current')
    capability = captured._capability
    run = conn.execute('SELECT uid FROM task_runs WHERE id=? AND task_id=?',
                       (capability.run_id, task_id)).fetchone()
    events = {}
    for kind in ('owner_admitted', 'worker_bound'):
        row = conn.execute('SELECT uid,payload FROM task_events WHERE task_id=? AND run_id=? '
                           'AND kind=? ORDER BY id DESC LIMIT 1', (task_id, capability.run_id, kind)).fetchone()
        if row is None or not _uid(row[0]):
            raise ReassessmentRefused('portable owner admission evidence is missing')
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError) as exc:
            raise ReassessmentRefused('owner admission evidence is malformed') from exc
        if not isinstance(payload, dict):
            raise ReassessmentRefused('owner admission evidence is malformed')
        events[kind] = (row[0], payload)
    if (run is None or not _uid(run[0])
            or events['owner_admitted'][1].get('session_id') != capability.session_id
            or events['worker_bound'][1].get('pid') != capability.pid
            or events['worker_bound'][1].get('claim_lock') != capability.claim_lock
            or captured.validated(conn) != {'creator': task_id}):
        raise ReassessmentRefused('owner admission identity changed')
    return {'task_id': task_id, 'session_id': capability.session_id, 'profile': capability.profile, 'run_uid': run[0],
            'owner_admitted_uid': events['owner_admitted'][0], 'worker_bound_uid': events['worker_bound'][0]}


def plan(value, *, now, final_backstop, awaiting, payload):
    """Shorten, never extend, the admitted wake; preserve the original backstop.

    Called only after ordinary awaiting/deploy-window/probe gates. This pure plan
    neither authenticates an owner nor writes a receipt. Expiry uses the existing
    dispatcher deadline, rather than an additional polling service.
    """
    value = request(value)
    if (type(now) is not int or type(final_backstop) is not int
            or not now < value['reassess_at'] <= final_backstop):
        raise ReassessmentRefused('reassessment must precede the admitted backstop')
    if (not _keys(awaiting, {'kind', 'ref'}, ('gateway', 'board', 'why'))
            or awaiting['kind'] not in ('card', 'ci', 'deploy', 'external')
            or not _text(awaiting['ref'], 512)
            or ('gateway' in awaiting and not _text(awaiting['gateway'], 200))
            or ('board' in awaiting and not _text(awaiting['board'], 200))
            or ('why' in awaiting and not _text(awaiting['why'], 2000))):
        raise ReassessmentRefused('an exact structured producer is required')
    if not isinstance(payload, dict) or payload.get('wake_at') != final_backstop:
        raise ReassessmentRefused('final admitted park plan changed')
    return {**deepcopy(payload), 'wake_at': value['reassess_at'], 'awaiting': deepcopy(awaiting)}, final_backstop


PENDING_TABLE = 'kanban_owner_reassessment_pending'
_PRODUCER_KEYS = ('kind', 'ref', 'gateway', 'board', 'why')


def producer(record):
    """Project the admitted normalized identity, not gate/probe diagnostic fields.

    Preserve board as well as gateway: the same task ref on a different board is
    not the same producer. Inferred and shared-blocker waits are never opt-ins.
    """
    if not isinstance(record, dict) or record.get('shared_blocker') or record.get('inferred'):
        return None
    return {k: deepcopy(record[k]) for k in _PRODUCER_KEYS if k in record}


def _refuse(message):
    raise ReassessmentRefused(message)


def ensure_schema(conn):
    """Internal immutable journal; created inside the caller's park transaction."""
    conn.execute(f'CREATE TABLE IF NOT EXISTS {PENDING_TABLE} (event_uid TEXT PRIMARY KEY, '
                 'task_id TEXT NOT NULL, episode_uid TEXT NOT NULL UNIQUE, '
                 'receipt TEXT NOT NULL, created_at INTEGER NOT NULL)')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {PENDING_TABLE}_immutable BEFORE UPDATE ON {PENDING_TABLE} '
                 "BEGIN SELECT RAISE(ABORT, 'owner reassessment receipt is immutable'); END")
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {PENDING_TABLE}_no_delete BEFORE DELETE ON {PENDING_TABLE} '
                 "BEGIN SELECT RAISE(ABORT, 'owner reassessment receipt is immutable'); END")
    # REPLACE may delete without firing DELETE triggers (recursive_triggers OFF).
    # Intercept unique-key AND explicit rowid conflicts before conflict handling.
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {PENDING_TABLE}_no_replace BEFORE INSERT ON {PENDING_TABLE} '
                 f'WHEN EXISTS (SELECT 1 FROM {PENDING_TABLE} WHERE event_uid=NEW.event_uid '
                 'OR episode_uid=NEW.episode_uid OR rowid=NEW.rowid) '
                 "BEGIN SELECT RAISE(ABORT, 'owner reassessment receipt is immutable'); END")


def pending(conn, task_id=None):
    """Parent accessor: exact journal rows ``{event_uid, task_id, episode_uid, receipt}`` (receipt is
    the stored JSON string, byte-for-byte). An absent table means nothing was ever recorded."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (PENDING_TABLE,)).fetchone() is None:
        return []
    sql, args = f'SELECT event_uid,task_id,episode_uid,receipt FROM {PENDING_TABLE}', ()
    if task_id is not None:
        sql, args = sql + ' WHERE task_id=?', (task_id,)
    return [dict(zip(('event_uid', 'task_id', 'episode_uid', 'receipt'), row))
            for row in conn.execute(sql + ' ORDER BY rowid', args).fetchall()]


def prepare(conn, task_id, value, *, captured, record, op, expected_run_id, now, final_backstop,
            immediate=False, clock_only=False, blocker_id=None, declared=False):
    """Validate an opt-in against the FINAL admitted park, before claims are cleared.

    Call after every ordinary awaiting/deploy-window/probe/escalation gate and before the
    task/run UPDATE. Raises without private captured authority, a structured supported
    producer, an admitted future wake, or evidence that already exists on this card.
    Writes nothing.
    """
    if immediate or clock_only or blocker_id or not declared:
        _refuse('reassessment needs a timed park on an exact producer')
    request(value)
    if isinstance(final_backstop, float) and final_backstop.is_integer():
        final_backstop = int(final_backstop)
    normalized = producer(record)
    result, backstop = plan(value, now=now, final_backstop=final_backstop, awaiting=normalized,
                            payload={'wake_at': final_backstop})
    try:
        who = issuer(captured, conn, task_id)
        capability = captured._capability
        for ref in request(value)['evidence_refs']:
            if conn.execute('SELECT 1 FROM task_events WHERE uid=? AND task_id=? AND run_id=?',
                            (ref['uid'], task_id, capability.run_id)).fetchone() is None:
                _refuse('portable evidence is not on this owner run')
    except sqlite3.OperationalError as exc:  # a board without portable uids cannot carry a receipt
        raise ReassessmentRefused('board has no portable event identities') from exc
    capability = captured._capability
    if expected_run_id is None or int(expected_run_id) != capability.run_id:
        _refuse('reassessment must come from the admitted owner run')
    from hermes_cli.kanban_db_sync import uuid7
    return {'request': request(value), 'issuer': who, 'producer': normalized, 'op': op,
            'wake_at': result['wake_at'], 'backstop_at': backstop, 'issued_at': now,
            'episode_uid': uuid7(), 'board': capability.board, 'gateway': capability.gateway,
            'run_id': capability.run_id}


def continuation_park(conn, task_id, *, checkpoint, wake_at, resume_after, wake_check, check_every,
                      clock_only, awaiting, blocker_id, metadata, owner_reassessment, owner_origin,
                      expected_run_id, current_run_id, streak, now):
    """Admit the ordinary continue park before shortening it for owner reassessment.

    Return (plan, terminal_result); refusals and repeated-park escalations terminate
    the caller unchanged. All gates run in its existing write transaction, before
    claims clear. Only reassessment refusals are converted at the prepare boundary.
    """
    from hermes_cli import kanban_wake_check as kwc

    record = None
    reassess = None
    if wake_at is None and owner_reassessment is not None:
        return None, {"ok": False, "reason_code": "owner_reassessment_refused",
                      "error": "owner reassessment needs a delayed continue"}
    if wake_at is not None:
        from hermes_cli import kanban_awaiting as kaw
        try:
            record = kaw.gate(conn, task_id, awaiting=awaiting, reason=checkpoint, wake_check=wake_check,
                              clock_only=clock_only, op="continue", wake_in=resume_after,
                              blocker_id=blocker_id or (metadata or {}).get("blocker_id"))
        except kaw.AwaitingError as exc:
            return None, {"ok": False, "error": str(exc), "reason_code": exc.code}
        except kaw.RepeatedPark as rp:
            owner = kaw.escalate(conn, task_id, record=rp.record, reason=checkpoint,
                                 expected_run_id=int(current_run_id))
            if owner is None:
                return None, {"ok": False, "error": "task changed state concurrently"}
            return None, {"ok": True, "status": "todo", "run_id": None, "continuation": streak,
                          "wake_at": None, "escalated_to": owner}
        from hermes_cli import kanban_deploy_window as kdw
        wake_at, noted = kdw.apply(record, wake_at, checkpoint, now=now)
        checkpoint = noted or checkpoint
        noted, check_every = kaw.resource_apply(record, checkpoint, check_every)
        checkpoint = noted or checkpoint
        wake_check, clock_only = kaw.park_probe(conn, task_id, record, wake_check, clock_only)
        why = kaw.park_cycle_error(conn, task_id, record, wake_check)
        if why:
            return None, {"ok": False, "error": why, "reason_code": kaw.INVALID_AWAITING}
    try:
        wake_check, check_every = kwc.validate_park(
            wake_at=wake_at, wake_check=wake_check,
            check_every=check_every, clock_only=clock_only)
    except ValueError as exc:
        return None, {"ok": False, "error": str(exc)}
    if owner_reassessment is not None and wake_at is not None:
        # Every ordinary gate above judged the ORIGINAL admitted wake; only now is it shortened.
        try:
            reassess = prepare(
                conn, task_id, owner_reassessment, captured=owner_origin, record=record, op="continue",
                expected_run_id=expected_run_id, now=now, final_backstop=wake_at, clock_only=clock_only,
                blocker_id=blocker_id or (metadata or {}).get("blocker_id"),
                declared=bool(awaiting))
        except ReassessmentRefused as exc:
            return None, {"ok": False, "error": str(exc), "reason_code": "owner_reassessment_refused"}
        wake_at = reassess["wake_at"]
        resume_after = wake_at - now
    return (record, checkpoint, wake_at, wake_check, check_every, clock_only, reassess, resume_after), None


def commit(conn, prepared, *, kind, run_id):
    """After the park event is appended in the same txn: record the exact immutable receipt."""
    if run_id != prepared['run_id']:
        _refuse('park did not end the admitted owner run')
    task_id = prepared['issuer']['task_id']
    row = conn.execute('SELECT id,uid,payload,created_at,run_id FROM task_events WHERE task_id=? '
                       'AND kind=? ORDER BY id DESC LIMIT 1', (task_id, kind)).fetchone()
    try:
        data = json.loads(row[2]) if row else None
    except (TypeError, ValueError):
        data = None
    awaiting = data.get('awaiting') if isinstance(data, dict) else None
    if (row is None or not _uid(row[1]) or row[4] != run_id or not isinstance(awaiting, dict)
            or data.get('owner_reassessment_episode_uid') != prepared['episode_uid']
            or data.get('wake_at') != prepared['wake_at']
            or producer(awaiting) != prepared['producer']):
        _refuse('park event does not carry the planned reassessment')
    run = conn.execute('SELECT uid FROM task_runs WHERE id=?', (run_id,)).fetchone()
    if run is None or run[0] != prepared['issuer']['run_uid']:
        _refuse('park run identity changed')
    req = prepared['request']
    receipt = {'version': 1, 'board': prepared['board'], 'task_id': task_id,
               'gateway': prepared['gateway'], 'episode_uid': prepared['episode_uid'],
               'park_event': {'uid': row[1], 'run_uid': run[0], 'kind': kind,
                              'created_at': row[3], 'payload': row[2]},
               'issuer': {k: v for k, v in prepared['issuer'].items() if k != 'task_id'},
               'producer': prepared['producer'], 'target': req['target'],
               'issued_at': prepared['issued_at'], 'reassess_at': prepared['wake_at'],
               'backstop_at': prepared['backstop_at'], 'evidence_refs': req['evidence_refs'],
               'suppresses': ['too_long'], 'execution_authority': False}
    ensure_schema(conn)
    conn.execute(f'INSERT INTO {PENDING_TABLE} (event_uid,task_id,episode_uid,receipt,created_at) VALUES (?,?,?,?,?)',
                 (row[1], task_id, receipt['episode_uid'],
                  json.dumps(receipt, sort_keys=True, separators=(',', ':'), ensure_ascii=False), int(time.time())))
    from hermes_cli.kanban_owner_reassessment_transport import capture
    capture(conn, receipt)
    return receipt
