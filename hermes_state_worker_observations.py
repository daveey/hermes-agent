"""Fresh worker reads fenced by the owner, without replayable write receipts."""


def worker_session_context(db, *, epoch, execution_id, session_id, generation, target):
    """Read assignment and authorized lineage context at one consistent database cut."""
    from hermes_state_runtime import RuntimeStoreError, _epoch, _worker_assignment
    from hermes_state_worker_compression import worker_lineage_context
    with db._read_ctx() as conn:
        began = not conn.in_transaction
        if began:
            conn.execute('BEGIN')
        try:
            _epoch(conn, epoch)
            row = _worker_assignment(conn, execution_id, session_id, generation)
            if row['owner_epoch'] != epoch:
                raise RuntimeStoreError('stale_epoch')
            if row['status'] not in ('registered', 'running'):
                raise RuntimeStoreError('stale_generation')
            return worker_lineage_context(db, conn, session_id, {'target': target})
        finally:
            if began and conn.in_transaction:
                conn.execute('ROLLBACK')
