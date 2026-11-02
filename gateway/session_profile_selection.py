"""Owner-committed selections of a managed session that follows its profile. [daveey]"""
import json
from dataclasses import asdict, replace

from hermes_state_runtime import RuntimeStoreError

PREFIX = 'gateway.profile_selection.v1.'


def released_pick(policy, model):
    """*policy* without its recorded pick (``model.selection_profile``), running *model*: what a pick
    that ended on a profile edit leaves. The followed ``model:`` block already replaces the frozen one
    once the pick no longer holds, so dropping the marker changes nothing this turn runs; it only stops
    the pick from holding again when the profile later returns to the target it diverged from."""
    config = json.loads(policy.config_json)
    block = config.get('model')
    if isinstance(block, dict):
        config['model'] = {k: v for k, v in block.items() if k != 'selection_profile'}
    return replace(policy, model=model, config_json=json.dumps(config))


def commit_selection(authority, ref, policy, config, model, generation, *, ended=False):
    """Authorize this effective selection, not arbitrary exact-pin failover.

    Only the owner calls this with the single profile read it sends to the child. The durable
    launch policy must still be this following policy, at this owner epoch and runtime generation.
    Remember the selection independently of runtime usage stamps so rebuilding an unchanged
    selection never re-arms a consumed intent. *ended*: the policy's recorded pick no longer holds on
    that read (``LocalSessionPolicy.effective_selection``); the same transaction commits its end to the
    receipt, without a generation bump (nothing this generation runs changes), and the owner's live
    policy is republished from it. A concurrent prepared mutation then fails its snapshot check
    (``revision_conflict``) rather than writing over the end.
    """
    if not policy.follows_profile or policy.ignore_user_config:
        return
    from agent.model_routing_switch_intent import record_in_transaction
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_runtime import _epoch, _json
    provider = (config.get('model') or {}).get('provider') or ''
    selected = json.dumps([model, provider])
    expected = json.loads(json.dumps(asdict(policy)))
    released = json.loads(json.dumps(asdict(released_pick(policy, model)))) if ended else None

    def write(conn):
        _epoch(conn, authority.epoch)
        receipt = conn.execute('SELECT value FROM state_meta WHERE key=?',
                               (POLICY_PREFIX + ref.session_id,)).fetchone()
        row = conn.execute('SELECT runtime_generation FROM sessions WHERE id=?', (ref.session_id,)).fetchone()
        saved = json.loads(receipt[0]) if receipt is not None else None
        if saved is None or saved['policy'] != expected or row is None or row[0] != generation:
            raise RuntimeStoreError('revision_conflict')
        if released is not None:
            saved['policy'] = released
            conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                         (_json(saved), POLICY_PREFIX + ref.session_id))
        key = PREFIX + ref.session_id
        old = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if old is not None and old[0] == selected:
            return
        record_in_transaction(conn, ref.session_id, model, provider, generation)
        conn.execute('INSERT INTO state_meta (key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, selected))
    authority.db._execute_write(write)
    if released is not None:
        from gateway.session_local import publish_local_policy
        # The agent this build replaces already ran the followed route: nothing cached to evict.
        publish_local_policy(authority, ref.session_id, evict=False)
