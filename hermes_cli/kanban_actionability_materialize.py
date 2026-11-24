"""Bounded home-only original-intent delivery; no escalation or lifecycle authority. [daveey]"""
from __future__ import annotations

import json

from hermes_cli import kanban_actionability_creation as creation
from hermes_cli import kanban_actionability_observation as observation
from hermes_cli import kanban_actionability_recovery as recovery
from hermes_cli import kanban_actionability_transport as transport
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_db_connect import write_txn


class StaleIntent(ValueError):
    """A positively observed source mismatch, not an unavailable observation."""


def _payload(conn, hub, board, row):
    if not callable(getattr(hub, '_call', None)):
        raise ValueError('canonical execution inventory transport unavailable')
    source = observation.observe(hub, board, row['task_id'], include_source=True)
    event, task = source['source_event'], source['source_task']
    if not isinstance(event.get('payload'), str):
        raise ValueError('canonical original source payload unavailable')
    if (event['uid'] != row['event_uid'] and row['source_kind'] == 'dependency_wait'
            and task['status'] == 'todo' and event['kind'] in ('created', 'linked')):
        raise ValueError('canonical dependency source unavailable')
    if (task['containment_episode'] or event['uid'] != row['event_uid']
            or event['kind'] != row['source_kind'] or event['created_at'] != row['source_at']
            or event.get('payload') != row['source_json'] or event['version'] != 1):
        raise StaleIntent('original source identity/status/run changed')
    data = json.loads(row['source_json'])
    if 'actionability_run_uid' not in data or data['actionability_run_uid'] != task['current_run_uid']:
        raise StaleIntent('original source run changed')
    from hermes_cli.kanban_actionability_sources import Reader
    if Reader(conn).pending(row['task_id']):
        raise ValueError('unresolved retain custody; administrative obligation remains pending')
    parents = [p['id'] for p in source['facts']['dependencies']]
    intent = creation.validated_intent(task, {'kind': event['kind'], 'created_at': event['created_at'], 'data': data},
                                       parent_ids=parents)
    if intent is None or intent['deadline_at'] != row['deadline_at']:
        raise StaleIntent('original owner/trigger/deadline/graph changed')
    from hermes_cli.kanban_actionability_parents import check
    check(data.get('actionability_parent_states'), source['raw_parent_states'])
    cap = transport.capability(hub, board)
    return recovery._payload(source, owner=intent['owner'], action=intent['action'],
                             instruction=intent['instruction'], prerequisite=intent['blocker']['prerequisite'],
                             trigger=intent['blocker']['trigger'], evidence=[row['event_uid']],
                             deadline_at=intent['deadline_at'], now=int(cap['hlc'][:13]) // 1000)


def _set(conn, row, state, reason=None):
    with write_txn(conn):
        conn.execute('UPDATE kanban_actionability_intents SET state=?,reason=? WHERE event_uid=? AND state=? AND op_id IS ?',
                     (state, reason, row['event_uid'], row['state'], row['op_id']))


def run(conn, hub, board):
    """One rotating eight-source window; uncertain reservations never rederive."""
    from hermes_cli.kanban_sync_client import HubError
    if conn.in_transaction or sync.board_mode(conn) != 'primary':
        return
    state = sync.sync_state(conn) or {}
    if state.get('board_id') != board or state.get('gateway_id') != hub.ident.gateway:
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_actionability_intents'").fetchone():
        return
    select = ('SELECT * FROM kanban_actionability_intents '
              "WHERE state IN ('pending','reserved') ORDER BY last_checked_at,event_uid LIMIT 8")
    rows = [dict(r) for r in conn.execute(select)]
    if not rows:
        return
    if not callable(getattr(hub, '_call', None)):
        reason = 'canonical execution inventory transport unavailable'
        for row in rows:
            if row['reason'] != reason:
                _set(conn, row, row['state'], reason)
        return
    with write_txn(conn):
        rows = [dict(r) for r in conn.execute(select)]
        conn.executemany(f'UPDATE kanban_actionability_intents SET last_checked_at={sync._NOW_MS_SQL} WHERE event_uid=?',
                         [(r['event_uid'],) for r in rows])
    for row in rows:
        uid = row['event_uid']
        try:
            if row['state'] == 'pending':
                payload = _payload(conn, hub, board, row)
                with write_txn(conn):
                    current = conn.execute('SELECT * FROM kanban_actionability_intents WHERE event_uid=?', (uid,)).fetchone()
                    if (current['state'] != 'pending' or current['source_json'] != row['source_json']
                            or current['deadline_at'] != row['deadline_at']):
                        raise ValueError('intent custody changed during canonical observation')
                    command = transport.reserve(conn, row['task_id'], payload)
                    conn.execute("UPDATE kanban_actionability_intents SET state='reserved',op_id=?,reason=NULL WHERE event_uid=?",
                                 (command['op_id'], uid))
                    row['op_id'] = command['op_id']
                    row['state'] = 'reserved'
            command = transport.command(conn, row['op_id'])
            if command and command['delivery_state'] == 'rejected':
                _set(conn, row, 'rejected', 'canonical request rejected')
                continue
            if not command or command['delivery_state'] != 'confirmed':
                transport.confirm(conn, hub, board, row['op_id'])
            _set(conn, row, 'confirmed')
        except StaleIntent as exc:
            _set(conn, row, 'stale', str(exc))
        except (ValueError, KeyError, TypeError, OSError, HubError) as exc:
            _set(conn, row, row['state'], str(exc)[:500])
