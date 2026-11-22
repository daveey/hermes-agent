"""Read-only recovery when automatic release won custody before own-run retain."""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_retain_transport as transport


def recover(conn, hub, board: str, run_uid: str) -> dict:
    owned = transport.command(conn, run_uid)
    if not owned or owned.get('operation_kind') == 'retain':
        raise ValueError('no earlier release custody to recover')
    task_id = owned['task_id']
    run = transport._evidence(hub, board, 'task_runs', run_uid)
    task = transport._evidence(hub, board, 'tasks', task_id)
    metadata = json.loads(run.get('metadata') or '{}')
    if (run.get('task_id') != task_id or run.get('outcome') != 'safety_refused' or run.get('ended_at') is None
            or task.get('current_run_uid') == run_uid or metadata.get('cause') != 'safety_refusal'):
        raise ValueError('earlier release is not an independently verified safety terminal')
    source = transport._evidence(hub, board, 'task_events', metadata['source_result_event_uid'])
    proof = json.loads(source.get('payload') or '{}')
    if (source.get('task_id') != task_id or source.get('run_uid') != run_uid or source.get('kind') != 'worker_result'
            or source.get('version') != 1 or source.get('origin_gateway') != hub.ident.gateway
            or proof.get('failed') is not True or proof.get('failure_reason') != 'content_policy_blocked'
            or proof.get('failure_retryable') is not False):
        raise ValueError('earlier release safety source differs from the admitted run')
    candidates = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND run_id=(SELECT id FROM task_runs WHERE uid=?) "
                              "AND kind IN ('spend_budget_triaged','decision_hold_restricted') AND json_valid(payload) "
                              "AND json_extract(payload,'$.cause')='safety_refusal' ORDER BY id DESC", (task_id, run_uid)).fetchall()
    if not candidates:
        raise ValueError('canonical safety annotation not received: pull the ordinary Board feed, then recover this same command')
    restriction_uid = candidates[0]['uid']
    restriction = transport._evidence(hub, board, 'task_events', restriction_uid)
    restriction_payload = json.loads(restriction.get('payload') or '{}')
    if (restriction.get('task_id') != task_id or restriction.get('run_uid') != run_uid
            or restriction.get('kind') not in ('spend_budget_triaged','decision_hold_restricted')
            or restriction_payload.get('cause') != 'safety_refusal'
            or restriction_payload.get('source_result_event_uid') != source['uid']):
        raise ValueError('canonical safety annotation identity changed')
    episode_uid = restriction_payload.get('episode') or restriction_uid
    receipt = {'task_uid':task_id, 'run_uid':run_uid, 'operation_kind':'release', 'op_id':owned['op_id'],
               'source_result_event_uid':source['uid'], 'restriction_event_uid':restriction_uid,
               'episode_uid':episode_uid, 'verified_safety_terminal':True}
    with kb.write_txn(conn):
        conn.execute("UPDATE kanban_failure_commands SET canonical_receipt_json=?,delivery_state='confirmed' WHERE run_uid=? AND op_id=?",
                     (transport.canonical(receipt), run_uid, owned['op_id']))
        conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],))
    return {'ok':True,'task_id':task_id,'run_uid':run_uid,'event_uid':restriction_uid,'episode_uid':episode_uid,
            'request_uid':owned['op_id'],'status':'triage','terminal_for_run':True,'dependency_satisfied':False,
            'disposition':'superseded_by_safety_stop','next_action':restriction_payload}
