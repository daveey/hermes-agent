"""Versioned headless producer receipts; queue acknowledgements are never delivery.

Persist prepare() before advance(). The frozen request survives process restarts,
clock/config changes and transport timeouts. No API here creates locally.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from hermes_cli.kanban_selfwork import SelfWorkError, contract

CAPABILITY = {'self_work_producer': 1}
_NAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}\Z')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def prepare(create):
    """Freeze a declared, non-chat producer request without any external write."""
    from hermes_cli.kanban_selfwork_owner import _runtime

    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_WORKER_TASK'):
        raise SelfWorkError('headless producer cannot replace worker origin forwarding')
    if not isinstance(create, dict) or set(create) - {'title', 'body', 'idempotency_key', 'assignee', 'self_work'}:
        raise SelfWorkError('invalid producer create fields')
    if contract(create.get('self_work')) is None or not all(
        isinstance(create.get(k), str) and create[k].strip() for k in ('title', 'body', 'idempotency_key', 'assignee')
    ):
        raise SelfWorkError('producer requires declaration and nonempty create fields')
    config, sender, _, _ = _runtime()
    home = config.get('kanban', {}).get('self_work', {}).get('home')
    if (not isinstance(home, dict) or set(home) != {'gateway', 'profile', 'board'}
            or not all(isinstance(v, str) and _NAME.fullmatch(v) for v in [sender, *home.values()])):
        raise SelfWorkError('invalid producer intake home')
    body = {'create': {**create, 'board': home['board'], 'tenant': os.environ.get('HERMES_TENANT')}}
    identity = {'sender': sender, 'home': home, 'key': create['idempotency_key']}
    frozen = {'version': 1, 'sender': sender, 'home': home, 'body': body, 'request_id': 'sw-' + _digest(identity)}
    return json.loads(json.dumps(frozen))


def validate(frozen):
    if (not isinstance(frozen, dict) or set(frozen) != {'version', 'sender', 'home', 'body', 'request_id'}
            or type(frozen['version']) is not int or frozen['version'] != 1):
        raise SelfWorkError('invalid producer snapshot version')
    home, body, sender = frozen['home'], frozen['body'], frozen['sender']
    if (not isinstance(home, dict) or set(home) != {'gateway', 'profile', 'board'}
            or not all(isinstance(v, str) and _NAME.fullmatch(v) for v in [sender, *home.values()])
            or not isinstance(body, dict) or set(body) != {'create'} or not isinstance(body['create'], dict)):
        raise SelfWorkError('invalid producer snapshot route')
    create = body['create']
    if (set(create) != {'title', 'body', 'idempotency_key', 'assignee', 'self_work', 'board', 'tenant'}
            or create['board'] != home['board'] or contract(create['self_work']) is None
            or not all(isinstance(create[k], str) and create[k].strip()
                       for k in ('title', 'body', 'idempotency_key', 'assignee'))
            or frozen['request_id'] != 'sw-' + _digest({'sender': sender, 'home': home, 'key': create['idempotency_key']})):
        raise SelfWorkError('invalid producer snapshot payload')


def receipt(frozen, row):
    """Consume only a route/payload-bound terminal receipt from the courier ledger."""
    pending = {'version': 1, 'status': 'pending', 'request_id': frozen['request_id'], 'owner': None}
    if row is None:
        return pending
    envelope = json.loads(row['body'])
    home = frozen['home']
    expected = {'to_gw': home['gateway'], 'coll': 'tasks', 'key': home['profile'] + '/' + home['board'],
                'action': 'selfwork-create', 'args': frozen['body'], 'id': frozen['request_id']}
    if not isinstance(envelope, dict) or any(envelope.get(k) != v for k, v in expected.items()):
        raise SelfWorkError('producer courier envelope mismatch')
    result = json.loads(row['result']) if row['result'] else None
    if (row['state'] != 'done' or not isinstance(result, dict) or result.get('ok') is not True
            or result.get('handoff_complete') is not True or result.get('ownership_pending') is not False
            or result.get('code') != 'self_work_accepted' or result.get('request_id') != frozen['request_id']
            or not isinstance(result.get('task_id'), str) or not _NAME.fullmatch(result['task_id'])
            or not ((result.get('disposition') == 'create' and result.get('created') is True)
                    or (result.get('disposition') == 'append' and result.get('created') is False
                        and type(result.get('comment_id')) is int and result['comment_id'] > 0))):
        return pending
    owner = {**home, 'task_id': result['task_id']}
    return {**pending, 'status': 'completed', 'owner': owner, 'disposition': result['disposition']}


def read_receipt(frozen):
    path = Path(os.environ.get('HERMES_HUB_STATE') or Path.home() / '.local/state/hermes-hub') / 'hub.db'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        sender = db.execute("SELECT v FROM meta WHERE k='gateway'").fetchone()
        if sender is None or sender[0] != frozen['sender']:
            raise SelfWorkError('producer sender changed')
        row = db.execute('SELECT body,state,result FROM sent WHERE id=?', (frozen['request_id'],)).fetchone()
        return receipt(frozen, row)


def advance(frozen):
    from hermes_cli.kanban_selfwork_outbound import send

    validate(frozen)
    # Read before resending, including sender validation. A rejection remains pending.
    result = read_receipt(frozen)
    if result['status'] == 'completed':
        return result
    send(frozen['home'], frozen['body'], frozen['request_id'])
    return read_receipt(frozen)


def command(args):
    try:
        if args.operation == 'capabilities':
            result = CAPABILITY
        else:
            raw = sys.stdin.read() if args.request_file == '-' else Path(args.request_file).read_text(encoding='utf-8-sig')
            value = json.loads(raw)
            result = prepare(value) if args.operation == 'prepare' else advance(value)
    except (SelfWorkError, OSError, sqlite3.Error, ValueError) as exc:
        result = {'version': 1, 'status': 'pending', 'owner': None, 'error': str(exc)}
    print(json.dumps(result))
    return 0
