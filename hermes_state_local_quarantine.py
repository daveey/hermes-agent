"""Terminal, fail-closed quarantine of an invalid stored local policy."""
import json
import time

from hermes_state_local import POLICY_PREFIX
from hermes_state_runtime import RuntimeStoreError, _epoch, _json


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
        saved = json.loads(row[0])
        if saved.get('quarantine') is not None:
            return False
        if saved != expected:
            raise RuntimeStoreError('admission_conflict')
        saved['quarantine'] = {'reason': 'invalid_stored_policy', 'error_type': error_type,
                               'at': time.time()}
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(saved), key))
        return True
    return db._execute_write(write)
