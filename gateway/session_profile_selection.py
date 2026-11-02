"""Owner-committed selections of a managed session that follows its profile."""
import json
from dataclasses import asdict

from hermes_state_runtime import RuntimeStoreError

PREFIX = 'gateway.profile_selection.v1.'


def commit_selection(authority, ref, policy, config, model, generation):
    """Authorize this effective selection, not arbitrary exact-pin failover.

    Only the owner calls this with the single profile read it sends to the child. The durable
    launch policy must still be this following policy. Remember the selection independently of
    runtime usage stamps so rebuilding an unchanged selection never re-arms a consumed intent.
    """
    if not policy.follows_profile or policy.ignore_user_config:
        return
    from agent.model_routing_switch_intent import record_in_transaction
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_runtime import _epoch
    provider = (config.get('model') or {}).get('provider') or ''
    selected = json.dumps([model, provider])
    expected = json.loads(json.dumps(asdict(policy)))

    def write(conn):
        _epoch(conn, authority.epoch)
        receipt = conn.execute('SELECT value FROM state_meta WHERE key=?',
                               (POLICY_PREFIX + ref.session_id,)).fetchone()
        row = conn.execute('SELECT runtime_generation FROM sessions WHERE id=?', (ref.session_id,)).fetchone()
        if (receipt is None or json.loads(receipt[0])['policy'] != expected
                or row is None or row[0] != generation):
            raise RuntimeStoreError('revision_conflict')
        key = PREFIX + ref.session_id
        old = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if old is not None and old[0] == selected:
            return
        record_in_transaction(conn, ref.session_id, model, provider, generation)
        conn.execute('INSERT INTO state_meta (key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, selected))
    authority.db._execute_write(write)
