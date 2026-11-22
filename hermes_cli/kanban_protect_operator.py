"""Scoped stop-only operator adapter; canonical uncertainty is never a terminal result."""
from __future__ import annotations

import json
import logging
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as courier
from hermes_cli import kanban_protect_stopped as kernel
from hermes_cli import kanban_protect_transport as transport

_log = logging.getLogger(__name__)
# Unresolvable pending rows back off per request (in-memory; a restart retries once, then backs off again).
_BACKOFF: dict[str, tuple[float, float]] = {}
_BACKOFF_START, _BACKOFF_MAX = 15.0, 900.0
ACTION = transport.KIND


def normalize(i, op):
    try:
        if not isinstance(op.get('task_id'), str) or not op['task_id']:
            raise ValueError('task_id required')
        args = kernel.normalize({k: v for k, v in op.items() if k not in ('op', 'action', 'task_id')})
        if type(args['expected_updated_at']) is not int:
            raise ValueError('canonical stop-only request requires an exact integer target clock')
    except ValueError as exc:
        raise courier._err('invalid_ops', f'op {i}: {exc}') from None
    return {'op': 'operator', 'action': ACTION, 'task_id': op['task_id'], **args}


def preflight(conn, requester, op):
    def refused(code, why):
        return {**op, 'verdict': 'refused', 'code': code, 'reason': why}
    if requester == op['task_id'] or op['requested_owner'] != requester:
        return refused('protect_request_scope', 'stop-only requester must be the named existing administrative owner, not target')
    if not courier._synced(conn) or not courier._own_gateway(conn):
        return refused('not_synced', 'foreign stop-only protection requires canonical courier custody; local board uses the dedicated tool')
    with courier._replica(conn) as rep:
        row = courier._target_row(rep, op['task_id'])
        if row is None or not row['created_on']:
            return refused('protect_evidence_unknown', 'canonical target home prerequisite unavailable')
        if row['status'] not in ('todo', 'ready', 'scheduled', 'triage', 'blocked', 'review'):
            return refused('protect_target_changed', 'target must be non-executing and nonterminal')
        if not kernel.scoped_requester(rep, op['task_id'], requester):
            return refused('protect_request_scope', 'requires existing direct parent or exact current decision-owner custody')
        home = row['created_on']
    rate = courier._rate_refusal(conn, requester)
    if rate:
        return refused('rate_limited', rate)
    # Same-home is deliberately supported ONLY for this stop-only action.
    return {**op, 'verdict': 'supervisor', 'home': home}


def handle(conn, own, event, payload, refusal):
    """Freeze before I/O; subsequent ticks recover this same durable request."""
    if refusal is not None:
        return courier._execute_one(conn, payload, own, refusal)[0]
    try:
        with kb.write_txn(conn):
            transport.reserve(conn, payload['board'], payload, event['uid'])
            courier.log(conn, request_uid=payload['request_uid'], side='execute', outcome='accepted_pending_canonical',
                        op={'action': ACTION, 'task_id': payload['target_task_id'], **payload['args']},
                        caller=payload['caller'], board=payload['board'], executor_gateway=own,
                        request_event_uid=event['uid'])
    except ValueError as exc:
        if transport.command(conn, payload['request_uid']) is None:
            return courier._execute_one(conn, payload, own, 'invalid stop-only request: '+str(exc))[0]
        _log.info('stop-only original custody retained for %s: %s', payload['request_uid'], exc)
    return 'accepted_pending_canonical'


def _finish(conn, own, payload, result, *, outcome='applied'):
    uid, target = payload['request_uid'], payload['target_task_id']
    with kb.write_txn(conn):
        old = conn.execute("SELECT finished_at FROM operator_calls WHERE request_uid=? AND side='execute'", (uid,)).fetchone()
        if old is None or old[0] is not None:
            return
        detail = ('exact canonical stop receipt and held replica confirmed' if outcome == 'applied'
                  else 'canonical refusal recorded; no stop receipt' if outcome == 'refused'
                  else 'historical stop receipt confirmed; current hold not confirmed after separate renewal')
        if outcome == 'refused':
            detail += ': ' + json.dumps(result['canonical_refusal'], sort_keys=True)
        detail += '; Administrative protection only; no execution authority'
        courier.log(conn, request_uid=uid, side='execute', outcome=outcome, finished=True, detail=detail)
        kb._append_event(conn, target, courier.RESULT_KIND, {
            'request_uid': uid, 'action': ACTION, 'outcome': outcome, 'executor_gateway': own,
            'detail': detail, 'protection': result,
            'caller': {k: payload['caller'].get(k) for k in ('gateway', 'task_id', 'run_id', 'run_uid')}})


def tick(conn, own, board):
    """Bounded rotating reconciliation, outside writer locks; unrelated courier calls keep progressing."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_protect_commands'").fetchone():
        return []
    # operator_calls is the pending registry; no whole request-event tail rescan.
    cursor = conn.execute('SELECT last_id FROM kanban_protect_scan WHERE board=?', (board,)).fetchone()
    last = cursor[0] if cursor else 0
    sql = ("SELECT p.*,o.id AS log_id FROM kanban_protect_commands p JOIN operator_calls o "
           "ON o.request_uid=p.request_uid AND o.side='execute' "
           "WHERE p.board=? AND p.home=? AND o.finished_at IS NULL AND o.id>? ORDER BY o.id LIMIT ?")
    rows = conn.execute(sql, (board, own, last, courier.MAX_PER_TICK)).fetchall()
    if not rows and last:
        rows = conn.execute(sql, (board, own, 0, courier.MAX_PER_TICK)).fetchall()
    if not rows:
        return []
    with kb.write_txn(conn):
        conn.execute('INSERT INTO kanban_protect_scan VALUES (?,?) ON CONFLICT(board) DO UPDATE SET last_id=excluded.last_id',
                     (board, rows[-1]['log_id']))
    from hermes_cli import kanban_sync_client as client
    hub = client.Hub(client.load_identity())
    done = []
    for row in rows:
        old = dict(row)
        if _BACKOFF.get(old['request_uid'], (0.0, 0.0))[0] > time.monotonic():
            continue
        try:
            wire = json.loads(old['request_json'])
            from hermes_cli import kanban_protect_evidence as evidence
            evidence.frozen(old, wire, board)
            ev = hub.row_evidence(board, 'task_events', old['request_event_uid'])
            evidence.request(hub, board, old, wire)
            payload = json.loads(ev['row']['payload'])
            try:
                if old['delivery_state'] == 'rejected':
                    refusal = json.loads(old['canonical_receipt_json'])
                    if refusal.get('request_sha256') != old['request_sha256']:
                        raise ValueError('protect refusal custody mismatch')
                    raise transport.Rejected(refusal['result'])
                result = transport.confirm(conn, hub, board, old['request_uid'])
            except transport.Rejected as exc:
                _finish(conn, own, payload, {'ok': False, 'canonical_refusal': exc.result}, outcome='refused')
                _BACKOFF.pop(old['request_uid'], None)
                done.append((old['request_uid'], 'refused'))
                continue
            outcome = 'applied' if result['protection_state'] == 'held_confirmed' else 'recorded'
            _finish(conn, own, payload, result, outcome=outcome)
            _BACKOFF.pop(old['request_uid'], None)
            done.append((old['request_uid'], outcome))
        except (ValueError, OSError, client.HubError) as exc:
            delay = min(_BACKOFF_MAX, _BACKOFF.get(old['request_uid'], (0.0, _BACKOFF_START / 2))[1] * 2)
            _BACKOFF[old['request_uid']] = (time.monotonic() + delay, delay)
            _log.info('kanban: stop-only request %s pending canonical evidence: %s', old['request_uid'], exc)
    return done


def request(conn, *, board, task_id, **args):
    """Public authenticated entry; only an actual canonical receipt can claim protection."""
    from hermes_cli import kanban_containment_actor as actors
    from hermes_cli import kanban_db_sync as sync
    args = kernel.normalize(args)
    actor = actors.current_actor(conn)
    if sync.board_mode(conn) == 'off' and not sync.sync_state(conn):
        return kernel.protect(conn, board=board, task_id=task_id, **args)
    from hermes_cli import kanban_supervisor_requests as requests
    run = kb.get_task(conn, actor.task).current_run_id
    result = requests.submit(conn, env_task=actor.task, env_run=str(run),
                             ops=[{'op': 'operator', 'action': ACTION, 'task_id': task_id, **args}])
    return {**result, 'ok': True, 'protection_state': 'requested', 'terminal_for_run': False,
            'execution_authority': False, 'dependency_satisfied': False}
