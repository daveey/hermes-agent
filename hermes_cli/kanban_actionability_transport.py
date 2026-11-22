"""Frozen administrative actionability commands; never execution or hold authority. [daveey]

Custody outlives transport ACK and disposable scheduling indexes. Unknown replies
retain exact bytes indefinitely. Production producers opt in only after deployment.
"""
from __future__ import annotations

import hashlib
import json
import re


from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_retain_transport as retain

KIND = 'actionability_record'


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def ensure_schema(conn) -> None:
    """Local custody, deliberately absent from sync/capture table inventories."""
    conn.execute('CREATE TABLE IF NOT EXISTS kanban_actionability_commands ('
                 'op_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, contract_key TEXT NOT NULL, phase TEXT NOT NULL, '
                 'request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL, delivery_state TEXT NOT NULL, '
                 'canonical_receipt_json TEXT, UNIQUE(task_id,contract_key,phase))')


def envelope(task_id: str, payload: dict, phase: str = 'contract') -> dict:
    if phase not in ('contract', 'escalation'):
        raise ValueError('unsupported actionability phase')
    payload = json.loads(canonical(payload))
    key = digest(canonical({'task': task_id, 'facts': payload['facts'], 'owner': payload['owner'],
                            'prerequisite': payload['blocker']['prerequisite'],
                            'trigger': payload['blocker']['trigger'], 'action': payload['action']}))
    if payload.get('contract_key', key) != key:
        raise ValueError('actionability semantic key changed')
    payload['contract_key'] = key
    identity = digest(task_id + ':' + key)
    return {'client_op_id': f'ar-{identity}-{phase}', 'op': KIND, 'task': task_id, 'phase': phase,
            'contract_uid': f'ac-{identity}', 'receipt_uid': f'ae-{identity}', 'notice_uid': f'an-{identity}',
            'payload': payload}


def command(conn, op_id: str) -> dict | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_actionability_commands'").fetchone():
        return None
    row = conn.execute('SELECT * FROM kanban_actionability_commands WHERE op_id=?', (op_id,)).fetchone()
    return dict(row) if row else None


def reserve(conn, task_id: str, payload: dict, *, phase: str = 'contract') -> dict:
    """Freeze inside the home producer transaction; never make network calls here."""
    if not conn.in_transaction or sync.board_mode(conn) != 'primary':
        raise ValueError('actionability reservation requires primary write transaction')
    state = sync.sync_state(conn) or {}
    task = conn.execute('SELECT created_on FROM tasks WHERE id=?', (task_id,)).fetchone()
    if task is None or not task[0] or task[0] != state.get('gateway_id'):
        raise ValueError('actionability requires exact home gateway')
    wire = envelope(task_id, payload, phase)
    ensure_schema(conn)
    old = command(conn, wire['client_op_id'])
    frozen = canonical(wire)
    if old:
        if old['request_json'] != frozen:
            raise ValueError('actionability request changed after first committed bytes')
        return old
    conn.execute('INSERT INTO kanban_actionability_commands '
                 '(op_id,task_id,contract_key,phase,request_json,request_sha256,delivery_state) '
                 "VALUES (?,?,?,?,?,?,'pending')", (wire['client_op_id'], task_id, wire['payload']['contract_key'],
                                                     phase, frozen, digest(frozen)))
    conn.execute('INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) '
                 f"VALUES (?,'tasks',?,?,?,'',{sync._NOW_MS_SQL})", (wire['client_op_id'], task_id, KIND, frozen))
    return command(conn, wire['client_op_id'])


def frozen_wire(row: dict) -> dict:
    value = row['fields'] if isinstance(row['fields'], dict) else json.loads(row['fields'])
    if (not isinstance(value, dict) or value.get('op') != KIND or value.get('client_op_id') != row['op_id']
            or value.get('task') != row['row_uid']):
        raise ValueError('actionability outbox identity differs from frozen request')
    return value


def outbound(conn, row: dict) -> dict:
    if row['op'] == 'retain':
        return retain.outbound(conn, row)
    wire = frozen_wire(row)
    owned = command(conn, row['op_id'])
    if (not owned or owned['delivery_state'] == 'rejected' or owned['request_json'] != canonical(wire)
            or owned['request_sha256'] != digest(owned['request_json'])
            or owned['task_id'] != wire['task'] or envelope(wire['task'], wire['payload'], wire['phase']) != wire):
        raise ValueError('actionability outbox lost immutable custody')
    return wire


def capability(hub, board: str) -> dict:
    value = hub.actionability_capability(board)
    if (not isinstance(value, dict) or type(value.get('actionability_record')) is not int
            or value['actionability_record'] != 1 or type(value.get('frozen_replay')) is not int
            or value['frozen_replay'] != 1 or value.get('diagnostic_only') is not True
            or not isinstance(value.get('hlc'), str)
            or not re.fullmatch(r'[0-9]{13}-[0-9]{4}-server', value['hlc'])):
        raise ValueError('actionability unsupported; canonical diagnostic deployment prerequisite')
    return value


def own_batch(rows: list[dict]) -> list[dict]:
    for i, row in enumerate(rows):
        if row['op'] in ('retain', KIND):
            return rows[:i] if i else rows[:1]
    return rows


def before_send(conn, rows, hub, board) -> None:
    retain.before_send(conn, rows, hub, board)
    if any(row['op'] == KIND for row in rows):
        capability(hub, board)
        for row in rows:
            if row['op'] == KIND:
                outbound(conn, row)


def record_result(conn, row: dict, result: dict) -> str:
    if row.get('op') != KIND:
        return retain.record_result(conn, row, result)
    old = command(conn, row['op_id'])
    if old is None:
        raise ValueError('actionability custody missing')
    if old['delivery_state'] in ('confirmed', 'acknowledged', 'rejected'):
        return 'deferred'
    from hermes_cli.kanban_sync_compat import transient_rejection
    if result.get('status') == 'applied':
        conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (row['op_id'],))
        state, count = 'acknowledged', 'applied'
    elif result.get('status') == 'rejected' and not transient_rejection(result):
        conn.execute("UPDATE kanban_sync_outbox SET state='rejected',last_error=? WHERE op_id=?",
                     (str(result.get('reason'))[:500], row['op_id']))
        state, count = 'rejected', 'rejected'
    else:
        conn.execute('UPDATE kanban_sync_outbox SET attempts=attempts+1,last_error=? WHERE op_id=?',
                     ('diagnostic delivery unknown; replay exact frozen identity after canonical recovery', row['op_id']))
        state, count = 'unknown', 'deferred'
    conn.execute('UPDATE kanban_actionability_commands SET delivery_state=? WHERE op_id=?', (state, row['op_id']))
    return count


def _evidence(hub, board, uid, task_id, kind, gateway):
    proof = hub.row_evidence(board, 'task_events', uid)
    row = proof.get('row') if isinstance(proof, dict) else None
    if (not isinstance(row, dict) or proof.get('board') != board or proof.get('table') != 'task_events'
            or proof.get('uid') != uid or proof.get('state') != 'live' or row.get('uid') != uid
            or row.get('task_id') != task_id or row.get('kind') != kind or row.get('version') != 1
            or row.get('deleted_at') is not None or row.get('origin_gateway') != gateway):
        raise ValueError('actionability canonical evidence unavailable or changed identity')
    payload = json.loads(row.get('payload') or '{}')
    if not isinstance(payload, dict):
        raise ValueError('actionability canonical evidence malformed')
    return row, payload


def confirm(conn, hub, board: str, op_id: str) -> dict:
    """Bounded exact replay, independent evidence and local projection, outside writer txn."""
    if conn.in_transaction:
        raise ValueError('actionability network confirmation cannot run under a writer transaction')
    old = command(conn, op_id)
    if not old or old['delivery_state'] == 'rejected':
        raise ValueError('actionability frozen custody unavailable or rejected')
    wire = outbound(conn, {'op': KIND, 'op_id': op_id, 'row_uid': old['task_id'], 'fields': old['request_json']})
    capability(hub, board)
    reply = hub.ops(board, [wire])
    results = reply.get('results', []) if isinstance(reply, dict) else []
    if (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
            or results[0].get('client_op_id') != op_id or results[0].get('status') != 'applied'):
        raise ValueError('actionability delivery unknown; preserve exact request')
    gateway = hub.ident.gateway
    root, root_p = _evidence(hub, board, wire['contract_uid'], wire['task'], 'actionability_contract', gateway)
    expected_root = envelope(wire['task'], wire['payload'], 'contract')
    if (root_p.get('contract') != wire['payload'] or root_p.get('home_gateway') != gateway
            or root_p.get('request_uid') != expected_root['client_op_id']
            or root_p.get('request_sha256') != digest(canonical(expected_root))):
        raise ValueError('actionability root does not attest frozen contract')
    rows, receipt = [root], root_p
    if wire['phase'] == 'escalation':
        event, receipt = _evidence(hub, board, wire['receipt_uid'], wire['task'], 'actionability_escalation_recorded', gateway)
        if (receipt.get('request_sha256') != old['request_sha256'] or receipt.get('request_uid') != op_id
                or receipt.get('contract') != wire['payload'] or receipt.get('contract_event_uid') != wire['contract_uid']
                or receipt.get('notice_event_uid') != wire['notice_uid'] or receipt.get('home_gateway') != gateway
                or receipt.get('outcome') not in ('recorded', 'route_unavailable')):
            raise ValueError('actionability escalation does not attest frozen request')
        dest = receipt.get('destination')
        if not isinstance(dest, dict) or dest.get('kind') not in ('task', 'chat', 'unavailable'):
            raise ValueError('actionability destination unconfirmed')
        owner = wire['payload']['owner']
        target = owner['refs'][0] if dest['kind'] == 'task' and owner['kind'] == 'task' and len(owner['refs']) == 1 else wire['task']
        outcome = 'route_unavailable' if dest['kind'] == 'unavailable' else 'recorded'
        if (set(dest) != {'task_id', 'kind'} or dest['task_id'] != target or receipt['outcome'] != outcome
                or dest['kind'] == 'task' and owner['kind'] != 'task'
                or dest['kind'] == 'chat' and owner['kind'] == 'task'):
            raise ValueError('actionability destination does not match frozen owner')
        notice, notice_p = _evidence(hub, board, wire['notice_uid'], dest.get('task_id'), 'decision_requested', gateway)
        expected = {'root_task': wire['task'], 'root_episode': wire['payload']['facts']['root_episode'],
                    'contract_key': wire['payload']['contract_key'], 'contract_event_uid': wire['contract_uid'],
                    'notice_event_uid': wire['notice_uid'], 'deadline_at': wire['payload']['deadline_at'], 'kind': 'administrative_only'}
        if receipt.get('envelope') != expected or notice_p.get('actionability') != expected:
            raise ValueError('actionability notice linkage unconfirmed')
        rows.extend([event, notice])
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_db_connect import write_txn
    # Actual authenticated remote-apply path. Do not advance the normal feed cursor.
    changes = [client._row_change('task_events', row) for row in rows]
    sync.apply_remote_changes(conn, changes, new_cursor=sync.sync_state(conn)['cursor'], board=board)
    with write_txn(conn):
        changed = conn.execute("UPDATE kanban_actionability_commands SET canonical_receipt_json=?,delivery_state='confirmed' "
                               "WHERE op_id=? AND request_sha256=? AND delivery_state!='rejected'",
                               (canonical(receipt), op_id, old['request_sha256'])).rowcount
        if not changed:
            raise ValueError('actionability custody changed during confirmation')
        conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (op_id,))
    return {'ok': True, 'task_id': wire['task'], 'contract_key': wire['payload']['contract_key'],
            'phase': wire['phase'], 'receipt_uid': wire['contract_uid'] if wire['phase'] == 'contract' else wire['receipt_uid'],
            'delivery_state': 'confirmed', 'outcome': receipt.get('outcome', 'contract_recorded'),
            'received_by_chat': False, 'terminal_for_run': False, 'execution_authority': False}
