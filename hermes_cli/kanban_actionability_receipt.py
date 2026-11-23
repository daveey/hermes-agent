"""Read-only confirmed escalation custody; notice emission is not delivery. [daveey]"""
from __future__ import annotations

import json

from hermes_cli import kanban_actionability_transport as transport


def escalation(reader, task, root):
    """No ACK-only receipt, damaged linkage or unrelated destination clears overdue."""
    wire = transport.envelope(task['id'], root['payload'], 'escalation')
    rows = reader.rows('SELECT * FROM kanban_actionability_commands WHERE op_id=?', (wire['client_op_id'],))
    if len(rows) != 1:
        return None
    owned = rows[0]
    try:
        if (owned['delivery_state'] != 'confirmed' or owned['phase'] != 'escalation'
                or owned['task_id'] != task['id'] or owned['contract_key'] != wire['payload']['contract_key']
                or owned['request_json'] != transport.canonical(wire)
                or owned['request_sha256'] != transport.digest(owned['request_json'])):
            return None
        receipt = json.loads(owned['canonical_receipt_json'])
        if (not isinstance(receipt, dict) or receipt.get('contract') != wire['payload']
                or receipt.get('request_uid') != wire['client_op_id']
                or receipt.get('request_sha256') != owned['request_sha256']
                or receipt.get('contract_event_uid') != wire['contract_uid']
                or receipt.get('notice_event_uid') != wire['notice_uid']
                or receipt.get('home_gateway') != task['created_on']):
            return None
        dest = receipt.get('destination')
        if not isinstance(dest, dict) or set(dest) != {'task_id', 'kind'}:
            return None
        owner = wire['payload']['owner']
        if dest['kind'] == 'task':
            if owner['kind'] != 'task' or owner['refs'] != [dest['task_id']]:
                return None
        elif dest['kind'] not in ('chat', 'unavailable') or dest['task_id'] != task['id']:
            return None
        if dest['kind'] == 'chat' and owner['kind'] == 'task':
            return None
        outcome = 'route_unavailable' if dest['kind'] == 'unavailable' else 'recorded'
        expected = {'root_task': task['id'], 'root_episode': wire['payload']['facts']['root_episode'],
                    'contract_key': wire['payload']['contract_key'], 'contract_event_uid': wire['contract_uid'],
                    'notice_event_uid': wire['notice_uid'], 'deadline_at': wire['payload']['deadline_at'],
                    'kind': 'administrative_only'}
        if receipt.get('outcome') != outcome or receipt.get('envelope') != expected:
            return None
        for uid, tid, kind, payload in (
                (wire['receipt_uid'], task['id'], 'actionability_escalation_recorded', receipt),
                (wire['notice_uid'], dest['task_id'], 'decision_requested', None)):
            events = reader.rows('SELECT * FROM task_events WHERE uid=? AND task_id=? AND kind=?', (uid, tid, kind))
            if (len(events) != 1 or events[0].get('deleted_at') is not None
                    or events[0]['origin_gateway'] != task['created_on']):
                return None
            data = json.loads(events[0]['payload'])
            if not isinstance(data, dict) or (data != payload if payload is not None else data.get('actionability') != expected):
                return None
        return {'state': outcome, 'receipt_uid': wire['receipt_uid']}
    except (KeyError, TypeError, ValueError):
        return None
