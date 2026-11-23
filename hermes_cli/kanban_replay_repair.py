"""Existing bounded identity-changing retry, only after final replay settlement. [daveey]"""
from time import time


def retryable(row, verdict):
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_sync_compat import transient_rejection
    return (verdict['status'] == 'rejected' and transient_rejection(verdict)
            and row.get('attempts', 0) + 1 < client.MAX_TRANSIENT_ATTEMPTS)


def remap_refusal(conn, row, verdict):
    """A recorded non-success may mint a new identity; an uncertain send may not."""
    from hermes_cli import kanban_sync_client as client
    if not retryable(row, verdict):
        return
    from hermes_cli import kanban_replay_journal as journal
    if journal.pending_wire(conn) is not None:
        raise ValueError('retry requires settled replay custody')
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_sync_omissions import remap
    from hermes_cli.kanban_sync_reserved_key import remap as remap_key
    identity = sync.uuid7()
    remap(conn, row['op_id'], identity)
    remap_key(conn, row['op_id'], identity)
    reason = f"{verdict.get('reason')}: {verdict.get('detail') or ''}".strip(': ')
    conn.execute("UPDATE kanban_sync_outbox SET op_id=?,wire_json=NULL,state='pending',last_error=?,retry_after=? "
                 "WHERE op_id=? AND state='rejected'",
                 (identity, f'retrying: {reason}'[:500], int(time()) + client._backoff_s(row['attempts']), row['op_id']))
