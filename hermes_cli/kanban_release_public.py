"""Public-only final APPLIED attestation from authenticated Board row evidence.

Not a private consumption certificate. Exact public payload strings avoid a new
cross-language JSON hash convention. Delivery is already public, including its
read inventory; no private task, allowance, proposal or separate comments escape.
"""
from __future__ import annotations

from hermes_cli import kanban_release_application as app, kanban_release_certificate as cert

MAX_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
FIELDS = set(app.EVENT_FIELDS)


def event(value, *, task, home=None):
    if (not cert._exact(value, FIELDS) or value['task_id'] != task
            or not all(cert.intent._coordinate(value[k]) for k in ('uid', 'origin_gateway'))
            or not isinstance(value['kind'], str) or not cert._integer(value['created_at'])
            or home is not None and value['origin_gateway'] != home):
        raise ValueError('invalid public application event')
    cert.parse(value['payload'])
    return value


def _validate(value, *, board, task, uid):
    if (not cert._exact(value, {'version', 'board', 'reader_gateway', 'intent', 'renewal', 'delivery', 'events'})
            or type(value['version']) is not int or value['version'] != 1 or value['board'] != board):
        raise ValueError('invalid public application attestation')
    cert.parse(cert.encode(value), max_bytes=MAX_BYTES)
    body = cert.intent.validate(value['intent'])
    if (cert.encode(body) != cert.encode(value['intent']) or body['board'] != board or body['task_id'] != task
            or body['actor']['gateway'] == body['home'] or not cert.intent._coordinate(value['reader_gateway'])
            or value['reader_gateway'] == body['home']):
        raise ValueError('public application intent differs')
    renewal = event(value['renewal'], task=task)
    if renewal['kind'] != 'scope_renewed':
        raise ValueError('public application renewal kind differs')
    digest = cert.intent.digest(body)
    cert._payload(renewal, body, digest, renewal['uid'])
    delivery = event(value['delivery'], task=task, home=body['actor']['gateway'])
    cp = cert.parse(delivery['payload'])
    if (delivery['kind'] != 'operator_call_requested' or cp.get('version') != 1
            or cp.get('carrier') != 'owner_release' or cp.get('action') != 'owner_release'
            or cp.get('request_uid') != delivery['uid'] or cp.get('board') != board
            or cp.get('target_task_id') != task or cp.get('home') != body['home']
            or cp.get('digest') != digest or cert.encode(cp.get('intent')) != cert.encode(body)):
        raise ValueError('public application carrier differs')
    rows = value['events']
    if not isinstance(rows, list) or not 3 <= len(rows) <= 38:
        raise ValueError('public application event count differs')
    for row in rows:
        event(row, task=task, home=renewal['origin_gateway'])
    ids = [body['intent_uid'], renewal['uid'], delivery['uid'], *[r['uid'] for r in rows]]
    if len(set(ids)) != len(ids) or rows[-1]['uid'] != uid:
        raise ValueError('public application identities collide')
    kinds = [r['kind'] for r in rows]
    prefix = ['wake_episode_evidence'] if kinds[0] == 'wake_episode_evidence' else []
    offset = len(prefix)
    if kinds[offset] == 'commented':
        prefix += ['commented', 'spend_budget_set']
    expected = prefix + ['stop_released'] * (1 + len(body['capability_episodes'])) + ['promoted_manual', 'owner_release_applied']
    if kinds != expected:
        raise ValueError('public application generated sequence differs')
    # Narrow validation context only: never returned as a consumption certificate.
    context = {'intent': body, 'renewal': renewal, 'consume': {'client_op_id': renewal['uid']}}
    result = app._result(rows[-1], context, delivery['uid'])
    effects = [r for r in rows if r['kind'] in ('stop_released', 'spend_budget_set', 'promoted_manual')]
    if result['outcome'] != 'released' or result['effect_uids'] != [r['uid'] for r in effects]:
        raise ValueError('public application result effects differ')
    app._effects(effects, context)
    return {'intent': body, 'renewal': renewal, 'delivery_uid': delivery['uid'],
            'result': result, 'result_event': rows[-1], 'effects': effects}


def validate(value, *, board, task, uid):
    try:
        return _validate(value, board=board, task=task, uid=uid)
    except (KeyError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid public application structure') from exc


def matches(current, frozen):
    """Portable projection equality, exact strings; local integer IDs irrelevant."""
    return (current.get('run_uid') is None and current.get('run_id') is None
            and current.get('deleted_at') is None
            and cert.encode({k: current.get(k) for k in app.EVENT_FIELDS}) == cert.encode(frozen))


def evidence(value, *, board, task, uid):
    if (not isinstance(value, dict) or value.get('board') != board or value.get('table') != 'task_events'
            or value.get('uid') != uid or value.get('state') != 'live' or value.get('tombstone') is not None):
        raise ValueError('public application row evidence differs')
    attestation = value['owner_release_applied_v1']
    result = validate(attestation, board=board, task=task, uid=uid)
    if not isinstance(value.get('row'), dict) or not matches(value['row'], result['result_event']):
        raise ValueError('public application canonical result differs')
    return attestation, result
