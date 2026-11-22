"""W=1 production push entry for already bound strict journals. [daveey]

No enrollment or activation. Until migration/provenance and all custody paths are
finished this is a task-upsert tracer only; other intents stop without deletion.
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
        if row is None:
            raise ValueError('issued source custody missing')
        wire = json.loads(journal.pending_wire(conn))
        del wire['replay']
        if row['wire_json'] is None or json.loads(row['wire_json']) != wire:
            raise ValueError('issued source custody changed')
        if (row['row_uid'] != wire['uid'] or row['table_name'] != wire['table'] or
                row['op'] != wire['op'] or row['state'] != 'pending'):
            raise ValueError('issued source custody routing changed')
        return row

    def record(verdict):
        pending = journal.snapshot(conn)['pending']
        if verdict['client_op_id'] != pending['op_id']:
            raise ValueError('source verdict identity mismatch')
        row = source()
        status = verdict['status']
        if status == 'rejected':
            reason = f"{verdict.get('reason') or 'rejected'}: {verdict.get('detail') or ''}".strip(': ')
            conn.execute("UPDATE kanban_sync_outbox SET state='rejected', last_error=?, attempts=attempts+1 "
                         "WHERE op_id=?", (reason[:500], pending['op_id']))
            out.rejected += 1
            return  # Keep the semantic obligation visible; no sync success or identity rotation.
        if status not in ('applied', 'merged'):
            raise ValueError('tracer final disposition requires custody integration')
        if verdict.get('version'):
            conn.execute('UPDATE tasks SET sync_version=MAX(COALESCE(sync_version,0),?) WHERE id=?',
                         (verdict['version'], row['row_uid']))
        conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (pending['op_id'],))
        setattr(out, status, getattr(out, status) + 1)

    for _ in range(max_batches):
        with write_txn(conn):
            decision = journal.recover(conn, remote, record)
            if decision['action'] == 'record':
                break  # recovery settles exactly one; no newly selected intent this turn
            encoded = journal.pending_wire(conn)
            if encoded is None:
                rows = [dict(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox WHERE state='pending' "
                                                      "ORDER BY created_at,rowid LIMIT 1")]
                if not rows:
                    break
                row = rows[0]
                if row['table_name'] != 'tasks' or row['op'] != 'upsert':
                    raise ValueError('tracer requires task upsert head')
                from hermes_cli import kanban_sync_envelope as envelopes
                from hermes_cli import kanban_sync_wire as wire_ops
                from hermes_cli.kanban_terminal_guard import strip_stale_push
                intent = envelopes.outbound(conn, row, lambda: strip_stale_push(conn, wire_ops.translate(
                    client._full_row_if_unseen(conn, row), client._next_hlc(conn, gateway))), supported=True)
                encoded = journal.issue(conn, intent)
            source()  # Validate retained source custody on RETRY as well as final recovery.
        wire = json.loads(encoded)
        wire['replay']['session'] = remote['session']
        hub._call('POST', root + 'ops', dict(identity, ops=[wire]))
        out.sent += 1
        remote = hub._call('GET', root + 'inspect')['slot']
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
