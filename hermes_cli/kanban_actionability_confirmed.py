"""Compose confirmed home diagnostic custody; never infer execution authority. [daveey]"""
from __future__ import annotations

import json

from hermes_cli import kanban_actionability_transport as transport
from hermes_cli.kanban_actionability import NONTERMINAL, positive_time


def _matches(reader, task, facts, payload):
    f = payload.get('facts')
    if not isinstance(f, dict):
        return False
    run_uid = None
    if task.get('current_run_id') is not None:
        if 'uid' not in reader.columns('task_runs'):
            return False
        run = reader.conn.execute('SELECT uid FROM task_runs WHERE id=? AND task_id=?',
                                  (task['current_run_id'], task['id'])).fetchone()
        if run is None:
            return False
        run_uid = run[0]
    if any(f.get(k) != v for k, v in (
            ('status', task['status']), ('assignee', task.get('assignee')),
            ('current_run_uid', run_uid),
            ('root_episode', task.get('containment_episode') or facts.get('stop_episode')))):
        return False
    sources = f.get('source_uids')
    if not isinstance(sources, list) or any(
            not isinstance(s, dict) or set(s) != {'uid', 'version'} or
            type(s['version']) is not int or s['version'] <= 0 for s in sources):
        return False
    if [s['uid'] for s in sources] != facts.get('source_refs'):
        return False
    # Include satisfied parents too, matching the canonical contract's full graph.
    # The read-only adapter already computes the effective restricted status.
    effective = {d['task_id']: d for d in facts.get('dependencies', [])}
    parents = []
    for (pid,) in reader.conn.execute('SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id',
                                      (task['id'],)):
        parent = reader.task(pid)
        if parent is None:
            return False
        status = parent['status']
        dep = effective.get(pid, {})
        if status in ('done', 'archived') and (dep.get('restricted') or parent.get('containment_episode')):
            status = 'triage'
        parents.append({'id': pid, 'status': status})
    return f.get('dependencies') == parents


def _confirmed(reader, task, facts):
    if ('kanban_actionability_commands' not in reader.tables or
            not {'uid', 'origin_gateway'} <= reader.event_columns):
        return
    required = {'op_id', 'contract_key', 'task_id', 'phase', 'delivery_state', 'request_json',
                'request_sha256', 'canonical_receipt_json'}
    if not required <= reader.columns('kanban_actionability_commands'):
        return
    for owned in reader.rows("SELECT * FROM kanban_actionability_commands WHERE task_id=? "
                             "AND phase='contract' AND delivery_state='confirmed'", (task['id'],)):
        try:
            wire = json.loads(owned['request_json'])
            payload = wire['payload']
            receipt = json.loads(owned['canonical_receipt_json'])
            if not isinstance(wire, dict) or not isinstance(payload, dict) or not isinstance(receipt, dict):
                continue
            if (wire.get('client_op_id') != owned['op_id'] or payload.get('contract_key') != owned['contract_key'] or
                    transport.digest(owned['request_json']) != owned['request_sha256'] or
                    transport.envelope(task['id'], payload) != wire or not _matches(reader, task, facts, payload)):
                continue
            event = reader.rows("SELECT * FROM task_events WHERE uid=? AND task_id=? "
                                "AND kind='actionability_contract'", (wire['contract_uid'], task['id']))
            if len(event) != 1 or not task.get('created_on') or event[0]['origin_gateway'] != task['created_on']:
                continue
            if event[0].get('deleted_at') is not None or json.loads(event[0]['payload']) != receipt:
                continue
            if (receipt.get('contract') != payload or receipt.get('request_uid') != wire['client_op_id'] or
                    receipt.get('request_sha256') != owned['request_sha256'] or
                    receipt.get('home_gateway') != task['created_on']):
                continue
            deadline = positive_time(payload.get('deadline_at'))
            if deadline:
                yield wire, deadline
        except (KeyError, TypeError, ValueError):
            continue  # Damaged custody is not positive diagnostic evidence.


def compose(reader, task, facts, row, *, now):
    """Only an exact confirmed local receipt may fill a missing diagnostic deadline.

    Foreign replicas have no confirmation custody and remain unknown. The canonical
    confirm path independently verifies immutable version-1 home evidence. This
    reader checks persisted local custody consistency, not a new canonical read.
    ACK-only rows, arbitrary events, and stale lifecycle/graph premises never
    qualify. Only missing deadlines are filled; existing source policy is intact.
    """
    if (row['status'] not in NONTERMINAL or facts.get('pending_command')):
        return row
    needs_deadline = (not row['escalation']['at'] and row['escalation']['state'] == 'unavailable'
                      and 'escalation_deadline_missing' in row['violations'])
    needs_owner = row['owner']['kind'] == 'unavailable' and 'owner_missing' in row['violations']
    if row['escalation']['state'] == 'not_applicable' or (
            not needs_deadline and not needs_owner and 'escalation_overdue' not in row['violations']):
        return row
    contracts = sorted(_confirmed(reader, task, facts), key=lambda item: (item[1], item[0]['contract_uid']))
    if not contracts:
        return row
    wire, deadline = contracts[0]
    if needs_owner and all(w['payload']['owner'] == wire['payload']['owner'] for w, _ in contracts):
        candidate = wire['payload']['owner']
        if candidate['kind'] == 'task' and len(candidate['refs']) == 1:
            parent = reader.task(candidate['refs'][0])
            linked = candidate['refs'][0] in {d['id'] for d in wire['payload']['facts']['dependencies']}
            if parent and linked and parent['status'] in NONTERMINAL and parent['id'] != task['id']:
                row['owner'] = candidate
                row['violations'] = [v for v in row['violations'] if v != 'owner_missing']
    if needs_deadline:
        row['escalation']['at'] = deadline
        row['escalation']['state'] = 'pending'
        row['violations'] = [v for v in row['violations'] if v != 'escalation_deadline_missing']
        if row['blocker'] and now >= deadline and 'escalation_overdue' not in row['violations']:
            row['violations'].append('escalation_overdue')
    if row['escalation']['at'] == deadline:
        from hermes_cli.kanban_actionability_receipt import escalation
        receipt = escalation(reader, task, wire)
        if receipt:
            row['escalation'].update(receipt)
            if receipt['state'] == 'recorded' and row['owner'] == wire['payload']['owner']:
                row['violations'] = [v for v in row['violations'] if v != 'escalation_overdue']
    row['source_refs'] = list(dict.fromkeys([*row['source_refs'], wire['contract_uid']]))
    # Identity, action, blocker, release policy and UNKNOWN eligibility remain source-derived.
    return row
