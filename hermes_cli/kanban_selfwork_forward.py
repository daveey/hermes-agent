"""Trusted courier authorization at the origin; never creates or queues work.

Only local accepted obligations plus a fresh authenticated Board lease authorize
an initial handoff. The immutable journal makes subsequent delivery retries safe.
"""
from __future__ import annotations

import hashlib
import json
import math
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


def _obligation(conn, origin, creator):
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
        binding = _obligation(conn, body['origin'], body['creator'])
    evidence = _lease(body['creator'], request['from'], gateway)
    downstream = {'id': 'swf-' + _hash([request['id'], request['from'], gateway, request['key'], home]),
                  'to_gw': home['gateway'], 'coll': 'tasks', 'key': home['profile'] + '/' + home['board'],
                  'action': 'selfwork-create',
                  'args': {'create': {**body['create'], 'board': home['board'],
                                      'session_id': body['origin']['session']}, 'origin': body['origin'],
                           'creator': body['creator']}}
    receipt = {'status': 'accepted', 'code': 'self_work_forward_authorized',
               'request_id': request['id'], 'downstream': downstream, 'handoff_complete': False}
    with ledger._db(path) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        prior = _replay(conn, request, home)
        if prior is not None:
            return prior
        if _obligation(conn, body['origin'], body['creator']) != binding:
            raise ValueError('self_work_creator_unauthorized')
        if evidence['lease']['expires'] <= time.time() * 1000:
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
