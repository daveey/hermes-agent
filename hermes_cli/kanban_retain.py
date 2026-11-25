"""Own admitted-run retention; never a release, completion or budget renewal."""
from __future__ import annotations

import json
import re
import time

from hermes_cli import kanban_containment_actor as actors
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_retain_transport as transport
from hermes_cli import kanban_spend_decision as decision


# The only reasons a worker may stop a scope (``reason='scope_stopped'``): the release mandate's list
# (``kanban_release.MANDATE``) plus a stop a human, the owning chat or the creator ordered, and a restriction the
# board itself reports. A finding, a design change, review CHANGES, red or pending CI and a sandbox refusal are
# none of these: on 2026-11-24/25 they were 15 of 82 scope stops and a main cause of re-stops after a release.
# The category travels in the summary (``[stop_on=...] ``) so stops can be counted by it: the Board's retain
# payload has no field for it. [daveey]
STOP_ON = {
    'provider_safety_refusal': 'a provider safety refusal on this work',
    'needs_david_physically': 'the next step needs David physically (a device in hand, a login only he can do)',
    'same_error_three_times': 'the same error three times',
    'run_budget': "the card's run budget is used up",
    'owner_instruction': ('a human told this card to stop or hand off, or the owning chat or the creator did in a '
                          'comment after the newest release'),
    'card_safety_exclusion': 'the next step would cross a safety exclusion written in the card body',
    'execution_restricted': 'the board reports a safety, containment or unreleased hold on this card',
}
NOT_A_STOP = ("Findings, design changes, review CHANGES, red or pending CI and sandbox refusals are work inside the "
              "round, not stops: fix and continue, or park the wait with kanban_continue or kanban_schedule. "
              "Agent-written stop rules older than the newest release mandate and earlier runs' stop reasons do "
              "not count; safety exclusions in the card body and a human's stop instructions do. If the "
              "acceptance is met, call kanban_complete (or kanban_request_review) instead.")
_SUMMARY_MAX = 2000


def tool_args(args: dict) -> dict:
    """The ``kanban_retain`` tool's arguments as :func:`retain` takes them, or ValueError. A scope stop must name
    an allowed ``stop_on``, folded into the summary as ``[stop_on=<category>] ``; other reasons may not carry
    one. Deterministic, so an identical retry yields the identical frozen request."""
    args = dict(args)
    stop_on = args.pop('stop_on', None)
    if args.get('reason') != 'scope_stopped':
        if stop_on is not None:
            raise ValueError("stop_on is only for reason='scope_stopped'")
        return args
    if stop_on not in STOP_ON:
        raise ValueError('A scope stop needs stop_on, one of: ' + ', '.join(STOP_ON) + '. ' + NOT_A_STOP)
    summary = args.get('summary')
    if isinstance(summary, str) and summary.strip():
        tag = f'[stop_on={stop_on}] '
        tagged = tag + summary.strip().removeprefix(tag.strip()).strip()  # a self-tagged summary is tagged once
        if len(tagged) > _SUMMARY_MAX:
            raise ValueError(f'the summary plus its [stop_on=...] tag exceeds {_SUMMARY_MAX} characters; shorten it')
        args['summary'] = tagged
    return args


def payload(args: dict) -> dict:
    allowed = {'summary', 'reason', 'next_step', 'release_condition', 'evidence', 'needs'}
    if set(args) - allowed or args.get('reason') not in {'scope_stopped', 'decision_pending', 'capability_unavailable'}:
        raise ValueError('retain requires a supported reason and no caller-supplied authority')
    for key, maximum in [('summary', 2000), ('next_step', 1000), ('release_condition', 1000)]:
        if not isinstance(args.get(key), str) or not args[key].strip() or len(args[key]) > maximum:
            raise ValueError(f'invalid retain {key}')
    evidence = args.get('evidence')
    if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 8
            or any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in evidence)):
        raise ValueError('retain requires 1..8 bounded evidence references')
    needs = args.get('needs', [])
    if (not isinstance(needs, list) or len(needs) > 8
            or any(not isinstance(v, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*:[A-Za-z0-9:._/-]{1,180}', v) for v in needs)):
        raise ValueError('invalid retain capabilities')
    return {'version': 1, **args}


def retain(conn, *, board: str, **args) -> dict:
    data = payload(args)
    actor = actors.bound_actor(conn)
    mode = sync.board_mode(conn)
    if mode == 'primary':
        from hermes_cli.kanban_sync_client import Hub, load_identity
        hub = Hub(load_identity())
        try:
            return _primary(conn, hub, board, actor, data)
        finally:
            hub.close()
    if mode != 'off' or sync.sync_state(conn):
        raise ValueError('retain requires owning primary or genuinely local board')
    return _local(conn, actor, data)


def _primary(conn, hub, board, actor, data):
    state = sync.sync_state(conn)
    if state['gateway_id'] != hub.ident.gateway or state['board_id'] != board:
        raise ValueError('retain enrolled board/gateway mismatch')
    transport.capability(hub, board)
    try:
        return _primary_retain(conn, hub, board, actor, data)
    except ValueError:
        from hermes_cli import kanban_retain_fallback
        result = kanban_retain_fallback.recover(conn, hub, board, actor, data)
        if result is not None:
            return result
        raise


def _primary_retain(conn, hub, board, actor, data):
    with kb.write_txn(conn):
        if transport.command(conn, actor.run_uid) is None:
            actors.current_actor(conn)
        stop = decision.pending_episode(conn, actor.task)
        expected = (stop.get('uid') or str(stop['id'])) if stop else None
        old = transport.command(conn, actor.run_uid)
        if old and old.get('operation_kind') == 'retain':
            expected = json.loads(old['request_json'])['expected_episode']
        owned = transport.reserve(conn, actor.task, actor.run_uid, data, expected)
    if owned.get('operation_kind') != 'retain':
        from hermes_cli.kanban_retain_safety import recover
        result = recover(conn, hub, board, actor.run_uid)
    else:
        from hermes_cli.kanban_sync_client import push
        push(conn, hub, board, hub.ident.gateway, primary=True)
        result = transport.confirm(conn, hub, board, actor.run_uid)
    transport.project(conn, hub, board, result)
    return result


def _local(conn, actor, data) -> dict:
    from hermes_cli.kanban_source_receipt import with_source_receipt
    with kb.write_txn(conn):
        prior = conn.execute("SELECT uid,payload FROM task_events WHERE task_id=? AND run_id="
                             "(SELECT id FROM task_runs WHERE uid=?) AND kind='decision_retained'",
                             (actor.task, actor.run_uid)).fetchone()
        if prior:
            receipt = json.loads(prior['payload'])
            if receipt['disposition'] != data:
                raise ValueError('retain disposition changed after terminal receipt')
            return receipt['result']
        actors.current_actor(conn)
        run_id = conn.execute('SELECT id FROM task_runs WHERE uid=?', (actor.run_uid,)).fetchone()[0]
        stop = decision.pending_episode(conn, actor.task)
        if data['reason'] == 'decision_pending' and stop is None:
            raise ValueError('missing_decision_episode')
        if stop is None:
            kb._append_event(conn, actor.task, decision.EVENT,
                             {'cause': data['reason'], 'reason': data['summary'], 'accounting_boundary': False}, run_id=run_id)
            stop = decision.pending_episode(conn, actor.task)
        elif data['reason'] == 'scope_stopped':
            kb._append_event(conn, actor.task, 'decision_hold_restricted',
                             {'cause': 'scope_stopped', 'episode': stop.get('uid') or str(stop['id'])}, run_id=run_id)
        if stop is None:
            raise ValueError('decision_episode_changed')
        decision.safe_ensure_producer(conn, actor.task)
        next_action = _next_action(conn, actor.task, stop, data)
        event_uid = sync.uuid7()
        result = {'ok': True, 'task_id': actor.task, 'run_uid': actor.run_uid, 'event_uid': event_uid,
                  'episode_uid': stop.get('uid') or str(stop['id']), 'terminal_for_run': True,
                  'dependency_satisfied': False, 'status': 'triage', 'next_action': next_action}
        with_source_receipt(conn, actor.task, lambda: conn.execute(
            "UPDATE tasks SET status='triage',claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,worker_started_at=NULL "
            "WHERE id=? AND current_run_id=?", (actor.task, run_id)), run_id=run_id)
        kb._end_run(conn, actor.task, outcome='retained', status='retained', summary=data['summary'])
        kb._append_event(conn, actor.task, 'decision_retained',
                                {'disposition': data, 'result': result, 'next_action': next_action,
                                 'episode_uid': result['episode_uid']}, run_id=run_id)
        conn.execute("UPDATE task_events SET uid=? WHERE id=(SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='decision_retained')", (event_uid, actor.task))
        return result


def _next_action(conn, task, stop, data) -> dict:
    episode = stop.get('uid') or str(stop['id'])
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? "
                       "AND json_extract(payload,'$.episode')=? ORDER BY id DESC LIMIT 1",
                       (task, decision.NOTICE, episode)).fetchone()
    route = json.loads(row[0]).get('producer') if row else None
    # No producer, or the producer found nobody to route to: the stranded-park sweep is what escalates.
    owned = isinstance(route, dict) and route.get('kind') != 'unavailable'
    return {'version': 1, 'owner': route or {'kind': 'unavailable'}, 'instruction': data['next_step'],
            'release': {'authority': 'unavailable', 'condition': data['release_condition']},
            'blocker': {'prerequisite': data['release_condition'], 'evidence': data['evidence']},
            'escalation': {'at': int(time.time()) + 86400, 'route': route if owned else None,
                           'state': 'pending' if owned else 'unavailable'}}
