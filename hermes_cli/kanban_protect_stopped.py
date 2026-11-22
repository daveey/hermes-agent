"""Scoped home administrative stop adoption, never provider attestation or release.

The admitted requester must be an existing direct parent or the exact episode's
recorded decision producer. This module owns the local stop-only kernel; synced
requests require canonical home confirmation, never a local fallback.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from hermes_cli import kanban_containment_actor as actors
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_spend_decision as decision

KIND = 'decision_protected_stopped'
FIELDS = {'source_event_uid', 'source_run_uid', 'expected_status', 'expected_current_run_uid',
          'expected_updated_at', 'expected_episode', 'requested_owner', 'next_step', 'recovery_trigger',
          'escalate_at', 'evidence'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def normalize(args):
    if set(args) != FIELDS:
        raise ValueError('protect_stopped requires exact stop-only fields')
    for key in ('source_event_uid', 'source_run_uid', 'requested_owner'):
        if not isinstance(args[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,200}', args[key]):
            raise ValueError('protect_stopped invalid coordinate: ' + key)
    if (args['expected_status'] not in ('todo', 'ready', 'scheduled', 'triage', 'blocked', 'review')
            or args['expected_current_run_uid'] is not None
            or args['expected_updated_at'] is not None and type(args['expected_updated_at']) is not int
            or args['expected_episode'] is not None and not isinstance(args['expected_episode'], str)):
        raise ValueError('protect_stopped invalid expected target')
    for key in ('next_step', 'recovery_trigger'):
        if not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > 1000:
            raise ValueError('protect_stopped invalid ' + key)
    evidence = args['evidence']
    if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 8
            or any(not isinstance(e, str) or not e.strip() or len(e) > 512 for e in evidence)
            or type(args['escalate_at']) is not int):
        raise ValueError('protect_stopped invalid evidence/deadline')
    return json.loads(canonical(args))


def scoped_requester(conn, target, requester):
    if target == requester:
        return False
    owner = kb.get_task(conn, requester)
    if owner is None or owner.status in ('done', 'archived'):
        return False
    if conn.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?', (requester, target)).fetchone():
        return True
    stop = decision.pending_episode(conn, target)
    if stop is None:
        return False
    episode = stop.get('uid') or str(stop['id'])
    def latest(task, kind):
        row = conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1', (task, kind)).fetchone()
        try:
            p = json.loads(row[0]) if row else None
        except (TypeError, ValueError):
            return {}
        return p if isinstance(p, dict) else {}
    role, notice = latest(requester, 'administrative_decision'), latest(target, decision.NOTICE)
    return (role.get('role') == 'administrative_decision' and role.get('root_task') == target
            and role.get('root_episode') == episode and notice.get('episode') == episode
            and notice.get('producer') == {'kind': 'supervisor', 'task_id': requester})


def eligible_target(conn, target, actor, args):
    if args['requested_owner'] != actor.task or not scoped_requester(conn, target, actor.task):
        raise ValueError('protect_request_scope')
    task = kb.get_task(conn, target)
    stop = decision.pending_episode(conn, target)
    episode = (stop.get('uid') or str(stop['id'])) if stop else None
    from hermes_cli.kanban_containment_guard import pending
    if task is None or pending(conn, target):
        raise ValueError('protect_target_unavailable')
    if (task.status != args['expected_status'] or args['expected_updated_at'] is not None
            or task.current_run_id is not None or task.claim_lock or episode != args['expected_episode']
            or conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL LIMIT 1', (target,)).fetchone()):
        raise ValueError('protect_target_changed')
    source = conn.execute('SELECT * FROM task_events WHERE task_id=? AND uid=?', (target, args['source_event_uid'])).fetchone()
    run = conn.execute('SELECT * FROM task_runs WHERE task_id=? AND uid=?', (target, args['source_run_uid'])).fetchone()
    try:
        p = json.loads(source['payload']) if source else {}
    except (TypeError, ValueError):
        p = {}
    if (not source or source['kind'] != 'failure_finalized' or not run or source['run_id'] != run['id']
            or run['ended_at'] is None or run['status'] == 'running' or not isinstance(p, dict)
            or p.get('run_uid') != args['source_run_uid'] or p.get('reason_code') != 'provider_refusal'
            or p.get('tripped') is not True or p.get('final_status') != 'triage'
            or p.get('infrastructure', False) is not False):
        raise ValueError('protect_source_unconfirmed')
    if not int(time.time()) < args['escalate_at'] <= int(time.time()) + 14 * 86400:
        raise ValueError('protect_stopped deadline must be future and within 14 days')
    return stop


def protect(conn, *, board, task_id, **args):
    args = normalize(args)
    actor = actors.bound_actor(conn)
    if sync.board_mode(conn) != 'off' or sync.sync_state(conn):
        raise ValueError('protect_stopped canonical home prerequisite; never local fallback')
    identity = hashlib.sha256(canonical({'actor': actor.task, 'run': actor.run_uid, 'target': task_id,
                                        'source': args['source_event_uid']}).encode()).hexdigest()
    uid = 'ps-' + identity
    frozen = canonical({'actor_task': actor.task, 'actor_run_uid': actor.run_uid, 'task': task_id, 'args': args})
    with kb.write_txn(conn):
        old = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? AND uid=?", (task_id, KIND, uid)).fetchone()
        if old:
            receipt = json.loads(old[0])
            if receipt['request_json'] != frozen:
                raise ValueError('protect_stopped frozen request conflict')
            if not decision.execution_restricted(conn, task_id):
                raise ValueError('protect_receipt_unconfirmed')
            return receipt['result']
        actors.current_actor(conn)
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
                        "AND json_extract(payload,'$.source_event_uid')=?", (task_id, KIND, args['source_event_uid'])).fetchone():
            raise ValueError('protection_already_recorded; recover original request, never renew deadline')
        stop = eligible_target(conn, task_id, actor, args)
        cause = {'cause': 'scope_stopped', 'accounting_boundary': False,
                 'reason': 'Explicit home administrative stop adoption; legacy provider provenance unattested',
                 'source_event_uid': args['source_event_uid'], 'source_run_uid': args['source_run_uid']}
        if stop is None:
            kb._append_event(conn, task_id, decision.EVENT, cause)
        elif not decision.execution_restricted(conn, task_id):
            kb._append_event(conn, task_id, 'decision_hold_restricted', {**cause, 'episode': stop.get('uid') or str(stop['id'])})
        stop = decision.pending_episode(conn, task_id)
        episode = stop.get('uid') or str(stop['id'])
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (task_id,))
        result = {'ok': True, 'task_id': task_id, 'event_uid': uid, 'episode_uid': episode,
                  'target_status': 'triage', 'terminal_for_run': False, 'dependency_satisfied': False,
                  'execution_authority': False}
        kb._append_event(conn, task_id, KIND, {'version': 1, 'request_json': frozen,
                          'source_event_uid': args['source_event_uid'], 'source_run_uid': args['source_run_uid'],
                          'episode_uid': episode, 'actor_task': actor.task, 'actor_run_uid': actor.run_uid,
                          'proof_class': 'administrative_adoption;historical_provider_unattested',
                          'disposition': args, 'result': result})
        conn.execute("UPDATE task_events SET uid=? WHERE id=(SELECT MAX(id) FROM task_events WHERE task_id=? AND kind=?)", (uid, task_id, KIND))
        from hermes_cli.kanban_protect_guards import install
        install(conn)
        return result
