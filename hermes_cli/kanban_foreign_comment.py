"""Append a CLI comment to a replica-only card through the authenticated Board. [daveey]

Never import a foreign task or write its replica. Only the comment and its classified writer event
are submitted. A lost/partial response is not retried: report both portable IDs for read-only recovery.
"""
from __future__ import annotations

import json
import os
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_home_command as home


class CommentUnconfirmed(ValueError):
    """The caller must reconcile the exact comment IDs, not submit a replacement."""


def append(conn, task_id, author, body, *, writer_kind, writer_session, origin_gateway=None):
    """Return the canonical comment UID, or None for the unchanged ordinary local path."""
    from hermes_cli import kanban_foreign_card as foreign
    from hermes_cli.kanban_db_connect import _main_db_file
    if conn.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
        return None
    state = sync.sync_state(conn)
    if not state or sync.board_mode(conn) not in ('primary', 'shadow'):
        return None
    with foreign.open_replica(_main_db_file(conn)) as replica:
        if replica is None or not replica.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
            return None
    if not body.strip() or not author.strip():
        raise ValueError('author and comment body are required')
    hub = client.Hub(client.load_identity())
    comment_uid, event_uid = sync.uuid7(), sync.uuid7()
    try:
        gateway = hub.ident.gateway
        if origin_gateway is not None and origin_gateway != gateway:
            raise ValueError('foreign comments cannot override the authenticated writer gateway')
        proof = hub.row_evidence(state['board_id'], 'tasks', task_id)
        row = proof.get('row') if proof.get('state') == 'live' else None
        if not isinstance(row, dict) or row.get('id') != task_id or row.get('deleted_at') is not None:
            raise ValueError('foreign comment target is not live on the Board')
        epoch = home.from_evidence(task_id, row)
        payload = {'author': author, 'len': len(body.strip()), 'comment_uid': comment_uid}
        if writer_kind:
            payload['writer_kind'] = writer_kind
            if writer_session:
                payload['writer_session'] = writer_session
        own = os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_WORKER_TASK')
        if own:
            payload['writer_task'] = own
        now = int(time.time())
        fields = [('task_comments', comment_uid, {'task_id': task_id, 'author': author.strip(),
                   'body': body.strip(), 'created_at': now}),
                  ('task_events', event_uid, {'task_id': task_id, 'run_uid': None, 'kind': 'commented',
                   'payload': json.dumps(payload), 'created_at': now})]
        with kb.write_txn(conn):
            ops = [home.outbound({'home_generations': epoch}, {'client_op_id': uid, 'op': 'upsert', 'table': table, 'uid': uid,
                    'hlc': client._next_hlc(conn, gateway), 'fields': values})
                   for table, uid, values in fields]
        recovery = (f'comment {comment_uid}, writer event {event_uid}. Do not retry; reconcile these IDs '
                    'through Board/replica readback before any replacement.')
        try:
            # The preflight GET may leave a reused socket. A fresh POST connection prevents the
            # generic stale-socket request retry from replaying this non-journaled append.
            hub.close()
            reply = hub.ops(state['board_id'], ops)
            results = reply.get('results') if isinstance(reply, dict) else None
            if (not isinstance(results, list) or len(results) != 2
                    or any(not isinstance(r, dict) or r.get('client_op_id') != op['client_op_id']
                           or r.get('status') != 'applied' for r, op in zip(results, ops))):
                raise CommentUnconfirmed('Board did not confirm both writes; ' + recovery)
            for table, uid, values in fields:
                proof = hub.row_evidence(state['board_id'], table, uid)
                row = proof.get('row') if isinstance(proof, dict) and proof.get('state') == 'live' else None
                if (not isinstance(row, dict) or row.get('uid') != uid
                        or row.get('origin_gateway') != gateway or row.get('deleted_at') is not None
                        or any(row.get(k) != v for k, v in values.items())):
                    raise CommentUnconfirmed('Board readback did not match; ' + recovery)
        except (client.HubError, client.SyncUnavailable, OSError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
            raise CommentUnconfirmed('Delivery outcome unknown; ' + recovery) from exc
        return comment_uid
    except (client.HubError, client.SyncUnavailable, OSError, TimeoutError) as exc:
        raise ValueError('Board unavailable before comment submission; no comment was sent') from exc
    finally:
        hub.close()
