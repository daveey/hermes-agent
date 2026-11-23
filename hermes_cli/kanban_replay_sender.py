"""W=1 production push entry for already bound strict journals. [daveey]

No enrollment or activation. This WIP handles ordinary upsert/delete intents and
parent-first transport, but still refuses unintegrated modes/control/dispositions.
Provenance, drainage and all custody consumers must pass before it can ship.
"""
import json
from dataclasses import dataclass
from urllib.parse import quote

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_replay_reconcile import reconcile


@dataclass
class PushResult:
    sent: int = 0
    applied: int = 0
    merged: int = 0
    rejected: int = 0
    deferred: int = 0
    cancelled: int = 0


def push(conn, hub, board, gateway, *, primary, max_batches=25, before_head=False, seen=None):
    """Select only the journal-bound transport; never auto-enroll or fall back."""
    from hermes_cli import kanban_sync_client as client
    client.refuse_if_schema_ahead(conn)
    if (sync.sync_state(conn) or {}).get('journal_id') is not None:
        if primary or before_head:
            raise ValueError('tracer push mode not integrated')
        return _push_strict(conn, hub, board, gateway, max_batches=max_batches)
    return client._push_legacy(conn, hub, board, gateway, primary=primary,
                               max_batches=max_batches, before_head=before_head, seen=seen)


def _push_strict(conn, hub, board, gateway, *, max_batches):
    from hermes_cli import kanban_sync_client as client
    root = f'/v2/boards/{quote(board, safe="")}/replay/'
    out = client.PushResult()
    local = journal.snapshot(conn)
    cap = hub._call('GET', root + 'capability')
    if (cap.get('processed_prefix') is not True or
            cap.get('board_incarnation') != local['board_incarnation'] or
            cap.get('gateway_principal') != local['gateway_principal']):
        raise ValueError('replay capability unavailable')
    remote = hub._call('GET', root + 'inspect')['slot']
    reconcile(local, remote)  # inspect before authenticated full-state session CAS
    identity = dict(board_incarnation=local['board_incarnation'], journal_id=local['journal_id'])
    expected = dict(remote, session=str(int(remote['session']) + 1))
    remote = hub._call('POST', root + 'session', dict(identity, expected=remote))['slot']
    if remote != expected:
        raise ValueError('session readback mismatch')
    reconcile(local, remote)

    def source():
        pending = journal.snapshot(conn)['pending']
        row = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (pending['op_id'],)).fetchone()
        wire = json.loads(journal.pending_wire(conn))
        del wire['replay']
        if row is None:
            saved = conn.execute('SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?', (wire['uid'],)).fetchone()
            if saved is None or json.loads(saved[0]) != wire:
                raise ValueError('issued source custody missing')
            return dict(row_uid=wire['uid'], table_name='tasks', op='upsert', _parent=True)
        if row['wire_json'] is None or json.loads(row['wire_json']) != wire:
            raise ValueError('issued source custody changed')
        if (row['row_uid'] != wire['uid'] or row['table_name'] != wire['table'] or
                row['op'] != wire['op'] or row['state'] != 'pending'):
            raise ValueError('issued source custody routing changed')
        return dict(row)

    membership = None

    def refresh_capacity():
        from hermes_cli import kanban_replay_deferred as deferred
        current = journal.snapshot(conn)
        decision = reconcile(current, remote)
        if decision['action'] != 'record' or decision['verdict']['status'] != 'deferred':
            return None
        wire = json.loads(journal.pending_wire(conn))
        active = [item for item in deferred.pending(conn)
                  if item['board_incarnation'] == current['board_incarnation'] and item['task'] == wire['uid']]
        if len(active) < 16 or cap.get('deferred_observation') != 'origin_pending1':
            return None
        source()  # Frozen source must still exist before any reconciliation read.
        reply = hub._call('GET', root + 'deferred?task=' + quote(wire['uid'], safe=''))
        return deferred.validate_membership(reply, current, wire['uid']), active

    def record(verdict):
        pending = journal.snapshot(conn)['pending']
        if verdict['client_op_id'] != pending['op_id']:
            raise ValueError('source verdict identity mismatch')
        row = source()
        status = verdict['status']
        if status in ('rejected', 'cancelled'):
            if row.get('_parent'):
                # Move exact synthetic custody into the ordinary visible repair queue.
                # This is not a new intent, a retry, or an authoritative row success.
                from time import time
                from hermes_cli.kanban_sync_envelope import retire_parent
                wire = json.loads(journal.pending_wire(conn))
                del wire['replay']
                conn.execute('INSERT INTO kanban_sync_outbox '
                             '(op_id,table_name,row_uid,op,fields,hlc,created_at,status_origin,wire_json) '
                             'VALUES (?,?,?,?,?,?,?,?,?)',
                             (pending['op_id'], 'tasks', row['row_uid'], 'upsert', json.dumps(wire['fields']),
                              wire['hlc'], int(time() * 1000), wire.get('status_origin'), json.dumps(wire)))
                retire_parent(conn, row['row_uid'])
            reason = f"{verdict.get('reason') or status}: {verdict.get('detail') or ''}".strip(': ')
            if status == 'cancelled':
                # Reuse non-success custody so dependency/import fences also hold
                # cancelled intents. The label and retained verdict distinguish them.
                reason = f'cancelled: {reason}'
            conn.execute("UPDATE kanban_sync_outbox SET state='rejected', last_error=?, attempts=attempts+1 "
                         "WHERE op_id=?", (reason[:500], pending['op_id']))
            setattr(out, status, getattr(out, status) + 1)
            return  # Keep the semantic obligation visible; no sync success or identity rotation.
        if status == 'deferred':
            if cap.get('deferred_observation') != 'origin_pending1' or row.get('_parent'):
                raise ValueError('tracer final disposition requires custody integration')
            from hermes_cli.kanban_replay_deferred import handoff
            handoff(conn, json.loads(journal.pending_wire(conn)), verdict, local['board_incarnation'],
                    membership=membership)
            conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (pending['op_id'],))
            out.deferred += 1
            return  # Transport handoff is not authoritative row success.
        if status not in ('applied', 'merged'):
            raise ValueError('tracer final disposition requires custody integration')
        if row.get('_parent') and not verdict.get('version'):
            raise ValueError('synthetic parent authoritative version unavailable')
        if verdict.get('version') and row['table_name'] == 'tasks':
            conn.execute('UPDATE tasks SET sync_version=MAX(COALESCE(sync_version,0),?) WHERE id=?',
                         (verdict['version'], row['row_uid']))
        elif verdict.get('version') and row['table_name'] == 'task_runs':
            conn.execute('UPDATE task_runs SET sync_version=MAX(COALESCE(sync_version,0),?) WHERE uid=?',
                         (verdict['version'], row['row_uid']))
        if row.get('_parent'):
            from hermes_cli.kanban_sync_envelope import retire_parent
            retire_parent(conn, row['row_uid'])
        else:
            conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (pending['op_id'],))
        setattr(out, status, getattr(out, status) + 1)

    for _ in range(max_batches):
        from hermes_cli.kanban_sync_parent_order import parent_first, select_pending
        from time import time
        rows = []
        if journal.pending_wire(conn) is None:
            rows, _, _ = select_pending(conn, hub, board, {}, now=int(time()), limit=1, before_head=False,
                                        hold_rejected=True)
        membership = refresh_capacity()
        with write_txn(conn):
            decision = journal.recover(conn, remote, record)
            if decision['action'] == 'record':
                break  # recovery settles exactly one; no newly selected intent this turn
            encoded = journal.pending_wire(conn)
            if encoded is None:
                if not rows:
                    break
                current = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (rows[0]['op_id'],)).fetchone()
                if current is None or dict(current) != rows[0]:
                    raise ValueError('selected source changed before issuance')
                rows = parent_first(conn, rows, int(time()), 1, hold_rejected=True)
                if not rows:
                    break
                row = rows[0]
                if row['op'] not in ('upsert', 'delete'):
                    raise ValueError('tracer operation kind not integrated')
                from hermes_cli import kanban_sync_envelope as envelopes
                from hermes_cli import kanban_sync_wire as wire_ops
                from hermes_cli.kanban_terminal_guard import strip_stale_push
                parents = client._unacked_parents(conn, rows, gateway)
                if parents:
                    op, tid = parents[0]
                    intent = envelopes.parent(conn, tid, lambda: op, supported=True)
                else:
                    intent = envelopes.outbound(conn, row, lambda: strip_stale_push(conn, wire_ops.translate(
                        client._full_row_if_unseen(conn, row), client._next_hlc(conn, gateway))), supported=True)
                encoded = journal.issue(conn, intent)
            source()  # Validate retained source custody on RETRY as well as final recovery.
        wire = json.loads(encoded)
        wire['replay']['session'] = remote['session']
        hub._call('POST', root + 'ops', dict(identity, ops=[wire]))
        out.sent += 1
        remote = hub._call('GET', root + 'inspect')['slot']
        membership = refresh_capacity()
        with write_txn(conn):
            decision = journal.recover(conn, remote, record)
        if decision['action'] != 'record':
            break
    settled = journal.snapshot(conn)
    if settled['settled'] != '0' and remote['a'] != settled['settled']:
        expected = dict(remote, a=settled['settled'])
        remote = hub._call('POST', root + 'ack', dict(identity, session=remote['session'],
            generation=settled['generation'], ack=dict(sequence=settled['settled'],
                op_id=settled['last_op_id'], payload_sha256=settled['last_payload_sha256'])))['slot']
        if remote != expected:
            raise ValueError('ack readback mismatch')
        reconcile(settled, remote)
    return out
