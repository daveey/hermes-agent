"""Fail-closed sender admission for damaged or unbound local replay custody."""
import json

from hermes_cli.kanban_db_connect import write_txn

_RECOVERY = 'kanban_replay_recovery_loss1'
_SCHEMA = f'CREATE TABLE IF NOT EXISTS {_RECOVERY} (singleton INTEGER PRIMARY KEY CHECK(singleton=1), loss TEXT NOT NULL)'


class RestoreRequired(ValueError):
    """Local custody is incomplete; network retry cannot repair this board."""


def mark_rebuilt(conn, losses, *, enrolled=False):
    """Persist recovery independently of a potentially lost sync-state row.

    This marker is custody LOSS, not a reconstructed receipt or reconciliation.
    Reconciliation of an incomplete database is deliberately not provided here.
    Never-enrolled images with no protocol-table loss keep legacy transport.
    """
    if not losses and not enrolled:
        return
    conn.execute(_SCHEMA)
    old = conn.execute(f'SELECT loss FROM {_RECOVERY} WHERE singleton=1').fetchone()
    history = json.loads(old[0]) if old else []
    history.append(dict(reason='rebuilt database; custody completeness unknown', tables=losses))
    conn.execute(f'INSERT OR REPLACE INTO {_RECOVERY} VALUES (1,?)', (json.dumps(history),))
    columns = {row[1] for row in conn.execute('PRAGMA table_info(kanban_sync_state)')}
    if 'replay_restore_required' in columns:
        conn.execute('UPDATE kanban_sync_state SET replay_restore_required=1')


def guard(conn):
    """Never use NULL anchor as evidence that previously bound custody was absent."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    recovered = _RECOVERY in tables
    state = conn.execute('SELECT journal_id,replay_restore_required FROM kanban_sync_state LIMIT 1').fetchone()
    held = recovered or (state is not None and state[1] != 0)
    if 'kanban_replay_sender1' in tables:
        from hermes_cli import kanban_replay_journal as journal
        try:
            journal.snapshot(conn)
        except ValueError:
            held = True
    elif state is not None and state[0] is not None:
        held = True
    if held:
        with write_txn(conn):
            conn.execute('UPDATE kanban_sync_state SET replay_restore_required=1')
        raise RestoreRequired('journal restore reconciliation required; custody LOSS or invalid anchor')
