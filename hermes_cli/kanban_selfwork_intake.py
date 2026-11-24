"""Single-writer admission; caller must authenticate routing before admission.

The journal is local to the configured authority, not replicated admission state.
It freezes a request's result, including appends, before transport acknowledges it.
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_selfwork as policy
from hermes_cli import kanban_selfwork_batch as batch
from hermes_cli import kanban_selfwork_guards as guards

_CREATION = ContextVar('self_work_intake_creation', default=None)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@contextmanager
def _creation_scope(conn, value):
    token = _CREATION.set((conn, _json(value), threading.get_ident()))
    try:
        yield
    finally:
        _CREATION.reset(token)


def assert_admitted_creation(conn, value):
    current = _CREATION.get()
    if (current is None or current[0] is not conn or current[1] != _json(value)
            or current[2] != threading.get_ident() or not conn.in_transaction):
        raise policy.SelfWorkError('declared self-work requires authoritative intake')


def _authority(conn, authority, local_gateway):
    from hermes_cli.kanban_db_sync import sync_state
    state = sync_state(conn)
    actual = state['gateway_id'] if state else local_gateway
    if not authority or not local_gateway or actual != local_gateway or actual != authority:
        raise policy.SelfWorkError('self-work admission must run at configured authority')


def replay(conn, request_id, request):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='self_work_intake' AND type='table'").fetchone():
        return None
    row = conn.execute('SELECT request_json,receipt_json FROM self_work_intake WHERE request_id=?',
                       (request_id,)).fetchone()
    if row is None:
        return None
    if row['request_json'] != _json(request):
        raise policy.SelfWorkError('self-work request identity reused with different intent')
    return {**json.loads(row['receipt_json']), 'replayed': True}


def _adopt(conn, intent, task_id, board):
    from hermes_cli.kanban_origin_creation import adopt_with_origin
    from hermes_cli.kanban_origin import OriginError
    adoption = {**intent, 'owner': {**intent['owner'], 'task_id': task_id}}
    try:
        adopt_with_origin(conn, adoption, board=board)
    except OriginError as exc:
        if exc.code != 'origin_already_bound':
            raise


def admit(conn, *, request_id, authority, local_gateway, options, now, create_guard=None,
          on_created=None, request=None, exception=None):
    _authority(conn, authority, local_gateway)
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', request_id):
        raise policy.SelfWorkError('self-work requires a stable request identity')
    value = policy.contract(options.get('self_work'))
    if value is None:
        raise policy.SelfWorkError('self-work intake requires explicit declaration')
    options = json.loads(_json({**options, 'self_work': value}))
    intent = options.get('origin_intent')
    if intent is not None and (not isinstance(intent, dict) or not isinstance(intent.get('owner'), dict)
                               or intent['owner'].get('gateway') != authority):
        raise policy.SelfWorkError('self-work origin owner must be admission authority')
    request = options if request is None else request
    with kb.write_txn(conn, allow_nested=True):
        _authority(conn, authority, local_gateway)
        conn.execute('CREATE TABLE IF NOT EXISTS self_work_intake ('
                     'request_id TEXT PRIMARY KEY NOT NULL, request_json TEXT NOT NULL, receipt_json TEXT NOT NULL)')
        prior = replay(conn, request_id, request)
        if prior is not None:
            return prior
        from hermes_cli.kanban_lanes import lane_tenant
        tenant = lane_tenant(conn, tuple(options.get('parents', ())), options.get('initial_status', 'running'),
                             options.get('triage', False), options.get('tenant'), options)
        if exception is not None:  # the one seam that spends it; a rollback below un-spends it
            from hermes_cli import kanban_selfwork_exception as sx
            sx.spend(conn, exception, request_id=request_id, tenant=tenant, now=now)
        decision = guards.choose(conn, value, now=now, tenant=tenant, excepted=exception is not None)
        if decision.disposition == 'append':
            receipt = batch.append(conn, decision, value, request_id=request_id,
                                   title=options['title'], body=options.get('body'),
                                   author=options.get('created_by') or 'system')
            if intent := options.get('origin_intent'):
                _adopt(conn, intent, decision.task_id, options.get('board') or kb.get_current_board())
        else:
            if options.get('idempotency_key') and conn.execute(
                "SELECT 1 FROM tasks WHERE idempotency_key=? AND status!='archived' LIMIT 1",
                (options['idempotency_key'],),
            ).fetchone():
                raise policy.SelfWorkError('self-work idempotency key already names another request')
            from hermes_cli.kanban_db_creation import run_insert_guard
            run_insert_guard(conn, create_guard)
            with _creation_scope(conn, value):
                # [daveey] Owned by its repository's standing owner chat, settled in this create transaction. No
                # requester chat authority is needed (#4252): not strict, so a state.db failure leaves it ownerless.
                from hermes_cli.kanban_owner_chat import selfwork_producer
                task_id = kb.create_task(conn, **{**options, 'settle_owner_chat': True,
                                                  'owner_producer': selfwork_producer(value)})
            policy.record(conn, task_id, value, day=batch.day_key(now))
            if on_created is not None:
                on_created(conn, task_id)
            receipt = dict(request_id=request_id, disposition='create', task_id=task_id, created=True)
        receipt.update(ownership_pending=False, **_origin_receipt(conn, options, receipt['task_id']))
        _store(conn, request_id, request, receipt)
    return receipt


def _store(conn, request_id, request, receipt):
    conn.execute('INSERT INTO self_work_intake VALUES (?,?,?)', (request_id, _json(request), _json(receipt)))


def _origin_receipt(conn, options, task_id):
    intent = options.get('origin_intent')
    if intent is None:
        return {}
    owner = {**intent['owner'], 'task_id': task_id}
    for row in conn.execute('SELECT * FROM origin_creation_intents WHERE task_id=?', (task_id,)):
        binding = json.loads(row['binding_json'])
        if binding['owner'] == owner and binding['origin'] == intent['origin']:
            return {'handoff_complete': False, 'origin_creation': {
                'request_id': row['request_id'], 'binding': binding, 'child_request_id': row['child_request_id']}}
    raise policy.SelfWorkError('self-work origin adoption has no durable receipt')


def append_existing(conn, *, request_id, authority, options, request, now, exception=None):
    """Try append before creation preflight; a miss performs no writes.

    With the home's exception capability this is a read-only check: an early append here would spend the
    exception outside :func:`admit` and skip the create path's validation, so reuse and create both go on to it."""
    _authority(conn, authority, authority)
    if exception is not None:
        from hermes_cli import kanban_selfwork_exception as sx
        prior = replay(conn, request_id, request)
        if prior is None:
            sx.check(conn, exception, request_id=request_id, now=now)
        return prior
    value = policy.contract(options.get('self_work'))
    with kb.write_txn(conn, allow_nested=True):
        prior = replay(conn, request_id, request)
        if prior is not None:
            return prior
        from hermes_cli.kanban_lanes import lane_tenant
        tenant = lane_tenant(conn, tuple(options.get('parents', ())), options.get('initial_status', 'running'),
                             options.get('triage', False), options.get('tenant'), options)
        if guards.choose(conn, value, now=now, tenant=tenant, report=False).disposition != 'append':
            return None
        return admit(conn, request_id=request_id, authority=authority, local_gateway=authority,
                     options=options, request=request, now=now)
