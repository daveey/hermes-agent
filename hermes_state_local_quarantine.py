"""Terminal, fail-closed quarantine of an invalid stored local policy."""
import time

from hermes_state_local import POLICY_PREFIX
from hermes_state_runtime import RuntimeStoreError, _epoch


def quarantine_local_policy(db, *, epoch, session_id, expected, error_type):
    """Keep policy/history intact; fence only the exact receipt the owner validated.

    Returns whether this transaction recorded the first refusal. A stale owner or a
    concurrently changed receipt must retry inspection rather than quarantine new data.
    """
    def write(conn):
        _epoch(conn, epoch)
        key = POLICY_PREFIX + session_id
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if row is None:
            raise RuntimeStoreError('storage_unavailable')
        from hermes_state_policy_config import decode_receipt
        saved = decode_receipt(conn, row[0])
        if saved.get('quarantine') is not None:
            return False
        if saved != expected:
            raise RuntimeStoreError('admission_conflict')
        quarantine = {'reason': 'invalid_stored_policy', 'error_type': error_type,
                               'at': time.time()}
        # Metadata-only edit: preserve the stored representation and its reference.
        import json
        from hermes_state_runtime import _json
        stored = json.loads(row[0])
        stored['quarantine'] = quarantine
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(stored), key))
        return True
    return db._execute_write(write)
