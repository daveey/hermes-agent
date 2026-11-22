"""Frozen home stop-only courier custody; transport ACK is never a lifecycle result.

The local command table is a durable outbox independent of disposable sync indexes.
Unknown replies retain the same bytes and identity; only canonical receipt plus an
actual authenticated replica projection can confirm administrative protection.
"""
from __future__ import annotations

import json
import urllib.parse

from hermes_cli import kanban_actionability_transport as wireutil
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_protect_stopped as kernel

KIND = 'protect_stopped'


def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS kanban_protect_commands ('
                 'request_uid TEXT PRIMARY KEY,board TEXT NOT NULL,task_id TEXT NOT NULL,home TEXT NOT NULL,'
                 'request_event_uid TEXT NOT NULL,request_json TEXT NOT NULL,request_sha256 TEXT NOT NULL,'
                 'delivery_state TEXT NOT NULL,canonical_receipt_json TEXT)')


def command(conn, request_uid):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_protect_commands'").fetchone():
        return None
    row=conn.execute('SELECT * FROM kanban_protect_commands WHERE request_uid=?',(request_uid,)).fetchone()
    return dict(row) if row else None


def envelope(payload, request_event_uid):
    args=kernel.normalize(payload['args'])
    if type(args['expected_updated_at']) is not int:
        raise ValueError('canonical protect_stopped requires exact home row clock')
    identity=wireutil.digest(wireutil.canonical({'board':payload['board'],'home':payload['home'],
                                               'request_uid':payload['request_uid']}))
    disposition={k:args[k] for k in ('next_step','recovery_trigger','escalate_at','evidence','requested_owner')}
    return {'client_op_id':'ps-'+identity,'op':KIND,'task':payload['target_task_id'],
            'actor_task':payload['caller']['task_id'],'actor_run_uid':payload['caller']['run_uid'],
            'request_event_uid':request_event_uid,'event_uid':'pe-'+identity,
            **{k:args[k] for k in kernel.FIELDS-disposition.keys()},
            'payload':{'version':1,'request_uid':payload['request_uid'],**disposition}}


def reserve(conn, board, payload, request_event_uid):
    """Freeze only after proving the canonical operator request; no network under lock."""
    if not conn.in_transaction or sync.board_mode(conn)!='primary':
        raise ValueError('protect reservation requires primary write transaction')
    state=sync.sync_state(conn) or {}
    home=state.get('gateway_id')
    task=conn.execute('SELECT created_on FROM tasks WHERE id=?',(payload['target_task_id'],)).fetchone()
    if (payload.get('action')!=KIND or payload.get('board')!=board or payload.get('home')!=home
            or task is None or task[0]!=home):
        raise ValueError('protect reservation requires exact canonical home')
    wire=envelope(payload,request_event_uid)
    ensure_schema(conn)
    old=command(conn,payload['request_uid'])
    frozen=wireutil.canonical(wire)
    if old:
        if old['request_json']!=frozen or old['board']!=board or old['home']!=home:
            raise ValueError('protect frozen command conflict; recover original custody')
        return old
    conn.execute('INSERT INTO kanban_protect_commands '
                 '(request_uid,board,task_id,home,request_event_uid,request_json,request_sha256,delivery_state) '
                 "VALUES (?,?,?,?,?,?,?,'pending')",(payload['request_uid'],board,wire['task'],home,
                  request_event_uid,frozen,wireutil.digest(frozen)))
    return command(conn,payload['request_uid'])


def capability(hub, board):
    value=hub._call('GET',f'/v2/boards/{urllib.parse.quote(board,safe="")}/protect-stopped/capability')
    if (not isinstance(value,dict) or type(value.get(KIND)) is not int or value[KIND]!=1
            or type(value.get('frozen_replay')) is not int or value['frozen_replay']!=1
            or value.get('administrative_only') is not True):
        raise ValueError('protect prerequisite unavailable; preserve frozen request')
    return value


def confirm(conn, hub, board, request_uid):
    if conn.in_transaction:
        raise ValueError('protect confirmation cannot run under writer transaction')
    old=command(conn,request_uid)
    state=sync.sync_state(conn) or {}
    if (not old or old['board']!=board or old['home']!=hub.ident.gateway
            or state.get('gateway_id')!=old['home'] or sync.board_mode(conn)!='primary'
            or wireutil.digest(old['request_json'])!=old['request_sha256']):
        raise ValueError('protect frozen custody unavailable')
    wire=json.loads(old['request_json'])
    capability(hub,board)  # Old serving DO/unsupported route cannot consume this identity.
    reply=hub.ops(board,[wire])
    results=reply.get('results',[]) if isinstance(reply,dict) else []
    if (not isinstance(results,list) or len(results)!=1 or not isinstance(results[0],dict)
            or results[0].get('client_op_id')!=wire['client_op_id'] or results[0].get('status')!='applied'):
        raise ValueError('protect delivery unknown; preserve exact request')
    gateway=old['home']
    event,p=wireutil._evidence(hub,board,wire['event_uid'],wire['task'],kernel.KIND,gateway)
    coordinates={'task_uid':wire['task'],'request_uid':request_uid,'request_sha256':old['request_sha256'],
                 'home_gateway':gateway,'actor_task':wire['actor_task'],'actor_run_uid':wire['actor_run_uid'],
                 'request_event_uid':wire['request_event_uid'],'source_event_uid':wire['source_event_uid'],
                 'source_run_uid':wire['source_run_uid'],'proof_class':'administrative_adoption;historical_provider_unattested',
                 'disposition':wire['payload']}
    if any(p.get(k)!=v for k,v in coordinates.items()):
        raise ValueError('protect canonical receipt differs from frozen command')
    episode,_stop=wireutil._evidence(hub,board,p.get('episode_uid'),wire['task'],'spend_budget_triaged',gateway)
    source_proof=hub.row_evidence(board,'task_events',wire['source_event_uid'])
    source=source_proof.get('row',{})
    runproof=hub.row_evidence(board,'task_runs',wire['source_run_uid'])
    run=runproof.get('row',{})
    targetproof=hub.row_evidence(board,'tasks',wire['task'])
    target=targetproof.get('row',{})
    for proof,table,uid,task in ((source_proof,'task_events',wire['source_event_uid'],wire['task']),
                               (runproof,'task_runs',wire['source_run_uid'],wire['task']),
                               (targetproof,'tasks',wire['task'],wire['task'])):
        row=proof.get('row') if isinstance(proof,dict) else None
        if (not isinstance(row,dict) or proof.get('board')!=board or proof.get('table')!=table
                or proof.get('uid')!=uid or proof.get('state')!='live' or row.get('deleted_at') is not None
                or (row.get('id') if table=='tasks' else row.get('task_id'))!=task):
            raise ValueError('protect canonical target/source/run evidence unavailable')
    if (source.get('kind')!='failure_finalized' or source.get('version')!=1 or source.get('run_uid')!=run.get('uid')
            or run.get('ended_at') is None or run.get('status')=='running'
            or target.get('status') not in ('triage','archived') or target.get('current_run_uid') is not None):
        raise ValueError('protect canonical target/source/run changed')
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_db_connect import write_txn
    rows=[client._row_change('tasks',target),client._row_change('task_runs',run),
          client._row_change('task_events',source),client._row_change('task_events',episode),
          client._row_change('task_events',event)]
    sync.apply_remote_changes(conn,rows,new_cursor=state['cursor'],board=board)
    # Existing budget episodes acquire a separate monotone restriction. Wait for
    # normal authenticated sync to carry it; never manufacture it from this receipt.
    if not kernel.decision.execution_restricted(conn,wire['task']):
        raise ValueError('protect restriction projection prerequisite; preserve frozen command')
    with write_txn(conn):
        changed=conn.execute("UPDATE kanban_protect_commands SET delivery_state='confirmed',canonical_receipt_json=? "
                             'WHERE request_uid=? AND request_sha256=?',
                             (wireutil.canonical(p),request_uid,old['request_sha256'])).rowcount
        if changed!=1:
            raise ValueError('protect custody changed during confirmation')
    return {'ok':True,'task_id':wire['task'],'receipt_uid':wire['event_uid'],'episode_uid':p['episode_uid'],
            'delivery_state':'confirmed','execution_authority':False,'dependency_satisfied':False,'terminal_for_run':False}
