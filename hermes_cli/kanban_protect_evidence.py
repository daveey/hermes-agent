"""Exact canonical stop-only proof validation; no mutation or release authority."""
from __future__ import annotations

import json

from hermes_cli import kanban_actionability_transport as util
from hermes_cli import kanban_protect_stopped as kernel


def request(hub, board, old, wire):
    from hermes_cli.kanban_protect_transport import envelope
    proof = hub.row_evidence(board, 'task_events', old['request_event_uid'])
    row = proof.get('row') if isinstance(proof, dict) else None
    if (not isinstance(row, dict) or proof.get('board') != board or proof.get('table') != 'task_events'
            or proof.get('uid') != old['request_event_uid'] or proof.get('state') != 'live'
            or row.get('uid') != old['request_event_uid'] or row.get('deleted_at') is not None
            or row.get('kind') != 'operator_call_requested' or row.get('version') != 1
            or row.get('task_id') != wire.get('actor_task') or row.get('run_uid') != wire.get('actor_run_uid')):
        raise ValueError('protect canonical request evidence unavailable')
    payload = json.loads(row['payload'])
    caller = payload.get('caller') if isinstance(payload, dict) else None
    if (not isinstance(caller, dict) or not caller.get('gateway') or row.get('origin_gateway') != caller['gateway']
            or caller.get('task_id') != wire.get('actor_task') or caller.get('run_uid') != wire.get('actor_run_uid')
            or payload.get('request_uid') != old['request_uid'] or payload.get('target_task_id') != old['task_id']
            or payload.get('action') != 'protect_stopped' or payload.get('board') != board or payload.get('home') != old['home']
            or envelope(payload, old['request_event_uid']) != wire):
        raise ValueError('protect canonical request differs from frozen command')
    return caller['gateway']


def rows(hub, board, wire, home):
    found = []
    for table, uid in (('task_events', wire['source_event_uid']), ('task_runs', wire['source_run_uid']),
                       ('tasks', wire['task'])):
        proof = hub.row_evidence(board, table, uid)
        row = proof.get('row') if isinstance(proof, dict) else None
        if (not isinstance(row, dict) or proof.get('board') != board or proof.get('table') != table
                or proof.get('uid') != uid or proof.get('state') != 'live' or row.get('deleted_at') is not None
                or (row.get('id') if table == 'tasks' else row.get('uid')) != uid
                or (row.get('id') if table == 'tasks' else row.get('task_id')) != wire['task']):
            raise ValueError('protect canonical target/source/run evidence unavailable')
        found.append(row)
    source, run, target = found
    p = json.loads(source['payload'])
    if (source.get('kind') != 'failure_finalized' or source.get('version') != 1
            or source.get('run_uid') != wire['source_run_uid'] or not isinstance(p, dict)
            or p.get('run_uid') != wire['source_run_uid'] or p.get('reason_code') != 'provider_refusal'
            or p.get('tripped') is not True or p.get('final_status') != 'triage'
            or p.get('infrastructure', False) is not False or run.get('ended_at') is None
            or run.get('status') == 'running' or target.get('created_on') != home
            or target.get('status') not in ('triage', 'archived') or target.get('current_run_uid') is not None):
        raise ValueError('protect canonical target/source/run changed')
    return source, run, target


def projected(conn, task, episode_uid):
    episode = kernel.decision.pending_episode(conn, task)
    if not episode or episode.get('uid') != episode_uid:
        return False
    p = episode.get('payload') or {}
    if isinstance(p, str):
        p = json.loads(p)
    if p.get('cause') in ('scope_stopped', 'safety_refusal'):
        return True
    return conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='decision_hold_restricted' "
                        "AND json_valid(payload) AND json_extract(payload,'$.episode')=? "
                        "AND json_extract(payload,'$.cause') IN ('scope_stopped','safety_refusal') LIMIT 1",
                        (task, episode_uid)).fetchone() is not None


def frozen(old, wire, board):
    identity = util.digest(util.canonical({'board': board, 'home': old['home'], 'request_uid': old['request_uid']}))
    if (wire.get('client_op_id') != 'ps-' + identity or wire.get('event_uid') != 'pe-' + identity
            or wire.get('op') != 'protect_stopped' or wire.get('task') != old['task_id']
            or wire.get('request_event_uid') != old['request_event_uid']
            or not isinstance(wire.get('payload'), dict) or wire['payload'].get('request_uid') != old['request_uid']
            or type(wire.get('expected_updated_at')) is not int):
        raise ValueError('protect frozen envelope identity unavailable')
