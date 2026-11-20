"""Trusted courier authorization at the origin; never creates or queues work.

Remote creators need an accepted origin obligation plus a fresh Board lease.
Same-gateway creators with an accepted obligation use that path too.
Origin-local detector tasks without an obligation prove the live task/run/session plus that lease.
A stopped local producer may repair the SAME rejected unauthorized envelope
from the origin hub journal using historical local provenance (sender held the
run; the journal's last-handled timestamp is within that run), never a new id.
A later journal replay can advance that timestamp and conservatively refuse repair.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_selfwork_owner as owner

_ID = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}')
_MAX_PAGES = 32
_MAX_ROWS = 1000


def _hash(value):
    return hashlib.sha256(ledger._json(value).encode()).hexdigest()


def _tuple(value, keys):
    return (isinstance(value, dict) and set(value) == set(keys)
            and all(isinstance(v, str) and _ID.fullmatch(v) for v in value.values()))


def _validate(request, config, gateway, profile):
    if not isinstance(request, dict) or not isinstance(request.get('args'), dict):
        raise ValueError('self_work_request_invalid')
    body = request['args']
    if (set(body) != {'origin', 'creator', 'create'}
            or not _tuple(body.get('origin'), ('gateway', 'profile', 'session'))
            or not _tuple(body.get('creator'), ('gateway', 'profile', 'board', 'task_id', 'run_uid'))
            or not isinstance(body.get('create'), dict)
            or body['create'].get('self_work') is None
            or any(k in body['create'] for k in ('origin_intent', 'selfwork_intent', 'origin_session', 'creator_task_id'))
            or not isinstance(request.get('id'), str) or not _ID.fullmatch(request['id'])):
        raise ValueError('self_work_request_invalid')
    from hermes_cli.kanban_selfwork import contract
    contract(body['create']['self_work'])
    origin = body['origin']
    sender = request.get('from')
    if (not isinstance(sender, str) or not sender.startswith('gw:') or not _ID.fullmatch(sender[3:])
            or request.get('coll') != 'sessions' or request.get('action') != 'selfwork-forward'
            or request.get('to_gw') != gateway or origin['gateway'] != gateway
            or profile == 'custom' or origin['profile'] != profile
            or request.get('key') != profile + '/' + origin['session']):
        raise ValueError('self_work_route_mismatch')
    kanban = config.get('kanban') if isinstance(config, dict) else None
    section = kanban.get('self_work') if isinstance(kanban, dict) else None
    home = section.get('home') if isinstance(section, dict) else None
    if not _tuple(home, ('gateway', 'profile', 'board')) or home['profile'] == 'custom':
        raise ValueError('self_work_home_invalid')
    from hermes_cli import kanban_db as kb
    if kb._normalize_board_slug(home['board']) != home['board']:
        raise ValueError('self_work_home_invalid')
    return home


def _obligation(conn, origin, creator, *, allow_missing=False):
    expected = {k: creator[k] for k in ('gateway', 'profile', 'board', 'task_id')}
    for key, digest, raw, released in conn.execute(
            'SELECT binding_id,binding_hash,binding_json,released FROM origin_obligations'):
        binding = json.loads(raw)
        ledger.validate_binding(binding)
        if binding['origin'] != origin or binding['owner'] != expected:
            continue
        if released or ledger.binding_hash(binding) != digest or ledger._binding_key(binding) != key:
            raise ValueError('self_work_creator_unauthorized')
        for stored_hash, receipt_raw in conn.execute(
                'SELECT binding_hash,receipt_json FROM origin_receipts WHERE binding_hash=?', (digest,)):
            ack = json.loads(receipt_raw)
            if (isinstance(ack, dict) and ack.get('status') == 'accepted'
                    and stored_hash == ack.get('binding_hash') == digest
                    and ack.get('binding_id') == binding['binding_id']
                    and ack.get('owner') == expected and ack.get('origin') == origin):
                return {'binding': binding, 'binding_hash': digest}
        raise ValueError('self_work_creator_unauthorized')
    if allow_missing:
        return None
    raise ValueError('self_work_creator_unauthorized')


_ENVELOPE = ('id', 'from', 'to_gw', 'coll', 'key', 'action', 'args', 'base_version')


def _local_origin(origin, creator, gateway, profile):
    return (creator['gateway'] == origin['gateway'] == gateway
            and creator['profile'] == origin['profile'] == profile
            and profile != 'custom')


def _local_proof(origin, creator, gateway, *, require_current):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_identity import _connect
    if kb._normalize_board_slug(creator['board']) != creator['board']:
        raise ValueError('self_work_creator_unauthorized')
    try:
        conn = _connect(kb.kanban_db_path(creator['board']))
    except (OSError, sqlite3.Error) as exc:
        raise ValueError('self_work_creator_unauthorized') from exc
    try:
        tasks = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
        runs = {row[1] for row in conn.execute('PRAGMA table_info(task_runs)')}
        if 'created_on' not in tasks or 'uid' not in runs:
            raise ValueError('self_work_creator_unauthorized')
        rows = conn.execute(
            '''SELECT t.created_on, t.status, t.current_run_id, r.id, r.uid, r.status, r.ended_at
               FROM tasks t JOIN task_runs r ON r.task_id=t.id
               WHERE t.id=? AND r.uid=?''', (creator['task_id'], creator['run_uid'])).fetchall()
        if len(rows) != 1 or rows[0][0] != gateway or rows[0][4] != creator['run_uid']:
            raise ValueError('self_work_creator_unauthorized')
        _created_on, status, current, run_id, _uid, run_status, ended = rows[0]
        if require_current and (status != 'running' or run_status != 'running'
                                or ended is not None or current != run_id):
            raise ValueError('self_work_creator_unauthorized')
        sessions = set()
        for (payload,) in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted'",
                (creator['task_id'], run_id)):
            try:
                data = json.loads(payload)
            except (TypeError, ValueError):
                raise ValueError('self_work_creator_unauthorized') from None
            if not isinstance(data, dict) or not isinstance(data.get('session_id'), str):
                raise ValueError('self_work_creator_unauthorized')
            sessions.add(data['session_id'])
        if sessions != {origin['session']}:
            raise ValueError('self_work_creator_unauthorized')
        return {'local_origin': True, 'task_id': creator['task_id'],
                'run_uid': creator['run_uid'], 'run_id': run_id, 'session': origin['session']}
    except sqlite3.Error as exc:
        raise ValueError('self_work_creator_unauthorized') from exc
    finally:
        conn.close()


def _rejected_unauthorized(request):
    path = Path(os.environ.get('HERMES_HUB_STATE') or Path.home() / '.local/state/hermes-hub') / 'hub.db'
    if not path.is_file() or not isinstance(request.get('id'), str):
        return None
    try:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)  # noqa: kanban-lockguard (read-only hub journal)
        try:
            row = conn.execute('SELECT envelope_json, result, at FROM req_ledger WHERE id=?',
                               (request['id'],)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None or not row[0] or not row[1] or type(row[2]) not in (int, float) or not math.isfinite(row[2]):
        return None
    try:
        stored, payload = json.loads(row[0]), json.loads(row[1])
    except (TypeError, ValueError):
        return None
    if (not isinstance(stored, dict) or not isinstance(payload, list) or len(payload) != 2
            or payload[0] != 'rejected' or not isinstance(payload[1], dict)
            or payload[1].get('code') != 'self_work_creator_unauthorized'):
        return None
    expected = {k: request[k] for k in _ENVELOPE if k in request}
    if ledger._json(stored) != ledger._json(expected):
        return None
    return row[2]


def _historical_proof(origin, creator, gateway, request):
    proof = _local_proof(origin, creator, gateway, require_current=False)
    sender = request.get('from')
    at = _rejected_unauthorized(request)
    if (not isinstance(sender, str) or not sender.startswith('gw:') or not _ID.fullmatch(sender[3:])
            or at is None):
        raise ValueError('self_work_creator_unauthorized')
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_identity import _connect
    try:
        conn = _connect(kb.kanban_db_path(creator['board']))
    except (OSError, sqlite3.Error) as exc:
        raise ValueError('self_work_creator_unauthorized') from exc
    try:
        rows = conn.execute(
            '''SELECT r.id, r.started_at, r.ended_at FROM tasks t JOIN task_runs r ON r.task_id=t.id
               WHERE t.id=? AND r.uid=?''', (creator['task_id'], creator['run_uid'])).fetchall()
        if len(rows) != 1 or rows[0][0] != proof['run_id']:
            raise ValueError('self_work_creator_unauthorized')
        run_id, started, ended = rows[0]
        if type(started) not in (int, float) or not math.isfinite(started) or at < started:
            raise ValueError('self_work_creator_unauthorized')
        if ended is not None and (type(ended) not in (int, float) or not math.isfinite(ended) or at > ended):
            raise ValueError('self_work_creator_unauthorized')
        claimed = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed' ORDER BY id DESC LIMIT 1",
            (creator['task_id'], run_id)).fetchone()
        if claimed is None:
            raise ValueError('self_work_creator_unauthorized')
        try:
            data = json.loads(claimed[0])
        except (TypeError, ValueError):
            raise ValueError('self_work_creator_unauthorized') from None
        if (not isinstance(data, dict) or data.get('holder') != sender[3:]
                or not isinstance(data.get('holder'), str) or not _ID.fullmatch(data['holder'])):
            raise ValueError('self_work_creator_unauthorized')
        return proof
    except sqlite3.Error as exc:
        raise ValueError('self_work_creator_unauthorized') from exc
    finally:
        conn.close()


def _lease(creator, sender, gateway):
    from hermes_cli.kanban_sync_client import Hub, HubError, SyncUnavailable, load_identity
    try:
        identity = load_identity()
        if identity.gateway != gateway:
            raise ValueError('self_work_route_mismatch')
        hub = Hub(identity, timeout=10, deadline=time.monotonic() + 30)
        after, seen, evidence, head = '', set(), None, None
        for _ in range(_MAX_PAGES):
            page = hub.snapshot_page(creator['board'], 'leases', after)
            if (not isinstance(page, dict) or page.get('table') != 'leases'
                    or type(page.get('head')) is not int or page['head'] < 0
                    or type(page.get('more')) is not bool
                    or not isinstance(page.get('next'), str)
                    or not isinstance(page.get('rows'), list) or len(page['rows']) > _MAX_ROWS):
                raise ValueError('self_work_lease_invalid')
            if head is not None and head != page['head']:
                raise ValueError('self_work_lease_invalid')
            head = page['head']
            previous = after
            for row in page['rows']:
                if (not isinstance(row, dict)
                        or not all(isinstance(row.get(k), str) and _ID.fullmatch(row[k])
                                   for k in ('task_id', 'run_uid', 'holder'))
                        or type(row.get('expires')) not in (int, float)
                        or not math.isfinite(row['expires'])):
                    raise ValueError('self_work_lease_invalid')
                if row['task_id'] <= previous:
                    raise ValueError('self_work_lease_invalid')
                previous = row['task_id']
                if row['task_id'] == creator['task_id']:
                    if evidence is not None:
                        raise ValueError('self_work_lease_invalid')
                    evidence = {'lease': row, 'head': page['head']}
            if page['next'] != previous:
                raise ValueError('self_work_lease_invalid')
            if not page['more']:
                if (evidence is None or evidence['lease']['run_uid'] != creator['run_uid']
                        or evidence['lease']['holder'] != sender[3:]
                        or evidence['lease']['expires'] <= time.time() * 1000):
                    raise ValueError('self_work_lease_invalid')
                return evidence
            after = page['next']
            if not after or after in seen or not page['rows'] or len(after) > 256:
                raise ValueError('self_work_lease_invalid')
            seen.add(after)
        raise ValueError('self_work_lease_invalid')
    except (OSError, HubError, SyncUnavailable) as exc:
        raise ValueError('self_work_lease_unavailable') from exc


def _schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS selfwork_forward_admissions (
        request_id TEXT PRIMARY KEY, envelope_hash TEXT NOT NULL, home_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL, receipt_json TEXT NOT NULL)''')
    conn.commit()


def _replay(conn, request, home):
    row = conn.execute('SELECT envelope_hash,home_json,receipt_json FROM selfwork_forward_admissions '
                       'WHERE request_id=?', (request['id'],)).fetchone()
    if row is None:
        return None
    if row[0] != _hash(request) or row[1] != ledger._json(home):
        raise ValueError('self_work_forward_conflict')
    return json.loads(row[2])


def receive(request: dict) -> dict:
    config, gateway, profile, root = owner._runtime()
    home = _validate(request, config, gateway, profile)
    path = root / 'kanban/origin-ledger.db'
    with ledger._db(path) as conn:
        _schema(conn)
        prior = _replay(conn, request, home)
        if prior is not None:
            return prior
        body = request['args']
        origin, creator = body['origin'], body['creator']
        historical = False
        binding = _obligation(conn, origin, creator,
                              allow_missing=_local_origin(origin, creator, gateway, profile))
        if binding is None:
            try:
                binding = _local_proof(origin, creator, gateway, require_current=True)
            except ValueError:
                if _rejected_unauthorized(request) is None:
                    raise
                binding = _historical_proof(origin, creator, gateway, request)
                historical = True
    evidence = ({'repair': 'rejected_unauthorized'} if historical
                else _lease(creator, request['from'], gateway))
    downstream = {'id': 'swf-' + _hash([request['id'], request['from'], gateway, request['key'], home]),
                  'to_gw': home['gateway'], 'coll': 'tasks', 'key': home['profile'] + '/' + home['board'],
                  'action': 'selfwork-create',
                  'args': {'create': {**body['create'], 'board': home['board'],
                                      'session_id': origin['session']}, 'origin': origin,
                           'creator': creator}}
    receipt = {'status': 'accepted', 'code': 'self_work_forward_authorized',
               'request_id': request['id'], 'downstream': downstream, 'handoff_complete': False}
    with ledger._db(path) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        prior = _replay(conn, request, home)
        if prior is not None:
            return prior
        if 'binding' not in binding and _obligation(conn, origin, creator, allow_missing=True) is not None:
            raise ValueError('self_work_creator_unauthorized')
        if historical:
            if _historical_proof(origin, creator, gateway, request) != binding:
                raise ValueError('self_work_creator_unauthorized')
        elif 'binding' in binding:
            if _obligation(conn, origin, creator) != binding:
                raise ValueError('self_work_creator_unauthorized')
        elif _local_origin(origin, creator, gateway, profile):
            if _local_proof(origin, creator, gateway, require_current=True) != binding:
                raise ValueError('self_work_creator_unauthorized')
        else:
            raise ValueError('self_work_creator_unauthorized')
        if not historical:
            lease = evidence['lease']
            if lease['expires'] <= time.time() * 1000:
                raise ValueError('self_work_lease_invalid')
        conn.execute('INSERT INTO selfwork_forward_admissions VALUES (?,?,?,?,?)',
                     (request['id'], _hash(request), ledger._json(home),
                      ledger._json({**binding, **evidence}), ledger._json(receipt)))
    return receipt


def command(args) -> int:
    try:
        raw = sys.stdin.read() if args.request_file == '-' else Path(args.request_file).read_text(encoding='utf-8-sig')
        result = receive(json.loads(raw))
    except (OSError, sqlite3.Error):
        result = {'status': 'pending', 'code': 'self_work_storage_unavailable', 'handoff_complete': False}
    except (KeyError, TypeError, ValueError) as exc:
        code = str(exc)
        if not re.fullmatch(r'(self_work|origin)_[a-z_]+', code):
            code = 'self_work_request_invalid'
        status = 'pending' if code == 'self_work_lease_unavailable' else 'rejected'
        result = {'status': status, 'code': code, 'handoff_complete': False}
    print(json.dumps(result))
    return 0
